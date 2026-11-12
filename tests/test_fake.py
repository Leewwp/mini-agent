"""FakeModel(--model fake:<script.json>)测试。

分片规律必须与工单 01 Comments 第 3 条 deepseek-flash 实测一致:
首片含 role;tool_call 首片含 index+id+name+空 arguments,后续片仅含
index+arguments;多调用交错索引;末片带 finish_reason 与 usage。
默认脚本不产生 reasoning_delta(CI 默认路径与演示路径一致)。
"""

import json

import pytest

from mini_agent.errors import ConfigError
from mini_agent.providers import (
    FakeProvider,
    FakeTurn,
    ReasoningDelta,
    ResponseTruncatedError,
    TextDelta,
    chunk_to_events,
    create_provider,
    fake_chunks_for_turn,
    load_fake_script,
)

ARGS_A = '{"path": "%s"}' % ("x" * 24)  # 足够长,保证参数跨多个分片
ARGS_B = '{"cmd": "%s"}' % ("y" * 24)


def turn(**kwargs) -> FakeTurn:
    """从脚本字段构造 FakeTurn(arguments 允许 dict)。"""
    tool_calls = [
        {"name": tc["name"], "arguments": tc["arguments"]} for tc in kwargs.get("tool_calls", [])
    ]
    return FakeTurn(
        text=kwargs.get("text", ""),
        reasoning=kwargs.get("reasoning", ""),
        tool_calls=tuple(tool_calls),
        finish_reason=kwargs.get("finish_reason"),
    )


def long_tool_call_turn() -> FakeTurn:
    return turn(
        text="我先看看仓库。",
        tool_calls=[
            {"name": "read_file", "arguments": json.loads(ARGS_A)},
            {"name": "run_tests", "arguments": json.loads(ARGS_B)},
        ],
    )


def chunks_and_events(fake_turn: FakeTurn, messages=None):
    chunks = fake_chunks_for_turn(
        fake_turn, messages=messages or [{"role": "user", "content": "任务"}]
    )
    events = [e for c in chunks for e in chunk_to_events(c)]
    return chunks, events


def flat_attrs(chunk):
    """取一个分片的首选 delta 属性,便于断言原始分片形状。"""
    choice = chunk.choices[0]
    return choice.delta, choice.finish_reason, chunk.usage


# ---------- 分片规律(实测形状) ----------


def test_fake_chunks_follow_measured_deepseek_shape():
    chunks, _ = chunks_and_events(long_tool_call_turn())
    # 首片:只有 role,没有内容
    first_delta, _, _ = flat_attrs(chunks[0])
    assert first_delta.role == "assistant"
    assert first_delta.content is None
    # 文本被切成多片
    text_chunks = [c for c in chunks if c.choices[0].delta.content]
    assert "".join(c.choices[0].delta.content for c in text_chunks) == "我先看看仓库。"
    assert len(text_chunks) >= 2
    # 末片:finish_reason=tool_calls 且带 usage
    _last_delta, finish, usage = flat_attrs(chunks[-1])
    assert finish == "tool_calls"
    assert usage.prompt_tokens > 0 and usage.completion_tokens > 0
    assert usage.total_tokens == usage.prompt_tokens + usage.completion_tokens


def test_tool_call_first_chunk_carries_id_name_then_only_argument_fragments():
    chunks, _ = chunks_and_events(long_tool_call_turn())
    tc_chunks = [
        c for c in chunks if c.choices[0].delta.tool_calls
    ]
    firsts = {}
    tail_indexes = []
    for c in tc_chunks:
        tc = c.choices[0].delta.tool_calls[0]
        if tc.id is not None:
            assert tc.function.arguments == ""  # 首片 arguments 为空
            firsts[tc.index] = (tc.id, tc.function.name)
        else:
            assert tc.function.name is None  # 后续片不再带 name/id
            tail_indexes.append(tc.index)
    assert set(firsts) == {0, 1}
    assert firsts[0][1] == "read_file" and firsts[1][1] == "run_tests"
    assert firsts[0][0] != firsts[1][0]  # 两个调用 id 不同
    # 交错:后续参数分片里 0 与 1 交替出现,而非一个调用的全发完再发另一个
    assert 0 in tail_indexes and 1 in tail_indexes
    first_switch = tail_indexes.index(1)
    assert 0 in tail_indexes[first_switch + 1:], "两个 tool call 的参数分片应交错到达"


