"""ReplApp 主循环(工单 11 / 设计票 07):脚本化输入 + StringIO,无 TTY 不挂起。

覆盖:多轮渲染(REPLIED/工具轮/SUCCESS/失败/中断)、斜杠命令、空输入、
Ctrl+C/Ctrl+D 语义、双击 Ctrl+C 2 秒窗口(注入时钟)、spinner 时序、/compact 反馈。
"""

import io

from mini_agent.executor import ToolResult
from mini_agent.loop import TurnOutcome, TurnState
from mini_agent.providers import TextDelta
from mini_agent.repl.app import DOUBLE_CTRL_C_WINDOW_SECONDS, ReplApp
from mini_agent.repl.render import Palette


class ScriptedInput:
    """脚本化输入:依次返回预设行;耗尽时抛 EOFError(= Ctrl+D)。"""

    def __init__(self, lines):
        self.lines = list(lines)
        self.consumed = 0

    def __call__(self) -> str:
        if self.consumed >= len(self.lines):
            raise EOFError("脚本输入耗尽 = Ctrl+D")
        line = self.lines[self.consumed]
        self.consumed += 1
        return line


class FakeSpinner:
    """记录 start/stop 顺序的假 spinner。"""

    def __init__(self, log, text):
        self.log = log
        self.text = text
        self.running = False

    def start(self):
        self.running = True
        self.log.append(("spinner-start", self.text))

    def stop(self):
        self.running = False
        self.log.append(("spinner-stop", self.text))


def make_outcome(state, **kwargs) -> TurnOutcome:
    defaults = {
        "reason": "r", "steps": 2, "prompt_tokens": 10, "completion_tokens": 5,
        "total_tokens": 15, "model": "fake",
    }
    defaults.update(kwargs)
    return TurnOutcome(state=state, **defaults)


class FakeScriptTurn:
    """一轮脚本:流式文本 + 可选工具回调 + 轮末 TurnOutcome。"""

    def __init__(self, text, result, tool_replies=False):
        self.text = text
        self.result = result
        self.tool_replies = tool_replies


def turn(text, state, *, tool=False, **kwargs) -> FakeScriptTurn:
    return FakeScriptTurn(text, make_outcome(state, **kwargs), tool)


class FakeTurnRunner:
    """按脚本执行轮次:模拟流式 TextDelta、工具活动与结果回调。"""

    def __init__(self, script, log):
        self.script = list(script)
        self.log = log
        self.messages = []

    def __call__(self, user_message, renderer):
        self.messages.append(user_message)
        item = self.script.pop(0)
        if item == "interrupt":
            raise KeyboardInterrupt
        if item.text:  # 真实 provider 只发非空 TextDelta
            renderer.on_event(TextDelta(item.text))
        if item.tool_replies:
            renderer.on_activity("[步骤 1] 调用工具 run_tests")
            renderer.on_tool_result(
                ToolResult(
                    call_id="c1",
                    name="run_tests",
                    full_content="命令:pytest\n退出码 0\n耗时 3 ms",
                    content="命令:pytest\n退出码 0\n耗时 3 ms",
                    duration_ms=3,
                )
            )
        return item.result


def make_app(lines, script, *, log=None, clock=None, spinner=True, **kwargs):
    log = log if log is not None else []
    runner = FakeTurnRunner(script, log)
    app = ReplApp(
        run_turn_fn=runner,
        output=io.StringIO(),
        input_fn=ScriptedInput(lines),
        spinner_factory=(lambda text: FakeSpinner(log, text)) if spinner else None,
        clock=clock if clock is not None else (lambda: 0.0),
        palette=Palette(enabled=False),
        **kwargs,
    )
    return app, runner, log


def out_text(app) -> str:
    return app._output.getvalue()


# ---------------------------------------------------------------------------
# 多轮渲染
# ---------------------------------------------------------------------------


def test_multi_turn_rendering_replied_tools_success():
    script = [
        turn("你好,我是回复。", TurnState.REPLIED),
        turn(
            "跑测试。",
            TurnState.SUCCESS,
            tool=True,
            steps=6,
            total_tokens=17600,
            summary="测试通过",
        ),
    ]
    app, runner, _ = make_app(["你好", "跑测试", "/exit"], script)
    code = app.run()
    assert code == 0
    text = out_text(app)
    assert "你好,我是回复。" in text  # 流式正文逐字上屏
    assert "✓ 已回复" in text
    assert "✓ 完成 · 6 步 · 17.6k tok" in text
    assert "模型声明:测试通过" in text
    assert "· [步骤 1] 调用工具 run_tests" in text  # 工具活动行
    assert "✓ run_tests · 退出码 0 · 3ms" in text  # 工具结果行(on_tool_result 可见)
    assert runner.messages == ["你好", "跑测试"]


