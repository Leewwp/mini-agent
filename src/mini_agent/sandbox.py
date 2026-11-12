"""沙箱护栏(尽力而为,防误伤不防对抗,spec Implementation Decisions)。

本模块承载四类边界,全部供工具层/CLI 复用:
- 路径围栏 PathFence:所有读写限制在 --repo 内,拒绝 ``..`` 逃逸、
  仓库外绝对路径与指向仓库外的符号链接穿透;
- 命令检查(工单 02):shlex 归一化 + 危险命令黑名单 + run_command allowlist;
- 子进程执行(工单 02 骨架):env 最小化、进程组超时击杀、输出上限;
- 影子 git 快照(工单 04):临时影子库 + ``--work-tree`` 指向目标仓库,
  目标仓库 HEAD/index/工作区零写入的改动凭证;git 不可用/超时自动降级。

威胁模型:防误伤,不防对抗;OS 级隔离明确不在 V1 范围。
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import tempfile
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


# ---------------------------------------------------------------------------
# 影子 git 快照(工单 04):目标仓库零写入的改动凭证
#
# recipe(15 分钟 spike 验证):临时目录 ``git --git-dir=SHADOW --work-tree=REPO
# init``,影子库 info/exclude 排除 ``mini-agent-runs/``;运行前 ``add -A`` +
# ``commit --allow-empty`` 记基线,结束再 ``add -A`` + ``commit``,diff 两个
# commit。全程只写影子库(GIT_DIR),目标仓库的 HEAD/index/工作区零写入;
# 目标目录是否有 .git、是否有未提交改动都不影响(基线把原有状态一并记录,
# diff 因此只反映本次 agent 的改动;untracked 新文件自动进入清单)。快照
# 遵循目标仓库 .gitignore 的忽略规则(git 原生语义)。commit 一律带
# ``-c user.name/-c user.email`` 兜底,干净机器无全局身份也能跑。
# ---------------------------------------------------------------------------

SNAPSHOT_TIMEOUT_SECONDS = 60.0  # 单条 git 命令的超时(仓库过大时触发降级)
SNAPSHOT_GIT_USER = ("mini-agent-snapshot", "mini-agent-snapshot@local")
# 外层环境里会劫持影子库定位的变量,一律剔除(显式 --git-dir/--work-tree 优先,
# 但 GIT_INDEX_FILE/GIT_OBJECT_DIRECTORY 等仍会覆盖我们指定的位置)
_GIT_ENV_EXCLUDES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
)
# 降级文件清单跳过的目录(与工具层 IGNORED_DIRS 语义一致;独立定义,
# 避免 sandbox ← tools 的反向依赖)
SNAPSHOT_IGNORED_DIRS = frozenset(
    {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", "mini-agent-runs"}
)
# 影子库 info/exclude 的排除模式(与 SNAPSHOT_IGNORED_DIRS 同一集合语义;
# __pycache__/.pytest_cache 是 pytest 副产物噪声,目标仓库无 .gitignore 时
# 也不能进 diff 与终报修改清单)
SNAPSHOT_EXCLUDE_PATTERNS = ("mini-agent-runs/", "__pycache__/", ".pytest_cache/")


class SnapshotDegradeError(Exception):
    """影子快照失败(git 不可用/命令超时/内部错误);携带降级原因代码。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class FileChange:
    """一个文件在本轮运行中的改动。``status`` 为 git 字母:A/M/D/…。"""

    path: str  # 相对仓库根,posix 风格
    status: str
    insertions: int | None  # 二进制文件为 None
    deletions: int | None


@dataclass(frozen=True)
class ChangeSet:
    """一次运行的改动凭证。

    ``available=True`` 时 ``diff_text`` 是两个快照 commit 间的完整 unified
    diff,``changes`` 是解析后的文件清单;``available=False`` 表示降级为文件
    清单对比(无 diff 凭证,``degraded_reason`` 说明原因)。
    """

    available: bool
    changes: tuple[FileChange, ...] = ()
    diff_text: str = ""
    degraded_reason: str | None = None
    baseline_dirty: bool = False  # 基线时目标仓库已有未提交改动(报告注明)
    baseline_untracked: int = 0
    notes: tuple[str, ...] = ()


def _manifest(repo: Path) -> dict[str, int]:
    """降级用文件清单:``相对路径 -> 字节数``,跳过忽略目录与符号链接。"""
    manifest: dict[str, int] = {}
    stack = [Path(repo)]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            path = Path(entry.path)
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                if entry.name in SNAPSHOT_IGNORED_DIRS:
                    continue
                stack.append(path)
            elif entry.is_file(follow_symlinks=False):
                try:
                    manifest[path.relative_to(repo).as_posix()] = entry.stat().st_size
                except OSError:
                    continue
    return manifest


