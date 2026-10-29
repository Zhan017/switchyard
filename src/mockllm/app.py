"""mockllm: a fake OpenAI-compatible provider you can break on command.

It answers ``POST /v1/chat/completions`` with deterministic text, streaming or not, and
honors a set of faults switched on with ``POST /chaos``. The outage scenarios and the
integration tests run against it, so they cost nothing and repeat exactly.

Faults (``mode``), each optionally limited to one ``model`` and a ``duration_s``:

- ``error_rate``: fail a share of requests: ``{"rate": 0.5, "status": 500}``
- ``status``: fail every request: ``{"status": 429, "retry_after": 30}``
- ``ttft_ms``: wait this long before the first token: ``{"ms": 3000}``
- ``token_delay_ms``: wait this long between tokens: ``{"ms": 50}``
- ``cut_after_tokens``: end the stream after N tokens, with no finish and no [DONE]
- ``stall_after_tokens``: stop sending after N tokens but keep the connection open;
  ``{"tokens": 20, "keep_alive_s": 1}`` also sends ``: keep-alive`` comments, as DeepSeek does
- ``malformed_chunk``: send a broken chunk after N tokens
- ``error_event``: send ``data: {"error": ...}`` inside the 200 stream after N tokens
- ``hang``: accept the request and never answer
- ``invalid_json``: in JSON mode, cut the answer short so it doesn't parse
- ``refuse_connections``: refuse TCP connections and reset open ones (gate mode only,
  see gate.py); applies to the whole server, not one model
- ``flap``: alternate healthy and down: ``{"period_s": 20, "down": "refuse"}``; ``down``
  is ``refuse`` (gate mode) or ``error`` (HTTP 503)
- ``replay``: play back a recorded stream file as it is, optionally cut short:
  ``{"file": "openai_text.sse", "cut_after_events": 5}``, from ``MOCKLLM_FIXTURES``

Any fault's params may include ``seed``, which reseeds the random choices (for
``error_rate``), so a scenario run with the same seed injects the same failures.

``GET /stats`` counts requests, and probes separately (``max_tokens`` of 1, which is what
Switchyard's prober sends), so a scenario can count upstream attempts.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import random
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from mockllm.gate import Gate

WORDS = tuple(
    [
        "the",
        "gateway",
        "routes",
        "each",
        "request",
        "to",
        "a",
        "healthy",
        "provider",
        "and",
        "keeps",
        "the",
        "answer",
        "flowing",
        "when",
        "one",
        "of",
        "them",
        "fails",
        "so",
        "the",
        "client",
        "sees",
        "a",
        "single",
        "steady",
        "stream",
        "of",
        "text",
        "while",
        "breakers",
        "retries",
        "and",
        "failover",
        "work",
        "quietly",
        "underneath",
    ]
)

MODES = {
    "error_rate",
    "status",
    "ttft_ms",
    "token_delay_ms",
    "cut_after_tokens",
    "stall_after_tokens",
    "malformed_chunk",
    "error_event",
    "hang",
    "invalid_json",
    "refuse_connections",
    "flap",
    "replay",
}
FIXTURES = Path(os.environ.get("MOCKLLM_FIXTURES", "tests/fixtures/streams"))


@dataclass
class Fault:
    mode: str
    params: dict[str, Any]
    model: str | None = None
    until: float | None = None

    def active(self, model: str, now: float) -> bool:
        if self.until is not None and now >= self.until:
            return False
        return self.model is None or self.model == model


@dataclass
class MockState:
    ttft_ms: float = float(os.environ.get("MOCKLLM_TTFT_MS", "20"))
    token_delay_ms: float = float(os.environ.get("MOCKLLM_TOKEN_MS", "5"))
    answer_tokens: int = int(os.environ.get("MOCKLLM_ANSWER_TOKENS", "48"))
    faults: list[Fault] = field(default_factory=list)
    requests: int = 0
    probes: int = 0
    by_model: Counter[str] = field(default_factory=Counter)
    rng: random.Random = field(default_factory=lambda: random.Random(7))
    gate: Gate | None = None  # set when served behind a gate (python -m mockllm)
    flapper: asyncio.Task[None] | None = None

    def fault(self, mode: str, model: str) -> dict[str, Any] | None:
        now = time.monotonic()
        self.faults = [f for f in self.faults if f.until is None or now < f.until]
        for fault in reversed(self.faults):
            if fault.mode == mode and fault.active(model, now):
                return fault.params
        return None


def _answer_words(seed_text: str, count: int) -> list[str]:
    rng = random.Random(hashlib.sha256(seed_text.encode()).hexdigest())
    return [rng.choice(WORDS) for _ in range(count)]


def _prompt_tokens(messages: list[dict[str, Any]]) -> int:
    text = " ".join(str(m.get("content") or "") for m in messages)
    return max(1, len(text.split())) + 4 * len(messages)


def create_mock_app() -> FastAPI:
    app = FastAPI(title="mockllm")
    state = MockState()
    app.state.mock = state

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/stats")
    async def stats() -> dict[str, Any]:
        return {
            "requests": state.requests,
            "probes": state.probes,
            "by_model": dict(state.by_model),
            "refusing": bool(state.gate and state.gate.refusing),
        }

    @app.post("/chaos", response_model=None)
    async def set_fault(request: Request) -> Response:
        body = await request.json()
        mode = body.get("mode")
        if mode not in MODES:
            return JSONResponse({"error": f"unknown mode {mode!r}"}, status_code=400)
        params = body.get("params") or {}
        duration = body.get("duration_s")
        if "seed" in params:
            state.rng = random.Random(params["seed"])
        gated = state.gate is not None
        down = params.get("down", "refuse" if gated else "error")
        if (mode == "refuse_connections" or (mode == "flap" and down == "refuse")) and not gated:
            return JSONResponse(
                {"error": f"{mode} needs gate mode: run python -m mockllm"}, status_code=400
            )
        until = time.monotonic() + float(duration) if duration else None
        state.faults.append(Fault(mode, params, body.get("model"), until))
        if mode == "refuse_connections" and state.gate is not None:
            state.gate.refuse(float(duration) if duration else None)
        if mode == "flap" and down == "refuse" and state.gate is not None:
            _stop_flapping(state)
            state.flapper = asyncio.create_task(
                _flap(state.gate, float(params.get("period_s", 20)), until)
            )
        return JSONResponse({"active": len(state.faults)})

    @app.delete("/chaos")
    async def clear_faults() -> dict[str, int]:
        state.faults.clear()
        _stop_flapping(state)
        if state.gate is not None and state.gate.refusing:
            await state.gate.accept()
        return {"active": 0}

    @app.post("/stats/reset")
    async def reset_stats() -> dict[str, int]:
        state.requests = state.probes = 0
        state.by_model.clear()
        return {"requests": 0}

    @app.post("/v1/chat/completions", response_model=None)
    async def chat(request: Request) -> Response:
        state.requests += 1
        body = await request.json()
        model = str(body.get("model") or "mock")
        state.by_model[model] += 1
        if (body.get("max_completion_tokens") or body.get("max_tokens")) == 1:
            state.probes += 1
        messages = body.get("messages") or []

        if (flap := state.fault("flap", model)) is not None:
            period = float(flap.get("period_s", 20))
            if flap.get("down", "error") == "error" and int(time.monotonic() // period) % 2:
                return JSONResponse(
                    {"error": {"message": "mockllm flapping: down", "type": "server_error"}},
                    status_code=503,
                )
        if (replay := state.fault("replay", model)) is not None:
            return _replay(replay)
        if (hang := state.fault("hang", model)) is not None:
            await asyncio.sleep(float(hang.get("seconds", 3600)))
        failure = _failure(state, model)
        if failure is not None:
            return failure

        ttft = state.fault("ttft_ms", model)
        delay = state.fault("token_delay_ms", model)
        ttft_s = float(ttft["ms"] if ttft else state.ttft_ms) / 1000
        delay_s = float(delay["ms"] if delay else state.token_delay_ms) / 1000
        limit = int(body.get("max_completion_tokens") or body.get("max_tokens") or 1_000_000)
        count = min(limit, state.answer_tokens)
        prompt = json.dumps(messages, sort_keys=True)
        broken_json = state.fault("invalid_json", model) is not None
        pieces, tool_call = _content(body, prompt, count, broken_json)
        finish = "length" if limit < state.answer_tokens and not tool_call else "stop"
        if tool_call:
            finish = "tool_calls"
        usage = {
            "prompt_tokens": _prompt_tokens(messages),
            "completion_tokens": len(pieces),
            "total_tokens": _prompt_tokens(messages) + len(pieces),
        }
        response_id = f"chatcmpl-mock-{uuid.uuid4().hex[:12]}"

        if not body.get("stream"):
            # Generate at the same pace as a stream, one sleep per token, as a real provider
            # does; otherwise a gateway that reads the answer as a stream looks slower.
            await asyncio.sleep(ttft_s)
            for _ in range(len(pieces) - 1):
                await asyncio.sleep(delay_s)
            message: dict[str, Any] = {"role": "assistant", "content": None}
            if tool_call:
                message["tool_calls"] = [
                    {
                        "id": tool_call["id"],
                        "type": "function",
                        "function": {"name": tool_call["name"], "arguments": "".join(pieces)},
                    }
                ]
            else:
                message["content"] = "".join(pieces)
            return JSONResponse(
                {
                    "id": response_id,
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": usage,
                }
            )

        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        stream = _stream(
            state,
            model,
            response_id,
            pieces,
            tool_call,
            finish,
            usage,
            include_usage,
            ttft_s,
            delay_s,
        )
        return StreamingResponse(stream, media_type="text/event-stream")

    return app


def _stop_flapping(state: MockState) -> None:
    if state.flapper is not None:
        state.flapper.cancel()
        state.flapper = None


async def _flap(gate: Gate, period_s: float, until: float | None) -> None:
    """Refuse for one period, accept for the next, until the fault ends."""
    with contextlib.suppress(asyncio.CancelledError):
        while until is None or time.monotonic() < until:
            await asyncio.sleep(period_s)
            gate.refuse(period_s)
            await asyncio.sleep(period_s)
        await gate.accept()


def _replay(params: dict[str, Any]) -> Response:
    """A recorded stream, byte for byte, optionally ending after N events with no [DONE]."""
    path = (FIXTURES / str(params.get("file", ""))).resolve()
    if FIXTURES.resolve() not in path.parents or not path.is_file():
        return JSONResponse({"error": {"message": f"no fixture {path.name}"}}, status_code=500)
    events = [e for e in path.read_bytes().split(b"\n\n") if e.strip()]
    cut = params.get("cut_after_events")
    if cut is not None:
        events = events[: int(cut)]
    delay_s = float(params.get("delay_ms", 5)) / 1000

    async def play() -> AsyncIterator[bytes]:
        for event in events:
            yield event + b"\n\n"
            await asyncio.sleep(delay_s)

    return StreamingResponse(play(), media_type="text/event-stream")


def _failure(state: MockState, model: str) -> Response | None:
    fixed = state.fault("status", model)
    rate = state.fault("error_rate", model)
    params = fixed
    if params is None and rate is not None and state.rng.random() < float(rate.get("rate", 0)):
        params = rate
    if params is None:
        return None
    status = int(params.get("status", 500))
    headers = {}
    if params.get("retry_after") is not None:
        headers["retry-after"] = str(params["retry_after"])
    return JSONResponse(
        {"error": {"message": f"mockllm injected {status}", "type": "server_error"}},
        status_code=status,
        headers=headers,
    )


def _argument_names(tool: dict[str, Any]) -> list[str]:
    """The tool's required parameters, else all of them, else "query"."""
    parameters = tool["function"].get("parameters") or {}
    names = parameters.get("required") or list((parameters.get("properties") or {}).keys())
    return [str(n) for n in names] or ["query"]


