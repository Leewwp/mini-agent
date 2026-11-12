"""沙箱护栏(尽力而为,防误伤不防对抗,spec Implementation Decisions)。

本模块承载三类边界,全部供工具层复用:
- 路径围栏 PathFence:所有读写限制在 --repo 内,拒绝 ``..`` 逃逸、
  仓库外绝对路径与指向仓库外的符号链接穿透;
- 命令检查(工单 02):shlex 归一化 + 危险命令黑名单 + run_command allowlist;
- 子进程执行(工单 02 骨架):env 最小化、进程组超时击杀、输出上限。

威胁模型:防误伤,不防对抗;OS 级隔离明确不在 V1 范围。
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# run_command 默认 allowlist:解释器/构建/测试类命令(spec「沙箱」节)。
# 有意可被 python -c / npm run 穿透——威胁模型是防误伤、不防对抗。
DEFAULT_COMMAND_ALLOWLIST = frozenset(
    {"python", "python3", "pytest", "node", "npm", "npx", "go", "cargo", "make"}
)


class FenceError(Exception):
    """路径越界(../ 逃逸、仓库外绝对路径、符号链接穿透仓库边界)。"""


class CommandRejected(Exception):
    """命令被拒绝(空命令/引号不合法/黑名单/不在 allowlist)。"""


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


# ---------------------------------------------------------------------------
# 命令检查:先 shlex 归一化,黑名单只保留明显危险命令(spec「沙箱」节)
# ---------------------------------------------------------------------------


def normalize_command(command: str) -> list[str]:
    """shlex 归一化:拆分词元、去引号;空命令与不合法引号直接拒绝。"""
    try:
        words = shlex.split(command)
    except ValueError as exc:
        raise CommandRejected(f"命令引号不合法,无法解析:{command}({exc})") from exc
    if not words:
        raise CommandRejected(f"空命令:{command!r}")
    return words


def _expanded_flags(words: list[str]) -> set[str]:
    """把 rm 的参数展开成单字符/长参数集合,覆盖 -rf / -fr / -r -f / --recursive --force。"""
    flags: set[str] = set()
    for word in words[1:]:
        if word == "--recursive" or word == "--force":
            flags.add(word.lstrip("-"))
        elif word.startswith("-") and not word.startswith("--") and len(word) > 1:
            flags.update(word[1:])
    return flags


def _looks_like_fork_bomb(words: list[str]) -> bool:
    """经典 fork 炸弹 ``:(){ :|:& };:`` 的特征:同时出现 ``(){`` 与 ``|:&`` 词元。"""
    return any("()" in w and "{" in w for w in words) and any("|:&" in w for w in words)


def check_blacklist(words: list[str]) -> None:
    """危险命令黑名单:rm -rf(递归+强制)、sudo(任意位置)、git push、fork 炸弹。"""
    first = os.path.basename(words[0])
    if first == "rm":
        flags = _expanded_flags(words)
        recursive = "r" in flags or "recursive" in flags
        force = "f" in flags or "force" in flags
        if recursive and force:
            raise CommandRejected("危险命令已被拒绝:rm 同时带递归(-r)与强制(-f)参数")
    if any(os.path.basename(w) == "sudo" for w in words):
        raise CommandRejected("危险命令已被拒绝:不允许以 sudo 提权执行任何命令")
    if first == "git" and len(words) > 1 and words[1] == "push":
        raise CommandRejected("危险命令已被拒绝:不允许 git push(会改动远端仓库)")
    if _looks_like_fork_bomb(words):
        raise CommandRejected("危险命令已被拒绝:检测到 fork 炸弹模式")


def check_allowlist(words: list[str], allowlist=DEFAULT_COMMAND_ALLOWLIST) -> None:
    """run_command 的 allowlist 检查:首个词元(取 basename)必须在允许集合内。"""
    first = os.path.basename(words[0])
    if first not in allowlist:
        allowed = "/".join(sorted(allowlist))
        raise CommandRejected(
            f"命令不在 allowlist 内:{first}(run_command 只允许 {allowed} "
            "等解释器/构建/测试类命令;运行测试请用 run_tests)"
        )


# ---------------------------------------------------------------------------
# 子进程 env 最小化:API key 对子进程不可见(spec「沙箱」节)
# ---------------------------------------------------------------------------


def minimal_env(base: dict | None = None) -> dict:
    """复制 base(默认 os.environ)并剔除所有 ``MINI_AGENT_*`` 变量。"""
    env = dict(os.environ if base is None else base)
    return {key: value for key, value in env.items() if not key.startswith("MINI_AGENT_")}


# ---------------------------------------------------------------------------
# 子进程执行骨架(工单 02 落地,03 的 run_tests/run_command 闭环复用)
# ---------------------------------------------------------------------------

SUBPROCESS_OUTPUT_CAP = 64 * 1024  # 每个流(stdout/stderr)的输出上限
OUTPUT_HEAD_BYTES = 32 * 1024
OUTPUT_TAIL_BYTES = 32 * 1024


@dataclass(frozen=True)
class ProcessResult:
    """一次子进程执行的结果(stdout/stderr 已按上限截断)。"""

    command: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int


def clip_output(data: bytes, *, cap: int = SUBPROCESS_OUTPUT_CAP) -> str:
    """把输出字节截到头尾各 32KB 并解码(纯函数;超限补中文说明)。"""
    if len(data) <= cap:
        return data.decode("utf-8", errors="replace")
    omitted = len(data) - OUTPUT_HEAD_BYTES - OUTPUT_TAIL_BYTES
    clipped = (
        data[:OUTPUT_HEAD_BYTES].decode("utf-8", errors="ignore")
        + f"\n…[输出已截断 {omitted} 字节]…\n"
        + data[-OUTPUT_TAIL_BYTES:].decode("utf-8", errors="ignore")
    )
    return clipped


def run_subprocess(
    words: list[str],
    *,
    cwd: Path,
    timeout: float,
    env_base: dict | None = None,
) -> ProcessResult:
    """在 cwd 执行 words(不经 shell):env 最小化、进程组超时击杀、输出上限。

    - ``start_new_session=True`` 使子进程成为新进程组长,超时用 ``killpg``
      击杀整个进程组(孙子进程不残留);
    - 超时后 ``timed_out=True``、returncode 为负(被 SIGKILL);
    - 命令不存在等启动失败向上抛 FileNotFoundError,由调用方转工具错误。
    """
    started = time.monotonic()
    proc = subprocess.Popen(
        words,
        cwd=str(cwd),
        env=minimal_env(env_base),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # pgid == pid(start_new_session)
        except ProcessLookupError:
            pass  # 进程恰好已退出
        out, err = proc.communicate()
    duration_ms = int((time.monotonic() - started) * 1000)
    return ProcessResult(
        command=list(words),
        returncode=proc.returncode,
        stdout=clip_output(out or b""),
        stderr=clip_output(err or b""),
        timed_out=timed_out,
        duration_ms=duration_ms,
    )
