"""The attempt runner: try candidates in order, retry once on the same provider when that
is safe, and fail over before the client has seen anything (spec 7.5).

Every request is read from the provider as a stream, including non-streaming ones, so the
first-token and stall limits apply to all of them (decision 5). An attempt is committed
once something the client can see is ready:

- ``stream`` mode: the first text or refusal, the first complete tool call, or the end;
- ``buffered`` mode (non-streaming requests and JSON answers): the end of the answer,
  after JSON is validated.

Until then a failure is invisible to the client and the runner moves on. After the commit
the stream belongs to the relay; a failure there is a mid-stream failure (week 4).
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from switchyard.accounting.usage import complete_usage
from switchyard.config import Resilience as Policy
from switchyard.config import RouteGroup
from switchyard.core.router import Candidate
from switchyard.core.types import (
    ChatRequest,
    Completion,
    Finish,
    FinishReason,
    RefusalDelta,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallArgs,
    ToolCallStart,
    Usage,
    UsageReport,
)
from switchyard.core.watch import StreamTiming, watch
from switchyard.providers.base import Adapter
from switchyard.providers.errors import RETRYABLE, ErrorKind, ProviderError
from switchyard.resilience.breaker import Breakers
from switchyard.streaming.relay import check_json

NO_USAGE = Usage()  # an attempt that failed before any output; providers don't bill these

# Same-provider retries only for these (spec 7.5): connection errors, timeouts, 5xx and
# 529. A malformed chunk or invalid JSON is more likely to repeat, so it fails over instead.
RETRY_KINDS = frozenset({ErrorKind.CONNECTION, ErrorKind.TIMEOUT, ErrorKind.SERVER_ERROR})
# Worth trying another model. A client error would fail anywhere, and a missed request
# deadline leaves no time to try.
FAILOVER_KINDS = RETRYABLE | {ErrorKind.RATE_LIMITED, ErrorKind.AUTH}

Mode = Literal["stream", "buffered"]
Role = Literal["primary", "retry", "failover"]


@dataclass
class Attempt:
    n: int
    role: Role
    candidate: Candidate
    started: float = field(default_factory=time.monotonic)
    started_wall: float = field(default_factory=time.time)
    ended: float | None = None
    ttft_s: float | None = None
    outcome: str = "pending"  # "ok", an error kind, "content_filter" or "client_closed"
    error: ProviderError | None = None
    usage: Usage = NO_USAGE
    delivered: bool = False  # did any of the answer reach the client

    @property
    def key(self) -> str:
        return self.candidate.key


@dataclass
class Opened:
    """A committed attempt, ready for the relay or for a non-streaming response."""

    attempt: Attempt
    head: list[StreamEvent]  # events read before the commit
    events: AsyncIterator[StreamEvent]  # the rest of the stream (empty in buffered mode)
    timing: StreamTiming
    attempts: list[Attempt]
    collected: list[StreamEvent] | None  # the whole answer, in buffered mode

    async def all_events(self) -> AsyncIterator[StreamEvent]:
        for event in self.head:
            yield event
        async for event in self.events:
            yield event


class NoCandidate(Exception):
    """Every candidate was skipped: breakers open or cooling down. No attempt was made."""

    def __init__(self, skipped: list[tuple[str, str]], retry_after: float) -> None:
        super().__init__("no healthy provider")
        self.skipped = skipped
        self.retry_after = retry_after


class Exhausted(Exception):
    """Attempts were made and none could be committed."""

    def __init__(self, error: ProviderError, attempts: list[Attempt], retry_after: float) -> None:
        super().__init__(error.message)
        self.error = error
        self.attempts = attempts
        self.retry_after = retry_after


class Runner:
    def __init__(
        self,
        *,
        breakers: Breakers,
        adapters: Mapping[str, Adapter],
        limits: RouteGroup,
        request: ChatRequest,
        candidates: Sequence[Candidate],
        deadline: float,
        mode: Mode,
        rng: random.Random | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.breakers = breakers
        self.policy: Policy = breakers.policy
        self.adapters = adapters
        self.limits = limits
        self.request = request
        self.candidates = candidates
        self.deadline = deadline
        self.mode = mode
        self.rng = rng or random.Random()
        self.sleep = sleep
        self.attempts: list[Attempt] = []
        self.skipped: list[tuple[str, str]] = []

    async def open(self) -> Opened:
        last_error: ProviderError | None = None
        retries = 0
        for candidate in self.candidates:
            if len(self.attempts) >= self.policy.max_attempts:
                break
            reason = self.breakers.skip_reason(candidate.key)
            if reason is not None:
                self.skipped.append((candidate.key, reason))
                continue
            role: Role = "primary" if not self.attempts else "failover"
            retried = False
            while True:
                if self.deadline - time.monotonic() <= 0:
                    raise self._exhausted(ProviderError(ErrorKind.DEADLINE, "deadline exceeded"))
                attempt = Attempt(len(self.attempts) + 1, role, candidate)
                self.attempts.append(attempt)
                self.breakers.note_attempt(candidate.key, retry=role == "retry")
                try:
                    return await self._try(attempt)
                except ProviderError as error:
                    last_error = error
                    if error.kind not in FAILOVER_KINDS:
                        raise self._exhausted(error) from None
                    if error.kind == ErrorKind.RATE_LIMITED:
                        self.breakers.cool_down(candidate.key, error.retry_after, "rate_limited")
                        break
                    if error.kind == ErrorKind.AUTH:
                        self.breakers.cool_down(candidate.key, None, "auth")
                        break
                    if not retried and self._may_retry(candidate, error):
                        delay = self.rng.uniform(
                            0, min(self.policy.retry_cap_s, self.policy.retry_base_s * 2**retries)
                        )
                        if time.monotonic() + delay < self.deadline:
                            retried, role, retries = True, "retry", retries + 1
                            await self.sleep(delay)
                            continue
                    break
        if not self.attempts:
            keys = [key for key, _ in self.skipped]
            raise NoCandidate(self.skipped, self.breakers.retry_after(keys))
        assert last_error is not None
        raise self._exhausted(last_error)

    def _may_retry(self, candidate: Candidate, error: ProviderError) -> bool:
        return (
            error.kind in RETRY_KINDS
            and len(self.attempts) < self.policy.max_attempts
            # The breaker may have opened on this very failure.
            and self.breakers.skip_reason(candidate.key) is None
            and self.breakers.allow_retry(candidate.key)
        )

    def _exhausted(self, error: ProviderError) -> Exhausted:
        keys = [a.key for a in self.attempts] + [key for key, _ in self.skipped]
        retry_after = error.retry_after or self.breakers.retry_after(keys)
        return Exhausted(error, self.attempts, retry_after)

    async def _try(self, attempt: Attempt) -> Opened:
        candidate = attempt.candidate
        adapter = self.adapters[candidate.provider]
        timing = StreamTiming()
        events = watch(
            adapter.stream(candidate.model, candidate.options, self.request),
            ttft_s=self.limits.ttft_timeout_s,
            stall_s=self.limits.stall_timeout_s,
            deadline=self.deadline,
            timing=timing,
        )
        buffer: list[StreamEvent] = []
        tool_starts = 0
        try:
            while True:
                try:
                    event = await anext(events)
                except StopAsyncIteration:
                    break
                buffer.append(event)
                if self.mode == "stream":
                    if isinstance(event, ToolCallStart):
                        tool_starts += 1
                    if _visible(event, tool_starts):
                        self._first_token(attempt, timing)
                        return Opened(attempt, buffer, events, timing, self.attempts, None)
            if self.mode == "buffered" and self.request.wants_json:
                text = "".join(e.text for e in buffer if isinstance(e, TextDelta))
                check_json(text, _finish(buffer), any(isinstance(e, RefusalDelta) for e in buffer))
        except ProviderError as error:
            await _close(events)
            self._failed(attempt, timing, error, buffer)
            raise
        except BaseException:
            await _close(events)
            raise
        # The answer ended: in buffered mode that is the commit point; in stream mode an
        # answer with no visible content (an empty reply) commits here too.
        self._first_token(attempt, timing)
        if self.mode == "buffered":
            attempt.ended = time.monotonic()
            attempt.usage = complete_usage(_reported(buffer), self.request, _output(buffer))
            self.finish(attempt, _health_outcome(buffer))
            return Opened(attempt, buffer, _nothing(), timing, self.attempts, buffer)
        return Opened(attempt, buffer, _nothing(), timing, self.attempts, None)

    def _first_token(self, attempt: Attempt, timing: StreamTiming) -> None:
        attempt.ttft_s = timing.ttft_s
        if timing.ttft_s is not None:
            self.breakers.record_ttft(attempt.key, timing.ttft_s, over_budget=False)

    def _failed(
        self,
        attempt: Attempt,
        timing: StreamTiming,
        error: ProviderError,
        buffer: list[StreamEvent],
    ) -> None:
        attempt.ended = time.monotonic()
        attempt.ttft_s = timing.ttft_s
        attempt.error = error
        attempt.outcome = error.kind.value
        if timing.ttft_s is not None:
            self.breakers.record_ttft(attempt.key, timing.ttft_s, over_budget=False)
        elif error.kind == ErrorKind.TIMEOUT:
            self.breakers.record_ttft(attempt.key, self.limits.ttft_timeout_s, over_budget=True)
        # Output the provider generated before failing is billed; nothing before that is.
        output = _output(buffer)
        if output or _reported(buffer) is not None:
            attempt.usage = complete_usage(_reported(buffer), self.request, output)
        self.breakers.record_outcome(attempt.key, attempt.outcome)

    def finish(self, attempt: Attempt, outcome: str) -> None:
        """Record how a committed attempt ended. A client that left says nothing about
        the provider, so it isn't counted."""
        attempt.outcome = outcome
        if attempt.ended is None:
            attempt.ended = time.monotonic()
        if outcome != "client_closed":
            self.breakers.record_outcome(attempt.key, outcome)


