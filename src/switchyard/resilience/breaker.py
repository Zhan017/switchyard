"""Circuit breakers, cooldowns and the retry budget, per provider and model (spec 7.3 to 7.5).

Everything here is synchronous and in process, so the request path never waits on the
network for it. With Redis configured, ``shared.SharedState`` syncs the counts and
breaker records in the background (spec 7.9).

States are ``closed`` and ``open``; there is no half-open state that lets user traffic
through. While open, the prober sends tiny requests on a fixed 10 s cadence and three
successes in a row close the breaker, so recovery takes at most
``P + (k - 1) * P + t_probe = 35 s`` with the defaults.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from switchyard.config import Resilience as Policy
from switchyard.resilience.health import (
    FAILURES,
    TTFT_EDGES,
    ModelHealth,
    histogram_index,
    percentile,
)

log = logging.getLogger("switchyard.breaker")

State = Literal["closed", "open"]
Force = Literal["open", "closed"]


@dataclass(frozen=True)
class Transition:
    key: str
    from_state: State
    to_state: State
    reason: str
    at: float  # wall clock
    # "local" if this replica made the change, "shared" if it was adopted from Redis.
    # Records and transition counters only count local changes, so replicas don't
    # double count.
    origin: str = "local"


@dataclass
class BreakerRecord:
    """The part of a model's state that replicas share through Redis."""

    state: State = "closed"
    forced: Force | None = None
    reason: str = ""
    version: int = 0  # bumped by every shared change, for compare-and-set
    changed_at: float = 0.0  # wall clock of the last change
    streak: int = 0  # successful probes in a row while open
    dirty: bool = False  # changed locally and not yet written to Redis

    @property
    def effective(self) -> State:
        return self.forced or self.state


@dataclass
class Cooldown:
    until: float  # monotonic
    reason: str  # "rate_limited" or "auth"


@dataclass
class ModelState:
    key: str
    health: ModelHealth
    breaker: BreakerRecord = field(default_factory=BreakerRecord)
    cooldown: Cooldown | None = None
    opened_mono: float | None = None  # when the breaker opened, for the probe schedule


