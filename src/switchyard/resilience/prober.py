"""Active probes for open breakers (spec 7.4).

While a breaker is open no user traffic reaches the model. Instead a tiny request
(``max_tokens: 1``) goes out every ``probe_interval_s`` on a fixed schedule counted from
when the breaker opened, each with a ``probe_timeout_s`` limit. Three successes in a row
close the breaker. The schedule doesn't drift with probe duration, which is what makes the
worst case ``P + (k - 1) * P + t_probe`` hold.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable

from switchyard.accounting.usage import complete_usage
from switchyard.api.state import GatewayState
from switchyard.core.types import ChatRequest, Finish, Message, TextDelta, Usage, UsageReport
from switchyard.core.watch import StreamTiming, watch
from switchyard.observability.metrics import Metrics
from switchyard.providers.errors import ProviderError
from switchyard.resilience.breaker import Breakers, Transition
from switchyard.resilience.shared import SharedState

log = logging.getLogger("switchyard.prober")

PROBE_REQUEST = ChatRequest(messages=(Message("user", "ping"),), max_tokens=1)
PROBE_TENANT = "switchyard"
PROBE_FEATURE = "probe"


class Prober:
    def __init__(
        self,
        breakers: Breakers,
        state: Callable[[], GatewayState],
        metrics: Metrics,
        shared: SharedState | None = None,
    ) -> None:
        self.breakers = breakers
        self.state = state
        self.metrics = metrics
        self.shared = shared
        self.wake = asyncio.Event()
        self.schedule: dict[str, tuple[float, float]] = {}  # key -> (opened_at, next probe)
        self.running: dict[str, asyncio.Task[None]] = {}
        self.task: asyncio.Task[None] | None = None
        breakers.listeners.append(self._on_transition)

    def _on_transition(self, transition: Transition) -> None:
        if transition.to_state == "open":
            self.wake.set()

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="switchyard-prober")

    async def stop(self) -> None:
        tasks = [t for t in (self.task, *self.running.values()) if t is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        interval = self.breakers.policy.probe_interval_s
        while True:
            now = time.monotonic()
            open_now = dict(self.breakers.open_models())
            for key in [k for k in self.schedule if k not in open_now]:
                del self.schedule[key]
            wait = 3600.0
            for key, opened in open_now.items():
                interval = self.breakers.policy.probe_interval_s
                known = self.schedule.get(key)
                next_at = opened + interval if known is None or known[0] != opened else known[1]
                if next_at <= now:
                    if key not in self.running:
                        self.running[key] = asyncio.create_task(self._probe(key))
                    while next_at <= now:
                        next_at += interval
                self.schedule[key] = (opened, next_at)
                wait = min(wait, next_at - now)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), timeout=max(0.01, wait))
            self.wake.clear()

    async def _probe(self, key: str) -> None:
        try:
            if self.shared is not None and not await self.shared.claim_probe(key):
                return  # another replica probes this model this round
            ok = await self.probe_once(key)
            if self.shared is not None:
                await self.shared.report_probe(key, ok)
            else:
                self.breakers.probe_result(key, ok)
        except Exception:
            log.exception("probe of %s failed unexpectedly", key)
        finally:
            self.running.pop(key, None)

    async def probe_once(self, key: str) -> bool:
        state = self.state()
        settings = state.settings
        if key not in settings.models:
            return False
        provider, model = settings.provider_of(key)
        adapter = state.adapters[provider]
        timeout = self.breakers.policy.probe_timeout_s
        timing = StreamTiming()
        events = watch(
            adapter.stream(model, settings.models[key], PROBE_REQUEST),
            ttft_s=timeout,
            stall_s=timeout,
            deadline=time.monotonic() + timeout,
            timing=timing,
        )
        finished = False
        text = ""
        reported: Usage | None = None
        outcome = "ok"
        try:
            async for event in events:
                if isinstance(event, Finish):
                    finished = True
                elif isinstance(event, TextDelta):
                    text += event.text
                elif isinstance(event, UsageReport):
                    reported = event.usage
        except ProviderError as error:
            outcome = error.kind.value
        if outcome == "ok" and not finished:
            outcome = "connection"
        usage = complete_usage(reported, PROBE_REQUEST, text) if outcome == "ok" else Usage()
        cost = state.prices.cost(key, usage) or 0.0
        estimated = "true" if usage.estimated else "false"
        labels = (PROBE_TENANT, PROBE_FEATURE, provider, model)
        self.metrics.attempts.labels(provider, model, "probe", outcome).inc()
        self.metrics.tokens.labels(*labels, "input", estimated).inc(usage.input_tokens)
        self.metrics.tokens.labels(*labels, "output", estimated).inc(usage.output_tokens)
        self.metrics.cost.labels(*labels, "probe", estimated).inc(cost)
        log.info("probe %s: %s", key, outcome)
        return outcome == "ok"
