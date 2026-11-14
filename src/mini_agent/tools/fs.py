"""文件四件套:list_files / read_file / write_file / search。

全部读写经 PathFence 围栏;write_file 强制"先读后写"(覆盖已存在文件前
必须本次运行读过,新建文件允许,父目录自动创建仍走围栏);read_file/
search 走密钥 deny-list(默认 ``.env`` 精确名、``*.pem``、``id_rsa*``,
可配置)——被拒读的文件因先读后写规则也无法被覆盖,零额外代码。
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from mini_agent.sandbox import PathFence
from mini_agent.tools.base import Tool, ToolError
from mini_agent.tools.descriptions import ZH_DESCRIPTIONS

# 忽略目录集(spec「工具集」节;与快照层 SNAPSHOT_IGNORED_DIRS 语义一致)
IGNORED_DIRS = frozenset(
    {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", "mini-agent-runs"}
)

# 上限(spec 钉死的默认值)
LIST_MAX_DEPTH = 8
LIST_MAX_ENTRIES = 500
READ_MAX_LINES = 2000
READ_MAX_BYTES = 100 * 1024
SEARCH_MAX_RESULTS = 50
BINARY_SNIFF_BYTES = 8192

# 默认密钥 deny-list:精确名(无通配符)按全名匹配,含通配符按 fnmatch
DEFAULT_DENY_PATTERNS = (".env", "*.pem", "id_rsa*")


def _is_denied(name: str, patterns: tuple[str, ...]) -> bool:
    # fnmatchcase 下无通配符的 pattern 即全等匹配(.env 精确命中,
    # .env.example 放行);大小写敏感,与 deny-list 字面语义一致
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


@dataclass
class FsContext:
    """四个文件工具共享的运行期状态:围栏、已读集合、密钥 deny-list。"""

    repo: Path
    fence: PathFence
    read_files: set[Path] = field(default_factory=set)
    deny_patterns: tuple[str, ...] = DEFAULT_DENY_PATTERNS

    @classmethod
    def create(
        cls, repo: Path, *, deny_patterns: tuple[str, ...] = DEFAULT_DENY_PATTERNS
    ) -> FsContext:
        fence = PathFence(repo)
        return cls(repo=fence.root, fence=fence, deny_patterns=deny_patterns)

    def is_denied(self, name: str) -> bool:
        return _is_denied(name, self.deny_patterns)

    def resolve(self, user_path: str) -> Path:
        return self.fence.resolve_path(user_path)

    def relpath(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.repo))
        except ValueError:  # pragma: no cover - resolve 已保证在仓库内
            return str(path)


def _read_text_file(path: Path) -> str:
    """读文本文件:不存在/是目录/二进制/不可解码都转成中文 ToolError。"""
    if not path.exists():
        raise ToolError(f"文件不存在:{path.name}")
    if not path.is_file():
        raise ToolError(f"目标不是文件(是目录):{path.name}")
    data = path.read_bytes()
    if b"\x00" in data[:BINARY_SNIFF_BYTES]:
        raise ToolError(f"拒绝读取二进制文件:{path.name}(仅支持文本文件)")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError(f"拒绝读取无法按 UTF-8 解码的文件:{path.name}({exc})") from exc


def _require_int(args: dict, key: str, *, default: int | None = None) -> int | None:
    value = args.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"参数 {key} 必须是整数")
    if value < 1:
        raise ToolError(f"参数 {key} 必须不小于 1(offset 从 1 开始,limit 至少 1)")
    return value


def _require_str(args: dict, key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str):
        raise ToolError(f"缺少字符串参数 {key}")
    return value


# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------


def _render_tree(root: Path, *, max_depth: int = LIST_MAX_DEPTH, max_entries: int = LIST_MAX_ENTRIES) -> str:
    """把目录渲染成树状文本(不跟随符号链接,忽略 IGNORED_DIRS)。

    超过深度上限的子目录不再展开;条数达到上限时补一条说明后停止。
    """
    lines: list[str] = [f"{root.name}/"]
    listed = 0

    def walk(directory: Path, prefix: str, depth: int) -> None:
        nonlocal listed
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name)
        except OSError:
            return
        for i, entry in enumerate(entries):
            if entry.is_dir() and not entry.is_symlink() and entry.name in IGNORED_DIRS:
                continue
            if listed >= max_entries:
                lines.append(f"{prefix}…(已达条数上限 {max_entries},其余条目未列出)")
                return
            last = i == len(entries) - 1
            connector = "└── " if last else "├── "
            listed += 1
            if entry.is_symlink():
                lines.append(f"{prefix}{connector}{entry.name} -> {os.readlink(entry)}")
            elif entry.is_dir():
                lines.append(f"{prefix}{connector}{entry.name}/")
                if depth < max_depth:
                    walk(entry, prefix + ("    " if last else "│   "), depth + 1)
            else:
                lines.append(f"{prefix}{connector}{entry.name}")

    walk(root, "", 1)
    return "\n".join(lines)


class ListFilesTool:
    name = "list_files"
    description = ZH_DESCRIPTIONS["list_files"]
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对仓库根的子目录路径,默认列出整个仓库"}
        },
        "required": [],
    }

    def __init__(self, ctx: FsContext) -> None:
        self._ctx = ctx

    def execute(self, args: dict) -> str:
        target = self._ctx.resolve(args.get("path") or ".")
        if not target.exists():
            raise ToolError(f"目录不存在:{args.get('path')}")
        if not target.is_dir():
            raise ToolError(f"目标不是目录:{args.get('path')}")
        return _render_tree(target)


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


class ReadFileTool:
    name = "read_file"
    description = ZH_DESCRIPTIONS["read_file"]
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径(相对仓库根,或仓库内绝对路径)"},
            "offset": {"type": "integer", "description": "起始行号(从 1 开始,默认 1)"},
            "limit": {"type": "integer", "description": "本次最多返回的行数"},
        },
        "required": ["path"],
    }

    def __init__(self, ctx: FsContext) -> None:
        self._ctx = ctx

    def execute(self, args: dict) -> str:
        user_path = _require_str(args, "path")
        offset = _require_int(args, "offset", default=1)
        limit = _require_int(args, "limit", default=None)
        path = self._ctx.resolve(user_path)
        if self._ctx.is_denied(path.name):
            raise ToolError(
                f"拒绝读取密钥文件:{path.name}(命中密钥 deny-list,"
                "此类文件不进入模型上下文;如属误伤请让用户调整配置)"
            )
        text = _read_text_file(path)
        lines = text.splitlines(keepends=True)
        start = (offset or 1) - 1
        selected = lines[start : start + limit] if limit else lines[start:]
        if len(selected) > READ_MAX_LINES:
            raise ToolError(
                f"本次读取 {len(selected)} 行,超过单次上限 {READ_MAX_LINES} 行:"
                "请用 offset/limit 分段读取"
            )
        content = "".join(selected)
        if len(content.encode("utf-8")) > READ_MAX_BYTES:
            raise ToolError(
                f"本次读取 {len(content.encode('utf-8'))} 字节,超过单次上限 100KB:"
                "请用 offset/limit 分段读取"
            )
        self._ctx.read_files.add(path)  # 先读后写标记
        return content


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


class WriteFileTool:
    name = "write_file"
    description = ZH_DESCRIPTIONS["write_file"]
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目标文件路径(相对仓库根,或仓库内绝对路径)"},
            "content": {"type": "string", "description": "写入的完整文件内容(整文件替换)"},
        },
        "required": ["path", "content"],
    }

    def __init__(self, ctx: FsContext) -> None:
        self._ctx = ctx

    def execute(self, args: dict) -> str:
        user_path = _require_str(args, "path")
        content = _require_str(args, "content")
        path = self._ctx.resolve(user_path)  # 父目录自动创建前先过围栏
        if path.exists() and not path.is_file():
            raise ToolError(f"目标是已存在的目录,无法写入:{self._ctx.relpath(path)}")
        rel = self._ctx.relpath(path)
        if path.exists() and path not in self._ctx.read_files:
            raise ToolError(
                f"文件已存在但本次运行尚未读取,禁止盲覆盖:{rel}"
                "(先读后写规则:请先用 read_file 读原内容再修改写入;新建文件不受此限制)"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        action = "覆盖已读文件" if rel and path in self._ctx.read_files else "新建文件"
        # 成功写入即视为已知内容:覆盖自己刚写的文件不算盲覆盖(先读后写的本意
        # 是防"没见过就覆盖",对刚写出的内容不成立)
        self._ctx.read_files.add(path)
        return f"已{action}:{rel}({len(content.encode('utf-8'))} 字节)"


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def _iter_repo_files(root: Path, *, max_depth: int = 32) -> Iterator[Path]:
    """确定性遍历仓库文件:跳过忽略目录与符号链接(围栏安全)。"""
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            path = Path(entry.path)
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                if entry.name in IGNORED_DIRS or depth + 1 > max_depth:
                    continue
                stack.append((path, depth + 1))
            elif entry.is_file():
                yield path


class SearchTool:
    name = "search"
    description = ZH_DESCRIPTIONS["search"]
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python 正则表达式(如 'def \\w+Handle'）"}
        },
        "required": ["pattern"],
    }

    def __init__(self, ctx: FsContext) -> None:
        self._ctx = ctx

    def execute(self, args: dict) -> str:
        pattern = _require_str(args, "pattern")
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ToolError(f"正则表达式不合法:{pattern}({exc})") from exc
        results: list[str] = []
        capped = False
        for path in _iter_repo_files(self._ctx.repo):
            if self._ctx.is_denied(path.name):
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:BINARY_SNIFF_BYTES]:
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            rel = self._ctx.relpath(path)
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    results.append(f"{rel}:{lineno}: {line}")
                    if len(results) >= SEARCH_MAX_RESULTS:
                        capped = True
                        break
            if capped:
                break
        if not results:
            return f"未找到匹配:{pattern}"
        suffix = f"\n…(已达 {SEARCH_MAX_RESULTS} 条上限,后续匹配未列出)" if capped else ""
        return "\n".join(results) + suffix


def make_fs_tools(ctx: FsContext) -> list[Tool]:
    """构造共享同一上下文的四个文件工具。"""
    return [ListFilesTool(ctx), ReadFileTool(ctx), WriteFileTool(ctx), SearchTool(ctx)]
