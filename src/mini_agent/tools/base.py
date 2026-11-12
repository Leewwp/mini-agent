"""工具协议与 ToolRegistry(扩展接缝之二)。

设计律:核心循环不认识具体工具——工具经 ToolRegistry 注入,支持运行期
注册(V2 的子 Agent 就是一个注册进来的、内部再跑循环的工具)。

Tool 是 Protocol:内置工具与 FunctionTool(闭包包装)都满足它;
execute(args) 返回给模型的文本内容,失败以异常抛出,由执行器转成
isError 的 tool_result,循环不中断。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable


class ToolError(Exception):
    """工具执行失败;执行器转成 isError 的 tool_result 回填,不中断循环。"""


@runtime_checkable
class Tool(Protocol):
    """工具协议:name/description/parameters + execute(args) -> str。"""

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def parameters(self) -> dict: ...

    def execute(self, args: dict) -> str: ...


@dataclass(frozen=True)
class FunctionTool:
    """用闭包快速构造的工具(V2 运行期注册的便捷形态)。"""

    name: str
    description: str
    parameters: dict
    func: Callable[[dict], str]

    def execute(self, args: dict) -> str:
        return self.func(args)


class ToolRegistry:
    """工具注册表:运行期注册、按名取用、导出 OpenAI function 定义。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册工具;重名拒绝注册(静默覆盖是 footgun)。"""
        if tool.name in self._tools:
            raise ToolError(f"工具名已注册:{tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def definitions(self) -> list[dict]:
        """导出 OpenAI function calling 的 tools 参数(注册顺序保留)。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self._tools.values()
        ]
