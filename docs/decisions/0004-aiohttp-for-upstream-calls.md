# 4. aiohttp, not httpx, for calls to providers

Date: 2026-10-28 · Status: accepted

## Context

The first overhead measurement, with httpx as the upstream client, showed the gateway adding about 3 ms to time to first token at 100 streamed requests a second, then collapsing between 150 and 200: requests queued for seconds. mockllm alone handled 200 a second with no change in latency, so the gateway was the bottleneck.

A sampling profile showed the cost was per streamed chunk, spread across httpx's connection pool, its pure-Python HTTP/1.1 parser (h11) and the anyio layers under it. A direct comparison on identical streams (400 concurrent 32-token streams from mockllm, client CPU time only):

| Client | CPU per stream | Reads per stream |
| --- | --- | --- |
| httpx 0.28 | 8.6 to 9.6 ms | 35 |
| aiohttp 3 | 0.69 to 0.71 ms | 33 |

aiohttp parses HTTP in C and returns whatever bytes have arrived in one read.

## Decision

Use aiohttp for every call to a provider. Keep `trust_env` off: with it on, aiohttp re-reads proxy and netrc settings on every request, which took a fifth of the gateway's CPU under load.

The same profiling led to three smaller changes: SSE events are split from raw bytes instead of decoded lines, JSON goes through orjson, and streams are read to the end so connections return to the pool. (Stopping at `[DONE]` had made every request open a new connection.)

## Consequences

- At 200 streamed requests a second the gateway adds 1.7 ms at p50 and 4.9 ms at p95 to time to first token, on a shared 2-vCPU machine. See `docs/results/`.
- Two HTTP libraries in the project: aiohttp for upstream calls, httpx in tests and the week 1 seam script.
- No proxy support for upstream calls yet. If needed, it will be an explicit setting rather than `trust_env`.
