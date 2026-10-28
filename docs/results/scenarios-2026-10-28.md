# Outage scenarios, week 3, 2026-10-28

Four scenarios from spec section 10, each with 5 seeds, against Switchyard with two mockllm providers. All four pass. Scenario files and targets were committed in `7e08ae8` before the first full run; the runs used `8de40c0`.

| Scenario | Check | Target | Measured | Verdict |
| --- | --- | --- | --- | --- |
| S1: the primary returns 500 on half its requests | Requests that completed | lower bound ≥ 99% | 6,000 of 6,000 (95% interval 99.94% to 100%) | pass |
| | The primary's breaker opens | opens | after 1.5 s on the worst seed | pass |
| S4: the primary refuses connections for 5 min | Requests that completed | lower bound ≥ 99% | 20,999 of 21,000 (99.97% to 100%) | pass |
| | Breaker opens after the outage starts | within 10 s | 0.33 s on the worst seed | pass |
| | Breaker closes after the primary recovers | within 45 s | 20.4 s on the worst seed | pass |
| S8-refuse: every provider refuses connections for 3 min | Requests during the outage answered with 503 | lower bound ≥ 99% within 1 s | 9,000 of 9,000 (99.96% to 100%) | pass |
| | Attempts per request over the run | ≤ 1.2x | 0.33x | pass |
| | Normal service after recovery | within 45 s | 20.1 s on the worst seed | pass |
| S8-hang: every provider hangs for 3 min | Requests from 15 s into the outage answered with 503 | lower bound ≥ 99% within 6 s | 8,250 of 8,250 (99.95% to 100%) | pass |
| | Attempts per request over the run | ≤ 1.2x | 0.39x | pass |
| | Normal service after recovery | within 45 s | 20.7 s on the worst seed | pass |

Full reports, with per-seed tables, raw requests and breaker timelines: [S1](scenarios/s1-20261028-130606/report.md), [S4](scenarios/s4-20261028-130606/report.md), [S8-refuse](scenarios/s8-refuse-20261028-130606/report.md), [S8-hang](scenarios/s8-hang-20261028-130606/report.md).

## What the numbers say

- **Failover before the first token is invisible.** In S1 every request completed although the primary failed half of them; in S4 every request but one did, while the primary was down for 5 minutes.
- **The one S4 failure is a mid-stream cut.** A streamed answer was already being sent when the primary's connections were reset at 60 s, so it ended with an error event. Finishing it on the other provider is week 4's mid-stream failover. The short S4 in the test suite shows the same thing.
- **Breakers open fast and keep traffic off dead providers.** During S8-refuse's outage, 9,000 requests caused 43 upstream attempts in total, and the 503s came back in 1.8 ms at p50 and 2.9 ms at p95, as seen by the client. During S8-hang's outage, 9,000 requests caused 853 attempts.
- **Recovery takes about 20 s,** against 35 s worst case by formula (spec 7.4): the probe schedule starts when the breaker opens, so recovery lands somewhere inside it.
- **Hangs cost the first few seconds.** In S8-hang, requests sent in the first 5.7 s of the outage waited for first-token timeouts until both breakers opened: 57 per seed waited longer than 6 s, the longest 15.2 s. Everything sent after that got a 503 within 6 s, so the 15 s exclusion in the target turned out more generous than needed.
- **Breakers flap under S1's 50% error rate, as designed.** Probes succeed half the time, so three in a row happen now and then, the breaker closes, and the 40% rule opens it again. Over all runs the primary's breaker opened 26 times.

## Cross-checks

Each run was counted three ways: by the client, by the mock servers, and by the gateway's own Postgres records.

| Scenario | Requests (client) | Requests (Postgres) | Attempts (mocks) | Attempts (Postgres) |
| --- | --- | --- | --- | --- |
| S1 | 6,000 | 6,000 | 6,066 | 6,066 |
| S4 | 21,000 | 21,000 | 21,009 | 21,028 |
| S8-refuse | 15,000 | 15,000 | 4,993 | 5,041 |

The mocks can't count a connection they refused, which is why their attempt counts run a little low in S4 and S8-refuse. The tagged CLI counted attempts at the mocks; it now counts them at the gateway when the gateway is Switchyard (`d3e1e5c`). Counted that way, S8-refuse is 0.336x instead of 0.333x. No Postgres rows were dropped, and Redis stayed reachable throughout (zero sync errors).

## Setup

- Gateway, two mockllm servers behind TCP gates, Redis 7 and Postgres 16, all on one shared 2-vCPU Linux machine with Python 3.12. Default resilience policy (spec 7.3 to 7.5), route group `mock`.
- Traffic: 10 requests a second, open loop, half streamed, `max_tokens` drawn from 16, 32, 48 and 64. Before each seed the CLI clears faults and resets the breakers through the admin API.
- Run with `uv run switchyard-chaos run chaos/scenarios/s1.yaml chaos/scenarios/s4.yaml chaos/scenarios/s8-refuse.yaml chaos/scenarios/s8-hang.yaml` against `docker compose up`, or the same stack started by hand.
- These are mock providers. Real providers fail in ways mockllm doesn't imitate; S5 and S6 on real providers come in week 4.
