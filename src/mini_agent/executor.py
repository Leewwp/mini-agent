"""工具执行器 + no-op 钩子链(扩展接缝之三)。

职责(spec「工具集」「扩展接缝」节):
- 每次工具调用产生 tool_call / tool_result 事件,日志存全量;
- 工具结果对模型上下文截断 ≤8KB,保头尾(测试输出偏重尾部);
- 工具异常转 isError 的 tool_result 回填,循环不中断;
- 三个钩子签名照 spec 钉死,默认空链 = 全 no-op:
  before_llm_call(messages, tools) -> (messages, tools)
  before_tool_call(call) -> {block: bool, reason: str}
  after_tool_call(call, result) -> result

V2 映射:审批=before_tool_call 拦截;压缩=before_llm_call 投影。
执行器不认识具体工具(ToolRegistry 注入)、不认识策略(钩子注入)。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from mini_agent.providers import ToolCall
from mini_agent.sandbox import clip_bytes
from mini_agent.session import SessionLogger
from mini_agent.tools.base import ToolError, ToolOutput, ToolRegistry

MAX_TOOL_RESULT_BYTES = 8 * 1024  # 模型上下文的工具结果上限
CLIP_HEAD_BYTES = 2 * 1024  # 保头
CLIP_TAIL_BYTES = 6 * 1024  # 保尾(测试输出偏重尾部)


def clip_for_model(
    text: str,
    *,
    limit: int = MAX_TOOL_RESULT_BYTES,
    head: int = CLIP_HEAD_BYTES,
    tail: int = CLIP_TAIL_BYTES,
) -> tuple[str, bool]:
    """把超限文本截成头+尾(纯函数);未超限原样返回。

    返回 (截断后文本, 是否发生了截断);截断只作用于模型上下文,日志存全量。
    """
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text, False
    clipped = clip_bytes(
        data,
        cap=limit,
        head=head,
        tail=tail,
        note="\n…[中间已截断 {omitted} 字节,完整内容见会话日志]…\n",
    )
    return clipped, True


@dataclass(frozen=True)
class ToolResult:
    """一次工具执行的完整结果。

    ``content`` 是给模型的(可能截断);``full_content`` 是全量(日志用)。
    ``is_error`` 为 True 时循环把它作为 isError 的 tool_result 回填,不中断。
    末四个字段是循环语义元数据(来自 ToolOutput),循环只读它们、
    不按工具名特判。
    """

    call_id: str
    name: str
    full_content: str
    content: str = ""  # 给模型的(可能截断);run() 统一从 full_content 裁剪回填
    is_error: bool = False
    truncated: bool = False
    duration_ms: int = 0
    terminal: bool = False
    finish_declared: str | None = None
    summary: str | None = None
    verification_passed: bool | None = None
    unverified_reason: str | None = None


class HookChain:
    """no-op 中间件链(扩展接缝之三):空链全 no-op,装上钩子即生效。"""

    def __init__(self) -> None:
        self._before_llm: list[Callable] = []
        self._before_tool: list[Callable] = []
        self._after_tool: list[Callable] = []

    # -- 注册(返回钩子本身,便于装饰器式使用) --
    def on_before_llm_call(self, hook: Callable) -> Callable:
        self._before_llm.append(hook)
        return hook

    def on_before_tool_call(self, hook: Callable) -> Callable:
        self._before_tool.append(hook)
        return hook

    def on_after_tool_call(self, hook: Callable) -> Callable:
        self._after_tool.append(hook)
        return hook

    # -- 执行(签名照 spec 钉死) --
    def before_llm_call(self, messages: list[dict], tools: list[dict] | None):
        """依次投过每个钩子;V2 压缩在此做非破坏性投影。"""
        for hook in self._before_llm:
            messages, tools = hook(messages, tools)
        return messages, tools

    def before_tool_call(self, call: ToolCall) -> dict:
        """任一钩子 block 即拦截;V2 审批在此拦截。"""
        for hook in self._before_tool:
            decision = hook(call)
            if decision and decision.get("block"):
                return {"block": True, "reason": str(decision.get("reason", ""))}
        return {"block": False, "reason": ""}

    def after_tool_call(self, call: ToolCall, result: ToolResult) -> ToolResult:
        """结果依次传过每个钩子,可改写。"""
        for hook in self._after_tool:
            result = hook(call, result)
        return result


class ToolExecutor:
    """执行一次工具调用:事件 → 钩子 → 执行 → 钩子 → 截断 → 事件。"""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        logger: SessionLogger,
        hooks: HookChain | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.registry = registry
        self.logger = logger
        self.hooks = hooks if hooks is not None else HookChain()
        self._clock = clock

    def _emit_call(self, call: ToolCall, extra: dict) -> None:
        self.logger.emit(
            "tool_call", call_id=call.id, name=call.name, arguments=call.arguments, **extra
        )

    def _emit_result(self, result: ToolResult, extra: dict) -> None:
        self.logger.emit(
            "tool_result",
            call_id=result.call_id,
            name=result.name,
            is_error=result.is_error,
            content=result.full_content,  # 日志存全量
            truncated=result.truncated,
            duration_ms=result.duration_ms,
            **extra,
        )

    def skip(self, call: ToolCall, *, step: int | None = None, reason: str) -> ToolResult:
        """不执行调用但补一条结果:每个 tool_call id 都必须有对应结果,
        否则下一轮请求非法(单轮超上限 / 循环已终止的调用走这里)。"""
        extra = {"step": step} if step is not None else {}
        self._emit_call(call, extra)
        result = ToolResult(
            call_id=call.id,
            name=call.name,
            content=reason,  # 回执原因必须到达模型(短定长字符串,无需裁剪)
            full_content=reason,
            is_error=True,
        )
        self._emit_result(result, extra)
        return result

    def run(self, call: ToolCall, *, step: int | None = None) -> ToolResult:
        """执行单个工具调用;任何工具侧失败都转 isError 结果,绝不向上抛。"""
        extra = {"step": step} if step is not None else {}
        self._emit_call(call, extra)
        decision = self.hooks.before_tool_call(call)
        if decision["block"]:
            result = ToolResult(
                call_id=call.id,
                name=call.name,
                full_content=f"工具调用被拦截:{decision['reason'] or '未说明原因'}",
                is_error=True,
            )
        else:
            result = self._execute(call)
        result = self.hooks.after_tool_call(call, result)
        clipped, truncated = clip_for_model(result.full_content)
        final = replace(result, content=clipped, truncated=truncated)
        self._emit_result(final, extra)
        return final

    def _execute(self, call: ToolCall) -> ToolResult:
        started = self._clock()
        try:
            try:
                args = json.loads(call.arguments) if call.arguments.strip() else {}
            except json.JSONDecodeError as exc:
                raise ToolError(f"工具参数不是合法 JSON:{exc}") from exc
            tool = self.registry.get(call.name)
            if tool is None:
                raise ToolError(
                    f"未知工具:{call.name}(可用工具:{', '.join(self.registry.names())})"
                )
            if not isinstance(args, dict):
                raise ToolError("工具参数必须是 JSON 对象(键值对)")
            output = tool.execute(args)
            duration_ms = int((self._clock() - started) * 1000)
            if isinstance(output, ToolOutput):  # 带循环语义元数据
                return ToolResult(
                    call_id=call.id,
                    name=call.name,
                    full_content=output.content,
                    is_error=output.is_error,
                    duration_ms=duration_ms,
                    terminal=output.terminal,
                    finish_declared=output.finish_declared,
                    summary=output.summary,
                    verification_passed=output.verification_passed,
                    unverified_reason=output.unverified_reason,
                )
            return ToolResult(
                call_id=call.id,
                name=call.name,
                full_content=output,
                duration_ms=duration_ms,
            )
        except ToolError as exc:
            return self._error_result(call, started, str(exc))
        except Exception as exc:  # noqa: BLE001 —— 工具异常不中断循环:转 isError 回填(spec 设计决定)
            return self._error_result(
                call, started, f"工具执行异常:{type(exc).__name__}: {exc}"
            )

    def _error_result(self, call: ToolCall, started: float, message: str) -> ToolResult:
        duration_ms = int((self._clock() - started) * 1000)
        return ToolResult(
            call_id=call.id,
            name=call.name,
            full_content=message,
            is_error=True,
            duration_ms=duration_ms,
        )
