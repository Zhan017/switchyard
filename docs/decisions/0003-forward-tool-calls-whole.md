# 3. Forward tool calls only when complete, and JSON answers only when valid

Date: 2026-10-28 · Status: accepted

## Context

Providers stream tool-call arguments as JSON fragments. If a provider dies halfway through a call, the client already holds half a call that no other provider can finish reliably. The same is true of a JSON answer: a continued JSON fragment can't be validated.

## Decision

- Tool-call fragments are buffered per call and sent as one delta once the call is complete: when the next call starts or the answer finishes.
- When `response_format` asks for JSON, the whole answer is buffered and sent once it is complete and parses. Invalid JSON with `finish_reason: stop` is treated as a provider failure.

## Consequences

- A mid-stream failure never leaves the client with half a tool call or half a JSON object.
- Clients see tool calls and JSON later than they would with fragment streaming. Agents and JSON consumers act on the whole object anyway, so the cost is small; it will be measured in week 4.
