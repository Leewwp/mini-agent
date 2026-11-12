"""finish 工具:显式的终止语义(spec「工具集」)。

- finish(result="success") 触发**声明时现场复验**:循环不信任任何历史
  测试结果,当场经 verifier 工具重跑;通过才 SUCCESS,失败则拒绝本次
  finish、把复验输出回填历史、继续循环(步数耗尽 → TESTS_FAILED);
- finish(result="failed") 直接终止 → TESTS_FAILED(model_declared_failure)。

语义经 ToolOutput 元数据(terminal / finish_declared)传给循环,
循环不按工具名特判。
"""

from __future__ import annotations

from typing import ClassVar

from mini_agent.tools.base import ToolError, ToolOutput
from mini_agent.tools.descriptions import ZH_DESCRIPTIONS

VALID_RESULTS = ("success", "failed")


class FinishTool:
    """声明任务结束的唯一出口。"""

    name = "finish"
    description = ZH_DESCRIPTIONS["finish"]
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "result": {
                "type": "string",
                "enum": list(VALID_RESULTS),
                "description": "任务结果:success(已完成,将现场复验)或 failed(无法完成)",
            },
            "summary": {"type": "string", "description": "中文结果摘要(修改了什么/为何失败)"},
        },
        "required": ["result"],
    }

    def execute(self, args: dict) -> ToolOutput:
        result = args.get("result")
        if result not in VALID_RESULTS:
            raise ToolError(
                f"参数 result 必须是 {' 或 '.join(VALID_RESULTS)},收到:{result!r}"
            )
        summary = args.get("summary") or ""
        if not isinstance(summary, str):
            raise ToolError("参数 summary 必须是字符串")
        return ToolOutput(
            content=f"已声明任务结束:{result}。{summary}".rstrip(),
            terminal=True,
            finish_declared=result,
            summary=summary,
        )
