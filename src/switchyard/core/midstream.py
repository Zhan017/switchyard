"""Mid-stream failover: finish an answer on another model after the client has started
reading it (spec 7.6).

The relay calls ``Continuer.resume`` when the provider dies after text went out. It closes
the account of the failed attempt, picks the next healthy candidate, sends the
continuation request for that candidate's strategy, and, for the repaired strategies,
buffers the first stretch of the continuation and runs seam repair on it. The relay
forwards what comes back as if nothing had happened.
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from switchyard.core.attempts import Attempt, Opened, Runner
from switchyard.core.router import Candidate
from switchyard.core.types import (
    ChatRequest,
    RefusalDelta,
    StreamEvent,
    TextDelta,
    ToolCallArgs,
    ToolCallStart,
)
from switchyard.providers.errors import ProviderError
from switchyard.streaming.continuation import (
    REPAIRED,
    Strategy,
    context_text,
    continuation_request,
)
from switchyard.streaming.relay import Segment
from switchyard.streaming.seam import is_meta_reply, repair

# How much of a continuation seam repair sees before anything is sent: at least 200
# characters, then on to the end of the first sentence, at most 400. The spec's first
# rule, "the first sentence or 200 characters, whichever comes first", gave a different
# result from repairing the whole continuation on 4 of the 90 real joins from the week 1
# seam test, because a sentence can end before the copied or repeated words do. This rule
# gave the same result on all 90.
SEAM_MIN_CHARS = 200
SEAM_MAX_CHARS = 400
_SENTENCE_END = re.compile(r"[.!?](?=\s)|\n")


@dataclass
class Resumed:
    """A continuation ready to forward: repaired first events, then the rest."""

    attempt: Attempt
    head: list[StreamEvent]
    events: AsyncIterator[StreamEvent]
    strategy: Strategy
    seam_actions: tuple[str, ...] = ()
    seam_latency_s: float | None = None

    @property
    def comment(self) -> str:
        return f"continuing on {self.attempt.key} with {self.strategy}"


@dataclass
class Failover:
    """One mid-stream failover, for metrics and records."""

    from_key: str
    to_key: str
    strategy: str
    seam_actions: tuple[str, ...]
    seam_latency_s: float | None


@dataclass
class Continuer:
    runner: Runner
    candidates: list[Candidate]
    request: ChatRequest
    current: Attempt
    failovers: list[Failover] = field(default_factory=list)
    refusal: str | None = None  # why the answer wasn't continued, when it wasn't

    async def resume(self, error: ProviderError, emitted: str, segment: Segment) -> Resumed | None:
        """Continue after ``error`` ended the current attempt, or return None to end the
        stream with the error event."""
        failed = self.current
        failed.delivered = True
        failed.error = error
        self.runner.settle(failed, segment.usage, segment.text)
        self.runner.finish(failed, error.kind.value)
        if self.request.stop:
            # A stop sequence could span the join, and the continuation would not see it.
            self.refusal = "stop_sequences"
            return None
        tried = {a.key for a in self.runner.attempts if a.outcome != "ok"}
        limit = self.runner.policy.max_midstream_failovers
        for candidate in self.candidates:
            if self._midstream_attempts() >= limit:
                self.refusal = "midstream_limit"
                return None
            if candidate.key in tried or self.runner.breakers.skip_reason(candidate.key):
                continue
            strategy: Strategy = candidate.options.continuation
            if strategy == "error":
                self.refusal = "strategy_error"
                return None
            resumed = await self._try(candidate, strategy, emitted)
            if resumed is not None:
                self.failovers.append(
                    Failover(
                        failed.key,
                        candidate.key,
                        strategy,
                        resumed.seam_actions,
                        resumed.seam_latency_s,
                    )
                )
                self.current = resumed.attempt
                return resumed
        self.refusal = self.refusal or "no_candidate"
        return None

    def _midstream_attempts(self) -> int:
        return sum(1 for a in self.runner.attempts if a.role == "midstream")

    async def _try(self, candidate: Candidate, strategy: Strategy, emitted: str) -> Resumed | None:
        request = continuation_request(strategy, self.request, emitted)
        try:
            opened = await self.runner.try_midstream(candidate, request, strategy, len(emitted))
        except ProviderError:
            return None  # recorded by the runner; try the next candidate
        if strategy not in REPAIRED:
            return Resumed(opened.attempt, opened.head, opened.events, strategy)
        return await self._repaired(opened, strategy, emitted)

    async def _repaired(self, opened: Opened, strategy: Strategy, emitted: str) -> Resumed | None:
        attempt = opened.attempt
        started = time.monotonic()
        text: list[str] = []
        held: list[StreamEvent] = []  # events after the buffered text, in order
        events = opened.all_events()
        try:
            async for event in events:
                if isinstance(event, TextDelta) and not held:
                    text.append(event.text)
                    if _seam_ready("".join(text)):
                        break
                    continue
                held.append(event)
                if isinstance(event, ToolCallStart | ToolCallArgs | RefusalDelta):
                    break  # the text part is over; repair what there is
        except ProviderError as error:
            self.runner.settle(attempt, None, "".join(text))
            attempt.error = error
            self.runner.finish(attempt, error.kind.value)
            return None
        except BaseException:
            await _close(events)  # the client left while the continuation was buffering
            raise
        buffered = "".join(text)
        if is_meta_reply(buffered):
            # The model talked about the task instead of continuing it. Never send that.
            await _close(events)
            self.runner.settle(attempt, None, buffered)
            self.runner.finish(attempt, "meta_reply")
            return None
        result = repair(
            emitted, buffered, context=context_text(self.request), anchored=strategy == "anchored"
        )
        head: list[StreamEvent] = [TextDelta(result.text)] if result.text else []
        head.extend(held)
        return Resumed(
            attempt,
            head,
            events,
            strategy,
            seam_actions=result.actions or ("none",),
            seam_latency_s=time.monotonic() - started,
        )


def _seam_ready(text: str) -> bool:
    if len(text) >= SEAM_MAX_CHARS:
        return True
    if len(text) < SEAM_MIN_CHARS:
        return False
    return _SENTENCE_END.search(text, SEAM_MIN_CHARS) is not None


async def _close(events: AsyncIterator[StreamEvent]) -> None:
    aclose = getattr(events, "aclose", None)
    if aclose is not None:
        await aclose()
