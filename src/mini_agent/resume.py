"""会话恢复(工单 15 / 设计票 06 最小版):``--resume <session-dir>``。

- 数据源 = 最后一条 ``turn_end.messages``(本轮结束后完整模型视图);
- 无 turn_end(崩溃/旧日志):取最后 ``llm_request.messages``,再按序应用其后的
  ``llm_response``/``tool_result`` 事件重放;末尾缺失的 tool 结果补
  ``[会话恢复] 该工具调用结果缺失`` 占位,保证下一请求合法;
- **绝不取摘要调用的 compact_request**(它不是 llm_request);
- 压缩过的会话恢复为压缩投影(turn_end.messages 天然是模型实际所见);
- system 消息由 ChatSession 用当前提示词 + 当前 AGENTS.md 重建(设计票 06);
- repo:``--repo`` 省略时用 ``session_meta.repo``;显式给出且不一致 → 拒绝;
- 一律新目录续写 + ``session_meta.resumed_from``(旧目录只读);
- JSONL 损坏/截断 → 降级到最后一条完整事件并提示;
- ``--resume`` 无参:列出该 repo(repo-slug 匹配)最近 3 个会话目录 +
  各自首条用户消息摘要后退出(非交互可发现性,不是交互选择器)。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from mini_agent.errors import ConfigError
from mini_agent.repl.orchestrator import repl_session_root, repo_slug

RESUME_PLACEHOLDER = "[会话恢复] 该工具调用结果缺失"
LIST_LIMIT = 3


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


def _has_corrupt_tail(session_jsonl: Path, events: list[dict]) -> bool:
    """行数(非空)多于事件数 = 存在损坏/截断行。"""
    try:
        lines = [ln for ln in Path(session_jsonl).read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return False
    return len(lines) > len(events)


def session_repo_of(session_dir: Path) -> str | None:
    path = Path(session_dir) / "session.jsonl"
    if not path.is_file():
        raise ConfigError(f"会话目录不存在或缺少 session.jsonl:{session_dir}")
    for event in read_events(path):
        if event.get("type") == "session_meta":
            repo = event.get("repo")
            return str(repo) if repo else None
    return None


def resolve_resume_repo(session_dir: Path, *, explicit_repo: str | None) -> str | None:
    """恢复 repo:显式 --repo 优先且必须与会话一致;省略时用 session_meta.repo。"""
    recorded = session_repo_of(session_dir)
    if recorded is None:
        return explicit_repo
    if explicit_repo is not None and Path(explicit_repo).resolve() != Path(recorded).resolve():
        raise ConfigError(
            f"--repo 与会话仓库不一致:--repo 指向 {explicit_repo},"
            f"会话记录的仓库是 {recorded};请用对应仓库启动或省略 --repo"
        )
    return recorded


def _replay_after_llm_request(events: list[dict], request_index: int) -> list[dict]:
    """崩溃重放:llm_request.messages + 其后 llm_response/tool_result 按序应用。"""
    messages = [dict(m) for m in events[request_index].get("messages") or []]
    for event in events[request_index + 1 :]:
        kind = event.get("type")
        if kind == "llm_request":
            break  # 下一次请求开始了(不该发生,防御)
        if kind == "llm_response":
            assistant: dict = {"role": "assistant", "content": event.get("text") or None}
            tool_calls = event.get("tool_calls") or []
            if tool_calls:
                assistant["tool_calls"] = [
                    {
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {"name": tc.get("name", ""), "arguments": tc.get("arguments", "")},
                    }
                    for tc in tool_calls
                ]
            messages.append(assistant)
        elif kind == "tool_result":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": event.get("call_id", ""),
                    "content": event.get("content") or "",
                }
            )
    # 末尾缺失的 tool 结果补占位,保证下一请求合法
    answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    for m in messages:
        for tc in m.get("tool_calls") or []:
            if tc.get("id") and tc["id"] not in answered:
                messages.append(
                    {"role": "tool", "tool_call_id": tc["id"], "content": RESUME_PLACEHOLDER}
                )
    return messages


def _project_after_compact(event: dict) -> list[dict]:
    """从 compact 事件重建压缩投影(设计票 06:恢复为模型实际所见,非原文全量)。

    compact 事件存 summary + replaced_messages(全文);新历史 =
    [system(原), user(SUMMARY_PREFIX+摘要), 最近 2 个完整 user 轮次]——
    与 compact_history 的构建规则一致,可确定性重建。
    """
    from mini_agent.loop import COMPACT_SUMMARY_PREFIX
    from mini_agent.repl.compact import _kept_tail

    replaced = event.get("replaced_messages") or []
    summary = event.get("summary") or ""
    summary_user = {"role": "user", "content": f"{COMPACT_SUMMARY_PREFIX}\n{summary}"}
    return [replaced[0], summary_user, *_kept_tail(replaced)]


def load_resume_messages(
    session_dir: Path, *, warn: Callable[[str], None] | None = None
) -> list[dict]:
    """恢复消息历史:turn_end / compact 投影优先,崩溃回退到 llm_request+重放;
    不恢复运行态(步数/usage/验证状态/快照)。"""
    session_dir = Path(session_dir)
    path = session_dir / "session.jsonl"
    if not path.is_file():
        raise ConfigError(f"会话目录不存在或缺少 session.jsonl:{session_dir}")
    events = read_events(path)
    if _has_corrupt_tail(path, events) and warn is not None:
        warn(f"会话日志存在损坏/截断行,已降级到最后一条完整事件:{path}")

    last_turn_end = -1
    last_compact = -1
    last_llm_request = -1
    for index, event in enumerate(events):
        kind = event.get("type")
        if kind == "turn_end":
            last_turn_end = index
        elif kind == "compact":
            last_compact = index
        elif kind == "llm_request":
            last_llm_request = index

    if last_compact > last_turn_end:
        # 最后一次 /compact 之后没有新轮次:恢复为压缩投影
        return _project_after_compact(events[last_compact])

    if last_turn_end >= 0 and last_turn_end > last_llm_request:
        messages = events[last_turn_end].get("messages")
        if isinstance(messages, list) and messages:
            return messages
        raise ConfigError(f"turn_end 事件的 messages 为空,无法恢复:{session_dir}")

    if last_llm_request >= 0:
        if warn is not None and last_turn_end < 0:
            warn("会话没有完整的轮次记录(turn_end),已从最后的 llm_request 重放恢复")
        return _replay_after_llm_request(events, last_llm_request)

    raise ConfigError(f"会话日志中没有可恢复的轮次(缺 turn_end/llm_request):{session_dir}")


def _first_user_message_summary(session_dir: Path) -> str:
    """会话首条用户消息摘要(来自首个 turn_start 或 llm_request)。"""
    path = Path(session_dir) / "session.jsonl"
    for event in read_events(path):
        if event.get("type") == "turn_start":
            return str(event.get("message") or "").replace("\n", " ")[:40]
        if event.get("type") == "llm_request":
            for m in event.get("messages") or []:
                if m.get("role") == "user":
                    return str(m.get("content") or "").replace("\n", " ")[:40]
    return "(无用户消息)"


def list_recent_sessions(repo: Path, *, runs_dir: Path | None = None) -> list[dict]:
    """该 repo(repo-slug 匹配)最近的会话目录(按目录名时间戳倒序,≤3 个)。"""
    slug_dir = repl_session_root(runs_dir) / repo_slug(repo)
    if not slug_dir.is_dir():
        return []
    candidates = [d for d in slug_dir.iterdir() if (d / "session.jsonl").is_file()]
    # 同秒创建的目录名时间戳相同,按 mtime(纳秒)排序保证「最近」语义
    candidates.sort(key=lambda d: (d.stat().st_mtime, d.name), reverse=True)
    sessions = []
    for session_dir in candidates:
        sessions.append(
            {
                "dir": session_dir,
                "summary": _first_user_message_summary(session_dir),
            }
        )
        if len(sessions) >= LIST_LIMIT:
            break
    return sessions


def render_session_list(sessions: list[dict]) -> str:
    lines = []
    if not sessions:
        lines.append("没有找到该仓库的历史会话(目录:~/.mini-agent/sessions/<repo-slug>/)。")
        lines.append("用 mini-agent --resume <会话目录> 恢复指定会话。")
    else:
        lines.append("该仓库最近的会话:")
        for i, item in enumerate(sessions, 1):
            lines.append(f"  {i}. {item['dir'].name}  {item['summary']}")
        lines.append(f"恢复方式:mini-agent --resume <会话目录>(完整路径:{sessions[0]['dir'].parent}/<目录名>)")
    return "\n".join(lines)
