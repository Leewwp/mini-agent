"""卡死检测与纠正性提醒(工单 06):纯逻辑监测器,loop 只管注入。

对每个**实际执行**的工具调用维护「当前签名连续出现」的 streak,签名一变,
streak 连同全部计数与已提醒标记重置。三模式是同一 streak 上的不同判定:

- P1 同签名连续失败 3 次 → 卡顿提醒(修正参数/换思路,或如实 finish);
- P2 同签名同结果冻结 4 次 → 卡顿提醒(重复执行不再产生新信息);
- P3 同签名成功复跑 3 次 → 收尾提醒(验收已满足即 finish,勿烧步数)。

出处:OpenHands software-agent-sdk 的 StuckDetector(MIT;action_error=3 /
action_observation=4 的软干预 nudge 级——只注入一次性 user 消息,不新增终止
路径);P3 为社区先例(gemini-cli 讨论 #23240)+ 本仓 v2 失败证据(同一验收
命令连续 6 次全过仍不 finish),非 OpenHands 既有模式。与工单 04 预算提醒的
分工:它防「只读探索耗尽预算」,本模块防「写后同签名调用原地打转」。
"""

from __future__ import annotations

import json

from mini_agent.executor import ToolResult
from mini_agent.providers import ToolCall

ACTION_ERROR_THRESHOLD = 3  # P1:同签名连续失败次数(OpenHands action_error)
ACTION_OBSERVATION_THRESHOLD = 4  # P2:同签名同结果次数(action_observation)
ACTION_SUCCESS_THRESHOLD = 3  # P3:同签名成功复跑次数(社区先例 + 本仓证据 B)

ERROR_EXCERPT_LIMIT = 120  # P1 文案里最近错误的摘录上限(字)


def signature(call: ToolCall) -> tuple[str, str]:
    """调用签名 = (工具名, 规范化参数串)。

    规范化:``json.loads`` 成功则 ``json.dumps(sort_keys=True, ensure_ascii=False)``
    (防键序漂移);失败则用参数原串(模型重复调用时参数串通常逐字相同)。
    """
    try:
        normalized = json.dumps(
            json.loads(call.arguments), sort_keys=True, ensure_ascii=False
        )
    except (json.JSONDecodeError, TypeError):
        normalized = call.arguments
    return call.name, normalized


def error_nudge_text(tool_name: str, error_content: str) -> str:
    """P1 文案(前缀「卡顿提醒:」是测试锚点,勿改);错误摘录截 ≤120 字。"""
    excerpt = error_content[:ERROR_EXCERPT_LIMIT]
    if len(error_content) > ERROR_EXCERPT_LIMIT:
        excerpt += "…"
    return (
        f"卡顿提醒:同一工具调用({tool_name})已连续第 {ACTION_ERROR_THRESHOLD} 次失败,"
        f"最近错误:{excerpt}。请修正调用参数或改换思路后再试;"
        "确实无法完成时,调用 finish 声明失败如实申报,优于继续重复。"
    )


def frozen_nudge_text(tool_name: str) -> str:
    """P2 文案(前缀「卡顿提醒:」是测试锚点,勿改)。"""
    return (
        f"卡顿提醒:同一工具调用({tool_name})已连续第 {ACTION_OBSERVATION_THRESHOLD} 次"
        "得到完全相同的结果,重复执行不会再有新信息。请改换方式推进,"
        "或以 finish 收尾(验证已过即 success;确实无法达标则如实申报失败)。"
    )


def success_nudge_text(tool_name: str) -> str:
    """P3 文案(前缀「收尾提醒:」是测试锚点,勿改)。"""
    return (
        f"收尾提醒:该命令({tool_name})已连续第 {ACTION_SUCCESS_THRESHOLD} 次成功。"
        "若验收标准已满足,请立即调用 finish 声明成功收尾;"
        "若存在无法同时满足的约束,请 finish 如实申报——继续重复同一命令将耗尽剩余步数。"
    )


class StuckMonitor:
    """同签名 streak 监测:喂实际执行的非 terminal 调用,返回应注入的提醒或 None。

    - 只喂真实执行的结果;skip 的(超上限/finish 被拒后跳过)与 terminal 的
      (finish)由调用方过滤,本类不感知;
    - 「结果」比较用 ``result.content``(给模型的、可能截断后的内容)——
      语义是「模型看到的东西没变」;
    - 三个计数独立维护:P1 错误链遇非 error 归零;P2 同结果链遇 content 变化
      归零;P3 成功链遇 error 归零。签名一变,三者与已提醒标记全部重置
      (每个 streak 每个模式最多提醒一次);
    - 同次观察多模式同时达标按 P1→P2→P3 取第一个(每步至多一条)。
    """

    def __init__(self) -> None:
        self._signature: tuple[str, str] | None = None
        self._error_streak = 0
        self._success_streak = 0
        self._frozen_streak = 0
        self._last_content: str | None = None
        self._error_reminded = False
        self._frozen_reminded = False
        self._success_reminded = False

    def _reset(self, sig: tuple[str, str]) -> None:
        """签名变化:streak 连同全部计数与已提醒标记重置。"""
        self._signature = sig
        self._error_streak = 0
        self._success_streak = 0
        self._frozen_streak = 0
        self._last_content = None
        self._error_reminded = False
        self._frozen_reminded = False
        self._success_reminded = False

    def observe(self, call: ToolCall, result: ToolResult) -> str | None:
        """喂一次已执行的非 terminal 调用;返回本次触发的提醒文本或 None。"""
        sig = signature(call)
        if sig != self._signature:
            self._reset(sig)

        # 三链独立计数(交叉重置:P1 遇成功归零 / P3 遇错误归零 / P2 遇内容变化归零)
        self._error_streak = self._error_streak + 1 if result.is_error else 0
        self._success_streak = 0 if result.is_error else self._success_streak + 1
        if result.content == self._last_content:
            self._frozen_streak += 1
        else:
            self._frozen_streak = 1
            self._last_content = result.content

        # P1 → P2 → P3:取第一个达标且未提醒的,每 streak 每模式最多一次
        if self._error_streak >= ACTION_ERROR_THRESHOLD and not self._error_reminded:
            self._error_reminded = True
            return error_nudge_text(call.name, result.content)
        if self._frozen_streak >= ACTION_OBSERVATION_THRESHOLD and not self._frozen_reminded:
            self._frozen_reminded = True
            return frozen_nudge_text(call.name)
        if self._success_streak >= ACTION_SUCCESS_THRESHOLD and not self._success_reminded:
            self._success_reminded = True
            return success_nudge_text(call.name)
        return None
