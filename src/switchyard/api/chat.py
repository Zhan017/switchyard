"""POST /v1/chat/completions.

The attempt runner (core/attempts.py) tries the route's candidates in order, with
same-provider retries and failover before the client has seen anything. This module
turns its result into an OpenAI-format response and does the accounting: one Prometheus
update, one log line and one Postgres record per request, with a row per attempt.
Mid-stream failover arrives in week 4.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import orjson
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.types import Receive, Scope, Send

from switchyard.api.errors import error_response
from switchyard.api.schemas import parse_body, to_chat_request
from switchyard.api.state import GatewayState, Services
from switchyard.config import Tenant
from switchyard.core.attempts import (
    Attempt,
    Exhausted,
    NoCandidate,
    Opened,
    Runner,
    assemble,
    health_outcome,
)
from switchyard.core.midstream import Continuer
from switchyard.core.router import Route, capable_candidates, resolve
from switchyard.core.types import ChatRequest, StreamEvent, Usage
from switchyard.observability.logging import log_request
from switchyard.storage.records import utc
from switchyard.streaming.relay import Relay, StreamResult, error_code, usage_json


@dataclass
class RequestContext:
    request_id: str
    tenant: Tenant
    feature: str
    received: float = field(default_factory=time.monotonic)
    received_wall: float = field(default_factory=time.time)


class RelayResponse(StreamingResponse):
    """A streaming response that always settles its accounting and closes the upstream
    stream, even if the client left before the body started."""

    def __init__(
        self,
        relay: Relay,
        events: AsyncIterator[StreamEvent],
        head: list[StreamEvent],
        headers: dict[str, str],
    ) -> None:
        super().__init__(relay.run(events, head), media_type="text/event-stream", headers=headers)
        self._relay = relay
        self._events = events

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._relay.close(self._events)


@dataclass
class Call:
    """Everything one request's accounting needs."""

    ctx: RequestContext
    state: GatewayState
    services: Services
    route: Route
    request: ChatRequest
    prompt_hash: str | None
    runner: Runner


async def handle_chat(
    raw: Any, ctx: RequestContext, state: GatewayState, services: Services
) -> JSONResponse | StreamingResponse:
    body = parse_body(raw)
    route = resolve(state.settings, body.model)
    request = to_chat_request(body, route.limits.default_max_tokens)
    candidates = capable_candidates(route, request)
    runner = Runner(
        breakers=services.breakers,
        adapters=state.adapters,
        limits=route.limits,
        request=request,
        candidates=candidates,
        deadline=ctx.received + route.limits.request_deadline_s,
        # A JSON answer is sent only once complete and valid, so it is read to the end
        # before committing, even when the client asked for a stream.
        mode="stream" if body.stream and not request.wants_json else "buffered",
    )
    call = Call(ctx, state, services, route, request, _prompt_hash(raw, services), runner)
    metrics = services.metrics
    metrics.in_flight.inc()
    try:
        opened = await runner.open()
    except NoCandidate as error:
        metrics.in_flight.dec()
        message = "No healthy provider: " + ", ".join(f"{k} is {r}" for k, r in error.skipped)
        _account(call, [], served=None, status="no_healthy_provider", error_message=message)
        return error_response(503, message, "no_healthy_provider", retry_after=error.retry_after)
    except Exhausted as error:
        metrics.in_flight.dec()
        code, status = error_code(error.error)
        _account(call, error.attempts, served=None, status=code, error_message=error.error.message)
        return error_response(
            status,
            error.error.message,
            code,
            retry_after=error.retry_after if status == 503 else None,
        )
    except BaseException:
        # The client went away while attempts were running.
        metrics.in_flight.dec()
        _account(call, runner.attempts, served=None, status="client_closed", error_message=None)
        raise
    if not body.stream:
        metrics.in_flight.dec()
        return _complete(call, opened)
    return _stream(call, opened)


def _complete(call: Call, opened: Opened) -> JSONResponse:
    attempt = opened.attempt
    attempt.delivered = True
    assert opened.collected is not None
    completion = assemble(opened.collected)
    overhead = None
    if attempt.n == 1 and attempt.ended is not None:
        # Gateway time on both sides of the provider's answer.
        overhead = (time.monotonic() - call.ctx.received) - (attempt.ended - attempt.started)
    text = completion.text + "".join(c.name + c.arguments for c in completion.tool_calls)
    summary = _account(
        call,
        opened.attempts,
        served=attempt,
        status="ok",
        error_message=None,
        overhead=(overhead, "unary"),
        response_chars=len(text),
    )
    message: dict[str, Any] = {
        "role": "assistant",
        "content": completion.text or (None if completion.tool_calls else ""),
        "refusal": completion.refusal,
    }
    if completion.tool_calls:
        message["tool_calls"] = [
            {
                "id": tool.id,
                "type": "function",
                "function": {"name": tool.name, "arguments": tool.arguments},
            }
            for tool in completion.tool_calls
        ]
    return JSONResponse(
        {
            "id": _response_id(call.ctx),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": call.route.alias,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": completion.finish_reason,
                    "logprobs": None,
                }
            ],
            "usage": usage_json(_total_usage(opened.attempts)),
            "switchyard": summary,
        },
        headers={"x-request-id": call.ctx.request_id},
    )


