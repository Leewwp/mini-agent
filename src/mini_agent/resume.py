"""会话恢复(工单 12 基础版 / 设计票 06 最小形态)。

数据源 = 最后一条 ``turn_end.messages``(本轮结束后完整模型视图,含最后
assistant 回复与中断修复;06 扩展崩溃回退:无 turn_end 时取最后
``llm_request`` + 按序应用其后 llm_response/tool_result 重放、缺结果补占位)。
system 消息由调用方(ChatSession._start)用当前提示词+当前 AGENTS.md 重建。
"""

from __future__ import annotations

import json
from pathlib import Path

from mini_agent.errors import ConfigError


def read_events(session_jsonl: Path) -> list[dict]:
    """逐行读取事件;损坏行即截断(降级到此前最后一条完整事件)。"""
    events: list[dict] = []
    try:
        with Path(session_jsonl).open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    break
    except OSError as exc:
        raise ConfigError(f"无法读取会话日志:{session_jsonl}({exc})") from exc
    return events


def load_resume_messages(session_dir: Path) -> list[dict]:
    """恢复消息历史:取最后一条 turn_end.messages(不含运行态/步数/usage)。"""
    path = Path(session_dir) / "session.jsonl"
    if not path.is_file():
        raise ConfigError(f"会话目录不存在或缺少 session.jsonl:{session_dir}")
    last_turn_end: dict | None = None
    for event in read_events(path):
        if event.get("type") == "turn_end":
            last_turn_end = event
    if last_turn_end is None:
        raise ConfigError(f"会话日志中没有可恢复的轮次(缺 turn_end 事件):{session_dir}")
    messages = last_turn_end.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ConfigError(f"turn_end 事件的 messages 为空,无法恢复:{session_dir}")
    return messages
