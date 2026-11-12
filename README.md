[English | 中文](README.zh-CN.md)

<!-- Badge pins the CI workflow (.github/workflows/ci.yml: ruff + pytest, no API key, no
     outbound network). The badge 404s until the first push to the public repo — expected. -->
[![CI](https://github.com/Leewwp/mini-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Leewwp/mini-agent/actions/workflows/ci.yml)

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
| Sandbox | `src/mini_agent/sandbox.py` | Path fence, command normalization/blacklist/allowlist, minimized subprocess env, process-group timeout kill, shadow-git snapshot (`ShadowGitSnapshot`) that produces the run-over diff |
| Report | `src/mini_agent/report.py` | Five-element final report rendered for every terminal state; persists `report.md` and the snapshot diff artifact `diffs/changes.diff` |

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

The two built-in fixtures (`fixtures/tags-repo`, `fixtures/algo-repo`) each carry a README
with their rehearsal commands; see [Demos](#demos) for rehearsed outcomes.

No API key? Every code path is reproducible offline with the scripted FakeModel:

```bash
cp -r fixtures/tags-repo /tmp/tags-repo-demo
uv run mini-agent --repo /tmp/tags-repo-demo \
  --task "修复 normalize_tags 对空白和空标签的处理,运行测试验证" \
  --test-cmd "pytest -q" \
  --model fake:examples/fix-cycle.json
```

What a successful run looks like (real terminal output from a 2026-11-12 rehearsal with
`deepseek-flash`; model text streams to stdout, thinking to stderr, tool activity lines
per step):

```
[步骤 5] 调用工具 run_tests
全部 6 个用例通过。[步骤 6] 调用工具 finish
[步骤 6] 调用工具 run_tests
================ 终报 ================
终态:SUCCESS(原因:verification_passed),退出码 0
模型声明:修复了 tags_utils/normalize.py 中的 normalize_tags:原实现直接
raw.split(","),未处理空白与空标签。……修复后 6 个用例全部通过。
步数:6;token 用量:prompt 17070 / completion 573 / total 17643;模型:deepseek-flash
测试命令:pytest -q;最近一次结果:退出码 0(通过)
修改文件(5):… tags_utils/normalize.py(修改,+9/-3)
改动凭证:diffs/changes.diff(影子 git 快照)
会话日志:/tmp/…/mini-agent-runs/20261112-224238-567c/session.jsonl
======================================
```

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
| `MINI_AGENT_TOOL_DESC_LANG` | `zh` | Tool-description language: `zh` (default) or `en` (hot backup). Swaps only the seven tool descriptions — behavior and parameter schemas unchanged; the language in use is recorded in `session_meta`. |

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
| `ERROR` | 3 | `model_stalled`, `provider_error`, `response_truncated`, `fake_script_exhausted`, `test_command_not_found`, `interrupted` | Model stalled 2 rounds in a row, provider failed after retries, output truncated by `max_tokens` (`finish_reason=length`), the test command does not exist, or the run was interrupted manually (Ctrl+C — changes and logs are kept, nothing rolled back). |
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

A real run record of an impossible task ("make all tests pass without creating, modifying,
or deleting any file"), rehearsed twice with identical outcomes — the model diagnosed the
root cause, refused to touch the source, and declared failure:

```
终态:TESTS_FAILED(原因:model_declared_failure),退出码 1
模型声明:无法在约束下完成。测试失败根因在仓库源码 tags_utils/normalize.py:
normalize_tags 仅做 raw.split(","),未去除标签两端空白、未丢弃空标签……
修复必须修改该源文件……但任务硬性约束禁止创建、修改或删除仓库内任何文件……
因此不存在任何合规的修复途径。按要求未做任何文件改动,如实申报失败。
步数:4;token 用量:prompt 9338 / completion 352 / total 9690
```

With wording that does not mention the failure exit, the same scenario once ended in an
honest `MAX_STEPS` (exit 2) after the model spun on `run_command` attempts — also
reported as-is, with zero source changes. Full records for both flavors:
[`fixtures/REHEARSALS.md`](fixtures/REHEARSALS.md).

## Demos

Three rehearsed paths, each stable across 2 consecutive real-model runs (2026-11-12,
`deepseek-flash`; full records incl. steps/tokens/exit codes in
[`fixtures/REHEARSALS.md`](fixtures/REHEARSALS.md)):

1. **`fixtures/tags-repo`** — the the README's own `normalize_tags` bug-fix scenario.
   Baseline 3 failed / 3 passed → read code+tests → run baseline → fix (`strip` + drop
   empty tags) → 6 passed → verified `SUCCESS`, exit 0. 6 steps both runs,
   ~17.6k tokens.
2. **`fixtures/algo-repo`** — implement `spiral_order` against a hidden test set (11
   cases, edge cases not spoiled by the task text). `SUCCESS`, exit 0 (5 and 8 steps
   across the two runs; the longer run used `run_command` for ad-hoc checks, which do
   not count as verification).
3. **Honest failure** — the impossible task above: `finish(result="failed")` →
   `TESTS_FAILED`, exit 1, twice; plus one `MAX_STEPS`, exit 2, variant. Failure
   reported as-is, never fabricated.

### Recorded demos (asciinema)

Two terminal recordings of real `deepseek-flash` runs (2026-11-12), each against a
temporary `/tmp` copy of a fixture — never the fixture itself, and nothing is uploaded
anywhere: the `.cast` files are committed under [`docs/demos/`](docs/demos/).

```bash
asciinema play docs/demos/tags-repo-success.cast   # demo 1: fix normalize_tags → SUCCESS (6 steps)
asciinema play docs/demos/honest-failure.cast      # demo 3: impossible task → honest TESTS_FAILED
```

(Requires [asciinema](https://asciinema.org) locally, e.g. `brew install asciinema`;
`--idle-time-limit` is baked into the casts, so playback skips the waiting.)

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
uv run --extra dev pytest -q   # 217 tests; no API key, no network
```

The only test seam is the Provider boundary: a scripted FakeModel streams chunks shaped
exactly like the real DeepSeek streaming API (id only on the first fragment, arguments
split across fragments, interleaved indexes), so the streaming aggregation code paths are
exercised for real. Everything above that seam — CLI, loop, fence, stopping conditions,
report — is the production code under test.

> **Quality gate:** `ruff` (default ruleset) plus a GitHub Actions job
> (`.github/workflows/ci.yml`: ruff + pytest, no API key, no outbound network) keep the
> suite green on every push, and `tests/test_cli_smoke.py` exercises the installed
> `mini-agent` entry point through a real subprocess — exit codes, terminal report and
> session log included.

## Repository layout

```
├── README.md / README.zh-CN.md   # this file + Chinese twin (top-of-file switch links)
├── DESIGN.md                     # design rationale: the four elements, cuts, V2 seams
├── AI-USAGE.md                   # honest disclosure of AI usage in building this project
├── GLOSSARY.md                   # bilingual glossary of the seven core terms
├── LICENSE                       # MIT
├── pyproject.toml                # hatchling; console script `mini-agent`; deps: openai
├── src/mini_agent/               # cli / providers / loop / executor / tools / session / sandbox
├── tests/                        # FakeModel-driven pytest suite (no key, no network)
├── fixtures/                     # tags-repo / algo-repo demo repos + REHEARSALS.md records
├── examples/                     # fix-cycle.json: scripted FakeModel replay of the fix loop
├── docs/demos/                   # asciinema recordings (success + honest failure)
└── .github/workflows/            # ci.yml: ruff + pytest (single job, no key, no network)
```

## License

[MIT](LICENSE) — Copyright (c) 2026 Leewwp.

## See also

- [DESIGN.md](DESIGN.md) — how and why the task loop, tool boundary, stopping conditions,
  and result judgment are built; what was cut and why; the V2 extension seams.
- [AI-USAGE.md](AI-USAGE.md) — honest disclosure of how AI assistants were used to build
  this project (and what the human did).
- [GLOSSARY.md](GLOSSARY.md) — the seven terms this project uses, EN / 中文.
