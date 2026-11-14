"""Agent Loop(spec「Agent Loop」「停止条件」):项目核心,全部自研。

设计律:循环不认识具体工具(ToolRegistry 注入)、不认识具体模型
(Provider 注入)、不认识具体策略(Hook 链注入)。

- 一次 LLM 往返计 1 步;单轮内多个工具调用按模型给出的顺序执行,
  上限 8 个,超出的调用补一条「未执行」tool 结果(每个 tool_call id
  都必须有对应结果,否则下一轮请求非法);
- 工具异常由执行器转 isError 的 tool_result 回填,循环不中断;只有
  Provider/内部错误才进 ERROR;
- 工具语义只通过结果元数据(terminal / finish_declared /
  verification_passed / unverified_reason)进入循环,循环不按工具名特判;
- finish(success) 声明时现场复验:不信任任何历史测试结果,经
  ToolRegistry 取标记为 verifier 的工具重跑;
- 连续 2 轮无工具调用也不 finish:第 1 轮注入中文提醒,第 2 轮
  ERROR(model_stalled),不空转到 MAX_STEPS;
- 步数预算提醒(仅批处理):步数用至 75% 仍无写入或结束声明时,一次性注入
  预算提醒——空转检测防「不干活」,它防「只读探索耗尽预算」;
- 卡死检测与纠正性提醒(仅批处理,工单 06):同一签名(工具名+规范化参数)
  的调用连续失败/同结果冻结/成功复跑达阈值时,一次性注入纠正性提醒
  (仿 OpenHands StuckDetector 的 nudge 级)——只提醒不终止,最坏仍 MAX_STEPS;
- 历史裁剪按「assistant(tool_calls) + 全部对应 tool 结果」整组替换为
  一条摘要文本,绝不单独删 tool 结果;阈值用上一响应 usage.prompt_tokens
  触发,context_trim 事件入日志、可还原。
"""

from __future__ import annotations

import enum
import math
import secrets
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from mini_agent.errors import (
    EXIT_ERROR,
    EXIT_MAX_STEPS,
    EXIT_SUCCESS,
    EXIT_TESTS_FAILED,
    EXIT_TIMEOUT,
    EXIT_UNVERIFIED,
)
from mini_agent.executor import ToolResult
from mini_agent.providers import (
    FakeScriptExhausted,
    LLMResponse,
    ProviderError,
    ResponseTruncatedError,
    ToolCall,
    estimate_tokens,
)
from mini_agent.session import SessionLogger
from mini_agent.stuck import StuckMonitor
from mini_agent.tools.base import ToolRegistry
from mini_agent.tools.finish import RESULT_FAILED, RESULT_SUCCESS

# ---------------------------------------------------------------------------
# 六终态(spec「停止条件」)
# ---------------------------------------------------------------------------


class FinalState(str, enum.Enum):
    SUCCESS = "SUCCESS"
    TESTS_FAILED = "TESTS_FAILED"
    MAX_STEPS = "MAX_STEPS"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    UNVERIFIED = "UNVERIFIED"


EXIT_CODES: dict[FinalState, int] = {
    FinalState.SUCCESS: EXIT_SUCCESS,
    FinalState.TESTS_FAILED: EXIT_TESTS_FAILED,
    FinalState.MAX_STEPS: EXIT_MAX_STEPS,
    FinalState.ERROR: EXIT_ERROR,
    FinalState.TIMEOUT: EXIT_TIMEOUT,
    FinalState.UNVERIFIED: EXIT_UNVERIFIED,
}


@dataclass(frozen=True)
class RunOutcome:
    """一次运行的终态报告(六要素:状态/原因/退出码/步数/token/模型)。"""

    state: FinalState
    reason: str
    steps: int
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    model: str
    summary: str = ""
    detail: str = ""  # ERROR 时的可操作错误文本

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.state]


# ---------------------------------------------------------------------------
# 轮次级(工单 10 / 设计票 03):REPL 的 turn 语义,不扩展 FinalState、
# 不影响批处理退出码;六终态同名值 + REPLIED + INTERRUPTED
# ---------------------------------------------------------------------------


class TurnState(str, enum.Enum):
    REPLIED = "REPLIED"  # 普通文本回复即合法轮末(chat 模式)
    INTERRUPTED = "INTERRUPTED"  # Ctrl+C 打断(已补齐 tool 回执)

    # 与 FinalState 同名六值(轮内工具语义与批处理一致)
    SUCCESS = "SUCCESS"
    TESTS_FAILED = "TESTS_FAILED"
    MAX_STEPS = "MAX_STEPS"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True)
