# Switchyard

An LLM gateway that keeps answers flowing when providers fail. When a provider dies halfway through a streamed answer, Switchyard finishes the answer on another provider, and measures how well each way of doing that works.

**Status:** week 2 of 5. The proxy works: one OpenAI-compatible endpoint in front of OpenAI, Anthropic and DeepSeek, streaming or not, with cost per tenant and feature. Breakers and failover come in week 3, mid-stream failover in week 4. The full plan is in [SPEC.md](SPEC.md).

## The question

API7's AISIX gateway already continues broken streams, and LiteLLM did until it removed the feature from chat completions. What nobody publishes is how good the joins are: how often a reader can tell the answer was interrupted, by strategy and provider pair, and what each failover costs. That table is the point of this project.

## Measured so far

- **Can another model finish an interrupted answer?** Yes, well enough to build on. Read at the join point, DeepSeek's native continuation looked seamless on 25 of 30 joins, and instructed continuation with seam repair on 55 of 90. [Week 1 report](eval/seam/results/20261023-050151/report.md).
- **What does the gateway cost?** At 200 streamed requests a second it adds 1.7 ms at p50 and 2.6 ms at p95 to time to first token, on a shared 2-vCPU machine. The first version collapsed between 150 and 200 a second; profiling traced it to the HTTP client, and switching from httpx to aiohttp cut its CPU per stream about twelvefold. [Results](docs/results/overhead-2026-10-28.md), [decision 4](docs/decisions/0004-aiohttp-for-upstream-calls.md).

## Try it

```sh
docker compose up --build
```

That starts the gateway on :8000, mockllm (a fake provider you can break on command) on :8001, and Prometheus on :9090, all on localhost only. Put provider keys in `.env` (see `.env.example`) to use the real models; a route skips any provider without a key, and the `mock` route always works. The shipped config uses development keys, shown below; replace them before running it anywhere others can reach.

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

`chat-fast` is a route group: Claude Haiku 5.5, GPT-6 Luna and DeepSeek Flash, in that order. Each request goes to the first model that supports what it asks for. Break the mock provider and watch what happens:

```sh
curl localhost:8001/chaos -d '{"mode": "error_event", "params": {"tokens": 5}}'
```

## What works now

- `POST /v1/chat/completions` in OpenAI's format: streaming and not, tools, JSON mode and JSON schemas, translated to each provider's API.
- Tenants with hashed keys and a feature header for cost attribution; cost per tenant, feature and model in Prometheus, using each provider's price table, including Claude's long-context tier and DeepSeek's off-peak discount.
- Time limits on the first token, on stalls and on the whole request. Provider failures before the first token become proper HTTP errors; after it, a clean error event the OpenAI SDKs raise.
- Tool calls forwarded only once complete, and JSON answers only once valid, so a failover never has half of either to take back ([decision 3](docs/decisions/0003-forward-tool-calls-whole.md)).
- mockllm with injectable faults: error rates, status codes, slow first tokens, cut streams, stalls with keep-alives, malformed chunks, error events inside a 200 stream.

What doesn't work yet is in [LIMITATIONS.md](LIMITATIONS.md).

## Development

```sh
uv sync
uv run pytest                        # unit, contract and end-to-end tests
uv run python -m bench.overhead      # gateway overhead against mockllm
uv run python -m eval.seam.spike --dry-run   # the week 1 seam test, offline
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
