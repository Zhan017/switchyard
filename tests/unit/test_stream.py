"""The stream watcher's time limits and the relay's output."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from switchyard.core.types import (
    ChatRequest,
    Finish,
    Message,
    RefusalDelta,
    ResponseFormat,
    StreamEvent,
    TextDelta,
    ToolCallArgs,
    ToolCallStart,
    Usage,
    UsageReport,
)
from switchyard.core.watch import StreamTiming, watch
from switchyard.providers.errors import ErrorKind, ProviderError
from switchyard.streaming.relay import Relay, StreamResult


async def scripted(steps: Sequence[float | StreamEvent | Exception]) -> AsyncIterator[StreamEvent]:
    """Yield events, sleep for floats, raise exceptions."""
    for step in steps:
        if isinstance(step, (int, float)):
            await asyncio.sleep(step)
        elif isinstance(step, Exception):
            raise step
        else:
            yield step


def run_watch(
    steps: Sequence[float | StreamEvent | Exception], deadline_s: float = 10.0
) -> tuple[list[StreamEvent], ProviderError | None]:
    async def go() -> tuple[list[StreamEvent], ProviderError | None]:
        out: list[StreamEvent] = []
        try:
            async for event in watch(
                scripted(steps),
                ttft_s=0.2,
                stall_s=0.1,
                deadline=time.monotonic() + deadline_s,
                timing=StreamTiming(),
            ):
                out.append(event)
        except ProviderError as error:
            return out, error
        return out, None

    return asyncio.run(go())


def test_watch_passes_a_healthy_stream() -> None:
    events, error = run_watch([0.05, TextDelta("a"), 0.05, TextDelta("b"), Finish("stop")])
    assert error is None and len(events) == 3


def test_watch_first_token_timeout() -> None:
    _, error = run_watch([0.5, TextDelta("late")])
    assert error is not None and error.kind == ErrorKind.TIMEOUT


def test_watch_stall_after_content() -> None:
    events, error = run_watch([TextDelta("a"), 0.3, TextDelta("late")])
    assert events == [TextDelta("a")]
    assert error is not None and error.kind == ErrorKind.STALL


def test_watch_deadline_wins_over_stall() -> None:
    _, error = run_watch(
        [TextDelta("a"), 0.08, TextDelta("b"), 0.08, TextDelta("c"), 0.08, TextDelta("d")],
        deadline_s=0.15,
    )
    assert error is not None and error.kind == ErrorKind.DEADLINE


# Relay


def relay_output(
    events: Sequence[StreamEvent | Exception], request: ChatRequest | None = None
) -> tuple[list[Any], StreamResult]:
    results: list[StreamResult] = []

    def finalize(result: StreamResult) -> dict[str, Any]:
        results.append(result)
        return {"cost_usd": 0}

    relay = Relay(
        response_id="chatcmpl-x",
        alias="mock",
        request=request or ChatRequest(messages=(Message("user", "hi"),), max_tokens=10),
        finalize=finalize,
    )

    async def go() -> list[Any]:
        out = []
        async for raw in relay.run(scripted(events)):
            text = raw.decode()
            assert text.startswith("data: ") and text.endswith("\n\n")
            payload = text[6:-2]
            out.append(payload if payload == "[DONE]" else json.loads(payload))
        return out

    chunks = asyncio.run(go())
    assert len(results) == 1, "finalize runs exactly once"
    return chunks, results[0]


def deltas(chunks: list[Any]) -> list[dict[str, Any]]:
    return [c["choices"][0]["delta"] for c in chunks if isinstance(c, dict) and c.get("choices")]


def test_relay_text_stream() -> None:
    chunks, result = relay_output(
        [TextDelta("Hel"), TextDelta("lo"), Finish("stop"), UsageReport(Usage(5, 2))]
    )
    assert {c["id"] for c in chunks if isinstance(c, dict)} == {"chatcmpl-x"}
    assert {c["model"] for c in chunks if isinstance(c, dict)} == {"mock"}
    assert deltas(chunks)[0] == {"role": "assistant", "content": ""}
    assert "".join(d.get("content", "") for d in deltas(chunks)) == "Hello"
    assert chunks[-3]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-2]["choices"] == [] and chunks[-2]["usage"]["total_tokens"] == 7
    assert chunks[-2]["switchyard"] == {"cost_usd": 0}
    assert chunks[-1] == "[DONE]"
    assert result.text == "Hello" and result.error is None


def test_relay_sends_each_tool_call_once_complete() -> None:
    chunks, result = relay_output(
        [
            ToolCallStart(0, "c0", "f"),
            ToolCallArgs(0, '{"a":'),
            ToolCallArgs(0, "1}"),
            ToolCallStart(1, "c1", "g"),
            ToolCallArgs(1, "{}"),
            Finish("tool_calls"),
        ]
    )
    calls = [d["tool_calls"] for d in deltas(chunks) if "tool_calls" in d]
    assert calls == [
        [
            {
                "index": 0,
                "id": "c0",
                "type": "function",
                "function": {"name": "f", "arguments": '{"a":1}'},
            }
        ],
        [
            {
                "index": 1,
                "id": "c1",
                "type": "function",
                "function": {"name": "g", "arguments": "{}"},
            }
        ],
    ]
    assert len(result.tool_calls) == 2


def test_relay_buffers_json_until_complete() -> None:
    json_request = ChatRequest(
        messages=(Message("user", "hi"),),
        max_tokens=10,
        response_format=ResponseFormat("json_object"),
    )
    chunks, _ = relay_output([TextDelta('{"a"'), TextDelta(": 1}"), Finish("stop")], json_request)
    contents = [d["content"] for d in deltas(chunks) if d.get("content")]
    assert contents == ['{"a": 1}']


def test_relay_rejects_invalid_json() -> None:
    json_request = ChatRequest(
        messages=(Message("user", "hi"),),
        max_tokens=10,
        response_format=ResponseFormat("json_object"),
    )
    chunks, result = relay_output([TextDelta('{"a": '), Finish("stop")], json_request)
    assert chunks[-2]["error"]["code"] == "no_healthy_provider"
    assert result.error is not None and result.error.kind == ErrorKind.INVALID_RESPONSE
    assert not any(d.get("content") for d in deltas(chunks))


def test_relay_error_mid_stream() -> None:
    chunks, result = relay_output(
        [TextDelta("Part"), ProviderError(ErrorKind.SERVER_ERROR, "boom")]
    )
    assert chunks[-2] == {
        "error": {"message": "boom", "type": "switchyard_error", "code": "no_healthy_provider"}
    }
    assert chunks[-1] == "[DONE]"
    assert result.text == "Part" and result.first_byte_at is not None


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        (ErrorKind.CLIENT_ERROR, "upstream_rejected"),
        (ErrorKind.DEADLINE, "deadline_exceeded"),
        (ErrorKind.AUTH, "upstream_auth_failed"),
        (ErrorKind.STALL, "no_healthy_provider"),
    ],
)
def test_relay_error_codes(kind: ErrorKind, code: str) -> None:
    chunks, _ = relay_output([ProviderError(kind, "x")])
    assert chunks[-2]["error"]["code"] == code


def test_watch_ends_quietly_if_only_the_tail_is_late() -> None:
    events, error = run_watch([TextDelta("a"), Finish("stop"), 0.3, UsageReport(Usage(1, 1))])
    assert error is None
    assert events == [TextDelta("a"), Finish("stop")]


JSON_REQUEST = ChatRequest(
    messages=(Message("user", "hi"),), max_tokens=10, response_format=ResponseFormat("json_object")
)


def test_relay_passes_a_json_mode_refusal_through() -> None:
    chunks, result = relay_output(
        [RefusalDelta("I can't help with that."), Finish("stop")], JSON_REQUEST
    )
    assert result.error is None
    assert {"refusal": "I can't help with that."} in deltas(chunks)
    assert chunks[-1] == "[DONE]" and "error" not in chunks[-2]


def test_relay_sends_empty_tool_arguments_as_an_empty_object() -> None:
    chunks, _ = relay_output([ToolCallStart(0, "c0", "now"), Finish("tool_calls")])
    calls = [d["tool_calls"] for d in deltas(chunks) if "tool_calls" in d]
    assert calls[0][0]["function"]["arguments"] == "{}"


def test_relay_ends_as_tool_calls_if_the_stream_dies_after_a_complete_call() -> None:
    chunks, result = relay_output(
        [
            ToolCallStart(0, "c0", "f"),
            ToolCallArgs(0, "{}"),
            ToolCallStart(1, "c1", "g"),
            ToolCallArgs(1, '{"half'),
            ProviderError(ErrorKind.CONNECTION, "cut"),
        ]
    )
    calls = [d["tool_calls"] for d in deltas(chunks) if "tool_calls" in d]
    assert [c[0]["id"] for c in calls] == ["c0"]  # the half-finished call is dropped
    finishes = [
        c["choices"][0]["finish_reason"] for c in chunks if isinstance(c, dict) and c.get("choices")
    ]
    assert finishes[-1] == "tool_calls"
    assert not any(isinstance(c, dict) and "error" in c for c in chunks)
    assert result.error is None and result.recovered_from is not None


def test_relay_close_accounts_once_even_if_never_started() -> None:
    calls: list[StreamResult] = []

    def finalize(result: StreamResult) -> dict[str, Any]:
        calls.append(result)
        return {}

    async def go() -> None:
        relay = Relay(response_id="x", alias="m", request=JSON_REQUEST, finalize=finalize)
        events = scripted([TextDelta("a")])
        await relay.close(events)  # the response never started
        await relay.close(events)

    asyncio.run(go())
    assert len(calls) == 1 and calls[0].aborted


def test_relay_close_after_a_full_run_does_not_account_again() -> None:
    calls: list[StreamResult] = []

    def finalize(result: StreamResult) -> dict[str, Any]:
        calls.append(result)
        return {}

    async def go() -> None:
        relay = Relay(
            response_id="x",
            alias="m",
            request=ChatRequest(messages=(Message("user", "hi"),), max_tokens=10),
            finalize=finalize,
        )
        events = scripted([TextDelta("a"), Finish("stop")])
        async for _ in relay.run(events):
            pass
        await relay.close(events)

    asyncio.run(go())
    assert len(calls) == 1 and not calls[0].aborted
