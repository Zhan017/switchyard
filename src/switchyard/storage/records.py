"""Request, attempt and breaker records in Postgres (spec section 8).

Writes never block a request: rows go into a bounded in-memory queue and a background
task copies them in batches, every second or every 500 rows. If Postgres is slow or down,
the queue fills, new rows are dropped and counted, and metrics keep flowing. Prompts and
answers are never stored, only a hash and a length.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import asyncpg

from switchyard.observability.metrics import Metrics
from switchyard.resilience.breaker import Transition

log = logging.getLogger("switchyard.records")

REQUEST_COLUMNS = (
    "id",
    "tenant",
    "feature",
    "route_group",
    "status",
    "started_at",
    "finished_at",
    "ttft_ms",
    "total_ms",
    "served_provider",
    "served_model",
    "strategy",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "usage_estimated",
    "failovers",
    "midstream_failovers",
    "seam_latency_ms",
    "seam_action",
    "prompt_hash",
    "prompt_chars",
    "response_chars",
    "error_code",
)
ATTEMPT_COLUMNS = (
    "request_id",
    "n",
    "role",
    "provider",
    "model",
    "started_at",
    "ttft_ms",
    "ended_at",
    "outcome",
    "input_tokens",
    "output_tokens",
    "wasted_tokens",
    "cost_usd",
    "usage_estimated",
    "emitted_chars_before",
)
BREAKER_COLUMNS = ("provider_model", "from_state", "to_state", "reason", "at")

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id text NOT NULL,
    tenant text NOT NULL,
    feature text NOT NULL,
    route_group text NOT NULL,
    status text NOT NULL,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    ttft_ms real,
    total_ms real NOT NULL,
    served_provider text,
    served_model text,
    strategy text,
    input_tokens integer NOT NULL,
    output_tokens integer NOT NULL,
    cost_usd double precision NOT NULL,
    usage_estimated boolean NOT NULL,
    failovers integer NOT NULL,
    midstream_failovers integer NOT NULL,
    seam_latency_ms real,
    seam_action text,
    prompt_hash text,
    prompt_chars integer,
    response_chars integer,
    error_code text
);
CREATE INDEX IF NOT EXISTS requests_started_at ON requests (started_at);
CREATE INDEX IF NOT EXISTS requests_id ON requests (id);
CREATE INDEX IF NOT EXISTS requests_tenant_feature ON requests (tenant, feature, started_at);

CREATE TABLE IF NOT EXISTS attempts (
    id bigserial PRIMARY KEY,
    request_id text NOT NULL,
    n integer NOT NULL,
    role text NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    started_at timestamptz NOT NULL,
    ttft_ms real,
    ended_at timestamptz,
    outcome text NOT NULL,
    input_tokens integer NOT NULL,
    output_tokens integer NOT NULL,
    wasted_tokens integer NOT NULL,
    cost_usd double precision NOT NULL,
    usage_estimated boolean NOT NULL,
    emitted_chars_before integer NOT NULL
);
CREATE INDEX IF NOT EXISTS attempts_request_id ON attempts (request_id);
CREATE INDEX IF NOT EXISTS attempts_started_at ON attempts (started_at);

CREATE TABLE IF NOT EXISTS breaker_events (
    id bigserial PRIMARY KEY,
    provider_model text NOT NULL,
    from_state text NOT NULL,
    to_state text NOT NULL,
    reason text NOT NULL,
    at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS breaker_events_at ON breaker_events (at);
"""

RETENTION_DAYS = 30
TABLES = ("requests", "attempts", "breaker_events")


def utc(wall: float) -> datetime:
    return datetime.fromtimestamp(wall, UTC)


class Records(Protocol):
    enabled: bool

    def add_request(self, row: dict[str, Any], attempts: list[dict[str, Any]]) -> None: ...

    def add_breaker_event(self, transition: Transition) -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class NoRecords:
    """Used when SWITCHYARD_DATABASE_URL isn't set."""

    enabled = False

    def add_request(self, row: dict[str, Any], attempts: list[dict[str, Any]]) -> None:
        return None

    def add_breaker_event(self, transition: Transition) -> None:
        return None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


@dataclass
class _Queue:
    rows: dict[str, deque[tuple[Any, ...]]] = field(
        default_factory=lambda: {t: deque() for t in TABLES}
    )

    def size(self) -> int:
        return sum(len(q) for q in self.rows.values())


