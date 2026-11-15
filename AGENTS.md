# AGENTS.md

给在本仓库工作的 AI 编程助手与人类贡献者的项目指引。用 mini-agent 本身在此
仓库运行 REPL 时,本文件会被注入 system prompt 作为项目记忆(仅 REPL,≤32KiB)。

## 项目是什么

从零自研的本地命令行编码 Agent:**任务循环、工具执行边界、停止条件、结果判断**
四要素全部手写,不基于任何 Agent 框架。第三方运行依赖仅 `openai` 客户端
(OpenAI 兼容传输层)与 `prompt_toolkit`(REPL 输入框)。红线:不能变成聊天
机器人,也不能变成现成 Coding Agent 的包装器。

## 开发命令

```bash
uv sync --frozen                # 安装依赖(与 CI 完全一致)
uv run pytest -q                # 全部测试:无 API key、无网络、无 TTY 即全绿
uv run ruff check .             # Lint(默认规则集)
```

真实模型冒烟(可选;永远先复制 fixture 到 /tmp,禁止直跑仓库内 fixture):

```bash
cp -r fixtures/tags-repo /tmp/tags-repo-demo
MINI_AGENT_API_KEY=sk-... uv run mini-agent \
  --repo /tmp/tags-repo-demo --task "修复 normalize_tags…" --test-cmd "pytest -q"
```

## 硬约束(改代码前必读)

- **测试不得引入网络 / API key / TTY 依赖**:模型行为一律用脚本化 FakeModel
  注入(`--model fake:<script.json>`),流式分片形状以 deepseek 实测记录为准
  (`tests/test_fake.py`)。
- **批处理与 REPL 是两条独立路径**:批处理(`--task`)是行为基线,REPL 改动
  不得改变批处理语义。
- **六终态与退出码是公开契约**(README / DESIGN §3):改语义必须同步双语
  README 与 DESIGN.md。
- **session.jsonl append-only、永不改写**:日志是事实来源;不记录 API key
  与请求头。
- **影子 git 快照不写入目标仓库**。
- **文案中文优先**:系统提示词 / 工具描述 / 界面输出为中文,英文为热备;
  七条工具描述的单一事实源在 `src/mini_agent/tools/descriptions.py`
  (`MINI_AGENT_TOOL_DESC_LANG=en` 一键切换)。
- **失败是合法终态**:不做静默回滚、不伪造成功;声明时复验
  (Verify-on-Finish)语义不可绕过。

## 代码地图

| 模块 | 位置 | 职责 |
|---|---|---|
| CLI | `src/mini_agent/cli.py` | 参数解析、配置(flag > env > 默认)、装配 |
| Agent 循环 | `src/mini_agent/loop.py` | 计步、空转检测、预算/卡死提醒、终态映射、声明时复验、历史裁剪 |
| 卡死检测 | `src/mini_agent/stuck.py` | 同签名三模式 streak 监测(连败 3/冻结 4/成功复跑 3)→一次性纠正性提醒 |
| Provider | `src/mini_agent/providers.py` | OpenAI 兼容流式客户端、FakeModel、重试分类 |
| 工具 | `src/mini_agent/tools/` | Tool 协议 + 注册表:`fs.py` / `exec.py` / `finish.py` |
| 执行器 | `src/mini_agent/executor.py` | 钩子链、结果截断(≤8KB 保头尾)、日志事件 |
| 会话日志 | `src/mini_agent/session.py` | append-only JSONL、每事件 flush |
| 沙箱 | `src/mini_agent/sandbox.py` | 路径围栏、命令黑/白名单、env 最小化、影子 git 快照 |
| 终报 | `src/mini_agent/report.py` | 五要素终报渲染 |
| REPL | `src/mini_agent/repl/` | 交互会话、UI、`/compact` 压缩 |
| 记忆/恢复 | `src/mini_agent/memory.py` / `resume.py` | AGENTS.md 注入(仅 REPL)/ 会话恢复 |

设计依据与取舍见 [DESIGN.md](DESIGN.md),术语对照见 [GLOSSARY.md](GLOSSARY.md),
AI 参与度披露见 [AI-USAGE.md](AI-USAGE.md),彩排实录见
[fixtures/REHEARSALS.md](fixtures/REHEARSALS.md)。