# ---------- FakeProvider 行为 ----------


def test_fake_provider_streams_and_aggregates_tool_calls():
    provider = FakeProvider([long_tool_call_turn()])
    seen = []
    resp = provider.complete(
        [{"role": "user", "content": "修复并测试"}], on_event=seen.append
    )
    assert resp.text == "我先看看仓库。"
    assert resp.finish_reason == "tool_calls"
    assert [tc.name for tc in resp.tool_calls] == ["read_file", "run_tests"]
    assert resp.tool_calls[0].arguments == ARGS_A
    assert resp.tool_calls[1].arguments == ARGS_B
    assert any(isinstance(e, TextDelta) for e in seen)
    assert resp.usage.estimated is False


def test_fake_provider_pops_turns_in_order():
    script = [turn(text="第一轮"), turn(text="第二轮")]
    provider = FakeProvider(script)
    assert provider.complete([{"role": "user", "content": "t"}]).text == "第一轮"
    assert provider.complete([{"role": "user", "content": "t"}]).text == "第二轮"


def test_fake_reasoning_turn_keeps_reasoning_out_of_text():
    provider = FakeProvider([turn(reasoning="先想清楚", text="结论如下")])
    seen = []
    resp = provider.complete([{"role": "user", "content": "t"}], on_event=seen.append)
    reasoning_seen = "".join(e.content for e in seen if isinstance(e, ReasoningDelta))
    assert reasoning_seen == "先想清楚"
    assert resp.reasoning == "先想清楚"
    assert resp.text == "结论如下"  # reasoning 绝不混入 content


def test_fake_turn_with_finish_reason_length_raises():
    provider = FakeProvider([turn(text="被截断的半截回", finish_reason="length")])
    with pytest.raises(ResponseTruncatedError, match="max_tokens"):
        provider.complete([{"role": "user", "content": "t"}])


def test_fake_provider_raises_when_script_exhausted():
    provider = FakeProvider([])
    with pytest.raises(Exception, match="耗尽"):
        provider.complete([{"role": "user", "content": "t"}])


# ---------- fake: 入口(create_provider + 脚本装载) ----------


def write_script(tmp_path, payload) -> str:
    path = tmp_path / "script.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_create_provider_fake_entry_loads_script(tmp_path):
    path = write_script(tmp_path, {"turns": [{"text": "你好,我是 mini-agent。"}]})
    provider = create_provider(f"fake:{path}")
    assert isinstance(provider, FakeProvider)
    resp = provider.complete([{"role": "user", "content": "用一句话介绍你自己"}])
    assert resp.text == "你好,我是 mini-agent。"


def test_create_provider_fake_missing_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="不存在"):
        create_provider(f"fake:{tmp_path / 'nope.json'}")


def test_create_provider_fake_invalid_json_raises_config_error(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="合法 JSON"):
        create_provider(f"fake:{path}")


def test_create_provider_fake_missing_turns_raises_config_error(tmp_path):
    path = write_script(tmp_path, {"steps": []})
    with pytest.raises(ConfigError, match="turns"):
        create_provider(f"fake:{path}")


def test_load_fake_script_defaults_are_plain_text_turns(tmp_path):
    """默认脚本不产生 reasoning_delta(spec:CI 默认路径与演示路径一致)。"""
    path = write_script(tmp_path, {"turns": [{"text": "纯文本"}]})
    turns = load_fake_script(path)
    assert len(turns) == 1
    assert turns[0].reasoning == ""
    assert turns[0].tool_calls == ()
