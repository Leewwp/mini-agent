"""REPL 装配(工单 12 / 设计票 08):模式分发、会话生命周期、turn 事件、终报。

全部 FakeModel/注入驱动,无 TTY 无网络;批处理 222 测试零改动的回归由全量
pytest 保证。通过 run_repl/main 直接驱动非 TTY 路径(纯文本输入、无 spinner)。
"""

import io
import json
import subprocess
from datetime import datetime
from pathlib import Path

from mini_agent.cli import (
    Config,
    build_arg_parser,
    detect_git_root,
    main,
    resolve_config,
    run_repl,
)
from mini_agent.providers import create_provider
from mini_agent.repl.orchestrator import (
    CHAT_SYSTEM_PROMPT,
    ChatSession,
    compose_system_prompt,
    create_repl_session_dir,
    repo_slug,
)
from mini_agent.repl.render import Palette, ReplRenderer, SpinnerHandle


def write_script(tmp_path, payload, name="script.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def make_repo(tmp_path, *, files: dict | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for fname, content in (files or {}).items():
        (repo / fname).write_text(content, encoding="utf-8")
    return repo


def repl_config(repo: Path, tmp_path, script=None, script_name="script.json", **overrides) -> Config:
    write_script(tmp_path, script if script is not None else {"turns": []}, script_name)
    argv = [
        "--repo", str(repo),
        "--model", f"fake:{tmp_path / script_name}",
        "--runs-dir", str(tmp_path / "runs"),
    ]
    for key, value in overrides.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]
    return resolve_config(build_arg_parser().parse_args(argv), env={})


def read_events(session_dir: Path) -> list[dict]:
    path = Path(session_dir) / "session.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def events_of(events, type_: str) -> list:
    return [e for e in events if e.get("type") == type_]


def assert_pairs_valid(messages) -> None:
    pending: set[str] = set()
    for m in messages:
        for tc in m.get("tool_calls") or []:
            pending.add(tc["id"])
        if m.get("role") == "tool":
            assert m["tool_call_id"] in pending
            pending.discard(m["tool_call_id"])
    assert not pending


def quiet_renderer(output):
    return ReplRenderer(output, palette=Palette(enabled=False), spinner=SpinnerHandle(None))


# ---------------------------------------------------------------------------
# chat system prompt 与记忆注入钩子
# ---------------------------------------------------------------------------


def test_chat_prompt_only_three_diffs_from_batch():
    from mini_agent.loop import SYSTEM_PROMPT

    # 三处差异:可反问 / 文字回复即轮末 / finish 仅验证收口
    assert "可以直接向用户提问" in CHAT_SYSTEM_PROMPT
    assert "不要向用户提问" not in CHAT_SYSTEM_PROMPT
    assert "直接用文字回复用户即可结束本轮" in CHAT_SYSTEM_PROMPT
    assert "不强制" in CHAT_SYSTEM_PROMPT and "验证收口" in CHAT_SYSTEM_PROMPT
    # 围栏/复验/失败语义逐字一致(关键句双方都在)
    for fragment in (
        "先读后写",
        "只有 run_tests 的结果计入成功判定",
        "密钥文件(.env、*.pem、id_rsa* 等)不可读写",
        "这是合法结果,优于伪造成功",
    ):
        assert fragment in CHAT_SYSTEM_PROMPT and fragment in SYSTEM_PROMPT


def test_compose_system_prompt_memory_hook():
    base = "BASE_PROMPT"
    assert compose_system_prompt(base, None) == base  # 无记忆:原样,不提 AGENTS.md
    composed = compose_system_prompt(base, "项目约定:用 ruff")
    assert '<project_instructions path="AGENTS.md">' in composed
    assert "项目约定:用 ruff" in composed
    assert "不得覆盖上述安全与流程规则" in composed
    assert "追加到 AGENTS.md" in composed  # 引导句在块尾(条件注入)


# ---------------------------------------------------------------------------
# 会话目录:repo-slug、权限、稳定用户级路径
# ---------------------------------------------------------------------------


def test_repo_slug_has_no_absolute_path(tmp_path):
    repo = tmp_path / "tags-repo"
    slug = repo_slug(repo)
    assert slug.startswith("tags-repo-")
    assert str(tmp_path) not in slug and "/" not in slug
    digest_part = slug.rsplit("-", 1)[1]
    assert len(digest_part) == 6 and all(c in "0123456789abcdef" for c in digest_part)
    # 同名不同路径 → slug 不同
    other = tmp_path / "other" / "tags-repo"
    assert repo_slug(other) != slug