class PostgresRecords:
    enabled = True

    def __init__(
        self,
        dsn: str,
        metrics: Metrics,
        *,
        flush_s: float = 1.0,
        batch: int = 500,
        max_queued: int = 20_000,
        prune_every_s: float = 3600.0,
    ) -> None:
        self.dsn = dsn
        self.metrics = metrics
        self.flush_s = flush_s
        self.batch = batch
        self.max_queued = max_queued
        self.prune_every_s = prune_every_s
        self.queue = _Queue()
        self.wake = asyncio.Event()
        self.pool: asyncpg.Pool | None = None
        self.task: asyncio.Task[None] | None = None
        self.written = {t: 0 for t in TABLES}
        self._stopping = False
        self._last_prune = 0.0

    # Producers, called on the request path: no awaits, no I/O.

    def _put(self, table: str, row: tuple[Any, ...]) -> None:
        if self.queue.size() >= self.max_queued:
            self.metrics.records_dropped.labels(table).inc()
            return
        self.queue.rows[table].append(row)
        if self.queue.size() >= self.batch:
            self.wake.set()

    def add_request(self, row: dict[str, Any], attempts: list[dict[str, Any]]) -> None:
        self._put("requests", tuple(row.get(c) for c in REQUEST_COLUMNS))
        for attempt in attempts:
            self._put("attempts", tuple(attempt.get(c) for c in ATTEMPT_COLUMNS))

    def add_breaker_event(self, transition: Transition) -> None:
        self._put(
            "breaker_events",
            (
                transition.key,
                transition.from_state,
                transition.to_state,
                transition.reason,
                utc(transition.at),
            ),
        )

    # The writer

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="switchyard-records")

    async def stop(self) -> None:
        self._stopping = True
        self.wake.set()
        if self.task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                try:
                    await asyncio.wait_for(self.task, timeout=5)
                except TimeoutError:
                    self.task.cancel()
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    async def _connect(self) -> asyncpg.Pool:
        if self.pool is None:
            pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=2, timeout=5)
            async with pool.acquire() as conn:
                await conn.execute(SCHEMA)
            self.pool = pool
            log.info("connected to Postgres for request records")
        return self.pool

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), timeout=self.flush_s)
            self.wake.clear()
            stopping = self._stopping
            try:
                pool = await self._connect()
                while self.queue.size():
                    await self._flush(pool)
                if time.monotonic() - self._last_prune >= self.prune_every_s:
                    await self._prune(pool)
                backoff = 1.0
            except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError, TimeoutError) as e:
                self._drop_all(e)
                if self.pool is not None:
                    pool_to_close, self.pool = self.pool, None
                    pool_to_close.terminate()
                if not stopping:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
            if stopping:
                return

    async def _flush(self, pool: asyncpg.Pool) -> None:
        batches: dict[str, list[tuple[Any, ...]]] = {}
        for table in TABLES:
            queued = self.queue.rows[table]
            take = [queued.popleft() for _ in range(min(len(queued), self.batch))]
            if take:
                batches[table] = take
        columns = {
            "requests": REQUEST_COLUMNS,
            "attempts": ATTEMPT_COLUMNS,
            "breaker_events": BREAKER_COLUMNS,
        }
        try:
            async with pool.acquire() as conn:
                for table, rows in batches.items():
                    await conn.copy_records_to_table(table, records=rows, columns=columns[table])
                    self.written[table] += len(rows)
        except BaseException:
            for table, taken in batches.items():
                self.metrics.records_dropped.labels(table).inc(len(taken))
            raise

    async def _prune(self, pool: asyncpg.Pool) -> None:
        self._last_prune = time.monotonic()
        interval = f"{RETENTION_DAYS} days"
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM attempts WHERE started_at < now() - $1::interval", interval
            )
            await conn.execute(
                "DELETE FROM requests WHERE started_at < now() - $1::interval", interval
            )
            await conn.execute(
                "DELETE FROM breaker_events WHERE at < now() - $1::interval", interval
            )

    def _drop_all(self, error: BaseException) -> None:
        dropped = 0
        for table in TABLES:
            rows = self.queue.rows[table]
            if rows:
                self.metrics.records_dropped.labels(table).inc(len(rows))
                dropped += len(rows)
                rows.clear()
        log.warning("Postgres write failed, %d queued rows dropped: %s", dropped, error)
