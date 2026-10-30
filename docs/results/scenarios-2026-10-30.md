# Mid-stream scenarios, week 4, 2026-10-30

S5 and S6 from spec section 10, each with 5 seeds, against Switchyard with two mockllm providers. Both pass. The scenario files and targets were committed in `232ea09` and pushed before the first full run; the runs used `5df9e15`.

| Scenario | Check | Target | Measured | Verdict |
| --- | --- | --- | --- | --- |
| S5: 30% of the primary's streams are cut partway through | Requests that completed | lower bound ≥ 99% | 3,000 of 3,000 (95% interval 99.87% to 100%) | pass |
| | Requests finished after a mid-stream failover that completed | lower bound ≥ 99% | 424 of 424 (99.10% to 100%) | pass |
| S6: 30% of the primary's streams stall partway through, with keep-alives | Requests that completed | lower bound ≥ 99% | 3,000 of 3,000 (99.87% to 100%) | pass |
| | Requests finished after a mid-stream failover that completed | lower bound ≥ 99% | 882 of 882 (99.57% to 100%) | pass |
| | Stall noticed after the last token | within 6 s, worst request | worst 5.02 s, median 5.00 s, over 882 stalls | pass |

Full reports, with per-seed tables and raw requests: [S5](scenarios/s5-20261030-060331/report.md), [S6](scenarios/s6-20261030-060331/report.md).

## What the numbers say

- **Every broken stream was finished.** All 1,306 streams that broke after the client had started reading (424 cuts, 882 stalls) were completed on the second model in the same stream, and no request failed. Postgres shows each of them as a failed primary attempt followed by one successful mid-stream attempt; none needed a second.
- **Stalls are noticed on time despite keep-alives.** The primary kept sending a keep-alive comment every second after it stopped producing tokens. Those don't count as content, so the 5 s stall limit fired after 5.00 s at the median and 5.02 s at worst.
- **Seam repair added 81 ms at the median and 183 ms at p95** before the continuation's first text went out, while it held the first 200 to 400 characters. mockllm writes a token every 5 ms; a real model writes more slowly, so the delay will be longer there.
- **The breaker opened in S5 but not in S6.** A cut is noticed at once, so five cuts in a row happened about three times per seed and opened the breaker 15 times in all; while it was open, requests went straight to the second model, which is why S5's mid-stream counts vary by seed (45 to 127). A stall is noticed 5 s later, after other requests have succeeded in between, so five in a row never lined up in S6.

## What this doesn't show

mockllm writes random words, so these runs show that answers complete, not that the joins read well. Seam repair mostly inserted a space between two random words (1,069 of 1,306 joins). How seamless the joins are on real models is the seam evaluation's job, and S5b and S6b (recorded real streams, cut and continued on real providers) are still to run.

## Setup

- Gateway, two mockllm servers behind TCP gates, Redis 7 and Postgres 16, on one shared 2-vCPU Linux machine with Python 3.12. Default resilience policy, route group `mock`; mock-b's strategy is the default, `instructed_repair`.
- Traffic: 5 requests a second for 120 s, open loop, every request streamed, `max_tokens` drawn from 16, 32, 48 and 64.
- Fault: on the primary, `cut_after_tokens` (S5) or `stall_after_tokens` with keep-alives every second (S6), on 30% of requests, at a random 20 to 80% of the answer, seeded.
- Run with `uv run switchyard-chaos run chaos/scenarios/s5.yaml chaos/scenarios/s6.yaml` against the stack.
