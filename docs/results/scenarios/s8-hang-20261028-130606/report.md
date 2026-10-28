# S8-hang: Every provider hangs for 3 minutes

Verdict: **pass** · 5 seeds · 15,000 requests · 2026-10-28 14:41 UTC · gateway http://localhost:8000

After a healthy minute, both models accept requests and never answer, for 3 minutes. Each attempt costs a full first-token timeout (5 s) until the breakers open, so the first requests of the outage wait up to about 15 s. The target applies from 15 s into the outage, once both breakers have had time to open; the first 15 s are reported in the per-seed latency columns. After that, requests should get a 503 within the first-token budget plus 1 s.

| Check | Target | Measured | Verdict |
| --- | --- | --- | --- |
| Requests from 15 s into the outage answered with 503 (requests sent 75 to 240 s) | lower bound >= 99% within 6 s | 100.00% (8250 of 8250; 95% interval 99.95% to 100.00%) | pass |
| Upstream attempts per request over the whole run, probes excluded | <= 1.2x requests | 0.387x (5812 upstream attempts for 15000 requests) | pass |
| Normal service after the providers recover | within 45 s | 20.7 s on the worst seed (last failed request after the outage) | pass |

## Per seed

| Seed | Requests | Succeeded | Upstream attempts | Probes | Failed requests: p50, max latency | Errors |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 3000 | 993 | 1162 | 39 | 0.00 s, 15.12 s | no_healthy_provider: 2007 |
| 2 | 3000 | 993 | 1162 | 39 | 0.00 s, 15.16 s | no_healthy_provider: 2007 |
| 3 | 3000 | 993 | 1163 | 39 | 0.00 s, 15.24 s | no_healthy_provider: 2007 |
| 4 | 3000 | 993 | 1162 | 39 | 0.00 s, 15.20 s | no_healthy_provider: 2007 |
| 5 | 3000 | 993 | 1163 | 39 | 0.00 s, 15.19 s | no_healthy_provider: 2007 |

## Setup

- Traffic: 10 requests a second for 300 s, open loop, to `mock`; 50% streaming; `max_tokens` drawn from [16, 32, 48, 64].
- gateway: Switchyard, route group `mock` (mock-a/mock first, then mock-b/mock)
- primary: mockllm behind a gate, serving mock-a/mock
- secondary: mockllm behind a gate, serving mock-b/mock
- Fault: `hang` on primary from 60 s to 240 s.
- Fault: `hang` on secondary from 60 s to 240 s.

## Environment

- date: 2026-10-28 14:41 UTC
- gateway: http://localhost:8000
- commit: 8de40c0
- host: Linux x86_64, 2 CPUs
- python: 3.12.3
