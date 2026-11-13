"""REPL 子包(工单 11/12):行内滚动式交互 REPL(aider 顺序式骨架)。

- ``app.ReplApp``:主循环/命令分发/信号语义(全依赖注入,可测);
- ``render``:ANSI 着色、状态行、工具结果行、/help、ReplRenderer;
- ``prompt``:prompt_toolkit PromptSession(Enter 提交、Esc+Enter 换行);
- ``waiting``:spinner(Apache-2.0,源自 aider/waiting.py,已去 rich 化)。
"""

from mini_agent.repl.app import DOUBLE_CTRL_C_WINDOW_SECONDS, ReplApp
from mini_agent.repl.render import Palette, ReplRenderer, format_tokens, render_help

__all__ = [
    "DOUBLE_CTRL_C_WINDOW_SECONDS",
    "Palette",
    "ReplApp",
    "ReplRenderer",
    "format_tokens",
    "render_help",
]
