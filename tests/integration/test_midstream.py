"""Mid-stream failover end to end: the primary dies after text went out, and mock-b
finishes the answer in the same stream (spec 7.6)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from tests.integration.test_gateway import chat, content_of, fault, metric

pytestmark = pytest.mark.anyio
Gateway = tuple[httpx.AsyncClient, FastAPI]


def frames(response: httpx.Response) -> list[Any]:
    """Data events and comment lines, in order."""
    out: list[Any] = []
    for block in response.text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith(": "):
                out.append(("comment", line[2:]))
            elif line.startswith("data: "):
                data = line[6:]
                out.append(data if data == "[DONE]" else json.loads(data))
    return out


def data(stream: list[Any]) -> list[Any]:
    return [f for f in stream if not isinstance(f, tuple)]


def comments(stream: list[Any]) -> list[str]:
    return [f[1] for f in stream if isinstance(f, tuple)]


@pytest.mark.parametrize(
    ("mode", "params", "kind"),
    [
        ("cut_after_tokens", {"tokens": 5}, "connection"),
        ("error_event", {"tokens": 5}, "server_error"),
        ("stall_after_tokens", {"tokens": 5, "keep_alive_s": 0.1}, "stall"),
    ],
)
async def test_a_broken_stream_is_finished_on_the_next_model(
    gateway: Gateway, chaos: httpx.Client, mode: str, params: dict[str, Any], kind: str
) -> None:
    client, _ = gateway
    fault(chaos, mode, **params)
    response = await client.post("/v1/chat/completions", json=chat(stream=True))
    assert response.status_code == 200
    stream = frames(response)
    events = data(stream)
    assert events[-1] == "[DONE]"
    assert not any(isinstance(e, dict) and "error" in e for e in events)
    finishes = [c["choices"][0]["finish_reason"] for c in events[:-1] if c.get("choices")]
    assert finishes[-1] == "stop"
    # Five words from mock-a, then mock-b's continuation.
    assert len(content_of(events).split()) > 5
    assert {e["id"] for e in events[:-1]} == {events[0]["id"]}
    notes = comments(stream)
    assert notes[0].startswith(f"switchyard failover: {kind} after")
    assert notes[-1] == "switchyard continuing on mock/mock-b with instructed_repair"
    summary = events[-2]["switchyard"]
    assert summary["midstream_failovers"] == 1
    assert summary["strategy"] == "instructed_repair"
    assert summary["served_model"] == "mock/mock-b"
    assert summary["seam"]["actions"] and summary["seam"]["latency_ms"] is not None
    assert summary["not_continued"] is None
    trail = summary["trail"]
    assert [(t["model"], t["role"], t["outcome"]) for t in trail] == [
        ("mock/mock-a", "primary", kind),
        ("mock/mock-b", "midstream", "ok"),
    ]
    assert trail[1]["output_tokens"] > 0
    # Usage covers both attempts: the continuation re-sends the prompt and partial answer.
    usage = events[-2]["usage"]
    assert usage["completion_tokens"] >= 5 + 1
    text = (await client.get("/metrics")).text
    assert metric(text, "switchyard_failovers_total", phase="midstream") == 1
    assert metric(text, "switchyard_attempts_total", role="midstream", outcome="ok") == 1
    assert metric(text, "switchyard_attempts_total", role="primary", outcome=kind) == 1
    assert metric(text, "switchyard_seam_repair_total") >= 1


async def test_heartbeats_while_the_continuation_starts(
    gateway: Gateway, chaos: httpx.Client
) -> None:
    client, _ = gateway
    fault(chaos, "cut_after_tokens", tokens=5)
    response = chaos.post(
        "/chaos", json={"mode": "ttft_ms", "params": {"ms": 700}, "model": "mock-b"}
    )
    assert response.status_code == 200
    response = await client.post("/v1/chat/completions", json=chat(stream=True))
    notes = comments(frames(response))
    # The test config sends a heartbeat every 0.2 s; mock-b takes 0.7 s to start.
    assert notes.count("heartbeat") >= 2
    assert notes.index("heartbeat") < notes.index(
        "switchyard continuing on mock/mock-b with instructed_repair"
    )


async def test_a_request_with_stop_sequences_is_not_continued(
    gateway: Gateway, chaos: httpx.Client
) -> None:
    client, _ = gateway
    fault(chaos, "cut_after_tokens", tokens=5)
    response = await client.post("/v1/chat/completions", json=chat(stream=True, stop=["END"]))
    events = data(frames(response))
    assert events[-2]["error"]["code"] == "no_healthy_provider"
    assert events[-2]["switchyard"]["not_continued"] == "stop_sequences"
    assert len(content_of(events).split()) == 5


async def test_non_streaming_requests_restart_invisibly(
    gateway: Gateway, chaos: httpx.Client
) -> None:
    """A non-streaming answer is read whole before anything is sent, so a cut is an
    ordinary failover, not a continuation."""
    client, _ = gateway
    fault(chaos, "cut_after_tokens", tokens=5)
    response = await client.post("/v1/chat/completions", json=chat())
    assert response.status_code == 200
    summary = response.json()["switchyard"]
    assert summary["midstream_failovers"] == 0
    assert summary["served_model"] == "mock/mock-b"
    assert len(response.json()["choices"][0]["message"]["content"].split()) == 48