def _content(
    body: dict[str, Any], prompt: str, count: int, broken_json: bool = False
) -> tuple[list[str], dict[str, str] | None]:
    """The answer as a list of streamed pieces, and the tool call if the answer is one."""
    words = _answer_words(prompt, count)
    tools = body.get("tools") or []
    choice = body.get("tool_choice")
    wants_tool = tools and (choice == "required" or isinstance(choice, dict))
    if wants_tool:
        name = tools[0]["function"]["name"]
        if isinstance(choice, dict):
            name = choice.get("function", {}).get("name", name)
        tool = next((t for t in tools if t["function"]["name"] == name), tools[0])
        arguments = json.dumps({key: " ".join(words[:6]) for key in _argument_names(tool)})
        pieces = [arguments[i : i + 8] for i in range(0, len(arguments), 8)]
        return pieces, {"id": f"call_{uuid.uuid4().hex[:10]}", "name": name}
    fmt = (body.get("response_format") or {}).get("type")
    if fmt in ("json_object", "json_schema"):
        text = json.dumps({"answer": " ".join(words)})
        if broken_json:
            text = text[:-2]
        return [text[i : i + 6] for i in range(0, len(text), 6)], None
    return [(" " if i else "") + w for i, w in enumerate(words)], None


async def _stream(
    state: MockState,
    model: str,
    response_id: str,
    pieces: list[str],
    tool_call: dict[str, str] | None,
    finish: str,
    usage: dict[str, int],
    include_usage: bool,
    ttft_s: float,
    delay_s: float,
) -> AsyncIterator[bytes]:
    created = int(time.time())

    def chunk(delta: dict[str, Any], finish_reason: str | None = None) -> bytes:
        payload = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        return f"data: {json.dumps(payload)}\n\n".encode()

    cut = state.fault("cut_after_tokens", model)
    stall = state.fault("stall_after_tokens", model)
    malformed = state.fault("malformed_chunk", model)
    error_event = state.fault("error_event", model)

    yield chunk({"role": "assistant", "content": ""})
    await asyncio.sleep(ttft_s)
    for i, piece in enumerate(pieces):
        if cut is not None and i >= int(cut.get("tokens", 0)):
            return
        if stall is not None and i >= int(stall.get("tokens", 0)):
            keep_alive = stall.get("keep_alive_s")
            while True:
                await asyncio.sleep(float(keep_alive or 3600))
                if keep_alive:
                    yield b": keep-alive\n\n"
        if malformed is not None and i >= int(malformed.get("tokens", 0)):
            yield b"data: {not json\n\n"
            return
        if error_event is not None and i >= int(error_event.get("tokens", 0)):
            message = error_event.get("message", "mockllm injected an error event")
            error = {"error": {"message": message, "type": "server_error"}}
            yield f"data: {json.dumps(error)}\n\n".encode()
            return
        if i:
            await asyncio.sleep(delay_s)
        if tool_call:
            call: dict[str, Any] = {"index": 0, "function": {"arguments": piece}}
            if i == 0:
                call.update({"id": tool_call["id"], "type": "function"})
                call["function"]["name"] = tool_call["name"]
            yield chunk({"tool_calls": [call]})
        else:
            yield chunk({"content": piece})
    yield chunk({}, finish)
    if include_usage:
        payload = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [],
            "usage": usage,
        }
        yield f"data: {json.dumps(payload)}\n\n".encode()
    yield b"data: [DONE]\n\n"


def app_from_env() -> FastAPI:
    """Entry point for ``uvicorn --factory mockllm.app:app_from_env`` (no gate)."""
    return create_mock_app()
