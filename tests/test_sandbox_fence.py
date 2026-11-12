"""路径围栏的外部行为测试:仓库内放行、../ 逃逸、仓库外绝对路径、符号链接穿透。"""

import pytest

from mini_agent.sandbox import FenceError, PathFence


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "README.md").write_text("# repo\n", encoding="utf-8")
    return root


def make_fence(repo) -> PathFence:
    return PathFence(repo)


def test_relative_path_inside_repo_resolves(repo):
    resolved = make_fence(repo).resolve_path("src/app.py")
    assert resolved == (repo / "src" / "app.py").resolve()


def test_new_nested_path_inside_repo_is_allowed(repo):
    # 新建文件的父目录尚不存在,仍须通过围栏校验
    resolved = make_fence(repo).resolve_path("new/dir/notes.txt")
    assert resolved == (repo / "new" / "dir" / "notes.txt").resolve()


def test_dotdot_escape_is_rejected(repo, tmp_path):
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    with pytest.raises(FenceError, match="仓库外"):
        make_fence(repo).resolve_path("../outside.txt")


def test_absolute_path_outside_repo_is_rejected(repo, tmp_path):
    outside = tmp_path / "elsewhere" / "f.txt"
    with pytest.raises(FenceError):
        make_fence(repo).resolve_path(str(outside))


def test_absolute_path_inside_repo_is_allowed(repo):
    absolute = str(repo / "src" / "app.py")
    resolved = make_fence(repo).resolve_path(absolute)
    assert resolved == (repo / "src" / "app.py").resolve()


def test_inner_dotdot_staying_inside_is_normalized(repo):
    resolved = make_fence(repo).resolve_path("src/../README.md")
    assert resolved == (repo / "README.md").resolve()


def test_symlinked_directory_pointing_outside_is_rejected(repo, tmp_path):
    secret_dir = tmp_path / "secret-dir"
    secret_dir.mkdir()
    (secret_dir / "f.txt").write_text("s", encoding="utf-8")
    (repo / "link").symlink_to(secret_dir)
    with pytest.raises(FenceError, match="符号链接"):
        make_fence(repo).resolve_path("link/f.txt")


def test_symlinked_file_pointing_outside_is_rejected(repo, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("s", encoding="utf-8")
    (repo / "link.txt").symlink_to(secret)
    with pytest.raises(FenceError):
        make_fence(repo).resolve_path("link.txt")


def test_symlink_pointing_inside_repo_is_allowed(repo):
    (repo / "real").mkdir()
    (repo / "real" / "f.txt").write_text("ok", encoding="utf-8")
    (repo / "data").symlink_to(repo / "real")
    resolved = make_fence(repo).resolve_path("data/f.txt")
    assert resolved == (repo / "real" / "f.txt").resolve()


def test_reject_message_is_chinese_and_mentions_repo(repo, tmp_path):
    (tmp_path / "outside.txt").write_text("x", encoding="utf-8")
    with pytest.raises(FenceError, match="仓库目录"):
        make_fence(repo).resolve_path("../outside.txt")