class Breakers:
    def __init__(
        self,
        policy: Policy,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.policy = policy
        self.clock = clock
        self.wall = wall
        self.models: dict[str, ModelState] = {}
        self.listeners: list[Callable[[Transition], None]] = []
        self.on_cooldown: list[Callable[[str, str], None]] = []
        self.on_budget_exhausted: list[Callable[[str], None]] = []

    # Bookkeeping

    def buckets_kept(self) -> int:
        p = self.policy
        # Rule 3 looks back over two whole periods plus the current one.
        return max(p.window_buckets, 3 * p.ttft_period_buckets)

    def model(self, key: str) -> ModelState:
        state = self.models.get(key)
        if state is None:
            state = self.models[key] = ModelState(key, ModelHealth(self.buckets_kept()))
        return state

    def index(self) -> int:
        return int(self.wall() // self.policy.bucket_s)

    def retain(self, keys: Iterable[str]) -> None:
        """After a config reload: a removed model loses its state, a new one starts closed."""
        keep = set(keys)
        for key in [k for k in self.models if k not in keep]:
            del self.models[key]
        for key in keep:
            self.model(key).health.keep = self.buckets_kept()

    # Routing

    def skip_reason(self, key: str) -> str | None:
        """Why the router must skip this model now, or None if it can be tried."""
        state = self.model(key)
        if state.breaker.effective == "open":
            return "open"
        if state.cooldown is not None:
            if self.clock() < state.cooldown.until:
                return "cooldown"
            state.cooldown = None
        return None

    def retry_after(self, keys: Iterable[str]) -> float:
        """Seconds until one of these models could serve again, for Retry-After."""
        now = self.clock()
        waits = []
        for key in keys:
            state = self.model(key)
            if state.breaker.effective == "open":
                waits.append(self.policy.probe_interval_s)
            elif state.cooldown is not None:
                waits.append(max(0.0, state.cooldown.until - now))
        return max(1.0, math.ceil(min(waits))) if waits else 1.0

    # Recording

    def record_ttft(self, key: str, ttft_s: float, over_budget: bool) -> None:
        """A first-token sample. A first-token timeout is a sample over budget."""
        state = self.model(key)
        bucket = state.health.current(self.index())
        bucket.ttft_samples += 1
        bucket.ttft_hist[histogram_index(ttft_s)] += 1
        if over_budget:
            bucket.ttft_over += 1
            self._check(state)

    def record_outcome(self, key: str, outcome: str) -> None:
        """How an attempt ended: "ok", an error kind, or "content_filter"."""
        state = self.model(key)
        state.health.current(self.index()).outcomes[outcome] += 1
        if outcome == "ok":
            state.health.consecutive_failures = 0
        elif outcome in FAILURES:
            state.health.consecutive_failures += 1
            self._check(state)

    def note_attempt(self, key: str, *, retry: bool) -> None:
        bucket = self.model(key).health.current(self.index())
        if retry:
            bucket.retries += 1
        else:
            bucket.attempts += 1

    def allow_retry(self, key: str) -> bool:
        """The retry budget: same-provider retries add at most ``retry_budget_ratio`` of the
        model's first attempts in the window, plus a small floor."""
        p = self.policy
        state = self.model(key)
        now = self.index()
        window = state.health.total(range(now - p.window_buckets + 1, now + 1))
        allowed = p.retry_budget_ratio * window.attempts + p.retry_budget_floor
        if window.retries + 1 > allowed:
            for listener in self.on_budget_exhausted:
                listener(key)
            return False
        return True

    def cool_down(self, key: str, seconds: float | None, reason: str) -> None:
        """A 429 is a quota signal, not a health signal: skip the model until Retry-After.
        A 401 or 403 means Switchyard's own key is bad, so the model is skipped for a while
        and the failure is logged as an error."""
        if seconds is None:
            seconds = (
                self.policy.auth_cooldown_s if reason == "auth" else self.policy.cooldown_default_s
            )
        state = self.model(key)
        until = self.clock() + seconds
        if state.cooldown is None or until > state.cooldown.until:
            state.cooldown = Cooldown(until, reason)
        for listener in self.on_cooldown:
            listener(key, reason)

    # Trip rules

    def _check(self, state: ModelState) -> None:
        if state.breaker.state == "open" or state.breaker.forced is not None:
            return
        reason = self.trip_reason(state)
        if reason is not None:
            self._set(state, "open", reason)

    def trip_reason(self, state: ModelState) -> str | None:
        p = self.policy
        health = state.health
        if health.consecutive_failures >= p.trip_consecutive_failures:
            return f"{health.consecutive_failures} failures in a row"
        now = self.index()
        window = health.total(range(now - p.window_buckets + 1, now + 1))
        counted = window.counted
        if counted >= p.trip_min_requests and window.failures >= p.trip_failure_rate * counted:
            window_s = p.window_buckets * p.bucket_s
            return f"{window.failures} of {counted} failed in {window_s:g} s"
        # Rule 3: periods are aligned to multiples of the period length, and only whole
        # periods count. p95 over budget means more than 5% of samples over budget.
        size = p.ttft_period_buckets
        current = now // size
        slow = 0
        for period in (current - 1, current - 2):
            sample = health.total(range(period * size, (period + 1) * size))
            if sample.ttft_samples >= p.ttft_min_samples and sample.ttft_over > (
                0.05 * sample.ttft_samples
            ):
                slow += 1
        if slow == 2:
            return f"p95 time to first token over budget in two {size * p.bucket_s:g} s periods"
        return None

    # Transitions

    def _set(self, state: ModelState, to: State, reason: str) -> None:
        record = state.breaker
        before = record.effective
        record.state = to
        record.reason = reason
        record.changed_at = self.wall()
        record.streak = 0
        record.dirty = True
        self._after_change(state, before)

    def _after_change(self, state: ModelState, before: State) -> None:
        record = state.breaker
        if record.state == "open":
            if state.opened_mono is None:
                state.opened_mono = self.clock()
        else:
            state.opened_mono = None
            state.health.reset()
        after = record.effective
        if after != before:
            transition = Transition(state.key, before, after, record.reason, record.changed_at)
            log.log(
                logging.WARNING if after == "open" else logging.INFO,
                "breaker %s %s -> %s: %s",
                state.key,
                before,
                after,
                record.reason,
            )
            for listener in self.listeners:
                listener(transition)

    def probe_result(self, key: str, ok: bool) -> None:
        """Local probe accounting, used when Redis is not configured or unreachable."""
        state = self.model(key)
        record = state.breaker
        if record.state != "open":
            return
        if not ok:
            record.streak = 0
            return
        record.streak += 1
        if record.streak >= self.policy.probe_successes:
            self._set(state, "closed", f"{record.streak} probes in a row succeeded")

    def force(self, key: str, mode: Literal["open", "closed", "auto"]) -> None:
        """Admin override. ``closed`` also resets the window and the breaker itself, so
        returning to ``auto`` starts from a clean slate."""
        state = self.model(key)
        record = state.breaker
        before = record.effective
        record.forced = None if mode == "auto" else mode
        if mode == "closed":
            record.state = "closed"
            record.streak = 0
        record.reason = f"forced {mode} by admin"
        record.changed_at = self.wall()
        record.dirty = True
        if mode == "closed":
            state.cooldown = None
        self._after_change(state, before)

    def adopt(self, key: str, remote: BreakerRecord, origin: str = "shared") -> None:
        """Take a newer record from Redis, usually written by another replica."""
        state = self.model(key)
        before = state.breaker.effective
        was_open = state.breaker.state == "open"
        remote.dirty = False
        state.breaker = remote
        if remote.state == "open" and not was_open:
            state.opened_mono = self.clock()
        elif remote.state == "closed" and was_open:
            state.opened_mono = None
            state.health.reset()
        after = remote.effective
        if after != before:
            transition = Transition(key, before, after, remote.reason, remote.changed_at, origin)
            for listener in self.listeners:
                listener(transition)

    # Views

    def open_models(self) -> list[tuple[str, float]]:
        """Models the prober should probe: open and not forced, with when they opened."""
        return [
            (key, s.opened_mono)
            for key, s in self.models.items()
            if s.breaker.state == "open" and s.breaker.forced is None and s.opened_mono is not None
        ]

    def snapshot(self, key: str) -> dict[str, Any]:
        p = self.policy
        state = self.model(key)
        now = self.index()
        window = state.health.total(range(now - p.window_buckets + 1, now + 1))
        p50, p95 = percentile(window.ttft_hist, 0.5), percentile(window.ttft_hist, 0.95)
        record = state.breaker
        cooldown = None
        if state.cooldown is not None and self.clock() < state.cooldown.until:
            cooldown = {
                "reason": state.cooldown.reason,
                "remaining_s": round(state.cooldown.until - self.clock(), 2),
            }
        return {
            "breaker": {
                "state": record.effective,
                "forced": record.forced,
                "reason": record.reason,
                "probe_streak": record.streak,
                "changed_at": record.changed_at or None,
            },
            "cooldown": cooldown,
            "health": {
                "window_s": p.window_buckets * p.bucket_s,
                "requests": window.counted,
                "failures": window.failures,
                "outcomes": dict(window.outcomes),
                "consecutive_failures": state.health.consecutive_failures,
                "ttft_p50_s": _seconds(p50),
                "ttft_p95_s": _seconds(p95),
                "attempts": window.attempts,
                "retries": window.retries,
            },
        }


def _seconds(value: float | None) -> float | str | None:
    """Histogram percentiles are bucket bounds; the open last bucket has no bound."""
    if value is not None and math.isinf(value):
        return f">{TTFT_EDGES[-1]:g}"
    return value
