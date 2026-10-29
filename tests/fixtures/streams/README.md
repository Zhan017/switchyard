# Stream fixtures

Hand-written from the event shapes in each provider's API documentation (checked 2026-10-28), not recorded from live traffic. They pin the parsers to the documented formats: OpenAI chunks and the usage chunk (with cached and cache-written prompt tokens), tool-call deltas keyed by `index`, an error object inside a 200 stream, DeepSeek's `: keep-alive` comments and cache-hit usage, and Anthropic's `ping` events, `tool_use` blocks, `error` events and `refusal` stop reason.

Recorded real streams will be added next to these once `scripts/record_stream.py` has been run against each provider.
