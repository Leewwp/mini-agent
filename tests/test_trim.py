"""历史裁剪(用例组 ⑥):按「assistant(tool_calls) + 全部对应 tool 结果」
整组替换为一条摘要文本;绝不单独删 tool 结果(否则下一轮请求 400)。

纯函数 trim_history 直接单测 + 循环集成断言每次请求的配对合法性。
"""

import json
from pathlib import Path

from mini_agent.loop import SYSTEM_PROMPT, AgentLoop, trim_history
from mini_agent.providers import create_provider
from mini_agent.session import SessionLogger
from mini_agent.executor import ToolExecutor
from mini_agent.tools import build_default_registry


def assistant_with_calls(*names) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": f"call_{i}_{name}", "type": "function",
             "function": {"name": name, "arguments": "{}"}}
            for i, name in enumerate(names)
        ],
    }


def tool_reply(call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def user(text: str) -> dict:
    return {"role": "user", "content": text}


def assert_pairs_valid(messages) -> None:
    """OpenAI 协议合法性:每个 assistant tool_call 之后都有对应 tool 结果,无孤儿。"""
    pending: set[str] = set()
    for m in messages:
        if m.get("tool_calls"):
            for tc in m["tool_calls"]:
                pending.add(tc["id"])
        elif m.get("role") == "tool":
            assert m["tool_call_id"] in pending, f"孤儿 tool 结果:{m['tool_call_id']}"
            pending.discard(m["tool_call_id"])
    assert not pending, f"tool_call 无结果:{pending}"


def group(call_ids_contents, tools=("read_file",)) -> list[dict]:
    """构造一组:assistant(tool_calls) + 每个调用的 tool 结果。"""
    names = [tools[i % len(tools)] for i in range(len(call_ids_contents))]
    msgs = [assistant_with_calls(*[f"{n}{i}" for i, n in enumerate(names)])]
    msgs += [
        tool_reply(tc["id"], content)
        for tc, content in zip(msgs[0]["tool_calls"], call_ids_contents)
    ]
    return msgs


# ---------------------------------------------------------------------------
# 纯函数:trim_history
# ---------------------------------------------------------------------------


def base_messages(*groups_) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": "sys"}, user("任务")]
    for g in groups_:
        msgs += g
    return msgs


def test_trim_replaces_whole_groups_with_single_summary_text():
    msgs = base_messages(group(["x" * 400]), group(["y" * 400]), group(["z" * 10]))
    trimmed, removed = trim_history(msgs, max_prompt_tokens=200, keep_last_groups=1)
    # 最老一组被裁,替换为一条纯文本摘要(不是删除 tool 结果)
    summaries = [m for m in trimmed if "历史" in (m.get("content") or "")]
    assert len(summaries) == 1 and summaries[0]["role"] == "assistant"
    assert "工具" in summaries[0]["content"]
    assert_pairs_valid(trimmed)
    # 被裁消息原样返回(供 context_trim 事件还原)
    assert removed == msgs[2:4]
    # system 与任务 user 消息永不裁
    assert trimmed[0] == msgs[0] and trimmed[1] == msgs[1]


def test_trim_never_breaks_pairing_even_when_hungry():
    """阈值再小也不裁穿:最近 keep_last_groups 组保底保留。"""
    msgs = base_messages(group(["x" * 100]), group(["y" * 100]), group(["z" * 100]))
    trimmed, _ = trim_history(msgs, max_prompt_tokens=1, keep_last_groups=2)
    assert_pairs_valid(trimmed)
    # 贪婪到阈值=1 也只裁到「只剩最近 keep_last_groups 组」为止,最后一组原样
    assert msgs[-2:] == trimmed[-2:]


def test_trim_noop_when_below_threshold():
    msgs = base_messages(group(["ok"]), group(["fine"]))
    trimmed, removed = trim_history(msgs, max_prompt_tokens=10_000)
    assert trimmed == msgs and removed == []


# ---------------------------------------------------------------------------
# 循环集成:触发(上一响应 usage.prompt_tokens)、事件可还原
# ---------------------------------------------------------------------------


def test_loop_trims_history_and_keeps_requests_valid(tmp_path):
    """多轮大 write_file 超阈值 → 裁剪后每次请求的 tool_call/tool_result 配对仍合法。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    big = "x" * 3000
    turns = {
        "turns": [
            {"tool_calls": [{"name": "write_file", "arguments": {"path": f"big{i}.txt", "content": big}}]}
            for i in range(5)
        ]
        + [{"tool_calls": [{"name": "finish", "arguments": {"result": "failed", "summary": "结束"}}]}]
    }
    script = tmp_path / "script.json"
    script.write_text(json.dumps(turns, ensure_ascii=False), encoding="utf-8")
    provider = create_provider(f"fake:{script}")

    logger = SessionLogger(tmp_path / "runs")
    registry = build_default_registry(repo, emit=logger.emit)
    executor = ToolExecutor(registry=registry, logger=logger)
    loop = AgentLoop(
        provider=provider,
        executor=executor,
        logger=logger,
        system_prompt=SYSTEM_PROMPT,
        task="写大文件",
        max_steps=10,
        max_context_tokens=4000,  # FakeProvider 的 prompt_tokens 即消息内容字符量
    )
    try:
        outcome = loop.run()
        events = [json.loads(line) for line in logger.path.read_text(encoding="utf-8").splitlines()]
    finally:
        logger.close()

    assert outcome.state == "TESTS_FAILED"  # 裁剪不影响正常终态
    trims = [e for e in events if e["type"] == "context_trim"]
    assert len(trims) >= 1
    # 被裁消息完整入事件:调用参数与结果都可从日志还原原始上下文
    removed_flat = [m for e in trims for m in e["removed_messages"]]
    assert any(m.get("role") == "tool" and "已新建文件" in m["content"] for m in removed_flat)
    assert any(
        "xxx" in json.dumps(m.get("tool_calls") or [], ensure_ascii=False)
        for m in removed_flat
    )

    # 裁剪后每个请求的消息历史配对合法,且包含摘要文本
    trimmed_requests = [
        e for e in events
        if e["type"] == "llm_request"
        and any("历史" in (m.get("content") or "") and m.get("role") == "assistant" for m in e["messages"])
    ]
    assert trimmed_requests, "应存在含摘要的请求"
    for e in trimmed_requests:
        assert_pairs_valid(e["messages"])
        assert e["messages"][0]["role"] == "system"  # 提示词不裁
        assert any(m.get("role") == "tool" for m in e["messages"])  # 最近交互保留
