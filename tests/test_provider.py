"""OpenAICompatProvider 行为测试:请求参数、流式消费、重试分类与致命错误。

全部通过注入伪客户端完成,无任何真实网络调用。
"""

import openai
import pytest
from helpers import (
    make_chunk,
    make_connection_error,
    make_status_error,
    make_stub_client,
)

from mini_agent.providers import (
    OpenAICompatProvider,
    ProviderRequestError,
    ProviderRetryExhausted,
    ResponseTruncatedError,
    TextDelta,
)

TEXT_STREAM = [
    make_chunk(role="assistant"),
    make_chunk(content="你"),
    make_chunk(content="好"),
    make_chunk(content="!"),
    make_chunk(
        finish_reason="stop",
        usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    ),
]


def make_provider(client, **overrides) -> OpenAICompatProvider:
    params = {"model": "deepseek-flash", "api_key": "test-key", "base_url": "https://api.deepseek.com"}
    params.update(overrides)
    return OpenAICompatProvider(client=client, **params)


# ---------- 请求参数 ----------


def test_request_params_stream_usage_and_thinking_disabled_by_default():
    client = make_stub_client([TEXT_STREAM])
    provider = make_provider(client)
    provider.complete([{"role": "user", "content": "你好"}])
    (kwargs,) = client.completions.calls
    assert kwargs["stream"] is True
    assert kwargs["stream_options"] == {"include_usage": True}
    assert kwargs["temperature"] == 0.0
    assert kwargs["max_tokens"] == 32768  # DEFAULT_MAX_TOKENS(providers.py 单一事实源)
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}  # thinking 默认显式关闭


def test_thinking_flag_passes_enabled():
    client = make_stub_client([TEXT_STREAM])
    provider = make_provider(client, thinking=True)
    provider.complete([{"role": "user", "content": "你好"}])
    (kwargs,) = client.completions.calls
    assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}


@pytest.mark.parametrize("thinking", [False, True])
def test_thinking_extra_body_omitted_for_non_deepseek_models(thinking):
    # thinking extra_body 是 DeepSeek 私有扩展(M1 实测仅 DeepSeek 有效);
    # 其他 OpenAI 兼容端点可能对未知字段直接 400,故不得携带
    client = make_stub_client([TEXT_STREAM])
    provider = make_provider(client, model="glm-4.6", thinking=thinking)
    provider.complete([{"role": "user", "content": "你好"}])
    (kwargs,) = client.completions.calls
    assert "extra_body" not in kwargs


# ---------- 客户端构造 ----------


def test_default_client_has_explicit_request_timeout():
    # 不注入 client 时自建 OpenAI client 必须带显式 timeout:
    # 单次请求硬上限 600s,防流式调用无限挂死(整体 deadline 在循环轮次间检查)
    provider = OpenAICompatProvider(
        model="deepseek-chat", api_key="k", base_url="https://example.invalid"
    )
    assert provider._client.timeout == 600.0


# ---------- 流式消费与响应拼装 ----------


def test_complete_streams_events_and_returns_full_response():
    client = make_stub_client([TEXT_STREAM])
    provider = make_provider(client)
    seen = []
    resp = provider.complete(
        [{"role": "user", "content": "用一句话介绍你自己"}],
        on_event=seen.append,
    )
    # 归一化事件流:文本增量按序到达,末尾附带 done 与 usage
    assert seen[:3] == [TextDelta("你"), TextDelta("好"), TextDelta("!")]
    assert seen[-1].estimated is False and seen[-1].total_tokens == 10
    assert resp.text == "你好!"
    assert resp.finish_reason == "stop"
    assert resp.usage.total_tokens == 10
    assert resp.usage.estimated is False


def test_complete_raises_on_finish_reason_length():
    stream = [
        make_chunk(role="assistant"),
        make_chunk(content="半截"),
        make_chunk(finish_reason="length"),
    ]
    client = make_stub_client([stream])
    provider = make_provider(client)
    with pytest.raises(ResponseTruncatedError, match="max_tokens"):
        provider.complete([{"role": "user", "content": "任务"}])


def test_truncation_error_is_actionable_and_names_current_limit():
    # 失败 1 的教训:报错必须指向可执行的出口(--max-tokens)并带上当前值
    stream = [
        make_chunk(role="assistant"),
        make_chunk(content="半截"),
        make_chunk(finish_reason="length"),
    ]
    client = make_stub_client([stream])
    provider = make_provider(client, max_tokens=8192)
    with pytest.raises(ResponseTruncatedError, match="--max-tokens") as excinfo:
        provider.complete([{"role": "user", "content": "任务"}])
    assert "8192" in str(excinfo.value)


# ---------- 重试策略:只对 429/5xx/网络错误,指数退避 2 次 ----------


def make_retry_provider(client) -> OpenAICompatProvider:
    # 重试间隔注入为 0,测试不真实等待
    return make_provider(client, retry_delays=(0.0, 0.0))


def test_retry_on_429_then_success():
    client = make_stub_client(
        [
            make_status_error(openai.RateLimitError, 429),
            make_status_error(openai.RateLimitError, 429),
            TEXT_STREAM,
        ]
    )
    provider = make_retry_provider(client)
    resp = provider.complete([{"role": "user", "content": "你好"}])
    assert resp.text == "你好!"
    assert len(client.completions.calls) == 3  # 首次 + 2 次重试


def test_retry_on_500_then_success():
    client = make_stub_client(
        [make_status_error(openai.InternalServerError, 500), TEXT_STREAM]
    )
    provider = make_retry_provider(client)
    provider.complete([{"role": "user", "content": "你好"}])
    assert len(client.completions.calls) == 2


def test_retry_on_connection_error_then_success():
    client = make_stub_client([make_connection_error(), TEXT_STREAM])
    provider = make_retry_provider(client)
    provider.complete([{"role": "user", "content": "你好"}])
    assert len(client.completions.calls) == 2


def test_retry_exhausted_after_two_retries():
    client = make_stub_client([make_status_error(openai.RateLimitError, 429)] * 3)
    provider = make_retry_provider(client)
    with pytest.raises(ProviderRetryExhausted, match="重试"):
        provider.complete([{"role": "user", "content": "你好"}])
    assert len(client.completions.calls) == 3  # 不会无限重试


@pytest.mark.parametrize(
    "cls, code",
    [
        (openai.BadRequestError, 400),
        (openai.AuthenticationError, 401),
        # 402 无专用异常类,用通用 APIStatusError(分类依据是 status_code)
        (openai.APIStatusError, 402),
        (openai.UnprocessableEntityError, 422),
    ],
)
def test_request_errors_are_fatal_without_retry(cls, code):
    client = make_stub_client([make_status_error(cls, code)])
    provider = make_retry_provider(client)
    with pytest.raises(ProviderRequestError) as excinfo:
        provider.complete([{"role": "user", "content": "你好"}])
    assert len(client.completions.calls) == 1  # 请求类错误绝不重试
    assert str(code) in str(excinfo.value)


def test_401_hint_mentions_api_key_env():
    client = make_stub_client([make_status_error(openai.AuthenticationError, 401)])
    provider = make_retry_provider(client)
    with pytest.raises(ProviderRequestError, match="MINI_AGENT_API_KEY"):
        provider.complete([{"role": "user", "content": "你好"}])


def test_402_hint_mentions_balance():
    client = make_stub_client([make_status_error(openai.APIStatusError, 402)])
    provider = make_retry_provider(client)
    with pytest.raises(ProviderRequestError, match="余额"):
        provider.complete([{"role": "user", "content": "你好"}])
