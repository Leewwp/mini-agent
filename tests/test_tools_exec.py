"""命令执行工具(run_command/run_tests)与子进程骨架的外部行为测试。

覆盖工单 02 验收:allowlist、黑名单优先于 allowlist、env 最小化(API key
对子进程不可见)、超时按进程组击杀(毫秒级等待)、输出上限;run_tests 执行
用户显式提供的可信命令(不经 allowlist)。
"""

import sys
from pathlib import Path

import pytest

from mini_agent.tools.base import ToolError
from mini_agent.tools.exec import RunCommandTool, RunTestsTool

PY = sys.executable  # 与调用环境完全一致的解释器绝对路径
PY_NAME = Path(PY).name


def make_run_command(tmp_path, **kwargs):
    defaults = {"timeout": 10.0, "allowlist": {PY_NAME}}
    defaults.update(kwargs)
    return RunCommandTool(repo=tmp_path, **defaults)


def test_run_command_executes_allowlisted_command(tmp_path):
    tool = make_run_command(tmp_path)
    out = tool.execute({"command": f'"{PY}" -c "print(\'pong\')"'})
    assert "pong" in out and "退出码 0" in out


def test_run_command_default_allowlist_accepts_python3(tmp_path):
    # 默认 allowlist 走 PATH 上的 python3(uv run 环境保证存在)
    tool = RunCommandTool(repo=tmp_path, timeout=10.0)
    out = tool.execute({"command": "python3 -c 'print(1)'"})
    assert "退出码 0" in out


def test_run_command_rejects_non_allowlisted_command(tmp_path):
    tool = make_run_command(tmp_path)
    with pytest.raises(ToolError, match="allowlist"):
        tool.execute({"command": "echo hi"})


def test_run_command_blacklist_takes_precedence_over_allowlist(tmp_path):
    tool = make_run_command(tmp_path, allowlist={"rm", "sudo", "echo"})
    with pytest.raises(ToolError, match="危险命令"):
        tool.execute({"command": "rm -rf x"})
    with pytest.raises(ToolError, match="危险命令"):
        tool.execute({"command": "sudo echo hi"})


def test_run_command_subprocess_env_excludes_mini_agent_vars(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_AGENT_API_KEY", "fake-key-must-not-leak")
    monkeypatch.setenv("MINI_AGENT_MODEL", "fake-model")
    tool = make_run_command(tmp_path)
    probe = (
        "import os; print(os.environ.get('MINI_AGENT_API_KEY'));"
        "print(os.environ.get('MINI_AGENT_MODEL'))"
    )
    out = tool.execute({"command": f'"{PY}" -c "{probe}"'})
    assert "fake-key-must-not-leak" not in out  # API key 对子进程不可见
    assert "None\nNone" in out


def test_run_command_timeout_kills_process_group_quickly(tmp_path):
    tool = make_run_command(tmp_path, timeout=0.3)
    out = tool.execute({"command": f'"{PY}" -c "import time; time.sleep(30)"'})
    assert "超时" in out
    assert "退出码" in out and "退出码 0" not in out


def test_run_command_caps_large_output(tmp_path):
    tool = make_run_command(tmp_path)
    out = tool.execute({"command": f'"{PY}" -c "print(\'a\' * 200000)"'})
    assert len(out) < 100_000  # 200KB 输出被截到头尾各 32KB
    assert "已截断" in out
    assert out.count("a") > 60_000  # 头尾内容仍在


def test_run_command_reports_nonzero_exit_and_stderr(tmp_path):
    tool = make_run_command(tmp_path)
    out = tool.execute(
        {"command": f'"{PY}" -c "import sys; print(\'boom\', file=sys.stderr); sys.exit(3)"'}
    )
    assert "退出码 3" in out
    assert "boom" in out


def test_run_command_missing_command_argument_is_error(tmp_path):
    with pytest.raises(ToolError, match="command"):
        make_run_command(tmp_path).execute({})


def test_run_command_allowlisted_but_missing_binary_is_tool_error(tmp_path):
    # 名字在 allowlist 里但机器上不存在 → 明确的"不存在"错误
    tool = make_run_command(tmp_path, allowlist={"definitely-not-a-binary-xyz"})
    with pytest.raises(ToolError, match="不存在"):
        tool.execute({"command": "definitely-not-a-binary-xyz -v"})


# ---------- run_tests:可信命令,不经 allowlist;verifier 元数据(工单 03) ----------


def test_run_tests_without_test_cmd_is_clear_unverified_error(tmp_path):
    tool = RunTestsTool(test_cmd=None, repo=tmp_path)
    out = tool.execute({})
    assert out.is_error is True
    assert "未配置测试命令" in out.content
    assert out.unverified_reason == "no_test_command"


def test_run_tests_runs_user_command_without_allowlist(tmp_path):
    # python/echo 都不在注入的 allowlist 里,证明 run_tests 视用户命令为可信输入
    tool = RunTestsTool(test_cmd=f'"{PY}" -c "print(42)"', repo=tmp_path, timeout=10.0)
    out = tool.execute({})
    assert "42" in out.content and "退出码 0" in out.content
    assert out.verification_passed is True
    assert out.is_error is False


def test_run_tests_echo_is_allowed(tmp_path):
    tool = RunTestsTool(test_cmd="echo trusted-ok", repo=tmp_path, timeout=10.0)
    out = tool.execute({})
    assert "trusted-ok" in out.content


def test_run_tests_missing_binary_is_unverified_not_tests_failed(tmp_path):
    """命令不存在是配置级错误(unverified_reason),不是"测试失败"。"""
    tool = RunTestsTool(test_cmd="definitely-not-a-binary-xyz", repo=tmp_path)
    out = tool.execute({})
    assert out.is_error is True
    assert "不存在" in out.content
    assert out.unverified_reason == "test_command_not_found"
    assert out.verification_passed is None


def test_run_tests_reports_failing_tests(tmp_path):
    tool = RunTestsTool(
        test_cmd=f'"{PY}" -c "import sys; print(\'assert failed\'); sys.exit(1)"',
        repo=tmp_path,
        timeout=10.0,
    )
    out = tool.execute({})
    assert "退出码 1" in out.content and "assert failed" in out.content
    assert out.verification_passed is False
