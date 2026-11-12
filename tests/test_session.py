"""SessionLogger 的外部行为测试:目录命名、首事件、flush、脱敏、repo 内回退。"""

import json
import tempfile
from datetime import datetime
from pathlib import Path

from mini_agent.session import RUNS_DIR_NAME, SessionLogger


def read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def make_logger(base_dir: Path, suffix: str = "ab12") -> SessionLogger:
    return SessionLogger.create(
        base_dir=base_dir,
        now=datetime(2026, 11, 12, 8, 5, 3),
        suffix=suffix,
    )


def test_session_dir_uses_timestamp_and_random_suffix(tmp_path):
    logger = make_logger(tmp_path)
    assert logger.session_dir == tmp_path / "20261112-080503-ab12"
    assert logger.path.name == "session.jsonl"
    assert logger.session_dir.is_dir()


def test_session_meta_is_first_event_with_model_conditions(tmp_path):
    with make_logger(tmp_path) as logger:
        logger.emit(
            "session_meta",
            model="deepseek-flash",
            base_url="https://api.deepseek.com",
            temperature=0.0,
            max_tokens=8192,
            thinking=False,
        )
        logger.emit("llm_request", messages=[{"role": "user", "content": "任务"}])
    events = read_events(logger.path)
    assert events[0]["type"] == "session_meta"
    assert events[0]["model"] == "deepseek-flash"
    assert events[0]["base_url"] == "https://api.deepseek.com"
    assert events[0]["temperature"] == 0.0
    assert "ts" in events[0]
    assert events[1]["type"] == "llm_request"


def test_emit_flushes_after_each_event(tmp_path):
    logger = make_logger(tmp_path)
    # 不关闭句柄,另起读取即要求已落盘(证明写后 flush)
    logger.emit("llm_request", messages=[{"role": "user", "content": "任务"}])
    text = logger.path.read_text(encoding="utf-8")
    assert "llm_request" in text
    logger.emit("llm_response", text="回复")
    text = logger.path.read_text(encoding="utf-8")
    assert "llm_response" in text
    logger.close()


def test_log_keeps_chinese_readable_and_records_no_secrets(tmp_path):
    with make_logger(tmp_path) as logger:
        logger.emit("llm_request", messages=[{"role": "user", "content": "你好,世界"}])
    text = logger.path.read_text(encoding="utf-8")
    assert "你好,世界" in text  # ensure_ascii=False,日志中文可读
    assert "api_key" not in text
    assert "Authorization" not in text
    assert "headers" not in text


def test_session_dir_falls_back_to_tempdir_when_inside_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    logger = SessionLogger.create(
        base_dir=repo / RUNS_DIR_NAME,
        repo=repo,
        now=datetime(2026, 11, 12, 8, 5, 3),
        suffix="cd34",
    )
    assert repo.resolve() not in logger.session_dir.resolve().parents
    assert logger.session_dir.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
    logger.close()
