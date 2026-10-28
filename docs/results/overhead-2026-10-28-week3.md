# Gateway overhead after week 3, 2026-10-28

Week 3 put every request through the attempt runner (retries, failover, health recording) and reads non-streaming answers from the provider as a stream ([decision 5](../decisions/0005-read-every-answer-as-a-stream.md)). This measures what that costs.

Machine: 2 vCPU, Linux x86_64, Python 3.12.3, shared: mockllm, the gateway and the load generator all ran on it and competed for CPU. Same method as the [week 2 results](overhead-2026-10-28.md): `uv run python -m bench.overhead --rates 100 200 300 --seconds 15`, open-loop load from a raw-socket client, mockllm with a fixed 50 ms to first token and 32 tokens. Overhead is the gateway's percentile minus the direct percentile: time to first content byte when streaming, total time otherwise. No Redis or Postgres configured.

This machine was slower than the one week 2 was measured on, so the week 2 code (commit `91ab8a2`) was measured again right after the week 3 code, on the same machine. Compare the two tables with each other, not with the week 2 report.

**Week 3 code:**

| Rate (req/s) | Mode | Direct p50 | Direct p95 | Gateway p50 | Gateway p95 | Overhead p50 | Overhead p95 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 100 | stream | 51.8 ms | 53.7 ms | 54.0 ms | 59.1 ms | 2.25 ms | 5.40 ms |
| 100 | unary | 88.3 ms | 92.4 ms | 95.8 ms | 102.6 ms | 7.57 ms | 10.14 ms |
| 200 | stream | 52.0 ms | 55.6 ms | 63.0 ms | 109.5 ms | 11.07 ms | 53.92 ms |
| 200 | unary | 88.0 ms | 93.3 ms | 100.3 ms | 115.3 ms | 12.36 ms | 22.00 ms |
| 300 | stream | 52.3 ms | 60.2 ms | 2088.0 ms | 3219.8 ms | 2035.73 ms | 3159.67 ms |
| 300 | unary | 87.7 ms | 92.5 ms | 194.9 ms | 370.7 ms | 107.21 ms | 278.15 ms |

**Week 2 code, same machine, minutes later:**

| Rate (req/s) | Mode | Direct p50 | Direct p95 | Gateway p50 | Gateway p95 | Overhead p50 | Overhead p95 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 100 | stream | 51.7 ms | 52.9 ms | 53.7 ms | 56.5 ms | 1.96 ms | 3.56 ms |
| 100 | unary | 82.8 ms | 84.0 ms | 84.2 ms | 86.0 ms | 1.48 ms | 2.04 ms |
| 200 | stream | 51.8 ms | 54.1 ms | 58.8 ms | 109.9 ms | 7.05 ms | 55.78 ms |
| 200 | unary | 82.5 ms | 83.6 ms | 83.9 ms | 86.0 ms | 1.42 ms | 2.46 ms |
| 300 | stream | 52.0 ms | 89.0 ms | 818.4 ms | 1017.0 ms | 766.46 ms | 928.06 ms |
| 300 | unary | 82.4 ms | 83.7 ms | 83.8 ms | 85.9 ms | 1.44 ms | 2.19 ms |

## What this shows

- **Streaming is about where it was.** At 100 requests a second, +0.3 ms at p50 and +1.8 ms at p95 over week 2. At 200 a second both versions are near this machine's limit (p95 54 ms and 56 ms) and both fall over at 300. Runs on this machine vary a lot at those rates, so the 200 and 300 rows say "saturated", not much more.
- **Non-streaming got slower, by design.** At 100 a second, 7.6 ms at p50 against 1.5 ms. Part of that is mockllm itself: its whole streamed answer takes 91.4 ms at p50 against 87.5 ms for the same answer in one body (measured on its own, 100 requests a second). So about 4 ms is the provider writing a stream and about 4 ms is the gateway reading it. The gateway's own histogram puts its work outside the provider exchange under 0.5 ms.
- **Target status.** The spec asks for p95 under 10 ms at 200 a second on a 4-vCPU container. On this 2-vCPU shared machine, non-streaming p95 is 22 ms at 200 a second (not met) and streaming p95 54 ms (week 2's code: 56 ms on the same machine). Neither has been measured on 4 vCPUs yet.
- mockllm's non-streaming answers now take as long to generate as streamed ones (one sleep per token), which is why the unary direct column rose from 82.8 ms to 88.3 ms. Before, a gateway reading the answer as a stream would have looked slower than it is.

## Profile

A sampling profile at 200 streamed requests a second (py-spy, 12 s) put the breaker, health and runner code under 1% of samples. Most time was in socket writes and the event loop. Two small costs it showed were removed before these runs: an extra async generator wrapping every token, and Postgres rows built when Postgres isn't configured.
