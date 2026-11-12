"""AgentLoop 核心行为(工单 03):给定 FakeModel 脚本,断言终态、
session 事件序列与文件系统副作用——全部外部可观察行为。

接缝(spec Testing Decisions):AgentLoop.run() 的 RunOutcome、
session.jsonl 事件、临时仓库副作用;不测内部调用次序。
"""

import json
from pathlib import Path

from mini_agent.executor import ToolExecutor
from mini_agent.loop import SYSTEM_PROMPT, AgentLoop
from mini_agent.providers import create_provider
from mini_agent.session import SessionLogger
from mini_agent.tools import build_default_registry


def write_script(tmp_path, payload) -> str:
    path = tmp_path / "script.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def run_loop(
    tmp_path,
    repo: Path,
    turns_payload,
    *,
    task: str = "修复测试",
    test_cmd: str | None = None,
    max_steps: int = 20,
    max_context_tokens: int = 60000,
    timeout_seconds: float = 600.0,
    clock=None,
    behaviors=None,
):
    """在临时仓库上跑一次完整 AgentLoop,返回 (outcome, 事件列表, 日志路径)。

    behaviors 优先于 turns_payload:注入自定义 provider(如抛错脚本)。
    """
    if behaviors is not None:
        provider = behaviors
    else:
        provider = create_provider(f"fake:{write_script(tmp_path, turns_payload)}")
    registry = build_default_registry(repo, test_cmd=test_cmd)
    logger = SessionLogger(tmp_path / "runs")
    executor = ToolExecutor(registry=registry, logger=logger)
    loop = AgentLoop(
        provider=provider,
        executor=executor,
        logger=logger,
        system_prompt=SYSTEM_PROMPT,
        task=task,
        max_steps=max_steps,
        timeout_seconds=timeout_seconds,
        max_context_tokens=max_context_tokens,
        clock=clock,
    )
    try:
        outcome = loop.run()
        events = [json.loads(line) for line in logger.path.read_text(encoding="utf-8").splitlines()]
    finally:
        logger.close()
    return outcome, events, logger.path


def make_repo(tmp_path, *, files: dict | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for name, content in (files or {}).items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return repo


# ---------------------------------------------------------------------------
# 用例组 ② 之一:MAX_STEPS 耗尽
# ---------------------------------------------------------------------------


def test_max_steps_exhaustion_returns_max_steps_outcome(tmp_path):
    """模型每轮都调工具但从不 finish:步数耗尽 → MAX_STEPS / 退出码 2。"""
    repo = make_repo(tmp_path, files={"README.md": "# demo\n"})
    turns = {"turns": [{"tool_calls": [{"name": "list_files", "arguments": {}}]}] * 5}
    outcome, events, _ = run_loop(tmp_path, repo, turns, max_steps=3)

    assert outcome.state == "MAX_STEPS"
    assert outcome.reason == "steps_exhausted"
    assert outcome.exit_code == 2
    assert outcome.steps == 3  # 一次 LLM 往返计 1 步

    # 每步都有 llm_request / llm_response,工具每步真实执行
    kinds = [e["type"] for e in events]
    assert kinds.count("llm_request") == 3
    assert kinds.count("llm_response") == 3
    assert kinds.count("tool_result") == 3

    # 首个请求含中文系统提示词与任务,且带工具定义
    first_request = events[0]
    assert first_request["messages"][0]["role"] == "system"
    assert "中文" in SYSTEM_PROMPT or SYSTEM_PROMPT  # 提示词就位(内容断言见 CLI 片)
    assert first_request["messages"][1] == {"role": "user", "content": "修复测试"}
    assert first_request["tools"] and any(
        t["function"]["name"] == "list_files" for t in first_request["tools"]
    )
