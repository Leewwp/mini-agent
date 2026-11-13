"""REPL 渲染层(工单 11 / 设计票 07):行内滚动式,独立于批处理渲染。

- ANSI 着色常量集中定义,``NO_COLOR``/非 TTY 全关色;
- 每轮状态行样式族(✓绿/✗红/⚠黄/●灰,token 千分位缩写);
- 工具结果行(✓/✗ 工具名 · 摘要 · 耗时;--verbose 看完整输出);
- /help 三列表格;
- ``ReplRenderer`` 挂 provider 事件流(on_event)、on_activity、on_tool_result:
  流式正文逐字上屏,首个内容分片前同步停 spinner;批处理 ``cli._stream_handler``
  不受影响(本模块是 REPL 专属独立层)。
"""

from __future__ import annotations

import os
import re
import sys
from typing import TextIO

from mini_agent.executor import ToolResult
from mini_agent.loop import TurnOutcome, TurnState
from mini_agent.providers import ReasoningDelta, TextDelta, ToolCallDelta

# ---------------------------------------------------------------------------
# ANSI 常量(集中定义;Palette 关闭时原样返回纯文本)
# ---------------------------------------------------------------------------

RESET = "\x1b[0m"
COLOR_CODES = {
    "green": "\x1b[32m",
    "red": "\x1b[31m",
    "yellow": "\x1b[33m",
    "gray": "\x1b[90m",
    "cyan": "\x1b[36m",
}


class Palette:
    """极简 ANSI 调色板;``enabled=False`` 时全部 no-op(NO_COLOR/非 TTY)。"""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    @classmethod
    def auto(cls, output: TextIO | None = None) -> Palette:
        is_tty = output.isatty() if output is not None else sys.stdout.isatty()
        return cls(enabled=is_tty and not os.environ.get("NO_COLOR"))

    def color(self, name: str, text: str) -> str:
        if not self.enabled:
            return text
        return f"{COLOR_CODES[name]}{text}{RESET}"

    def green(self, text: str) -> str:
        return self.color("green", text)

    def red(self, text: str) -> str:
        return self.color("red", text)

    def yellow(self, text: str) -> str:
        return self.color("yellow", text)

    def gray(self, text: str) -> str:
        return self.color("gray", text)

    def cyan(self, text: str) -> str:
        return self.color("cyan", text)


def format_tokens(tokens: int | None) -> str:
    """token 千分位缩写:17600 → 17.6k;None → '-'。"""
    if tokens is None:
        return "-"
    if tokens < 1000:
        return str(tokens)
    return f"{tokens / 1000:.1f}".rstrip("0").rstrip(".") + "k"


# ---------------------------------------------------------------------------
# 每轮状态行(设计票 07 Answer 1:样式族锁定,文案可微调)
# ---------------------------------------------------------------------------


def turn_status_line(outcome: TurnOutcome, palette: Palette) -> str:
    state = outcome.state
    if state is TurnState.REPLIED:
        return palette.green("✓ 已回复")
    if state is TurnState.SUCCESS:
        return palette.green(
            f"✓ 完成 · {outcome.steps} 步 · {format_tokens(outcome.total_tokens)} tok"
        )
    if state is TurnState.TESTS_FAILED:
        return palette.red('✗ 测试未通过 · 可输入"继续修复"')
    if state is TurnState.MAX_STEPS:
        return palette.red('✗ MAX_STEPS · 可输入"继续"接续')
    if state is TurnState.UNVERIFIED:
        return palette.yellow("⚠ 未配置测试命令,成功未经验证")
    if state is TurnState.INTERRUPTED:
        return palette.gray('● 本轮已中断 · 输入"继续"可接续')
    if state is TurnState.TIMEOUT:
        return palette.red('✗ 本轮超时 · 可输入"继续"接续')
    # ERROR:detail 已含可操作提示,取首行
    first_line = (outcome.detail or outcome.reason or "未知错误").splitlines()[0]
    return palette.red(f"✗ 本轮出错 · {first_line}")


# ---------------------------------------------------------------------------
# 工具结果行(设计票 07 评审约束 1:摘要行/完整输出二选一)
# ---------------------------------------------------------------------------

TOOL_SUMMARY_MAX_CHARS = 60


def _tool_summary(result: ToolResult) -> str:
    """摘要:优先「退出码」行(run_tests/run_command),否则首行截断。"""
    source = result.content or result.full_content or ""
    lines = [ln for ln in source.splitlines() if ln.strip()]
    if not lines:
        return "(无输出)"
    for ln in lines[:3]:
        if "退出码" in ln:
            return ln.strip()
    summary = lines[0].strip()
    return summary[:TOOL_SUMMARY_MAX_CHARS] + ("…" if len(summary) > TOOL_SUMMARY_MAX_CHARS else "")


def tool_result_line(result: ToolResult, *, palette: Palette, verbose: bool = False) -> str:
    mark = palette.red("✗") if result.is_error else palette.green("✓")
    name = result.name
    if verbose:
        body = (result.content or result.full_content or "").replace("\n", " ⏎ ")
        body = body[:400] + ("…" if len(body) > 400 else "")
        detail = body
    else:
        detail = _tool_summary(result)
    return f"{mark} {name} · {detail} · {result.duration_ms}ms"


