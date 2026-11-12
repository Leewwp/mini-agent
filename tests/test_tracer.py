"""端到端曳光弹(工单 02 验收第 1 条):FakeModel 下达任务 → 模型经工具调用
真实创建/修改临时仓库里的文件,全程落 tool_call/tool_result 事件。

完整 Agent 循环(历史回填、多轮、终态)属工单 03;这里验证的接缝是:
Provider 产出的 tool_calls → 执行器 → 真实文件系统副作用 → 事件日志。
"""

import json
from pathlib import Path

from mini_agent.executor import ToolExecutor
from mini_agent.providers import create_provider
from mini_agent.session import SessionLogger
from mini_agent.tools import build_default_registry


def write_script(tmp_path, payload) -> str:
    path = tmp_path / "script.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def run_script_turns(tmp_path, script_payload, repo: Path, *, turns: int = 1):
    """装载 FakeModel,把每轮 tool_calls 交给执行器,返回全部结果与日志路径。"""
    provider = create_provider(f"fake:{write_script(tmp_path, script_payload)}")
    registry = build_default_registry(repo)
    with SessionLogger(tmp_path / "runs") as logger:
        executor = ToolExecutor(registry=registry, logger=logger)
        messages = [{"role": "user", "content": "在仓库里创建 notes.txt 并写入 hello"}]
        results = []
        for step in range(1, turns + 1):
            resp = provider.complete(messages, tools=registry.definitions())
            for call in resp.tool_calls:
                results.append(executor.run(call, step=step))
        log_path = logger.path
    return results, log_path


def test_model_tool_call_really_creates_notes_txt(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")

    results, log_path = run_script_turns(
        tmp_path,
        {"turns": [{"tool_calls": [{"name": "write_file", "arguments": {"path": "notes.txt", "content": "hello"}}]}]},
        repo,
    )
    assert (repo / "notes.txt").read_text(encoding="utf-8") == "hello"  # 真实落盘
    assert len(results) == 1 and results[0].is_error is False

    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    kinds = [e["type"] for e in events]
    assert kinds == ["tool_call", "tool_result"]
    assert events[0]["step"] == 1 and events[0]["name"] == "write_file"
    assert events[1]["is_error"] is False
    assert "notes.txt" in events[1]["content"]


def test_model_reads_then_overwrites_existing_file_in_one_turn(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

    results, log_path = run_script_turns(
        tmp_path,
        {
            "turns": [
                {
                    "tool_calls": [
                        {"name": "read_file", "arguments": {"path": "app.py"}},
                        {"name": "write_file", "arguments": {"path": "app.py", "content": "VALUE = 2\n"}},
                    ]
                }
            ]
        },
        repo,
    )
    assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert [r.is_error for r in results] == [False, False]  # 先读后写放行
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [e["type"] for e in events] == ["tool_call", "tool_result"] * 2


def test_model_blind_overwrite_is_refused_but_execution_continues(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

    results, _ = run_script_turns(
        tmp_path,
        {
            "turns": [
                {
                    "tool_calls": [
                        {"name": "write_file", "arguments": {"path": "app.py", "content": "clobbered"}},  # 未读先写
                        {"name": "write_file", "arguments": {"path": "fresh.txt", "content": "ok"}},  # 新文件放行
                    ]
                }
            ]
        },
        repo,
    )
    assert results[0].is_error is True and "先读" in results[0].content
    assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"  # 原文件未被动
    assert results[1].is_error is False  # 前一个失败不中断后续工具调用
    assert (repo / "fresh.txt").read_text(encoding="utf-8") == "ok"


def test_default_registry_exposes_six_tools_with_chinese_descriptions(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    registry = build_default_registry(repo)
    assert registry.names() == [
        "list_files", "read_file", "write_file", "search", "run_command", "run_tests",
    ]
    for definition in registry.definitions():
        description = definition["function"]["description"]
        assert any("\u4e00" <= ch <= "\u9fff" for ch in description), description