class TurnOutcome:
    """一轮(turn)的结果(与 RunOutcome 同构,无退出码;tokens 容 None)。"""

    state: TurnState
    reason: str
    steps: int
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    model: str
    summary: str = ""
    detail: str = ""



# ---------------------------------------------------------------------------
# 中文系统提示词(工单 05 定稿:finish 用法 / 先读后写 / 验证优先 /
# 只有 run_tests 计入成功判定,四条契约在工具描述与系统提示词中各说一遍)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
你是 mini-agent,一个在本地命令行运行的编码助手,负责在指定仓库内自主完成
用户交给你的编码任务。工作方法:

1. 先用 list_files 了解仓库结构,用 read_file / search 阅读相关代码,理解现状;
   必要时可先跑一次 run_tests 看当前基线;
2. 用 write_file 修改或创建文件(整文件写入,需提供完整内容)。覆盖已存在的
   文件前,该文件必须在本次运行中读过或由你写入过——先读后写;新建文件不受
   此限制;
3. 每次修改后立即用 run_tests 验证;测试失败时仔细阅读错误输出,继续修改并
   重新验证,形成修复循环——不要在未验证的情况下宣称完成;
4. 任务完成时调用 finish(result="success", summary="…") 声明结束:系统会当场
   重跑一次测试命令复验,复验通过才判定成功;
5. 确实无法完成时,调用 finish(result="failed", summary="…") 如实申报失败——
   这是合法结果,优于伪造成功。

规则:
- 只有 run_tests 的结果计入成功判定;run_command 只用于运行辅助脚本或查看
  行为,其结果不构成任何验证;
