"""REPL 输入框(工单 11 / 设计票 07):prompt_toolkit PromptSession 构造。

骨架参考 gptme(util/prompt.py,MIT)与 prompt_toolkit 官方示例:
- FileHistory + AutoSuggestFromHistory + enable_history_search(历史上/下键);
- Enter 提交、Esc+Enter 换行(multiline 下自定义 Enter 绑定);
- 历史文件放用户目录(~/.mini-agent/history,权限 0600),不放 repo 内;
- 非 TTY 场景不构造会话,由调用方降级为内置 input(经注入的 input_fn)。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.shortcuts import PromptSession

PROMPT_TEXT = "❯ "


def history_file_path(base_dir: Path | None = None) -> Path:
    """ REPL 输入历史文件路径(~/.mini-agent/history,0600;目录 0700)。"""
    base = Path(base_dir) if base_dir is not None else Path.home() / ".mini-agent"
    base.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(base, 0o700)
    except OSError:  # pragma: no cover - 权限收紧失败不阻塞(如共享目录)
        pass
    path = base / "history"
    if not path.exists():
        path.touch(0o600)
    else:
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover
            pass
    return path


def _build_key_bindings() -> KeyBindings:
    """Enter 提交;Esc+Enter 插入换行(gptme 式多行输入)。"""
    kb = KeyBindings()

    @kb.add("enter")
    def _accept(event):
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")
    def _newline(event):
        event.current_buffer.insert_text("\n")

    return kb


def build_prompt_session(history_path: Path | None = None) -> PromptSession:
    """构造常驻 PromptSession(TTY 下由 08 装配调用;测试可注入历史路径)。"""
    return PromptSession(
        history=FileHistory(str(history_path or history_file_path())),
        auto_suggest=AutoSuggestFromHistory(),
        enable_history_search=True,
        multiline=True,
        key_bindings=_build_key_bindings(),
    )


def make_plain_input(stream=None, prompt_text: str = PROMPT_TEXT) -> Callable[[], str]:
    """非 TTY 降级输入:TTY 流走内置 input();管道/文件流按行读,EOF 抛 EOFError。"""

    def _input() -> str:
        source = stream if stream is not None else sys.stdin
        if source.isatty():
            return input(prompt_text)
        line = source.readline()
        if line == "":
            raise EOFError
        return line.rstrip("\n")

    return _input
