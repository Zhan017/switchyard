# Switchyard

An LLM gateway that keeps answers flowing when providers fail. When a provider dies halfway through a streamed answer, Switchyard finishes the answer on another provider, and measures how well each way of doing that works.

**Status:** week 4 of 5. One OpenAI-compatible endpoint in front of OpenAI, Anthropic and DeepSeek, with circuit breakers, retries, failover, and mid-stream failover that finishes a broken answer on another model; cost per tenant and feature. Measuring how well those joins read comes next. The full plan is in [SPEC.md](SPEC.md).

## The question

API7's AISIX gateway already continues broken streams, and LiteLLM did until it removed the feature from chat completions. What nobody publishes is how good the joins are: how often a reader can tell the answer was interrupted, by strategy and provider pair, and what each failover costs. That table is the point of this project.

## Measured so far

- **Can another model finish an interrupted answer?** Yes, well enough to build on. Read at the join point, DeepSeek's native continuation looked seamless on 25 of 30 joins, and instructed continuation with seam repair on 55 of 90. [Week 1 report](eval/seam/results/20261023-050151/report.md).
- **Does failover work?** In four outage scenarios with 5 seeds each, against two mock providers: with the primary failing half its requests, 6,000 of 6,000 requests completed; with it down for 5 minutes, 20,999 of 21,000 (the one miss was a stream cut mid-answer, which is week 4's job). With every provider down, requests got a 503 in about 2 ms instead of piling up, 9,000 of them caused 43 upstream attempts, and service was back about 20 s after recovery. All targets were committed before the runs. [Results](docs/results/scenarios-2026-10-28.md).
- **Does mid-stream failover finish broken answers?** On mocks, yes: with 30% of the primary's streams cut or stalled partway through, all 6,000 requests completed, including all 1,306 that were finished on the second model in the same stream. Stalls were noticed after 5.0 s despite keep-alives. Whether those joins read well needs real models; that's the seam evaluation, next. [Results](docs/results/scenarios-2026-10-30.md).
- **What does the gateway cost?** On a 4-vCPU GitHub runner at 200 requests a second, 0.96 ms at p95 added to time to first token when streaming and 4.8 ms when not, under the 10 ms target for each. Non-streaming costs about 3 ms more than in week 2, because answers are now read from the provider as a stream so a hung provider is caught in 5 s. Streaming saturates between 350 and 400 requests a second. The first version collapsed between 150 and 200 a second; switching from httpx to aiohttp cut its CPU per stream about twelvefold. [4-vCPU results](docs/results/overhead-2026-10-29-4vcpu.md) (rerun them from the Actions tab), [earlier results](docs/results/), decisions [4](docs/decisions/0004-aiohttp-for-upstream-calls.md) and [5](docs/decisions/0005-read-every-answer-as-a-stream.md).

## Try it

```sh
docker compose up --build
```

That starts the gateway on :8000, two mockllm providers (fakes you can break on command) on :8001 and :8002 with their fault controls on :8101 and :8102, Redis, Postgres and Prometheus on :9090, all on localhost only. Put provider keys in `.env` (see `.env.example`) to use the real models; a route skips any provider without a key, and the `mock` route always works. The shipped config uses development keys, shown below; replace them before running it anywhere others can reach.

```sh
curl localhost:8000/v1/chat/completions \
  -H 'authorization: Bearer sy-dev-local-key' \
  -H 'x-switchyard-feature: chat' \
  -H 'content-type: application/json' \
  -d '{"model": "mock", "messages": [{"role": "user", "content": "hi"}], "stream": true}'
```

Any OpenAI client works by changing its base URL:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="sy-dev-local-key",
    default_headers={"X-Switchyard-Feature": "chat"},
)
client.chat.completions.create(model="chat-fast", messages=[{"role": "user", "content": "hi"}])
```

`chat-fast` is a route group: Claude Haiku 5.5, GPT-6 Luna and DeepSeek Flash, in that order. Each request goes to the first healthy model that supports what it asks for. Take the first mock provider down and watch requests fail over to the second, and its breaker open:

```sh
curl localhost:8101/chaos -d '{"mode": "refuse_connections", "duration_s": 60}'
curl -s localhost:8000/admin/providers -H 'authorization: Bearer sy-admin-local-key'
```

Run an outage scenario against it (any OpenAI-compatible URL works with `--gateway`):

```sh
uv run switchyard-chaos run chaos/scenarios/s4.yaml
```

## What works now

- `POST /v1/chat/completions` in OpenAI's format: streaming and not, tools, JSON mode and JSON schemas, translated to each provider's API.
- Tenants with hashed keys and a feature header for cost attribution; cost per tenant, feature and model in Prometheus, using each provider's price table, including Claude's long-context tier and DeepSeek's off-peak discount.
- Time limits on the first token, on stalls and on the whole request, for streamed and non-streamed requests alike ([decision 5](docs/decisions/0005-read-every-answer-as-a-stream.md)).
- Circuit breakers per provider and model: trip on 40% failures over 60 s, 5 failures in a row, or slow first tokens; recover through tiny probes every 10 s, at most 35 s after the provider is healthy. A 429 is a cooldown, not a failure.
- Retries on the same provider only before the first token, at most once, within a 20% retry budget; failover down the route, at most 3 attempts. Refusals and content filters are never failed over.
- Breakers and health shared between replicas through Redis with no Redis call on the request path, failing open to local state when Redis is down ([decision 6](docs/decisions/0006-no-redis-on-the-request-path.md)). Request and attempt records in Postgres, batched so they never block a request.
- Smoke-tested against the real OpenAI, Anthropic and DeepSeek APIs: text, streaming, tools, JSON and the breaker's probe on each model ([report](docs/results/smoke/20261029-070701/report.md)). The first run found that GPT-6 Luna rejects one-token requests, so its breaker could never have closed; fixed and rerun.
- Mid-stream failover: when a stream dies after text went out, the next healthy model finishes the answer in the same stream, with the model's configured strategy (restart, native, instructed, instructed with seam repair, or anchored), heartbeats while it starts, and one usage total. Seam repair trims repeated words, prefaces and broken joins in the first 200 to 400 characters.
- Provider failures before anything reached the client become a failover, or a proper HTTP error if nothing is left. After text went out and no continuation is possible, a clean error event the OpenAI SDKs raise.
- Tool calls forwarded only once complete, and JSON answers only once valid, so a failover never has half of either to take back ([decision 3](docs/decisions/0003-forward-tool-calls-whole.md)).
- mockllm with injectable faults: error rates, status codes, slow first tokens, cut streams, stalls with keep-alives, malformed chunks, error events inside a 200 stream, hangs, flapping, recorded-stream replay, and real refused connections through a TCP gate.
- `switchyard-chaos`: outage scenarios with fixed targets, judged on 95% intervals, with JSON and Markdown reports.

What doesn't work yet is in [LIMITATIONS.md](LIMITATIONS.md).

## Development

```sh
uv sync
uv run pytest                        # unit, contract and end-to-end tests
uv run python -m bench.overhead      # gateway overhead against mockllm
uv run switchyard-chaos run chaos/scenarios/s1.yaml   # needs compose (or the same stack) running
uv run python -m eval.seam.spike --dry-run   # the week 1 seam test, offline
uv run python scripts/smoke_real.py --dry-run  # the real-API smoke test, against mockllm
```

The end-to-end tests run a real mockllm server with the gateway in front of it, including the official OpenAI SDK as the client. The provider contract tests run each adapter against stream fixtures written from the providers' documentation; `scripts/record_stream.py` records real ones.

## Week 1 seam test

The test asks Claude, GPT and DeepSeek for full answers to 30 prompts, cuts each answer at a random point, and has another model finish it five ways:

| Strategy | What happens |
| --- | --- |
| R, restart | The fallback answers again from the top, so the reader sees the start twice |
| I, instructed | The fallback is told the answer was cut off and asked to continue from the exact point |
| I+S | The same continuation as I, cleaned up by [seam repair](src/switchyard/streaming/seam.py) |
| A, anchored | The partial answer is quoted in the request, the fallback starts by copying its last few words, and seam repair cuts there |
| N, native | DeepSeek's beta prefix completion continues the partial answer directly |

Run it for real with keys in `.env` (about 360 API calls; the script prints a worst-case cost and stops if it is over the budget in `eval/seam/config.yaml`):

```sh
uv run python -m eval.seam.spike
```

Seam repair only post-processes text, so its rules can be improved against a finished run without new API calls: `uv run python -m eval.seam.rescore eval/seam/results/<run>`.

## Docs

- [SPEC.md](SPEC.md): the plan, scope and targets
- [LIMITATIONS.md](LIMITATIONS.md): what it doesn't do yet
- [docs/decisions](docs/decisions): why it is built the way it is
- [docs/results](docs/results): measurements

## License

Apache 2.0
