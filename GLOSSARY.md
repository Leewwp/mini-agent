# GLOSSARY(术语表)

mini-agent 的七个核心术语,中英并列(全中文项目,界面与提示词均为中文,术语
保留英文原名便于对照源码与文献)。

| English | 中文 | 一句话定义(代码落点) |
|---|---|---|
| Agent Loop | 代理循环 | 手写的核心 while 循环:LLM 往返计步、按序执行工具、读元数据判终止(`src/mini_agent/loop.py::AgentLoop`) |
| Tool Registry | 工具注册表 | 工具的注册/取用/定义导出,支持运行期注册;循环经它拿工具,不认识具体工具(`src/mini_agent/tools/base.py::ToolRegistry`) |
| Path Fence | 路径围栏 | 把一切文件读写限制在 `--repo` 内,拒绝 `..` 逃逸、仓库外绝对路径与符号链接穿透(`src/mini_agent/sandbox.py::PathFence`) |
| Session Event Log | 会话事件日志 | append-only 的 JSONL 事件流,每事件写后 flush、永不改写,是还原过程与 V2 压缩/记忆的数据地基(`src/mini_agent/session.py::SessionLogger`) |
| Extension Hook | 扩展钩子 | `before_llm_call` / `before_tool_call` / `after_tool_call` 三个 no-op 中间件钩子,V2 审批/压缩的接入位(`src/mini_agent/executor.py::HookChain`) |
| Finish Tool | 终止工具 | 模型显式声明任务结束的唯一出口,`result ∈ {success, failed}`(`src/mini_agent/tools/finish.py::FinishTool`) |
| Verify-on-Finish | 声明时复验 | `finish(success)` 时不信任任何历史结果,当场重跑全部 verifier 工具,通过才判 SUCCESS(`src/mini_agent/loop.py::_handle_finish_success`) |
