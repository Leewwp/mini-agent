"""影子 git 快照(工单 04):目标仓库零写入的改动凭证。

全部走本地临时目录 + 真实 git 子进程,零网络。公共接缝:
- ``ShadowGitSnapshot.start(repo)`` 建影子库并记基线 commit;
- ``snapshot.finish() -> ChangeSet`` 结束 commit 并 diff 两次快照;
- 降级(git 不可用/超时)→ ``ChangeSet.available=False`` + 文件清单。
"""

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
