"""Calls to the gateway, as a client sees them."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import orjson

from eval.seam.v01.stack import FEATURE, TENANT_KEY

RETRY_STATUS = {408, 429, 500, 502, 503, 504, 529, 599}  # 599: no response at all
# Attempt outcomes that are the provider's fault, worth trying again later (ErrorKind).
TRANSIENT = {
    "connection",
    "timeout",
    "stall",
    "deadline",
    "server_error",
    "rate_limited",
    "invalid_response",
}


class Fatal(Exception):
    """Something no retry will fix, such as a rejected API key. Stops the stage."""


def http_client(base_url: str) -> httpx.AsyncClient:
    # trust_env off: a proxy set for the internet must not catch 127.0.0.1.
    return httpx.AsyncClient(
        base_url=base_url,
        trust_env=False,
        timeout=httpx.Timeout(400.0, connect=10.0),
        headers={"authorization": f"Bearer {TENANT_KEY}", "x-switchyard-feature": FEATURE},
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=50),
    )


@dataclass
class Reply:
    """A non-streaming answer."""

    status: int
    text: str = ""
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def cost(self) -> float:
        return float(self.summary.get("cost_usd") or 0.0)

    @property
    def retryable(self) -> bool:
        return self.status in RETRY_STATUS


@dataclass
class StreamRead:
    """A streamed answer, split at the failover comment."""

    status: int
    before: str = ""  # text before the failover comment: what the primary sent
    after: str = ""  # text after it: the continuation
    failover: str | None = None
    continuing: str | None = None
    seam_ms: float | None = None  # failover comment to the first continued text
    heartbeats: int = 0
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def trail(self) -> list[dict[str, Any]]:
        return list(self.summary.get("trail") or [])

    @property
    def midstream(self) -> list[dict[str, Any]]:
        return [t for t in self.trail if t.get("role") == "midstream"]

    @property
    def cost(self) -> float:
        return float(self.summary.get("cost_usd") or 0.0)


async def complete(http: httpx.AsyncClient, route: str, prompt: str, max_tokens: int) -> Reply:
    body: dict[str, Any] = {
        "model": route,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    try:
        response = await http.post("/v1/chat/completions", json=body)
    except httpx.TransportError as error:
        return Reply(599, error=f"{type(error).__name__}: {error}")
    data = _json(response.content)
    if response.status_code != 200:
        problem = data.get("error") or {}
        message = problem.get("message") or response.text[:300]
        _raise_if_fatal(response.status_code, message, problem.get("code"))
        return Reply(response.status_code, error=f"HTTP {response.status_code}: {message}")
    choice = (data.get("choices") or [{}])[0]
    return Reply(
        200,
        text=str((choice.get("message") or {}).get("content") or ""),
        finish_reason=choice.get("finish_reason"),
        usage=data.get("usage") or {},
        summary=data.get("switchyard") or {},
    )


async def stream(http: httpx.AsyncClient, route: str, prompt: str, max_tokens: int) -> StreamRead:
    body = {
        "model": route,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": True,
    }
    before: list[str] = []
    after: list[str] = []
    failed_at: float | None = None
    try:
        async with http.stream("POST", "/v1/chat/completions", json=body) as response:
            read = StreamRead(response.status_code)
            if response.status_code != 200:
                data = _json(await response.aread())
                problem = data.get("error") or {}
                message = problem.get("message") or ""
                _raise_if_fatal(response.status_code, message, problem.get("code"))
                read.error = f"HTTP {response.status_code}: {message}"
                return read
            async for line in response.aiter_lines():
                if line.startswith(":"):
                    note = line[1:].strip()
                    if note.startswith("switchyard failover"):
                        read.failover, failed_at = note, time.monotonic()
                    elif note == "heartbeat":
                        read.heartbeats += 1
                    elif note.startswith("switchyard continuing"):
                        read.continuing = note
                    continue
                if not line.startswith("data:"):
                    continue
                data_text = line[5:].strip()
                if data_text == "[DONE]":
                    break
                event = _json(data_text.encode())
                if event.get("switchyard") is not None:
                    read.summary = event["switchyard"]
                if "error" in event:
                    read.error = str((event["error"] or {}).get("message") or event["error"])
                    continue
                if event.get("usage"):
                    read.usage = event["usage"]
                for choice in event.get("choices") or []:
                    content = (choice.get("delta") or {}).get("content")
                    if content:
                        if failed_at is None:
                            before.append(content)
                        else:
                            if not after:
                                read.seam_ms = (time.monotonic() - failed_at) * 1000
                            after.append(content)
                    if choice.get("finish_reason"):
                        read.finish_reason = choice["finish_reason"]
    except httpx.TransportError as error:
        read = StreamRead(599, error=f"{type(error).__name__}: {error}")
    read.before, read.after = "".join(before), "".join(after)
    for attempt in read.trail:
        if attempt.get("outcome") == "auth":
            raise Fatal(f"the provider rejected the API key for {attempt.get('model')}")
    return read


def _raise_if_fatal(status: int, message: str, code: str | None = None) -> None:
    if status in (401, 403) or code == "upstream_auth_failed":
        raise Fatal(f"HTTP {status} from the gateway: {message}")


def _json(content: bytes) -> dict[str, Any]:
    try:
        data = orjson.loads(content)
    except orjson.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
