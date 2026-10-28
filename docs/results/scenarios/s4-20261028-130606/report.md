# S4: The primary refuses connections for 5 minutes, then recovers

Verdict: **pass** · 5 seeds · 21,000 requests · 2026-10-28 13:51 UTC · gateway http://localhost:8000

After a healthy minute, the first model's server refuses every TCP connection and resets the open ones for 5 minutes, then accepts again. Requests should fail over to the second model, the breaker should open quickly, and probes should close it within the worst case of 35 s after recovery (spec 7.4), plus margin.

| Check | Target | Measured | Verdict |
| --- | --- | --- | --- |
| Requests that completed | lower bound >= 99% | 100.00% (20999 of 21000; 95% interval 99.97% to 100.00%) | pass |
| The primary's breaker opens after the outage starts | within 10 s | opened after 0.33 s on the worst seed (polled every 0.25 s) | pass |
| The primary's breaker closes after it recovers | within 45 s | closed after 20.37 s on the worst seed (polled every 0.25 s) | pass |

## Per seed

| Seed | Requests | Succeeded | Upstream attempts | Probes | Failed requests: p50, max latency | Errors |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 4200 | 4200 | 4202 | 3 | none failed | none |
| 2 | 4200 | 4200 | 4202 | 3 | none failed | none |
| 3 | 4200 | 4199 | 4202 | 3 | 0.20 s, 0.20 s | no_healthy_provider: 1 |
| 4 | 4200 | 4200 | 4201 | 3 | none failed | none |
| 5 | 4200 | 4200 | 4202 | 3 | none failed | none |

## Setup

- Traffic: 10 requests a second for 420 s, open loop, to `mock`; 50% streaming; `max_tokens` drawn from [16, 32, 48, 64].
- gateway: Switchyard, route group `mock` (mock-a/mock first, then mock-b/mock)
- primary: mockllm behind a gate, serving mock-a/mock
- secondary: mockllm behind a gate, serving mock-b/mock
- Fault: `refuse_connections` on primary from 60 s to 360 s.

## Environment

- date: 2026-10-28 13:51 UTC
- gateway: http://localhost:8000
- commit: 8de40c0
- host: Linux x86_64, 2 CPUs
- python: 3.12.3
