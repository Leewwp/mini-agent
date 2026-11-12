"""append-only JSONL 会话事件日志(扩展接缝之四)。

语义(来自 spec):
- 目录 ``<base>/<时间戳>-<随机后缀>/session.jsonl``;默认 base 为 ``./mini-agent-runs``,
  若其落在目标仓库内则改用系统临时目录,避免污染目标仓库。
- append-only:只增不改写(V2 压缩/记忆的数据地基);每事件写后 flush,被 kill 时日志仍完整。
- 不记录 API key 与请求头。
"""

from __future__ import annotations

import json
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUNS_DIR_NAME = "mini-agent-runs"


def resolve_runs_base(*, base_dir: Path | None = None, repo: Path | None = None) -> Path:
    """确定会话根目录;默认位置落在目标仓库内时回退到系统临时目录。"""
    base = Path(base_dir) if base_dir is not None else Path.cwd() / RUNS_DIR_NAME
    base = base.resolve()
    if repo is not None:
        try:
            base.relative_to(Path(repo).resolve())
        except ValueError:
            pass  # 不在 repo 内,正常使用
        else:
            base = (Path(tempfile.gettempdir()) / RUNS_DIR_NAME).resolve()
    return base


class SessionLogger:
    """向 session.jsonl 追加事件;每个事件一行 JSON,写后立即 flush。"""

    def __init__(self, session_dir: Path) -> None:
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.session_dir / "session.jsonl"
        self._file = self.path.open("a", encoding="utf-8")

    @classmethod
    def create(
        cls,
        *,
        base_dir: Path | None = None,
        repo: Path | None = None,
        now: datetime | None = None,
        suffix: str | None = None,
    ) -> "SessionLogger":
        """按 ``<YYYYmmdd-HHMMSS>-<随机后缀>`` 命名新建会话目录。

        ``now``/``suffix`` 可注入,测试用确定性值。
        """
        base = resolve_runs_base(base_dir=base_dir, repo=repo)
        now = now if now is not None else datetime.now()
        suffix = suffix if suffix is not None else secrets.token_hex(2)
        return cls(base / f"{now:%Y%m%d-%H%M%S}-{suffix}")

    def emit(self, type_: str, /, **payload: Any) -> None:
        """追加一条事件;``type`` 为事件类型,payload 任意 JSON 字段。"""
        event = {
            "type": type_,
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            **payload,
        }
        self._file.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> "SessionLogger":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
