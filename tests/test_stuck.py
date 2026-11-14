"""StuckMonitor 纯单测(工单 06):直接构造调用/结果喂监测器,不跑循环。

三模式(P1 连续失败 / P2 同结果冻结 / P3 成功复跑)的阈值时机、一次性
语义、签名重置与三链独立计数——集成行为(注入通道、chat 不注入)在
test_loop.py / test_turn_loop.py。
"""

from mini_agent.executor import ToolResult
from mini_agent.providers import ToolCall
from mini_agent.stuck import (
    ACTION_ERROR_THRESHOLD,
    ACTION_OBSERVATION_THRESHOLD,
    ACTION_SUCCESS_THRESHOLD,
    ERROR_EXCERPT_LIMIT,
    StuckMonitor,
    error_nudge_text,
    frozen_nudge_text,
    signature,
    success_nudge_text,
)


def call(
    name: str = "run_command",
    arguments: str = '{"command": "echo hi"}',
    call_id: str = "c1",
) -> ToolCall:
    return ToolCall(index=0, id=call_id, name=name, arguments=arguments)


def result(content: str = "输出", *, is_error: bool = False) -> ToolResult:
    return ToolResult(
        call_id="c1",
        name="run_command",
        full_content=content,
        content=content,
        is_error=is_error,
    )


# ---------------------------------------------------------------------------
# P1:同签名连续失败 3 次
# ---------------------------------------------------------------------------


def test_p1_nudge_on_third_consecutive_error_then_once_per_streak():
    """恰在第 3 次同签名连续失败时返回文本;之后同 streak 不再(每模式一次)。

    第 4 次用不同错误内容:防 P2(同结果冻结)接棒计数,隔离验证 P1 一次性
    (同内容连错到第 4 次时 P2 会接棒提醒,那是 P2 自己的语义,另有用例覆盖)。
    """
    monitor = StuckMonitor()
    assert monitor.observe(call(), result("错A", is_error=True)) is None  # streak 1
    assert monitor.observe(call(), result("错A", is_error=True)) is None  # streak 2
    nudge = monitor.observe(call(), result("错A", is_error=True))  # streak 3 → 触发
    assert nudge is not None and nudge.startswith("卡顿提醒:")
    assert "连续第 3 次失败" in nudge and "run_command" in nudge and "错A" in nudge
    assert monitor.observe(call(), result("错B", is_error=True)) is None  # 已提醒过


def test_p1_signature_change_resets_and_new_streak_can_retrigger():
    """签名一变全部重置;新 streak 重新计数、可再次触发。"""
    monitor = StuckMonitor()
    for _ in range(3):
        nudge = monitor.observe(call(), result("错", is_error=True))
    assert nudge is not None  # 第一条 streak 已提醒
    other = call(arguments='{"command": "echo other"}')
    assert monitor.observe(other, result("错2", is_error=True)) is None  # 新 streak 1
    assert monitor.observe(other, result("错2", is_error=True)) is None  # 新 streak 2
    again = monitor.observe(other, result("错2", is_error=True))  # 新 streak 3 → 再触发
    assert again is not None and again.startswith("卡顿提醒:")


def test_p1_error_counter_resets_on_success_mid_streak():
    """同签名中途一次成功 → 错误计数归零,仍需再连错 3 次才触发。"""
    monitor = StuckMonitor()
    assert monitor.observe(call(), result("错", is_error=True)) is None
    assert monitor.observe(call(), result("错", is_error=True)) is None
    assert monitor.observe(call(), result("成功")) is None  # 错误链归零(成功链=1)
    assert monitor.observe(call(), result("错", is_error=True)) is None  # 1
    assert monitor.observe(call(), result("错", is_error=True)) is None  # 2
    assert monitor.observe(call(), result("错", is_error=True)) is not None  # 3 → 触发


# ---------------------------------------------------------------------------
# P2:同签名同结果冻结 4 次(无论成败,只看 content)
# ---------------------------------------------------------------------------


def test_p2_frozen_on_fourth_identical_content():
    """恰在第 4 次同签名同 content 触发;成败混合不影响 frozen 链。

    交替 is_error 使 P1/P3 计数互相归零、均不达标,隔离验证 P2。
    """
    monitor = StuckMonitor()
    assert monitor.observe(call(), result("同")) is None  # frozen 1
    assert monitor.observe(call(), result("同", is_error=True)) is None  # frozen 2
    assert monitor.observe(call(), result("同")) is None  # frozen 3
    nudge = monitor.observe(call(), result("同", is_error=True))  # frozen 4 → 触发
    assert nudge is not None and nudge.startswith("卡顿提醒:")
    assert "连续第 4 次" in nudge and "完全相同的结果" in nudge


