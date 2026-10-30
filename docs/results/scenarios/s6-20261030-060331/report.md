# S6: The primary stops sending tokens partway through the answer

Verdict: **pass** · 5 seeds · 3,000 requests · 2026-10-30 06:24 UTC · gateway http://localhost:8000

For the whole run, 30% of the first model's streams stop sending tokens at a random 20 to 80% of the answer and keep the connection open, sending a keep-alive comment every second as DeepSeek does while queued. Keep-alives aren't content, so the 5 s stall limit should still fire, and Switchyard should finish the answer on the second model. Every request streams, and 30% stays under the breaker's 40% rule, as in S5.

| Check | Target | Measured | Verdict |
| --- | --- | --- | --- |
| Requests that completed | lower bound >= 99% | 100.00% (3000 of 3000; 95% interval 99.87% to 100.00%) | pass |
| Requests finished after a mid-stream failover that completed | lower bound >= 99% | 100.00% (882 of 882; 95% interval 99.57% to 100.00%) | pass |
| Stall noticed after the last token, stall limit (5 s) plus 1 s | within 6 s, worst request | worst 5.02 s, median 5.00 s, over 882 failovers | pass |

## Per seed

| Seed | Requests | Succeeded | Mid-stream failovers | Attempts seen by the mocks | Attempts counted by the gateway | Probes seen by the mocks | Failed requests: p50, max latency | Errors |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 600 | 600 | 169 | 769 | 769 | 0 | none failed | none |
| 2 | 600 | 600 | 173 | 773 | 773 | 0 | none failed | none |
| 3 | 600 | 600 | 171 | 771 | 771 | 0 | none failed | none |
| 4 | 600 | 600 | 183 | 783 | 783 | 0 | none failed | none |
| 5 | 600 | 600 | 186 | 786 | 786 | 0 | none failed | none |

## Setup

- Traffic: 5 requests a second for 120 s, open loop, to `mock`; 100% streaming; `max_tokens` drawn from [16, 32, 48, 64].
- gateway: Switchyard, route group `mock` (mock-a/mock first, then mock-b/mock)
- primary: mockllm behind a gate, serving mock-a/mock
- secondary: mockllm behind a gate, serving mock-b/mock
- Fault: `stall_after_tokens` (fraction=[0.2, 0.8], rate=0.3, keep_alive_s=1) on primary from 0 s to 120 s.

## Environment

- date: 2026-10-30 06:24 UTC
- gateway: http://localhost:8000
- commit: 5df9e15
- host: Linux x86_64, 2 CPUs
- python: 3.12.3