def _visible(event: StreamEvent, tool_starts: int) -> bool:
    """Would the relay send something to the client for this event? Tool calls are only
    sent once complete, so the first one counts when the second one starts."""
    if isinstance(event, TextDelta | RefusalDelta | Finish):
        return True
    return isinstance(event, ToolCallStart) and tool_starts >= 2


def _finish(events: list[StreamEvent]) -> FinishReason | None:
    return next((e.reason for e in reversed(events) if isinstance(e, Finish)), None)


def _reported(events: list[StreamEvent]) -> Usage | None:
    return next((e.usage for e in reversed(events) if isinstance(e, UsageReport)), None)


def _output(events: list[StreamEvent]) -> str:
    parts = []
    for event in events:
        if isinstance(event, TextDelta | RefusalDelta):
            parts.append(event.text)
        elif isinstance(event, ToolCallStart):
            parts.append(event.name)
        elif isinstance(event, ToolCallArgs):
            parts.append(event.delta)
    return "".join(parts)


def _health_outcome(events: list[StreamEvent]) -> str:
    return health_outcome(_finish(events), any(isinstance(e, RefusalDelta) for e in events))


def health_outcome(finish: FinishReason | None, refused: bool) -> str:
    """A refusal or a content filter is a normal answer, not a provider failure."""
    return "content_filter" if refused or finish == "content_filter" else "ok"


