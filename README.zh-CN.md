<!-- SKELETON: drafted post-03, finalize in ticket 07 -->
<!-- TODO(07): 本文件为骨架初稿,逐节对齐 README.md 的完整中文化在工单 07 定稿时完成。 -->

[English | 中文](README.md)

# mini-agent(中文说明)

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
  ——威胁模型是「防误伤、不防对抗」(见 [DESIGN.md](DESIGN.md))。
- 六终态各有退出码;**失败也是合法结果**,如实报告,绝不静默回滚。
- 声明时复验(Verify-on-Finish):`finish(result="success")` 只有当场重跑测试命令
  并通过才被采信。
- append-only JSONL 会话日志(`session.jsonl`):模型条件、每次 LLM 请求/响应、
  每次工具调用/结果、测试运行、上下文裁剪、终报全部入档。
- 测试套件**无 API key、无网络**即可全绿(FakeModel 经 `--model fake:<script.json>`
  注入)。

系统提示词、工具描述与界面输出为中文,这是对齐项目需求演示要求的设计决定。

## 架构

数据流:CLI → AgentLoop → Provider / ToolRegistry / HookChain → SessionLog → Report。
ASCII 图与「图 ↔ 代码模块」对照表见 [README.md](README.md#architecture)(英文版,
工单 07 定稿时补全本文件的对应图)。

<!-- TODO(07): 在此嵌入中文版架构 ASCII 图与模块对照表(与英文版一致)。 -->

## 快速开始

需要 Python ≥ 3.10 与 [uv](https://docs.astral.sh/uv/)(或普通 pip)。

```bash
git clone https://github.com/Leewwp/mini-agent.git
cd mini-agent
uv venv && uv pip install -e ".[dev]"
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

> **TODO(05):** `fixtures/` 由工单 05 落地;落地前可把 `--repo` 指向任意一次性目录,
> 并用 `--test-cmd` 传入可运行的测试命令。

无 key 复现:`--model fake:<script.json>` 注入脚本化 FakeModel,以上全部代码路径
(循环/围栏/停止条件/报告)离线可跑。

## CLI 参数

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--repo` | (必填) | 目标仓库目录;agent 真实写入,改动不自动回滚 |
| `--task` | (必填) | 自然语言任务(中文最佳) |
| `--max-steps` | `20` | 步数预算;一次 LLM 往返计 1 步 |
| `--test-cmd` | 无 | 预配置测试命令(如 `pytest -q`);缺省时成功无法验证(→ `UNVERIFIED`) |
| `--model` | `deepseek-flash` | 模型名;或 `fake:<script.json>` 注入 FakeModel |
| `--timeout` | `600` | 整体超时秒数 |
| `--max-context-tokens` | `60000` | 历史裁剪阈值(token) |
| `--thinking` | 关 | 开启思考模式;reasoning 仅展示(stderr),绝不进入上下文 |

环境变量(优先级:flag > 环境变量 > 默认值):`MINI_AGENT_API_KEY`(必需,除非
`fake:`)、`MINI_AGENT_BASE_URL`(默认 `https://api.deepseek.com`)、`MINI_AGENT_MODEL`。
请求默认:`temperature=0.0`、`max_tokens=8192`、恒流式;只对 429/5xx/网络错误重试
2 次,400/401/402/422 等请求错误直接报可操作提示。

## 六终态与退出码

| 终态 | 退出码 | reason | 含义 |
|---|---|---|---|
| `SUCCESS` | 0 | `verification_passed` | 声明成功且**声明时复验**通过 |
| `TESTS_FAILED` | 1 | `verification_failed` / `model_declared_failure` | 复验失败(或模型认输 `finish(failed)`) |
| `MAX_STEPS` | 2 | `steps_exhausted` | 步数耗尽 |
| `ERROR` | 3 | `model_stalled` / `provider_error` / `response_truncated` / `fake_script_exhausted` / `test_command_not_found` | 空转两轮 / 重试后仍失败 / 输出截断 / 测试命令不存在等 |
| `TIMEOUT` | 4 | `deadline_exceeded` | 整体超时 |
| `UNVERIFIED` | 6 | `no_test_command` | 声明成功但未配置 `--test-cmd`,如实报告"无法验证" |
| (配置错误) | 5 | — | CLI 层配置错误(缺 key、`--repo` 不存在等),不属于循环终态 |

## 诚实失败

项目需求要求:失败和无法继续也是合法结果,但必须如实报告。mini-agent 的做法:

- `finish(result="failed")` 是一等公民终态(`TESTS_FAILED` / `model_declared_failure`);
- `finish(success)` 复验失败会被拒绝并回填输出,步数耗尽则 `TESTS_FAILED`,不伪造成功;
- 改动**绝不自动回滚**;失败时改动与原因都在报告中如实陈述;
- 测试通过后再写坏文件再 finish,会被声明时复验当场抓住。

> **TODO(05):** 「任务不可能完成 → 如实报失败」的真实运行记录(终端输出 + 日志
> 路径)在工单 05 彩排后填入。

## 测试

```bash
uv run --extra dev pytest -q   # 工单 03 收尾时 179 个测试全绿;无 key、无网络
```

唯一测试接缝在 Provider 边界:脚本化 FakeModel 按真实 DeepSeek 流式分片规律吐
chunk(id 只在首片、arguments 跨片、多调用交错),其上全部是生产代码。

> **质量门:**ruff(默认规则集)+ GitHub Actions 单 job(`.github/workflows/ci.yml`:
> ruff + pytest,无 API key、无网络出站)守住每次推送;`tests/test_cli_smoke.py`
> 以真实子进程跑安装好的 `mini-agent` 入口,断言退出码、终报与会话日志。

## 许可证

> **TODO(07):** MIT License 随发布工单落地。

## 延伸阅读

- [DESIGN.md](DESIGN.md) — 四要素的实现与取舍、砍了什么为什么、V2 接缝映射、安全边界。
- [GLOSSARY.md](GLOSSARY.md) — 七个核心术语的中英对照。
