"""REPL 渲染层(工单 11):调色板/状态行/工具行/help 表/token 缩写。"""

import io

from mini_agent.executor import ToolResult
from mini_agent.loop import TurnOutcome, TurnState
from mini_agent.repl.render import (
    Palette,
    format_tokens,
    render_help,
    tool_result_line,
    turn_status_line,
)


def outcome(state, **kwargs) -> TurnOutcome:
    defaults = {
        "reason": "r", "steps": 6, "prompt_tokens": 100, "completion_tokens": 50,
        "total_tokens": 17600, "model": "fake",
    }
    defaults.update(kwargs)
    return TurnOutcome(state=state, **defaults)


def test_format_tokens_abbreviation():
    assert format_tokens(17600) == "17.6k"
    assert format_tokens(2000) == "2k"
    assert format_tokens(999) == "999"
    assert format_tokens(None) == "-"


def test_palette_disabled_by_no_color_env(monkeypatch):
    import io

    monkeypatch.setenv("NO_COLOR", "1")
    palette = Palette.auto(io.StringIO())
    assert palette.enabled is False
    assert palette.green("文本") == "文本"  # 无 ANSI 码


def test_palette_enabled_on_tty(monkeypatch):
    class FakeTty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    palette = Palette.auto(FakeTty())
    assert palette.enabled is True
    assert "\x1b[32m" in palette.green("文本") and "\x1b[0m" in palette.green("文本")


def test_status_line_family_styles_locked():
    on = Palette(enabled=True)
    off = Palette(enabled=False)

    replied = turn_status_line(outcome(TurnState.REPLIED), on)
    assert "✓ 已回复" in replied and "\x1b[32m" in replied
    assert turn_status_line(outcome(TurnState.REPLIED), off) == "✓ 已回复"

    success = turn_status_line(outcome(TurnState.SUCCESS), on)
    assert "✓ 完成 · 6 步 · 17.6k tok" in success and "\x1b[32m" in success

    failed = turn_status_line(outcome(TurnState.TESTS_FAILED), on)
    assert "✗ 测试未通过" in failed and "继续修复" in failed and "\x1b[31m" in failed

    max_steps = turn_status_line(outcome(TurnState.MAX_STEPS), on)
    assert "✗ MAX_STEPS" in max_steps and "继续" in max_steps

    unverified = turn_status_line(outcome(TurnState.UNVERIFIED), on)
    assert "⚠ 未配置测试命令" in unverified and "\x1b[33m" in unverified

    interrupted = turn_status_line(outcome(TurnState.INTERRUPTED), on)
    assert "● 本轮已中断" in interrupted and "\x1b[90m" in interrupted

    timeout = turn_status_line(outcome(TurnState.TIMEOUT), on)
    assert "✗ 本轮超时" in timeout and "继续" in timeout

    error = turn_status_line(
        outcome(TurnState.ERROR, detail="模型请求被拒绝(HTTP 401)\n可操作提示:检查 key"), on
    )
    assert "✗ 本轮出错 · 模型请求被拒绝(HTTP 401)" in error


def test_tool_result_line_summary_and_verbose():
    palette = Palette(enabled=False)
    run_tests = ToolResult(
        call_id="c1",
        name="run_tests",
        full_content="命令:python3 -m pytest -q\n退出码 1\n耗时 30 ms\n--- stdout ---\nFAILED",
        content="命令:python3 -m pytest -q\n退出码 1\n耗时 30 ms\n--- stdout ---\nFAILED",
        duration_ms=30,
    )
    line = tool_result_line(run_tests, palette=palette)
    assert line.startswith("✓ run_tests · 退出码 1 · 30ms")

    write = ToolResult(
        call_id="c2",
        name="write_file",
        full_content="已新建文件:calc.py(128 字节)",
        content="已新建文件:calc.py(128 字节)",
        duration_ms=5,
    )
    assert tool_result_line(write, palette=palette) == "✓ write_file · 已新建文件:calc.py(128 字节) · 5ms"

    failing = ToolResult(
        call_id="c3",
        name="read_file",
        full_content="工具执行失败:路径越界:拒绝访问仓库外路径 /etc/passwd",
        is_error=True,
        duration_ms=1,
    )
    err_line = tool_result_line(failing, palette=palette)
    assert err_line.startswith("✗ read_file ·") and "/etc/passwd" in err_line

    verbose_line = tool_result_line(run_tests, palette=palette, verbose=True)
    assert "stdout" in verbose_line and "FAILED" in verbose_line  # 完整输出


def test_render_help_three_columns_and_tail_note():
    palette = Palette(enabled=False)
    text = render_help(palette)
    for command in ("/help", "/new", "/exit", "/compact"):
        assert command in text
    assert "命令" in text and "说明" in text and "示例" in text
    assert "非斜杠输入将作为对话消息发送" in text
    assert "/clear 同 /new" in text
