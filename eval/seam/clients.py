"""Minimal, non-streaming clients for the week 1 seam test.

These are deliberately small. The gateway's real streaming adapters come in week 2; this
test only needs finished text and token counts.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypedDict, cast

import httpx

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}
MAX_ATTEMPTS = 4


class Message(TypedDict):
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class Price:
    input: float
    cached_input: float
    output: float


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    finish_reason: str
    latency_s: float

    def cost(self, price: Price) -> float:
        uncached = self.input_tokens - self.cached_input_tokens
        return (
            uncached * price.input
            + self.cached_input_tokens * price.cached_input
            + self.output_tokens * price.output
        ) / 1_000_000


class ProviderError(Exception):
    pass


class Client(Protocol):
    family: str
    model: str
    supports_prefix: bool

    async def complete(
        self, messages: list[Message], max_tokens: int, prefix: bool = False
    ) -> Completion: ...


async def _post(
    http: httpx.AsyncClient, url: str, headers: dict[str, str], body: dict[str, Any]
) -> dict[str, Any]:
    """POST with retries on transient errors, honoring Retry-After."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = await http.post(url, headers=headers, json=body)
        except httpx.TransportError as error:
            if attempt == MAX_ATTEMPTS:
                raise ProviderError(f"{type(error).__name__}: {error}") from error
            await asyncio.sleep(_backoff(attempt, None))
            continue
        if response.status_code in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS:
            await asyncio.sleep(_backoff(attempt, response.headers.get("retry-after")))
            continue
        if response.status_code >= 400:
            raise ProviderError(f"HTTP {response.status_code}: {response.text[:500]}")
        return cast(dict[str, Any], response.json())
    raise ProviderError("unreachable")


def _backoff(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(float(retry_after), 60.0)
        except ValueError:
            pass
    return float(random.uniform(0, min(30.0, 2.0**attempt)))


class AnthropicClient:
    supports_prefix = False  # Claude 4.6 and later reject a trailing assistant message

    def __init__(self, http: httpx.AsyncClient, api_key: str, model: str, family: str) -> None:
        self.http, self.api_key, self.model, self.family = http, api_key, model, family

    async def complete(
        self, messages: list[Message], max_tokens: int, prefix: bool = False
    ) -> Completion:
        if prefix:
            raise ProviderError("Anthropic does not support prefix continuation")
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": messages,
            # Thinking is on by default on Claude Haiku 5.5. v1 has no reasoning models,
            # and temperature, top_p and top_k must be omitted on this model.
            "thinking": {"type": "disabled"},
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        started = time.monotonic()
        data = await _post(self.http, "https://api.anthropic.com/v1/messages", headers, body)
        text = "".join(block.get("text", "") for block in data.get("content", []))
        usage = data.get("usage", {})
        cached = int(usage.get("cache_read_input_tokens") or 0)
        return Completion(
            text=text,
            input_tokens=int(usage.get("input_tokens", 0)) + cached,
            cached_input_tokens=cached,
            output_tokens=int(usage.get("output_tokens", 0)),
            finish_reason=str(data.get("stop_reason")),
            latency_s=time.monotonic() - started,
        )


class OpenAIClient:
    supports_prefix = False  # no documented continuation parameter

    def __init__(self, http: httpx.AsyncClient, api_key: str, model: str, family: str) -> None:
        self.http, self.api_key, self.model, self.family = http, api_key, model, family

    def _url(self, prefix: bool) -> str:
        return "https://api.openai.com/v1/chat/completions"

    def _body(self, messages: list[Message], max_tokens: int, prefix: bool) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": messages,
            "max_completion_tokens": max_tokens,
            "reasoning_effort": "none",
        }

    def _cached(self, usage: dict[str, Any]) -> int:
        details = usage.get("prompt_tokens_details") or {}
        return int(details.get("cached_tokens") or 0)

    async def complete(
        self, messages: list[Message], max_tokens: int, prefix: bool = False
    ) -> Completion:
        if prefix and not self.supports_prefix:
            raise ProviderError(f"{self.family} does not support prefix continuation")
        headers = {"authorization": f"Bearer {self.api_key}", "content-type": "application/json"}
        started = time.monotonic()
        data = await _post(
            self.http, self._url(prefix), headers, self._body(messages, max_tokens, prefix)
        )
        choice = (data.get("choices") or [{}])[0]
        usage = data.get("usage") or {}
        return Completion(
            text=str((choice.get("message") or {}).get("content") or ""),
            input_tokens=int(usage.get("prompt_tokens", 0)),
            cached_input_tokens=self._cached(usage),
            output_tokens=int(usage.get("completion_tokens", 0)),
            finish_reason=str(choice.get("finish_reason")),
            latency_s=time.monotonic() - started,
        )


class DeepSeekClient(OpenAIClient):
    """DeepSeek's API is OpenAI-compatible, with its own continuation feature."""

    supports_prefix = True

    def _url(self, prefix: bool) -> str:
        # Prefix completion is a beta feature, served only from the beta base URL.
        base = "https://api.deepseek.com/beta" if prefix else "https://api.deepseek.com"
        return f"{base}/chat/completions"

    def _body(self, messages: list[Message], max_tokens: int, prefix: bool) -> dict[str, Any]:
        wire: list[dict[str, Any]] = [dict(message) for message in messages]
        if prefix:
            wire[-1]["prefix"] = True
        return {
            "model": self.model,
            "messages": wire,
            "max_tokens": max_tokens,
            "thinking": {"type": "disabled"},  # on by default; v1 has no reasoning models
        }

    def _cached(self, usage: dict[str, Any]) -> int:
        return int(usage.get("prompt_cache_hit_tokens") or 0)


PROVIDERS: dict[str, tuple[type[AnthropicClient] | type[OpenAIClient], str]] = {
    "anthropic": (AnthropicClient, "ANTHROPIC_API_KEY"),
    "openai": (OpenAIClient, "OPENAI_API_KEY"),
    "deepseek": (DeepSeekClient, "DEEPSEEK_API_KEY"),
}
