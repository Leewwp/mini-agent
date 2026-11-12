"""内置 fixture 的结构测试(工单 05):FakeModel 驱动,无网络。

两类断言:
1. **fixture 纯净性**:tags-repo 副本初始必须恰好 3/6 失败、algo-repo 副本
   全部失败——fixture 的「未解决」状态是演示与彩排的前提,谁不小心修好了
   fixture 里的代码,这里的测试立刻红;
2. **脚本化修复闭环**:examples/fix-cycle.json(FakeModel 脚本)在 tags-repo
   真实副本上走通 list→read→write→run_tests→finish 的完整生产路径,
   断言退出码 0、副本被修复、而仓库内 fixture 本体保持未解决状态。
"""

import shutil
import subprocess
import sys
from pathlib import Path

from test_cli_smoke import run_cli

REPO_ROOT = Path(__file__).resolve().parent.parent
TAGS_FIXTURE = REPO_ROOT / "fixtures" / "tags-repo"
ALGO_FIXTURE = REPO_ROOT / "fixtures" / "algo-repo"


def copy_fixture(fixture: Path, dest: Path) -> Path:
    """fixture 一律 cp 到临时目录,本体保持洁净(彩排纪律同此)。"""
    copied = dest / fixture.name
    shutil.copytree(fixture, copied)
    return copied


def pytest_in(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """用 venv 解释器在 repo 内跑 pytest(不依赖 PATH 里的 pytest)。"""
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *args],
        cwd=repo, capture_output=True, text=True, timeout=120, check=False,
    )


# ---------- fixture 纯净性 ----------


def test_tags_fixture_initially_has_exactly_three_failing_tests(tmp_path):
    proc = pytest_in(copy_fixture(TAGS_FIXTURE, tmp_path))
    assert proc.returncode != 0
    assert "3 failed, 3 passed" in proc.stdout
    for name in ("test_strips_whitespace", "test_drops_empty_tags", "test_all_empty"):
        assert f"FAILED tests/test_normalize_tags.py::{name}" in proc.stdout


def test_algo_fixture_all_tests_fail_before_implementation(tmp_path):
    proc = pytest_in(copy_fixture(ALGO_FIXTURE, tmp_path))
    assert proc.returncode != 0
    assert "11 failed" in proc.stdout
    assert "NotImplementedError" in proc.stdout


# ---------- 脚本化修复闭环(真实 fixture + FakeModel 脚本 + 真实子进程) ----------


def test_fix_cycle_script_repairs_tags_repo_end_to_end(tmp_path):
    repo = copy_fixture(TAGS_FIXTURE, tmp_path)
    scratch = tmp_path / "cwd"
    scratch.mkdir()

    proc = run_cli(
        [
            "--repo", str(repo),
            "--task", "修复 normalize_tags 对空白和空标签的处理,运行测试验证",
            "--model", f"fake:{REPO_ROOT / 'examples' / 'fix-cycle.json'}",
            "--test-cmd", f"{sys.executable} -m pytest -q",
        ],
        cwd=scratch,
    )

    assert proc.returncode == 0, f"stdout:{proc.stdout[-500:]!r} stderr:{proc.stderr[-500:]!r}"
    assert "终态:SUCCESS" in proc.stdout and "退出码 0" in proc.stdout
    # 修复真实写入了副本:strip + 过滤空标签的规范实现落地
    fixed = (repo / "tags_utils" / "normalize.py").read_text(encoding="utf-8")
    assert "[tag.strip() for tag in raw.split(\",\") if tag.strip()]" in fixed
    # 修复后的副本测试全绿
    assert pytest_in(repo).returncode == 0
    # fixture 本体未被污染(先写后读规则外的另一重保险:彩排纪律)
    assert "return raw.split(\",\")" in (
        TAGS_FIXTURE / "tags_utils" / "normalize.py"
    ).read_text(encoding="utf-8")