def _stream(call: Call, opened: Opened) -> RelayResponse:
    first = opened.attempt
    # A buffered answer (JSON) is never continued: nothing reaches the client before it
    # is complete, so a failure there is an ordinary failover.
    continuer = None
    if opened.collected is None:
        continuer = Continuer(call.runner, list(call.runner.candidates), call.request, first)

    def finalize(result: StreamResult) -> dict[str, Any]:
        call.services.metrics.in_flight.dec()
        output = result.text + "".join(c.name + c.arguments for c in result.tool_calls)
        if result.error is not None:
            status, outcome = error_code(result.error)[0], result.error.kind.value
        elif result.aborted:
            status = outcome = "client_closed"
        elif result.recovered_from is not None:
            status, outcome = "ok", result.recovered_from.kind.value
        else:
            status = "ok"
            outcome = health_outcome(result.finish_reason, bool(result.refusal))
        if continuer is None:
            # A buffered attempt was settled and recorded when it committed.
            first.delivered = result.first_byte_at is not None
            if outcome == "client_closed":
                first.outcome = outcome
            served = first
        else:
            served = continuer.current
            if served.outcome == "pending":  # else a failed resume already recorded it
                call.runner.settle(served, result.usage, result.segment_text)
                if served.role == "midstream":
                    served.delivered = bool(result.segment_text)
                else:
                    served.delivered = result.first_byte_at is not None
                call.runner.finish(served, outcome)
        overhead = None
        # Only meaningful when nothing was retried and the first chunk follows the first
        # upstream token, which is not the case for a buffered JSON answer.
        ttft = opened.timing.ttft_s
        if first.n == 1 and continuer is not None and result.started_at and ttft:
            overhead = (result.started_at - call.ctx.received) - ttft
        result.usage = _total_usage(opened.attempts)
        error = result.error or result.recovered_from
        return _account(
            call,
            opened.attempts,
            served=served,
            status=status,
            error_message=error.message if error else None,
            overhead=(overhead, "stream"),
            response_chars=len(output),
            continuer=continuer,
        )

    relay = Relay(
        response_id=_response_id(call.ctx),
        alias=call.route.alias,
        request=call.request,
        finalize=finalize,
        resume=continuer.resume if continuer is not None else None,
        heartbeat_s=call.services.breakers.policy.heartbeat_s,
    )
    headers = {
        "x-request-id": call.ctx.request_id,
        "cache-control": "no-cache",
        "x-accel-buffering": "no",
    }
    return RelayResponse(relay, opened.events, opened.head, headers)


def _total_usage(attempts: list[Attempt]) -> Usage:
    total = Usage()
    for attempt in attempts:
        total = total + attempt.usage
    return total


