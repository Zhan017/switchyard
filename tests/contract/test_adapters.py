"""Adapters against documented provider streams: what each sends, and what it makes of
the reply. A local aiohttp server plays the provider, writing each fixture in small pieces
so events split across network reads are exercised too."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from switchyard.config import ModelConfig, ProviderConfig
from switchyard.core.types import (
    ChatRequest,
    Finish,
    Message,
    ResponseFormat,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallArgs,
    ToolCallStart,
    ToolSpec,
    Usage,
    UsageReport,
)
from switchyard.providers.anthropic import AnthropicAdapter
from switchyard.providers.base import Adapter
from switchyard.providers.errors import ErrorKind, ProviderError
from switchyard.providers.openai import DeepSeekAdapter, OpenAIAdapter

STREAMS = Path(__file__).parent.parent / "fixtures" / "streams"
WEATHER = ToolSpec(
    "get_weather",
    "Weather for a city",
    {"type": "object", "properties": {"city": {"type": "string"}}},
)
PIECE = 7  # bytes per write, so SSE events arrive split across reads


@asynccontextmanager
async def provider(
    body: bytes, status: int = 200, headers: dict[str, str] | None = None
) -> AsyncIterator[tuple[aiohttp.ClientSession, str, list[dict[str, Any]]]]:
    """A fake provider that answers every POST with ``body``. Yields a client session,
    the server's base URL, and the requests it received."""
    seen: list[dict[str, Any]] = []

    async def handler(request: web.Request) -> web.StreamResponse:
        seen.append(
            {"path": request.path, "headers": request.headers, "json": await request.json()}
        )
        response = web.StreamResponse(status=status, headers=headers or {})
        await response.prepare(request)
        for i in range(0, len(body), PIECE):
            await response.write(body[i : i + PIECE])
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_route("POST", "/{tail:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        async with aiohttp.ClientSession() as session:
            yield session, str(server.make_url("")).rstrip("/"), seen
    finally:
        await server.close()


def adapter(kind: str, http: aiohttp.ClientSession, base: str) -> Adapter:
    if kind == "anthropic":
        return AnthropicAdapter("anthropic", ProviderConfig(type="anthropic", base_url=base), http)
    if kind == "deepseek":
        return DeepSeekAdapter("deepseek", ProviderConfig(type="deepseek", base_url=base), http)
    return OpenAIAdapter("openai", ProviderConfig(type="openai", base_url=f"{base}/v1"), http)


def request(**kwargs: Any) -> ChatRequest:
    fields: dict[str, Any] = {
        "messages": (Message("system", "Be brief."), Message("user", "Hi")),
        "max_tokens": 100,
    }
    fields.update(kwargs)
    return ChatRequest(**fields)


def collect(
    kind: str, fixture: str, req: ChatRequest | None = None, **options: Any
) -> tuple[list[StreamEvent], dict[str, Any], ProviderError | None]:
    events: list[StreamEvent] = []
    error: ProviderError | None = None
    sent: dict[str, Any] = {}

    async def go() -> None:
        nonlocal error, sent
        async with provider((STREAMS / fixture).read_bytes()) as (http, base, seen):
            try:
                async for event in adapter(kind, http, base).stream(
                    "model-x", ModelConfig(**options), req or request()
                ):
                    events.append(event)
            except ProviderError as caught:
                error = caught
            if seen:
                sent = seen[0]["json"]

    asyncio.run(go())
    return events, sent, error


def text_of(events: list[StreamEvent]) -> str:
    return "".join(e.text for e in events if isinstance(e, TextDelta))


# OpenAI


def test_openai_text_stream() -> None:
    events, sent, error = collect("openai", "openai_text.sse")
    assert error is None
    assert text_of(events) == "Hello world"
    assert Finish("stop") in events
    assert UsageReport(Usage(12, 2, cached_input_tokens=4, cache_write_tokens=6)) in events
    assert sent["stream"] is True
    assert sent["stream_options"] == {"include_usage": True}
    assert sent["max_completion_tokens"] == 100
    assert sent["messages"][0] == {"role": "system", "content": "Be brief."}


def test_openai_parallel_tool_calls() -> None:
    events, _, error = collect("openai", "openai_tools.sse", request(tools=(WEATHER,)))
    assert error is None
    assert events[:4] == [
        ToolCallStart(0, "call_a", "get_weather"),
        ToolCallArgs(0, '{"city": '),
        ToolCallArgs(0, '"Astana"}'),
        ToolCallStart(1, "call_b", "get_time"),
    ]
    assert Finish("tool_calls") in events


def test_openai_error_inside_200_stream() -> None:
    events, _, error = collect("openai", "openai_error_event.sse")
    assert text_of(events) == "Partial"
    assert error is not None and error.kind == ErrorKind.SERVER_ERROR


def test_openai_stream_cut_before_finish() -> None:
    events, _, error = collect("openai", "openai_cut.sse")
    assert text_of(events) == "Partial answer"
    assert error is not None and error.kind == ErrorKind.CONNECTION


def test_openai_request_translation() -> None:
    req = request(
        temperature=0.2,
        stop=("END",),
        tools=(WEATHER,),
        tool_choice="get_weather",
        parallel_tool_calls=False,
        response_format=ResponseFormat("json_schema", {"type": "object"}, "out", True),
        messages=(
            Message("user", "Weather?"),
            Message("assistant", None, (ToolCall("call_1", "get_weather", '{"city":"Oslo"}'),)),
            Message("tool", "5C", tool_call_id="call_1"),
        ),
    )
    _, sent, _ = collect(
        "openai", "openai_text.sse", req, request_overrides={"reasoning_effort": "none"}
    )
    assert sent["temperature"] == 0.2
    assert sent["stop"] == ["END"]
    assert sent["tool_choice"] == {"type": "function", "function": {"name": "get_weather"}}
    assert sent["parallel_tool_calls"] is False
    assert sent["response_format"]["json_schema"] == {
        "name": "out",
        "schema": {"type": "object"},
        "strict": True,
    }
    assert sent["reasoning_effort"] == "none"
    assert sent["messages"][1]["tool_calls"][0]["function"]["name"] == "get_weather"
    assert sent["messages"][2] == {"role": "tool", "content": "5C", "tool_call_id": "call_1"}


def test_sampling_params_dropped_when_unsupported() -> None:
    _, sent, _ = collect(
        "openai", "openai_text.sse", request(temperature=0.7, top_p=0.9), sampling_params=False
    )
    assert "temperature" not in sent and "top_p" not in sent


# DeepSeek


def test_deepseek_keep_alive_comments_and_cache_usage() -> None:
    events, sent, error = collect(
        "deepseek", "deepseek_keepalive.sse", request_overrides={"thinking": {"type": "disabled"}}
    )
    assert error is None
    assert text_of(events) == "Hi"
    assert UsageReport(Usage(30, 1, cached_input_tokens=10)) in events
    assert sent["max_tokens"] == 100 and "max_completion_tokens" not in sent
    assert sent["thinking"] == {"type": "disabled"}


# Anthropic


def test_anthropic_text_stream_ignores_pings() -> None:
    events, sent, error = collect("anthropic", "anthropic_text.sse")
    assert error is None
    assert text_of(events) == "Hello world"
    assert Finish("stop") in events
    # Input counts cached tokens; output comes from the final, cumulative message_delta.
    assert UsageReport(Usage(30, 15, cached_input_tokens=5)) in events
    assert sent["system"] == "Be brief."
    assert sent["messages"] == [{"role": "user", "content": [{"type": "text", "text": "Hi"}]}]


def test_anthropic_tool_use_becomes_tool_call_zero() -> None:
    events, _, error = collect("anthropic", "anthropic_tools.sse", request(tools=(WEATHER,)))
    assert error is None
    assert text_of(events) == "Checking."
    assert ToolCallStart(0, "toolu_01", "get_weather") in events
    args = "".join(e.delta for e in events if isinstance(e, ToolCallArgs))
    assert json.loads(args) == {"city": "Astana"}
    assert Finish("tool_calls") in events


def test_anthropic_error_event_inside_200_stream() -> None:
    events, _, error = collect("anthropic", "anthropic_overloaded.sse")
    assert text_of(events) == "Partial"
    assert error is not None and error.kind == ErrorKind.SERVER_ERROR


def test_anthropic_refusal_is_content_filter() -> None:
    events, _, _ = collect("anthropic", "anthropic_refusal.sse")
    assert Finish("content_filter") in events


def test_anthropic_request_translation() -> None:
    req = request(
        temperature=0.5,
        stop=("END",),
        tools=(WEATHER,),
        tool_choice="required",
        parallel_tool_calls=False,
        response_format=ResponseFormat("json_schema", {"type": "object"}),
        user="u-1",
        messages=(
            Message("system", "Rules."),
            Message("user", "Weather in Oslo and Astana?"),
            Message(
                "assistant",
                "Checking.",
                (
                    ToolCall("toolu_1", "get_weather", '{"city":"Oslo"}'),
                    ToolCall("toolu_2", "get_weather", '{"city":"Astana"}'),
                ),
            ),
            Message("tool", "5C", tool_call_id="toolu_1"),
            Message("tool", "-3C", tool_call_id="toolu_2"),
        ),
    )
    _, sent, _ = collect(
        "anthropic",
        "anthropic_text.sse",
        req,
        sampling_params=False,
        request_overrides={"thinking": {"type": "disabled"}},
    )
    assert "temperature" not in sent
    assert sent["stop_sequences"] == ["END"]
    assert sent["tools"][0]["input_schema"] == WEATHER.parameters
    assert sent["tool_choice"] == {"type": "any", "disable_parallel_tool_use": True}
    assert sent["output_config"] == {
        "format": {"type": "json_schema", "schema": {"type": "object"}}
    }
    assert sent["metadata"] == {"user_id": "u-1"}
    assert sent["thinking"] == {"type": "disabled"}
    assert sent["system"] == "Rules."
    assistant, results = sent["messages"][1], sent["messages"][2]
    assert [b["type"] for b in assistant["content"]] == ["text", "tool_use", "tool_use"]
    assert assistant["content"][1]["input"] == {"city": "Oslo"}
    # Both tool results go back in one user turn.
    assert results["role"] == "user"
    assert [b["tool_use_id"] for b in results["content"]] == ["toolu_1", "toolu_2"]


def test_anthropic_rejects_json_without_schema() -> None:
    _, _, error = collect(
        "anthropic", "anthropic_text.sse", request(response_format=ResponseFormat("json_object"))
    )
    assert error is not None and error.kind == ErrorKind.CLIENT_ERROR


# HTTP errors before the stream


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (429, ErrorKind.RATE_LIMITED),
        (500, ErrorKind.SERVER_ERROR),
        (529, ErrorKind.SERVER_ERROR),
        (401, ErrorKind.AUTH),
        (400, ErrorKind.CLIENT_ERROR),
    ],
)
def test_http_errors_are_classified(status: int, kind: ErrorKind) -> None:
    body = b'{"error": {"type": "x", "message": "nope"}}'

    async def go() -> ProviderError:
        async with provider(body, status, {"retry-after": "7"}) as (http, base, _):
            with pytest.raises(ProviderError) as caught:
                async for _ in adapter("anthropic", http, base).stream(
                    "m", ModelConfig(), request()
                ):
                    pass
            return caught.value

    error = asyncio.run(go())
    assert error.kind == kind
    assert error.status == status
    assert error.message == "nope"
    assert error.retry_after == 7.0