- 路径被限制在目标仓库内;密钥文件(.env、*.pem、id_rsa* 等)不可读写;
- 请始终用中文回复;不要向用户提问,基于仓库内的信息自主决策并如实汇报。
"""


MAX_TOOL_CALLS_PER_TURN = 8
MIN_KEPT_GROUPS = 2  # 历史裁剪时保底保留的最近交互组数(turn 模式=最近 2 个 user 轮次)

# 步数预算提醒(工单 04):批处理下步数用至 75% 仍无写入/结束声明时一次性注入
BUDGET_REMINDER_RATIO = 0.75

# /compact 语义摘要 user 消息的固定前缀(工单 13 / 设计票 04)。常量在 loop.py
# 定义仅为避免循环导入;语义归属压缩机制:turn 分组裁剪按它识别受保护头部。
COMPACT_SUMMARY_PREFIX = "[会话摘要] 以下是本会话此前的压缩摘要"

# Ctrl+C 打断工具轮时,为未回执 tool_call 补的中断回执
INTERRUPTED_TOOL_RESULT = "[本轮被用户中断,工具未执行]"


# ---------------------------------------------------------------------------
# 历史裁剪(纯函数):整组替换为摘要,绝不单独删 tool 结果
# ---------------------------------------------------------------------------


def _history_tokens(messages: list[dict]) -> int:
    """按内容估算消息历史的 token(与 Provider 的估算口径一致)。"""
    total = 0
    for m in messages:
        total += estimate_tokens(m.get("content") or "")
        for tc in m.get("tool_calls") or []:
            total += estimate_tokens(tc.get("function", {}).get("arguments") or "")
    return total


def _find_interaction_groups(messages: list[dict]) -> list[tuple[int, int]]:
    """定位全部交互组:[assistant(tool_calls) + 其后连续 tool 结果] 的 [start, end)。"""
    groups: list[tuple[int, int]] = []
    i = 0
    while i < len(messages):
        if messages[i].get("role") == "assistant" and messages[i].get("tool_calls"):
            start = i
            i += 1
            while i < len(messages) and messages[i].get("role") == "tool":
                i += 1
            groups.append((start, i))
        else:
            i += 1
    return groups


def _find_turn_groups(messages: list[dict]) -> list[tuple[int, int]]:
    """turn 分组:每条 user 消息到下一条 user 消息为一个 [start, end)。

    组内天然保持 assistant/tool 配对(整轮原样)。批处理不用它;
    chat 模式的裁剪与 /compact 的切点共用。
    """
    starts = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    return [
        (start, starts[j + 1] if j + 1 < len(starts) else len(messages))
        for j, start in enumerate(starts)
    ]


def _protected_head_end(messages: list[dict]) -> int:
    """turn 裁剪的受保护头部右边界:首条**非摘要** user 消息的下标。

    即 [system, user(COMPACT_SUMMARY_PREFIX+摘要)] 整体受保护——语义摘要
    绝不被当最老 turn 组移除或被机械摘要替换(设计票 03 复审修订)。
    """
    for i, m in enumerate(messages):
        if m.get("role") != "user":
            continue
        if not str(m.get("content") or "").startswith(COMPACT_SUMMARY_PREFIX):
            return i
    return len(messages)



def _summary_text(group_count: int, removed_messages: list[dict]) -> str:
    counts = Counter(
        tc.get("function", {}).get("name", "?")
        for m in removed_messages
        for tc in m.get("tool_calls") or []
    )
    stats = "、".join(f"{name}×{n}" for name, n in counts.items()) or "无"
    return (
        f"[历史裁剪] 为控制上下文长度,已把较早的 {group_count} 轮工具交互"
        f"({stats})整组省略并替换为本摘要;完整原始内容见会话日志的 "
        "context_trim 事件(append-only,可还原)。"
    )


def _turn_summary_text(removed_turns: int) -> str:
    return (
        f"[历史裁剪] 为控制上下文长度,已把较早的 {removed_turns} 轮对话"
        "整段省略并替换为本摘要;完整原始内容见会话日志的 "
        "context_trim 事件(append-only,可还原)。"
    )


def trim_history(
    messages: list[dict],
    *,
    max_prompt_tokens: int,
    keep_last_groups: int = MIN_KEPT_GROUPS,
    grouping: str = "tool",
) -> tuple[list[dict], list[dict]]:
    """把最老的历史分组整组替换为一条摘要文本,直到估算低于阈值。

    - 触发前的判断由调用方做(阈值用上一响应 usage.prompt_tokens);
    - ``grouping="tool"``(默认,批处理):按 [assistant(tool_calls)+tool 结果]
      交互组切;``grouping="turn"``(chat):按 user 轮次切,保底最近
      ``keep_last_groups`` 轮,受保护头部(system+压缩摘要)绝不移除;
    - 最近 ``keep_last_groups`` 组保底保留,绝不裁穿;
    - 返回 (新消息列表, 被移除的原始消息);后者进 context_trim 事件供还原。
    """
    if grouping == "turn":
        return _trim_by_turn_groups(
            messages, max_prompt_tokens=max_prompt_tokens, keep_last_groups=keep_last_groups
        )
    groups = _find_interaction_groups(messages)
    if not groups or _history_tokens(messages) <= max_prompt_tokens:
        return messages, []

    removed_ranges: list[tuple[int, int]] = []
    for gi, span in enumerate(groups):
        if len(groups) - gi <= keep_last_groups:
            break  # 保底:最近 N 组不动
        removed_ranges.append(span)
        trial = _rebuild_with_summary(
            messages,
            removed_ranges,
            lambda count, removed: {
                "role": "assistant",
                "content": _summary_text(count, removed),
            },
        )
        if _history_tokens(trial) <= max_prompt_tokens:
            break
    if not removed_ranges:
        return messages, []
    removed_messages = [m for start, end in removed_ranges for m in messages[start:end]]
    new_messages = _rebuild_with_summary(
        messages,
        removed_ranges,
        lambda count, removed: {"role": "assistant", "content": _summary_text(count, removed)},
    )
    return new_messages, removed_messages


def _trim_by_turn_groups(
    messages: list[dict], *, max_prompt_tokens: int, keep_last_groups: int
) -> tuple[list[dict], list[dict]]:
    """turn 分组裁剪(chat):移除最老 user 轮次,保护压缩摘要头部。

    极端超限(保底轮内仍超阈值)时,只对保留轮内部按现有 tool 组逻辑
    降载——语义摘要 user 消息不是交互组,永远不会被机械摘要替换。
    """
    if _history_tokens(messages) <= max_prompt_tokens:
        return messages, []

    def turn_summary(count: int, removed: list[dict]) -> dict:
        return {"role": "assistant", "content": _turn_summary_text(count)}

    head_end = _protected_head_end(messages)
    removable = [span for span in _find_turn_groups(messages) if span[0] >= head_end]
    removed_ranges: list[tuple[int, int]] = []
    for gi, span in enumerate(removable):
        if len(removable) - gi <= keep_last_groups:
            break  # 保底:最近 N 个 user 轮次不动
        removed_ranges.append(span)
        trial = _rebuild_with_summary(messages, removed_ranges, turn_summary)
        if _history_tokens(trial) <= max_prompt_tokens:
            break
    if not removed_ranges:
        # 保底轮数内仍超限:退化为 tool 组降载(受保护头部不受影响)
        return trim_history(
            messages,
            max_prompt_tokens=max_prompt_tokens,
            keep_last_groups=keep_last_groups,
            grouping="tool",
        )
    removed_messages = [m for start, end in removed_ranges for m in messages[start:end]]
    new_messages = _rebuild_with_summary(messages, removed_ranges, turn_summary)
    if _history_tokens(new_messages) > max_prompt_tokens:
        # 移除旧轮后仍超限:保留轮内部再按 tool 组降载
        further, extra = trim_history(
            new_messages,
            max_prompt_tokens=max_prompt_tokens,
            keep_last_groups=keep_last_groups,
            grouping="tool",
        )
        if extra:
            new_messages = further
            removed_messages.extend(extra)
    return new_messages, removed_messages


def _rebuild_with_summary(
    messages: list[dict],
    ranges: list[tuple[int, int]],
    summary_factory,
) -> list[dict]:
    """把给定区间整组移除,在首个区间位置放一条合并摘要。"""
    drop = {i for start, end in ranges for i in range(start, end)}
    group_count = len(ranges)
    removed = [m for i, m in enumerate(messages) if i in drop]
    first = ranges[0][0]
    summary_message = summary_factory(group_count, removed)
    new_messages: list[dict] = []
    for i, m in enumerate(messages):
        if i == first:
            new_messages.append(summary_message)
        elif i not in drop:
            new_messages.append(m)
    return new_messages


# ---------------------------------------------------------------------------
# AgentLoop
# ---------------------------------------------------------------------------


def _tool_message(call_id: str, content: str, *, is_error: bool) -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": ("[工具执行失败] " + content) if is_error else content,
    }


def _assistant_message(resp: LLMResponse) -> dict:
    message: dict = {"role": "assistant", "content": resp.text or None}
    if resp.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in resp.tool_calls
        ]
    return message


@dataclass
class _UsageAccumulator:
    prompt_tokens: int | None = None
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, usage) -> None:
        if usage.prompt_tokens is not None:
            self.prompt_tokens = (self.prompt_tokens or 0) + usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens or 0
        self.total_tokens += usage.total_tokens or 0


class AgentLoop:
    """核心循环:Provider 注入模型、executor 注入工具与钩子、logger 注入日志。"""

    def __init__(
        self,
        *,
        provider,
        executor,
        logger: SessionLogger,
        system_prompt: str,
        task: str | None = None,
        initial_messages: list[dict] | None = None,
        chat_mode: bool = False,
        max_steps: int = 20,
        timeout_seconds: float = 600.0,
        max_context_tokens: int = 60000,
        clock: Callable[[], float] | None = None,
        on_activity: Callable[[str], None] | None = None,
        on_tool_result: Callable[[ToolResult], None] | None = None,
        stream=None,
    ) -> None:
        if initial_messages is not None and task is not None:
            raise ValueError("task 与 initial_messages 不能同时提供")
        if initial_messages is None and task is None:
            raise ValueError("必须提供 task 或 initial_messages 之一")
        if initial_messages is not None and (
            not initial_messages or initial_messages[0].get("role") != "system"
        ):
            raise ValueError("initial_messages 必须以 system 消息开头")
        self._provider = provider
        self._executor = executor
        self._registry: ToolRegistry = executor.registry
        self._logger = logger
        if initial_messages is not None:
            # REPL 轮次形态(设计票 03 决议 1):每轮新实例,注入 [system, ...历史]
            self._messages: list[dict] = [dict(m) for m in initial_messages]
        else:
            self._messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]
        self._chat_mode = chat_mode
        self._max_steps = max_steps
        self._timeout_seconds = timeout_seconds
        self._max_context_tokens = max_context_tokens
        self._clock = clock if clock is not None else time.monotonic
        self._on_activity = on_activity
        self._on_tool_result = on_tool_result
        self._stream = stream
        self._usage = _UsageAccumulator()
        self._verification_rejected = False
        self._last_prompt_tokens: int | None = None
        self._steps = 0  # 已完成计数(中断路径终报用;run() 内同步)
        self._produced = False  # 已调用 writer 工具或 terminal(finish)声明
        self._budget_reminded = False  # 一次性预算提醒(仅批处理)
        self._stuck_monitor = StuckMonitor()  # 卡死检测(工单 06)
        self._pending_nudge: str | None = None  # 待注入的卡顿/收尾提醒(批处理)

    @property
    def messages(self) -> list[dict]:
        """当前消息历史(拷贝);REPL 每轮以此为下一轮的 initial_messages。"""
        return [dict(m) for m in self._messages]

    # -- 主循环 --

    def run(self) -> RunOutcome:
        started = self._clock()
        stalled_rounds = 0
        steps = 0
        while True:
            self._steps = steps
            if steps >= self._max_steps:
                return self._finish_run(FinalState.MAX_STEPS, "steps_exhausted", steps)
            if self._clock() - started > self._timeout_seconds:
                return self._finish_run(FinalState.TIMEOUT, "deadline_exceeded", steps)
            self._maybe_trim()
            try:
                resp, steps = self._call_llm(steps)
            except ProviderError as exc:
                return self._finish_run(
                    FinalState.ERROR, _provider_reason(exc), steps, detail=str(exc)
                )
            steps += 1

            self._messages.append(_assistant_message(resp))

            if not resp.tool_calls:
                stalled_rounds += 1
                if stalled_rounds >= 2:
                    return self._finish_run(FinalState.ERROR, "model_stalled", steps)
                self._messages.append(
                    {
                        "role": "user",
                        "content": (
                            "提醒:你已连续 1 轮既未调用任何工具,也未声明结束。"
                            "请调用工具(如 list_files、read_file、run_tests)推进任务,"
                            "或调用 finish 声明结束;再次空转将终止本次运行。"
                        ),
                    }
                )
                continue
            stalled_rounds = 0
            outcome = self._execute_turn(resp, steps)
            if outcome is not None:
                return outcome
            self._maybe_remind_budget(steps)
            self._maybe_nudge_stuck()
            if self._verification_rejected and steps >= self._max_steps:
                # finish(success) 复验失败被拒后步数耗尽 → TESTS_FAILED(spec)
                return self._finish_run(FinalState.TESTS_FAILED, "verification_failed", steps)

    # -- 轮次主循环(chat / REPL;设计票 03 决议 1/2/4) --

    def run_turn(self, user_message: str) -> TurnOutcome:
        """跑一轮:追加用户消息 → 主循环 → 轮末 TurnOutcome。

        chat_mode 下轮末**不发任何终报事件**(turn_result/final_report 均由
        REPL 编排层发,防双发);Ctrl+C 在此捕获并补齐缺失 tool 回执后以
        INTERRUPTED 返回——批处理的 KeyboardInterrupt 路径(cli.py)不受影响。
        """
        self._messages.append({"role": "user", "content": user_message})
        try:
            return self._chat_loop()
        except KeyboardInterrupt:
            self._repair_interrupted_tool_results()
            return self._turn_outcome(
                TurnState.INTERRUPTED, "user_interrupted", self._steps
            )

    def _chat_loop(self) -> TurnOutcome:
        started = self._clock()
        steps = 0
        while True:
            self._steps = steps
            if steps >= self._max_steps:
                return self._turn_outcome(TurnState.MAX_STEPS, "steps_exhausted", steps)
            if self._clock() - started > self._timeout_seconds:
                return self._turn_outcome(TurnState.TIMEOUT, "deadline_exceeded", steps)
            self._maybe_trim()
            try:
                resp, steps = self._call_llm(steps)
            except ProviderError as exc:
                return self._turn_outcome(
                    TurnState.ERROR, _provider_reason(exc), steps, detail=str(exc)
                )
            steps += 1

            self._messages.append(_assistant_message(resp))

            if not resp.tool_calls:
                # chat 模式:普通文本回复(含空回复)= 合法轮末,不注入 stall 提醒
                return self._turn_outcome(TurnState.REPLIED, "text_reply", steps)
            outcome = self._execute_turn(resp, steps)
            if outcome is not None:
                return self._as_turn_outcome(outcome)
            if self._verification_rejected and steps >= self._max_steps:
                # finish(success) 复验失败被拒后步数耗尽 → TESTS_FAILED(与批处理一致)
                return self._turn_outcome(
                    TurnState.TESTS_FAILED, "verification_failed", steps
                )

    def _turn_outcome(
        self,
        state: TurnState,
        reason: str,
        steps: int,
        *,
        summary: str = "",
        detail: str = "",
    ) -> TurnOutcome:
        return TurnOutcome(
            state=state,
            reason=reason,
            steps=steps,
            prompt_tokens=self._usage.prompt_tokens,
            completion_tokens=self._usage.completion_tokens,
            total_tokens=self._usage.total_tokens,
            model=self._provider.model,
            summary=summary,
            detail=detail,
        )

    @staticmethod
    def _as_turn_outcome(outcome: RunOutcome) -> TurnOutcome:
        """轮内终态(FinalState 同名六值)→ TurnOutcome。"""
        return TurnOutcome(
            state=TurnState(outcome.state.value),
            reason=outcome.reason,
            steps=outcome.steps,
            prompt_tokens=outcome.prompt_tokens,
            completion_tokens=outcome.completion_tokens,
            total_tokens=outcome.total_tokens,
            model=outcome.model,
            summary=outcome.summary,
            detail=outcome.detail,
        )

    def _repair_interrupted_tool_results(self) -> None:
        """为已入历史但未回执的 tool_call 补中断回执,保证下一请求合法。"""
        answered = {
            m.get("tool_call_id") for m in self._messages if m.get("role") == "tool"
        }
        missing = [
            tc["id"]
            for m in self._messages
            for tc in m.get("tool_calls") or []
            if tc.get("id") not in answered
        ]
        for call_id in missing:
            self._messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": INTERRUPTED_TOOL_RESULT,
                }
            )

    def emergency_error_outcome(self, detail: str) -> RunOutcome:
        """防御路径:循环应内部消化一切 Provider/内部错误,此方法兜底意外泄漏。

        经 ``_finish_run`` 产出——ERROR 终态同样落 ``final_report`` 事件,
        保证所有终态在 session.jsonl 与终报中都有交代。
        """
        return self._finish_run(FinalState.ERROR, "provider_error", self._steps, detail=detail)

    def interrupted_outcome(self) -> RunOutcome:
        """手动中断(Ctrl+C):如实以 ERROR/interrupted 终结,改动与日志保留。"""
        return self._finish_run(
            FinalState.ERROR,
            "interrupted",
            self._steps,
            detail="运行被手动中断(Ctrl+C):已完成的改动与日志全部保留,不自动回滚",
        )

    def _finish_run(
        self,
        state: FinalState,
        reason: str,
        steps: int,
        *,
        summary: str | None = None,
        detail: str = "",
    ) -> RunOutcome:
        outcome = RunOutcome(
            state=state,
            reason=reason,
            steps=steps,
            prompt_tokens=self._usage.prompt_tokens,
            completion_tokens=self._usage.completion_tokens,
            total_tokens=self._usage.total_tokens,
            model=self._provider.model,
            summary=summary or "",
            detail=detail,
        )
        if self._chat_mode:
            # chat 模式轮末不发终报事件(turn_result/final_report 由 08 编排层发)
            return outcome
        self._logger.emit(
            "final_report",
            state=outcome.state.value,
            reason=outcome.reason,
            steps=outcome.steps,
            prompt_tokens=outcome.prompt_tokens,
            completion_tokens=outcome.completion_tokens,
            total_tokens=outcome.total_tokens,
            model=outcome.model,
        )
        return outcome

    # -- 一步 LLM 往返 --

    def _call_llm(self, step: int) -> tuple[LLMResponse, int]:
        messages, tools = self._executor.hooks.before_llm_call(
            list(self._messages), self._registry.definitions()
        )
        self._logger.emit("llm_request", step=step + 1, messages=messages, tools=tools)
        started = self._clock()
        resp = self._provider.complete(messages, tools=tools, on_event=self._stream)
        duration_ms = int((self._clock() - started) * 1000)
        self._usage.add(resp.usage)
        self._logger.emit(
            "llm_response",
            step=step + 1,
            text=resp.text,
            reasoning=resp.reasoning,
            tool_calls=[
                {"index": tc.index, "id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in resp.tool_calls
            ],
            usage={
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
                "total_tokens": resp.usage.total_tokens,
                "estimated": resp.usage.estimated,
            },
            finish_reason=resp.finish_reason,
            duration_ms=duration_ms,
        )
        self._last_prompt_tokens = resp.usage.prompt_tokens
        return resp, step

    def _maybe_trim(self) -> None:
        """上一响应 prompt_tokens 超阈值 → 整组裁剪最老交互并落 context_trim 事件。"""
        prompt = self._last_prompt_tokens
        if prompt is None:
            prompt = _history_tokens(self._messages)
        if prompt <= self._max_context_tokens:
            return
        grouping = "turn" if self._chat_mode else "tool"
        new_messages, removed = trim_history(
            self._messages, max_prompt_tokens=self._max_context_tokens, grouping=grouping
        )
        if not removed:
            return
        if self._chat_mode:
            # chat 模式按 user 轮次切(批处理事件字段保持原样)
            removed_turns = sum(1 for m in removed if m.get("role") == "user")
            self._logger.emit(
                "context_trim",
                trigger_prompt_tokens=prompt,
                max_context_tokens=self._max_context_tokens,
                grouping="turn",
                removed_turns=removed_turns,
                removed_messages=removed,  # 原始消息入日志,append-only 可还原
                kept_messages=len(new_messages),
            )
        else:
            removed_groups = len(_find_interaction_groups(removed))
            self._logger.emit(
                "context_trim",
                trigger_prompt_tokens=prompt,
                max_context_tokens=self._max_context_tokens,
                removed_groups=removed_groups,
                removed_messages=removed,  # 原始消息入日志,append-only 可还原
                kept_messages=len(new_messages),
            )
        self._messages = new_messages

    # -- 一轮工具执行(≤8,按序;超出补「未执行」结果) --

    def _maybe_remind_budget(self, steps: int) -> None:
        """批处理一次性预算提醒:步数用至 75% 仍无写入或结束声明时注入。

        只防「过度准备耗尽预算」(真实彩排失败 2 的教训——stall 检测防空转,
        不防只读探索);复用 stall 提醒的 user 消息注入通道;「是否已产出」只看
        工具元数据(is_writer / terminal),不按工具名特判。chat 模式不注入:
        user 消息是轮次边界,注入会干扰 turn 分组裁剪与 /compact 切点。
        """
        if self._chat_mode or self._budget_reminded or self._produced:
            return
        threshold = math.ceil(self._max_steps * BUDGET_REMINDER_RATIO)
        if threshold <= steps < self._max_steps:
            self._budget_reminded = True
            self._messages.append(
                {
                    "role": "user",
                    "content": (
                        f"预算提醒:已用 {steps}/{self._max_steps} 步,"
                        "尚未产生写入或结束声明。请优先落地产物或如实 finish;"
                        "继续只读探索将耗尽步数。"
                    ),
                }
            )

    def _maybe_nudge_stuck(self) -> None:
        """批处理一次性卡顿/收尾提醒:同签名重复调用达阈值时注入(工单 06)。

        只提醒、不新增终止路径——最坏情况退化为现状(MAX_STEPS)。chat 模式
        不注入(user 消息是轮次边界,会干扰 turn 分组裁剪与 /compact 切点,
        理由同预算提醒):pending 直接丢弃,REPL 每轮新实例,不跨轮残留。
        """
        if self._chat_mode or not self._pending_nudge:
            return
        self._messages.append({"role": "user", "content": self._pending_nudge})
        self._pending_nudge = None

    def _execute_turn(self, resp: LLMResponse, step: int) -> RunOutcome | None:
        """执行本轮全部工具调用;返回 RunOutcome 表示循环就此终止。"""
        calls = resp.tool_calls
        for index, call in enumerate(calls):
            if self._registry.is_writer(call.name):
                self._produced = True  # writer 工具被调用即视为进入产出阶段
            if index >= MAX_TOOL_CALLS_PER_TURN:
                result = self._executor.skip(
                    call, step=step, reason="未执行:超过单轮上限(8 个工具调用)"
                )
                self._notify_tool_result(result)
            else:
                self._activity(step, call)
                result = self._executor.run(call, step=step)
                self._notify_tool_result(result)
                if not result.terminal:
                    # 卡死检测(工单 06):只喂实际执行的非 terminal 调用;
                    # skip(超上限/finish 被拒后跳过)与 finish 不喂
                    nudge = self._stuck_monitor.observe(call, result)
                    self._pending_nudge = self._pending_nudge or nudge
            if result.terminal:
                self._produced = True  # 结束声明已出现,预算提醒失去对象
                outcome = self._handle_terminal(call, result, step)
                if outcome is not None:
                    return outcome
                # 终止被拒(复验失败/声明无法识别):本轮剩余调用补「未执行」
                # 回执——每个 tool_call id 都要有结果,下一轮请求才合法
                for remaining in calls[index + 1 :]:
                    skipped = self._executor.skip(
                        remaining, step=step, reason="未执行:本轮 finish 被拒绝后跳过"
                    )
                    self._notify_tool_result(skipped)
                    self._messages.append(
                        _tool_message(remaining.id, skipped.content, is_error=True)
                    )
                return None
            self._messages.append(
                _tool_message(call.id, result.content, is_error=result.is_error)
            )
        return None

    # -- 终止语义(只读元数据,不认识具体工具名) --

    def _handle_terminal(self, call: ToolCall, result, step: int) -> RunOutcome | None:
        declared = result.finish_declared
        if declared == RESULT_FAILED:
            # finish(failed):直接终止,如实报告模型认输
            self._messages.append(_tool_message(call.id, result.content, is_error=False))
            return self._finish_run(
                FinalState.TESTS_FAILED, "model_declared_failure", step, summary=result.summary
            )
        if declared == RESULT_SUCCESS:
            return self._handle_finish_success(call, result, step)
        # terminal 但无声明:按约定未知,拒绝终止并回填(isError)
        self._messages.append(
            _tool_message(
                call.id,
                f"{result.content}\n终止声明无法识别(finish_declared 缺失),已忽略本次终止请求",
                is_error=True,
            )
        )
        return None

    def _handle_finish_success(self, call: ToolCall, result, step: int) -> RunOutcome | None:
        """声明时现场复验:不信任任何历史测试结果,重跑全部 verifier 工具。"""
        verify_results = self._run_verifiers(step)
        if not verify_results:
            self._messages.append(_tool_message(call.id, result.content, is_error=False))
            return self._finish_run(
                FinalState.UNVERIFIED, "no_test_command", step, summary=result.summary
            )

        outputs = "\n\n".join(
            f"--- {r.name} ---\n{r.content}" for r in verify_results
        )
        if all(r.verification_passed is True for r in verify_results):
            self._messages.append(_tool_message(call.id, result.content, is_error=False))
            return self._finish_run(
                FinalState.SUCCESS, "verification_passed", step, summary=result.summary
            )

        # 无法验证的情形:未配置测试命令 / 测试命令不存在(不是"测试失败")
        for r in verify_results:
            if r.verification_passed is None:
                if r.unverified_reason == "no_test_command":
                    self._messages.append(_tool_message(call.id, result.content, is_error=False))
                    return self._finish_run(
                        FinalState.UNVERIFIED, "no_test_command", step, summary=result.summary
                    )
                if r.unverified_reason == "test_command_not_found":
                    self._messages.append(
                        _tool_message(call.id, r.content, is_error=True)
                    )
                    return self._finish_run(
                        FinalState.ERROR, "test_command_not_found", step, summary=result.summary
                    )

        # 复验失败:拒绝本次 finish,输出回填历史,继续修复循环
        self._verification_rejected = True
        self._messages.append(
            _tool_message(
                call.id,
                (
                    "已拒绝 finish(success):现场复验未通过(声明时复验不采信任何历史结果)。"
                    "请根据以下最新输出继续修复,然后重新验证:\n" + outputs
                ),
                is_error=True,
            )
        )
        return None

    def _run_verifiers(self, step: int) -> list:
        """向 ToolRegistry 取标记为 verifier 的工具重跑(不硬编码工具名)。"""
        results = []
        for index, tool in enumerate(self._registry.verifiers()):
            call = ToolCall(
                index=0,
                id=f"verify_{step}_{index}_{secrets.token_hex(3)}",
                name=tool.name,
                arguments="{}",
            )
            self._activity(step, call)
            result = self._executor.run(call, step=step)
            self._notify_tool_result(result)
            results.append(result)
        return results

    def _activity(self, step: int, call: ToolCall) -> None:
        if self._on_activity is not None:
            self._on_activity(f"[步骤 {step}] 调用工具 {call.name}")

    def _notify_tool_result(self, result: ToolResult) -> None:
        """工具结果回调(执行、skip、verifier 重跑都触发);批处理不传时无行为变化。"""
        if self._on_tool_result is not None:
            self._on_tool_result(result)


def _provider_reason(exc: ProviderError) -> str:
    if isinstance(exc, ResponseTruncatedError):
        return "response_truncated"
    if isinstance(exc, FakeScriptExhausted):
        return "fake_script_exhausted"
    return "provider_error"
