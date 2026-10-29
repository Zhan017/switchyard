"""The breaker's probe against a provider that, like GPT-6 Luna, rejects a request for
fewer than 3 output tokens. Found by the first real-API smoke test on 2026-10-29: with a
one-token probe, an open breaker on Luna could never close."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import ValidationError

from switchyard.accounting.pricing import load_prices
from switchyard.api.state import build_state
from switchyard.config import ModelConfig, ProviderConfig, RouteGroup, Settings
from switchyard.observability.metrics import Metrics
from switchyard.resilience.breaker import Breakers
from switchyard.resilience.prober import Prober

ROOT = Path(__file__).parent.parent.parent
RECORDED = ROOT / "tests" / "fixtures" / "streams" / "recorded" / "openai_text.sse"
KEY = "openai/gpt-6-luna"
# OpenAI's answer to max_completion_tokens of 1 or 2 on Luna, 2026-10-29.
TOO_FEW = {
    "error": {
        "message": "Could not finish the message because max_tokens or model output limit "
        "was reached. Please try again with higher max_tokens.",
        "type": "invalid_request_error",
        "param": None,
        "code": None,
    }
}

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@asynccontextmanager
async def luna() -> AsyncIterator[tuple[str, list[int]]]:
    """A fake OpenAI that rejects fewer than 3 output tokens and otherwise replays a
    recorded Luna stream. Yields its base URL and the token limit of each request."""
    asked: list[int] = []

    async def handler(request: web.Request) -> web.StreamResponse:
        asked.append(int((await request.json())["max_completion_tokens"]))
        if asked[-1] < 3:
            return web.json_response(TOO_FEW, status=400)
        response = web.StreamResponse(headers={"content-type": "text/event-stream"})
        await response.prepare(request)
        await response.write(RECORDED.read_bytes())
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        yield f"http://127.0.0.1:{server.port}/v1", asked
    finally:
        await server.close()


async def probe(base_url: str, options: ModelConfig) -> str:
    settings = Settings(
        providers={"openai": ProviderConfig(type="openai", base_url=base_url)},
        models={KEY: options},
        route_groups={"chat": RouteGroup(models=[KEY])},
        tenants=[],
    )
    prices = load_prices(ROOT / "config" / "prices.yaml")
    async with aiohttp.ClientSession() as http:
        state = build_state(settings, prices, http)
        prober = Prober(Breakers(settings.resilience), lambda: state, Metrics())
        return await prober.probe_once(KEY)


async def test_the_probe_asks_for_the_models_probe_max_tokens() -> None:
    async with luna() as (url, asked):
        assert await probe(url, ModelConfig(probe_max_tokens=16)) == "ok"
    assert asked == [16]


async def test_the_probe_asks_for_one_token_by_default() -> None:
    # mockllm counts a request for 1 token as a probe, so the default stays 1.
    async with luna() as (url, asked):
        assert await probe(url, ModelConfig()) == "client_error"
    assert asked == [1]


def test_probe_max_tokens_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        ModelConfig(probe_max_tokens=0)
