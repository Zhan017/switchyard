# 2. Wait for the provider's first event before answering a stream

Date: 2026-10-28 · Status: accepted

## Context

A streaming response commits to HTTP 200 once its headers are sent. If the gateway sent headers as soon as it called the provider, every failure before the first token (a 429, a 500, a timeout) would reach the client as a 200 with an error event inside, and a failover before the first token would be impossible to tell apart from a mid-stream one.

## Decision

The gateway awaits the provider's first content event (or its failure) before returning the streaming response. Until then nothing is sent.

## Consequences

- Failures before the first token get real status codes: 503 with `Retry-After`, 504, or 400 passed through. Clients and their retry logic see what they expect.
- From week 3, a failure before the first token is an ordinary failover the client never sees.
- The client's time to headers equals the provider's time to first token plus the gateway's overhead, which is no worse than sending headers early and then waiting.
