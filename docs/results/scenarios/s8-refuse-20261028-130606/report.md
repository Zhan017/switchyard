# S8-refuse: Every provider refuses connections for 3 minutes

Verdict: **pass** · 5 seeds · 15,000 requests · 2026-10-28 14:16 UTC · gateway http://localhost:8000

After a healthy minute, both models' servers refuse connections for 3 minutes. Nothing can succeed, so the gateway should fail fast with 503 and Retry-After instead of making clients wait, should not pile retries on dead providers, and should serve normally again soon after they recover.

| Check | Target | Measured | Verdict |
| --- | --- | --- | --- |
| Requests during the outage answered with 503 (requests sent 60 to 240 s) | lower bound >= 99% within 1 s | 100.00% (9000 of 9000; 95% interval 99.96% to 100.00%) | pass |
| Upstream attempts per request over the whole run, probes excluded | <= 1.2x requests | 0.333x (4993 upstream attempts for 15000 requests) | pass |
| Normal service after the providers recover | within 45 s | 20.1 s on the worst seed (last failed request after the outage) | pass |

## Per seed

| Seed | Requests | Succeeded | Upstream attempts | Probes | Failed requests: p50, max latency | Errors |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 3000 | 996 | 999 | 6 | 0.00 s, 0.35 s | no_healthy_provider: 2004 |
| 2 | 3000 | 996 | 998 | 6 | 0.00 s, 0.27 s | no_healthy_provider: 2004 |
| 3 | 3000 | 996 | 999 | 6 | 0.00 s, 0.21 s | no_healthy_provider: 2004 |
| 4 | 3000 | 997 | 999 | 6 | 0.00 s, 0.23 s | no_healthy_provider: 2003 |
| 5 | 3000 | 997 | 998 | 6 | 0.00 s, 0.16 s | no_healthy_provider: 2003 |

## Setup

- Traffic: 10 requests a second for 300 s, open loop, to `mock`; 50% streaming; `max_tokens` drawn from [16, 32, 48, 64].
- gateway: Switchyard, route group `mock` (mock-a/mock first, then mock-b/mock)
- primary: mockllm behind a gate, serving mock-a/mock
- secondary: mockllm behind a gate, serving mock-b/mock
- Fault: `refuse_connections` on primary from 60 s to 240 s.
- Fault: `refuse_connections` on secondary from 60 s to 240 s.

## Environment

- date: 2026-10-28 14:16 UTC
- gateway: http://localhost:8000
- commit: 8de40c0
- host: Linux x86_64, 2 CPUs
- python: 3.12.3
