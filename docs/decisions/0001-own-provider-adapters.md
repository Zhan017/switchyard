# 1. Own provider adapters instead of the LiteLLM SDK

Date: 2026-10-19 · Status: accepted

## Context

Switchyard needs full control of each provider's stream: where it can be cut, which events count as progress, what usage arrives and when, and how a partial answer can be continued on another provider. LiteLLM's SDK covers far more providers, but it hides exactly those details behind a common interface.

## Decision

Write small adapters over httpx for the providers in use (OpenAI and OpenAI-compatible servers, Anthropic, DeepSeek). Each adapter translates the canonical request to the provider's wire format and the provider's stream to a handful of canonical events (`TextDelta`, `ToolCallStart`, `ToolCallArgs`, `Finish`, `UsageReport`).

## Consequences

- Provider quirks are visible and tested: Anthropic `ping` and `error` events inside a 200 stream, DeepSeek `: keep-alive` comments and cache-hit usage, OpenAI's usage chunk after the finish chunk.
- Fewer providers. Adding one means writing an adapter and its contract tests.
- LiteLLM's proxy is still used, as the baseline the outage tests compare against.
