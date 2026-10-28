"""Real Redis and Postgres. Skipped unless SWITCHYARD_TEST_REDIS_URL or
SWITCHYARD_TEST_DATABASE_URL is set; CI runs both as service containers."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator

import asyncpg
import pytest
import redis.asyncio as redis

from switchyard.config import Resilience
from switchyard.observability.metrics import Metrics
from switchyard.resilience.breaker import Breakers, Transition
from switchyard.resilience.shared import SharedState
from switchyard.storage.records import PostgresRecords, utc

pytestmark = pytest.mark.anyio
REDIS_URL = os.environ.get("SWITCHYARD_TEST_REDIS_URL")
DATABASE_URL = os.environ.get("SWITCHYARD_TEST_DATABASE_URL")
KEY = "p/m"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def redis_url() -> AsyncIterator[str]:
    if not REDIS_URL:
        pytest.skip("SWITCHYARD_TEST_REDIS_URL not set")
    client = redis.Redis.from_url(REDIS_URL)
    await client.flushdb()
    await client.aclose()
    yield REDIS_URL


@pytest.fixture
async def database_url() -> AsyncIterator[str]:
    if not DATABASE_URL:
        pytest.skip("SWITCHYARD_TEST_DATABASE_URL not set")
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("DROP TABLE IF EXISTS requests, attempts, breaker_events")
    await conn.close()
    yield DATABASE_URL


async def test_lua_scripts_on_real_redis(redis_url: str) -> None:
    replicas = []
    for _ in range(2):
        breakers = Breakers(Resilience())
        breakers.model(KEY)
        replicas.append((breakers, SharedState.from_url(redis_url, breakers, Metrics())))
    (a, shared_a), (b, shared_b) = replicas
    for _ in range(5):
        a.record_outcome(KEY, "connection")
    assert await shared_a.sync() and await shared_b.sync()
    assert b.skip_reason(KEY) == "open"
    for shared in (shared_a, shared_b, shared_a):
        await shared.report_probe(KEY, True)
    assert a.skip_reason(KEY) is None
    await shared_b.sync()
    assert b.skip_reason(KEY) is None
    b.force(KEY, "open")
    await shared_b.sync()
    await shared_a.sync()
    assert a.model(KEY).breaker.forced == "open"
    for _, shared in replicas:
        await shared.stop()


async def test_records_reach_postgres(database_url: str) -> None:
    metrics = Metrics()
    records = PostgresRecords(database_url, metrics, flush_s=0.05)
    await records.start()
    now = time.time()
    request = {
        "id": "sy-test",
        "tenant": "t",
        "feature": "f",
        "route_group": "mock",
        "status": "ok",
        "started_at": utc(now),
        "finished_at": utc(now + 0.2),
        "total_ms": 200.0,
        "input_tokens": 10,
        "output_tokens": 20,
        "cost_usd": 0.001,
        "usage_estimated": False,
        "failovers": 1,
        "midstream_failovers": 0,
    }
    attempt = {
        "request_id": "sy-test",
        "role": "primary",
        "provider": "mock-a",
        "model": "mock",
        "started_at": utc(now),
        "outcome": "server_error",
        "input_tokens": 0,
        "output_tokens": 0,
        "wasted_tokens": 0,
        "cost_usd": 0.0,
        "usage_estimated": False,
        "emitted_chars_before": 0,
    }
    records.add_request(request, [{**attempt, "n": 1}, {**attempt, "n": 2, "outcome": "ok"}])
    records.add_breaker_event(Transition(KEY, "closed", "open", "5 failures in a row", now))
    for _ in range(100):
        if records.written["attempts"] == 2 and records.written["breaker_events"] == 1:
            break
        await asyncio.sleep(0.05)
    await records.stop()
    conn = await asyncpg.connect(database_url)
    try:
        row = await conn.fetchrow("SELECT failovers, cost_usd FROM requests WHERE id = 'sy-test'")
        assert row is not None and row["failovers"] == 1
        outcomes = await conn.fetch("SELECT outcome FROM attempts ORDER BY n")
        assert [r["outcome"] for r in outcomes] == ["server_error", "ok"]
        assert await conn.fetchval("SELECT reason FROM breaker_events") == "5 failures in a row"
    finally:
        await conn.close()
