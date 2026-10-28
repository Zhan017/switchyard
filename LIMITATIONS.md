# Limitations

What Switchyard does not do, or does only partly, as of week 2. Each item says whether it is planned.

## Routing and resilience

- **One attempt per request.** A request goes to the first model in its route that can serve it. Retries, circuit breakers, failover and the retry budget arrive in week 3; mid-stream failover in week 4.
- **No request queue.** When a provider fails, the request fails fast with 503 and `Retry-After`. Deliberate: see the spec, section 2.2.

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
- **No request records in Postgres yet.** Cost and tokens are in Prometheus and in one JSON log line per request. Postgres records arrive in week 3.

## Security

- **The shipped config has development keys** (`sy-dev-local-key` for the tenant, `sy-admin-local-key` for admin). The gateway logs a warning at startup while they are configured, and `compose.yml` binds every port to localhost. Replace both hashes before running it anywhere others can reach: anyone with the tenant key can spend your provider keys.

## Testing and measurement

- **Provider stream fixtures are written from the providers' documentation,** not recorded. `scripts/record_stream.py` records real streams; recorded fixtures will be added next to the written ones.
- **Gateway overhead was measured on a shared 2-vCPU machine,** with mockllm and the load generator on the same machine. Treat the numbers as an upper bound; see `docs/results/`.
