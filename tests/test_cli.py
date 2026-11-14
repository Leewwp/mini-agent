"""CLI 曳光弹的端到端行为测试(FakeModel 注入,无任何网络)。

覆盖:参数全集与默认值、环境变量解析与覆盖、流式上屏、session.jsonl
首事件 session_meta、缺 API key → CONFIG_ERROR(5)、--repo 不存在 → 5、
模型截断 → ERROR(3)。
"""

import json

import pytest

from mini_agent.cli import build_arg_parser, main, resolve_config
from mini_agent.errors import ConfigError

REPLY = "你好,我是 mini-agent,一个本地命令行编码助手。"


def write_script(tmp_path, payload) -> str:
    path = tmp_path / "script.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def clean_env(monkeypatch):
    for name in ("MINI_AGENT_API_KEY", "MINI_AGENT_BASE_URL", "MINI_AGENT_MODEL"):
        monkeypatch.delenv(name, raising=False)


# ---------- 参数全集与默认值 ----------


def test_argparse_full_param_set_and_defaults():
    args = build_arg_parser().parse_args(["--repo", "r", "--task", "t"])
    assert args.max_steps == 20
    assert args.test_cmd is None
    assert args.model is None
    assert args.timeout == 600
    assert args.max_context_tokens == 60000
    assert args.thinking is False
    assert args.max_tokens == 32768  # DEFAULT_MAX_TOKENS(providers.py 单一事实源)
    # 全参数形式可解析
    full = build_arg_parser().parse_args(
        [
            "--repo", "r", "--task", "t", "--max-steps", "5",
            "--test-cmd", "pytest -q", "--model", "deepseek-v4-pro",
            "--timeout", "120", "--max-context-tokens", "30000", "--thinking",
            "--max-tokens", "65536",
        ]
    )
    assert full.max_steps == 5 and full.test_cmd == "pytest -q"
    assert full.model == "deepseek-v4-pro" and full.timeout == 120
    assert full.max_context_tokens == 30000 and full.thinking is True
    assert full.max_tokens == 65536


def test_help_copy_is_chinese():
    help_text = build_arg_parser().format_help()
    assert "目标仓库" in help_text and "任务" in help_text


# ---------- 配置解析:环境变量 + flag 覆盖 ----------


def make_args(tmp_path, **overrides):
    base = {"repo": str(tmp_path), "task": "t"}
    base.update(overrides)
    argv: list[str] = []
    for key, value in base.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]
    return build_arg_parser().parse_args(argv)


def test_model_resolution_default_env_and_flag(monkeypatch, tmp_path):
    clean_env(monkeypatch)
    # 默认模型取 deepseek-flash;但真实模型缺 API key 直接 CONFIG_ERROR(验收项)
    with pytest.raises(ConfigError, match="MINI_AGENT_API_KEY"):
        resolve_config(make_args(tmp_path), env={})

    cfg = resolve_config(
        make_args(tmp_path),
        env={
            "MINI_AGENT_MODEL": "glm-4.7",
            "MINI_AGENT_BASE_URL": "https://open.bigmodel.cn/api/paas/v4",
            "MINI_AGENT_API_KEY": "k",
        },
    )
    assert cfg.model == "glm-4.7"
    assert cfg.base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert cfg.api_key == "k"

    cfg = resolve_config(
        make_args(tmp_path, model="deepseek-v4-pro"),
        env={"MINI_AGENT_MODEL": "glm-4.7", "MINI_AGENT_API_KEY": "k"},
    )
    assert cfg.model == "deepseek-v4-pro"  # CLI flag 覆盖环境变量


def test_max_tokens_default_flag_and_validation(monkeypatch, tmp_path):
    clean_env(monkeypatch)
    env = {"MINI_AGENT_API_KEY": "k"}
    cfg = resolve_config(make_args(tmp_path), env=env)
    assert cfg.max_tokens == 32768  # 默认输出上限(DeepSeek 官方上限 384K,取宽裕默认)
    cfg = resolve_config(make_args(tmp_path, max_tokens=65536), env=env)
    assert cfg.max_tokens == 65536
    with pytest.raises(ConfigError, match="--max-tokens"):
        resolve_config(make_args(tmp_path, max_tokens=0), env=env)


