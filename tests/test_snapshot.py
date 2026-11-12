"""影子 git 快照(工单 04):目标仓库零写入的改动凭证。

全部走本地临时目录 + 真实 git 子进程,零网络。公共接缝:
- ``ShadowGitSnapshot.start(repo)`` 建影子库并记基线 commit;
- ``snapshot.finish() -> ChangeSet`` 结束 commit 并 diff 两次快照;
- 降级(git 不可用/超时)→ ``ChangeSet.available=False`` + 文件清单。
"""

import subprocess
from pathlib import Path

from mini_agent.sandbox import ChangeSet, ShadowGitSnapshot


def git_status(repo: Path) -> str:
    """目标仓库自身的 git status(排序后),用于零写入断言。"""
    import subprocess

    proc = subprocess.run(
        ["git", "-C", str(repo), "--no-optional-locks", "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    return "".join(sorted(proc.stdout.splitlines(keepends=True)))


# ---------- 切片 A:diff 只反映本次 agent 的改动 ----------


def test_snapshot_diff_reflects_agent_changes_with_untracked(tmp_path):
    """非 git 目录同样适用:基线 → 改文件 + 新建 untracked 文件 → diff 如实呈现。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

    snapshot = ShadowGitSnapshot.start(repo)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "new.txt").write_text("hello\n", encoding="utf-8")
    changes = snapshot.finish()

    assert isinstance(changes, ChangeSet)
    assert changes.available
    assert [(c.path, c.status) for c in changes.changes] == [
        ("calc.py", "M"),
        ("new.txt", "A"),
    ]
    assert changes.diff_text and "+    return a + b" in changes.diff_text
    assert "+hello" in changes.diff_text


# ---------- 切片 B:零写入 + spike 四种情况 ----------


def make_git_repo(path: Path) -> Path:
    """带一个已提交文件的干净 git 仓库。"""
    path.mkdir(parents=True)
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    run = lambda *a: subprocess.run(
        ["git", "-C", str(path), *a], capture_output=True, text=True, check=True
    )
    run("init", "-q")
    run("add", "-A")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed")
    return path


def test_snapshot_never_writes_to_target_git_repo(tmp_path):
    """验收核心:快照全程(含基线脏状态探测)后,目标仓库 HEAD/index/工作区零写入。"""
    repo = make_git_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")  # 未提交改动
    (repo / "untracked.txt").write_text("old\n", encoding="utf-8")  # 原有 untracked
    import hashlib

    def git_state() -> str:
        parts = [git_status(repo)]
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout
        parts.append(head)
        blob = hashlib.sha256()
        for f in sorted((repo / ".git").rglob("*")):
            if f.is_file():
                blob.update(f.relative_to(repo).as_posix().encode())
                blob.update(f.read_bytes())
        parts.append(blob.hexdigest())
        return "\n".join(parts)

    before = git_state()
    snapshot = ShadowGitSnapshot.start(repo)
    changes = snapshot.finish()  # 不做任何改动:纯快照操作本身必须零写入
    after = git_state()

    assert before == after
    assert changes.available and changes.changes == () and changes.diff_text == ""


def test_snapshot_baseline_folds_preexisting_dirty_so_diff_is_agent_only(tmp_path):
    """基线含原有未提交改动与 untracked:diff 只反映本次 agent 的改动,并注明基线状态。"""
    repo = make_git_repo(tmp_path / "repo")
    (repo / "seed.txt").write_text("locally modified\n", encoding="utf-8")  # 原有未提交改动
    (repo / "old_untracked.txt").write_text("old\n", encoding="utf-8")  # 原有 untracked

    snapshot = ShadowGitSnapshot.start(repo)
    (repo / "calc.py").write_text("print('fixed')\n", encoding="utf-8")  # agent 唯一改动
    changes = snapshot.finish()

    assert [(c.path, c.status) for c in changes.changes] == [("calc.py", "A")]
    assert changes.baseline_dirty is True
    assert changes.baseline_untracked == 1
    assert any("未提交" in note for note in changes.notes)


def test_snapshot_excludes_mini_agent_runs_dir(tmp_path):
    """仓库内存在 mini-agent-runs/:快照排除它(runs 目录内的增改不入 diff)。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "code.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "mini-agent-runs" / "20261112-ab").mkdir(parents=True)
    (repo / "mini-agent-runs" / "20261112-ab" / "session.jsonl").write_text("{}\n", encoding="utf-8")

    snapshot = ShadowGitSnapshot.start(repo)
    (repo / "code.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "mini-agent-runs" / "20261112-ab" / "session.jsonl").write_text("more\n", encoding="utf-8")
    (repo / "mini-agent-runs" / "new-session").mkdir()
    (repo / "mini-agent-runs" / "new-session" / "report.md").write_text("r\n", encoding="utf-8")
    changes = snapshot.finish()

    assert [(c.path, c.status) for c in changes.changes] == [("code.py", "M")]


def test_snapshot_respects_target_gitignore(tmp_path):
    """目标目录有 .gitignore:遵循其忽略规则(git 原生语义,被忽略的新文件不入 diff)。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "code.py").write_text("x = 1\n", encoding="utf-8")
    (repo / ".gitignore").write_text("__pycache__/\n*.log\n", encoding="utf-8")

    snapshot = ShadowGitSnapshot.start(repo)
    (repo / "code.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "debug.log").write_text("noise\n", encoding="utf-8")  # 被忽略
    changes = snapshot.finish()

    assert [(c.path, c.status) for c in changes.changes] == [("code.py", "M")]
    assert any(".gitignore" in note for note in changes.notes)
