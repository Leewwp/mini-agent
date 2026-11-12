"""工具协议与 ToolRegistry(扩展接缝之二)。

设计律:核心循环不认识具体工具——工具经 ToolRegistry 注入,支持运行期
注册(V2 的子 Agent 就是一个注册进来的、内部再跑循环的工具)。

Tool 是 Protocol:内置工具与 FunctionTool(闭包包装)都满足它;
execute(args) 返回 str 或 ToolOutput。返回 ToolOutput 时可携带循环语义
元数据(terminal / finish_declared / verification_passed /
unverified_reason)——循环只读元数据,不按工具名特判。失败以异常抛出,
由执行器转成 isError 的 tool_result,循环不中断。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable


class ToolError(Exception):
    """工具执行失败;执行器转成 isError 的 tool_result 回填,不中断循环。"""


@dataclass(frozen=True)
class ToolOutput:
    """工具执行结果 + 循环语义元数据。

    - ``terminal``:该调用请求终止循环(如 finish);
    - ``finish_declared``:finish 的声明,"success" / "failed";
    - ``summary``:声明终止时的结果摘要(终报用);
    - ``verification_passed``:验证类工具的结果(True=通过 / False=失败),
      None 表示无法验证或本结果与验证无关;
    - ``unverified_reason``:无法验证的原因代码
      ("no_test_command" / "test_command_not_found"),供终态映射;
    - ``is_error``:True 时按 isError 的 tool_result 回填。
    """

    content: str
    is_error: bool = False
    terminal: bool = False
    finish_declared: str | None = None
    summary: str | None = None
    verification_passed: bool | None = None
    unverified_reason: str | None = None


@runtime_checkable
class Tool(Protocol):
    """工具协议:name/description/parameters + execute(args)。

    可选属性 ``is_verifier``(默认 False):标记该工具是验证工具
    (其结果计入成功判定)。循环做 finish(success) 复验时,向
    ToolRegistry 取全部 verifier 工具重跑——不硬编码工具名。
    """

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def parameters(self) -> dict: ...

    def execute(self, args: dict) -> "str | ToolOutput": ...


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

    def verifiers(self) -> list[Tool]:
        """全部标记为 verifier 的工具(声明时复验重跑它们,不硬编码名字)。"""
        return [tool for tool in self._tools.values() if getattr(tool, "is_verifier", False)]

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
