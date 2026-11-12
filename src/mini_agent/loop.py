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
- 历史裁剪按「assistant(tool_calls) + 全部对应 tool 结果」整组替换为
  一条摘要文本,绝不单独删 tool 结果;阈值用上一响应 usage.prompt_tokens
  触发,context_trim 事件入日志、可还原。
"""

from __future__ import annotations

import enum
import secrets
import time
from dataclasses import dataclass
from typing import Callable

from mini_agent.errors import (
    EXIT_ERROR,
    EXIT_MAX_STEPS,
    EXIT_SUCCESS,
    EXIT_TESTS_FAILED,
    EXIT_TIMEOUT,
    EXIT_UNVERIFIED,
)
from mini_agent.providers import (
    FakeScriptExhausted,
    LLMResponse,
    ProviderError,
    ResponseTruncatedError,
    ToolCall,
    estimate_tokens,
)
from mini_agent.session import SessionLogger
from mini_agent.tools.base import ToolRegistry

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


def _outcome(
    state: FinalState,
    reason: str,
    *,
    steps: int,
    usage,
    model: str,
    summary: str = "",
    detail: str = "",
) -> RunOutcome:
    return RunOutcome(
        state=state,
        reason=reason,
        steps=steps,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
        model=model,
        summary=summary,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# 中文系统提示词(工单 03 就位;fixture 润色属工单 05)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
你是 mini-agent,一个在本地命令行运行的编码助手。你的工作方式:

1. 用 list_files / read_file / search 阅读仓库,理解现有代码;
2. 用 write_file 修改或创建文件(整文件写入;覆盖已存在文件前必须先 read_file 读过);
3. 用 run_tests 运行用户预配置的测试命令,根据退出码与输出判断结果;
4. 测试失败时阅读错误输出,继续修改并重新验证,形成修复循环;
5. 任务完成时调用 finish(result="success", summary="…") 声明结束——系统会当场
   重跑一次测试复验,复验通过才判定成功;无法完成时调用 finish(result="failed", …)
   如实声明失败。

注意:
- run_command 只能执行 allowlist 内的命令,其结果不计入成功判定;
  验证一律使用 run_tests;
- 请始终用中文回复;不要向用户提问,自主决策并如实汇报。
"""


MAX_TOOL_CALLS_PER_TURN = 8
MIN_KEPT_GROUPS = 2  # 历史裁剪时保底保留的最近交互组数


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


def _summary_text(group_count: int, removed_messages: list[dict]) -> str:
    counts: dict[str, int] = {}
    for m in removed_messages:
        for tc in m.get("tool_calls") or []:
            name = tc.get("function", {}).get("name", "?")
            counts[name] = counts.get(name, 0) + 1
    stats = "、".join(f"{name}×{n}" for name, n in counts.items()) or "无"
    return (
        f"[历史裁剪] 为控制上下文长度,已把较早的 {group_count} 轮工具交互"
        f"({stats})整组省略并替换为本摘要;完整原始内容见会话日志的 "
        "context_trim 事件(append-only,可还原)。"
    )


def trim_history(
    messages: list[dict], *, max_prompt_tokens: int, keep_last_groups: int = MIN_KEPT_GROUPS
) -> tuple[list[dict], list[dict]]:
    """把最老的交互组整组替换为一条摘要文本,直到估算低于阈值。

    - 触发前的判断由调用方做(阈值用上一响应 usage.prompt_tokens);
    - 最近 ``keep_last_groups`` 组保底保留,绝不裁穿;
    - 返回 (新消息列表, 被移除的原始消息);后者进 context_trim 事件供还原。
    """
    groups = _find_interaction_groups(messages)
    if not groups or _history_tokens(messages) <= max_prompt_tokens:
        return messages, []

    removed_ranges: list[tuple[int, int]] = []
    for gi, span in enumerate(groups):
        if len(groups) - gi <= keep_last_groups:
            break  # 保底:最近 N 组不动
        removed_ranges.append(span)
        trial = _rebuild_with_summary(messages, removed_ranges)
        if _history_tokens(trial) <= max_prompt_tokens:
            break
    if not removed_ranges:
        return messages, []
    removed_messages = [m for start, end in removed_ranges for m in messages[start:end]]
    new_messages = _rebuild_with_summary(messages, removed_ranges)
    return new_messages, removed_messages


