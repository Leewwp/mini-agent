"""测试共用工具:构造与 OpenAI SDK 同形的鸭子类型对象与真实异常。"""

from types import SimpleNamespace

import httpx2
import openai


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
    """构造流式分片(与 openai.types.chat.ChatCompletionChunk 同形,鸭子类型)。"""
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


def make_status_error(cls: type[Exception], code: int) -> Exception:
    """构造真实的 openai 状态码异常(429/400/401/402/422/5xx)。"""
    request = httpx2.Request("POST", "https://api.deepseek.com/chat/completions")
    response = httpx2.Response(code, request=request)
    return cls(f"http {code}", response=response, body=None)


def make_connection_error() -> Exception:
    request = httpx2.Request("POST", "https://api.deepseek.com/chat/completions")
    return openai.APIConnectionError(message="connection refused", request=request)


class StubCompletions:
    """按脚本回放的 chat.completions.create:抛异常或返回分片迭代器。"""

    def __init__(self, behaviors: list):
        self.behaviors = list(behaviors)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.behaviors:
            raise AssertionError("stub 行为脚本耗尽")
        behavior = self.behaviors.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        return iter(behavior)


def make_stub_client(behaviors: list) -> StubCompletions:
    """返回一个可直接注入 provider 的伪 OpenAI 客户端。"""
    completions = StubCompletions(behaviors)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    client.completions = completions  # 便于断言调用参数
    return client
