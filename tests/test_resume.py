"""会话恢复(工单 15 / 设计票 06 最小版):turn_end 恢复/崩溃重放/压缩投影/
repo 校验/损坏降级/新目录+resumed_from/无参列出。"""

import io
import json
from pathlib import Path

from mini_agent.cli import build_arg_parser, main, resolve_config
from mini_agent.errors import ConfigError
from mini_agent.providers import create_provider
from mini_agent.repl.orchestrator import repo_slug
from mini_agent.resume import (
    RESUME_PLACEHOLDER,
    list_recent_sessions,
    load_resume_messages,
    render_session_list,
    resolve_resume_repo,
)


def write_script(tmp_path, payload, name="script.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def make_repo(tmp_path, files=None):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    for fname, content in (files or {}).items():
        (repo / fname).write_text(content, encoding="utf-8")
    return repo


def run_session(tmp_path, repo, turns, stdin_lines, script_name="s.json", runs_name="runs"):
    """跑一段 REPL 会话(写入脚本 → run_repl),返回其会话目录。"""
    from mini_agent.cli import run_repl

    write_script(tmp_path, turns, script_name)
    argv = [
        "--repo", str(repo), "--model", f"fake:{tmp_path / script_name}",
        "--runs-dir", str(tmp_path / runs_name),
    ]
    config = resolve_config(build_arg_parser().parse_args(argv), env={})
    output = io.StringIO()
    code = run_repl(
        config, create_provider(config.model),
        stdin=io.StringIO("\n".join(stdin_lines) + "\n"), stdout=output,
    )
    assert code == 0
    slug_dir = tmp_path / runs_name / repo_slug(repo)
    sessions = sorted(slug_dir.iterdir())
    return sessions[-1], output


def read_events_file(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def assert_pairs_valid(messages) -> None:
    pending: set[str] = set()
    for m in messages:
        for tc in m.get("tool_calls") or []:
            pending.add(tc["id"])
        if m.get("role") == "tool":
            assert m["tool_call_id"] in pending
            pending.discard(m["tool_call_id"])
    assert not pending


# ---------------------------------------------------------------------------
# 正常恢复:turn_end 数据源 + 新目录 + resumed_from + 旧目录只读
# ---------------------------------------------------------------------------


def test_resume_end_to_end_via_main(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path, {"README.md": "x"})
    script1 = {"turns": [{"text": "第一轮回复"}, {"text": "第二轮回复"}]}
    first_dir, _ = run_session(
        tmp_path, repo, script1, ["问题一", "问题二", "/exit"], "s1.json",
    )
    first_log_before = (first_dir / "session.jsonl").read_bytes()

    script2 = {"turns": [{"text": "恢复后的回复"}]}
    monkeypatch.setattr("sys.stdin", io.StringIO("继续干活\n/exit\n"))
    code = main([
        "--resume", str(first_dir),
        "--model", f"fake:{write_script(tmp_path, script2, 's2.json')}",
        "--runs-dir", str(tmp_path / "runs"),
    ])
    assert code == 0
    # 旧目录未被修改
    assert (first_dir / "session.jsonl").read_bytes() == first_log_before
    # 新目录 + resumed_from;恢复的历史进入下一轮请求
    slug_dir = tmp_path / "runs" / repo_slug(repo)
    sessions = sorted(slug_dir.iterdir())
    assert len(sessions) == 2
    new_dir = sessions[-1] if sessions[-1] != first_dir else sessions[0]
    events = read_events_file(new_dir / "session.jsonl")
    meta = events[0]
    assert meta["resumed_from"] == str(first_dir.resolve())
    assert meta["mode"] == "chat"
    request = next(e for e in events if e["type"] == "llm_request")
    contents = json.dumps(request["messages"], ensure_ascii=False)
    assert "问题一" in contents and "第二轮回复" in contents  # 最后一轮回复不丢
    assert request["messages"][-1] == {"role": "user", "content": "继续干活"}
    assert request["messages"][0]["role"] == "system"
    out = capsys.readouterr().out
    assert "会话终报" in out


def test_resume_repo_mismatch_rejected(tmp_path):
    repo = make_repo(tmp_path, {"README.md": "x"})
    script = {"turns": [{"text": "回复"}]}
    first_dir, _ = run_session(tmp_path, repo, script, ["问", "/exit"], "s1.json")
    other = make_repo(tmp_path / "other-place", {"README.md": "y"})
    with __import__("pytest").raises(ConfigError, match="不一致"):
        resolve_resume_repo(first_dir, explicit_repo=str(other))


def test_resume_repo_default_from_session_meta(tmp_path):
    repo = make_repo(tmp_path, {"README.md": "x"})
    script = {"turns": [{"text": "回复"}]}
    first_dir, _ = run_session(tmp_path, repo, script, ["问", "/exit"], "s1.json")
    recorded = resolve_resume_repo(first_dir, explicit_repo=None)
    assert Path(recorded).resolve() == repo.resolve()


def test_resume_missing_dir_is_config_error(tmp_path):
    import pytest

    with pytest.raises(ConfigError):
        load_resume_messages(tmp_path / "no-such-session")


# ---------------------------------------------------------------------------
# 崩溃恢复:llm_request + 重放,缺 tool 结果补占位
# ---------------------------------------------------------------------------


def make_crashed_log(session_dir: Path, *, with_partial_tools: bool = True) -> None:
    """构造崩溃日志:turn_end 缺失,尾部 assistant(tool_calls) 只有部分结果。"""
    session_dir.mkdir(parents=True, exist_ok=True)
    events = [
        {"type": "session_meta", "repo": "/anywhere/repo", "mode": "chat"},
        {"type": "turn_start", "turn": 1, "message": "修复测试"},
        {
            "type": "llm_request", "step": 1,
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "修复测试"},
            ],
        },
        {
            "type": "llm_response", "step": 1, "text": "我来看代码。",
            "tool_calls": [
                {"index": 0, "id": "call_a", "name": "read_file", "arguments": "{\"path\": \"calc.py\"}"},
                {"index": 1, "id": "call_b", "name": "run_tests", "arguments": "{}"},
            ],
        },
        {"type": "tool_call", "call_id": "call_a", "name": "read_file"},
        {"type": "tool_result", "call_id": "call_a", "name": "read_file", "content": "def add(a,b): …"},
    ]
    if not with_partial_tools:
        events = events[:-2]  # 连 tool_result 都没有(请求中途崩溃)
    with (session_dir / "session.jsonl").open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def test_crash_recovery_replays_and_fills_placeholders(tmp_path):
    session_dir = tmp_path / "crashed"
    make_crashed_log(session_dir)
    warnings: list[str] = []
    messages = load_resume_messages(session_dir, warn=warnings.append)
    assert warnings and "重放" in warnings[0]
    assert_pairs_valid(messages)
    # assistant 回复与其 tool_calls 都恢复
    assistant = next(m for m in messages if m.get("tool_calls"))
    assert assistant["content"] == "我来看代码。"
    assert {tc["id"] for tc in assistant["tool_calls"]} == {"call_a", "call_b"}
    # call_a 真实结果恢复;call_b 补占位
    replies = {m["tool_call_id"]: m["content"] for m in messages if m.get("role") == "tool"}
    assert "def add" in replies["call_a"]
    assert replies["call_b"] == RESUME_PLACEHOLDER


