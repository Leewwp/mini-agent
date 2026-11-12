"""Provider 层(扩展接缝之一):LLMProvider 协议 + 归一化事件流。

归一化事件流是 Provider 对外的唯一语言:
    text_delta / reasoning_delta / tool_call_delta / usage / done
tool-call 增量聚合、分片转换、响应拼装、用量估算全部是纯函数——
这是 V1 最易出 bug 处,单测直接打在纯函数上(分片规律以工单 01
Comments 第 3 条 deepseek-flash 实测为准)。
"""

from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol, runtime_checkable

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


class ProviderError(Exception):
    """Provider 调用失败(循环转 ERROR 终态)。"""


class ResponseTruncatedError(ProviderError):
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
        on_event: callable | None = None,  # type: ignore[valid-type]
    ) -> LLMResponse: ...


# ---------------------------------------------------------------------------
# OpenAI 兼容实现(唯一真实 Provider)
# ---------------------------------------------------------------------------


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
        }
        # thinking extra_body 是 DeepSeek 私有扩展,仅经 M1 在 DeepSeek 实测有效
        # (事实基准见工单 01 Comments);其他 OpenAI 兼容端点(智谱/MiniMax)
        # 可能对未知字段直接 400,故仅 deepseek* 模型携带,其余省略。
        if self._model.startswith("deepseek"):
            kwargs["extra_body"] = {"thinking": {"type": "enabled" if self._thinking else "disabled"}}
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


# ---------------------------------------------------------------------------
# FakeModel(--model fake:<script.json>):测试与无 key 复现的最高接缝
# ---------------------------------------------------------------------------


class FakeScriptExhausted(ProviderError):
    """脚本轮次耗尽(正常使用不应触达)。"""


@dataclass(frozen=True)
class FakeTurn:
    """脚本中的一轮回复。

    - ``tool_calls`` 的 ``arguments`` 允许 dict 或 JSON 字符串;
    - ``finish_reason`` 可显式覆盖(如 "length" 用于复现截断);
    - 默认不产生 reasoning(CI 默认路径与演示路径一致)。
    """

    text: str = ""
    reasoning: str = ""
    tool_calls: tuple[dict, ...] = ()
    finish_reason: str | None = None


def load_fake_script(path: str) -> list[FakeTurn]:
    """装载 fake 脚本;格式 ``{"turns": [{"text":..., "reasoning":...,
    "tool_calls": [{"name":..., "arguments": {...}}], "finish_reason":...}]} ``。"""
    from mini_agent.errors import ConfigError

    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"FakeModel 脚本不存在:{path}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"FakeModel 脚本不是合法 JSON:{path}({exc})") from exc
    turns = data.get("turns") if isinstance(data, dict) else None
    if not isinstance(turns, list):
        raise ConfigError(f"FakeModel 脚本缺少 turns 数组:{path}")
    result: list[FakeTurn] = []
    for i, raw in enumerate(turns):
        if not isinstance(raw, dict):
            raise ConfigError(f"FakeModel 脚本第 {i} 轮不是对象:{path}")
        tool_calls = raw.get("tool_calls") or []
        result.append(
            FakeTurn(
                text=str(raw.get("text", "")),
                reasoning=str(raw.get("reasoning", "")),
                tool_calls=tuple(
                    {"name": tc.get("name", ""), "arguments": tc.get("arguments", {})}
                    for tc in tool_calls
                ),
                finish_reason=raw.get("finish_reason"),
            )
        )
    return result


def _fragments(text: str, size: int) -> list[str]:
    """把字符串切成固定大小的分片(流式仿真)。"""
    if not text:
        return []
    return [text[i : i + size] for i in range(0, len(text), size)]


def _fake_chunk(*, role=None, content=None, reasoning=None, tool_calls=None, finish_reason=None, usage=None):
    """构造与 OpenAI 兼容流式分片同形的鸭子类型对象(不依赖 SDK 类型)。"""
    delta = SimpleNamespace(role=role, content=content, tool_calls=tool_calls, reasoning_content=reasoning)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage)


