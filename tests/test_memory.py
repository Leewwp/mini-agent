"""项目记忆文件(工单 14 / 设计票 05):AGENTS.md 加载/注入/安全/重载/batch 隔离。"""

import io
import json
import os

from mini_agent.cli import build_arg_parser, main, resolve_config
from mini_agent.memory import MAX_MEMORY_BYTES, load_project_memory
from mini_agent.providers import create_provider
from mini_agent.repl.orchestrator import CHAT_SYSTEM_PROMPT, ChatSession
from mini_agent.repl.render import Palette, ReplRenderer, SpinnerHandle


def make_repo(tmp_path, *, files: dict | None = None):
    repo = tmp_path / "repo"
    repo.mkdir()
    for fname, content in (files or {}).items():
        path = repo / fname
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return repo


def quiet_renderer(output):
    return ReplRenderer(output, palette=Palette(enabled=False), spinner=SpinnerHandle(None))


def make_session(tmp_path, repo, turns):
    path = tmp_path / "script.json"
    path.write_text(json.dumps({"turns": turns}, ensure_ascii=False), encoding="utf-8")
    argv = ["--repo", str(repo), "--model", f"fake:{path}", "--runs-dir", str(tmp_path / "runs")]
    config = resolve_config(build_arg_parser().parse_args(argv), env={})
    output = io.StringIO()
    session = ChatSession(
        config=config, provider=create_provider(config.model), output=output,
        runs_dir=tmp_path / "runs",
    )
    return session, output


def events_of(session, type_):
    path = session.session_dir / "session.jsonl"
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [e for e in events if e.get("type") == type_]


# ---------------------------------------------------------------------------
# 加载函数:存在/不存在/截断/UTF-8 容错/符号链接逃逸/非常规文件
# ---------------------------------------------------------------------------


def test_load_memory_missing_file_returns_none(tmp_path):
    repo = make_repo(tmp_path)
    assert load_project_memory(repo) is None


def test_load_memory_reads_root_agents_md(tmp_path):
    repo = make_repo(tmp_path, files={"AGENTS.md": "# 项目约定\n用 ruff 检查代码。"})
    memory = load_project_memory(repo)
    assert memory is not None
    assert "用 ruff" in memory.text
    assert memory.event_fields == {"path": "AGENTS.md", "bytes": os.path.getsize(repo / "AGENTS.md"), "truncated": False}


def test_load_memory_truncates_at_32kib(tmp_path):
    big = "x" * (MAX_MEMORY_BYTES + 100)
    repo = make_repo(tmp_path, files={"AGENTS.md": big})
    memory = load_project_memory(repo)
    assert memory is not None
    assert memory.event_fields["truncated"] is True
    assert memory.event_fields["bytes"] == MAX_MEMORY_BYTES + 100
    assert len(memory.text.encode("utf-8")) == MAX_MEMORY_BYTES  # 只读前 32KiB


