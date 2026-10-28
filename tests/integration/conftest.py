"""A real mockllm server on a free port, and the gateway in front of it."""

from __future__ import annotations

import threading
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import aiohttp
import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from mockllm.app import create_mock_app
from switchyard.api.app import create_app

CONFIG = Path(__file__).parent.parent / "fixtures" / "config.yaml"
KEY = "sy-dev-local-key"
ADMIN_KEY = "sy-admin-local-key"


@pytest.fixture(scope="session")
def mock_url() -> Iterator[str]:
    config = uvicorn.Config(
        create_mock_app(), host="127.0.0.1", port=0, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("mockllm did not start")
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def chaos(mock_url: str) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=mock_url, trust_env=False) as client:
        client.delete("/chaos")
        yield client
        client.delete("/chaos")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def gateway(
    mock_url: str, chaos: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[httpx.AsyncClient, FastAPI]]:
    monkeypatch.setenv("MOCKLLM_URL", mock_url)
    upstream = aiohttp.ClientSession()
    app = create_app(CONFIG, http=upstream)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://sy") as client:
            client.headers.update(
                {"authorization": f"Bearer {KEY}", "x-switchyard-feature": "chat"}
            )
            yield client, app
    await upstream.close()
