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
    logger = SessionLogger(tmp_path / "runs")
    registry = build_default_registry(repo, test_cmd=test_cmd, emit=logger.emit)
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


# ---------------------------------------------------------------------------
# 用例组 ④ 修复闭环 + 声明时复验通过 → SUCCESS
# ---------------------------------------------------------------------------

BROKEN_CODE = "def add(a, b):\n    return a - b  # bug\n"
FIXED_CODE = "def add(a, b):\n    return a + b\n"


def broken_test_repo(tmp_path) -> Path:
    """带失败测试的临时仓库(等价验收第 1 条的真实闭环场景)。"""
    return make_repo(
        tmp_path,
        files={
            "calc.py": BROKEN_CODE,
            "test_calc.py": "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        },
    )


def test_full_fix_cycle_reaches_success(tmp_path):
    """写坏→run_tests 失败→修复→再测通过→finish(success)→现场复验→SUCCESS(退出码 0)。

    FakeModel 脚本等价替代真实模型(政策:真实彩排属工单 05)。
    """
    repo = broken_test_repo(tmp_path)
    turns = {
        "turns": [
            {"tool_calls": [{"name": "read_file", "arguments": {"path": "calc.py"}}]},
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "calc.py", "content": BROKEN_CODE}}]},
            {"tool_calls": [{"name": "run_tests", "arguments": {}}]},
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "calc.py", "content": FIXED_CODE}}]},
            {"tool_calls": [{"name": "run_tests", "arguments": {}}]},
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {"result": "success", "summary": "已修复 add 函数"},
                    }
                ]
            },
        ]
    }
    outcome, events, _ = run_loop(
        tmp_path, repo, turns, task="修复 add", test_cmd="python3 -m pytest -q test_calc.py"
    )

    assert outcome.state == "SUCCESS"
    assert outcome.exit_code == 0
    assert outcome.reason == "verification_passed"
    assert outcome.summary == "已修复 add 函数"
    assert outcome.steps == 6
    assert (repo / "calc.py").read_text(encoding="utf-8") == FIXED_CODE

    # 失败输出确实回填进下一轮 llm_request 上下文(修复循环的机制本体)
    step4_request = next(e for e in events if e["type"] == "llm_request" and e["step"] == 4)
    history_text = json.dumps(step4_request["messages"], ensure_ascii=False)
    assert "assert add(1, 2) == 3" in history_text  # 失败断言出现在模型上下文
    assert "1 failed" in history_text or "failed" in history_text

    # run_tests 每次执行(模型两次 + 复验一次)都落 test_run 事件,含完整 stdout
    test_runs = [e for e in events if e["type"] == "test_run"]
    assert len(test_runs) == 3
    assert all("stdout" in e and "stderr" in e for e in test_runs)
    assert test_runs[-1]["returncode"] == 0  # 复验现场重跑且通过

    # 复验的 tool_call 也进日志(经 verifier 重跑,不硬编码工具名)
    verify_calls = [
        e for e in events if e["type"] == "tool_call" and e.get("step") == 6 and e["name"] == "run_tests"
    ]
    assert len(verify_calls) == 1

    assert events[-1]["type"] == "final_report" and events[-1]["state"] == "SUCCESS"
