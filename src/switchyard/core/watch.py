"""Time limits on a provider's stream: first token, stalls, and the whole request.

Only content counts as progress. Anthropic pings and DeepSeek keep-alive comments never
reach this layer, so a provider that keeps the connection open without writing anything
still trips the limits.

Each wait gets its own timer. One timer kept across events would be cheaper, but the
stream is read first by the request handler and then by the response writer, which are
different tasks, and a timer cancels the task that armed it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from switchyard.core.types import CONTENT_EVENTS, Finish, StreamEvent
from switchyard.providers.errors import ErrorKind, ProviderError


@dataclass
class StreamTiming:
    started: float = field(default_factory=time.monotonic)
    first_content: float | None = None

    @property
    def ttft_s(self) -> float | None:
        return None if self.first_content is None else self.first_content - self.started


async def watch(
    events: AsyncIterator[StreamEvent],
    *,
    ttft_s: float,
    stall_s: float,
    deadline: float,
    timing: StreamTiming,
) -> AsyncIterator[StreamEvent]:
    """Yield events from ``events`` until it ends, raising ProviderError on a missed limit.

    ``deadline`` is an absolute time.monotonic() value for the whole request.
    """
    iterator = aiter(events)
    finished = False
    try:
        while True:
            waiting_for_first = timing.first_content is None
            limit = ttft_s if waiting_for_first else stall_s
            remaining = deadline - time.monotonic()
            hits_deadline = remaining <= limit
            try:
                async with asyncio.timeout(max(0.0, min(limit, remaining))):
                    event = await anext(iterator)
            except StopAsyncIteration:
                return
            except TimeoutError:
                if finished:
                    # The answer is complete and only the trailing usage or [DONE] is
                    # late. End normally; usage is estimated if it never came.
                    return
                if hits_deadline:
                    raise ProviderError(ErrorKind.DEADLINE, "request deadline exceeded") from None
                if waiting_for_first:
                    raise ProviderError(
                        ErrorKind.TIMEOUT, f"no first token within {ttft_s:g} s"
                    ) from None
                raise ProviderError(ErrorKind.STALL, f"no tokens for {stall_s:g} s") from None
            if waiting_for_first and isinstance(event, CONTENT_EVENTS):
                timing.first_content = time.monotonic()
            elif isinstance(event, Finish):
                finished = True
            yield event
    finally:
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            await aclose()