def test_crash_before_any_tool_result(tmp_path):
    session_dir = tmp_path / "crashed-early"
    make_crashed_log(session_dir, with_partial_tools=False)
    messages = load_resume_messages(session_dir)
    assert_pairs_valid(messages)
    assert {m["content"] for m in messages if m.get("role") == "tool"} == {RESUME_PLACEHOLDER}


def test_corrupt_jsonl_degrades_to_last_complete_event(tmp_path):
    repo = make_repo(tmp_path, {"README.md": "x"})
    script = {"turns": [{"text": "回复"}]}
    first_dir, _ = run_session(tmp_path, repo, script, ["问", "/exit"], "s1.json")
    # 人为追加损坏行(截断的 JSON)
    with (first_dir / "session.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"type": "turn_end", "messages": [受损截断')
    warnings: list[str] = []
    messages = load_resume_messages(first_dir, warn=warnings.append)
    assert any("降级" in w for w in warnings)
    assert messages[-1]["role"] == "assistant"  # 从最后完整的 turn_end 恢复


# ---------------------------------------------------------------------------
# 压缩投影恢复 + system 重建用当前 AGENTS.md
# ---------------------------------------------------------------------------


def test_compacted_session_resumes_as_projection(tmp_path):
    """压缩后的会话恢复为模型实际所见(摘要投影),不是原文全量。"""
    from mini_agent.loop import COMPACT_SUMMARY_PREFIX

    repo = make_repo(tmp_path, {"README.md": "x"})
    filler = "x" * 4000
    script = {
        "turns": [
            {"text": f"回复{i} {filler}"} for i in range(4)
        ] + [{"text": "## 目标\n继续\n## 关键上下文\ncalc.py"}]
    }
    first_dir, _ = run_session(
        tmp_path, repo, script,
        [f"问{i} {filler}" for i in range(4)] + ["/compact", "/exit"], "sc.json",
    )
    messages = load_resume_messages(first_dir)
    # 投影:system + 摘要 user + 最近 2 轮(不含被压缩的旧轮原文)
    contents = json.dumps(messages, ensure_ascii=False)
    assert messages[1]["content"].startswith(COMPACT_SUMMARY_PREFIX)
    assert f"回复0 {filler}" not in contents  # 旧轮原文不在恢复视图
    assert f"回复3 {filler}" in contents  # 最近轮保留


def test_resume_rebuilds_system_with_current_agents_md(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path, {"README.md": "x"})
    script1 = {"turns": [{"text": "第一轮回复"}]}
    first_dir, _ = run_session(tmp_path, repo, script1, ["问题一", "/exit"], "s1.json")
    # 恢复前 AGENTS.md 出现(旧会话没有记忆注入)
    (repo / "AGENTS.md").write_text("新约定:提交前跑 ruff", encoding="utf-8")
    script2 = {"turns": [{"text": "ok"}]}
    monkeypatch.setattr("sys.stdin", io.StringIO("继续\n/exit\n"))
    code = main([
        "--resume", str(first_dir),
        "--model", f"fake:{write_script(tmp_path, script2, 's2.json')}",
        "--runs-dir", str(tmp_path / "runs"),
    ])
    assert code == 0
    slug_dir = tmp_path / "runs" / repo_slug(repo)
    new_dir = max(d for d in slug_dir.iterdir() if d != first_dir)
    events = read_events_file(new_dir / "session.jsonl")
    request = next(e for e in events if e["type"] == "llm_request")
    system = request["messages"][0]["content"]
    assert "新约定:提交前跑 ruff" in system  # 当前 AGENTS.md 生效
    assert "问题一" in json.dumps(request["messages"], ensure_ascii=False)  # 其余历史沿用


# ---------------------------------------------------------------------------
# --resume 无参:列出最近 3 个
# ---------------------------------------------------------------------------


def test_resume_listing_shows_recent_three(tmp_path, capsys):
    repo = make_repo(tmp_path, {"README.md": "x"})
    other_repo = make_repo(tmp_path / "elsewhere", {"README.md": "y"})
    for i in range(4):
        script = {"turns": [{"text": f"第{i}轮回复"}]}
        run_session(
            tmp_path, repo, script, [f"用户问题编号{i}", "/exit"], f"list-{i}.json",
        )
    run_session(tmp_path, other_repo, {"turns": [{"text": "别家"}]}, ["别家问题", "/exit"], "other.json")

    code = main(["--resume", "--repo", str(repo), "--runs-dir", str(tmp_path / "runs")])
    assert code == 0
    out = capsys.readouterr().out
    assert "最近的会话" in out
    assert "用户问题编号3" in out and "用户问题编号2" in out and "用户问题编号1" in out
    assert "用户问题编号0" not in out  # 只列最近 3 个
    assert "别家问题" not in out  # 其他 repo 不混入


def test_listing_empty_hint(tmp_path, capsys):
    repo = make_repo(tmp_path)
    code = main(["--resume", "--repo", str(repo), "--runs-dir", str(tmp_path / "runs")])
    assert code == 0
    assert "没有找到" in capsys.readouterr().out


def test_list_recent_sessions_unit(tmp_path):
    repo = make_repo(tmp_path)
    sessions = list_recent_sessions(repo, runs_dir=tmp_path / "runs")
    assert sessions == []
    assert "没有找到" in render_session_list(sessions)