def test_connection_refused_is_a_connection_error() -> None:
    async def go() -> ProviderError:
        async with aiohttp.ClientSession() as http:
            with pytest.raises(ProviderError) as caught:
                stream = adapter("openai", http, "http://127.0.0.1:9").stream(
                    "m", ModelConfig(), request()
                )
                async for _ in stream:
                    pass
            return caught.value

    assert asyncio.run(go()).kind == ErrorKind.CONNECTION


def test_requests_carry_auth_and_version_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SY_TEST_KEY", "secret")

    async def go() -> Any:
        body = (STREAMS / "anthropic_text.sse").read_bytes()
        async with provider(body) as (http, base, seen):
            config = ProviderConfig(type="anthropic", base_url=base, api_key_env="SY_TEST_KEY")
            async for _ in AnthropicAdapter("a", config, http).stream(
                "m", ModelConfig(), request()
            ):
                pass
            return seen[0]

    seen = asyncio.run(go())
    assert seen["path"] == "/v1/messages"
    assert seen["headers"]["x-api-key"] == "secret"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"


def test_non_streaming_completions() -> None:
    openai_body = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "f", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    }
    anthropic_body = {
        "content": [
            {"type": "text", "text": "Sure."},
            {"type": "tool_use", "id": "toolu_1", "name": "f", "input": {"a": 1}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 5, "output_tokens": 3},
    }

    async def go(kind: str, payload: dict[str, Any]) -> Any:
        async with provider(json.dumps(payload).encode()) as (http, base, _):
            return await adapter(kind, http, base).complete("m", ModelConfig(), request())

    openai = asyncio.run(go("openai", openai_body))
    assert openai.tool_calls == (ToolCall("call_1", "f", "{}"),)
    assert openai.finish_reason == "tool_calls"
    anthropic = asyncio.run(go("anthropic", anthropic_body))
    assert anthropic.text == "Sure."
    assert json.loads(anthropic.tool_calls[0].arguments) == {"a": 1}
    assert anthropic.usage == Usage(5, 3)


def test_anthropic_null_usage_fields_keep_known_counts() -> None:
    start = {
        "type": "message_start",
        "message": {
            "usage": {"input_tokens": 10000, "cache_read_input_tokens": 0, "output_tokens": 1}
        },
    }
    delta = {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn"},
        "usage": {"input_tokens": None, "cache_read_input_tokens": None, "output_tokens": 7},
    }
    lines = [
        ("message_start", start),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hi"},
            },
        ),
        ("message_delta", delta),
        ("message_stop", {"type": "message_stop"}),
    ]
    body = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in lines).encode()

    async def go() -> list[StreamEvent]:
        async with provider(body) as (http, base, _):
            return [
                e
                async for e in adapter("anthropic", http, base).stream(
                    "m", ModelConfig(), request()
                )
            ]

    events = asyncio.run(go())
    assert UsageReport(Usage(10000, 7)) in events


