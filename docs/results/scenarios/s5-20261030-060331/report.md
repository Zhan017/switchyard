# S5: The primary's stream is cut partway through the answer

Verdict: **pass** · 5 seeds · 3,000 requests · 2026-10-30 06:13 UTC · gateway http://localhost:8000

For the whole run, 30% of the first model's streams end without warning at a random 20 to 80% of the answer, after the client has started reading it. Switchyard should finish each one on the second model in the same stream (mid-stream failover, spec 7.6). Every request streams, since only streamed answers are continued; a cut non-streamed answer is an ordinary failover. 30% stays under the breaker's 40% rule, so most requests still reach the primary and hundreds of them are continued; a run of 5 cuts in a row can still open the breaker for a while.

| Check | Target | Measured | Verdict |
| --- | --- | --- | --- |
| Requests that completed | lower bound >= 99% | 100.00% (3000 of 3000; 95% interval 99.87% to 100.00%) | pass |
| Requests finished after a mid-stream failover that completed | lower bound >= 99% | 100.00% (424 of 424; 95% interval 99.10% to 100.00%) | pass |

## Per seed

| Seed | Requests | Succeeded | Mid-stream failovers | Attempts seen by the mocks | Attempts counted by the gateway | Probes seen by the mocks | Failed requests: p50, max latency | Errors |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 600 | 600 | 92 | 692 | 692 | 6 | none failed | none |
| 2 | 600 | 600 | 45 | 645 | 645 | 9 | none failed | none |
| 3 | 600 | 600 | 127 | 727 | 727 | 2 | none failed | none |
| 4 | 600 | 600 | 113 | 713 | 713 | 4 | none failed | none |
| 5 | 600 | 600 | 47 | 647 | 647 | 8 | none failed | none |

## Setup

- Traffic: 5 requests a second for 120 s, open loop, to `mock`; 100% streaming; `max_tokens` drawn from [16, 32, 48, 64].
- gateway: Switchyard, route group `mock` (mock-a/mock first, then mock-b/mock)
- primary: mockllm behind a gate, serving mock-a/mock
- secondary: mockllm behind a gate, serving mock-b/mock
- Fault: `cut_after_tokens` (fraction=[0.2, 0.8], rate=0.3) on primary from 0 s to 120 s.

## Environment

- date: 2026-10-30 06:13 UTC
- gateway: http://localhost:8000
- commit: 5df9e15
- host: Linux x86_64, 2 CPUs
- python: 3.12.3