def _manifest_changes(before: dict[str, int], after: dict[str, int]) -> tuple[FileChange, ...]:
    """对比两份文件清单:A 新增 / D 删除 / M 大小变化(降级模式无行级 diff)。"""
    changes = []
    for path in sorted(set(before) | set(after)):
        if path not in before:
            changes.append(FileChange(path, "A", None, None))
        elif path not in after:
            changes.append(FileChange(path, "D", None, None))
        elif before[path] != after[path]:
            changes.append(FileChange(path, "M", None, None))
    return tuple(changes)


def _parse_numstat_line(line: str) -> tuple[str, int | None, int | None] | None:
    """解析 ``git diff --numstat --no-renames`` 的一行:<ins>\\t<del>\\t<path>。"""
    parts = line.split("\t")
    if len(parts) != 3:
        return None
    ins_raw, del_raw, path = parts
    insertions = None if ins_raw == "-" else int(ins_raw)
    deletions = None if del_raw == "-" else int(del_raw)
    return path, insertions, deletions


class ShadowGitSnapshot:
    """目标仓库零写入的前后快照;git 不可用/超时自动降级为文件清单。

    用法:``snapshot = ShadowGitSnapshot.start(repo)`` 在运行开始时调用;
    agent 改动目标仓库;``changes = snapshot.finish()`` 拿到改动凭证。
    ``git_bin``/``timeout`` 可注入(测试用);失败绝不向上抛——凭证是尽力而为,
    主流程不因快照而中断。
    """

    def __init__(self, repo: Path, *, git_bin: str = "git", timeout: float = SNAPSHOT_TIMEOUT_SECONDS) -> None:
        self.repo = Path(repo).resolve()
        self.git_bin = git_bin  # 公开可注入:finish 前替换可模拟结束阶段故障
        self.timeout = timeout
        self._shadow_dir: Path | None = None
        self._baseline: str | None = None
        self._baseline_dirty = False
        self._baseline_untracked = 0
        self._baseline_notes: tuple[str, ...] = ()
        self._start_manifest: dict[str, int] | None = None
        self._degraded_reason: str | None = None

    # -- 生命周期 --

    @classmethod
    def start(
        cls,
        repo: Path,
        *,
        git_bin: str = "git",
        timeout: float = SNAPSHOT_TIMEOUT_SECONDS,
    ) -> ShadowGitSnapshot:
        """建影子库并记基线 commit;任何失败都降级,绝不向上抛。"""
        snapshot = cls(repo, git_bin=git_bin, timeout=timeout)
        try:
            snapshot._start_git()
        except SnapshotDegradeError as exc:
            snapshot._degrade(exc.reason)
        except Exception as exc:  # noqa: BLE001 —— 防御:凭证失败不能中断主流程,一律降级
            snapshot._degrade(f"snapshot_failed: {type(exc).__name__}: {exc}")
        return snapshot

    def finish(self) -> ChangeSet:
        """结束 commit + diff 两个快照;失败降级,绝不向上抛。"""
        if self._degraded_reason is not None or self._start_manifest is not None:
            return self._finish_degraded()
        try:
            return self._finish_git()
        except SnapshotDegradeError as exc:
            self._degrade(exc.reason)
            return self._finish_degraded()
        except Exception as exc:  # noqa: BLE001 —— 防御:同上,凭证失败降级不外抛
            self._degrade(f"snapshot_failed: {type(exc).__name__}: {exc}")
            return self._finish_degraded()

    # -- git 模式 --

    def _start_git(self) -> None:
        self._shadow_dir = Path(tempfile.mkdtemp(prefix="mini-agent-shadow-"))
        self._git(["init", "-q"])
        exclude = self._shadow_dir / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as fh:
            fh.write("\n" + "\n".join(SNAPSHOT_EXCLUDE_PATTERNS) + "\n")
        self._record_baseline_state()
        self._commit("mini-agent baseline snapshot")
        self._baseline = self._rev("HEAD")

    def _finish_git(self) -> ChangeSet:
        assert self._shadow_dir is not None and self._baseline is not None
        self._commit("mini-agent final snapshot")
        final = self._rev("HEAD")
        name_status = self._git(["diff", "--no-renames", "--name-status", self._baseline, final])
        numstat = self._git(["diff", "--no-renames", "--numstat", self._baseline, final])
        diff_text = self._git(["diff", "--no-renames", self._baseline, final])
        stats: dict[str, tuple[int | None, int | None]] = {}
        for line in numstat.splitlines():
            parsed = _parse_numstat_line(line)
            if parsed:
                path, ins, dels = parsed
                stats[path] = (ins, dels)
        changes = []
        for line in name_status.splitlines():
            if not line.strip():
                continue
            status, _, path = line.partition("\t")
            ins, dels = stats.get(path, (None, None))
            changes.append(FileChange(path, status, ins, dels))
        notes = ["快照遵循目标仓库 .gitignore 的忽略规则(被忽略的文件不进入 diff)"]
        if self._baseline_dirty:
            notes.append(
                f"基线包含目标仓库原有的未提交改动(untracked {self._baseline_untracked} 个);"
                "diff 只反映本次运行的改动"
            )
        changeset = ChangeSet(
            available=True,
            changes=tuple(changes),
            diff_text=diff_text,
            baseline_dirty=self._baseline_dirty,
            baseline_untracked=self._baseline_untracked,
            notes=tuple(notes),
        )
        self._cleanup()
        return changeset

    # -- 降级模式 --

    def _degrade(self, reason: str) -> None:
        """进入降级模式:改用文件清单对比;已建影子库就地清理。"""
        if reason.startswith("git_unavailable"):
            code = "git_unavailable"
        elif reason.startswith("snapshot_timeout"):
            code = "snapshot_timeout"
        else:
            code = "snapshot_failed"
        if self._start_manifest is None:
            # 基线阶段就失败:现在取清单作为"起点"(改动可能已发生,如实降级)
            self._start_manifest = _manifest(self.repo)
        self._degraded_reason = code
        self._cleanup()

    def _finish_degraded(self) -> ChangeSet:
        end_manifest = _manifest(self.repo)
        start = self._start_manifest or {}
        changes = _manifest_changes(start, end_manifest)
        return ChangeSet(
            available=False,
            changes=changes,
            degraded_reason=self._degraded_reason,
            baseline_dirty=self._baseline_dirty,
            baseline_untracked=self._baseline_untracked,
            notes=(
                ("无 diff 凭证:git 不可用或快照超时,已降级为文件清单对比"
                "(新增/删除/大小变化,无行级 diff)"),
            ),
        )

    def _cleanup(self) -> None:
        if self._shadow_dir is not None:
            shutil.rmtree(self._shadow_dir, ignore_errors=True)
            self._shadow_dir = None

    # -- git 子进程封装 --

    def _base_args(self) -> list[str]:
        return [self.git_bin, "--git-dir", str(self._shadow_dir), "--work-tree", str(self.repo)]

    def _env(self) -> dict:
        """复制当前环境并剔除会劫持影子库定位的 GIT_* 变量。"""
        return {k: v for k, v in os.environ.items() if k not in _GIT_ENV_EXCLUDES}

    def _git(self, args: list[str]) -> str:
        """执行影子库 git 命令并返回 stdout(超时/不可用 → SnapshotDegradeError)。"""
        assert self._shadow_dir is not None
        try:
            proc = subprocess.run(
                self._base_args() + args,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=self._env(),
                check=False,  # 返回码在下方统一判定并转降级
            )
        except FileNotFoundError as exc:
            raise SnapshotDegradeError("git_unavailable", f"git 不可用:{exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise SnapshotDegradeError("snapshot_timeout", f"git 命令超时:{args[0]}") from exc
        if proc.returncode != 0:
            raise SnapshotDegradeError(
                "snapshot_failed", f"git {args[0]} 失败:{proc.stderr.strip()[:500]}"
            )
        return proc.stdout

    def _commit(self, message: str) -> None:
        """add -A + commit(--allow-empty 空改动也成功;-c 身份兜底)。"""
        self._git(["add", "-A"])
        self._git(
            [
                "-c", f"user.name={SNAPSHOT_GIT_USER[0]}",
                "-c", f"user.email={SNAPSHOT_GIT_USER[1]}",
                "commit", "-q", "--allow-empty", "-m", message,
            ]
        )

    def _rev(self, rev: str) -> str:
        return self._git(["rev-parse", rev]).strip()

    def _record_baseline_state(self) -> None:
        """探测目标仓库基线时是否已有未提交改动/untracked(只读,供报告注明)。

        用目标仓库自己的 git,``--no-optional-locks`` 保证纯只读(不刷新
        index 的 stat 缓存);目标目录不是 git 仓库时静默跳过(影子库仍完整
        记录当前状态)。
        """
        try:
            proc = subprocess.run(
                [self.git_bin, "-C", str(self.repo), "--no-optional-locks", "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=self._env(),
                check=False,  # 非 git 目录返回非零,下方静默跳过
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return
        if proc.returncode != 0:
            return  # 非 git 目录:零写入前提下无从谈起"未提交改动"
        for line in proc.stdout.splitlines():
            if line.startswith("??"):
                if not line[3:].strip('\"').startswith("mini-agent-runs"):
                    self._baseline_untracked += 1
            else:
                self._baseline_dirty = True