def test_p2_frozen_chain_resets_on_content_change():
    """content 一变 frozen 链归零,新 content 要重新数满 4 次才触发。"""
    monitor = StuckMonitor()
    assert monitor.observe(call(), result("A")) is None
    assert monitor.observe(call(), result("A", is_error=True)) is None  # A 链 2
    assert monitor.observe(call(), result("B")) is None  # 变化 → B 链 1
    assert monitor.observe(call(), result("B", is_error=True)) is None  # B 链 2
    assert monitor.observe(call(), result("B")) is None  # B 链 3(P1=0/P3=1 不触发)
    nudge = monitor.observe(call(), result("B", is_error=True))  # B 链 4 → 触发
    assert nudge is not None and "连续第 4 次" in nudge


# ---------------------------------------------------------------------------
# P3:同签名成功复跑 3 次
# ---------------------------------------------------------------------------


def test_p3_nudge_on_third_consecutive_success_and_reset_on_error():
    """恰在第 3 次同签名连续成功触发;中途一次失败 → 成功计数归零。"""
    monitor = StuckMonitor()
    assert monitor.observe(call(), result("o1")) is None  # success 1
    assert monitor.observe(call(), result("o2")) is None  # success 2
    assert monitor.observe(call(), result("e1", is_error=True)) is None  # 成功链归零
    assert monitor.observe(call(), result("o3")) is None  # success 1
    assert monitor.observe(call(), result("o4")) is None  # success 2
    nudge = monitor.observe(call(), result("o5"))  # success 3 → 触发
    assert nudge is not None and nudge.startswith("收尾提醒:")
    assert "连续第 3 次成功" in nudge and "run_command" in nudge


def test_p3_and_p1_counters_are_independent():
    """error 与 success 计数互不干扰:成功 2 次被 1 次错误归零后,连错 3 次触发 P1。"""
    monitor = StuckMonitor()
    assert monitor.observe(call(), result("s1")) is None  # success 1
    assert monitor.observe(call(), result("s2")) is None  # success 2(未到 3)
    assert monitor.observe(call(), result("e1", is_error=True)) is None  # success 归零
    assert monitor.observe(call(), result("e2", is_error=True)) is None  # error 2
    nudge = monitor.observe(call(), result("e3", is_error=True))  # error 3 → P1
    assert nudge is not None and nudge.startswith("卡顿提醒:")


# ---------------------------------------------------------------------------
# 签名规范化与文案锚点
# ---------------------------------------------------------------------------


def test_signature_normalizes_json_key_order():
    """同参数不同键序的 JSON 视为同签名;非 JSON 参数串用原串。"""
    a = call(arguments='{"command": "echo hi", "extra": 1}')
    b = call(arguments='{"extra": 1, "command": "echo hi"}')
    assert signature(a) == signature(b)
    assert signature(call(arguments="not-json")) == ("run_command", "not-json")


def test_nudge_texts_carry_anchor_prefix_and_counts():
    """三个文案前缀(测试锚点)与「连续第 N 次」计数表述齐备;错误摘录截 ≤120 字。"""
    error_text = error_nudge_text("run_command", "boom")
    assert error_text.startswith("卡顿提醒:")
    assert f"连续第 {ACTION_ERROR_THRESHOLD} 次失败" in error_text
    assert "finish" in error_text and "run_command" in error_text

    frozen_text = frozen_nudge_text("run_tests")
    assert frozen_text.startswith("卡顿提醒:")
    assert f"连续第 {ACTION_OBSERVATION_THRESHOLD} 次" in frozen_text

    success_text = success_nudge_text("run_tests")
    assert success_text.startswith("收尾提醒:")
    assert f"连续第 {ACTION_SUCCESS_THRESHOLD} 次成功" in success_text

    long_error = "x" * 500
    long_text = error_nudge_text("run_command", long_error)
    excerpt = long_text.split("最近错误:", 1)[1].split("。请修正", 1)[0]
    assert len(excerpt) <= ERROR_EXCERPT_LIMIT + 1 and excerpt.endswith("…")
