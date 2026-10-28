# Limitations

What Switchyard does not do, or does only partly, as of week 3. Each item says whether it is planned.

## Routing and resilience

- **Failover happens only before the client sees anything.** Once text, a refusal or a complete tool call has been sent, a provider failure ends the stream with an error event. Finishing the answer on another provider is mid-stream failover, in week 4.
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
- **OpenAI cache writes are not counted.** GPT-6 Luna has a cache-write price, but the usage field that would report written tokens isn't documented in a form the adapter reads yet.
- **Attempts that fail before the first token are recorded with zero tokens,** since providers generally don't bill them. A failure mid-answer counts the tokens received, estimated if the provider never reported them.
- **Request records are best effort.** Rows are written to Postgres in batches by a background task. If Postgres is slow or down, queued rows are dropped and counted (`switchyard_records_dropped_total`); Prometheus and the log line still have every request. Rows are kept 30 days.
- **Probes cost a little.** An open breaker is probed every 10 s with a one-token request, about six a minute per open model, recorded with cost class `probe`.

## Security

- **The shipped config has development keys** (`sy-dev-local-key` for the tenant, `sy-admin-local-key` for admin). The gateway logs a warning at startup while they are configured, and `compose.yml` binds every port to localhost. Replace both hashes before running it anywhere others can reach: anyone with the tenant key can spend your provider keys.

## Testing and measurement

- **Provider stream fixtures are written from the providers' documentation,** not recorded. `scripts/record_stream.py` records real streams; recorded fixtures will be added next to the written ones.
- **Gateway overhead was measured on a shared 2-vCPU machine,** with mockllm and the load generator on the same machine. Treat the numbers as an upper bound; see `docs/results/`. After week 3, non-streaming overhead at 200 requests a second (p95 22 ms) is over the 10 ms target on that machine; it hasn't been measured on the 4-vCPU container the target names.
- **Against another gateway, attempts during a refused-connection outage are undercounted.** The scenario CLI counts attempts at the gateway when it is Switchyard, and otherwise at the mock servers, which can't see a connection they refused. The report says which count it used.
- **Outage scenarios run against mockllm,** not real providers. `refuse_connections` is a real refused TCP connection (mockllm's gate), but real providers fail in ways a mock doesn't imitate.
