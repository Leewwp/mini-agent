"""工具协议与 ToolRegistry(扩展接缝之二)的外部行为测试。

ToolRegistry 支持运行期注册(V2 子 Agent = 注册一个内部再跑循环的工具);
definitions() 输出 OpenAI function calling 格式。
"""

import pytest

from mini_agent.tools.base import FunctionTool, ToolError, ToolRegistry


def make_echo_tool(name="echo_file", description="回显参数(测试用工具)"):
    def run(args: dict) -> str:
        return f"收到:{args['text']}"

    schema = {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "任意文本"}},
        "required": ["text"],
    }
    return FunctionTool(name=name, description=description, parameters=schema, func=run)


def test_registry_registers_and_executes_tool():
    registry = ToolRegistry()
    registry.register(make_echo_tool())
    tool = registry.get("echo_file")
    assert tool is not None
    assert tool.execute({"text": "你好"}) == "收到:你好"


def test_registry_get_unknown_tool_returns_none():
    assert ToolRegistry().get("nope") is None


def test_registry_duplicate_name_is_rejected():
    registry = ToolRegistry()
    registry.register(make_echo_tool())
    with pytest.raises(ToolError, match="echo_file"):
        registry.register(make_echo_tool())


def test_registry_definitions_follow_openai_function_schema():
    registry = ToolRegistry()
    registry.register(make_echo_tool())
    definitions = registry.definitions()
    assert definitions == [
        {
            "type": "function",
            "function": {
                "name": "echo_file",
                "description": "回显参数(测试用工具)",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string", "description": "任意文本"}},
                    "required": ["text"],
                },
            },
        }
    ]


def test_registry_runtime_registration_after_construction():
    # 接缝②:运行期注入新工具后立刻出现在 definitions(注册顺序保留)
    registry = ToolRegistry()
    registry.register(make_echo_tool(name="a_tool"))
    registry.register(make_echo_tool(name="b_tool"))
    assert [d["function"]["name"] for d in registry.definitions()] == ["a_tool", "b_tool"]


def test_registry_lists_registered_names():
    registry = ToolRegistry()
    registry.register(make_echo_tool())
    assert registry.names() == ["echo_file"]
