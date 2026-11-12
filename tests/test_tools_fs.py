"""文件四件套工具的外部行为测试:list_files / read_file / write_file / search。

覆盖工单 02 验收:树状忽略目录、深度条数上限、offset/limit、2000 行/100KB
拒读、拒二进制、密钥 deny-list(可配置,先读后写联动)、新建/覆盖二分、
父目录自动创建仍走围栏、search ≤50 条带行号。
"""

import pytest

from mini_agent.sandbox import FenceError
from mini_agent.tools.fs import FsContext, make_fs_tools


@pytest.fixture()
def ctx(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    return FsContext.create(repo)


@pytest.fixture()
def tools(ctx):
    return {tool.name: tool for tool in make_fs_tools(ctx)}


def run_tool(tools, name, **args):
    return tools[name].execute(args)


# ---------- list_files ----------


def test_list_files_renders_tree(ctx, tools):
    out = run_tool(tools, "list_files")
    assert "README.md" in out and "src/" in out and "app.py" in out
    assert "├──" in out or "└──" in out  # 树状连接符


def test_list_files_ignores_special_directories(ctx, tools):
    for name in (".git", ".venv", "node_modules", "__pycache__", "mini-agent-runs"):
        d = ctx.repo / name
        d.mkdir()
        (d / "should_not_appear.txt").write_text("x", encoding="utf-8")
    out = run_tool(tools, "list_files")
    assert "should_not_appear.txt" not in out


def test_list_files_caps_entry_count(ctx, tools):
    many = ctx.repo / "many"
    many.mkdir()
    for i in range(600):
        (many / f"f{i:03d}.txt").write_text("x", encoding="utf-8")
    out = run_tool(tools, "list_files", path="many")
    listed = [line for line in out.splitlines() if ".txt" in line]
    assert len(listed) == 500
    assert "条数上限" in out


def test_list_files_caps_depth(ctx, tools):
    deep = ctx.repo
    for i in range(1, 13):
        deep = deep / f"d{i}"
    deep.mkdir(parents=True)
    (ctx.repo / "d1" / "d2" / "d3" / "d4" / "d5" / "d6" / "d7" / "visible.txt").write_text(
        "v", encoding="utf-8"
    )
    (ctx.repo / "d1" / "d2" / "d3" / "d4" / "d5" / "d6" / "d7" / "d8" / "deep.txt").write_text(
        "d", encoding="utf-8"
    )
    out = run_tool(tools, "list_files")
    assert "visible.txt" in out  # 第 8 层的条目可见
    assert "d8/" in out  # 第 8 层目录本身列出
    assert "deep.txt" not in out  # 第 9 层不再展开


def test_list_files_subpath(ctx, tools):
    out = run_tool(tools, "list_files", path="src")
    assert "app.py" in out and "README.md" not in out


def test_list_files_missing_subpath_is_error(ctx, tools):
    from mini_agent.tools.base import ToolError

    with pytest.raises(ToolError, match="不存在"):
        run_tool(tools, "list_files", path="nope")


# ---------- read_file ----------


def test_read_file_returns_exact_content(ctx, tools):
    content = "a\nb\r\nc\n"
    (ctx.repo / "lines.txt").write_bytes(content.encode("utf-8"))
    assert run_tool(tools, "read_file", path="lines.txt") == content


def test_read_file_offset_and_limit(ctx, tools):
    (ctx.repo / "n.txt").write_text(
        "\n".join(f"line{i}" for i in range(1, 11)) + "\n", encoding="utf-8"
    )
    out = run_tool(tools, "read_file", path="n.txt", offset=3, limit=2)
    assert out == "line3\nline4\n"


def test_read_file_defaults_read_whole_file(ctx, tools):
    (ctx.repo / "small.txt").write_text("one\ntwo\n", encoding="utf-8")
    assert run_tool(tools, "read_file", path="small.txt") == "one\ntwo\n"


def test_read_file_validates_offset_and_limit(ctx, tools):
    from mini_agent.tools.base import ToolError

    (ctx.repo / "small.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(ToolError, match="offset"):
        run_tool(tools, "read_file", path="small.txt", offset=0)
    with pytest.raises(ToolError, match="limit"):
        run_tool(tools, "read_file", path="small.txt", limit=0)


def test_read_file_refuses_slice_over_2000_lines(ctx, tools):
    (ctx.repo / "big.txt").write_text(
        "\n".join(f"l{i}" for i in range(2001)) + "\n", encoding="utf-8"
    )
    from mini_agent.tools.base import ToolError

    with pytest.raises(ToolError, match="2000 行"):
        run_tool(tools, "read_file", path="big.txt")
    # 分段读取是逃生通道
    out = run_tool(tools, "read_file", path="big.txt", offset=1, limit=10)
    assert out.startswith("l0\n")


def test_read_file_refuses_slice_over_100kb(ctx, tools):
    # 101 行 × 1100 字节 ≈ 111KB,行数远低于 2000,触发字节上限
    (ctx.repo / "wide.txt").write_text(
        "\n".join("a" * 1100 for _ in range(101)) + "\n", encoding="utf-8"
    )
    from mini_agent.tools.base import ToolError

    with pytest.raises(ToolError, match="100KB"):
        run_tool(tools, "read_file", path="wide.txt")
    # 用 limit 缩小后可读
    out = run_tool(tools, "read_file", path="wide.txt", limit=10)
    assert out.count("\n") == 10


def test_read_file_refuses_binary(ctx, tools):
    (ctx.repo / "blob.bin").write_bytes(b"PK\x03\x04\x00\x05")
    from mini_agent.tools.base import ToolError

    with pytest.raises(ToolError, match="二进制"):
        run_tool(tools, "read_file", path="blob.bin")


def test_read_file_missing_file_is_error(ctx, tools):
    from mini_agent.tools.base import ToolError

    with pytest.raises(ToolError, match="不存在"):
        run_tool(tools, "read_file", path="ghost.txt")


# ---------- 密钥 deny-list ----------


def test_read_file_denies_env_exact_but_allows_example(ctx, tools):
    from mini_agent.tools.base import ToolError

    (ctx.repo / ".env").write_text("API_KEY=x\n", encoding="utf-8")
    (ctx.repo / ".env.example").write_text("API_KEY=\n", encoding="utf-8")
    with pytest.raises(ToolError, match="密钥"):
        run_tool(tools, "read_file", path=".env")
    assert "API_KEY=" in run_tool(tools, "read_file", path=".env.example")


def test_read_file_denies_pem_and_rsa_patterns(ctx, tools):
    from mini_agent.tools.base import ToolError

    for name in ("server.pem", "id_rsa", "id_rsa_github"):
        (ctx.repo / name).write_text("SECRET\n", encoding="utf-8")
    (ctx.repo / "key.txt").write_text("not secret\n", encoding="utf-8")
    for name in ("server.pem", "id_rsa", "id_rsa_github"):
        with pytest.raises(ToolError, match=name):
            run_tool(tools, "read_file", path=name)
    assert run_tool(tools, "read_file", path="key.txt") == "not secret\n"


def test_deny_patterns_are_configurable(tmp_path):
    from mini_agent.tools.base import ToolError

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "secrets.key").write_text("s\n", encoding="utf-8")
    (repo / "server.pem").write_text("s\n", encoding="utf-8")
    custom = FsContext.create(repo, deny_patterns=(".env", "*.key"))
    tools = {t.name: t for t in make_fs_tools(custom)}
    with pytest.raises(ToolError, match="secrets.key"):
        tools["read_file"].execute({"path": "secrets.key"})
    assert tools["read_file"].execute({"path": "server.pem"}) == "s\n"  # 不再默认拒绝


# ---------- write_file:新建/覆盖二分 + 父目录自动创建 + 围栏 ----------


def test_write_new_file_with_missing_parents_is_allowed(ctx, tools):
    out = run_tool(tools, "write_file", path="docs/notes/notes.txt", content="hello")
    assert (ctx.repo / "docs" / "notes" / "notes.txt").read_text(encoding="utf-8") == "hello"
    assert "新建" in out


def test_write_existing_file_without_read_is_refused(ctx, tools):
    from mini_agent.tools.base import ToolError

    before = (ctx.repo / "src" / "app.py").read_text(encoding="utf-8")
    with pytest.raises(ToolError, match="先读"):
        run_tool(tools, "write_file", path="src/app.py", content="clobbered")
    assert (ctx.repo / "src" / "app.py").read_text(encoding="utf-8") == before  # 文件未被改


def test_write_existing_file_after_read_is_allowed(ctx, tools):
    run_tool(tools, "read_file", path="src/app.py")
    out = run_tool(tools, "write_file", path="src/app.py", content="print('fixed')\n")
    assert (ctx.repo / "src" / "app.py").read_text(encoding="utf-8") == "print('fixed')\n"
    assert "覆盖" in out


def test_second_write_requires_fresh_read(ctx, tools):
    from mini_agent.tools.base import ToolError

    run_tool(tools, "write_file", path="fresh.txt", content="v1")
    with pytest.raises(ToolError, match="先读"):
        run_tool(tools, "write_file", path="fresh.txt", content="v2")
    run_tool(tools, "read_file", path="fresh.txt")
    run_tool(tools, "write_file", path="fresh.txt", content="v3")
    assert (ctx.repo / "fresh.txt").read_text(encoding="utf-8") == "v3"


def test_write_new_file_path_still_goes_through_fence(ctx, tools):
    with pytest.raises(FenceError):
        run_tool(tools, "write_file", path="../evil.txt", content="x")
    with pytest.raises(FenceError):
        run_tool(tools, "write_file", path="new/../..//escape.txt", content="x")


def test_write_existing_directory_is_error(ctx, tools):
    from mini_agent.tools.base import ToolError

    with pytest.raises(ToolError, match="目录"):
        run_tool(tools, "write_file", path="src", content="x")


def test_denied_file_cannot_be_overwritten(ctx, tools):
    # 拒读 + 先读后写 ⇒ 零额外代码实现"密钥文件不可覆盖"
    from mini_agent.tools.base import ToolError

    (ctx.repo / ".env").write_text("API_KEY=x\n", encoding="utf-8")
    with pytest.raises(ToolError, match="密钥"):
        run_tool(tools, "read_file", path=".env")
    with pytest.raises(ToolError, match="先读"):
        run_tool(tools, "write_file", path=".env", content="stolen")
    assert (ctx.repo / ".env").read_text(encoding="utf-8") == "API_KEY=x\n"


def test_write_missing_content_argument_is_error(ctx, tools):
    from mini_agent.tools.base import ToolError

    with pytest.raises(ToolError, match="content"):
        run_tool(tools, "write_file", path="x.txt")


# ---------- search ----------


def test_search_returns_matches_with_line_numbers(ctx, tools):
    (ctx.repo / "src" / "util.py").write_text(
        "def a():\n    return 'needle here'\n\ndef b():\n    return 'needle again'\n",
        encoding="utf-8",
    )
    out = run_tool(tools, "search", pattern="needle")
    assert "src/util.py:2:" in out  # 带行号
    assert "src/util.py:5:" in out
    assert "'needle here'" in out


def test_search_caps_at_50_results(ctx, tools):
    for i in range(60):
        (ctx.repo / f"m{i:02d}.txt").write_text("haystack needle\n", encoding="utf-8")
    out = run_tool(tools, "search", pattern="needle")
    result_lines = [line for line in out.splitlines() if ":1:" in line]
    assert len(result_lines) == 50
    assert "50 条" in out


def test_search_skips_ignored_dirs_and_denied_files(ctx, tools):
    (ctx.repo / ".env").write_text("needle\n", encoding="utf-8")
    d = ctx.repo / "node_modules"
    d.mkdir()
    (d / "dep.js").write_text("needle\n", encoding="utf-8")
    (ctx.repo / "ok.txt").write_text("needle\n", encoding="utf-8")
    out = run_tool(tools, "search", pattern="needle")
    assert "ok.txt" in out
    assert ".env" not in out
    assert "dep.js" not in out


def test_search_skips_binary_files(ctx, tools):
    (ctx.repo / "data.bin").write_bytes(b"needle\x00binary")
    (ctx.repo / "ok.txt").write_text("needle\n", encoding="utf-8")
    out = run_tool(tools, "search", pattern="needle")
    assert "ok.txt" in out and "data.bin" not in out


def test_search_invalid_regex_is_error(ctx, tools):
    from mini_agent.tools.base import ToolError

    with pytest.raises(ToolError, match="正则"):
        run_tool(tools, "search", pattern="([unclosed")


def test_search_no_match_reports_none(ctx, tools):
    out = run_tool(tools, "search", pattern="definitely_absent_string")
    assert "未找到匹配" in out


# ---------- 工具描述与 schema ----------


def test_fs_tool_descriptions_are_chinese(tools):
    for tool in tools.values():
        assert tool.description, tool.name
        for ch in tool.description:
            if "\u4e00" <= ch <= "\u9fff":
                break
        else:
            pytest.fail(f"工具 {tool.name} 的描述不是中文:{tool.description}")


def test_fs_tools_share_read_state_within_context(ctx, tools):
    # read_file 标记的"已读"对同一上下文中的 write_file 可见(先读后写联动)
    run_tool(tools, "read_file", path="README.md")
    run_tool(tools, "write_file", path="README.md", content="updated\n")
    assert (ctx.repo / "README.md").read_text(encoding="utf-8") == "updated\n"
