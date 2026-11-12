"""工具执行器与 no-op 钩子链(扩展接缝之三)的外部行为测试。

覆盖:tool_call/tool_result 事件、8KB 保头尾截断(只作用于模型上下文,
日志存全量)、工具异常转 isError 不中断、钩子阻断/改写(接缝证明)。
"""

import json
from dataclasses import replace
from pathlib import Path

from mini_agent.executor import HookChain, ToolExecutor
from mini_agent.providers import ToolCall
from mini_agent.session import SessionLogger
from mini_agent.tools.base import FunctionTool, ToolRegistry


def read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def make_call(name: str, arguments: str, call_id: str = "call_1") -> ToolCall:
    return ToolCall(index=0, id=call_id, name=name, arguments=arguments)


def make_registry(*tools) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def echo_tool() -> FunctionTool:
    return FunctionTool(
        name="echo",
        description="回显文本(测试用)",
        parameters={"type": "object", "properties": {}, "required": []},
        func=lambda args: f"收到:{args.get('text', '')}",
    )


def big_tool(payload: str) -> FunctionTool:
    return FunctionTool(
        name="big",
        description="返回大文本(测试用)",
        parameters={"type": "object", "properties": {}, "required": []},
        func=lambda args: payload,
    )


def make_executor(registry: ToolRegistry, hooks: HookChain | None = None, tmp: Path = Path("/tmp")):
    logger = SessionLogger(tmp)  # session.jsonl 直接落在 tmp 下
    return ToolExecutor(registry=registry, logger=logger, hooks=hooks), logger


def test_run_executes_tool_and_emits_call_and_result_events(tmp_path):
    executor, logger = make_executor(make_registry(echo_tool()), tmp=tmp_path)
    result = executor.run(make_call("echo", '{"text": "你好"}'), step=3)
    assert result.content == "收到:你好"
    assert result.is_error is False
    events = read_events(logger.path)
    assert [e["type"] for e in events] == ["tool_call", "tool_result"]
    assert events[0]["step"] == 3
    assert events[0]["call_id"] == "call_1"
    assert events[0]["name"] == "echo"
    assert events[0]["arguments"] == '{"text": "你好"}'  # 原样记录模型给出的参数
    assert events[1]["content"] == "收到:你好"
    assert events[1]["is_error"] is False
    logger.close()


def test_result_over_8kb_is_clipped_for_model_but_logged_in_full(tmp_path):
    payload = "HEAD>" + "x" * 20000 + "<TAIL"
    executor, logger = make_executor(make_registry(big_tool(payload)), tmp=tmp_path)
    result = executor.run(make_call("big", "{}"))
    assert result.truncated is True
    assert "HEAD>" in result.content and "<TAIL" in result.content  # 保头尾
    assert "已截断" in result.content
    assert len(result.content.encode("utf-8")) < len(payload.encode("utf-8"))
    # 截断只作用于模型上下文,日志存全量
    logged = read_events(logger.path)[1]
    assert logged["content"] == payload
    assert logged["truncated"] is True
    logger.close()


def test_unknown_tool_becomes_is_error_result(tmp_path):
    executor, logger = make_executor(make_registry(echo_tool()), tmp=tmp_path)
    result = executor.run(make_call("nope", "{}"))
    assert result.is_error is True
    assert "未知工具" in result.content and "echo" in result.content
    logger.close()


def test_invalid_json_arguments_become_is_error_result(tmp_path):
    executor, logger = make_executor(make_registry(echo_tool()), tmp=tmp_path)
    result = executor.run(make_call("echo", '{"text": '))  # 半截 JSON
    assert result.is_error is True
    assert "JSON" in result.content
    logger.close()


def test_tool_exception_becomes_is_error_and_execution_continues(tmp_path):
    def boom(args: dict) -> str:
        raise RuntimeError("磁盘假装坏了")

    registry = make_registry(
        FunctionTool("boom", "抛异常", {"type": "object", "properties": {}}, boom),
        echo_tool(),
    )
    executor, logger = make_executor(registry, tmp=tmp_path)
    first = executor.run(make_call("boom", "{}", call_id="c1"))
    second = executor.run(make_call("echo", '{"text": "继续"}', call_id="c2"))  # 循环不中断
    assert first.is_error is True and "磁盘假装坏了" in first.content
    assert second.is_error is False and second.content == "收到:继续"
    logger.close()


def test_before_tool_call_block_prevents_execution(tmp_path):
    executed = []
    registry = make_registry(
        FunctionTool(
            "write",
            "写文件(测试用)",
            {"type": "object", "properties": {}},
            lambda args: executed.append(args) or "写好了",
        )
    )
    hooks = HookChain()
    hooks.on_before_tool_call(lambda call: {"block": True, "reason": "需要人工审批"})
    executor, logger = make_executor(registry, hooks=hooks, tmp=tmp_path)
    result = executor.run(make_call("write", '{"path": "x"}'))
    assert executed == []  # 工具未执行
    assert result.is_error is True
    assert "需要人工审批" in result.content
    events = read_events(logger.path)  # 拦截也留痕:两个事件都在
    assert [e["type"] for e in events] == ["tool_call", "tool_result"]
    assert events[1]["is_error"] is True
    logger.close()


def test_after_tool_call_can_rewrite_result(tmp_path):
    hooks = HookChain()
    hooks.on_after_tool_call(lambda call, result: replace(result, full_content=result.full_content + "[审计]"))
    executor, logger = make_executor(make_registry(echo_tool()), hooks=hooks, tmp=tmp_path)
    result = executor.run(make_call("echo", '{"text": "hi"}'))
    assert result.content.endswith("[审计]")
    logger.close()


def test_hook_chain_before_llm_call_rewrites_messages():
    # 接缝证明:装一个改写 messages 的 before_llm_call,断言替换生效
    hooks = HookChain()
    hooks.on_before_llm_call(
        lambda messages, tools: ([{"role": "user", "content": "压缩后的上下文"}], tools)
    )
    messages, tools = hooks.before_llm_call([{"role": "user", "content": "很长的原始上下文"}], [])
    assert messages == [{"role": "user", "content": "压缩后的上下文"}]


def test_empty_hook_chain_is_noop():
    hooks = HookChain()
    messages = [{"role": "user", "content": "原"}]
    assert hooks.before_llm_call(messages, None) == (messages, None)
    assert hooks.before_tool_call(make_call("echo", "{}")) == {"block": False, "reason": ""}
    sentinel = object()  # after_tool_call 恒等传递
    assert hooks.after_tool_call("call", sentinel) is sentinel
