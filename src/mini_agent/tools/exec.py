"""命令执行工具:run_command(allowlist 约束)/ run_tests(可信命令)。

本票只落执行骨架供 03 复用;完整闭环语义(SUCCESS 复验、终态映射、
test_command_not_found → ERROR 等)属工单 03,这里不越界。

权限分层(spec「沙箱」节):
- run_command:shlex 归一化 → 黑名单 → allowlist,黑名单优先;
- run_tests:执行用户显式提供的 ``--test-cmd``,视为可信输入,不经
  allowlist/黑名单;
- 两者共同:子进程 env 最小化(API key 不可见)、进程组超时击杀、输出上限。
"""

from __future__ import annotations

from pathlib import Path

from mini_agent.sandbox import (
    DEFAULT_COMMAND_ALLOWLIST,
    CommandRejected,
    ProcessResult,
    check_allowlist,
    check_blacklist,
    normalize_command,
    run_subprocess,
)
from mini_agent.tools.base import Tool, ToolError

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
    description = (
        "在目标仓库目录执行一条命令。只允许解释器/构建/测试类命令"
        "(python/python3/pytest/node/npm/npx/go/cargo/make 等 allowlist),"
        "危险命令(rm -rf、sudo、git push、fork 炸弹)一律拒绝;"
        "默认 120 秒超时,超时会击杀整个进程组;输出超限会截断。"
        "注意:本命令的结果不计入成功判定,验证请使用 run_tests。"
    )
    parameters = {
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
    """执行用户预配置的测试命令(可信输入,不经 allowlist/黑名单)。"""

    name = "run_tests"
    description = (
        "执行用户通过 --test-cmd 预配置的测试命令,返回退出码与 stdout/stderr。"
        "成功判定只以本工具的现场结果为准(run_command 的结果不计入验证);"
        "未配置测试命令时会返回明确的工具错误。"
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(
        self,
        test_cmd: str | None,
        repo: Path,
        *,
        timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    ) -> None:
        self._test_cmd = test_cmd
        self._repo = Path(repo)
        self._timeout = timeout

    def execute(self, args: dict) -> str:
        if not self._test_cmd:
            raise ToolError(
                "未配置测试命令:请用户通过 --test-cmd 提供(如 'pytest -q'),"
                "或改用 run_command 运行可用命令"
            )
        words = _normalize(self._test_cmd)  # 可信输入:只归一化,不经 allowlist/黑名单
        return _run(words, repo=self._repo, timeout=self._timeout)


def make_exec_tools(
    repo: Path, *, test_cmd: str | None = None, timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS
) -> list[Tool]:
    """构造命令执行类工具(run_tests 的完整闭环语义在工单 03 接入)。"""
    return [RunCommandTool(repo, timeout=timeout), RunTestsTool(test_cmd, repo, timeout=timeout)]