def test_main_records_max_tokens_in_session_meta(monkeypatch, tmp_path):
    run_fake_main(
        monkeypatch,
        tmp_path,
        {
            "turns": [
                {
                    "text": "ok",
                    "tool_calls": [{"name": "finish", "arguments": {"result": "success", "summary": "ok"}}],
                }
            ]
        },
        extra_args=["--max-tokens", "4096"],
    )
    runs_dir = tmp_path / "mini-agent-runs"
    (session_dir,) = [d for d in runs_dir.iterdir() if d.is_dir()]
    events = [
        json.loads(line)
        for line in (session_dir / "session.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events[0]["max_tokens"] == 4096  # 配置真实进入会话元数据


# ---------- 端到端(FakeModel) ----------


def run_fake_main(monkeypatch, tmp_path, script_payload, extra_args=(), repo=None):
    clean_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    repo = repo or (tmp_path / "repo")
    repo.mkdir(exist_ok=True)
    script = write_script(tmp_path, script_payload)
    return main(
        ["--repo", str(repo), "--task", "用一句话介绍你自己", "--model", f"fake:{script}", *extra_args]
    )


def test_main_fake_streams_reply_and_writes_session_log(monkeypatch, tmp_path, capsys):
    """流式上屏 + 会话日志(循环语义:正文与 finish 并存一轮,复验通过)。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    code = run_fake_main(
        monkeypatch,
        tmp_path,
        {
            "turns": [
                {
                    "text": REPLY,
                    "tool_calls": [
                        {"name": "finish", "arguments": {"result": "success", "summary": "自我介绍"}}
                    ],
                }
            ]
        },
        extra_args=["--test-cmd", "python3 -m pytest -q test_ok.py"],
        repo=repo,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert REPLY in out  # 流式上屏(逐字写 stdout)

    runs_dir = tmp_path / "mini-agent-runs"
    (session_dir,) = [d for d in runs_dir.iterdir() if d.is_dir()]
    events = [
        json.loads(line)
        for line in (session_dir / "session.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events[0]["type"] == "session_meta"
    assert events[0]["model"].startswith("fake:")
    assert events[0]["temperature"] == 0.0
    assert "base_url" in events[0] and "ts" in events[0]
    assert events[0]["thinking"] is False

    llm_request = next(e for e in events if e["type"] == "llm_request")
    assert any("用一句话介绍你自己" in str(m.get("content", "")) for m in llm_request["messages"])
    log_text = (session_dir / "session.jsonl").read_text(encoding="utf-8")
    assert "api_key" not in log_text and "Authorization" not in log_text

    llm_response = next(e for e in events if e["type"] == "llm_response")
    assert llm_response["text"] == REPLY
    assert llm_response["usage"]["total_tokens"] > 0
    assert llm_response["duration_ms"] >= 0

    assert "终态:SUCCESS" in out and "会话日志" in out  # 终报行 + 日志位置(中文)


def test_main_records_thinking_flag_in_session_meta(monkeypatch, tmp_path, capsys):
    run_fake_main(
        monkeypatch,
        tmp_path,
        {
            "turns": [
                {
                    "text": REPLY,
                    "tool_calls": [{"name": "finish", "arguments": {"result": "success", "summary": "ok"}}],
                }
            ]
        },
        extra_args=["--thinking"],
    )
    runs_dir = tmp_path / "mini-agent-runs"
    (session_dir,) = [d for d in runs_dir.iterdir() if d.is_dir()]
    events = [
        json.loads(line)
        for line in (session_dir / "session.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events[0]["thinking"] is True
    assert "终态:UNVERIFIED" in capsys.readouterr().out  # 无 --test-cmd 时如实报告


def test_main_missing_api_key_returns_config_error(monkeypatch, tmp_path, capsys):
    clean_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    code = main(["--repo", str(repo), "--task", "你好"])
    assert code == 5  # CONFIG_ERROR
    err = capsys.readouterr().err
    assert "MINI_AGENT_API_KEY" in err
    assert "配置错误" in err


def test_main_missing_repo_returns_config_error(monkeypatch, tmp_path, capsys):
    clean_env(monkeypatch)
    code = main(["--repo", str(tmp_path / "nope"), "--task", "你好"])
    assert code == 5
    err = capsys.readouterr().err
    assert "不存在" in err


def test_main_truncated_response_returns_error(monkeypatch, tmp_path, capsys):
    code = run_fake_main(
        monkeypatch, tmp_path,
        {"turns": [{"text": "半截回复", "finish_reason": "length"}]},
    )
    assert code == 3  # ERROR
    captured = capsys.readouterr()
    assert "终态:ERROR" in captured.out and "max_tokens" in captured.out  # detail 进终报
    # 调用前写入的 llm_request 仍在日志里
    runs_dir = tmp_path / "mini-agent-runs"
    (session_dir,) = [d for d in runs_dir.iterdir() if d.is_dir()]
    log_text = (session_dir / "session.jsonl").read_text(encoding="utf-8")
    assert "llm_request" in log_text


# ---------- 完整循环接通(工单 03:替换曳光弹) ----------

# 注意:坏/好版本字节数刻意不同——Python 的 pyc 缓存按 (秒级 mtime, size) 判有效期,
# 同字节数 + 同秒内改回会命中旧缓存,让子进程 pytest 读到坏版本(语言级陷阱)。
BROKEN = "def add(a, b):\n    return a - b  # bug\n"
FIXED = "def add(a, b):\n    return a + b\n"


def fix_cycle_turns() -> dict:
    return {
        "turns": [
            {"tool_calls": [{"name": "read_file", "arguments": {"path": "calc.py"}}]},
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "calc.py", "content": BROKEN}}]},
            {"tool_calls": [{"name": "run_tests", "arguments": {}}]},
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "calc.py", "content": FIXED}}]},
            {"tool_calls": [{"name": "run_tests", "arguments": {}}]},
            {
                "tool_calls": [
                    {"name": "finish", "arguments": {"result": "success", "summary": "修复 add"}}
                ]
            },
        ]
    }


def broken_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "calc.py").write_text(BROKEN, encoding="utf-8")
    (repo / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8"
    )
    return repo


def test_cli_full_fix_cycle_success_with_report_and_activity_lines(monkeypatch, tmp_path, capsys):
    """端到端:修复 → 复验 → SUCCESS(0);终报行含终态/原因/步数/token,每步有工具活动行。"""
    repo = broken_repo(tmp_path)
    code = run_fake_main(
        monkeypatch, tmp_path, fix_cycle_turns(),
        extra_args=["--test-cmd", "python3 -m pytest -q test_calc.py", "--task", "修复 add"],
        repo=repo,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "终态:SUCCESS(原因:verification_passed),退出码 0" in out
    assert "步数:6" in out and "token" in out
    assert "[步骤 1] 调用工具 read_file" in out  # 每步工具活动行
    assert "[步骤 3] 调用工具 run_tests" in out
    assert (repo / "calc.py").read_text(encoding="utf-8") == FIXED


def test_cli_exit_codes_across_terminal_states(monkeypatch, tmp_path):
    """退出码映射:UNVERIFIED=6、MAX_STEPS=2、TESTS_FAILED=1、ERROR=3(TIMEOUT=4 属注入时钟层)。"""
    # UNVERIFIED:无 --test-cmd 时 finish(success)
    code = run_fake_main(
        monkeypatch, tmp_path,
        {"turns": [{"tool_calls": [{"name": "finish", "arguments": {"result": "success", "summary": "ok"}}]}]},
    )
    assert code == 6

    # MAX_STEPS:步数耗尽
    code = run_fake_main(
        monkeypatch, tmp_path,
        {"turns": [{"tool_calls": [{"name": "list_files", "arguments": {}}]}] * 3},
        extra_args=["--max-steps", "2"],
    )
    assert code == 2

    # TESTS_FAILED:finish(failed)
    code = run_fake_main(
        monkeypatch, tmp_path,
        {"turns": [{"tool_calls": [{"name": "finish", "arguments": {"result": "failed", "summary": "放弃"}}]}]},
        extra_args=["--test-cmd", "python3 -m pytest -q"],
    )
    assert code == 1

    # ERROR:测试命令不存在(test_command_not_found)
    repo = broken_repo(tmp_path)
    code = run_fake_main(
        monkeypatch, tmp_path,
        {"turns": [{"tool_calls": [{"name": "finish", "arguments": {"result": "success", "summary": "ok"}}]}]},
        extra_args=["--test-cmd", "no-such-test-runner", "--task", "修复"],
        repo=repo,
    )
    assert code == 3
