"""命令检查的外部行为测试:shlex 归一化、黑名单、run_command allowlist、env 最小化。

黑名单与 allowlist 在本票落地供 03 复用;这里直接测纯函数行为。
"""

import pytest

from mini_agent.sandbox import (
    CommandRejected,
    check_allowlist,
    check_blacklist,
    minimal_env,
    normalize_command,
)

DEFAULT_ALLOWLIST = {"python", "python3", "pytest", "node", "npm", "npx", "go", "cargo", "make"}


# ---------- shlex 归一化 ----------


def test_normalize_splits_and_strips_quotes():
    assert normalize_command("  python3   -c 'print(1)' ") == ["python3", "-c", "print(1)"]
    assert normalize_command('echo "a b"') == ["echo", "a b"]


def test_normalize_empty_or_blank_command_is_rejected():
    with pytest.raises(CommandRejected, match="空命令"):
        normalize_command("   ")


# ---------- 黑名单(先归一化再匹配) ----------


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf /tmp/x",
        "rm -fr x",
        "rm -r -f x",
        "rm --recursive --force x",
        "rm -rfx",  # 组合短参
        "sudo ls",
        "sudo apt install x",
        "git push origin main",
        "git push",
        ":(){ :|:& };:",
    ],
)
def test_blacklist_rejects_obviously_dangerous_commands(cmd):
    words = normalize_command(cmd)
    with pytest.raises(CommandRejected):
        check_blacklist(words)


@pytest.mark.parametrize(
    "cmd",
    [
        "rm notes.txt",  # 普通删除不拦(防误伤不防对抗)
        "rm -r build",  # 递归但不强制
        "git status",
        "git commit -m x",
        "python3 -c 'print(1)'",
        "make test",
    ],
)
def test_blacklist_allows_ordinary_commands(cmd):
    check_blacklist(normalize_command(cmd))


def test_blacklist_matches_executable_basename():
    # sudo 不在首位同样危险(管道后接 sudo);按任意位置匹配
    with pytest.raises(CommandRejected):
        check_blacklist(normalize_command("echo x | sudo tee /etc/hosts"))


def test_blacklist_message_is_chinese():
    words = normalize_command("rm -rf /")
    with pytest.raises(CommandRejected, match="危险命令"):
        check_blacklist(words)


# ---------- run_command allowlist ----------


def test_allowlist_accepts_default_tool_commands():
    for cmd in ("python -V", "python3 -c 'print(1)'", "pytest -q", "node -e 1", "npm test", "make"):
        check_allowlist(normalize_command(cmd), DEFAULT_ALLOWLIST)


def test_allowlist_accepts_absolute_path_of_allowed_command():
    check_allowlist(normalize_command("/usr/bin/python3 -V"), DEFAULT_ALLOWLIST)


def test_allowlist_rejects_commands_not_in_list():
    for cmd in ("curl http://x", "sh -c 'rm x'", "echo hi", "git status"):
        with pytest.raises(CommandRejected, match="allowlist"):
            check_allowlist(normalize_command(cmd), DEFAULT_ALLOWLIST)


def test_allowlist_is_configurable():
    check_allowlist(normalize_command("echo hi"), {"echo"})
    with pytest.raises(CommandRejected):
        check_allowlist(normalize_command("python -V"), {"echo"})


# ---------- 子进程 env 最小化 ----------


def test_minimal_env_strips_mini_agent_vars():
    base = {
        "PATH": "/usr/bin",
        "MINI_AGENT_API_KEY": "k-test-only",
        "MINI_AGENT_MODEL": "fake",
        "HOME": "/Users/dev",
    }
    env = minimal_env(base)
    assert env == {"PATH": "/usr/bin", "HOME": "/Users/dev"}


def test_minimal_env_copies_and_does_not_mutate_base():
    base = {"MINI_AGENT_API_KEY": "k-test-only", "KEEP": "1"}
    minimal_env(base)
    assert base == {"MINI_AGENT_API_KEY": "k-test-only", "KEEP": "1"}
