# GLOSSARY(术语表)

mini-agent 的核心术语,中英并列(全中文项目,界面与提示词均为中文,术语
保留英文原名便于对照源码与文献)。

## 基础七术语(批处理与 REPL 共用)

| English | 中文 | 一句话定义(代码落点) |
|---|---|---|
| Agent Loop | 代理循环 | 手写的核心 while 循环:LLM 往返计步、按序执行工具、读元数据判终止(`src/mini_agent/loop.py::AgentLoop`) |
| Tool Registry | 工具注册表 | 工具的注册/取用/定义导出,支持运行期注册;循环经它拿工具,不认识具体工具(`src/mini_agent/tools/base.py::ToolRegistry`) |
| Path Fence | 路径围栏 | 把一切文件读写限制在 `--repo` 内,拒绝 `..` 逃逸、仓库外绝对路径与符号链接穿透(`src/mini_agent/sandbox.py::PathFence`) |
| Session Event Log | 会话事件日志 | append-only 的 JSONL 事件流,每事件写后 flush、永不改写,是还原过程与压缩/记忆/恢复的数据地基(`src/mini_agent/session.py::SessionLogger`) |
| Extension Hook | 扩展钩子 | `before_llm_call` / `before_tool_call` / `after_tool_call` 三个 no-op 中间件钩子,审批类扩展的接入位(`src/mini_agent/executor.py::HookChain`) |
| Finish Tool | 终止工具 | 模型显式声明任务结束的唯一出口,`result ∈ {success, failed}`(`src/mini_agent/tools/finish.py::FinishTool`) |
| Verify-on-Finish | 声明时复验 | `finish(success)` 时不信任任何历史结果,当场重跑全部 verifier 工具,通过才判 SUCCESS(`src/mini_agent/loop.py::_handle_finish_success`) |

## REPL 术语(交互式会话,2026-11 REPL 实现新增)

| English | 中文 | 一句话定义(代码落点) |
|---|---|---|
| Run | 运行 | 批处理模式的一次完整执行:`--task` 给出、跑到六终态之一、进程退出(`cli.py::run`) |
| Session | 会话 | REPL 模式的持续对话单位:裸命令进入,跨多个 turn,`/exit` 时出一次会话级终报;日志在 `~/.mini-agent/sessions/<repo-slug>/`(`repl/orchestrator.py::ChatSession`) |
| Turn | 轮次 | 一条用户消息到本轮结束(纯文本回复、finish、MAX_STEPS、超时、中断皆可为轮末);步数/超时预算按轮独立(`loop.py::run_turn`) |
| TurnState / TurnOutcome | 轮次状态/轮次结果 | 轮次级结果类型:FinalState 同名六值 + `REPLIED`(文本回复即合法轮末)+ `INTERRUPTED`(Ctrl+C 中断,已补齐 tool 回执);不扩展批处理的 FinalState(`loop.py::TurnState`) |
| Manual Compaction | 手动压缩 | `/compact` 命令:LLM 生成语义摘要并把旧历史整体替换为 `[system, 摘要 user 消息, 最近 2 个完整 user 轮次]`;任何失败都不动历史(`repl/compact.py`) |
| Auto Trim | 自动裁剪 | 上下文超阈值时的机械安全阀:按交互组(批处理)或 user 轮次(REPL)整组替换为一条「[历史裁剪]」摘要,与手动压缩职责不同、兜底衔接(`loop.py::trim_history`) |
| Project Memory File | 项目记忆文件 | repo 根 `AGENTS.md`,启动期只读注入 system prompt 的 `<project_instructions>` 块;32KiB 截断;REPL-only,batch 零改动(`memory.py::load_project_memory`) |
| Session Resume | 会话恢复 | `--resume <会话目录>` 从最后一条 `turn_end.messages`(或崩溃日志重放)恢复消息历史,新目录续写、旧目录只读;不恢复运行态(`resume.py`) |

