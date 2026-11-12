"""CLI 终报接通(工单 04):快照挂进主路径,所有终态打印五要素终报。

全部 FakeModel/本地临时 git 仓库,零网络。断言外部可观察行为:
report.md/diffs/ 产物、终端终报内容、目标仓库零写入、降级与中断路径。
"""

import hashlib
import json
import subprocess


from mini_agent.cli import main, resolve_config, run
from mini_agent.providers import FakeProvider, FakeTurn
from mini_agent.sandbox import ChangeSet

BROKEN = "def add(a, b):\n    return a - b  # bug\n"
FIXED = "def add(a, b):\n    return a + b\n"


def git_repo_with(path, files: dict) -> None:
    """已提交 files 的干净 git 仓库(测试自带身份,不依赖全局配置)。"""
    path.mkdir(parents=True)

    def g(*args):
        subprocess.run(
            ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
        )

    g("init", "-q")
    for rel, text in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    g("add", "-A")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed")


def repo_git_fingerprint(repo) -> str:
    """HEAD + .git 全量内容校验和(零写入断言用)。"""
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout
    blob = hashlib.sha256()
    for f in sorted((repo / ".git").rglob("*")):
        if f.is_file():
            blob.update(f.relative_to(repo).as_posix().encode())
            blob.update(f.read_bytes())
    return head + blob.hexdigest()


def porcelain(repo) -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), "--no-optional-locks", "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    )
    return sorted(proc.stdout.splitlines())


def fake_main(monkeypatch, tmp_path, turns, repo, extra_args=()):
    script = tmp_path / "script.json"
    payload = {"turns": [t if isinstance(t, dict) else t for t in turns]}
    script.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    argv = [
        "--repo", str(repo), "--task", "修复 add",
        "--model", f"fake:{script}", *extra_args,
    ]
    monkeypatch.chdir(tmp_path)
    return main(argv)


def find_session_dir(tmp_path):
    runs = tmp_path / "mini-agent-runs"
    (session_dir,) = [d for d in runs.iterdir() if d.is_dir()]
    return session_dir


def events_of(session_dir):
    return [
        json.loads(line)
        for line in (session_dir / "session.jsonl").read_text(encoding="utf-8").splitlines()
    ]


# ---------- 成功路径:五要素落盘 + 终端打印 + 零写入 ----------


def test_cli_success_run_writes_report_and_diff_with_zero_write(monkeypatch, tmp_path, capsys):
    repo = tmp_path / "repo"
    git_repo_with(
        repo,
        {
            "calc.py": BROKEN,
            "test_calc.py": "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
            ".gitignore": "__pycache__/\n.pytest_cache/\n",
        },
    )
    before_fp = repo_git_fingerprint(repo)
    before_status = porcelain(repo)

    turns = [
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "calc.py"}}]},
        {"tool_calls": [{"name": "write_file", "arguments": {"path": "calc.py", "content": FIXED}}]},
        {"tool_calls": [{"name": "run_tests", "arguments": {}}]},
        {"tool_calls": [{"name": "finish", "arguments": {"result": "success", "summary": "修复 add"}}]},
    ]
    code = fake_main(
        monkeypatch, tmp_path, turns, repo,
        extra_args=["--test-cmd", "python3 -m pytest -q test_calc.py"],
    )
    assert code == 0

    # 目标仓库零写入:HEAD 与 .git 内容校验和不变;工作区差异恰好是 agent 的改动
    after_fp = repo_git_fingerprint(repo)
    assert after_fp == before_fp
    assert set(porcelain(repo)) - set(before_status) == {" M calc.py"}

    session_dir = find_session_dir(tmp_path)
    report = (session_dir / "report.md").read_text(encoding="utf-8")
    # 五要素
    assert "终态:SUCCESS(原因:verification_passed)" in report
    assert "calc.py" in report and "修改" in report
    assert "python3 -m pytest -q test_calc.py" in report and "退出码 0(通过)" in report
    assert "步数:4" in report
    assert str((session_dir / "session.jsonl").resolve()) in report
    assert "deepseek" not in report  # 模型名如实记录(fake 注入)
    assert "fake:" in report
    # diff 产物
    diff = (session_dir / "diffs" / "changes.diff").read_text(encoding="utf-8")
    assert "+    return a + b" in diff and "-    return a - b  # bug" in diff
    # 终端终报(所有终态共用)
    out = capsys.readouterr().out
    assert "终态:SUCCESS(原因:verification_passed),退出码 0" in out
    assert "calc.py(修改" in out
    assert str((session_dir / "report.md").resolve()) in out
    # final_report 事件在 session.jsonl
    final = [e for e in events_of(session_dir) if e["type"] == "final_report"]
    assert final and final[-1]["state"] == "SUCCESS"