# ---------------------------------------------------------------------------
# /help 三列表格(设计票 07 Answer 2)
# ---------------------------------------------------------------------------

HELP_ROWS = [
    ("/help", "显示本帮助", "/help"),
    ("/new", "开启新会话(清空历史、重载 AGENTS.md)", "/new"),
    ("/exit", "退出并生成会话终报", "/exit"),
    ("/compact", "压缩上下文为语义摘要(可带聚焦指令)", "/compact 只保留测试相关上下文"),
]
HELP_TAIL_NOTE = "非斜杠输入将作为对话消息发送;/clear 同 /new;双击 Ctrl+C 或 Ctrl+D 也可退出"

_CJK_WIDTH_RE = re.compile(r"[\u3000-\u9fff\uff00-\uffef]")


def display_width(text: str) -> int:
    """终端显示宽度(CJK 记 2,其余记 1);用于表格对齐。"""
    cjk = len(_CJK_WIDTH_RE.findall(text))
    return len(text) + cjk


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def render_help(palette: Palette) -> str:
    headers = ("命令", "说明", "示例")
    col_widths = [
        max(display_width(row[i]) for row in [*HELP_ROWS, headers])
        for i in range(3)
    ]
    lines = [
        palette.cyan(
            pad(headers[0], col_widths[0])
            + "  " + pad(headers[1], col_widths[1])
            + "  " + headers[2]
        )
    ]
    for command, desc, example in HELP_ROWS:
        lines.append(
            pad(command, col_widths[0]) + "  " + pad(desc, col_widths[1]) + "  " + example
        )
    lines.append(palette.gray(HELP_TAIL_NOTE))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# spinner 生命周期包装
# ---------------------------------------------------------------------------


class SpinnerHandle:
    """spinner 启停包装:factory=None(非 TTY)时全部 no-op;stop 幂等且同步(join)。"""

    def __init__(self, factory) -> None:
        self._factory = factory
        self._spinner = None

    def start(self, text: str) -> None:
        if self._factory is None or self._spinner is not None:
            return
        self._spinner = self._factory(text)
        self._spinner.start()

    def stop(self) -> None:
        if self._spinner is None:
            return
        self._spinner.stop()  # WaitingSpinner.stop 内部 join 线程(同步)
        self._spinner = None


# ---------------------------------------------------------------------------
# 渲染器:provider 事件流 + 工具活动/结果 → 终端
# ---------------------------------------------------------------------------


class ReplRenderer:
    """REPL 行内渲染器(挂 03 的 stream/on_activity/on_tool_result 三个接缝)。

    - 流式正文逐字上屏(aider plain 路径);首个 TextDelta/ReasoningDelta/
      ToolCallDelta 前**同步**停 spinner(避免首字被覆盖);
    - ``--thinking`` 时 reasoning 走 stderr 暗色(默认不显示);
    - 轮末状态行由 :func:`turn_status_line` 输出;空回复补「(空回复)」。
    """

    def __init__(
        self,
        output: TextIO,
        *,
        palette: Palette,
        spinner: SpinnerHandle,
        verbose: bool = False,
        show_reasoning: bool = False,
        stderr: TextIO | None = None,
    ) -> None:
        self._output = output
        self._palette = palette
        self._spinner = spinner
        self._verbose = verbose
        self._show_reasoning = show_reasoning
        self._stderr = stderr
        self._dirty_line = False  # 流式文本未换行,下一次整行输出前先补换行
        self._turn_saw_text = False

    # -- provider 事件(stream 接缝) --

    def on_event(self, event) -> None:
        if isinstance(event, (TextDelta, ReasoningDelta, ToolCallDelta)):
            self._spinner.stop()  # 首个内容分片前同步停
        if isinstance(event, TextDelta):
            self._output.write(event.content)
            self._output.flush()
            self._dirty_line = True
            self._turn_saw_text = True
        elif isinstance(event, ReasoningDelta) and self._show_reasoning:
            sink = self._stderr if self._stderr is not None else sys.stderr
            sink.write(self._palette.gray(event.content))
            sink.flush()
        # ToolCallDelta / UsageInfo / Done:不渲染

    # -- 工具活动/结果(on_activity / on_tool_result 接缝) --

    def on_activity(self, line: str) -> None:
        self._line_out(self._palette.gray(f"· {line}"))
        self._spinner.start("工具执行中…")  # 长工具期间活动指示;on_tool_result 成对停止

    def on_tool_result(self, result: ToolResult) -> None:
        self._spinner.stop()
        self._line_out(tool_result_line(result, palette=self._palette, verbose=self._verbose))

    # -- 轮次边界 --

    def turn_started(self) -> None:
        self._turn_saw_text = False

    def turn_finished(self, outcome: TurnOutcome) -> None:
        self._line_out(turn_status_line(outcome, self._palette))
        if outcome.state is TurnState.REPLIED and not self._turn_saw_text:
            self._line_out(self._palette.gray("(空回复)"))
        if outcome.summary:
            self._line_out(self._palette.gray(f"模型声明:{outcome.summary}"))

    # -- 内部 --

    def _line_out(self, text: str) -> None:
        if self._dirty_line:
            self._output.write("\n")
            self._dirty_line = False
        self._output.write(text + "\n")
        self._output.flush()