def fake_chunks_for_turn(
    fake_turn: FakeTurn,
    *,
    messages: list[dict] | None = None,
) -> list[object]:
    """把一轮脚本展开成流式分片序列,分片规律与 deepseek-flash 实测一致:

    首片只有 role;reasoning/text 各自分片;每个 tool_call 的首片含
    index+id+name+空 arguments;其后各调用的参数分片**交错**发出(多调用
    交错索引);末片带 finish_reason 与确定性 usage。
    """
    chunks: list[object] = []
    chunks.append(_fake_chunk(role="assistant"))
    for piece in _fragments(fake_turn.reasoning, 3):
        chunks.append(_fake_chunk(reasoning=piece))
    for piece in _fragments(fake_turn.text, 3):
        chunks.append(_fake_chunk(content=piece))

    ids = [f"call_{i}_{secrets.token_hex(3)}" for i in range(len(fake_turn.tool_calls))]
    args_strings: list[str] = []
    for i, tc in enumerate(fake_turn.tool_calls):
        args = tc.get("arguments", "")
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        args_strings.append(args)
        chunks.append(
            _fake_chunk(
                tool_calls=[
                    SimpleNamespace(
                        index=i,
                        id=ids[i],
                        function=SimpleNamespace(name=tc.get("name", ""), arguments=""),
                    )
                ]
            )
        )
    # 参数增量交错:轮转各调用,模拟并行 tool_calls 的交错分片
    pending = [_fragments(args, 4) for args in args_strings]
    while any(pending):
        for i, frag_list in enumerate(pending):
            if frag_list:
                chunks.append(
                    _fake_chunk(
                        tool_calls=[
                            SimpleNamespace(
                                index=i,
                                id=None,
                                function=SimpleNamespace(name=None, arguments=frag_list.pop(0)),
                            )
                        ]
                    )
                )

    finish = fake_turn.finish_reason or ("tool_calls" if fake_turn.tool_calls else "stop")
    # prompt 估算口径与真实 API 一致:content 与 tool_calls 的 arguments 都计入
    prompt = 0
    for m in messages or []:
        prompt += len(str(m.get("content") or ""))
        for tc in m.get("tool_calls") or []:
            prompt += len(str(tc.get("function", {}).get("arguments") or ""))
    completion = (
        estimate_tokens(fake_turn.text)
        + estimate_tokens(fake_turn.reasoning)
        + sum(estimate_tokens(a) for a in args_strings)
    )
    usage = SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
    )
    chunks.append(_fake_chunk(finish_reason=finish, usage=usage))
    return chunks


class FakeProvider:
    """按脚本吐出流式分片的伪 Provider:其上全部真实代码,其下只有网络被替换。"""

    def __init__(self, turns: list[FakeTurn], *, model_label: str = "fake") -> None:
        self._turns = list(turns)
        self._label = model_label

    @property
    def model(self) -> str:
        return self._label

    def complete(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        on_event=None,
    ) -> LLMResponse:
        if not self._turns:
            raise FakeScriptExhausted("FakeModel 脚本轮次已耗尽:请检查脚本与 max-steps 是否匹配")
        turn = self._turns.pop(0)
        events: list[ProviderEvent] = []
        for chunk in fake_chunks_for_turn(turn, messages=messages):
            events.extend(chunk_to_events(chunk))
        for event in events:
            if on_event is not None:
                on_event(event)
        return assemble_response(events)


def create_provider(
    model_spec: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 8192,
    thinking: bool = False,
    client: object | None = None,
    retry_delays: tuple[float, ...] = (1.0, 2.0),
) -> LLMProvider:
    """按 --model 取值构造 Provider:``fake:<script.json>`` 注入 FakeModel,
    其余视为 OpenAI 兼容模型名。"""
    if model_spec.startswith("fake:"):
        return FakeProvider(load_fake_script(model_spec[len("fake:") :]), model_label=model_spec)
    return OpenAICompatProvider(
        model=model_spec,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        max_tokens=max_tokens,
        thinking=thinking,
        client=client,
        retry_delays=retry_delays,
    )
