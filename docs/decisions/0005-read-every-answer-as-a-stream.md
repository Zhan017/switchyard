# 5. Read every answer from the provider as a stream

Date: 2026-10-28 · Status: accepted, confirmed 2026-10-29 on a 4-vCPU runner

## Context

Week 3 adds failover. To fail over, the gateway must notice that a provider is broken, and the cheapest signal is time: no first token within 5 s, or no new token for 5 s. For a streamed request both are visible as they happen.

A non-streaming request gives no such signal. The provider sends nothing, not even response headers, until the whole answer is ready, which can take a minute for a long answer. A provider that has hung looks exactly like one that is still writing, until the 120 s request deadline. In scenario S8 half the traffic is non-streaming, so a hung provider would hold those requests for two minutes, and the breaker would not see five failures in a row until long after the outage started.

## Decision

Read every answer from the provider as a stream, including answers to non-streaming requests. The gateway collects the stream and sends the client one JSON response at the end. The first-token, stall and deadline limits then apply to every request, and every request reaches the same commit point logic in the attempt runner.

## Consequences

- A hung provider is caught within 5 s on every request, streamed or not, so failover and the breaker work the same way for both.
- Parsing a stream costs more CPU than parsing one body. On GitHub's 4-vCPU runner ([results](../results/overhead-2026-10-29-4vcpu.md)), non-streaming overhead at 200 requests a second is 3.2 ms at p50 and 4.8 ms at p95, against 0.5 ms for the week 2 code on the same runner. The target, p95 under 10 ms at 200 requests a second, is met. Non-streaming saturates between 450 and 500 requests a second.
- On a shared 2-vCPU machine ([results](../results/overhead-2026-10-28-week3.md)) the cost was larger: 7.6 ms at p50 at 100 requests a second, and 22 ms at p95 at 200. About half of it there was mockllm writing a stream instead of one body.
- Usage comes from the stream's usage chunk, which OpenAI sends only with `stream_options.include_usage` (always set) and may skip when a stream is cut. Cut answers already get estimated usage.
- A provider whose non-streaming API behaves differently from its streaming API would be a surprise. None of the three does today.

## Alternatives considered (2026-10-29)

- **A per-route switch back to plain non-streaming calls.** Rejected: it adds a second path on which a hung provider holds a request for the full 120 s deadline and trips the breaker late, plus the tests for it, to save about 3 ms on requests that take seconds against real providers. Revisit if a provider or model supports only non-streaming calls.
- **A timeout on the whole non-streaming call, scaled to `max_tokens`.** Rejected: it can't tell a slow long answer from a hung provider, and it fires late either way.
