"""The gateway the evaluation runs through, and the fake upstream it talks to.

Every answer in the evaluation goes through a real Switchyard gateway, started in this
process on a local port with a config generated from config/switchyard.yaml:

- ``ref-<family>``: one real model, for the reference answers.
- ``cont-<primary>-<fallback>-<strategy>``: a fake primary, then the real fallback with
  that continuation strategy. The fake primary streams the primary's real reference
  answer and drops the connection at the cut, so the gateway's own continuer finishes it.
- ``judge-<name>``: one real model, for the judges.

Breakers are switched off (their trip thresholds are set out of reach): the fake primary
fails every request on purpose, and an open breaker would skip it, so nothing would be
continued. With ``dry_run`` every real provider is replaced by a fake that writes plausible
text and judges at random, so the whole evaluation can be tested without keys or cost.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import orjson
import uvicorn
import yaml
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from eval.seam.clients import Message
from eval.seam.fake import FakeClient
from eval.seam.spike import _PIECE
from eval.seam.v01.plan import GATEWAY_CONFIG, PRICES, STRATEGY_CODES, Plan
from switchyard.api.app import create_app
from switchyard.config import expand_env, hash_key, load_settings

TENANT_KEY = "sy-eval-local-key"  # the gateway only listens on 127.0.0.1
FEATURE = "eval"
JUDGE_MARKER = 'end with a line that says exactly "Answer: A" or "Answer: B"'
# Out of reach, so no breaker opens during the evaluation.
NEVER = 1_000_000_000


def route_ref(family: str) -> str:
    return f"ref-{family}"


def route_cont(primary: str, fallback: str, strategy: str) -> str:
    return f"cont-{primary}-{fallback}-{STRATEGY_CODES[strategy]}"


def route_judge(name: str) -> str:
    return f"judge-{name}"


def write_gateway_config(plan: Plan, upstream_url: str, out: Path, dry_run: bool) -> Path:
    """Write the gateway's config and price table into ``out`` and return the config."""
    shipped = yaml.safe_load(expand_env(GATEWAY_CONFIG.read_text()))
    shipped_prices = yaml.safe_load(PRICES.read_text())
    providers: dict[str, Any] = {}
    models: dict[str, Any] = {}
    prices: dict[str, Any] = {}
    routes: dict[str, Any] = {}

    def add(alias: str, real_key: str, options: dict[str, Any] | None = None) -> str:
        """A provider alias for the real model ``real_key``, with its own options."""
        provider_name, _, model = real_key.partition("/")
        spec = dict(shipped["providers"][provider_name])
        if dry_run:
            # DeepSeek's API is OpenAI's, and native continuation needs a deepseek
            # provider, so it stays one. The others become OpenAI-type fakes.
            suffix = "" if spec["type"] == "deepseek" else "/v1"
            if spec["type"] != "deepseek":
                spec["type"] = "openai"
            spec["base_url"] = f"{upstream_url}/fake/{alias}{suffix}"
            spec.pop("api_key_env", None)
        providers[alias] = spec
        key = f"{alias}/{model}"
        models[key] = dict(options if options is not None else shipped["models"][real_key])
        prices[key] = shipped_prices["models"][real_key]
        return key

    for primary in plan.primaries:
        providers[f"primary-{primary}"] = {
            "type": "openai",
            "base_url": f"{upstream_url}/primary/{primary}/v1",
        }
        models[f"primary-{primary}/replay"] = {}
        prices[f"primary-{primary}/replay"] = {"input": 0, "output": 0}
        key = add(primary, plan.families[primary])
        routes[route_ref(primary)] = _limits([key], plan.max_tokens, ttft=30, stall=30)

    for primary, fallback in plan.pairs:
        real = plan.families[fallback]
        for strategy in plan.strategies_for(fallback):
            alias = f"{fallback}-{STRATEGY_CODES[strategy]}"
            options = {**shipped["models"][real], "continuation": plan.strategies[strategy]}
            key = add(alias, real, options) if alias not in providers else f"{alias}/{_m(real)}"
            # The production limits, so failures to start in time count as they would.
            routes[route_cont(primary, fallback, strategy)] = {
                "models": [f"primary-{primary}/replay", key],
                "default_max_tokens": plan.max_tokens,
                "request_deadline_s": 180,
            }

    for name, judge in plan.judges.items():
        key = add(f"judge-{name}", judge.model, judge.options or None)
        # Thinking isn't forwarded, so a thinking judge's first token comes late.
        routes[route_judge(name)] = _limits([key], judge.max_tokens, ttft=90, stall=60)

    resilience = dict(shipped.get("resilience") or {})
    resilience.update(
        trip_min_requests=NEVER, trip_consecutive_failures=NEVER, ttft_min_samples=NEVER
    )
    config = {
        "providers": providers,
        "models": models,
        "route_groups": routes,
        "tenants": [{"name": "eval", "key_sha256": hash_key(TENANT_KEY), "features": [FEATURE]}],
        "prices_file": "gateway-prices.yaml",
        "resilience": resilience,
    }
    out.mkdir(parents=True, exist_ok=True)
    checked = shipped_prices["checked"]
    price_table = {"checked": checked, "models": prices}
    (out / "gateway-prices.yaml").write_text(yaml.safe_dump(price_table, sort_keys=False))
    path = out / "gateway.yaml"
    header = "# Generated by eval/seam/v01/stack.py for one stage of the seam evaluation.\n"
    path.write_text(header + yaml.safe_dump(config, sort_keys=False))
    load_settings(path)  # fail here with the reason, not as "server did not start"
    return path