def test_load_memory_utf8_tolerant(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_bytes("正文".encode() + b"\xff\xfe")
    memory = load_project_memory(repo)
    assert memory is not None
    assert "正文" in memory.text  # errors="replace" 不崩溃


def test_load_memory_symlink_escape_ignored_with_warning(tmp_path):
    repo = make_repo(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("仓库外内容", encoding="utf-8")
    os.symlink(outside, repo / "AGENTS.md")
    warnings: list[str] = []
    memory = load_project_memory(repo, warn=warnings.append)
    assert memory is None
    assert len(warnings) == 1 and "仓库外" in warnings[0]


def test_load_memory_symlink_inside_repo_allowed(tmp_path):
    repo = make_repo(tmp_path, files={"docs/notes.md": "约定内容"})
    os.symlink("docs/notes.md", repo / "AGENTS.md")
    memory = load_project_memory(repo)
    assert memory is not None and "约定内容" in memory.text


def test_load_memory_directory_ignored(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "AGENTS.md").mkdir()
    warnings: list[str] = []
    assert load_project_memory(repo, warn=warnings.append) is None
    assert any("不是普通文件" in w for w in warnings)


# ---------------------------------------------------------------------------
# REPL 注入与事件
# ---------------------------------------------------------------------------


def test_repl_injects_memory_into_system_prompt_and_logs_event(tmp_path):
    repo = make_repo(tmp_path, files={
        "AGENTS.md": "项目约定:测试命令 python3 -m pytest -q",
        "README.md": "x",
    })
    session, output = make_session(tmp_path, repo, [{"text": "好的"}])
    assert '<project_instructions path="AGENTS.md">' in session.messages[0]["content"]
    assert "项目约定:测试命令" in session.messages[0]["content"]
    assert "追加到 AGENTS.md" in session.messages[0]["content"]  # 引导句
    assert CHAT_SYSTEM_PROMPT in session.messages[0]["content"]

    loaded = events_of(session, "memory_loaded")
    assert len(loaded) == 1
    assert loaded[0]["path"] == "AGENTS.md" and loaded[0]["truncated"] is False
    assert loaded[0]["bytes"] > 0
    # session_meta.memory_file 与事件一致
    meta = next(json.loads(line) for line in (session.session_dir / "session.jsonl").read_text(encoding="utf-8").splitlines())
    assert meta["memory_file"] == {"path": "AGENTS.md", "bytes": loaded[0]["bytes"], "truncated": False}

    session.turn("你好", quiet_renderer(output))
    session.finish()


def test_repl_without_memory_no_injection_no_event(tmp_path):
    repo = make_repo(tmp_path, files={"README.md": "x"})
    session, _output = make_session(tmp_path, repo, [{"text": "好的"}])
    assert session.messages[0]["content"] == CHAT_SYSTEM_PROMPT  # 原样,不提 AGENTS.md
    assert "AGENTS.md" not in session.messages[0]["content"]
    assert events_of(session, "memory_loaded") == []
    session.finish()


def test_repl_truncated_memory_notifies_user(tmp_path):
    repo = make_repo(tmp_path, files={"AGENTS.md": "y" * (MAX_MEMORY_BYTES + 10)})
    session, output = make_session(tmp_path, repo, [])
    assert "已截断注入" in output.getvalue()
    loaded = events_of(session, "memory_loaded")
    assert loaded[0]["truncated"] is True
    session.finish()


def test_new_session_reloads_memory(tmp_path):
    repo = make_repo(tmp_path, files={"AGENTS.md": "第一版约定"})
    session, _output = make_session(tmp_path, repo, [{"text": "ok"}, {"text": "ok2"}])
    assert "第一版约定" in session.messages[0]["content"]
    (repo / "AGENTS.md").write_text("第二版约定", encoding="utf-8")
    # 会话中途不自动重载
    assert "第一版约定" in session.messages[0]["content"]
    session.new_session()
    assert "第二版约定" in session.messages[0]["content"]  # /new 重载
    # 新段再次记录 memory_loaded
    loaded = events_of(session, "memory_loaded")
    assert len(loaded) == 1  # 当前段一次
    session.finish()


# ---------------------------------------------------------------------------
# batch 完全不受影响
# ---------------------------------------------------------------------------


def test_batch_system_prompt_and_log_untouched_by_agents_md(tmp_path, monkeypatch, capsys):
    """AGENTS.md 存在时,batch 的 system prompt 与日志完全不含记忆注入。"""
    repo = make_repo(tmp_path, files={
        "AGENTS.md": "项目约定:不应出现在批处理",
        "README.md": "x",
    })
    script = {
        "turns": [
            {"tool_calls": [{"name": "finish", "arguments": {"result": "failed", "summary": "结束"}}]}
        ]
    }
    script_path = tmp_path / "batch.json"
    script_path.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(tmp_path)  # mini-agent-runs 落 tmp
    code = main([
        "--repo", str(repo), "--task", "批处理",
        "--model", f"fake:{script_path}",
    ])
    assert code == 1
    runs = json.loads("[]")
    log_path = None
    for candidate in sorted((tmp_path / "mini-agent-runs").iterdir()):
        if (candidate / "session.jsonl").exists():
            log_path = candidate / "session.jsonl"
    assert log_path is not None
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    requests = [e for e in events if e["type"] == "llm_request"]
    system = requests[0]["messages"][0]["content"]
    assert "不应出现在批处理" not in system
    assert "project_instructions" not in system
    assert all(e["type"] != "memory_loaded" for e in events)
    assert runs == []  # (占位;核心断言在上面)
