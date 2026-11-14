"""spinner 搬运件(工单 11)与 prompt 构造(轻量冒烟;不依赖 TTY)。"""

import os
import time
from pathlib import Path

from mini_agent.repl.prompt import build_prompt_session, history_file_path
from mini_agent.repl.waiting import WaitingSpinner


def test_waiting_spinner_start_stop_no_rich_no_tty():
    """非 TTY 下 start/stop 全程无异常、无线程残留(去 rich 化搬运件)。"""
    spinner = WaitingSpinner("测试", delay=0.01)
    spinner.start()
    time.sleep(0.03)
    spinner.stop()  # join(timeout=delay) 同步停止
    spinner._thread.join(timeout=1.0)  # 测试侧兜底 join,消除与 sleep 周期的竞态
    assert not spinner._thread.is_alive()


def test_waiting_spinner_has_apache_header():
    source = Path(__file__).parents[1] / "src" / "mini_agent" / "repl" / "waiting.py"
    text = source.read_text(encoding="utf-8")
    assert "Apache License 2.0" in text
    assert "已修改" in text  # §4(b) 修改声明
    assert "Aider-AI/aider" in text


def test_history_file_permissions(tmp_path):
    path = history_file_path(tmp_path / "home")
    assert path.exists()
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert (tmp_path / "home").stat().st_mode & 0o777 == 0o700


def test_build_prompt_session_injectable_history(tmp_path):
    session = build_prompt_session(tmp_path / "hist")
    assert session is not None  # 构造不打开终端(prompt 时才用)