def test_empty_reply_renders_placeholder():
    script = [turn("", TurnState.REPLIED)]
    app, _, _ = make_app(["继续", "/exit"], script)
    app.run()
    assert "(空回复)" in out_text(app)


def test_failed_and_unverified_turn_lines():
    script = [
        turn("失败轮。", TurnState.TESTS_FAILED),
        turn("未验证轮。", TurnState.UNVERIFIED),
        turn("中断轮。", TurnState.INTERRUPTED),
    ]
    app, _, _ = make_app(["a", "b", "c", "/exit"], script)
    app.run()
    text = out_text(app)
    assert "✗ 测试未通过" in text and "继续修复" in text
    assert "⚠ 未配置测试命令" in text
    assert "● 本轮已中断" in text


# ---------------------------------------------------------------------------
# spinner:首字前同步停、长工具成对指示、非 TTY 禁用
# ---------------------------------------------------------------------------


def test_spinner_stops_before_first_delta_and_around_tools():
    script = [turn("带工具的回复", TurnState.SUCCESS, tool=True, summary="完成")]
    app, _, log = make_app(["跑", "/exit"], script)
    app.run()
    kinds = [entry[0] for entry in log]
    # 时序:思考 spinner start → 首个 TextDelta 前同步 stop → 工具活动 start → 工具结果 stop
    assert kinds[0] == "spinner-start" and log[0][1] == "思考中…"
    assert kinds[1] == "spinner-stop"
    assert ("spinner-start", "工具执行中…") in log
    assert kinds[-1] == "spinner-stop"
    starts = sum(1 for k in kinds if k == "spinner-start")
    stops = sum(1 for k in kinds if k == "spinner-stop")
    assert starts == stops  # 严格成对,无残留线程


def test_spinner_disabled_without_factory():
    script = [turn("x", TurnState.REPLIED)]
    app, _, _ = make_app(["hi", "/exit"], script, spinner=False)
    code = app.run()
    assert code == 0  # factory=None 全程 no-op,不报错


# ---------------------------------------------------------------------------
# 斜杠命令 / 空输入
# ---------------------------------------------------------------------------


def test_help_new_clear_exit_commands():
    new_calls = []
    script = [turn("x", TurnState.REPLIED)]
    app, _, _ = make_app(
        ["/help", "  ", "/new", "/clear", "hi", "/exit"],
        script,
        on_new=lambda: new_calls.append(1),
    )
    code = app.run()
    assert code == 0
    text = out_text(app)
    assert "/compact" in text and "未知命令" not in text
    assert text.count("已开启新会话") == 2  # /new 与 /clear 行为一致
    assert len(new_calls) == 2
    assert "✓ 已回复" in text  # 空输入被忽略,只跑了一轮(hi)


def test_unknown_slash_command_only_hints_help():
    script = []
    app, runner, _ = make_app(["/model gpt-5", "/exit"], script)
    app.run()
    assert "未知命令,/help 查看可用命令" in out_text(app)
    assert runner.messages == []  # 未当作用户消息发送


def test_compact_command_spinner_and_completion():
    compact_calls = []
    log = []

    def fake_compact(instructions):
        compact_calls.append(instructions)

    script = [turn("ok", TurnState.REPLIED)]
    app, _, _ = make_app(
        ["/compact 只保留测试上下文", "hi", "/exit"],
        script,
        log=log,
        on_compact=fake_compact,
    )
    app.run()
    assert compact_calls == ["只保留测试上下文"]
    assert ("spinner-start", "正在压缩…") in log
    assert ("spinner-stop", "正在压缩…") in log


def test_compact_interrupted_returns_to_input():
    log = []

    def interrupting_compact(instructions):
        raise KeyboardInterrupt

    script = [turn("ok", TurnState.REPLIED)]
    app, runner, _ = make_app(
        ["/compact", "继续", "/exit"], script, log=log, on_compact=interrupting_compact
    )
    code = app.run()
    assert code == 0
    assert "已中断压缩:历史保持原样" in out_text(app)
    assert ("spinner-stop", "正在压缩…") in log
    assert runner.messages == ["继续"]  # 中断后回到输入,下一行正常处理


