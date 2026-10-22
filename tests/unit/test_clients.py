"""Check the request each client sends and how it reads the response, without a network."""

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx

from eval.seam.clients import AnthropicClient, DeepSeekClient, Message, OpenAIClient

MESSAGES: list[Message] = [
    {"role": "user", "content": "Explain tides."},
    {"role": "assistant", "content": "Tides are caused by"},
]


def capture(
    response: dict[str, Any], status: int = 200
) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=response)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


def body(request: httpx.Request) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(request.content)
    return data


def run(make: Callable[[httpx.AsyncClient], Any], response: dict[str, Any], **kwargs: Any) -> Any:
    http, seen = capture(response)
    result = asyncio.run(make(http).complete(MESSAGES, 300, **kwargs))
    return result, seen


def test_anthropic_request_and_usage() -> None:
    response = {
        "content": [{"type": "text", "text": " the moon."}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 40, "cache_read_input_tokens": 10, "output_tokens": 5},
    }
    result, seen = run(
        lambda http: AnthropicClient(http, "key", "claude-haiku-5-5", "claude"), response
    )
    sent = body(seen[0])
    assert str(seen[0].url) == "https://api.anthropic.com/v1/messages"
    assert seen[0].headers["x-api-key"] == "key"
    assert sent["thinking"] == {"type": "disabled"}
    assert "temperature" not in sent
    assert result.text == " the moon."
    assert (result.input_tokens, result.cached_input_tokens, result.output_tokens) == (50, 10, 5)


def test_openai_request_and_usage() -> None:
    response = {
        "choices": [{"message": {"content": " the moon."}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 50,
            "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 10},
        },
    }
    result, seen = run(lambda http: OpenAIClient(http, "key", "gpt-6-luna", "gpt"), response)
    sent = body(seen[0])
    assert str(seen[0].url) == "https://api.openai.com/v1/chat/completions"
    assert sent["max_completion_tokens"] == 300
    assert sent["reasoning_effort"] == "none"
    assert (result.input_tokens, result.cached_input_tokens, result.output_tokens) == (50, 10, 5)


DEEPSEEK_RESPONSE = {
    "choices": [{"message": {"content": " the moon."}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 50, "completion_tokens": 5, "prompt_cache_hit_tokens": 20},
}


def test_deepseek_prefix_uses_beta_endpoint() -> None:
    result, seen = run(
        lambda http: DeepSeekClient(http, "key", "deepseek-flash", "deepseek"),
        DEEPSEEK_RESPONSE,
        prefix=True,
    )
    sent = body(seen[0])
    assert str(seen[0].url) == "https://api.deepseek.com/beta/chat/completions"
    assert sent["messages"][-1] == {
        "role": "assistant",
        "content": "Tides are caused by",
        "prefix": True,
    }
    assert sent["thinking"] == {"type": "disabled"}
    assert sent["max_tokens"] == 300
    assert result.cached_input_tokens == 20


def test_deepseek_without_prefix_uses_normal_endpoint() -> None:
    _, seen = run(
        lambda http: DeepSeekClient(http, "key", "deepseek-flash", "deepseek"), DEEPSEEK_RESPONSE
    )
    assert str(seen[0].url) == "https://api.deepseek.com/chat/completions"
    assert "prefix" not in body(seen[0])["messages"][-1]
