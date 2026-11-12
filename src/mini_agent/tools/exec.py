"""命令执行工具:run_command(allowlist 约束)/ run_tests(可信命令,verifier)。

权限分层(spec「沙箱」节):
- run_command:shlex 归一化 → 黑名单 → allowlist,黑名单优先;
  其执行结果**不计入成功判定**(run_command 不标 is_verifier);
- run_tests:执行用户显式提供的 ``--test-cmd``,视为可信输入,不经
  allowlist/黑名单;标记 ``is_verifier``——唯一计入成功判定的工具,
  结果经 ToolOutput.verification_passed 元数据进入循环;
- 两者共同:子进程 env 最小化(API key 不可见)、进程组超时击杀、输出上限;
- run_tests 每次执行落 ``test_run`` 事件(完整 stdout/stderr;模型上下文
  用截断版),emit 回调由装配方注入,默认不发。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from mini_agent.sandbox import (
    DEFAULT_COMMAND_ALLOWLIST,
    CommandRejected,
    ProcessResult,
    check_allowlist,
    check_blacklist,
    normalize_command,
    run_subprocess,
)
from mini_agent.tools.base import Tool, ToolError, ToolOutput
from mini_agent.tools.descriptions import ZH_DESCRIPTIONS

DEFAULT_COMMAND_TIMEOUT_SECONDS = 120.0


def _format_process(result: ProcessResult) -> str:
    lines = [
        f"命令:{' '.join(result.command)}",
        f"退出码 {result.returncode if result.returncode is not None else '未知'}",
        f"耗时 {result.duration_ms} ms",
    ]
    if result.timed_out:
        lines.append("超时:命令被强制终止(进程组已击杀)")
    lines.append("--- stdout ---")
    lines.append(result.stdout or "(空)")
    lines.append("--- stderr ---")
    lines.append(result.stderr or "(空)")
    return "\n".join(lines)


def _normalize(command: str) -> list[str]:
    """shlex 归一化;解析失败转中文 ToolError。"""
    try:
        return normalize_command(command)
    except CommandRejected as exc:
        raise ToolError(str(exc)) from exc


def _prepare(command: str, *, allowlist) -> list[str]:
    """run_command 的完整检查链:归一化 → 黑名单 → allowlist(黑名单优先)。"""
    words = _normalize(command)
    try:
        check_blacklist(words)
        check_allowlist(words, allowlist)
    except CommandRejected as exc:
        raise ToolError(str(exc)) from exc
    return words


def _run(words: list[str], *, repo: Path, timeout: float) -> str:
    try:
        result = run_subprocess(words, cwd=repo, timeout=timeout)
    except FileNotFoundError as exc:
        raise ToolError(f"命令不存在或不可执行:{' '.join(words)}({exc})") from exc
    except PermissionError as exc:
        raise ToolError(f"命令不可执行(权限不足):{' '.join(words)}({exc})") from exc
    return _format_process(result)


class RunCommandTool:
    """围栏内的通用命令执行:黑名单 + allowlist 双重检查。"""

    name = "run_command"
    description = ZH_DESCRIPTIONS["run_command"]
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令(含参数,如 'python3 scripts/run.py')"}
        },
        "required": ["command"],
    }

    def __init__(
        self,
        repo: Path,
        *,
        timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        allowlist=DEFAULT_COMMAND_ALLOWLIST,
    ) -> None:
        self._repo = Path(repo)
        self._timeout = timeout
        self._allowlist = allowlist

    def execute(self, args: dict) -> str:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ToolError("缺少字符串参数 command")
        words = _prepare(command, allowlist=self._allowlist)
        return _run(words, repo=self._repo, timeout=self._timeout)


class RunTestsTool:
    """执行用户预配置的测试命令(可信输入,不经 allowlist/黑名单)。

    结果元数据(循环只读元数据):
    - 退出码 0 且未超时 → verification_passed=True;
    - 退出码非 0 或超时被击杀 → verification_passed=False(超时也是失败);
    - 未配置 --test-cmd → is_error + unverified_reason="no_test_command";
    - 命令不存在/不可执行 → is_error + unverified_reason="test_command_not_found"。
    """

    name = "run_tests"
    description = ZH_DESCRIPTIONS["run_tests"]
    parameters: ClassVar[dict] = {"type": "object", "properties": {}, "required": []}
    is_verifier = True

    def __init__(
        self,
        test_cmd: str | None,
        repo: Path,
        *,
        timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        emit: Callable[..., None] | None = None,
    ) -> None:
        self._test_cmd = test_cmd
        self._repo = Path(repo)
        self._timeout = timeout
        self._emit = emit

    def execute(self, args: dict) -> ToolOutput:
        if not self._test_cmd:
            return ToolOutput(
                content=(
                    "未配置测试命令:请用户通过 --test-cmd 提供(如 'pytest -q'),"
                    "或改用 run_command 运行可用命令"
                ),
                is_error=True,
                unverified_reason="no_test_command",
            )
        words = _normalize(self._test_cmd)  # 可信输入:只归一化,不经 allowlist/黑名单
        try:
            result = run_subprocess(words, cwd=self._repo, timeout=self._timeout)
        except (FileNotFoundError, PermissionError) as exc:
            # 测试命令不存在/不可执行是配置级错误,不是"测试失败"
            return ToolOutput(
                content=f"测试命令不存在或不可执行:{' '.join(words)}({exc})",
                is_error=True,
                unverified_reason="test_command_not_found",
            )
        if self._emit is not None:
            self._emit(
                "test_run",
                command=" ".join(result.command),
                returncode=result.returncode,
                stdout=result.stdout,  # 完整(仅受子进程输出上限约束),模型上下文用截断版
                stderr=result.stderr,
                timed_out=result.timed_out,
                duration_ms=result.duration_ms,
            )
        return ToolOutput(
            content=_format_process(result),
            verification_passed=(result.returncode == 0 and not result.timed_out),
        )


def make_exec_tools(
    repo: Path,
    *,
    test_cmd: str | None = None,
    timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    emit: Callable[..., None] | None = None,
) -> list[Tool]:
    """构造命令执行类工具;emit 用于落 test_run 事件(由装配方注入)。"""
    return [
        RunCommandTool(repo, timeout=timeout),
        RunTestsTool(test_cmd, repo, timeout=timeout, emit=emit),
    ]