def test_compact_failure_reports_and_continues():
    def failing_compact(instructions):
        raise RuntimeError("模型服务不可用")

    script = [turn("ok", TurnState.REPLIED)]
    app, _, _ = make_app(["/compact", "hi", "/exit"], script, on_compact=failing_compact)
    app.run()
    assert "压缩失败:模型服务不可用" in out_text(app)


def test_compact_without_handler_hints_unsupported():
    script = []
    app, _, _ = make_app(["/compact", "/exit"], script)
    app.run()
    assert "不支持 /compact" in out_text(app)


# ---------------------------------------------------------------------------
# 信号:Ctrl+C / Ctrl+D / 双击窗口
# ---------------------------------------------------------------------------


class Clock:
    """可手动推进的注入时钟。"""

    def __init__(self):
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class InterruptibleInput:
    """把哨兵行转成 KeyboardInterrupt 的脚本化输入。"""

    def __init__(self, lines, on_interrupt=None):
        self.inputs = ScriptedInput(lines)
        self.on_interrupt = on_interrupt

    def __call__(self) -> str:
        line = self.inputs()
        if line == "__INTERRUPT__":
            if self.on_interrupt is not None:
                self.on_interrupt()
            raise KeyboardInterrupt
        return line


def test_ctrl_c_at_input_clears_line_and_double_press_exits():
    clock = Clock()
    script = [turn("ok", TurnState.REPLIED), turn("ok2", TurnState.REPLIED)]
    lines = ["hi", "__INTERRUPT__", "again", "__INTERRUPT__", "__INTERRUPT__"]
    app, runner, _ = make_app([], script, clock=clock)
    app._input_fn = InterruptibleInput(lines)
    code = app.run()
    assert code == 0
    text = out_text(app)
    assert text.count("^C(已清除当前输入") == 1  # 第一次清行不退出
    assert runner.messages == ["hi", "again"]


def test_double_ctrl_c_window_expires_after_2_seconds():
    clock = Clock()
    script = []

    def advance_past_window():
        clock.now += DOUBLE_CTRL_C_WINDOW_SECONDS + 0.1  # 超窗:第二次不退出

    lines = ["__INTERRUPT__", "__INTERRUPT__", "/exit"]
    app, _, _ = make_app([], script, clock=clock)
    app._input_fn = InterruptibleInput(lines, on_interrupt=advance_past_window)
    code = app.run()
    assert code == 0
    assert out_text(app).count("^C") == 2  # 两次都按清行处理,未触发退出


def test_interrupted_turn_then_quick_ctrl_c_exits():
    """生成态 Ctrl+C(中断本轮)后,2 秒内输入态再按 → /exit。"""
    clock = Clock()
    script = ["interrupt", turn("x", TurnState.REPLIED)]
    lines = ["跑起来", "继续", "__INTERRUPT__", "/exit"]
    app, runner, _ = make_app([], script, clock=clock)

    def advance_inside_window():
        clock.now += 0.5  # 仍在窗口内

    app._input_fn = InterruptibleInput(lines, on_interrupt=advance_inside_window)
    code = app.run()
    assert code == 0
    assert "● 本轮已中断" in out_text(app)  # 中断轮如实渲染
    assert runner.messages == ["跑起来", "继续"]


def test_ctrl_d_eof_exits():
    script = [turn("ok", TurnState.REPLIED)]
    exit_calls = []
    app, _, _ = make_app(["hi", "/exit"], script, on_exit=lambda: exit_calls.append(1))
    code = app.run()
    assert code == 0 and len(exit_calls) == 1

    # 输入耗尽 = EOFError = /exit
    script2 = [turn("ok", TurnState.REPLIED)]
    exit_calls2 = []
    app2, _, _ = make_app(["hi"], script2, on_exit=lambda: exit_calls2.append(1))
    code2 = app2.run()
    assert code2 == 0 and len(exit_calls2) == 1


def test_banner_printed_once():
    script = []
    app, _, _ = make_app(["/exit"], script, banner="mini-agent 交互会话(输入 /help 查看命令)")
    app.run()
    assert out_text(app).startswith("mini-agent 交互会话")