def test_cli_report_alerts_when_test_file_modified(monkeypatch, tmp_path, capsys):
    repo = tmp_path / "repo"
    new_test = (
        "from calc import add\n\n"
        "# 补充断言\n"
        "def test_add():\n    assert add(1, 2) == 3\n"
    )
    git_repo_with(
        repo,
        {
            "calc.py": FIXED,
            "test_calc.py": "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
            ".gitignore": "__pycache__/\n.pytest_cache/\n",
        },
    )
    turns = [
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "test_calc.py"}}]},
        {"tool_calls": [{"name": "write_file", "arguments": {"path": "test_calc.py", "content": new_test}}]},
        {"tool_calls": [{"name": "run_tests", "arguments": {}}]},
        {"tool_calls": [{"name": "finish", "arguments": {"result": "success", "summary": "补测试"}}]},
    ]
    code = fake_main(
        monkeypatch, tmp_path, turns, repo,
        extra_args=["--test-cmd", "python3 -m pytest -q test_calc.py"],
    )
    assert code == 0
    session_dir = find_session_dir(tmp_path)
    report = (session_dir / "report.md").read_text(encoding="utf-8")
    assert "测试文件" in report and "test_calc.py" in report
    out = capsys.readouterr().out
    assert "⚠ 测试文件 test_calc.py" in out


def test_cli_error_state_still_prints_full_report(monkeypatch, tmp_path, capsys):
    """ERROR 终态(截断)同样打印五要素并落盘 report.md——不只成功路径。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    turns = [{"text": "半截回复", "finish_reason": "length"}]
    code = fake_main(monkeypatch, tmp_path, turns, repo)
    assert code == 3

    out = capsys.readouterr().out
    assert "终态:ERROR(原因:response_truncated),退出码 3" in out
    assert "修改文件(0)" in out
    assert "会话日志:" in out and "终报全文:" in out
    session_dir = find_session_dir(tmp_path)
    report = (session_dir / "report.md").read_text(encoding="utf-8")
    assert "response_truncated" in report
    assert not (session_dir / "diffs" / "changes.diff").exists()  # 无改动 → 无 diff 文件


def test_cli_degraded_snapshot_reports_no_diff_voucher(monkeypatch, tmp_path, capsys):
    """git 不可用 → 报告注明无 diff 凭证,降级清单仍列出改动。"""

    class DegradedSnapshot:
        @classmethod
        def start(cls, repo, **kwargs):
            return cls()

        def finish(self) -> ChangeSet:
            return ChangeSet(
                available=False,
                changes=(),
                degraded_reason="git_unavailable",
                notes=("无 diff 凭证:git 不可用或快照超时,已降级为文件清单对比",),
            )

    import mini_agent.cli as cli_module

    monkeypatch.setattr(cli_module, "ShadowGitSnapshot", DegradedSnapshot)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    turns = [{"tool_calls": [{"name": "finish", "arguments": {"result": "failed", "summary": "放弃"}}]}]
    code = fake_main(
        monkeypatch, tmp_path, turns, repo, extra_args=["--test-cmd", "python3 -c pass"]
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "无 diff 凭证(原因:git_unavailable)" in out
    session_dir = find_session_dir(tmp_path)
    report = (session_dir / "report.md").read_text(encoding="utf-8")
    assert "无 diff 凭证" in report


# ---------- 中断路径(所有终态含中断都出报告) ----------


class InterruptingProvider:
    """第 1 轮正常回复(写文件),之后模拟用户 Ctrl+C。"""

    model = "fake-interrupt"

    def __init__(self) -> None:
        self._inner = FakeProvider(
            [
                FakeTurn(
                    tool_calls=[
                        {"name": "read_file", "arguments": {"path": "calc.py"}},
                        {
                            "name": "write_file",
                            "arguments": {"path": "calc.py", "content": FIXED},
                        },
                    ]
                )
            ]
        )

    def complete(self, messages, *, tools=None, on_event=None):
        if self._inner._turns:
            return self._inner.complete(messages, tools=tools, on_event=on_event)
        raise KeyboardInterrupt


def test_cli_interrupted_run_prints_report_and_keeps_changes(monkeypatch, tmp_path, capsys):
    repo = tmp_path / "repo"
    git_repo_with(repo, {"calc.py": BROKEN})
    monkeypatch.chdir(tmp_path)

    import argparse

    config = resolve_config(
        argparse.Namespace(
            repo=str(repo), task="修复", model="fake:whatever",
            max_steps=20, test_cmd=None, timeout=600,
            max_context_tokens=60000, thinking=False,
        ),
        env={},
    )
    code = run(config, InterruptingProvider())

    assert code == 3  # ERROR/interrupted
    out = capsys.readouterr().out
    assert "终态:ERROR(原因:interrupted),退出码 3" in out
    assert "终报全文:" in out
    assert (repo / "calc.py").read_text(encoding="utf-8") == FIXED  # 改动保留,不回滚
    session_dir = find_session_dir(tmp_path)
    report = (session_dir / "report.md").read_text(encoding="utf-8")
    assert "interrupted" in report and "calc.py" in report
    final = [e for e in events_of(session_dir) if e["type"] == "final_report"]
    assert final and final[-1]["reason"] == "interrupted"
