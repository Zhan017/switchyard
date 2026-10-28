"""The records writer never blocks or grows without bound when Postgres is away."""

from __future__ import annotations

import asyncio

import pytest

from switchyard.observability.metrics import Metrics
from switchyard.storage.records import PostgresRecords

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def dropped(metrics: Metrics, table: str) -> float:
    value = metrics.registry.get_sample_value("switchyard_records_dropped_total", {"table": table})
    return value or 0.0


async def test_a_full_queue_drops_and_counts() -> None:
    metrics = Metrics()
    records = PostgresRecords("postgresql://nobody@127.0.0.1:1/none", metrics, max_queued=3)
    for _ in range(5):
        records.add_request({"id": "x"}, [])
    assert records.queue.size() == 3
    assert dropped(metrics, "requests") == 2


async def test_unreachable_postgres_drops_rows_and_keeps_going() -> None:
    metrics = Metrics()
    records = PostgresRecords("postgresql://nobody@127.0.0.1:1/none", metrics, flush_s=0.05)
    await records.start()
    records.add_request({"id": "x"}, [{"request_id": "x"}])
    for _ in range(100):
        if dropped(metrics, "requests") == 1:
            break
        await asyncio.sleep(0.05)
    assert dropped(metrics, "requests") == 1 and dropped(metrics, "attempts") == 1
    assert records.queue.size() == 0
    await records.stop()
