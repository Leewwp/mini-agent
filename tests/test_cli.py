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
    # 全参数形式可解析
    full = build_arg_parser().parse_args(
        [
            "--repo", "r", "--task", "t", "--max-steps", "5",
            "--test-cmd", "pytest -q", "--model", "deepseek-v4-pro",
            "--timeout", "120", "--max-context-tokens", "30000", "--thinking",
        ]
    )
    assert full.max_steps == 5 and full.test_cmd == "pytest -q"
    assert full.model == "deepseek-v4-pro" and full.timeout == 120
    assert full.max_context_tokens == 30000 and full.thinking is True


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
    code = run_fake_main(monkeypatch, tmp_path, {"turns": [{"text": REPLY}]})
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

    assert "会话日志" in out  # 结束时报告日志位置(中文)


def test_main_records_thinking_flag_in_session_meta(monkeypatch, tmp_path, capsys):
    run_fake_main(monkeypatch, tmp_path, {"turns": [{"text": REPLY}]}, extra_args=["--thinking"])
    runs_dir = tmp_path / "mini-agent-runs"
    (session_dir,) = [d for d in runs_dir.iterdir() if d.is_dir()]
    events = [
        json.loads(line)
        for line in (session_dir / "session.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events[0]["thinking"] is True


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
    err = capsys.readouterr().err
    assert "max_tokens" in err
    # 调用前写入的 llm_request 仍在日志里
    runs_dir = tmp_path / "mini-agent-runs"
    (session_dir,) = [d for d in runs_dir.iterdir() if d.is_dir()]
    log_text = (session_dir / "session.jsonl").read_text(encoding="utf-8")
    assert "llm_request" in log_text
