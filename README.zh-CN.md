[English | 中文](README.md)

<!-- 徽章指向 CI 工作流(.github/workflows/ci.yml:ruff + pytest,无 API key、无网络出站)。
     公开仓库首次推送前该徽章 404 属预期。 -->
[![CI](https://github.com/Leewwp/mini-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Leewwp/mini-agent/actions/workflows/ci.yml)

# mini-agent

一个自研的本地命令行编码 Agent:输入仓库路径和自然语言任务,它自主阅读代码、
修改文件、运行你预配置的测试命令,把失败输出回填给模型继续修复,直到测试通过
——或者如实报告失败。

这是一个从零自研的迷你编码智能体。项目的核心在于 **任务循环、工具执行边界、停止
条件、结果判断** 四要素全部在本仓库内自研(`src/mini_agent/loop.py` 等)并能逐点
解释。它**不是**现成 Coding Agent(Claude Code、Codex CLI 等)的包装器:依赖树里
唯一的 SDK 是 `openai` 客户端,只当 OpenAI 兼容传输层用。

- 自研 Agent 循环:流式输出;一次 LLM 往返计 1 步;单轮工具调用 ≤8 个;空转检测
  (`model_stalled`)。
- 7 个工具:`list_files` / `read_file` / `write_file` / `search` / `run_command` /
  `run_tests` / `finish`。
- 诚实的护栏:路径围栏、短黑名单 + 解释器/构建类 allowlist、子进程环境最小化
  ——威胁模型是「防误伤、不防对抗」(见 [DESIGN.md](DESIGN.md#7-安全边界与威胁模型))。
- 六终态各有退出码;**失败也是合法结果**,如实报告,绝不静默回滚。
- 声明时复验(Verify-on-Finish):`finish(result="success")` 只有当场重跑测试命令
  并通过才被采信。
- append-only JSONL 会话日志(`session.jsonl`):模型条件、每次 LLM 请求/响应、
  每次工具调用/结果、测试运行、上下文裁剪、终报全部入档。
- 测试套件**无 API key、无网络**即可全绿(FakeModel 经 `--model fake:<script.json>`
  注入)。

系统提示词、工具描述与界面输出为中文,这是对齐项目需求演示要求的设计决定。

## 架构

```
                        ┌─────────────────────────────────────────────┐
                        │ cli.py                                      │
                        │  argparse → Config(flag > env > 默认值)     │
                        │  Provider 工厂 + 会话日志接线               │
                        └──────────────────────┬──────────────────────┘
                                               │ config + provider
                                               ▼
┌───────────────────┐  消息与工具定义    ┌─────────────────────────────────┐
│ providers.py      │ ◄───────────────── │ loop.py — AgentLoop(核心)       │
│ 接缝① Provider    │                    │  • 1 次 LLM 往返 = 1 步         │
│ OpenAI 兼容       │ ─────────────────► │  • 单轮工具调用 ≤8 个           │
│ 流式客户端        │ 归一化事件流+响应  │  • 只读结果元数据,              │
└───────────────────┘                    │    绝不特判工具名               │
                                         │  • 声明时复验                   │
                                         └──────┬────────────────┬─────────┘
                                      工具调用  │                 │ 全部事件
                                                ▼                ▼
                                 ┌───────────────────────┐ ┌────────────────────┐
                                 │ executor.py           │ │ session.py         │
                                 │ 接缝③ 钩子链          │ │ 接缝④              │
                                 │  (空链 no-op)         │ │ append-only JSONL  │
                                 │ 截断 ≤8KB 保头尾      │ │ 每事件 flush       │
                                 └───────────┬───────────┘ └────────────────────┘
                                             │ registry.get(name)
                                             ▼
┌──────────────────────────────────────────────────────┐  ┌─────────────────────────┐
│ tools/ — 接缝② ToolRegistry                          │  │ sandbox.py              │
│  list_files / read_file / write_file / search        │◄┤   路径围栏               │
│  run_command(allowlist)/ run_tests(验证器)           │  │  shlex + 黑名单         │
│  finish(result="success"|"failed")                   │  │  env 最小化             │
└──────────────────────────────────────────────────────┘  │  进程组击杀             │
                                                          └─────────────────────────┘
                                 循环结束时  │
                                             ▼
                                 ┌───────────────────────────────┐
                                 │ report.py — 五要素终报        │
                                 │ + 影子 git 快照 diff 凭证     │
                                 └───────────────────────────────┘
```

| 图中模块 | 代码位置 | 职责 |
|---|---|---|
| CLI | `src/mini_agent/cli.py` | 参数解析、配置解析(flag > 环境变量 > 默认值)、装配注册表/执行器/循环、终端终报 |
| Agent 循环 | `src/mini_agent/loop.py` | 手写核心:计步、空转检测、终态映射、声明时复验、历史裁剪 |
| Provider | `src/mini_agent/providers.py` | 接缝①。OpenAI 兼容流式客户端、归一化事件流、纯函数 tool-call 增量聚合、重试分类、FakeModel |
| 工具 | `src/mini_agent/tools/` | 接缝②。`Tool` 协议 + `ToolRegistry`(运行期注册);`fs.py`、`exec.py`、`finish.py` |
| 执行器 + 钩子 | `src/mini_agent/executor.py` | 接缝③。单个工具调用过钩子链执行、为模型截断结果、写 `tool_call`/`tool_result` 事件 |
| 会话日志 | `src/mini_agent/session.py` | 接缝④。append-only JSONL、每事件 flush、永不改写 |
| 沙箱 | `src/mini_agent/sandbox.py` | 路径围栏、命令归一化/黑名单/allowlist、子进程 env 最小化、进程组超时击杀 |
| 终报 | `src/mini_agent/report.py` | 所有终态共用的五要素终报;影子 git 快照 diff 凭证(`diffs/changes.diff`) |

## 快速开始

需要 Python ≥ 3.10 与 [uv](https://docs.astral.sh/uv/)(或普通 pip)。

```bash
git clone https://github.com/Leewwp/mini-agent.git
cd mini-agent

# 用 uv
uv venv && uv pip install -e ".[dev]"

# 或用 pip
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

export MINI_AGENT_API_KEY=sk-...   # 默认 DeepSeek;换端点见下表
```

把 fixture 复制到临时目录再运行(agent 真的会写入 `--repo`,且不回滚):

```bash
cp -r fixtures/tags-repo /tmp/tags-repo-demo
uv run mini-agent \
  --repo /tmp/tags-repo-demo \
  --task "修复 normalize_tags 对空白和空标签的处理,运行测试验证" \
  --test-cmd "pytest -q" \
  --max-steps 20
```

两个内置 fixture(`fixtures/tags-repo` 修 bug 场景、`fixtures/algo-repo` 算法题
隐藏测试)各带 README 说明彩排命令;彩排实录见
[`fixtures/REHEARSALS.md`](fixtures/REHEARSALS.md) 与[演示](#演示)一节。

没有 key?全部代码路径都能用脚本化 FakeModel 离线复现:

```bash
cp -r fixtures/tags-repo /tmp/tags-repo-demo
uv run mini-agent --repo /tmp/tags-repo-demo \
  --task "修复 normalize_tags 对空白和空标签的处理,运行测试验证" \
  --test-cmd "pytest -q" \
  --model fake:examples/fix-cycle.json
```

一次成功运行长什么样(2026-11-12 真实彩排的终端输出;模型正文流式上屏
stdout、思考走 stderr、每步一行工具活动):

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

## CLI 参数

```
mini-agent --repo <路径> --task <文本> [--max-steps N] [--test-cmd CMD] [--model NAME]
           [--timeout 秒] [--max-context-tokens N] [--thinking]
```

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--repo` | (必填) | 目标仓库目录;agent 真实写入,改动不自动回滚 |
| `--task` | (必填) | 自然语言任务(中文最佳) |
| `--max-steps` | `20` | 步数预算;一次 LLM 往返计 1 步 |
| `--test-cmd` | 无 | 预配置测试命令(如 `pytest -q`);缺省时成功无法验证(→ `UNVERIFIED`) |
| `--model` | `deepseek-flash` | 模型名;或 `fake:<script.json>` 注入脚本化 FakeModel(无 key、无网络) |
| `--timeout` | `600` | 整体超时秒数 |
| `--max-context-tokens` | `60000` | 历史裁剪阈值(token,由上一响应的 `usage.prompt_tokens` 触发) |
| `--thinking` | 关 | 开启思考模式;reasoning 仅展示(stderr),绝不进入消息历史 |

环境变量(优先级:flag > 环境变量 > 默认值):

| 环境变量 | 默认值 | 含义 |
|---|---|---|
| `MINI_AGENT_API_KEY` | — | API key;除 `--model fake:` 外必填 |
| `MINI_AGENT_BASE_URL` | `https://api.deepseek.com` | 任意 OpenAI 兼容端点(DeepSeek 已验证;智谱/MiniMax 尽力支持) |
| `MINI_AGENT_MODEL` | `deepseek-flash` | 模型名覆盖 |
| `MINI_AGENT_TOOL_DESC_LANG` | `zh` | 工具描述语言:`zh`(默认)或 `en`(热备);只换七条工具描述文案,行为与参数 schema 不动,当前语言记入 `session_meta` |

其余请求默认(见 `cli.py`):`temperature=0.0`、`max_tokens=8192`、恒流式、
`stream_options.include_usage=true`(provider 不回 usage 时降级为估算,报告标注
「(估算值)」)。重试:只对 429/5xx/网络错误指数退避重试 2 次;400/401/402/422
等请求错误直接失败并给出可操作的中文提示。

## 六终态与退出码

循环必定终止于六个终态之一并打印报告;退出码 5 是 CLI 层配置错误,不属于循环终态。

| 终态 | 退出码 | `reason` | 含义 |
|---|---|---|---|
| `SUCCESS` | 0 | `verification_passed` | 声明成功**且**声明时复验重跑测试命令通过 |
| `TESTS_FAILED` | 1 | `verification_failed` / `model_declared_failure` | 复验失败(被拒 finish 后步数耗尽),或模型 `finish(result="failed")` 认输 |
| `MAX_STEPS` | 2 | `steps_exhausted` | 步数预算耗尽 |
| `ERROR` | 3 | `model_stalled` / `provider_error` / `response_truncated` / `fake_script_exhausted` / `test_command_not_found` | 空转两轮 / 重试后仍失败 / 输出被 max_tokens 截断 / 测试命令不存在等 |
| `TIMEOUT` | 4 | `deadline_exceeded` | 整体超时 |
| `UNVERIFIED` | 6 | `no_test_command` | 声明成功但未配置 `--test-cmd`——如实报告「无法验证」,不猜结果 |
| (配置错误) | 5 | — | CLI 层配置错误(缺 key、`--repo` 不存在、`fake:` 脚本非法等) |

## 诚实失败

项目需求要求:失败和无法继续也是合法结果,但必须如实报告。mini-agent 的做法:

- `finish(result="failed")` 是一等公民终态(`TESTS_FAILED` / `model_declared_failure`);
- `finish(success)` 复验失败会被拒绝并回填输出,模型可继续修复;步数耗尽则
  `TESTS_FAILED`,不伪造成功;
- 改动**绝不自动回滚**;失败时的现场就是证据,diff 即凭证;
- 测试通过后再写坏文件再 finish,会被声明时复验当场抓住——过期的通过结果
  买不来 `SUCCESS`。

「任务不可能完成 → 如实报失败」的真实运行记录(任务:不得创建/修改/删除任何
文件的前提下让全部测试通过;连续两次同结果):

```
终态:TESTS_FAILED(原因:model_declared_failure),退出码 1
模型声明:无法在约束下完成。测试失败根因在仓库源码 tags_utils/normalize.py:
normalize_tags 仅做 raw.split(","),未去除标签两端空白、未丢弃空标签……
修复必须修改该源文件……但任务硬性约束禁止创建、修改或删除仓库内任何文件……
因此不存在任何合规的修复途径。按要求未做任何文件改动,如实申报失败。
步数:4;token 用量:prompt 9338 / completion 352 / total 9690
```

任务措辞不提示可声明失败时,同一场景还有一次诚实终止于 `MAX_STEPS`(退出码 2)
的记录——模型在 run_command 上打转被步数预算截停,同样零改动、如实入报。
完整记录(含步数/token/退出码):[`fixtures/REHEARSALS.md`](fixtures/REHEARSALS.md)。

## 演示

三条彩排路径,各连续 2 次真实模型运行结果稳定(2026-11-12,`deepseek-flash`;
完整记录见 [`fixtures/REHEARSALS.md`](fixtures/REHEARSALS.md)):

1. **`fixtures/tags-repo`**——项目需求同款 `normalize_tags` 修 bug 场景。基线
   3 失败 / 3 通过 → 读实现与测试 → 跑基线 → 修复(strip + 丢弃空标签)→
   6 通过 → 复验 `SUCCESS`,退出码 0;两次均 6 步、~17.6k token。
2. **`fixtures/algo-repo`**——对着隐藏测试集实现 `spiral_order`(11 个用例,
   边界不剧透)。`SUCCESS`,退出码 0(两次分别 5 步与 8 步;较长那次用
   `run_command` 做过自查,该结果不计入成功判定)。
3. **诚实失败**——上面的不可完成任务:`finish(result="failed")` →
   `TESTS_FAILED`,退出码 1,两次;另有 `MAX_STEPS`(退出码 2)变体一次。
   失败如实报告,绝不伪造。

### 录屏(asciinema)

两段真实 `deepseek-flash` 运行的终端录像(2026-11-12),均在 fixture 的 `/tmp`
临时副本上录制——绝不动仓库内 fixture,也**未上传任何外部站点**:
`.cast` 文件直接入库于 [`docs/demos/`](docs/demos/)。

```bash
asciinema play docs/demos/tags-repo-success.cast   # 演示 1:修 normalize_tags → SUCCESS(6 步)
asciinema play docs/demos/honest-failure.cast      # 演示 3:不可完成任务 → 如实 TESTS_FAILED
```

(本机需安装 [asciinema](https://asciinema.org),如 `brew install asciinema`;
录像已内置 `--idle-time-limit`,回放自动跳过等待。)

## 可观测性

每次运行在工作目录旁写 `mini-agent-runs/<YYYYmmdd-HHMMSS>-<随机后缀>/session.jsonl`
(若解析后落在 `--repo` 内则改用系统临时目录,避免污染目标仓库)。日志
append-only、每事件写后 flush——进程被 kill 也留下完整可读的日志;永不记录
API key 与请求头。事件类型八类:

`session_meta`、`llm_request`、`llm_response`、`tool_call`、`tool_result`、
`test_run`、`context_trim`、`final_report`

值得注意的细节:`llm_request` 存实际发送的 messages(还原上下文所必需);
`test_run` 存完整 stdout/stderr,而模型只看截断版;`context_trim` 存被移除消息
的原文——仅凭日志即可还原裁剪前的全过程。

## 测试

```bash
uv run --extra dev pytest -q   # 217 个测试;无 key、无网络
```

唯一测试接缝在 Provider 边界:脚本化 FakeModel 按真实 DeepSeek 流式分片规律吐
chunk(id 只在首片、arguments 跨片、多调用交错),流式聚合代码路径被真实执行;
接缝之上的 CLI、循环、围栏、停止条件、报告全部是被测的生产代码。

> **质量门:**ruff(默认规则集)+ GitHub Actions 单 job
> (`.github/workflows/ci.yml`:ruff + pytest,无 API key、无网络出站)守住每次
> 推送;`tests/test_cli_smoke.py` 以真实子进程跑安装好的 `mini-agent` 入口,
> 断言退出码、终报与会话日志。

## 仓库结构

```
├── README.md / README.zh-CN.md   # 英文主文档 + 中文版(顶部互切链接)
├── DESIGN.md                     # 设计文档:四要素、砍了什么为什么、V2 接缝
├── AI-USAGE.md                   # AI 使用声明(诚实披露人机分工)
├── GLOSSARY.md                   # 七个核心术语中英对照
├── LICENSE                       # MIT
├── pyproject.toml                # hatchling;console script `mini-agent`;依赖:openai
├── src/mini_agent/               # cli / providers / loop / executor / tools / session / sandbox
├── tests/                        # FakeModel 驱动的 pytest 套件(无 key、无网络)
├── fixtures/                     # tags-repo / algo-repo 演示仓库 + REHEARSALS.md 彩排实录
├── examples/                     # fix-cycle.json:修复循环的 FakeModel 脚本回放
├── docs/demos/                   # asciinema 录像(成功路径 + 诚实失败)
└── .github/workflows/            # ci.yml:ruff + pytest(单 job,无 key、无网络)
```

## 许可证

[MIT](LICENSE) — Copyright (c) 2026 Leewwp.

## 延伸阅读

- [DESIGN.md](DESIGN.md) — 四要素的实现与取舍、砍了什么为什么、V2 接缝映射、安全边界。
- [AI-USAGE.md](AI-USAGE.md) — 本项目开发中 AI 助手的使用方式与人的角色(如实披露)。
- [GLOSSARY.md](GLOSSARY.md) — 七个核心术语的中英对照。
