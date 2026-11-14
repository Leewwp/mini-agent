"""REPL 主循环(工单 11 / 设计票 07):命令分发、信号语义、可注入可测。

顺序式 REPL(aider 骨架):取输入与跑模型在同一主线程交替;生成期输入框
让位 spinner。``ReplApp`` 全依赖注入(input_fn/output/spinner_factory/clock),
测试用 StringIO + 脚本化输入,不依赖 TTY。

信号语义(设计票 07 Answer 3 + 复审修订 2):
- Ctrl+C 输入态 = 清空当前行不退出;生成态 = 中断本轮(03 补回执)回输入;
- 任意状态第一次 Ctrl+C 按其语义处理,**2 秒内**第二次 SIGINT = /exit
  (窗口常量集中定义,时钟可注入);超窗重新计;
- Ctrl+D(EOF)= /exit;空输入忽略;未知斜杠命令仅提示 /help。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TextIO

from mini_agent.loop import TurnOutcome, TurnState
from mini_agent.repl.render import Palette, ReplRenderer, SpinnerHandle, render_help

# 双击 Ctrl+C 判定窗口(秒);设计票 07 复审修订 2
DOUBLE_CTRL_C_WINDOW_SECONDS = 2.0


class ReplApp:
    """可注入的 REPL 主循环;run_turn_fn 由 08 装配层提供。"""

    def __init__(
        self,
        *,
        run_turn_fn: Callable[[str, ReplRenderer], TurnOutcome],
        output: TextIO,
        input_fn: Callable[[], str],
        spinner_factory: Callable | None = None,
        clock: Callable[[], float] = time.monotonic,
        verbose: bool = False,
        show_reasoning: bool = False,
        palette: Palette | None = None,
        stderr: TextIO | None = None,
        on_new: Callable[[], None] | None = None,
        on_compact: Callable[[str], None] | None = None,
        on_exit: Callable[[], None] | None = None,
        banner: str | None = None,
        patch_stdout: bool = False,
    ) -> None:
        self._run_turn_fn = run_turn_fn
        self._output = output
        self._input_fn = input_fn
        self._clock = clock
        self._palette = palette if palette is not None else Palette.auto(output)
        self._spinner = SpinnerHandle(spinner_factory)
        self._renderer = ReplRenderer(
            output,
            palette=self._palette,
            spinner=self._spinner,
            verbose=verbose,
            show_reasoning=show_reasoning,
            stderr=stderr,
        )
        self._on_new = on_new
        self._on_compact = on_compact
        self._on_exit = on_exit
        self._banner = banner
        self._patch_stdout = patch_stdout
        self._last_sigint: float | None = None

    # -- 主循环 --

    def run(self) -> int:
        if self._banner:
            self._print(self._banner)
        while True:
            try:
                raw = self._read_input()
            except KeyboardInterrupt:
                if self._note_sigint():
                    return self._exit()
                self._print(self._palette.gray("^C(已清除当前输入;双击 Ctrl+C 退出)"))
                continue
            except EOFError:
                return self._exit()
            if self._handle(raw):
                return self._exit()

    # -- 输入 --

    def _read_input(self) -> str:
        if self._patch_stdout:
            from prompt_toolkit.patch_stdout import patch_stdout

            with patch_stdout(raw=True):
                return self._input_fn()
        return self._input_fn()

    # -- 分发 --

    def _handle(self, raw: str) -> bool:
        text = raw.strip()
        if not text:
            return False  # 空输入忽略
        if text.startswith("/"):
            return self._dispatch_command(text)
        self._run_turn(text)
        return False

    def _dispatch_command(self, text: str) -> bool:
        command, _, arg = text.partition(" ")
        command = command.lower()
        if command == "/exit":
            return True
        if command in ("/new", "/clear"):  # /clear 是 /new 的别名(一行映射)
            if self._on_new is not None:
                self._on_new()
            self._print(self._palette.gray("已开启新会话(历史已清空)"))
            return False
        if command == "/help":
            self._print(render_help(self._palette))
            return False
        if command == "/compact":
            self._do_compact(arg.strip())
            return False
        self._print(self._palette.yellow("未知命令,/help 查看可用命令"))
        return False

    # -- 轮次 --

    def _run_turn(self, user_message: str) -> None:
        self._renderer.turn_started()
        self._spinner.start("思考中…")
        try:
            outcome = self._run_turn_fn(user_message, self._renderer)
        except KeyboardInterrupt:
            # 编排层被直接打断(run_turn 内部已吞掉工具轮中断);记录双击窗口
            self._record_sigint()
            outcome = self._interrupted_outcome()
        finally:
            self._spinner.stop()
        if outcome.state is TurnState.INTERRUPTED:
            self._record_sigint()  # 生成态 Ctrl+C 计入双击窗口
        self._renderer.turn_finished(outcome)

    def _interrupted_outcome(self) -> TurnOutcome:
        return TurnOutcome(
            state=TurnState.INTERRUPTED,
            reason="user_interrupted",
            steps=0,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            model="-",
        )

    # -- /compact(摘要调用由 08/04 的回调执行;此处只管 UI 反馈与中断) --

    def _do_compact(self, instructions: str) -> None:
        if self._on_compact is None:
            self._print(self._palette.yellow("当前会话不支持 /compact"))
            return
        self._spinner.start("正在压缩…")
        try:
            self._on_compact(instructions)
        except KeyboardInterrupt:
            self._record_sigint()
            self._print(self._palette.yellow("已中断压缩:历史保持原样"))
        except Exception as exc:  # noqa: BLE001 —— 压缩失败不动历史,提示后回输入
            self._print(self._palette.red(f"压缩失败:{exc}"))
        finally:
            self._spinner.stop()

    # -- 双击 Ctrl+C 窗口 --

    def _record_sigint(self) -> None:
        self._last_sigint = self._clock()

    def _note_sigint(self) -> bool:
        """记录一次 Ctrl+C;返回 True 表示窗口内第二次 → 应 /exit。"""
        now = self._clock()
        last = self._last_sigint
        self._last_sigint = now
        return last is not None and now - last <= DOUBLE_CTRL_C_WINDOW_SECONDS

    # -- 输出与退出 --

    def _print(self, text: str) -> None:
        self._output.write(text + "\n")
        self._output.flush()

    def _exit(self) -> int:
        if self._on_exit is not None:
            self._on_exit()
        return 0