def _account(
    call: Call,
    attempts: list[Attempt],
    *,
    served: Attempt | None,
    status: str,
    error_message: str | None,
    overhead: tuple[float | None, str] = (None, ""),
    response_chars: int | None = None,
    continuer: Continuer | None = None,
) -> dict[str, Any]:
    """Metrics, cost, the log line and the Postgres record for one finished request.
    Returns the summary sent to the client in the ``switchyard`` object."""
    ctx, services, route = call.ctx, call.services, call.route
    metrics = services.metrics
    tenant, feature = ctx.tenant.name, ctx.feature
    total_s = time.monotonic() - ctx.received
    total_cost = 0.0
    attempt_rows = []
    previous: Attempt | None = None
    for attempt in attempts:
        provider, model = attempt.candidate.provider, attempt.candidate.model
        usage = attempt.usage
        cost = call.state.prices.cost(attempt.key, usage) or 0.0
        total_cost += cost
        estimated = "true" if usage.estimated else "false"
        cost_class = "served" if attempt.delivered else "wasted"
        outcome = attempt.outcome if attempt.outcome != "pending" else "client_closed"
        metrics.attempts.labels(provider, model, attempt.role, outcome).inc()
        if attempt.ttft_s is not None:
            metrics.ttft.labels(provider).observe(attempt.ttft_s)
        labels = (tenant, feature, provider, model)
        metrics.tokens.labels(*labels, "input", estimated).inc(usage.input_tokens)
        metrics.tokens.labels(*labels, "output", estimated).inc(usage.output_tokens)
        metrics.cost.labels(*labels, cost_class, estimated).inc(cost)
        if attempt.role == "failover" and previous is not None:
            metrics.failovers.labels(previous.key, attempt.key, "before_first_token", "none").inc()
        previous = attempt
        if not services.records.enabled:
            continue
        attempt_rows.append(
            {
                "request_id": ctx.request_id,
                "n": attempt.n,
                "role": attempt.role,
                "provider": provider,
                "model": model,
                "started_at": utc(attempt.started_wall),
                "ttft_ms": _ms(attempt.ttft_s),
                "ended_at": (
                    utc(attempt.started_wall + (attempt.ended - attempt.started))
                    if attempt.ended is not None
                    else None
                ),
                "outcome": outcome,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "wasted_tokens": 0
                if attempt.delivered
                else usage.input_tokens + usage.output_tokens,
                "cost_usd": cost,
                "usage_estimated": usage.estimated,
                "emitted_chars_before": attempt.emitted_chars_before,
            }
        )
    for key, reason in call.runner.skipped:
        metrics.skipped.labels(key, reason).inc()
    midstream = continuer.failovers if continuer is not None else []
    for failover in midstream:
        metrics.failovers.labels(
            failover.from_key, failover.to_key, "midstream", failover.strategy
        ).inc()
        for action in failover.seam_actions:
            metrics.seam_repair.labels(action).inc()
    last_failover = midstream[-1] if midstream else None

    last = served or (attempts[-1] if attempts else None)
    provider_label = last.candidate.provider if last else "none"
    metrics.requests.labels(tenant, feature, route.alias, status).inc()
    metrics.request_seconds.labels(provider_label).observe(total_s)
    overhead_s, overhead_mode = overhead
    if overhead_s is not None:
        metrics.overhead.labels(overhead_mode).observe(max(0.0, overhead_s))

    usage = _total_usage(attempts)
    failovers = sum(1 for a in attempts if a.role == "failover")
    summary = {
        "request_id": ctx.request_id,
        "provider": last.candidate.provider if last else None,
        "served_model": served.key if served else None,
        "attempts": len(attempts),
        "failovers": failovers,
        "midstream_failovers": len(midstream),
        "strategy": last_failover.strategy if last_failover else None,
        "cost_usd": round(total_cost, 8),
        "usage_estimated": usage.estimated,
    }
    log_request(
        {
            **summary,
            "tenant": tenant,
            "feature": feature,
            "route": route.alias,
            "status": status,
            "error": error_message,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "ttft_ms": _ms(served.ttft_s) if served else None,
            "total_ms": _ms(total_s),
            "overhead_ms": _ms(overhead_s),
            "trail": [
                {"model": a.key, "role": a.role, "outcome": a.outcome, "ms": _ms(_took(a))}
                for a in attempts
            ],
            "skipped": [f"{k} ({r})" for k, r in call.runner.skipped] or None,
            "seam": (
                {
                    "actions": list(last_failover.seam_actions),
                    "latency_ms": _ms(last_failover.seam_latency_s),
                }
                if last_failover and last_failover.seam_actions
                else None
            ),
            "not_continued": continuer.refusal if continuer and continuer.refusal else None,
        }
    )
    records = services.records
    if records.enabled:
        records.add_request(
            {
                "id": ctx.request_id,
                "tenant": tenant,
                "feature": feature,
                "route_group": route.alias,
                "status": status,
                "started_at": utc(ctx.received_wall),
                "finished_at": utc(ctx.received_wall + total_s),
                "ttft_ms": _ms(served.ttft_s) if served else None,
                "total_ms": _ms(total_s),
                "served_provider": served.candidate.provider if served else None,
                "served_model": served.candidate.model if served else None,
                "strategy": last_failover.strategy if last_failover else None,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cost_usd": total_cost,
                "usage_estimated": usage.estimated,
                "failovers": failovers,
                "midstream_failovers": len(midstream),
                "seam_latency_ms": _ms(last_failover.seam_latency_s) if last_failover else None,
                "seam_action": ",".join(last_failover.seam_actions) if last_failover else None,
                "prompt_hash": call.prompt_hash,
                "prompt_chars": sum(len(m.content or "") for m in call.request.messages),
                "response_chars": response_chars,
                "error_code": None if status in ("ok", "client_closed") else status,
            },
            attempt_rows,
        )
    return summary


def _prompt_hash(raw: Any, services: Services) -> str | None:
    if not services.records.enabled or not isinstance(raw, dict):
        return None
    try:
        data = orjson.dumps(raw.get("messages"), option=orjson.OPT_SORT_KEYS)
    except TypeError:
        return None
    return hashlib.sha256(data).hexdigest()


def _took(attempt: Attempt) -> float | None:
    return None if attempt.ended is None else attempt.ended - attempt.started


def _ms(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds * 1000, 2)


def _response_id(ctx: RequestContext) -> str:
    return f"chatcmpl-{ctx.request_id}"


def new_request_id(header: str | None) -> str:
    if header and len(header) <= 128 and header.replace("-", "").replace("_", "").isalnum():
        return header
    return f"sy-{uuid.uuid4().hex}"
