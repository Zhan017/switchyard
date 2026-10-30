# Limitations

What Switchyard does not do, or does only partly, as of week 4. Each item says whether it is planned.

## Routing and resilience

- **Mid-stream failover continues text only.** When a stream dies after text went out, another model finishes it in the same stream. It doesn't continue a tool call, a refusal, a JSON answer (those are buffered and restarted), or a request with stop sequences (a stop sequence could span the join): those end with an error event, or as a tool-call turn when complete tool calls already went out. At most 2 continuation attempts per request.
- **The continuation strategies aren't measured yet.** Each model's `continuation` setting comes from the week 1 seam test, read by one person on 30 prompts. The full evaluation, with three judges and hand labels, decides them. Mid-stream failover has run only against mockllm, whose text is random words, so the scenarios show that answers complete, not that the joins read well.
- **A repaired continuation starts a little late.** Seam repair holds the first 200 to 400 characters of a continuation before sending any of it, and the delay is recorded as seam latency.
- **No request queue.** When every provider is down, open or cooling down, the request fails fast with 503 and `Retry-After`. Deliberate: see the spec, section 2.2.
- **Non-streaming requests are read from the provider as a stream** (decision 5), so they cost about as much gateway CPU as streamed ones. In exchange, a hung provider is caught within the first-token limit on every request.
- **Replicas agree within about half a second,** not instantly: breakers are shared through Redis by a background sync (decision 6). The five-in-a-row trip rule and cooldowns count per replica. Redis Cluster isn't supported.
- **Breaker windows are bucketed by the wall clock,** so replicas agree on buckets. A large clock jump on one replica skews its counts for up to a minute.

## API coverage

- **Text only.** Image and audio content, `n` above 1 and `logprobs` are rejected with 400. Not planned for v1.
- **JSON modes depend on the model.** Claude takes JSON only with a schema (`json_schema`); DeepSeek takes only `json_object`. Requests are routed to a model that supports what they ask for, or rejected with `unsupported_feature`.
- **Tool calls and JSON are not streamed piece by piece.** Each tool call is sent once complete, and a JSON answer once complete and valid (decision 3).
- **No reasoning models** in v1 route groups. Thinking is turned off on Claude Haiku 5.5 and DeepSeek, and `reasoning_effort` is `none` on GPT-6 Luna.

## Cost accounting

- **Estimated usage is a character count,** about 3.5 characters per token plus a 15% margin, used only when a provider doesn't report usage (usually a stream cut short). Estimated numbers are always flagged and reported separately.
- **DeepSeek's off-peak discount ignores Chinese public holidays,** so costs on those days are overstated.
- **Attempts that fail before the first token are recorded with zero tokens,** since providers generally don't bill them. A failure mid-answer counts the tokens received, estimated if the provider never reported them.
- **Request records are best effort.** Rows are written to Postgres in batches by a background task. If Postgres is slow or down, queued rows are dropped and counted (`switchyard_records_dropped_total`); Prometheus and the log line still have every request. Rows are kept 30 days.
- **Probes cost a little.** An open breaker is probed every 10 s with a request for one output token (16 on GPT-6 Luna, which rejects fewer than 3), about six a minute per open model, recorded with cost class `probe`.

## Security

- **The shipped config has development keys** (`sy-dev-local-key` for the tenant, `sy-admin-local-key` for admin). The gateway logs a warning at startup while they are configured, and `compose.yml` binds every port to localhost. Replace both hashes before running it anywhere others can reach: anyone with the tenant key can spend your provider keys.

## Testing and measurement

- **The gateway has met the real APIs only in a smoke test:** one request per check for each of the three models (text, tools, JSON, a temperature and the breaker's probe), all passing on 2026-10-29, OpenAI's after a probe fix ([first run](docs/results/smoke/20261029-070701/report.md), [rerun](docs/results/smoke/20261029-105741/report.md)). Load, failures and the outage scenarios run on mockllm.
- **Recorded real streams cover the happy path only.** Text and parallel tool-call streams from all three providers, recorded on 2026-10-29, are replayed through the adapters in the contract tests. Error events, refusals and cut streams are tested on fixtures written from the providers' documentation.
- **Gateway overhead is measured on shared machines,** with mockllm and the load generator competing for the same CPUs: GitHub's 4-vCPU runners (which vary in CPU from run to run) and a 2-vCPU sandbox. Treat the numbers as upper bounds; see `docs/results/`.
- **Past saturation the week 3 code degrades faster than week 2's** (streaming at 450 requests a second: 509 ms at p95 against 98 ms on the same runner). The cause hasn't been profiled.
- **Against another gateway, attempts during a refused-connection outage are undercounted.** The scenario CLI counts attempts at the gateway when it is Switchyard, and otherwise at the mock servers, which can't see a connection they refused. The report says which count it used.
- **Outage scenarios run against mockllm,** not real providers. `refuse_connections` is a real refused TCP connection (mockllm's gate), but real providers fail in ways a mock doesn't imitate.
