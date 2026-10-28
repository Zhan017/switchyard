# 5. Read every answer from the provider as a stream

Date: 2026-10-28 · Status: accepted

## Context

Week 3 adds failover. To fail over, the gateway must notice that a provider is broken, and the cheapest signal is time: no first token within 5 s, or no new token for 5 s. For a streamed request both are visible as they happen.

A non-streaming request gives no such signal. The provider sends nothing, not even response headers, until the whole answer is ready, which can take a minute for a long answer. A provider that has hung looks exactly like one that is still writing, until the 120 s request deadline. In scenario S8 half the traffic is non-streaming, so a hung provider would hold those requests for two minutes, and the breaker would not see five failures in a row until long after the outage started.

## Decision

Read every answer from the provider as a stream, including answers to non-streaming requests. The gateway collects the stream and sends the client one JSON response at the end. The first-token, stall and deadline limits then apply to every request, and every request reaches the same commit point logic in the attempt runner.

## Consequences

- A hung provider is caught within 5 s on every request, streamed or not, so failover and the breaker work the same way for both.
- Parsing a stream costs more CPU than parsing one body. Measured against mockllm on a shared 2-vCPU machine (all numbers in [the week 3 overhead results](../results/overhead-2026-10-28-week3.md)): non-streaming overhead went from 1.5 ms to 7.6 ms at p50 at 100 requests a second. About half of the difference is mockllm writing a stream instead of one body (its whole streamed answer takes 3.9 ms longer than its unary one); the rest is the gateway reading it. The gateway's own work outside the provider exchange stays under 0.5 ms.
- On that machine the non-streaming target (p95 under 10 ms at 200 requests a second) is no longer met: p95 is 22 ms. The target was set for a 4-vCPU container; it needs measuring there.
- Usage comes from the stream's usage chunk, which OpenAI sends only with `stream_options.include_usage` (always set) and may skip when a stream is cut. Cut answers already get estimated usage.
- A provider whose non-streaming API behaves differently from its streaming API would be a surprise. None of the three does today.