def test_create_repl_session_dir_layout_and_permissions(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    now = datetime(2026, 11, 14, 6, 30, 0)
    session_dir = create_repl_session_dir(repo, runs_dir=tmp_path / "runs", now=now, suffix="ab12")
    root = tmp_path / "runs"
    assert session_dir.parent.parent == root
    assert session_dir.name == "20261114-063000-ab12"
    assert root.stat().st_mode & 0o777 == 0o700
    assert str(session_dir).startswith(str(tmp_path))  # runs_dir 覆盖生效,不落系统 /tmp


# ---------------------------------------------------------------------------
# 模式分发(CLI)
# ---------------------------------------------------------------------------


def test_mode_dispatch_task_and_resume_conflict(tmp_path, capsys):
    repo = make_repo(tmp_path)
    code = main([
        "--repo", str(repo), "--task", "批处理任务",
        "--resume", str(tmp_path / "some-session"),
        "--model", f"fake:{write_script(tmp_path, {'turns': []})}",
    ])
    assert code == 5
    err = capsys.readouterr().err
    assert "--task" in err and "--resume" in err


def test_mode_dispatch_task_runs_batch_unchanged(tmp_path, capsys):
    repo = make_repo(tmp_path, files={"README.md": "x"})
    script = {
        "turns": [
            {"tool_calls": [{"name": "finish", "arguments": {"result": "failed", "summary": "结束"}}]}
        ]
    }
    code = main([
        "--repo", str(repo), "--task", "批处理任务",
        "--model", f"fake:{write_script(tmp_path, script)}",
    ])
    assert code == 1  # TESTS_FAILED:批处理退出码不变
    out = capsys.readouterr().out
    assert "终报" in out and "TESTS_FAILED" in out


def test_repl_requires_key_fail_fast(monkeypatch, tmp_path):
    monkeypatch.delenv("MINI_AGENT_API_KEY", raising=False)
    repo = make_repo(tmp_path)
    code = main(["--repo", str(repo)])  # 无 --task → REPL;真实模型无 key → exit 5
    assert code == 5


def test_bare_command_enters_repl_via_main(tmp_path, monkeypatch, capsys):
    """裸 mini-agent(无 --task)进 REPL:脚本化 stdin + FakeModel 全链路。"""
    repo = make_repo(tmp_path, files={"README.md": "x"})
    script = {"turns": [{"text": "你好,这里是回复。"}]}
    monkeypatch.setattr("sys.stdin", io.StringIO("打个招呼\n/exit\n"))
    code = main([
        "--repo", str(repo),
        "--model", f"fake:{write_script(tmp_path, script)}",
        "--runs-dir", str(tmp_path / "runs"),
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "你好,这里是回复。" in out
    assert "✓ 已回复" in out
    assert "会话终报" in out
    # 会话目录按 repo-slug 分组
    slug_dir = tmp_path / "runs" / repo_slug(repo)
    assert slug_dir.is_dir()
    session_dirs = list(slug_dir.iterdir())
    assert len(session_dirs) == 1
    events = read_events(session_dirs[0])
    kinds = [e["type"] for e in events]
    assert kinds[0] == "session_meta" and events[0]["mode"] == "chat"
    assert "turn_start" in kinds and "turn_result" in kinds and "turn_end" in kinds
    assert "final_report" not in kinds  # REPL 轮末不发批处理终报
    assert (session_dirs[0] / "report.md").exists()


def test_detect_git_root(tmp_path):
    nested = tmp_path / "sub" / "deeper"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    assert detect_git_root(nested) == tmp_path.resolve()
    # 独立非 git 目录 → None(调用方回退 cwd)
    outside_repo = tmp_path.parent / "no-git-here"
    outside_repo.mkdir(exist_ok=True)
    proc = subprocess.run(
        ["git", "-C", str(outside_repo), "--no-optional-locks", "rev-parse", "--show-toplevel"],
        capture_output=True, check=False,
    )
    if proc.returncode != 0:
        assert detect_git_root(outside_repo) is None


# ---------------------------------------------------------------------------
# session_meta / turn 事件 / turn_end 完整性
# ---------------------------------------------------------------------------


def test_session_meta_field_set_and_turn_events(tmp_path):
    repo = make_repo(tmp_path, files={"README.md": "x"})
    script = {
        "turns": [
            {"text": "第一轮"},
            {
                "tool_calls": [
                    {"name": "finish", "arguments": {"result": "failed", "summary": "收工"}}
                ]
            },
        ]
    }
    output = io.StringIO()
    config = repl_config(repo, tmp_path, script)
    provider = create_provider(config.model)
    code = run_repl(
        config, provider,
        stdin=io.StringIO("问题一\n继续收尾\n/exit\n"),
        stdout=output,
    )
    assert code == 0
    session_dir = next((tmp_path / "runs" / repo_slug(repo)).iterdir())
    events = read_events(session_dir)
    meta = events[0]
    for field in (
        "mode", "repo", "model", "base_url", "temperature", "max_tokens",
        "thinking", "tool_desc_lang", "max_steps", "test_cmd", "timeout_seconds",
        "memory_file", "resumed_from",
    ):
        assert field in meta, f"session_meta 缺字段 {field}"
    assert meta["mode"] == "chat" and meta["memory_file"] is None
    assert meta["resumed_from"] is None

    turn_starts = events_of(events, "turn_start")
    turn_results = events_of(events, "turn_result")
    turn_ends = events_of(events, "turn_end")
    assert [t["turn"] for t in turn_starts] == [1, 2]
    assert [t["state"] for t in turn_results] == ["REPLIED", "TESTS_FAILED"]
    assert turn_results[1]["steps"] == 1 and turn_results[1]["model"].startswith("fake:")
    # turn_end.messages = 本轮结束后完整模型视图
    assert turn_ends[0]["messages"][-1]["role"] == "assistant"
    assert turn_ends[1]["messages"][-1]["role"] == "tool"
    for end in turn_ends:
        assert_pairs_valid(end["messages"])
        assert end["messages"][0]["role"] == "system"
    # 顺序:turn_start 在 llm_request 前,turn_result/turn_end 在其后
    kinds = [e["type"] for e in events]
    assert kinds.index("turn_start") < kinds.index("llm_request")
    assert kinds.index("llm_response") < kinds.index("turn_result") < kinds.index("turn_end")


def test_turn_end_includes_interrupt_repair(tmp_path):
    """中断轮的 turn_end.messages 是修复后的历史(每个 tool_call id 有结果)。"""
    repo = make_repo(tmp_path, files={"README.md": "x"})
    script = {
        "turns": [
            {
                "tool_calls": [
                    {"name": "boomer", "arguments": {}},
                    {"name": "boomer", "arguments": {}},
                ]
            },
            {"text": "继续"},
        ]
    }
    output = io.StringIO()
    config = repl_config(repo, tmp_path, script)
    provider = create_provider(config.model)

    class Boomer:
        calls = 0

        @property
        def name(self):
            return "boomer"

        @property
        def description(self):
            return "测试用:第二次调用抛 KeyboardInterrupt"

        @property
        def parameters(self):
            return {"type": "object", "properties": {}}

        def execute(self, args):
            Boomer.calls += 1
            if Boomer.calls >= 2:
                raise KeyboardInterrupt
            return "第一次执行成功"

    session = ChatSession(
        config=config, provider=provider, output=output, runs_dir=tmp_path / "runs2",
    )
    session._registry.register(Boomer())
    outcome = session.turn("跑两个工具", quiet_renderer(output))
    assert outcome.state.value == "INTERRUPTED"
    events = read_events(session.session_dir)
    turn_end = events_of(events, "turn_end")[0]
    assert_pairs_valid(turn_end["messages"])  # 中断修复已入 turn_end
    session.finish()


# ---------------------------------------------------------------------------
# /new 轮换与 /exit 报告
# ---------------------------------------------------------------------------


def test_new_session_rotates_logger_snapshot_and_resets_usage(tmp_path):
    repo = make_repo(tmp_path, files={"README.md": "x"})
    script = {"turns": [{"text": "回复A"}, {"text": "回复B"}, {"text": "回复C"}]}
    output = io.StringIO()
    config = repl_config(repo, tmp_path, script)
    provider = create_provider(config.model)
    session = ChatSession(
        config=config, provider=provider, output=output, runs_dir=tmp_path / "runs",
    )
    session.turn("第一问", quiet_renderer(output))
    session.turn("第二问", quiet_renderer(output))
    first_dir = session.session_dir
    assert session.usage.turns == 2

    session.new_session()
    assert session.session_dir != first_dir
    assert session.usage.turns == 0  # usage 重置
    assert len(session.messages) == 1 and session.messages[0]["role"] == "system"

    session.turn("新会话第一问", quiet_renderer(output))
    assert session.usage.turns == 1
    session.finish()

    # 旧段只含前两轮且不写终报;新段只含第三轮并持有 report.md
    first_events = read_events(first_dir)
    assert len(events_of(first_events, "turn_start")) == 2
    assert not (first_dir / "report.md").exists()
    second_events = read_events(session.session_dir)
    assert len(events_of(second_events, "turn_start")) == 1
    report = (session.session_dir / "report.md").read_text(encoding="utf-8")
    assert "轮次:1" in report  # 当前段 usage


def test_new_session_records_changes_across_segments(tmp_path):
    """/new 分段的改动累积:report 修改清单合并两段(写入文件跨段保留)。"""
    repo = make_repo(tmp_path, files={"README.md": "x"})
    script = {
        "turns": [
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "a.txt", "content": "A"}}]},
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "b.txt", "content": "B"}}]},
        ]
    }
    output = io.StringIO()
    config = repl_config(repo, tmp_path, script)
    provider = create_provider(config.model)
    session = ChatSession(
        config=config, provider=provider, output=output, runs_dir=tmp_path / "runs",
    )
    renderer = quiet_renderer(output)
    session.turn("写 a", renderer)  # 写入 a.txt 后未 finish → MAX_STEPS?不会:单 tool_call 轮后继续
    session.new_session()
    session.turn("写 b", renderer)
    session.finish()
    report = (session.session_dir / "report.md").read_text(encoding="utf-8")
    assert "a.txt" in report and "b.txt" in report
    assert "段(/new 轮换)" in report


