"""Measure how much time the gateway adds.

Starts mockllm with a fixed latency and the gateway in front of it, then sends an
open-loop stream of requests (a new request every 1/rate seconds, whether or not earlier
ones finished) to mockllm directly and through the gateway. Overhead is the difference:

- streaming: time to the first content byte, gateway minus direct
- non-streaming: total time, gateway minus direct

The gateway's own histogram (switchyard_gateway_overhead_seconds) is reported too.

    uv run python -m bench.overhead --rates 100 200 400 --seconds 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).parent.parent
KEY = "sy-dev-local-key"
BODY = {"model": "mock", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 32}


@dataclass
class Sample:
    ttft_s: float
    total_s: float
    ok: bool


def start(args: list[str], env: dict[str, str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", *args, "--no-access-log", "--log-level", "warning"],
        env={**os.environ, **env},
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_ready(url: str) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, trust_env=False).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"{url} did not come up")


FIRST_CONTENT = re.compile(rb'"content":\s?"[^"]')


async def one(host: str, port: int, headers: dict[str, str], stream: bool) -> Sample:
    """One request over a raw socket. A full HTTP client would cost the load generator
    more CPU than the gateway it is measuring, on a machine they share."""
    body = json.dumps({**BODY, "stream": stream}).encode()
    head = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    request = (
        f"POST /v1/chat/completions HTTP/1.1\r\nhost: bench\r\n"
        f"content-type: application/json\r\ncontent-length: {len(body)}\r\n"
        f"connection: close\r\n{head}\r\n"
    ).encode() + body
    started = time.perf_counter()
    first = None
    received = b""
    try:
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(request)
        while chunk := await reader.read(65536):
            if first is None:
                received += chunk
                if stream and FIRST_CONTENT.search(received):
                    first = time.perf_counter()
        writer.close()
        ok = received.startswith(b"HTTP/1.1 200")
    except OSError:
        ok = False
    end = time.perf_counter()
    return Sample((first or end) - started, end - started, ok)


async def load(
    port: int, headers: dict[str, str], rate: float, seconds: float, stream: bool
) -> list[Sample]:
    for _ in range(20):  # warm up
        await one("127.0.0.1", port, headers, stream)
    tasks = []
    interval = 1 / rate
    began = time.perf_counter()
    for i in range(int(rate * seconds)):
        delay = began + i * interval - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        tasks.append(asyncio.create_task(one("127.0.0.1", port, headers, stream)))
    return list(await asyncio.gather(*tasks))


def pct(values: list[float], q: int) -> float:
    return (
        statistics.quantiles(values, n=100, method="inclusive")[q - 1]
        if len(values) > 1
        else values[0]
    )


def server_overhead(metrics: str, mode: str) -> tuple[float, float] | None:
    """p50 and p95 upper bounds from the gateway's own histogram buckets."""
    buckets: list[tuple[float, float]] = []
    for line in metrics.splitlines():
        prefix = 'switchyard_gateway_overhead_seconds_bucket{le="'
        if line.startswith(prefix) and f'mode="{mode}"' in line:
            le = line.split('le="')[1].split('"')[0]
            if le != "+Inf":
                buckets.append((float(le), float(line.rsplit(" ", 1)[1])))
    if not buckets:
        return None
    total = max(count for _, count in buckets)
    if not total:
        return None

    def upper(q: float) -> float:
        return next((le for le, count in buckets if count >= q * total), buckets[-1][0])

    return upper(0.5), upper(0.95)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rates", type=float, nargs="+", default=[100, 200, 400])
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--ttft-ms", type=float, default=50)
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "results")
    args = parser.parse_args()

    mock_port, gateway_port = 18101, 18100
    mock = start(
        [
            "--factory",
            "mockllm.app:app_from_env",
            "--port",
            str(mock_port),
            "--loop",
            "uvloop",
            "--http",
            "httptools",
        ],
        {
            "MOCKLLM_TTFT_MS": str(args.ttft_ms),
            "MOCKLLM_TOKEN_MS": "1",
            "MOCKLLM_ANSWER_TOKENS": "32",
        },
    )
    gateway = start(
        [
            "--factory",
            "switchyard.api.app:app_from_env",
            "--port",
            str(gateway_port),
            "--loop",
            "uvloop",
            "--http",
            "httptools",
        ],
        {"MOCKLLM_A_URL": f"http://127.0.0.1:{mock_port}", "SWITCHYARD_LOG_LEVEL": "WARNING"},
    )
    rows = []
    try:
        wait_ready(f"http://127.0.0.1:{mock_port}/healthz")
        wait_ready(f"http://127.0.0.1:{gateway_port}/healthz")
        headers = {"authorization": f"Bearer {KEY}", "x-switchyard-feature": "bench"}
        for rate in args.rates:
            for stream in (True, False):
                a = await load(mock_port, {}, rate, args.seconds, stream)
                b = await load(gateway_port, headers, rate, args.seconds, stream)
                key = "ttft_s" if stream else "total_s"
                da = [getattr(s, key) for s in a if s.ok]
                db = [getattr(s, key) for s in b if s.ok]
                row = {
                    "rate": rate,
                    "mode": "stream" if stream else "unary",
                    "requests": len(b),
                    "errors": sum(not s.ok for s in b) + sum(not s.ok for s in a),
                    "direct_p50": pct(da, 50) * 1000,
                    "direct_p95": pct(da, 95) * 1000,
                    "gateway_p50": pct(db, 50) * 1000,
                    "gateway_p95": pct(db, 95) * 1000,
                }
                row["overhead_p50"] = row["gateway_p50"] - row["direct_p50"]
                row["overhead_p95"] = row["gateway_p95"] - row["direct_p95"]
                rows.append(row)
                print(
                    f"{rate:>5.0f}/s {row['mode']:<6} overhead p50 {row['overhead_p50']:6.2f} ms "
                    f"p95 {row['overhead_p95']:6.2f} ms  errors {row['errors']}"
                )
        metrics = httpx.get(f"http://127.0.0.1:{gateway_port}/metrics", trust_env=False).text
    finally:
        gateway.terminate()
        mock.terminate()
        gateway.wait()
        mock.wait()

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    lines = [
        f"# Gateway overhead, {stamp}",
        "",
        f"Machine: {os.cpu_count()} vCPU, {platform.system()} {platform.machine()}, Python "
        f"{platform.python_version()}. mockllm, the gateway and the load generator all ran on "
        f"this one machine, so they competed for CPU. mockllm answered with a fixed "
        f"{args.ttft_ms:g} ms to first token and 32 tokens. Open-loop load, {args.seconds:g} s "
        "per row, after a 20-request warm-up, from a raw-socket client that opens a new "
        "connection per request. Overhead is the gateway's percentile minus the direct "
        "percentile: time to first content byte when streaming, total time otherwise.",
        "",
        "| Rate (req/s) | Mode | Requests | Errors | Direct p50 | Direct p95 "
        "| Gateway p50 | Gateway p95 | Overhead p50 | Overhead p95 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        lines.append(
            f"| {r['rate']:g} | {r['mode']} | {r['requests']} | {r['errors']} "
            f"| {r['direct_p50']:.1f} ms | {r['direct_p95']:.1f} ms "
            f"| {r['gateway_p50']:.1f} ms | {r['gateway_p95']:.1f} ms "
            f"| {r['overhead_p50']:.2f} ms | {r['overhead_p95']:.2f} ms |"
        )
    for mode in ("stream", "unary"):
        seen = server_overhead(metrics, mode)
        if seen:
            lines += [
                "",
                f"The gateway's own histogram, all rates, {mode}: p50 at most "
                f"{seen[0] * 1000:g} ms, p95 at most {seen[1] * 1000:g} ms (bucket upper bounds).",
            ]
    path = args.out / f"overhead-{stamp}.md"
    path.write_text("\n".join(lines) + "\n")
    print(f"Written to {path}")


if __name__ == "__main__":
    asyncio.run(main())
