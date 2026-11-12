"""沙箱护栏(尽力而为,防误伤不防对抗,spec Implementation Decisions)。

本模块承载三类边界,全部供工具层复用:
- 路径围栏 PathFence:所有读写限制在 --repo 内,拒绝 ``..`` 逃逸、
  仓库外绝对路径与指向仓库外的符号链接穿透;
- 命令检查(工单 02):shlex 归一化 + 危险命令黑名单 + run_command allowlist;
- 子进程执行(工单 02 骨架):env 最小化、进程组超时击杀、输出上限。

威胁模型:防误伤,不防对抗;OS 级隔离明确不在 V1 范围。
"""

from __future__ import annotations

from pathlib import Path


class FenceError(Exception):
    """路径越界(../ 逃逸、仓库外绝对路径、符号链接穿透仓库边界)。"""


class PathFence:
    """把一切文件路径限制在 root 内;判定前先做符号链接解析,防穿透。

    规则:相对路径以 root 为基准拼接;绝对路径仅当解析后仍落在 root 内
    才放行;``..`` 归一化后越界即拒绝(未越界的 ``sub/../x`` 放行)。
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    def resolve_path(self, user_path: str) -> Path:
        """把模型/调用方给出的路径解析为仓库内绝对路径;越界抛 FenceError。"""
        raw = Path(user_path)
        candidate = raw if raw.is_absolute() else self.root / raw
        resolved = candidate.resolve(strict=False)
        if not self.contains(resolved):
            raise FenceError(
                f"路径越界:拒绝访问仓库外路径 {user_path}"
                f"(所有读写必须限制在仓库目录 {self.root} 内;"
                ".. 逃逸、仓库外绝对路径与指向仓库外的符号链接都会被拒绝)"
            )
        return resolved

    def contains(self, path: Path) -> bool:
        """路径(解析符号链接后)是否落在仓库内。"""
        resolved = Path(path) if Path(path).is_absolute() else (self.root / path)
        try:
            resolved.resolve(strict=False).relative_to(self.root)
        except ValueError:
            return False
        return True
