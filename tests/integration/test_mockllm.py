"""mockllm's own faults: the gate that refuses connections, flapping, replay, stats."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import aiohttp
import pytest

from mockllm.__main__ import GatedMock

pytestmark = pytest.mark.anyio
BODY = {"model": "mock-a", "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def mock() -> AsyncIterator[GatedMock]:
    gated = GatedMock("127.0.0.1", 0, "127.0.0.1", 0)
    await gated.start()
    yield gated
    await gated.stop()


def urls(mock: GatedMock) -> tuple[str, str]:
    return f"http://127.0.0.1:{mock.port}", f"http://127.0.0.1:{mock.control_port}"


async def test_refuse_connections_resets_pooled_connections_then_recovers(
    mock: GatedMock,
) -> None:
    public, control = urls(mock)
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{public}/v1/chat/completions", json=BODY) as response:
            assert response.status == 200  # the pool now holds a keep-alive connection
        async with session.post(
            f"{control}/chaos", json={"mode": "refuse_connections", "duration_s": 0.5}
        ) as response:
            assert response.status == 200
        for _ in range(2):  # the pooled connection is reset, then a new one is refused
            with pytest.raises(aiohttp.ClientConnectionError):
                async with session.post(f"{public}/v1/chat/completions", json=BODY) as r:
                    await r.read()
        async with session.get(f"{control}/stats") as response:
            assert (await response.json())["refusing"] is True
        await asyncio.sleep(0.7)
        async with session.post(f"{public}/v1/chat/completions", json=BODY) as response:
            assert response.status == 200


async def test_clearing_faults_accepts_again(mock: GatedMock) -> None:
    public, control = urls(mock)
    async with aiohttp.ClientSession() as session:
        await session.post(f"{control}/chaos", json={"mode": "refuse_connections"})
        with pytest.raises(aiohttp.ClientConnectionError):
            await session.post(f"{public}/v1/chat/completions", json=BODY)
        await session.delete(f"{control}/chaos")
        async with session.post(f"{public}/v1/chat/completions", json=BODY) as response:
            assert response.status == 200


async def test_probes_are_counted_apart(mock: GatedMock) -> None:
    public, control = urls(mock)
    async with aiohttp.ClientSession() as session:
        await session.post(f"{public}/v1/chat/completions", json=BODY)
        await session.post(f"{public}/v1/chat/completions", json={**BODY, "max_tokens": 1})
        async with session.get(f"{control}/stats") as response:
            stats = await response.json()
    assert stats["requests"] == 2 and stats["probes"] == 1


async def test_replay_plays_a_fixture_and_can_cut_it(mock: GatedMock) -> None:
    public, control = urls(mock)
    params = {"file": "openai_text.sse", "cut_after_events": 2, "delay_ms": 0}
    async with aiohttp.ClientSession() as session:
        await session.post(f"{control}/chaos", json={"mode": "replay", "params": params})
        async with session.post(
            f"{public}/v1/chat/completions", json={**BODY, "stream": True}
        ) as response:
            text = await response.text()
    assert text.count("data: ") == 2 and "[DONE]" not in text


async def test_seeded_error_rate_repeats(mock: GatedMock) -> None:
    public, control = urls(mock)

    async def run() -> list[int]:
        fault = {"mode": "error_rate", "params": {"rate": 0.5, "status": 500, "seed": 3}}
        async with aiohttp.ClientSession() as session:
            await session.delete(f"{control}/chaos")
            await session.post(f"{control}/chaos", json=fault)
            statuses = []
            for _ in range(20):
                async with session.post(f"{public}/v1/chat/completions", json=BODY) as r:
                    statuses.append(r.status)
            return statuses

    first, second = await run(), await run()
    assert first == second and 500 in first and 200 in first