def test_exit_report_even_when_last_turn_failed(tmp_path):
    repo = make_repo(tmp_path, files={"README.md": "x"})
    script = {
        "turns": [
            {"text": "第一轮"},
            {
                "tool_calls": [
                    {"name": "finish", "arguments": {"result": "failed", "summary": "没修好"}}
                ]
            },
        ]
    }
    output = io.StringIO()
    config = repl_config(repo, tmp_path, script)
    provider = create_provider(config.model)
    code = run_repl(
        config, provider,
        stdin=io.StringIO("开始\n继续\n/exit\n"),
        stdout=output,
    )
    assert code == 0  # 最后轮次失败不影响 /exit 退出码
    text = output.getvalue()
    assert "会话终报" in text and "正常退出" in text
    assert "轮次 2" in text
    report = next((tmp_path / "runs" / repo_slug(repo)).iterdir()) / "report.md"
    content = report.read_text(encoding="utf-8")
    assert "轮次:2" in content and "会话状态:正常退出" in content


def test_empty_session_exit_still_reports(tmp_path):
    repo = make_repo(tmp_path)
    output = io.StringIO()
    config = repl_config(repo, tmp_path)
    provider = create_provider(config.model)
    code = run_repl(config, provider, stdin=io.StringIO("/exit\n"), stdout=output)
    assert code == 0
    assert "轮次 0" in output.getvalue()


