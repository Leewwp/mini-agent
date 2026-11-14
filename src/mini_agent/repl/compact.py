"""/compact 手动压缩(工单 13 / 设计票 04):LLM 语义摘要 + 历史整体替换。

哲学:压缩是纯增益——**任何失败都不动历史**、不写 ``compact`` 事件。

- 切点按 user 轮次(复用 03 的 ``_find_turn_groups``),保留最近 2 个完整轮次,
  组内 assistant/tool 配对天然完整;
- 压缩后结构定死:``[system(不动), user(SUMMARY_PREFIX+摘要), 最近 2 个完整
  user 轮次]``;保留 2 轮后仍超阈值交自动裁剪兜底(turn 分组,摘要头部受保护);
- 摘要 prompt = pi 六段结构(末段强制保留精确路径/函数名/错误信息原文)+
  ``<conversation>`` 包裹的 pi 式序列化 + Codex handoff 语义 +
  ``/compact [聚焦指令]`` 拼接模板尾;
- 摘要调用 ``provider.complete(summary_messages, tools=None)`` 单独记
  ``compact_request``/``compact_response``(不污染 resume 的 llm_request);
  **只有成功落地才写 ``compact`` 事件**(summary/replaced_messages/trigger/
  model/usage/kept_turns/tokens_before/tokens_after,append-only 可还原);
- 响应校验:非空、无 tool_calls、非 length、≤4k token;任一不过 → 失败路径;
- 短历史(<6 条或估算 <5k token)提示跳过;连续 /compact 的旧摘要并入
  ``<conversation>`` 首条(UPDATE prompt 不做)。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from mini_agent.loop import (
    COMPACT_SUMMARY_PREFIX,
    _find_turn_groups,
    _history_tokens,
    _protected_head_end,
)
from mini_agent.providers import ProviderError, estimate_tokens

SHORT_HISTORY_MESSAGES = 6  # 消息数低于此值提示跳过
SHORT_HISTORY_TOKENS = 5000  # 估算 token 低于此值提示跳过
MAX_SUMMARY_TOKENS = 4000  # 摘要超限 → 拒绝压缩、保留原历史
KEEP_TURNS = 2  # 压缩后保留的最近完整 user 轮次

# pi 的 SUMMARIZATION_SYSTEM_PROMPT 中文版
COMPACT_SYSTEM_PROMPT = (
    "你是上下文摘要助手。你的任务是阅读一段用户与 AI 编码助手的对话,"
    "然后按指定格式输出结构化摘要。不要继续这段对话,"
    "不要回答对话中的任何问题,只输出结构化摘要。"
)

# pi 六段结构 + Codex handoff 语义 + 聚焦指令拼接位
COMPACT_USER_TEMPLATE = """\
以下是一段需要压缩的对话(其中可能包含此前的会话摘要条目):

<conversation>
{conversation}
</conversation>

请为这段对话生成一份交接摘要:它将替代原对话,供另一个模型接手继续工作。严格按以下结构输出:

## 目标
(用户想完成什么)
## 约束与偏好
(用户提出的要求、偏好与约束)
## 进展
(已完成 / 进行中 / 受阻)
## 关键决策
(已做出的重要决定及理由)
## 下一步
(接下来应该做什么)
## 关键上下文
(继续工作必需的信息——必须保留精确的文件路径、函数名、命令与错误信息原文)

