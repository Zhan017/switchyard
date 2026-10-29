# Gateway overhead on a 4-vCPU runner, 2026-10-29

Measured on GitHub's hosted runners for public repositories (4 vCPUs, 16 GB, Linux x86_64, Python 3.12.3), the machine size the spec's targets name, with [`.github/workflows/bench.yml`](../../.github/workflows/bench.yml). Each run measures the week 3 code (`5a77e49`) and then the week 2 code (`91ab8a2`) on the same runner, so the two can be compared. mockllm, the gateway and the load generator share the runner. Same method as before: `bench.overhead`, open-loop load for 15 s per row, mockllm with a fixed 50 ms to first token and 32 tokens. Overhead is the gateway's percentile minus the direct percentile: time to first content byte when streaming, total time otherwise.

Anyone can rerun it from the Actions tab. Runners differ: the two runs below landed on different CPUs.

## Run 1: AMD EPYC 9V74 ([log](https://github.com/Zhan017/switchyard/actions/runs/40820912832))

| Rate (req/s) | Mode | Week 3 p50 | Week 3 p95 | Week 2 p50 | Week 2 p95 |
| --- | --- | --- | --- | --- | --- |
| 100 | stream | 0.68 ms | 0.67 ms | 0.54 ms | 0.82 ms |
| 100 | unary | 2.83 ms | 3.68 ms | 0.61 ms | 0.19 ms |
| 200 | stream | 0.65 ms | 0.96 ms | 0.58 ms | 0.69 ms |
| 200 | unary | 3.15 ms | 4.76 ms | 0.47 ms | 0.51 ms |
| 300 | stream | 1.34 ms | 1.46 ms | 1.24 ms | 1.25 ms |
| 300 | unary | 4.15 ms | 5.45 ms | 0.54 ms | 0.63 ms |
| 400 | stream | 18.18 ms | 91.32 ms | 1.91 ms | 5.20 ms |
| 400 | unary | 4.31 ms | 5.98 ms | 0.45 ms | 0.72 ms |

## Run 2: Intel Xeon Platinum 8573C ([log](https://github.com/Zhan017/switchyard/actions/runs/40821854456))

| Rate (req/s) | Mode | Week 3 p50 | Week 3 p95 | Week 2 p50 | Week 2 p95 |
| --- | --- | --- | --- | --- | --- |
| 300 | stream | 1.83 ms | 2.37 ms | 1.78 ms | 2.25 ms |
| 300 | unary | 6.70 ms | 8.06 ms | 0.65 ms | 0.76 ms |
| 350 | stream | 2.79 ms | 9.08 ms | 2.85 ms | 7.72 ms |
| 350 | unary | 7.92 ms | 10.24 ms | 0.70 ms | 0.86 ms |
| 400 | stream | 11.63 ms | 68.34 ms | 7.18 ms | 59.97 ms |
| 400 | unary | 7.39 ms | 15.28 ms | 0.66 ms | 0.98 ms |
| 450 | stream | 67.96 ms | 509.26 ms | 36.56 ms | 98.32 ms |
| 450 | unary | 6.66 ms | 11.65 ms | 0.68 ms | 0.84 ms |
| 500 | stream | 610.94 ms | 778.22 ms | 69.22 ms | 129.81 ms |
| 500 | unary | 13.76 ms | 34.17 ms | 0.74 ms | 0.80 ms |

A p95 below its p50 (week 2, unary, 100 req/s) is not an error: each column is the gateway's percentile minus the direct percentile, and those can differ by less than the noise.

## What this shows

- **Both targets are met at 200 requests a second** (run 1): 0.96 ms at p95 added to time to first token when streaming, 4.76 ms at p95 when not, against 10 ms for each. Run 2 started at 300 req/s, so it has no 200 row.
- **Non-streaming costs 3 to 7 ms more than in week 2**, because answers are now read from the provider as a stream ([decision 5](../decisions/0005-read-every-answer-as-a-stream.md)). Part of that is mockllm, whose answers now take as long to generate as its streams (the direct unary column rose from about 82 ms to 84 ms). In exchange, a hung provider is caught within 5 s on every request.
- **Streaming saturates below 400 requests a second** for the week 3 code on both runners: its p95 passes 25 ms between 300 and 400 on run 1, and between 350 and 400 on run 2. Week 2's code saturated at the same point on run 2 and held to 5.2 ms at 400 on run 1. Past saturation the week 3 code degrades faster (at 450 req/s, 509 ms against 98 ms), which suggests more work per request that shows only under overload. Not profiled yet.
- **Non-streaming saturates between 450 and 500 requests a second** (run 2). Week 2's code didn't saturate in the tested range.
- These runs are on a shared runner with the load generator and mockllm competing for the same 4 vCPUs, so treat them as upper bounds. The [2-vCPU results](overhead-2026-10-28-week3.md) from week 3 show the same pattern on a smaller, noisier machine.
