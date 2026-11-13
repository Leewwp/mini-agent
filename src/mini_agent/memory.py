"""项目记忆文件(工单 14 / 设计票 05):repo 根 ``AGENTS.md``,REPL-only。

- 只认 repo 根目录的 ``AGENTS.md``(不做子目录级联/override/local 变体;
  ``CLAUDE.md`` 次选不做);文件不存在静默跳过;
- 安全读取:``Path.resolve()`` 后必须在 repo 内、``is_file()``、先
  ``stat().st_size`` 再读、拒绝指向 repo 外的符号链接(忽略并告警);
- 32KiB 截断 + UTF-8 ``errors="replace"``;截断在 ``memory_loaded`` 事件
  ``truncated=True`` 标记并向用户提示;
- 只读加载,不自动改写;写入靠模型用现有 read_file/write_file(先读后写
  围栏天然生效),不新增第 8 个工具;
- 注入只发生在 REPL 装配层(compose_system_prompt);batch 的 system
  prompt/日志/测试零改动。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MEMORY_FILE_NAME = "AGENTS.md"
MAX_MEMORY_BYTES = 32 * 1024


@dataclass(frozen=True)
class MemoryFile:
    """加载结果;``event_fields`` 即 ``memory_loaded`` 事件字段(path/bytes/truncated)。"""

    text: str
    event_fields: dict


def load_project_memory(
    repo: Path,
    *,
    warn: Callable[[str], None] | None = None,
) -> MemoryFile | None:
    """读取 repo 根 ``AGENTS.md``;不存在/不合法 → None(静默或告警)。"""
    repo = Path(repo).resolve()
    raw = repo / MEMORY_FILE_NAME
    if not raw.exists():
        return None
    # 安全:符号链接解析后必须仍在 repo 内(防逃逸);必须普通文件
    resolved = raw.resolve()
    try:
        resolved.relative_to(repo)
    except ValueError:
        if warn is not None:
            warn(f"已忽略 {MEMORY_FILE_NAME}:符号链接指向仓库外({raw})")
        return None
    if not resolved.is_file():
        if warn is not None:
            warn(f"已忽略 {MEMORY_FILE_NAME}:不是普通文件({raw})")
        return None

    stat_size = resolved.stat().st_size  # 先 stat 后读
    truncated = stat_size > MAX_MEMORY_BYTES
    with resolved.open("rb") as fh:
        data = fh.read(MAX_MEMORY_BYTES)  # 截断:只读前 32KiB
    text = data.decode("utf-8", errors="replace")
    event_fields = {
        "path": MEMORY_FILE_NAME,  # 相对路径,不泄露绝对路径
        "bytes": stat_size,
        "truncated": truncated,
    }
    return MemoryFile(text=text, event_fields=event_fields)


def make_memory_loader(
    repo: Path,
) -> Callable[[], tuple[str | None, dict | None]]:
    """ChatSession 的默认记忆加载器(REPL 装配层用;batch 不经过这里)。"""

    def _load() -> tuple[str | None, dict | None]:
        memory = load_project_memory(repo)
        if memory is None:
            return None, None
        return memory.text, memory.event_fields

    return _load


__all__ = ["MAX_MEMORY_BYTES", "MEMORY_FILE_NAME", "MemoryFile", "load_project_memory", "make_memory_loader"]