def test_uncaught_exception_exits_3_with_error_report(tmp_path):
    repo = make_repo(tmp_path, files={"README.md": "x"})

    class ExplodingProvider:
        @property
        def model(self):
            return "fake-exploding"

        def complete(self, messages, *, tools=None, on_event=None):
            raise RuntimeError("意外爆炸")

    output = io.StringIO()
    config = repl_config(repo, tmp_path)
    code = run_repl(
        config, ExplodingProvider(),
        stdin=io.StringIO("你好\n"), stdout=output,
    )
    assert code == 3
    assert "异常退出" in output.getvalue()
    report = next((tmp_path / "runs" / repo_slug(repo)).iterdir()) / "report.md"
    assert "异常退出" in report.read_text(encoding="utf-8")


def test_session_usage_accumulates_and_compact_column(tmp_path):
    repo = make_repo(tmp_path, files={"README.md": "x"})
    script = {"turns": [{"text": "回复一"}, {"text": "回复二"}]}
    output = io.StringIO()
    config = repl_config(repo, tmp_path, script)
    provider = create_provider(config.model)
    code = run_repl(
        config, provider,
        stdin=io.StringIO("问一\n问二\n/exit\n"), stdout=output,
    )
    assert code == 0
    report = next((tmp_path / "runs" / repo_slug(repo)).iterdir()) / "report.md"
    content = report.read_text(encoding="utf-8")
    assert "轮次:2" in content
    assert "total " in content
    # compact_tokens 占位口径:未压缩时不出现单列(13 接入后压缩会话出现)
    assert "其中压缩摘要" not in content


def test_session_token_line_with_compact_column():
    from mini_agent.report import SessionSummary, _session_token_text

    summary = SessionSummary(
        state="正常退出", turns=3, steps=9, prompt_tokens=100, completion_tokens=50,
        total_tokens=150, compact_tokens=30, model="fake",
    )
    text = _session_token_text(summary)
    assert "total 150" in text and "其中压缩摘要 30 tok" in text
