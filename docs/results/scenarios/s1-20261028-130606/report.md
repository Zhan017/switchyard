# S1: The primary returns 500 on half its requests

Verdict: **pass** · 5 seeds · 6,000 requests · 2026-10-28 13:16 UTC · gateway http://localhost:8000

For the whole run, the first model in the route answers half its requests with HTTP 500 before sending anything. Retries on the same provider are limited by the retry budget, failover is not, and the breaker should open on the 40% rule and keep traffic away.

| Check | Target | Measured | Verdict |
| --- | --- | --- | --- |
| Requests that completed | lower bound >= 99% | 100.00% (6000 of 6000; 95% interval 99.94% to 100.00%) | pass |
| The primary's breaker opens | opens | opened after 1.52 s on the worst seed (polled every 0.25 s) | pass |

## Per seed

| Seed | Requests | Succeeded | Upstream attempts | Probes | Failed requests: p50, max latency | Errors |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 1200 | 1200 | 1207 | 11 | none failed | none |
| 2 | 1200 | 1200 | 1217 | 11 | none failed | none |
| 3 | 1200 | 1200 | 1212 | 11 | none failed | none |
| 4 | 1200 | 1200 | 1224 | 11 | none failed | none |
| 5 | 1200 | 1200 | 1206 | 11 | none failed | none |

## Setup

- Traffic: 10 requests a second for 120 s, open loop, to `mock`; 50% streaming; `max_tokens` drawn from [16, 32, 48, 64].
- gateway: Switchyard, route group `mock` (mock-a/mock first, then mock-b/mock)
- primary: mockllm behind a gate, serving mock-a/mock
- secondary: mockllm behind a gate, serving mock-b/mock
- Fault: `error_rate` (rate=0.5, status=500) on primary from 0 s to 120 s.

## Environment

- date: 2026-10-28 13:16 UTC
- gateway: http://localhost:8000
- commit: 8de40c0
- host: Linux x86_64, 2 CPUs
- python: 3.12.3
