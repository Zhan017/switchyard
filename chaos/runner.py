"""Run a scenario against any OpenAI-compatible base URL and record what the client saw.

Traffic is open loop: request i starts at ``i / rate`` seconds whether or not earlier ones
finished, so a slow gateway can't slow the load down and hide its own latency. Faults are
switched on mockllm's control port on schedule. When the gateway is Switchyard, its admin
API is polled for breaker states; against anything else the breaker checks are "n/a".
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import aiohttp
import orjson

from chaos.scenario import Scenario

WORDS = [
    "route",
    "stream",
    "token",
    "answer",
    "provider",
    "breaker",
    "probe",
    "retry",
    "budget",
    "window",
]


@dataclass
class Target:
    gateway: str  # base URL, without /v1
    upstreams: dict[str, str]  # upstream name -> mockllm control URL
    key: str | None = None
    admin_key: str | None = None
    feature: str = "chaos"


@dataclass
class Result:
    i: int
    at_s: float  # when the request was sent, from the start of traffic
    stream: bool
    status: int = 0  # HTTP status, or 0 if the client couldn't connect
    ok: bool = False
    error: str | None = None
    finish_reason: str | None = None
    latency_s: float | None = None  # until the response ended
    ttft_s: float | None = None  # until the first content, when streaming
    attempts: int | None = None
    failovers: int | None = None
    midstream: int | None = None  # mid-stream failovers, from Switchyard's summary
    # Seconds from the last content to Switchyard's failover comment: how long a dead or
    # stalled stream took to notice. The longest, if there was more than one.
    detection_s: float | None = None
    served_model: str | None = None


FAILOVER_COMMENT = b": switchyard failover"


@dataclass
class SeedRun:
    seed: int
    started: float  # wall clock
    results: list[Result] = field(default_factory=list)
    breakers: list[tuple[float, dict[str, str]]] = field(default_factory=list)
    observer: bool = False  # did the admin API answer
    upstream_requests: dict[str, int] = field(default_factory=dict)  # probes excluded
    upstream_probes: dict[str, int] = field(default_factory=dict)
    # Attempts counted by the gateway itself, probes excluded, when it is Switchyard. The
    # mock servers can't count an attempt whose connection they refused.
    gateway_attempts: int | None = None
    fault_errors: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "started": self.started,
            "observer": self.observer,
            "upstream_requests": self.upstream_requests,
            "upstream_probes": self.upstream_probes,
            "gateway_attempts": self.gateway_attempts,
            "fault_errors": self.fault_errors,
        }


def plan(scenario: Scenario, seed: int) -> list[dict[str, Any]]:
    """The requests for one seed: the same seed gives the same mix."""
    rng = random.Random(seed)
    traffic = scenario.traffic
    out = []
    for _ in range(traffic.requests):
        words = " ".join(rng.choice(WORDS) for _ in range(rng.randint(5, 120)))
        out.append(
            {
                "model": traffic.model,
                "messages": [{"role": "user", "content": words}],
                "max_tokens": rng.choice(traffic.max_tokens),
                "stream": rng.random() < traffic.stream_share,
            }
        )
    return out


async def run_seed(scenario: Scenario, target: Target, seed: int) -> SeedRun:
    timeout = aiohttp.ClientTimeout(total=scenario.traffic.timeout_s)
    # Close idle connections before the server does. uvicorn drops them after 5 s and
    # aiohttp would keep them 15 s, so a request could go out on a connection the server is
    # closing and fail without ever reaching it: a load-generator error, not a gateway one.
    connector = aiohttp.TCPConnector(limit=0, keepalive_timeout=2.0)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        await _reset(session, scenario, target)
        before = await _upstream_stats(session, target)
        gateway_before = await gateway_attempts(session, target)
        run = SeedRun(seed=seed, started=time.time())
        requests = plan(scenario, seed)
        start = time.monotonic() + 0.5
        observer = asyncio.create_task(_observe(session, target, start, run))
        faults = [
            asyncio.create_task(_fault(session, target, fault, start, seed, run))
            for fault in scenario.faults
        ]
        tasks = []
        rate = scenario.traffic.rate_rps
        for i, body in enumerate(requests):
            delay = start + i / rate - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            tasks.append(asyncio.create_task(_request(session, target, i, body, start)))
        run.results = list(await asyncio.gather(*tasks))
        for task in faults:
            await task
        observer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await observer
        after = await _upstream_stats(session, target)
        gateway_after = await gateway_attempts(session, target)
        if gateway_before is not None and gateway_after is not None:
            run.gateway_attempts = gateway_after - gateway_before
        for name in target.upstreams:
            requests_delta = after[name][0] - before[name][0]
            probes_delta = after[name][1] - before[name][1]
            run.upstream_requests[name] = requests_delta - probes_delta
            run.upstream_probes[name] = probes_delta
        await _clear(session, target)
    return run


def _headers(target: Target, admin: bool = False) -> dict[str, str]:
    headers = {"x-switchyard-feature": target.feature}
    key = target.admin_key if admin else target.key
    if key:
        headers["authorization"] = f"Bearer {key}"
    return headers


async def _clear(session: aiohttp.ClientSession, target: Target) -> None:
    for url in target.upstreams.values():
        async with session.delete(f"{url}/chaos") as response:
            response.raise_for_status()


async def _reset(session: aiohttp.ClientSession, scenario: Scenario, target: Target) -> None:
    """Start every seed from the same place: no faults, and breakers closed with clean
    windows. Without the admin API, wait out the worst-case breaker recovery instead."""
    await _clear(session, target)
    if target.admin_key:
        try:
            async with session.get(
                f"{target.gateway}/admin/providers", headers=_headers(target, admin=True)
            ) as response:
                if response.status == 200:
                    providers = await response.json()
                    for provider in providers.values():
                        for key in provider.get("models", {}):
                            for force in ("closed", "auto"):
                                async with session.post(
                                    f"{target.gateway}/admin/providers/{key}/breaker",
                                    json={"force": force},
                                    headers=_headers(target, admin=True),
                                ) as forced:
                                    forced.raise_for_status()
                    await asyncio.sleep(1.0)  # let replicas pick up the reset through Redis
                    return
        except aiohttp.ClientError:
            pass
    await asyncio.sleep(40.0)


async def _upstream_stats(
    session: aiohttp.ClientSession, target: Target
) -> dict[str, tuple[int, int]]:
    out = {}
    for name, url in target.upstreams.items():
        async with session.get(f"{url}/stats") as response:
            response.raise_for_status()
            stats = await response.json()
        out[name] = (int(stats["requests"]), int(stats.get("probes", 0)))
    return out


async def gateway_attempts(session: aiohttp.ClientSession, target: Target) -> int | None:
    """Switchyard's own count of upstream attempts, probes excluded, from /metrics. None
    for any other gateway."""
    try:
        async with session.get(f"{target.gateway}/metrics") as response:
            if response.status != 200:
                return None
            text = await response.text()
    except aiohttp.ClientError:
        return None
    total, seen = 0.0, False
    for line in text.splitlines():
        if line.startswith("switchyard_attempts_total{") and 'role="probe"' not in line:
            total += float(line.rsplit(" ", 1)[1])
            seen = True
    return round(total) if seen else (0 if "switchyard_attempts_total" in text else None)


async def _fault(
    session: aiohttp.ClientSession,
    target: Target,
    fault: Any,
    start: float,
    seed: int,
    run: SeedRun,
) -> None:
    await asyncio.sleep(max(0.0, start + fault.start_s - time.monotonic()))
    params = {"seed": seed, **fault.params}
    body = {"mode": fault.mode, "params": params, "duration_s": fault.duration_s}
    if fault.model:
        body["model"] = fault.model
    try:
        async with session.post(f"{target.upstreams[fault.upstream]}/chaos", json=body) as r:
            if r.status != 200:
                run.fault_errors.append(f"{fault.mode} on {fault.upstream}: HTTP {r.status}")
    except aiohttp.ClientError as error:
        run.fault_errors.append(f"{fault.mode} on {fault.upstream}: {error}")


async def _observe(
    session: aiohttp.ClientSession, target: Target, start: float, run: SeedRun
) -> None:
    if not target.admin_key:
        return
    while True:
        try:
            async with session.get(
                f"{target.gateway}/admin/providers",
                headers=_headers(target, admin=True),
                timeout=aiohttp.ClientTimeout(total=2),
            ) as response:
                if response.status != 200:
                    return  # not Switchyard, or no admin key: breaker checks are n/a
                providers = await response.json()
            states = {
                key: model["breaker"]["state"]
                for provider in providers.values()
                for key, model in provider.get("models", {}).items()
            }
            run.observer = True
            run.breakers.append((round(time.monotonic() - start, 3), states))
        except (aiohttp.ClientError, TimeoutError, KeyError, TypeError):
            pass
        await asyncio.sleep(0.25)


async def _request(
    session: aiohttp.ClientSession,
    target: Target,
    i: int,
    body: dict[str, Any],
    start: float,
) -> Result:
    sent = time.monotonic()
    result = Result(i=i, at_s=round(sent - start, 4), stream=body["stream"])
    url = f"{target.gateway}/v1/chat/completions"
    try:
        async with session.post(url, json=body, headers=_headers(target)) as response:
            result.status = response.status
            if body["stream"] and response.status == 200:
                await _read_stream(response, result, sent)
            else:
                data = orjson.loads(await response.read())
                _read_body(data, result)
    except (aiohttp.ClientError, TimeoutError, orjson.JSONDecodeError) as error:
        result.error = f"client: {type(error).__name__}"
    result.latency_s = round(time.monotonic() - sent, 4)
    if result.ok:
        result.error = None
    return result


def _read_body(data: Any, result: Result) -> None:
    if not isinstance(data, dict):
        result.error = "client: unexpected body"
        return
    if "error" in data:
        error = data["error"]
        result.error = str(
            error.get("code") or error.get("type") if isinstance(error, dict) else error
        )
        return
    choices = data.get("choices") or [{}]
    result.finish_reason = choices[0].get("finish_reason")
    _read_summary(data, result)
    result.ok = result.status == 200 and result.finish_reason in ("stop", "length")


async def _read_stream(response: aiohttp.ClientResponse, result: Result, sent: float) -> None:
    done = False
    buffer = b""
    last_content = sent
    async for chunk in response.content.iter_any():
        buffer += chunk
        while b"\n\n" in buffer:
            block, buffer = buffer.split(b"\n\n", 1)
            for line in block.split(b"\n"):
                if line.startswith(FAILOVER_COMMENT):
                    gap = round(time.monotonic() - last_content, 4)
                    result.detection_s = max(gap, result.detection_s or 0.0)
                    continue
                if not line.startswith(b"data: "):
                    continue
                payload = line[6:]
                if payload == b"[DONE]":
                    done = True
                    continue
                data = orjson.loads(payload)
                if "error" in data:
                    error = data["error"]
                    result.error = str(error.get("code") if isinstance(error, dict) else error)
                    continue
                for choice in data.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content") or delta.get("tool_calls"):
                        last_content = time.monotonic()
                        if result.ttft_s is None:
                            result.ttft_s = round(last_content - sent, 4)
                    if choice.get("finish_reason"):
                        result.finish_reason = choice["finish_reason"]
                _read_summary(data, result)
    result.ok = done and result.error is None and result.finish_reason in ("stop", "length")


def _read_summary(data: dict[str, Any], result: Result) -> None:
    summary = data.get("switchyard")
    if isinstance(summary, dict):
        result.attempts = summary.get("attempts")
        result.failovers = summary.get("failovers")
        result.midstream = summary.get("midstream_failovers")
        result.served_model = summary.get("served_model")


def result_rows(run: SeedRun) -> list[dict[str, Any]]:
    return [{"seed": run.seed, **asdict(r)} for r in run.results]
