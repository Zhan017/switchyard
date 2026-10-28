"""POST /v1/chat/completions.

Week 2 serves each request from the first capable model in its route. Retries, breakers
and failover arrive in week 3, mid-stream failover in week 4; the accounting, metrics and
error shapes here already follow the spec, so those weeks only add attempts.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from fastapi.responses import JSONResponse, StreamingResponse
from starlette.types import Receive, Scope, Send

from switchyard.accounting.usage import complete_usage
from switchyard.api.errors import error_response
from switchyard.api.schemas import parse_body, to_chat_request
from switchyard.api.state import GatewayState
from switchyard.config import Tenant
from switchyard.core.router import Candidate, Route, capable_candidates, resolve
from switchyard.core.types import (
    ChatRequest,
    Completion,
    Finish,
    RefusalDelta,
    StreamEvent,
    TextDelta,
    Usage,
    UsageReport,
)
from switchyard.core.watch import StreamTiming, watch
from switchyard.observability.logging import log_request
from switchyard.observability.metrics import Metrics
from switchyard.providers.errors import ErrorKind, ProviderError
from switchyard.streaming.relay import Relay, StreamResult, check_json, error_code, usage_json

NO_USAGE = Usage()  # an attempt that failed before the first token; providers don't bill these


@dataclass
class RequestContext:
    request_id: str
    tenant: Tenant
    feature: str
    received: float = field(default_factory=time.monotonic)


@dataclass
class Outcome:
    """One finished attempt, ready for accounting."""

    candidate: Candidate
    usage: Usage
    status: str  # "ok", an error code, or "client_closed"
    attempt_outcome: str  # "ok", an error kind, or "client_closed"
    delivered: bool  # did any of the answer reach the client
    upstream_ttft_s: float | None
    overhead_s: float | None
    overhead_mode: str


class RelayResponse(StreamingResponse):
    """A streaming response that always settles its accounting and closes the upstream
    stream, even if the client left before the body started."""

    def __init__(
        self,
        relay: Relay,
        events: AsyncIterator[StreamEvent],
        first: StreamEvent | None,
        headers: dict[str, str],
    ) -> None:
        super().__init__(relay.run(events, first), media_type="text/event-stream", headers=headers)
        self._relay = relay
        self._events = events

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._relay.close(self._events)


async def handle_chat(
    raw: Any, ctx: RequestContext, state: GatewayState, metrics: Metrics
) -> JSONResponse | StreamingResponse:
    body = parse_body(raw)
    route = resolve(state.settings, body.model)
    request = to_chat_request(body, route.limits.default_max_tokens)
    candidate = capable_candidates(route, request)[0]
    if body.stream:
        return await _stream(ctx, state, metrics, route, candidate, request)
    metrics.in_flight.inc()
    try:
        return await _complete(ctx, state, metrics, route, candidate, request)
    finally:
        metrics.in_flight.dec()


async def _complete(
    ctx: RequestContext,
    state: GatewayState,
    metrics: Metrics,
    route: Route,
    candidate: Candidate,
    request: ChatRequest,
) -> JSONResponse:
    adapter = state.adapters[candidate.provider]
    remaining = ctx.received + route.limits.request_deadline_s - time.monotonic()
    started = time.monotonic()
    usage = NO_USAGE
    try:
        try:
            async with asyncio.timeout(remaining):
                completion = await adapter.complete(candidate.model, candidate.options, request)
        except TimeoutError:
            raise ProviderError(ErrorKind.DEADLINE, "request deadline exceeded") from None
        # The provider answered, so its usage counts even if the JSON turns out invalid.
        usage = complete_usage(completion.usage, request, _output_text(completion))
        if request.wants_json:
            check_json(completion.text, completion.finish_reason, bool(completion.refusal))
    except ProviderError as error:
        code, status = error_code(error)
        _account(
            ctx,
            state,
            metrics,
            route,
            Outcome(candidate, usage, code, error.kind.value, False, None, None, "unary"),
            error=error,
        )
        return error_response(status, error.message, code, retry_after=_retry_after(error, status))
    upstream_s = time.monotonic() - started
    overhead = (time.monotonic() - ctx.received) - upstream_s
    summary = _account(
        ctx,
        state,
        metrics,
        route,
        Outcome(candidate, usage, "ok", "ok", True, None, overhead, "unary"),
    )
    message: dict[str, Any] = {
        "role": "assistant",
        "content": completion.text or (None if completion.tool_calls else ""),
        "refusal": completion.refusal,
    }
    if completion.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments or "{}"},
            }
            for call in completion.tool_calls
        ]
    return JSONResponse(
        {
            "id": _response_id(ctx),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": route.alias,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": completion.finish_reason,
                    "logprobs": None,
                }
            ],
            "usage": usage_json(usage),
            "switchyard": summary,
        },
        headers={"x-request-id": ctx.request_id},
    )


async def _stream(
    ctx: RequestContext,
    state: GatewayState,
    metrics: Metrics,
    route: Route,
    candidate: Candidate,
    request: ChatRequest,
) -> JSONResponse | StreamingResponse:
    adapter = state.adapters[candidate.provider]
    limits = route.limits
    timing = StreamTiming()
    events = watch(
        adapter.stream(candidate.model, candidate.options, request),
        ttft_s=limits.ttft_timeout_s,
        stall_s=limits.stall_timeout_s,
        deadline=ctx.received + limits.request_deadline_s,
        timing=timing,
    )
    metrics.in_flight.inc()

    def fail(error: ProviderError, usage: Usage) -> JSONResponse:
        metrics.in_flight.dec()
        code, status = error_code(error)
        _account(
            ctx,
            state,
            metrics,
            route,
            Outcome(candidate, usage, code, error.kind.value, False, timing.ttft_s, None, "stream"),
            error=error,
        )
        return error_response(status, error.message, code, retry_after=_retry_after(error, status))

    # Wait for the first event before answering, so a provider that fails before
    # sending anything gets a proper HTTP error instead of a 200 with an error inside.
    try:
        first: StreamEvent | None = await anext(events)
    except StopAsyncIteration:
        first = None
    except ProviderError as error:
        return fail(error, NO_USAGE)

    source: AsyncIterator[StreamEvent] = events
    if request.wants_json:
        # A JSON answer is sent only once complete and valid, so read all of it now: then
        # any failure, including invalid JSON, is still a proper HTTP error.
        collected = [first] if first is not None else []
        try:
            async for event in events:
                collected.append(event)
            text = "".join(e.text for e in collected if isinstance(e, TextDelta))
            finish = next((e.reason for e in reversed(collected) if isinstance(e, Finish)), None)
            refused = any(isinstance(e, RefusalDelta) for e in collected)
            check_json(text, finish, refused)
        except ProviderError as error:
            reported = next(
                (e.usage for e in reversed(collected) if isinstance(e, UsageReport)), None
            )
            text = "".join(e.text for e in collected if isinstance(e, TextDelta))
            return fail(error, complete_usage(reported, request, text))
        first, source = None, _replay(collected)

    def finalize(result: StreamResult) -> dict[str, Any]:
        metrics.in_flight.dec()
        output = result.text + "".join(c.name + c.arguments for c in result.tool_calls)
        result.usage = complete_usage(result.usage, request, output)
        if result.error is not None:
            status, outcome = error_code(result.error)[0], result.error.kind.value
        elif result.aborted:
            status = outcome = "client_closed"
        elif result.recovered_from is not None:
            status, outcome = "ok", result.recovered_from.kind.value
        else:
            status = outcome = "ok"
        overhead = None
        # Only meaningful when the first chunk follows the first upstream token, which is
        # not the case for a buffered JSON answer.
        if not request.wants_json and result.started_at is not None and timing.ttft_s is not None:
            overhead = (result.started_at - ctx.received) - timing.ttft_s
        return _account(
            ctx,
            state,
            metrics,
            route,
            Outcome(
                candidate,
                result.usage,
                status,
                outcome,
                result.first_byte_at is not None,
                timing.ttft_s,
                overhead,
                "stream",
            ),
            error=result.error or result.recovered_from,
        )

    relay = Relay(
        response_id=_response_id(ctx), alias=route.alias, request=request, finalize=finalize
    )
    headers = {
        "x-request-id": ctx.request_id,
        "cache-control": "no-cache",
        "x-accel-buffering": "no",
    }
    return RelayResponse(relay, source, first, headers)


async def _replay(events: list[StreamEvent]) -> AsyncIterator[StreamEvent]:
    for event in events:
        yield event


def _account(
    ctx: RequestContext,
    state: GatewayState,
    metrics: Metrics,
    route: Route,
    outcome: Outcome,
    error: ProviderError | None = None,
) -> dict[str, Any]:
    """Metrics, cost and the log line for one finished request. Returns the summary sent to
    the client in the ``switchyard`` object."""
    candidate, usage = outcome.candidate, outcome.usage
    tenant, feature = ctx.tenant.name, ctx.feature
    cost = state.prices.cost(candidate.key, usage) or 0.0
    estimated = "true" if usage.estimated else "false"
    cost_class = "served" if outcome.delivered else "wasted"
    total_s = time.monotonic() - ctx.received

    metrics.requests.labels(tenant, feature, route.alias, outcome.status).inc()
    metrics.attempts.labels(
        candidate.provider, candidate.model, "primary", outcome.attempt_outcome
    ).inc()
    metrics.request_seconds.labels(candidate.provider).observe(total_s)
    if outcome.upstream_ttft_s is not None:
        metrics.ttft.labels(candidate.provider).observe(outcome.upstream_ttft_s)
    if outcome.overhead_s is not None:
        metrics.overhead.labels(outcome.overhead_mode).observe(max(0.0, outcome.overhead_s))
    labels = (tenant, feature, candidate.provider, candidate.model)
    metrics.tokens.labels(*labels, "input", estimated).inc(usage.input_tokens)
    metrics.tokens.labels(*labels, "output", estimated).inc(usage.output_tokens)
    metrics.cost.labels(*labels, cost_class, estimated).inc(cost)

    summary = {
        "request_id": ctx.request_id,
        "provider": candidate.provider,
        "served_model": candidate.key,
        "attempts": 1,
        "failovers": 0,
        "midstream_failovers": 0,
        "cost_usd": round(cost, 8),
        "usage_estimated": usage.estimated,
    }
    log_request(
        {
            **summary,
            "tenant": tenant,
            "feature": feature,
            "route": route.alias,
            "status": outcome.status,
            "error": error.message if error else None,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "ttft_ms": _ms(outcome.upstream_ttft_s),
            "total_ms": _ms(total_s),
            "overhead_ms": _ms(outcome.overhead_s),
        }
    )
    return summary


def _ms(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds * 1000, 2)


def _retry_after(error: ProviderError, status: int) -> float | None:
    if status != 503:
        return None
    return error.retry_after if error.retry_after is not None else 1.0


def _output_text(completion: Completion) -> str:
    return completion.text + "".join(c.name + c.arguments for c in completion.tool_calls)


def _response_id(ctx: RequestContext) -> str:
    return f"chatcmpl-{ctx.request_id}"


def new_request_id(header: str | None) -> str:
    if header and len(header) <= 128 and header.replace("-", "").replace("_", "").isalnum():
        return header
    return f"sy-{uuid.uuid4().hex}"