要求:简洁、结构化,聚焦于让接手者无缝继续工作;不要提及本次摘要请求本身。{focus}"""


def serialize_conversation(messages: list[dict]) -> str:
    """pi 式序列化(跳过 system;旧摘要条目作为首条输入保留)。"""
    lines: list[str] = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            content = str(m.get("content") or "")
            tag = "[会话摘要]" if content.startswith(COMPACT_SUMMARY_PREFIX) else "[用户]"
            lines.append(f"{tag} {content}")
        elif role == "assistant":
            if m.get("content"):
                lines.append(f"[助手] {m['content']}")
            for tc in m.get("tool_calls") or []:
                function = tc.get("function", {})
                lines.append(f"[工具调用] {function.get('name', '?')}({function.get('arguments', '')})")
        elif role == "tool":
            lines.append(f"[工具结果] {m.get('content') or ''}")
    return "\n".join(lines)


def _kept_tail(messages: list[dict], keep: int = KEEP_TURNS) -> list[dict]:
    """保留最近 ``keep`` 个完整 user 轮次(压缩摘要头部不算轮次,绝不重复保留)。"""
    head_end = _protected_head_end(messages)
    groups = [span for span in _find_turn_groups(messages) if span[0] >= head_end]
    kept = groups[-keep:] if keep > 0 else []
    if not kept:
        return []
    return [m for start, end in kept for m in messages[start:end]]


@dataclass
class CompactResult:
    """一次 /compact 的结果;失败时 ``new_messages`` 为 None、历史不动。"""

    ok: bool
    message: str
    new_messages: list[dict] | None = None
    usage_total_tokens: int = 0  # 摘要调用真实花费(计入会话总量,08 单列口径)
    skipped: bool = False
    detail: dict = field(default_factory=dict)


def _fail(message: str, **detail) -> CompactResult:
    return CompactResult(ok=False, message=message, detail=detail)


def compact_history(
    *,
    provider,
    logger,
    messages: list[dict],
    instructions: str = "",
    clock: Callable[[], float] = time.monotonic,
) -> CompactResult:
    """执行一次手动压缩;绝不抛出(KeyboardInterrupt 除外——交给 UI 中断路径)。"""
    if len(messages) < SHORT_HISTORY_MESSAGES or _history_tokens(messages) < SHORT_HISTORY_TOKENS:
        return CompactResult(
            ok=False,
            skipped=True,
            message="当前上下文很短,无需压缩",
            detail={"messages": len(messages), "estimated_tokens": _history_tokens(messages)},
        )

    conversation = serialize_conversation(messages)
    focus = f"\n\n用户特别要求聚焦:{instructions}" if instructions else ""
    summary_messages = [
        {"role": "system", "content": COMPACT_SYSTEM_PROMPT},
        {"role": "user", "content": COMPACT_USER_TEMPLATE.format(conversation=conversation, focus=focus)},
    ]
    logger.emit(
        "compact_request",
        model=provider.model,
        messages=len(summary_messages),
        instructions=instructions or None,
        estimated_prompt_tokens=_history_tokens(summary_messages),
    )
    started = clock()
    try:
        resp = provider.complete(summary_messages, tools=None)
    except ProviderError as exc:
        logger.emit(
            "compact_response",
            model=provider.model,
            duration_ms=int((clock() - started) * 1000),
            error=str(exc),
        )
        return _fail(f"摘要调用失败,已保留原历史:{exc}")
    duration_ms = int((clock() - started) * 1000)
    usage_total = (
        resp.usage.total_tokens
        if resp.usage.total_tokens is not None
        else estimate_tokens(resp.text)
    )
    logger.emit(
        "compact_response",
        model=provider.model,
        duration_ms=duration_ms,
        text=resp.text,
        usage={
            "prompt_tokens": resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
            "total_tokens": resp.usage.total_tokens,
            "estimated": resp.usage.estimated,
        },
        finish_reason=resp.finish_reason,
    )

    # 响应校验(设计票 04 评审约束 4 + Codex issue #55508 教训)
    if not resp.text.strip():
        return _fail("摘要为空,已保留原历史")
    if resp.tool_calls:
        return _fail("摘要响应包含工具调用,已保留原历史")
    summary_tokens = estimate_tokens(resp.text)
    if summary_tokens > MAX_SUMMARY_TOKENS:
        return _fail(
            f"摘要超限(约 {summary_tokens} token > {MAX_SUMMARY_TOKENS}),已保留原历史;可换聚焦指令重试"
        )

    system = messages[0]
    summary_user = {"role": "user", "content": f"{COMPACT_SUMMARY_PREFIX}\n{resp.text}"}
    tail = _kept_tail(messages)
    new_messages = [system, summary_user, *tail]
    tokens_before = _history_tokens(messages)
    tokens_after = _history_tokens(new_messages)
    logger.emit(
        "compact",
        summary=resp.text,
        replaced_messages=messages,  # 原始消息全文,append-only 可还原
        trigger="manual",
        model=provider.model,
        usage={
            "prompt_tokens": resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
            "total_tokens": resp.usage.total_tokens,
            "estimated": resp.usage.estimated,
        },
        kept_turns=len([m for m in tail if m.get("role") == "user"]),
        tokens_before=tokens_before,
        tokens_after=tokens_after,
    )
    return CompactResult(
        ok=True,
        message=(
            f"已压缩:{len(messages)} 条消息 → 摘要 + 最近 {KEEP_TURNS} 轮"
            f"(估算 {tokens_before} → {tokens_after} tok,其中压缩摘要 {usage_total} tok)"
        ),
        new_messages=new_messages,
        usage_total_tokens=usage_total,
        detail={"tokens_before": tokens_before, "tokens_after": tokens_after},
    )
