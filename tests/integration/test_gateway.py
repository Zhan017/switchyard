"""End to end: client -> gateway -> mockllm, including injected faults."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import httpx2
import openai
import pytest
from fastapi import FastAPI

from tests.integration.conftest import ADMIN_KEY, KEY

pytestmark = pytest.mark.anyio
Gateway = tuple[httpx.AsyncClient, FastAPI]


def chat(**body: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"model": "mock", "messages": [{"role": "user", "content": "hi"}]}
    payload.update(body)
    return payload


def events(response: httpx.Response) -> list[Any]:
    out: list[Any] = []
    for block in response.text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                data = line[6:]
                out.append(data if data == "[DONE]" else json.loads(data))
    return out


def content_of(stream: list[Any]) -> str:
    return "".join(
        c["choices"][0]["delta"].get("content") or ""
        for c in stream
        if isinstance(c, dict) and c.get("choices")
    )


def fault(chaos: httpx.Client, mode: str, **params: Any) -> None:
    response = chaos.post("/chaos", json={"mode": mode, "params": params, "model": "mock-a"})
    assert response.status_code == 200


# Happy paths


async def test_non_streaming(gateway: Gateway) -> None:
    client, _ = gateway
    response = await client.post("/v1/chat/completions", json=chat())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "mock"
    assert body["choices"][0]["message"]["content"]
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["switchyard"]["served_model"] == "mock/mock-a"
    assert body["switchyard"]["cost_usd"] > 0
    assert body["usage"]["total_tokens"] > 0
    assert response.headers["x-request-id"].startswith("sy-")


async def test_streaming(gateway: Gateway) -> None:
    client, _ = gateway
    response = await client.post(
        "/v1/chat/completions", json=chat(stream=True), headers={"x-request-id": "req-123"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-request-id"] == "req-123"
    stream = events(response)
    assert stream[-1] == "[DONE]"
    assert len(content_of(stream).split()) == 48
    usage_chunk = stream[-2]
    assert usage_chunk["choices"] == [] and usage_chunk["usage"]["completion_tokens"] == 48
    assert usage_chunk["switchyard"]["usage_estimated"] is False
    assert {c["id"] for c in stream if isinstance(c, dict)} == {"chatcmpl-req-123"}


async def test_streaming_tool_call_arrives_whole(gateway: Gateway) -> None:
    client, _ = gateway
    tool = {"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}}
    response = await client.post(
        "/v1/chat/completions", json=chat(stream=True, tools=[tool], tool_choice="required")
    )
    stream = events(response)
    calls = [
        c["choices"][0]["delta"]["tool_calls"]
        for c in stream
        if isinstance(c, dict) and c.get("choices") and "tool_calls" in c["choices"][0]["delta"]
    ]
    assert len(calls) == 1
    assert calls[0][0]["function"]["name"] == "search"
    assert "query" in json.loads(calls[0][0]["function"]["arguments"])
    finishes = [
        c["choices"][0]["finish_reason"] for c in stream if isinstance(c, dict) and c.get("choices")
    ]
    assert finishes[-1] == "tool_calls"


async def test_json_mode_arrives_as_one_valid_chunk(gateway: Gateway) -> None:
    client, _ = gateway
    response = await client.post(
        "/v1/chat/completions", json=chat(stream=True, response_format={"type": "json_object"})
    )
    stream = events(response)
    pieces = [
        c["choices"][0]["delta"]["content"]
        for c in stream
        if isinstance(c, dict) and c.get("choices") and c["choices"][0]["delta"].get("content")
    ]
    assert len(pieces) == 1 and "answer" in json.loads(pieces[0])


async def test_capability_routing_skips_models_without_tools(gateway: Gateway) -> None:
    client, _ = gateway
    tool = {"type": "function", "function": {"name": "f"}}
    response = await client.post("/v1/chat/completions", json=chat(model="plain", tools=[tool]))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_feature"


async def test_list_models(gateway: Gateway) -> None:
    client, _ = gateway
    ids = [m["id"] for m in (await client.get("/v1/models")).json()["data"]]
    assert {"mock", "plain", "mock/mock-a"} <= set(ids)


# Client errors


async def test_auth_and_feature_header(gateway: Gateway) -> None:
    client, _ = gateway
    no_key = await client.post("/v1/chat/completions", json=chat(), headers={"authorization": ""})
    assert no_key.status_code == 401
    assert no_key.json()["error"]["code"] == "invalid_api_key"
    no_feature = await client.post(
        "/v1/chat/completions", json=chat(), headers={"x-switchyard-feature": ""}
    )
    assert no_feature.status_code == 400
    assert no_feature.json()["error"]["code"] == "missing_feature"


async def test_unknown_model_and_bad_body(gateway: Gateway) -> None:
    client, _ = gateway
    unknown = await client.post("/v1/chat/completions", json=chat(model="gpt-9"))
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "model_not_found"
    bad = await client.post("/v1/chat/completions", json=chat(n=3))
    assert bad.status_code == 400
    assert bad.json()["error"]["param"] == "n"
    not_json = await client.post("/v1/chat/completions", content=b"{nope")
    assert not_json.status_code == 400


# Provider failures


@pytest.mark.parametrize("stream", [False, True])
async def test_provider_500_before_first_byte(
    gateway: Gateway, chaos: httpx.Client, stream: bool
) -> None:
    client, _ = gateway
    fault(chaos, "status", status=500)
    response = await client.post("/v1/chat/completions", json=chat(stream=stream))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "no_healthy_provider"
    assert response.headers["retry-after"] == "1"


async def test_provider_429_retry_after_is_passed_on(gateway: Gateway, chaos: httpx.Client) -> None:
    client, _ = gateway
    fault(chaos, "status", status=429, retry_after=30)
    response = await client.post("/v1/chat/completions", json=chat(stream=True))
    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"


async def test_provider_400_is_passed_through(gateway: Gateway, chaos: httpx.Client) -> None:
    client, _ = gateway
    fault(chaos, "status", status=400)
    response = await client.post("/v1/chat/completions", json=chat())
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "upstream_rejected"


@pytest.mark.parametrize(
    ("mode", "params"),
    [
        ("error_event", {"tokens": 5}),
        ("cut_after_tokens", {"tokens": 5}),
        ("malformed_chunk", {"tokens": 5}),
        ("stall_after_tokens", {"tokens": 5, "keep_alive_s": 0.1}),
    ],
)
async def test_mid_stream_failures_end_with_an_error_event(
    gateway: Gateway, chaos: httpx.Client, mode: str, params: dict[str, Any]
) -> None:
    client, _ = gateway
    fault(chaos, mode, **params)
    started = time.monotonic()
    response = await client.post("/v1/chat/completions", json=chat(stream=True))
    assert response.status_code == 200
    stream = events(response)
    assert len(content_of(stream).split()) == 5
    assert stream[-2]["error"]["code"] == "no_healthy_provider"
    assert stream[-1] == "[DONE]"
    # A stall is caught by the 0.5 s stall limit even with keep-alive comments flowing.
    assert time.monotonic() - started < 3


async def test_slow_first_token_is_a_timeout(gateway: Gateway, chaos: httpx.Client) -> None:
    client, _ = gateway
    fault(chaos, "ttft_ms", ms=1500)  # the test route allows 1 s
    response = await client.post("/v1/chat/completions", json=chat(stream=True))
    assert response.status_code == 503
    assert "first token" in response.json()["error"]["message"]


# Metrics, admin


async def test_metrics_count_requests_cost_and_overhead(gateway: Gateway) -> None:
    client, _ = gateway
    await client.post("/v1/chat/completions", json=chat(stream=True))
    await client.post(
        "/v1/chat/completions", json=chat(), headers={"x-switchyard-feature": "unknown-feature"}
    )
    text = (await client.get("/metrics")).text
    ok = 'feature="chat",route_group="mock",status="ok",tenant="team-a"} 1.0'
    assert "switchyard_requests_total{" + ok in text
    assert 'feature="other"' in text and 'feature="unknown-feature"' not in text
    assert 'switchyard_gateway_overhead_seconds_count{mode="stream"} 1.0' in text
    assert 'switchyard_cost_usd_total{cost_class="served"' in text


async def test_config_reload_needs_the_admin_key(gateway: Gateway) -> None:
    client, _ = gateway
    denied = await client.post("/admin/config/reload")
    assert denied.status_code == 401
    allowed = await client.post(
        "/admin/config/reload", headers={"authorization": f"Bearer {ADMIN_KEY}"}
    )
    assert allowed.status_code == 200
    providers = await client.get(
        "/admin/providers", headers={"authorization": f"Bearer {ADMIN_KEY}"}
    )
    assert providers.json()["mock"]["models"] == ["mock/mock-a", "mock/mock-b"]


# The official OpenAI SDK as the client


def sdk(gateway: Gateway) -> openai.AsyncOpenAI:
    _, app = gateway
    return openai.AsyncOpenAI(
        api_key=KEY,
        base_url="http://sy/v1",
        default_headers={"x-switchyard-feature": "chat"},
        # The SDK uses httpx2, its own fork of httpx.
        http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)),
        max_retries=0,
    )


async def test_openai_sdk_streams_through_the_gateway(gateway: Gateway) -> None:
    client = sdk(gateway)
    stream = await client.chat.completions.create(
        model="mock",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
        stream_options={"include_usage": True},
    )
    text, usage = "", None
    async for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            text += chunk.choices[0].delta.content
        if chunk.usage:
            usage = chunk.usage
    assert len(text.split()) == 48
    assert usage is not None and usage.completion_tokens == 48


async def test_openai_sdk_raises_on_a_mid_stream_error(
    gateway: Gateway, chaos: httpx.Client
) -> None:
    fault(chaos, "error_event", tokens=3)
    client = sdk(gateway)
    stream = await client.chat.completions.create(
        model="mock", messages=[{"role": "user", "content": "hi"}], stream=True
    )
    with pytest.raises(openai.APIError, match="mockllm injected"):
        async for _ in stream:
            pass


async def test_openai_sdk_raises_status_errors(gateway: Gateway, chaos: httpx.Client) -> None:
    fault(chaos, "status", status=500)
    client = sdk(gateway)
    with pytest.raises(openai.APIStatusError) as caught:
        await client.chat.completions.create(
            model="mock", messages=[{"role": "user", "content": "hi"}]
        )
    assert caught.value.status_code == 503


# Fixes from the week 2 review


def metric(text: str, name: str, **labels: str) -> float:
    """The value of the one series of ``name`` whose labels include ``labels``."""
    for line in text.splitlines():
        if line.startswith(name + "{") and all(f'{k}="{v}"' in line for k, v in labels.items()):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


async def test_invalid_json_stream_is_an_http_error(gateway: Gateway, chaos: httpx.Client) -> None:
    client, _ = gateway
    fault(chaos, "invalid_json")
    body = chat(stream=True, response_format={"type": "json_object"})
    response = await client.post("/v1/chat/completions", json=body)
    assert response.status_code == 503
    assert "invalid JSON" in response.json()["error"]["message"]


async def test_invalid_json_keeps_the_real_usage(gateway: Gateway, chaos: httpx.Client) -> None:
    client, _ = gateway
    fault(chaos, "invalid_json")
    response = await client.post(
        "/v1/chat/completions", json=chat(response_format={"type": "json_object"})
    )
    assert response.status_code == 503
    text = (await client.get("/metrics")).text
    # mockllm's reported tokens were billed even though the JSON was bad, and kept exact.
    assert metric(text, "switchyard_tokens_total", direction="output", estimated="false") > 0
    assert metric(text, "switchyard_tokens_total", direction="output", estimated="true") == 0


async def test_failures_before_the_first_token_cost_nothing(
    gateway: Gateway, chaos: httpx.Client
) -> None:
    client, _ = gateway
    fault(chaos, "status", status=500)
    await client.post("/v1/chat/completions", json=chat(stream=True))
    await client.post("/v1/chat/completions", json=chat())
    text = (await client.get("/metrics")).text
    assert metric(text, "switchyard_tokens_total", direction="input") == 0
    assert metric(text, "switchyard_cost_usd_total", cost_class="wasted") == 0


async def test_in_flight_gauge_returns_to_zero(gateway: Gateway, chaos: httpx.Client) -> None:
    client, _ = gateway
    await client.post("/v1/chat/completions", json=chat(stream=True))
    await client.post("/v1/chat/completions", json=chat())
    fault(chaos, "status", status=500)
    await client.post("/v1/chat/completions", json=chat(stream=True))
    text = (await client.get("/metrics")).text
    assert "switchyard_requests_in_flight 0.0" in text


async def test_buffered_json_is_not_counted_as_overhead(gateway: Gateway) -> None:
    client, _ = gateway
    body = chat(stream=True, response_format={"type": "json_object"})
    assert (await client.post("/v1/chat/completions", json=body)).status_code == 200
    text = (await client.get("/metrics")).text
    assert metric(text, "switchyard_gateway_overhead_seconds_count", mode="stream") == 0


async def test_provider_without_a_key_is_a_clear_503(
    gateway: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = gateway
    monkeypatch.delenv("TEST_ANTHROPIC_KEY", raising=False)
    response = await client.post("/v1/chat/completions", json=chat(model="anthropic/claude-test"))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_not_configured"