def _m(key: str) -> str:
    return key.partition("/")[2]


def _limits(models: list[str], max_tokens: int, ttft: float, stall: float) -> dict[str, Any]:
    return {
        "models": models,
        "default_max_tokens": max_tokens,
        "request_deadline_s": 300,
        "ttft_timeout_s": ttft,
        "stall_timeout_s": stall,
    }


# The fake upstream


class Upstream:
    """Fake primaries that replay real references up to the cut, and, in dry runs, fake
    stand-ins for the real providers."""

    def __init__(self) -> None:
        self.cuts: dict[tuple[str, str], tuple[str, int]] = {}
        self.cut_streams = 0

    def register(self, family: str, prompt: str, reference: str, cut: int) -> None:
        self.cuts[(family, prompt)] = (reference, cut)

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/primary/{family}/v1/chat/completions", self.primary, methods=["POST"]),
                Route("/fake/{alias}/{rest:path}", self.fake, methods=["POST"]),
            ]
        )

    async def primary(self, request: Request) -> Response:
        family = request.path_params["family"]
        body = orjson.loads(await request.body())
        found = self.cuts.get((family, _last_user_text(body)))
        if found is None:
            # A probe or a stray request: answer it properly.
            return _sse(_chunks(["OK."], finish="stop", usage=(5, 1)))
        reference, cut = found
        self.cut_streams += 1
        # The stream ends at the cut with no finish and no [DONE]: the connection died.
        return _sse(_chunks(pieces(reference[:cut]), finish=None, usage=None))

    async def fake(self, request: Request) -> Response:
        alias = request.path_params["alias"]
        body = orjson.loads(await request.body())
        wire = [m for m in body.get("messages", []) if m.get("role") in ("user", "assistant")]
        messages: list[Message] = [
            {"role": m["role"], "content": str(m.get("content") or "")} for m in wire
        ]
        prefix = bool(wire and wire[-1].get("prefix"))
        max_tokens = int(body.get("max_tokens") or body.get("max_completion_tokens") or 1024)
        last = messages[-1]["content"] if messages else ""
        if JUDGE_MARKER in last:
            seed = hashlib.sha256((alias + last).encode()).hexdigest()
            letter = random.Random(seed).choice("AB")
            text = f"The join reads unevenly in one of them.\nAnswer: {letter}"
            usage = (len(last) // 4, 12)
        else:
            family = alias.split("-")[0]
            client = FakeClient(family, "dry", supports_prefix=prefix)
            done = await client.complete(messages, max_tokens, prefix=prefix)
            text, usage = done.text, (done.input_tokens, done.output_tokens)
        return _sse(_chunks(pieces(text), finish="stop", usage=usage))


def pieces(text: str) -> list[str]:
    """Token-like pieces, as a provider streams them; the cut lands between two."""
    found = [m.group(0) for m in _PIECE.finditer(text)]
    if "".join(found) != text:  # trailing whitespace, which the pattern leaves out
        found.append(text[len("".join(found)) :])
    return found


def _last_user_text(body: dict[str, Any]) -> str:
    for message in reversed(body.get("messages") or []):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def _sse(chunks: AsyncIterator[bytes]) -> StreamingResponse:
    return StreamingResponse(chunks, media_type="text/event-stream")


async def _chunks(
    parts: list[str], finish: str | None, usage: tuple[int, int] | None
) -> AsyncIterator[bytes]:
    created = int(time.time())

    def chunk(delta: dict[str, Any], reason: str | None = None) -> bytes:
        payload = {
            "id": "chatcmpl-eval",
            "object": "chat.completion.chunk",
            "created": created,
            "model": "eval",
            "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
        }
        return b"data: " + orjson.dumps(payload) + b"\n\n"

    yield chunk({"role": "assistant", "content": ""})
    for i, part in enumerate(parts):
        if i % 20 == 19:
            await asyncio.sleep(0.001)
        yield chunk({"content": part})
    if finish is None:
        return
    yield chunk({}, finish)
    if usage is not None:
        prompt_tokens, completion_tokens = usage
        payload = {
            "id": "chatcmpl-eval",
            "object": "chat.completion.chunk",
            "created": created,
            "model": "eval",
            "choices": [],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
        yield b"data: " + orjson.dumps(payload) + b"\n\n"
    yield b"data: [DONE]\n\n"


# Serving


@contextmanager
def serve(app: Any) -> Iterator[int]:
    """Serve an ASGI app on a free local port in a background thread."""
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_level="warning",
        lifespan="on",
        timeout_keep_alive=60,
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("server did not start")
        time.sleep(0.01)
    try:
        yield int(server.servers[0].sockets[0].getsockname()[1])
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@contextmanager
def stack(plan: Plan, out: Path, dry_run: bool) -> Iterator[tuple[str, Upstream, Path]]:
    """Start the fake upstream and the gateway. Yields the gateway's URL, the upstream
    (to register cuts on) and the gateway's config path."""
    # The evaluation keeps no records in Postgres and shares no state through Redis.
    for name in ("SWITCHYARD_DATABASE_URL", "SWITCHYARD_REDIS_URL"):
        os.environ.pop(name, None)
    os.environ.setdefault("SWITCHYARD_LOG_LEVEL", "WARNING")
    upstream = Upstream()
    with serve(upstream.app()) as upstream_port:
        config = write_gateway_config(plan, f"http://127.0.0.1:{upstream_port}", out, dry_run)
        with serve(create_app(config)) as gateway_port:
            yield f"http://127.0.0.1:{gateway_port}", upstream, config