def assemble(events: list[StreamEvent]) -> Completion:
    """A whole answer from its stream events, for non-streaming responses."""
    text: list[str] = []
    refusal: list[str] = []
    calls: dict[int, list[str]] = {}  # index -> [id, name, arguments]
    for event in events:
        if isinstance(event, TextDelta):
            text.append(event.text)
        elif isinstance(event, RefusalDelta):
            refusal.append(event.text)
        elif isinstance(event, ToolCallStart):
            calls[event.index] = [event.id, event.name, ""]
        elif isinstance(event, ToolCallArgs) and event.index in calls:
            calls[event.index][2] += event.delta
    tool_calls = tuple(ToolCall(c[0], c[1], c[2] or "{}") for _, c in sorted(calls.items()))
    finish = _finish(events) or ("tool_calls" if tool_calls else "stop")
    return Completion(
        text="".join(text),
        tool_calls=tool_calls,
        finish_reason=finish,
        usage=_reported(events) or NO_USAGE,
        refusal="".join(refusal) or None,
    )


async def _nothing() -> AsyncIterator[StreamEvent]:
    return
    yield  # an async generator with no events


async def _close(events: AsyncIterator[StreamEvent]) -> None:
    aclose = getattr(events, "aclose", None)
    if aclose is not None:
        await aclose()