def _rebuild_with_summary(messages: list[dict], ranges: list[tuple[int, int]]) -> list[dict]:
    """把给定区间整组移除,在首个区间位置放一条合并摘要。"""
    drop = {i for start, end in ranges for i in range(start, end)}
    group_count = len(ranges)
    removed = [m for i, m in enumerate(messages) if i in drop]
    first = ranges[0][0]
    new_messages: list[dict] = []
    for i, m in enumerate(messages):
        if i == first:
            new_messages.append(
                {"role": "assistant", "content": _summary_text(group_count, removed)}
            )
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
    estimated: bool = False

    def add(self, usage) -> None:
        if usage.prompt_tokens is not None:
            self.prompt_tokens = (self.prompt_tokens or 0) + usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens or 0
        self.total_tokens += usage.total_tokens or 0
        if usage.estimated:
            self.estimated = True


class AgentLoop:
    """核心循环:Provider 注入模型、executor 注入工具与钩子、logger 注入日志。"""

    def __init__(
        self,
        *,
        provider,
        executor,
        logger: SessionLogger,
        system_prompt: str,
        task: str,
        max_steps: int = 20,
        timeout_seconds: float = 600.0,
        max_context_tokens: int = 60000,
        clock: Callable[[], float] | None = None,
        on_activity: Callable[[str], None] | None = None,
        stream=None,
    ) -> None:
        self._provider = provider
        self._executor = executor
        self._registry: ToolRegistry = executor.registry
        self._logger = logger
        self._messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]
        self._max_steps = max_steps
        self._timeout_seconds = timeout_seconds
        self._max_context_tokens = max_context_tokens
        self._clock = clock if clock is not None else time.monotonic
        self._on_activity = on_activity
        self._stream = stream
        self._usage = _UsageAccumulator()
        self._verification_rejected = False
        self._last_prompt_tokens: int | None = None
        self._steps = 0  # 已完成计数(中断路径终报用;run() 内同步)

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
            if self._verification_rejected and steps >= self._max_steps:
                # finish(success) 复验失败被拒后步数耗尽 → TESTS_FAILED(spec)
                return self._finish_run(FinalState.TESTS_FAILED, "verification_failed", steps)

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
        outcome = _outcome(
            state,
            reason,
            steps=steps,
            usage=self._usage,
            model=self._provider.model,
            summary=summary or "",
            detail=detail,
        )
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
        new_messages, removed = trim_history(
            self._messages, max_prompt_tokens=self._max_context_tokens
        )
        if not removed:
            return
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

    def _execute_turn(self, resp: LLMResponse, step: int) -> RunOutcome | None:
        """执行本轮全部工具调用;返回 RunOutcome 表示循环就此终止。"""
        calls = resp.tool_calls
        for index, call in enumerate(calls):
            if index >= MAX_TOOL_CALLS_PER_TURN:
                result = self._executor.skip(
                    call, step=step, reason="未执行:超过单轮上限(8 个工具调用)"
                )
            else:
                self._activity(step, call)
                result = self._executor.run(call, step=step)
            if result.terminal:
                outcome = self._handle_terminal(call, result, step)
                if outcome is not None:
                    return outcome
                # 终止被拒(复验失败/声明无法识别):本轮剩余调用补「未执行」
                # 回执——每个 tool_call id 都要有结果,下一轮请求才合法
                for remaining in calls[index + 1 :]:
                    skipped = self._executor.skip(
                        remaining, step=step, reason="未执行:本轮 finish 被拒绝后跳过"
                    )
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
        if declared == "failed":
            # finish(failed):直接终止,如实报告模型认输
            self._messages.append(_tool_message(call.id, result.content, is_error=False))
            return self._finish_run(
                FinalState.TESTS_FAILED, "model_declared_failure", step, summary=result.summary
            )
        if declared == "success":
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
            results.append(self._executor.run(call, step=step))
        return results

    def _activity(self, step: int, call: ToolCall) -> None:
        if self._on_activity is not None:
            self._on_activity(f"[步骤 {step}] 调用工具 {call.name}")


def _provider_reason(exc: ProviderError) -> str:
    if isinstance(exc, ResponseTruncatedError):
        return "response_truncated"
    if isinstance(exc, FakeScriptExhausted):
        return "fake_script_exhausted"
    return "provider_error"
