"""Breakers and health shared through Redis, with fail-open (spec 7.4, 7.9). Runs the real
Lua scripts on fakeredis."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import fakeredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from switchyard.config import Resilience
from switchyard.observability.metrics import Metrics
from switchyard.resilience.breaker import Breakers
from switchyard.resilience.shared import SharedState, breaker_key

pytestmark = pytest.mark.anyio
KEY = "p/m"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_020.0

    def __call__(self) -> float:
        return self.now


def replica(server: fakeredis.FakeServer, clock: Clock) -> tuple[Breakers, SharedState]:
    breakers = Breakers(Resilience(), clock=clock, wall=clock)
    breakers.model(KEY)
    client = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    return breakers, SharedState(client, breakers, Metrics())


def fail(breakers: Breakers, times: int, outcome: str = "connection") -> None:
    for _ in range(times):
        breakers.record_outcome(KEY, outcome)


async def test_a_trip_on_one_replica_reaches_the_other() -> None:
    server, clock = fakeredis.FakeServer(), Clock()
    a, shared_a = replica(server, clock)
    b, shared_b = replica(server, clock)
    fail(a, 5)
    assert a.skip_reason(KEY) == "open"
    assert b.skip_reason(KEY) is None
    assert await shared_a.sync()
    assert await shared_b.sync()
    assert b.skip_reason(KEY) == "open"
    assert b.model(KEY).breaker.version == a.model(KEY).breaker.version == 1


async def test_health_counts_are_merged_across_replicas() -> None:
    server, clock = fakeredis.FakeServer(), Clock()
    a, shared_a = replica(server, clock)
    b, shared_b = replica(server, clock)
    # Each replica alone: 4 of 8 and 2 of 4 failed, below 10 requests. Together: 6 of 12.
    for outcome in ["ok", "server_error"] * 4:
        a.record_outcome(KEY, outcome)
    for outcome in ["ok", "server_error"] * 2:
        b.record_outcome(KEY, outcome)
    await shared_a.sync()
    await shared_b.sync()
    await shared_a.sync()
    assert a.snapshot(KEY)["health"]["requests"] == 12
    assert a.skip_reason(KEY) is None
    a.record_outcome(KEY, "server_error")  # the next failure trips on the merged counts
    assert a.skip_reason(KEY) == "open"
    assert "of 13 failed" in a.model(KEY).breaker.reason


async def test_a_restart_keeps_the_shared_state() -> None:
    server, clock = fakeredis.FakeServer(), Clock()
    a, shared_a = replica(server, clock)
    fail(a, 5)
    await shared_a.sync()
    fresh, shared_fresh = replica(server, clock)
    await shared_fresh.sync()
    assert fresh.skip_reason(KEY) == "open"
    assert fresh.open_models()  # and the new replica will probe it


async def test_redis_down_fails_open_and_keeps_unsent_counts() -> None:
    server, clock = fakeredis.FakeServer(), Clock()
    a, shared_a = replica(server, clock)
    server.connected = False
    a.record_outcome(KEY, "ok")
    a.record_outcome(KEY, "server_error")
    assert not await shared_a.sync()
    assert not shared_a.up
    fail(a, 5)  # decisions keep working from the local copy
    assert a.skip_reason(KEY) == "open"
    server.connected = True
    assert await shared_a.sync()
    assert shared_a.up
    assert a.snapshot(KEY)["health"]["requests"] == 7  # nothing lost, nothing doubled
    record = await shared_a.client.hgetall(breaker_key(KEY))
    assert record["state"] == "open"  # the trip made while Redis was down was written


async def test_probe_streak_survives_a_change_of_prober() -> None:
    server, clock = fakeredis.FakeServer(), Clock()
    a, shared_a = replica(server, clock)
    b, shared_b = replica(server, clock)
    fail(a, 5)
    await shared_a.sync()
    await shared_b.sync()
    assert await shared_a.claim_probe(KEY)
    assert not await shared_b.claim_probe(KEY)  # one prober per round
    await shared_a.report_probe(KEY, True)
    await shared_b.report_probe(KEY, True)
    await shared_a.report_probe(KEY, True)
    assert a.skip_reason(KEY) is None
    await shared_b.sync()
    assert b.skip_reason(KEY) is None
    # Closing cleared the shared window, so the old failures can't trip it again.
    assert b.snapshot(KEY)["health"]["failures"] == 0


async def test_a_failed_probe_resets_the_shared_streak() -> None:
    server, clock = fakeredis.FakeServer(), Clock()
    a, shared_a = replica(server, clock)
    fail(a, 5)
    await shared_a.sync()
    for ok in (True, True, False, True, True):
        await shared_a.report_probe(KEY, ok)
    assert a.skip_reason(KEY) == "open"
    await shared_a.report_probe(KEY, True)
    assert a.skip_reason(KEY) is None


async def test_a_local_change_during_a_write_is_not_lost() -> None:
    """Forcing closed and straight back to auto must end in auto, even when the second
    change lands while the first one is being written to Redis."""
    server, clock = fakeredis.FakeServer(), Clock()
    a, shared_a = replica(server, clock)
    script: Callable[..., Awaitable[Any]] = shared_a._transition

    async def slow_write(**kwargs: Any) -> Any:
        result = await script(**kwargs)
        clock.now += 0.01
        a.force(KEY, "auto")  # arrives while the write is in flight
        return result

    a.force(KEY, "closed")
    shared_a._transition = slow_write  # type: ignore[assignment]
    await shared_a.sync()
    assert a.model(KEY).breaker.forced is None
    shared_a._transition = script  # type: ignore[assignment]
    await shared_a.sync()
    record = await shared_a.client.hgetall(breaker_key(KEY))
    assert record["forced"] == ""
    assert not a.model(KEY).breaker.dirty


async def test_the_newer_change_wins_after_a_split() -> None:
    server, clock = fakeredis.FakeServer(), Clock()
    a, shared_a = replica(server, clock)
    b, shared_b = replica(server, clock)
    server.connected = False
    fail(a, 5)  # a opens while Redis is away
    clock.now += 1
    b.force(KEY, "closed")  # b forces closed a second later
    await shared_a.sync()
    await shared_b.sync()
    server.connected = True
    await shared_a.sync()
    await shared_b.sync()
    await shared_a.sync()
    assert a.model(KEY).breaker.forced == "closed"
    assert b.model(KEY).breaker.forced == "closed"


async def test_connection_errors_are_counted() -> None:
    server, clock = fakeredis.FakeServer(), Clock()
    _, shared = replica(server, clock)

    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RedisConnectionError("down")

    shared.client.set = broken  # type: ignore[method-assign]
    shared.up = True
    assert await shared.claim_probe(KEY)  # can't coordinate: probe locally
    assert not shared.up
