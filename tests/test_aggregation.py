"""Provider 归一化事件流纯函数的单元测试。

分片规律以工单 01 Comments 第 3 条的 deepseek-flash 实测为准:
首片 delta 含 role;每个 tool_call 首片含 index+id+name+空 arguments,
后续片仅含 index+arguments 增量;末片带 finish_reason 与 usage。
"""

from types import SimpleNamespace

import pytest

from mini_agent.providers import (
    Done,
    ReasoningDelta,
    ResponseTruncatedError,
    TextDelta,
    ToolCallDelta,
    UsageInfo,
    aggregate_tool_call_deltas,
    assemble_response,
    chunk_to_events,
    estimate_tokens,
)


def make_chunk(
    *,
    role=None,
    content=None,
    reasoning=None,
    tool_calls=None,
    finish_reason=None,
    usage=None,
    empty_choices=False,
):
    """构造与 OpenAI SDK 流式分片同形的鸭子类型对象。"""
    if tool_calls is not None:
        tool_calls = [
            SimpleNamespace(
                index=tc["index"],
                id=tc.get("id"),
                function=SimpleNamespace(
                    name=tc.get("name"),
                    arguments=tc.get("arguments", ""),
                ),
            )
            for tc in tool_calls
        ]
    delta = SimpleNamespace(
        role=role,
        content=content,
        tool_calls=tool_calls,
        reasoning_content=reasoning,  # DeepSeek 扩展字段,SDK 模型 extra=allow
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    usage_obj = None
    if usage is not None:
        usage_obj = SimpleNamespace(
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
        )
    return SimpleNamespace(
        choices=[] if empty_choices else [choice],
        usage=usage_obj,
    )


# ---------- aggregate_tool_call_deltas ----------


def test_aggregate_keeps_id_from_first_fragment_and_concatenates_arguments():
    deltas = [
        ToolCallDelta(index=0, id="call_0", name="read_file", arguments_fragment='{"pa'),
        ToolCallDelta(index=0, arguments_fragment='th": "a.py"}'),
    ]
    calls = aggregate_tool_call_deltas(deltas)
    assert len(calls) == 1
    call = calls[0]
    assert call.index == 0
    assert call.id == "call_0"
    assert call.name == "read_file"
    assert call.arguments == '{"path": "a.py"}'


def test_aggregate_multiple_tool_calls_with_interleaved_indexes():
    deltas = [
        ToolCallDelta(index=0, id="call_0", name="read_file", arguments_fragment='{"pa'),
        ToolCallDelta(index=1, id="call_1", name="run_tests", arguments_fragment='{"x"'),
        ToolCallDelta(index=0, arguments_fragment='th": "a.py"}'),
        ToolCallDelta(index=1, arguments_fragment=': 1}'),
    ]
    calls = aggregate_tool_call_deltas(deltas)
    assert [c.index for c in calls] == [0, 1]
    assert calls[0].id == "call_0" and calls[0].name == "read_file"
    assert calls[0].arguments == '{"path": "a.py"}'
    assert calls[1].id == "call_1" and calls[1].name == "run_tests"
    assert calls[1].arguments == '{"x": 1}'


def test_aggregate_preserves_half_json_arguments_verbatim():
    """流中断导致 arguments 半截 JSON:聚合只做拼接,原样保留,不解析不崩溃。"""
    deltas = [
        ToolCallDelta(index=0, id="call_0", name="write_file", arguments_fragment='{"path": "x", "conte'),
        ToolCallDelta(index=0, arguments_fragment='nt": "abc'),
    ]
    calls = aggregate_tool_call_deltas(deltas)
    assert calls[0].arguments == '{"path": "x", "content": "abc'


# ---------- chunk_to_events ----------


def test_chunk_to_events_follows_measured_deepseek_chunk_shape():
    chunks = [
        make_chunk(role="assistant"),
        make_chunk(content="我来看"),
        make_chunk(tool_calls=[{"index": 0, "id": "call_0", "name": "read_file", "arguments": ""}]),
        make_chunk(tool_calls=[{"index": 0, "arguments": '{"path": "a.py"}'}]),
        make_chunk(
            finish_reason="tool_calls",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        ),
    ]
    events = [e for chunk in chunks for e in chunk_to_events(chunk)]
    assert events == [
        TextDelta("我来看"),
        ToolCallDelta(index=0, id="call_0", name="read_file", arguments_fragment=""),
        ToolCallDelta(index=0, arguments_fragment='{"path": "a.py"}'),
        Done("tool_calls"),
        UsageInfo(prompt_tokens=10, completion_tokens=5, total_tokens=15, estimated=False),
    ]


def test_chunk_to_events_maps_reasoning_and_usage_only_chunk():
    events = []
    events += chunk_to_events(make_chunk(reasoning="先想一下"))
    events += chunk_to_events(
        make_chunk(
            usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            empty_choices=True,
        )
    )
    assert events == [
        ReasoningDelta("先想一下"),
        UsageInfo(prompt_tokens=1, completion_tokens=2, total_tokens=3, estimated=False),
    ]


# ---------- assemble_response ----------


def test_assemble_response_combines_text_and_tool_calls():
    """实测结论 #2:assistant content 可与 tool_calls 并存,必须同时保留。"""
    events = [
        TextDelta("我先读"),
        TextDelta("这个文件。"),
        ToolCallDelta(index=0, id="call_0", name="read_file", arguments_fragment='{"pa'),
        ToolCallDelta(index=0, arguments_fragment='th": "a.py"}'),
        UsageInfo(prompt_tokens=8, completion_tokens=4, total_tokens=12, estimated=False),
        Done("tool_calls"),
    ]
    resp = assemble_response(events)
    assert resp.text == "我先读这个文件。"
    assert resp.reasoning == ""
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].arguments == '{"path": "a.py"}'
    assert resp.usage == UsageInfo(prompt_tokens=8, completion_tokens=4, total_tokens=12, estimated=False)
    assert resp.finish_reason == "tool_calls"


def test_assemble_response_raises_on_finish_reason_length():
    """实测结论 #5:finish_reason=length 必须明确报错,禁止使用半截输出。"""
    events = [TextDelta("半截的回"), Done("length")]
    with pytest.raises(ResponseTruncatedError, match="max_tokens"):
        assemble_response(events)


def test_assemble_response_estimates_usage_when_provider_omits_it():
    events = [TextDelta("你好"), ReasoningDelta("想"), Done("stop")]
    resp = assemble_response(events)
    assert resp.usage.estimated is True
    assert resp.usage.completion_tokens == estimate_tokens("你好") + estimate_tokens("想")
    assert resp.usage.prompt_tokens is None
    assert resp.usage.total_tokens == resp.usage.completion_tokens


# ---------- estimate_tokens ----------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("你好世界", 4),  # CJK 每字约 1 token
        ("abcd", 1),  # ASCII 约 4 字符 1 token
        ("你好abcd", 3),  # 混合
        ("", 0),
    ],
)
def test_estimate_tokens_known_literals(text, expected):
    assert estimate_tokens(text) == expected