def test_anthropic_tool_history_without_tools_declares_them() -> None:
    req = request(
        messages=(
            Message("user", "Weather?"),
            Message("assistant", None, (ToolCall("toolu_1", "get_weather", '{"city":"Oslo"}'),)),
            Message("tool", "5C", tool_call_id="toolu_1"),
            Message("user", "Summarise that."),
        )
    )
    _, sent, _ = collect("anthropic", "anthropic_text.sse", req)
    assert sent["tools"] == [{"name": "get_weather", "input_schema": {"type": "object"}}]
    assert sent["tool_choice"] == {"type": "none"}


# Native continuation (DeepSeek beta prefix completion)


def test_deepseek_native_continuation_uses_the_beta_url_and_prefix() -> None:
    from switchyard.streaming.continuation import continuation_request

    req = continuation_request("native", request(), "The sky is")
    events: list[StreamEvent] = []

    async def go() -> list[dict[str, Any]]:
        body = (STREAMS / "recorded" / "deepseek_text.sse").read_bytes()
        async with provider(body) as (http, base, seen):
            async for event in adapter("deepseek", http, base).stream(
                "deepseek-flash", ModelConfig(continuation="native"), req
            ):
                events.append(event)
            return seen

    seen = asyncio.run(go())
    assert seen[0]["path"] == "/beta/chat/completions"
    last = seen[0]["json"]["messages"][-1]
    assert last == {"role": "assistant", "content": "The sky is", "prefix": True}
    assert Finish("stop") in events


def test_prefix_is_not_sent_to_openai() -> None:
    sent = collect("openai", "openai_text.sse")[1]
    assert all("prefix" not in m for m in sent["messages"])
