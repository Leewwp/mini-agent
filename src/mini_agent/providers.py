"""Provider 层(扩展接缝之一):LLMProvider 协议 + 归一化事件流。

归一化事件流是 Provider 对外的唯一语言:
    text_delta / reasoning_delta / tool_call_delta / usage / done
tool-call 增量聚合、分片转换、响应拼装、用量估算全部是纯函数——
这是 V1 最易出 bug 处,单测直接打在纯函数上(分片规律以工单 01
Comments 第 3 条 deepseek-flash 实测为准)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# 归一化事件
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextDelta:
    """模型正文增量。"""

    content: str


@dataclass(frozen=True)
class ReasoningDelta:
    """思考增量(reasoning_content);只做展示,绝不混入 content。"""

    content: str


@dataclass(frozen=True)
class ToolCallDelta:
    """工具调用增量;index 分组,id/name 只在各自首片出现。"""

    index: int
    id: str | None = None
    name: str | None = None
    arguments_fragment: str = ""


@dataclass(frozen=True)
class UsageInfo:
    """token 用量;provider 未回传时用估算兜底(estimated=True)。"""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    estimated: bool = False


@dataclass(frozen=True)
class Done:
    """流结束;finish_reason 例如 stop / tool_calls / length。"""

    finish_reason: str | None


ProviderEvent = TextDelta | ReasoningDelta | ToolCallDelta | UsageInfo | Done


# ---------------------------------------------------------------------------
# 完整响应(循环只拿完整响应,不碰增量)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    index: int
    id: str
    name: str
    arguments: str  # 原始 JSON 字符串,可能因流中断而是半截


@dataclass(frozen=True)
class LLMResponse:
    text: str = ""
    reasoning: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: UsageInfo = field(default_factory=UsageInfo)
    finish_reason: str | None = None


class ResponseTruncatedError(Exception):
    """finish_reason=length:输出被 max_tokens 截断,必须明确报错。"""


# ---------------------------------------------------------------------------
# 纯函数:估算 / 聚合 / 转换 / 拼装
# ---------------------------------------------------------------------------

_CJK_RE = re.compile(r"[\u3000-\u9fff\uff00-\uffef]")


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数:CJK 每字约 1 token,其余约 4 字符 1 token。"""
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return cjk + (other + 3) // 4


def estimate_usage(text: str, reasoning: str = "") -> UsageInfo:
    """provider 未随流回传 usage 时的兜底估算。"""
    completion = estimate_tokens(text) + estimate_tokens(reasoning)
    return UsageInfo(
        prompt_tokens=None,
        completion_tokens=completion,
        total_tokens=completion,
        estimated=True,
    )


def aggregate_tool_call_deltas(deltas: Iterable[ToolCallDelta]) -> list[ToolCall]:
    """按 index 分组聚合工具调用增量(纯函数)。

    - id/name 取各自首个非 None 片(实测:只在首片出现;若 provider 重复发,保留首个);
    - arguments 按到达顺序拼接;流中断时原样保留半截 JSON,不解析。
    - 输出按 index 升序。
    """
    order: list[int] = []
    acc: dict[int, dict] = {}
    for d in deltas:
        slot = acc.get(d.index)
        if slot is None:
            slot = {"id": None, "name": None, "arguments": ""}
            acc[d.index] = slot
            order.append(d.index)
        if d.id is not None and slot["id"] is None:
            slot["id"] = d.id
        if d.name is not None and slot["name"] is None:
            slot["name"] = d.name
        slot["arguments"] += d.arguments_fragment
    return [
        ToolCall(index=i, id=acc[i]["id"] or "", name=acc[i]["name"] or "", arguments=acc[i]["arguments"])
        for i in sorted(order)
    ]


def chunk_to_events(chunk: object) -> list[ProviderEvent]:
    """把一个 OpenAI 兼容流式分片转换为归一化事件(纯函数,鸭子类型)。

    兼容只有 usage 没有 choices 的末片;``reasoning_content`` 是 DeepSeek
    扩展字段(SDK 类型未声明但 extra=allow),用 getattr 安全读取。
    """
    events: list[ProviderEvent] = []
    choices = getattr(chunk, "choices", None) or []
    if choices:
        choice = choices[0]
        delta = getattr(choice, "delta", None)
        if delta is not None:
            content = getattr(delta, "content", None)
            if content:
                events.append(TextDelta(content))
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                events.append(ReasoningDelta(reasoning))
            for tc in getattr(delta, "tool_calls", None) or []:
                events.append(
                    ToolCallDelta(
                        index=tc.index,
                        id=getattr(tc, "id", None),
                        name=getattr(tc.function, "name", None),
                        arguments_fragment=getattr(tc.function, "arguments", None) or "",
                    )
                )
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason:
            events.append(Done(finish_reason))
    usage = getattr(chunk, "usage", None)
    if usage is not None:
        events.append(
            UsageInfo(
                prompt_tokens=getattr(usage, "prompt_tokens", None),
                completion_tokens=getattr(usage, "completion_tokens", None),
                total_tokens=getattr(usage, "total_tokens", None),
                estimated=False,
            )
        )
    return events


def assemble_response(events: Iterable[ProviderEvent]) -> LLMResponse:
    """把归一化事件流拼装成完整响应(纯函数)。

    finish_reason=length 时抛 ResponseTruncatedError——半截输出不可用,
    禁止据此写文件;usage 缺失时以估算兜底。
    """
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_deltas: list[ToolCallDelta] = []
    usage: UsageInfo | None = None
    finish_reason: str | None = None
    for event in events:
        if isinstance(event, TextDelta):
            text_parts.append(event.content)
        elif isinstance(event, ReasoningDelta):
            reasoning_parts.append(event.content)
        elif isinstance(event, ToolCallDelta):
            tool_deltas.append(event)
        elif isinstance(event, UsageInfo):
            usage = event
        elif isinstance(event, Done):
            finish_reason = event.finish_reason
    if finish_reason == "length":
        raise ResponseTruncatedError(
            "模型输出因 max_tokens 上限被截断(finish_reason=length),"
            "内容不完整,已丢弃:请调大 max_tokens 或缩小任务后重试"
        )
    text = "".join(text_parts)
    reasoning = "".join(reasoning_parts)
    return LLMResponse(
        text=text,
        reasoning=reasoning,
        tool_calls=tuple(aggregate_tool_call_deltas(tool_deltas)),
        usage=usage if usage is not None else estimate_usage(text, reasoning),
        finish_reason=finish_reason,
    )


# ---------------------------------------------------------------------------
# Provider 协议(扩展接缝之一:循环只认识协议,不认识具体模型)
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMProvider(Protocol):
    """LLM 提供方协议:一次调用 = 归一化事件流回调 + 完整响应。"""

    def complete(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        on_event: "callable | None" = None,  # type: ignore[valid-type]
    ) -> LLMResponse: ...


def iter_all_events(chunks: Iterable[object]) -> Iterator[ProviderEvent]:
    for chunk in chunks:
        yield from chunk_to_events(chunk)
