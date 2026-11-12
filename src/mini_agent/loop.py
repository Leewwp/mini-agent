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
import time
from dataclasses import dataclass, field
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


# ---------------------------------------------------------------------------
# AgentLoop
# ---------------------------------------------------------------------------

MAX_TOOL_CALLS_PER_TURN = 8
MIN_KEPT_GROUPS = 2  # 历史裁剪时保底保留的最近交互组数

ProviderFactory = Callable[[], object]


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

    # -- 主循环 --

    def run(self) -> RunOutcome:
        started = self._clock()
        stalled_rounds = 0
        steps = 0
        while True:
            if steps >= self._max_steps:
                return self._finish_run(FinalState.MAX_STEPS, "steps_exhausted", steps)
            if self._clock() - started > self._timeout_seconds:
                return self._finish_run(FinalState.TIMEOUT, "deadline_exceeded", steps)
            try:
                resp, steps = self._call_llm(steps)
            except ProviderError as exc:
                return self._finish_run(FinalState.ERROR, _provider_reason(exc), steps)
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
            self._execute_turn(resp, steps)

    def _finish_run(self, state: FinalState, reason: str, steps: int) -> RunOutcome:
        outcome = _outcome(
            state,
            reason,
            steps=steps,
            usage=self._usage,
            model=self._provider.model,
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
        return resp, step

    # -- 一轮工具执行(≤8,按序;超出补「未执行」结果) --

    def _execute_turn(self, resp: LLMResponse, step: int) -> None:
        for index, call in enumerate(resp.tool_calls):
            if index >= MAX_TOOL_CALLS_PER_TURN:
                result = self._executor.skip(call, step=step, reason="未执行:超过单轮上限(8 个工具调用)")
            else:
                self._activity(step, call)
                result = self._executor.run(call, step=step)
            self._messages.append(
                _tool_message(call.id, result.content, is_error=result.is_error)
            )

    def _activity(self, step: int, call: ToolCall) -> None:
        if self._on_activity is not None:
            self._on_activity(f"[步骤 {step}] 调用工具 {call.name}")


def _provider_reason(exc: ProviderError) -> str:
    if isinstance(exc, ResponseTruncatedError):
        return "response_truncated"
    return "provider_error"
