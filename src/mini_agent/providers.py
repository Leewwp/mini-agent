"""Provider 层(扩展接缝之一):LLMProvider 协议 + 归一化事件流。

归一化事件流是 Provider 对外的唯一语言:
    text_delta / reasoning_delta / tool_call_delta / usage / done
tool-call 增量聚合、分片转换、响应拼装、用量估算全部是纯函数——
这是 V1 最易出 bug 处,单测直接打在纯函数上(分片规律以工单 01
Comments 第 3 条 deepseek-flash 实测为准)。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Iterable, Protocol, runtime_checkable

import openai


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


# ---------------------------------------------------------------------------
# OpenAI 兼容实现(唯一真实 Provider)
# ---------------------------------------------------------------------------


class ProviderError(Exception):
    """Provider 调用失败(循环转 ERROR 终态)。"""


class ProviderRequestError(ProviderError):
    """400/401/402/422 等请求错误:不可重试,附可操作提示。"""

    def __init__(self, message: str, status_code: int | None, hint: str):
        super().__init__(f"{message}\n可操作提示:{hint}")
        self.status_code = status_code
        self.hint = hint


class ProviderRetryExhausted(ProviderError):
    """429/5xx/网络错误重试 2 次后仍失败。"""


# 请求类错误(不重试)的中文可操作提示
_FATAL_HINTS = {
    400: "请求参数被拒绝:请检查模型名与请求内容,或缩小任务后重试",
    401: "API Key 无效或未授权:请检查环境变量 MINI_AGENT_API_KEY 是否为有效密钥",
    402: "账户余额不足:请到模型服务商控制台充值后重试",
    403: "当前 Key 无权访问该模型:请确认套餐或更换 --model",
    404: "模型不存在:请用 --model 指定有效模型名",
    422: "请求体无法处理:请检查任务与工具定义的格式",
}

_UNKNOWN_FATAL_HINT = "请求被服务方拒绝:请检查配置(模型名/密钥/参数)后重试"


def classify_provider_error(exc: Exception) -> tuple[str, int | None]:
    """错误分类:retry = 429/5xx/网络错误;fatal = 其余(含 400/401/402/422)。"""
    if isinstance(exc, openai.APIConnectionError):
        # 含 APITimeoutError:网络类错误一律可重试
        return "retry", None
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status == 429 or status >= 500:
            return "retry", status
        return "fatal", status
    return "fatal", None


class OpenAICompatProvider:
    """OpenAI 兼容端点的流式实现(DeepSeek 已实测,智谱/MiniMax best-effort)。

    - stream=True + stream_options.include_usage 取 token 用量(缺失时估算兜底);
    - thinking 默认显式关闭,--thinking 开启后 reasoning_content 只做展示不回传;
    - 只对 429/5xx/网络错误指数退避重试 2 次;400/401/402/422 直接转可操作错误。
    """

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 8192,
        thinking: bool = False,
        client: object | None = None,
        retry_delays: tuple[float, ...] = (1.0, 2.0),
        sleep=time.sleep,
    ) -> None:
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._thinking = thinking
        self._retry_delays = tuple(retry_delays)
        self._sleep = sleep
        if client is not None:
            self._client = client
        else:
            self._client = openai.OpenAI(api_key=api_key, base_url=base_url)

    @property
    def model(self) -> str:
        return self._model

    def complete(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        on_event=None,
    ) -> LLMResponse:
        kwargs: dict = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
            "extra_body": {"thinking": {"type": "enabled" if self._thinking else "disabled"}},
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        stream = self._create_with_retry(kwargs)
        events: list[ProviderEvent] = []
        for chunk in stream:
            for event in chunk_to_events(chunk):
                events.append(event)
                if on_event is not None:
                    on_event(event)
        return assemble_response(events)

    def _create_with_retry(self, kwargs: dict):
        """执行 create,按分类重试;重试次数 = len(retry_delays)。"""
        total_attempts = len(self._retry_delays) + 1
        for attempt in range(1, total_attempts + 1):
            try:
                return self._client.chat.completions.create(**kwargs)
            except Exception as exc:
                kind, status = classify_provider_error(exc)
                if kind == "fatal":
                    hint = _FATAL_HINTS.get(status, _UNKNOWN_FATAL_HINT)
                    raise ProviderRequestError(
                        f"模型请求被拒绝(HTTP {status}):{exc}", status, hint
                    ) from exc
                if attempt == total_attempts:
                    raise ProviderRetryExhausted(
                        f"模型服务持续不可用,已重试 {attempt - 1} 次后仍失败"
                        f"(429/5xx/网络错误):{exc}\n"
                        "可操作提示:请稍后重试,或用 --model 切换模型"
                    ) from exc
                self._sleep(self._retry_delays[attempt - 1])
