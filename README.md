<!-- SKELETON: drafted post-03, finalize in ticket 07 -->

[English | 中文](README.zh-CN.md)

<!-- TODO(06): CI badge placeholder — workflow filename to be pinned when ticket 06 lands the
     GitHub Actions job (ruff + pytest, no API key, no outbound network). -->
[![CI](https://github.com/Leewwp/mini-agent/actions/workflows/WORKFLOW-NAME-TODO/badge.svg)](https://github.com/Leewwp/mini-agent/actions/workflows/WORKFLOW-NAME-TODO)

# mini-agent

A minimal, self-built coding agent for the terminal. Point it at a repository path and a
natural-language task; it autonomously reads the code, edits files, runs your pre-configured
test command, feeds failures back into the conversation, and keeps fixing until the tests
pass — or reports honestly that it could not.

This is an self-built coding agent. The point of the project is that the four things an
agent must explain — **the task loop, the tool-execution boundary, the stopping conditions,
and the result judgment** — are implemented from scratch in this repository
(`src/mini_agent/loop.py` and friends). It is *not* a wrapper around an existing coding
agent (Claude Code, Codex CLI, …): the only SDK in the dependency tree is the plain
`openai` client, used as a dumb OpenAI-compatible transport.

- Self-written agent loop: streaming output, one LLM round-trip = one step, at most 8 tool
  calls per turn, stall detection (`model_stalled`).
- 7 tools: `list_files` / `read_file` / `write_file` / `search` / `run_command` /
  `run_tests` / `finish`.
- Guards that are honest about being guards: a path fence, a short command blacklist plus
  an interpreter/build allowlist, and a minimized subprocess environment — threat model is
  "prevent accidents, not adversaries" (see [DESIGN.md](DESIGN.md#7-security-boundary--threat-model)).
- Six terminal states with distinct exit codes; **failure is a legal outcome** and is
  reported as-is, never rolled back silently.
- Verify-on-finish: `finish(result="success")` is only believed after the test command is
  re-run right then and passes.
- Append-only JSONL session log (`session.jsonl`) recording model conditions, every LLM
  request/response, every tool call/result, test runs, context trims, and the final report.
- Test suite runs with **no API key and no network** (FakeModel injected via
  `--model fake:<script.json>`).

The CLI conversation layer (prompts, tool descriptions, terminal output) is in Chinese by
design — this matches the original the project's demo requirements.

## Architecture

```
                        ┌─────────────────────────────────────────────┐
                        │ cli.py                                      │
                        │  argparse → Config (flag > env > default)   │
                        │  provider factory + session logger wiring   │
                        └──────────────────────┬──────────────────────┘
                                               │ config + provider
                                               ▼
┌───────────────────┐   messages + tools  ┌─────────────────────────────────┐
│ providers.py      │ ◄────────────────── │ loop.py — AgentLoop (the core)  │
│ Seam #1 Provider  │ ──────────────────► │  • 1 LLM round-trip = 1 step    │
│ OpenAI-compatible │  normalized stream  │  • ≤ 8 tool calls per turn      │
│ streaming client  │  events + response  │  • reads result metadata only,  │
└───────────────────┘                     │    never tool names             │
                                          │  • verify-on-finish             │
                                          └──────┬────────────────┬─────────┘
                                    tool call    │                │ every event
                                                 ▼                ▼
                                 ┌───────────────────────┐ ┌──────────────────┐
                                 │ executor.py           │ │ session.py       │
                                 │ Seam #3 HookChain     │ │ Seam #4          │
                                 │  (no-op middleware)   │ │ append-only JSONL│
                                 │ clip ≤8KB head/tail   │ │ flush per event  │
                                 └──────────┬────────────┘ └──────────────────┘
                                            │ registry.get(name)
                                            ▼
┌──────────────────────────────────────────────────────┐ ┌───────────────────────┐
│ tools/ — Seam #2 ToolRegistry                        │ │ sandbox.py            │
│  list_files / read_file / write_file / search        │◄┤  PathFence            │
│  run_command (allowlist) / run_tests (verifier)      │ │  shlex + blacklist    │
│  finish(result="success"|"failed")                   │ │  minimal env          │
└──────────────────────────────────────────────────────┘ │  process-group kill   │
                                                         └───────────────────────┘
                                            │ when the loop ends
                                            ▼
                                 ┌───────────────────────────────┐
                                 │ report.py — five-element      │
                                 │ final report + shadow-git     │
                                 │ diff evidence                 │
                                 └───────────────────────────────┘
```

| Box in the diagram | Module | Responsibility |
|---|---|---|
| CLI | `src/mini_agent/cli.py` | Argument parsing, config resolution, assembly of registry / executor / loop, terminal report |
| Agent loop | `src/mini_agent/loop.py` | The hand-written core: steps, stall detection, terminal-state mapping, verify-on-finish, history trimming |
| Provider | `src/mini_agent/providers.py` | Seam #1. OpenAI-compatible streaming client, normalized event stream, pure-function tool-call delta aggregation, retry classification, FakeModel |
| Tools | `src/mini_agent/tools/` | Seam #2. `Tool` protocol + `ToolRegistry` (runtime registration); `fs.py`, `exec.py`, `finish.py` |
| Executor + hooks | `src/mini_agent/executor.py` | Seam #3. Runs one tool call through the hook chain, clips results for the model, writes `tool_call`/`tool_result` events |
| Session log | `src/mini_agent/session.py` | Seam #4. Append-only JSONL, flush after every event, never rewritten |
| Sandbox | `src/mini_agent/sandbox.py` | Path fence, command normalization/blacklist/allowlist, minimized subprocess env, process-group timeout kill |
| Report | `src/mini_agent/report.py` | **TODO(04):** module lands with ticket 04 (shadow-git snapshot + five-element final report). The diagram edge is already correct; the box is not in the tree yet. |

## Quick start

Requires Python ≥ 3.10 and [uv](https://docs.astral.sh/uv/) (or plain pip).

```bash
git clone https://github.com/Leewwp/mini-agent.git
cd mini-agent

# with uv
uv venv && uv pip install -e ".[dev]"

# or with pip
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

export MINI_AGENT_API_KEY=sk-...   # DeepSeek by default; see "Model configuration"
```

Run it against a **temporary copy** of a fixture repo (never the fixture itself — the agent
really writes into `--repo` and does not roll back):

```bash
cp -r fixtures/tags-repo /tmp/tags-repo-demo
uv run mini-agent \
  --repo /tmp/tags-repo-demo \
  --task "修复 normalize_tags 对空白和空标签的处理,运行测试验证" \
  --test-cmd "pytest -q" \
  --max-steps 20
```

> **TODO(05):** `fixtures/` lands with ticket 05. Until then, point `--repo` at any local
> throwaway directory that has a test command you can pass via `--test-cmd`.

No API key? Every code path is reproducible offline with the scripted FakeModel:

```bash
uv run mini-agent --repo /tmp/some-dir \
  --task "..." --test-cmd "pytest -q" \
  --model fake:examples/fix-cycle.json
```

> **TODO(05):** the example FakeModel script referenced above ships with ticket 05's
> rehearsal material.

What a successful run looks like today (terminal output is Chinese by design; model text
streams to stdout, thinking to stderr):

```
[步骤 1] 调用工具 list_files
[步骤 2] 调用工具 run_tests
…model text streams here…
终态:SUCCESS(原因:verification_passed),退出码 0
模型声明:已修复 normalize_tags 并通过全部测试
步数:5;token 用量:prompt 12345 / completion 678 / total 13023
模型:deepseek-flash;会话日志:/abs/mini-agent-runs/20261112-220000-a1b2/session.jsonl
```

> **TODO(04):** the five-element final report (modified files + diff summary, test command
> and latest result, steps, log path, terminal state) and `report.md` in the session
> directory land with ticket 04. The lines above are the current report printed by
> `cli.py`.

## CLI reference

```
mini-agent --repo <path> --task <text> [--max-steps N] [--test-cmd CMD] [--model NAME]
           [--timeout SECONDS] [--max-context-tokens N] [--thinking]
```

| Flag | Default | Meaning |
|---|---|---|
| `--repo` | *(required)* | Target repository directory. The agent really writes here; changes are never auto-rolled back. |
| `--task` | *(required)* | Natural-language task (Chinese works best). |
| `--max-steps` | `20` | Step budget. One LLM round-trip counts as one step. |
| `--test-cmd` | none | The repository's pre-configured test command (e.g. `pytest -q`). Without it, success cannot be verified (→ `UNVERIFIED`). |
| `--model` | `deepseek-flash` | Model name, or `fake:<script.json>` to inject the scripted FakeModel (no key, no network). |
| `--timeout` | `600` | Overall deadline in seconds. |
| `--max-context-tokens` | `60000` | History-trim threshold, in tokens (triggered by the previous response's `usage.prompt_tokens`). |
| `--thinking` | off | Enable model thinking mode. Reasoning is displayed (stderr) only — it never enters the message history. |

Model configuration via environment (precedence: flag > env > default):

| Env var | Default | Meaning |
|---|---|---|
| `MINI_AGENT_API_KEY` | — | API key. Required unless `--model fake:` is used. |
| `MINI_AGENT_BASE_URL` | `https://api.deepseek.com` | Any OpenAI-compatible endpoint (DeepSeek verified; Zhipu/MiniMax best-effort). |
| `MINI_AGENT_MODEL` | `deepseek-flash` | Model name override. |

Other request defaults (from `cli.py`): `temperature=0.0`, `max_tokens=8192`, streaming
always on, `stream_options.include_usage=true` (token usage falls back to an estimate,
marked `(估算值)` in the report, when the provider omits it). Retries: only 429/5xx/network
errors, exponential backoff, 2 retries; request errors (400/401/402/422 …) fail fast with
an actionable Chinese hint.

## Terminal states and exit codes

Six terminal states (the loop always ends in one of them and prints a report); exit code 5
is a CLI-level configuration error, not a loop state.

| State | Exit code | `reason` values | Meaning |
|---|---|---|---|
| `SUCCESS` | 0 | `verification_passed` | Model declared success **and** the verify-on-finish re-run of the test command passed. |
| `TESTS_FAILED` | 1 | `verification_failed`, `model_declared_failure` | Tests failed at finish-time re-verification (steps exhausted after a rejected finish), or the model declared `finish(result="failed")`. |
| `MAX_STEPS` | 2 | `steps_exhausted` | Step budget ran out. |
| `ERROR` | 3 | `model_stalled`, `provider_error`, `response_truncated`, `fake_script_exhausted`, `test_command_not_found` | Model stalled 2 rounds in a row, provider failed after retries, output truncated by `max_tokens` (`finish_reason=length`), or the test command does not exist. |
| `TIMEOUT` | 4 | `deadline_exceeded` | Overall deadline exceeded. |
| `UNVERIFIED` | 6 | `no_test_command` | Model declared success but no `--test-cmd` was configured — reported honestly instead of guessing. |
| *(config error)* | 5 | — | Bad CLI usage: missing API key, `--repo` not a directory, invalid `fake:` script, … |

## Honest failure is a legal outcome

The design principle is plain: failure and inability to continue are legal results, but
they must be reported truthfully. mini-agent leans into that:

- `finish(result="failed")` is a first-class outcome (`TESTS_FAILED` / `model_declared_failure`).
- A rejected `finish(result="success")` (re-verification failed) is fed back into the
  conversation so the model can keep fixing; if steps run out, the run ends
  `TESTS_FAILED`, not fake success.
- Changes are **never** auto-rolled-back. If the agent broke something, the report says so
  and the diff is the evidence.
- If the model writes a file *after* tests pass and then finishes, the finish-time
  re-verification catches it — passing an outdated test result cannot buy a `SUCCESS`.

> **TODO(05):** "An impossible task, honestly reported" — a real run record
> (terminal output + session log path) of an unsolvable task ending in
> `finish(failed)` / `MAX_STEPS` goes here after the ticket-05 rehearsal.

## Demos

> **TODO(05):** three rehearsed demo paths with expected transcripts land here:
>
> 1. `fixtures/tags-repo` — the the README's own `normalize_tags` bug-fix scenario (edit →
>    test → fail → fix → pass → verified `SUCCESS`).
> 2. `fixtures/algo-repo` — implement a function against hidden tests; uses `run_command`
>    for ad-hoc script runs plus `run_tests` for verification.
> 3. Honest failure — an impossible task ending in `finish(result="failed")` or
>    `MAX_STEPS`, demonstrating that failure is reported as-is.
>
> asciinema recordings (success path + honest-failure path) will be embedded here as well.

## Observability

Every run writes `mini-agent-runs/<YYYYmmdd-HHMMSS>-<suffix>/session.jsonl` next to your
working directory (moved to the system temp dir if that would land inside `--repo`). The
log is append-only and flushed after every event — killing the process still leaves a
readable log. It never contains the API key or request headers. Event types:

`session_meta`, `llm_request`, `llm_response`, `tool_call`, `tool_result`, `test_run`,
`context_trim`, `final_report`

Notable details: `llm_request` stores the actual messages sent (that is what context
reconstruction needs); `test_run` stores full stdout/stderr while the model sees a clipped
version; `context_trim` stores the removed messages verbatim, so trimming is reversible
from the log alone.

## Testing

```bash
uv run --extra dev pytest -q   # 179 tests at ticket-03 closeout; no API key, no network
```

The only test seam is the Provider boundary: a scripted FakeModel streams chunks shaped
exactly like the real DeepSeek streaming API (id only on the first fragment, arguments
split across fragments, interleaved indexes), so the streaming aggregation code paths are
exercised for real. Everything above that seam — CLI, loop, fence, stopping conditions,
report — is the production code under test.

> **TODO(06):** ruff + GitHub Actions CI badge and the CLI subprocess smoke test land with
> ticket 06.

## Repository layout

```
├── README.md / README.zh-CN.md   # this file + Chinese twin (top-of-file switch links)
├── DESIGN.md                     # design rationale: the four elements, cuts, V2 seams
├── GLOSSARY.md                   # bilingual glossary of the seven core terms
├── pyproject.toml                # hatchling; console script `mini-agent`; deps: openai
├── src/mini_agent/               # cli / providers / loop / executor / tools / session / sandbox
├── tests/                        # FakeModel-driven pytest suite (no key, no network)
├── fixtures/                     # TODO(05): tags-repo, algo-repo demo repositories
└── .github/workflows/            # TODO(06): ruff + pytest CI
```

## License

> **TODO(07):** MIT License file lands with the release ticket.

## See also

- [DESIGN.md](DESIGN.md) — how and why the task loop, tool boundary, stopping conditions,
  and result judgment are built; what was cut and why; the V2 extension seams.
- [GLOSSARY.md](GLOSSARY.md) — the seven terms this project uses, EN / 中文.
