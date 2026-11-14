[English | 中文](README.zh-CN.md)

[![CI](https://github.com/Leewwp/mini-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Leewwp/mini-agent/actions/workflows/ci.yml)

# mini-agent

A minimal, self-built coding agent for the terminal — now with an interactive REPL as the
primary way to use it. Point it at a repository and either start a continuous chat session
(bare `mini-agent`) or hand it a one-shot task (`--task`); it autonomously reads the code,
edits files, runs your pre-configured test command, feeds failures back into the
conversation, and keeps fixing until the tests pass — or reports honestly that it could not.

```bash
# one-shot batch run (core scenario, unchanged)
mini-agent --repo /tmp/tags-repo-demo --task "修复 normalize_tags…" --test-cmd "pytest -q"

# or: interactive REPL session (primary usage — continuous chat, tools visible)
mini-agent --repo /tmp/tags-repo-demo --test-cmd "pytest -q"
```

The point of this project is that the four things an
agent must explain — **the task loop, the tool-execution boundary, the stopping conditions,
and the result judgment** — are implemented from scratch in this repository
(`src/mini_agent/loop.py` and friends). It is *not* a wrapper around an existing coding
agent (Claude Code, Codex CLI, …): the only SDKs in the dependency tree are the plain
`openai` client (dumb OpenAI-compatible transport) and `prompt_toolkit` (REPL input box).

- Self-written agent loop: streaming output, one LLM round-trip = one step, at most 8 tool
  calls per turn, stall detection (`model_stalled`); turn-scoped variant for the REPL
  (`run_turn()` / `TurnState`, batch path untouched).
- 7 tools: `list_files` / `read_file` / `write_file` / `search` / `run_command` /
  `run_tests` / `finish`.
- **Interactive REPL**: inline-scrolling Claude Code-style session — streaming replies,
  colored tool-result lines, spinner, `/help` `/connect` `/new` `/compact` `/exit` slash
  commands, per-turn status lines, double-Ctrl+C to exit. `/connect` supplies the API key
  in-session (masked input; memory-only — never persisted to disk, history, or logs).
- **Project memory**: repo-root `AGENTS.md` injected into the system prompt (REPL-only,
  32KiB cap, append-don't-overwrite guidance).
- **Manual compaction**: `/compact` replaces old history with an LLM semantic summary +
  the last 2 complete turns; any failure leaves history untouched. Mechanical auto-trim
  remains as a safety valve.
- **Session resume**: `mini-agent --resume <session-dir>` continues a previous
  conversation in a new session directory (old log read-only).
- Guards that are honest about being guards: a path fence, a short command blacklist plus
  an interpreter/build allowlist, and a minimized subprocess environment — threat model is
  "prevent accidents, not adversaries" (see [DESIGN.md](DESIGN.md#7-security-boundary--threat-model)).
- Six terminal states with distinct exit codes; **failure is a legal outcome** and is
  reported as-is, never rolled back silently.
- Verify-on-finish: `finish(result="success")` is only believed after the test command is
  re-run right then and passes.
- Append-only JSONL session log (`session.jsonl`) recording model conditions, every LLM
  request/response, every tool call/result, test runs, context trims, compactions, and the
  final report.
- Test suite runs with **no API key and no network** (FakeModel injected via
  `--model fake:<script.json>`); REPL tests are fully injectable (StringIO/scripted input)
  and never need a TTY.

The CLI conversation layer (prompts, tool descriptions, terminal output) is in Chinese by
design — this matches the project's Chinese-first demo scenario.

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

export MINI_AGENT_API_KEY=sk-...   # required for batch (--task; fail-fast exit 5 without it).
                                   # The REPL starts without a key — run /connect there instead.
                                   # --model fake: works with no key. See "Model configuration".
```

### Interactive REPL (primary usage)

Run against a **temporary copy** of a fixture repo (never the fixture itself — the agent
really writes into `--repo` and does not roll back). Bare `mini-agent` (no `--task`) starts
the session; `--repo` defaults to the git root of your current directory:

```bash
cp -r fixtures/tags-repo /tmp/tags-repo-demo
cd /tmp/tags-repo-demo
uv run --project /path/to/mini-agent mini-agent --test-cmd "pytest -q"
```

You get a chat: type messages (Enter sends; Esc+Enter inserts a newline), watch tool calls
happen with visible results, and use slash commands:

| Command | Meaning |
|---|---|
| `/help` | show the command table |
| `/connect` | supply the API key in-session (masked input; memory-only, never written to disk/history/logs; running it again rotates the key) |
| `/new` (alias `/clear`) | start a fresh session: clear history, reload `AGENTS.md`, rotate log & snapshot |
| `/compact [focus]` | compact the conversation into a semantic summary + the last 2 turns |
| `/exit` | write the session report (`report.md` + diff evidence) and exit (also Ctrl+D or double Ctrl+C within 2s) |

What a REPL session looks like (real `deepseek-flash` run; see [Demos](#demos) for the
recorded cast):

```
❯ 修复 normalize_tags 对空白和空标签的处理,跑测试验证
· [步骤 1] 调用工具 read_file
✓ read_file · def normalize_tags(raw): · 3ms
…(模型继续读测试、改代码)…
✓ run_tests · 退出码 0 · 842ms
✓ 完成 · 6 步 · 17.6k tok
模型声明:修复了 normalize_tags:strip + 丢弃空标签,6 个用例通过
❯ /exit
================ 会话终报 ================
会话状态:正常退出;轮次 1;总步数 6;模型 deepseek-flash
token 用量:prompt 17070 / completion 573 / total 17643
修改文件(1):  tags_utils/normalize.py(修改,+9/-3)
…
```

REPL sessions live under `~/.mini-agent/sessions/<repo-slug>/` (override with
`--runs-dir`); each `session.jsonl` is append-only and supports `--resume`:

```bash
mini-agent --resume            # list the 3 most recent sessions for this repo
mini-agent --resume <dir>      # continue one in a NEW session directory (old log untouched)
```

If the repo has an `AGENTS.md` at its root, its content (≤32KiB) is injected into the
system prompt as project instructions, and `/new` reloads it. Batch mode (`--task`) is
deliberately unaffected by memory injection.

### One-shot batch (core scenario)

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
# REPL offline too (pipe input; /exit at the end):
printf '看看仓库结构\n/exit\n' | uv run mini-agent --repo /tmp/tags-repo-demo \
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
mini-agent [--repo <path>] [--task <text>] [--resume [SESSION_DIR]] [--runs-dir DIR]
           [--verbose] [--max-steps N] [--test-cmd CMD] [--model NAME]
           [--timeout SECONDS] [--max-context-tokens N] [--max-tokens N] [--thinking]
```

Mode dispatch: `--task` → one-shot batch (unchanged); `--resume <dir>` → resume a session
into the REPL; bare command → interactive REPL. `--task` and `--resume` are mutually
exclusive (exit 5).

| Flag | Default | Meaning |
|---|---|---|
| `--repo` | git root of cwd, else cwd | Target repository directory (explicit flag always wins). The agent really writes here; changes are never auto-rolled back. |
| `--task` | none | Natural-language task (Chinese works best). Present → batch mode. |
| `--resume` | none | Session directory to resume (new dir is written, old log read-only). Bare `--resume` lists the 3 most recent sessions for this repo and exits. |
| `--runs-dir` | `~/.mini-agent/sessions` | REPL session root (batch keeps `./mini-agent-runs` + temp fallback). |
| `--verbose` | off | REPL tool-result lines show full output instead of the summary line. |
| `--max-steps` | `20` batch / `50` REPL | Step budget. One LLM round-trip counts as one step. Per-turn in the REPL; at 75% a one-shot reminder line is shown, and after hitting the cap typing "继续" (continue) resumes with full history. |
| `--test-cmd` | none | The repository's pre-configured test command (e.g. `pytest -q`). Without it, success cannot be verified (→ `UNVERIFIED`). |
| `--model` | `deepseek-flash` | Model name, or `fake:<script.json>` to inject the scripted FakeModel (no key, no network). |
| `--timeout` | `600` | Overall deadline in seconds. Per-turn in the REPL. |
| `--max-context-tokens` | `60000` | History-trim threshold, in tokens (triggered by the previous response's `usage.prompt_tokens`). |
| `--max-tokens` | `32768` | Per-response output cap. DeepSeek's documented maximum is 384K; lower it explicitly on endpoints with a smaller output cap. |
| `--thinking` | off | Enable model thinking mode. Reasoning is displayed (stderr, dimmed in REPL) only — it never enters the message history. |

REPL exit codes: `/exit` (incl. Ctrl+D / double Ctrl+C) → 0 even if the last turn failed;
config errors (no API key, bad `--resume`, conflicting flags) → 5; unexpected internal
errors → 3 with a session report. Batch exit codes: see the table below.

Model configuration via environment (precedence: flag > env > default):

| Env var | Default | Meaning |
|---|---|---|
| `MINI_AGENT_API_KEY` | — | API key. Required for batch (`--task`, fail-fast exit 5). The REPL starts without it: plain messages are gated until `/connect` supplies the key in-session (memory-only). `--model fake:` needs no key. |
| `MINI_AGENT_BASE_URL` | `https://api.deepseek.com` | Any OpenAI-compatible endpoint (DeepSeek verified; Zhipu/MiniMax best-effort). |
| `MINI_AGENT_MODEL` | `deepseek-flash` | Model name override. |
| `MINI_AGENT_TOOL_DESC_LANG` | `zh` | Tool-description language: `zh` (default) or `en` (hot backup). Swaps only the seven tool descriptions — behavior and parameter schemas unchanged; the language in use is recorded in `session_meta`. |

Runtime dependencies: `openai` (Apache-2.0) and `prompt_toolkit` (BSD-3-Clause; REPL input
box). Third-party code reuse and licenses: [NOTICE](NOTICE).

Other request defaults (from `cli.py` / `providers.py`): `temperature=0.0`,
`max_tokens=32768` (tunable via `--max-tokens`; a truncated response fails honestly as
`ERROR/response_truncated`, naming the current cap), streaming
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

A core design principle: failure and inability to continue are legal results, but
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

1. **`fixtures/tags-repo`** — the README's own `normalize_tags` bug-fix scenario.
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

Three terminal recordings of real `deepseek-flash` runs, each against a temporary `/tmp`
copy of a fixture — never the fixture itself, and nothing is uploaded anywhere: the `.cast`
files are committed under [`docs/demos/`](docs/demos/).

```bash
asciinema play docs/demos/repl-session.cast        # REPL: multi-turn chat + tool results + /compact + /exit report
asciinema play docs/demos/tags-repo-success.cast   # demo 1: fix normalize_tags → SUCCESS (6 steps)
asciinema play docs/demos/honest-failure.cast      # demo 3: impossible task → honest TESTS_FAILED
```

(Requires [asciinema](https://asciinema.org) locally, e.g. `brew install asciinema`;
`--idle-time-limit` is baked into the casts, so playback skips the waiting.)

## Observability

Every batch run writes `mini-agent-runs/<YYYYmmdd-HHMMSS>-<suffix>/session.jsonl` next to
your working directory (moved to the system temp dir if that would land inside `--repo`);
REPL sessions live under `~/.mini-agent/sessions/<repo-slug>/…/session.jsonl`. The
log is append-only and flushed after every event — killing the process still leaves a
readable log. It never contains the API key or request headers. Event types:

`session_meta`, `llm_request`, `llm_response`, `tool_call`, `tool_result`, `test_run`,
`context_trim`, `final_report`, plus REPL-only `turn_start` / `turn_result` / `turn_end`
(full model view at turn end — the resume data source), `memory_loaded`,
`compact_request` / `compact_response` / `compact`

Notable details: `llm_request` stores the actual messages sent (that is what context
reconstruction needs); `test_run` stores full stdout/stderr while the model sees a clipped
version; `context_trim` stores the removed messages verbatim; `compact` stores the replaced
messages verbatim — trimming and compaction are both reversible from the log alone.

## Testing

```bash
uv run --extra dev pytest -q   # 345 tests; no API key, no network, no TTY
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
├── DESIGN.md                     # design rationale: the four elements, cuts, seams, REPL, memory & compaction
├── AI-USAGE.md                   # honest disclosure of AI usage in building this project
├── GLOSSARY.md                   # bilingual glossary (core 7 terms + REPL terms)
├── NOTICE                        # third-party code reuse & licenses (aider Apache-2.0 etc.)
├── LICENSE                       # MIT
├── pyproject.toml                # hatchling; console script `mini-agent`; deps: openai, prompt_toolkit
├── src/mini_agent/               # cli / providers / loop / executor / tools / session / sandbox
│   └── repl/                     # REPL: app / render / prompt / waiting / orchestrator / compact
│   └── memory.py / resume.py     # AGENTS.md project memory; session resume
├── tests/                        # FakeModel-driven pytest suite (no key, no network, no TTY)
├── fixtures/                     # tags-repo / algo-repo demo repos + REHEARSALS.md records
├── examples/                     # fix-cycle.json + repl-demo.sh (recorded-demo rehearsal script)
├── docs/demos/                   # asciinema recordings (REPL + batch success + honest failure)
└── .github/workflows/            # ci.yml: ruff + pytest (single job, no key, no network)
```

## License

[MIT](LICENSE) — Copyright (c) 2026 Leewwp. Third-party notices: [NOTICE](NOTICE).

## See also

- [DESIGN.md](DESIGN.md) — how and why the task loop, tool boundary, stopping conditions,
  and result judgment are built; what was cut and why; the V2 extension seams.
- [AI-USAGE.md](AI-USAGE.md) — honest disclosure of how AI assistants were used to build
  this project (and what the human did).
- [GLOSSARY.md](GLOSSARY.md) — the seven terms this project uses, EN / 中文.
