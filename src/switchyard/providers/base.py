"""The adapter interface and the HTTP plumbing every adapter shares."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aiohttp
import orjson

from switchyard.config import ModelConfig, ProviderConfig
from switchyard.core.types import ChatRequest, Completion, StreamEvent
from switchyard.providers.errors import (
    ErrorKind,
    ProviderError,
    error_from_exception,
    error_from_response,
)
from switchyard.providers.sse import ServerSentEvent, iter_sse


class Adapter(ABC):
    """Translates canonical requests to one provider's API and its replies back."""

    def __init__(
        self, provider_id: str, config: ProviderConfig, http: aiohttp.ClientSession
    ) -> None:
        self.provider_id = provider_id
        self.config = config
        self.http = http

    @abstractmethod
    def url(self) -> str: ...

    def url_for(self, request: ChatRequest) -> str:
        """The endpoint for this request. DeepSeek serves prefix completion elsewhere."""
        return self.url()

    @abstractmethod
    def body(
        self, model: str, options: ModelConfig, request: ChatRequest, *, stream: bool
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def complete(
        self, model: str, options: ModelConfig, request: ChatRequest
    ) -> Completion: ...

    @abstractmethod
    def stream(
        self, model: str, options: ModelConfig, request: ChatRequest
    ) -> AsyncIterator[StreamEvent]: ...

    def headers(self) -> dict[str, str]:
        return {"content-type": "application/json", **self.config.headers}

    async def post_json(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            async with self.http.post(
                url, data=orjson.dumps(body), headers=self.headers()
            ) as response:
                content = await response.read()
                if response.status >= 400:
                    raise error_from_response(response.status, content, response.headers)
        except ProviderError:
            raise
        except Exception as error:
            raise error_from_exception(error) from error
        try:
            data = orjson.loads(content)
        except orjson.JSONDecodeError as error:
            raise ProviderError(
                ErrorKind.INVALID_RESPONSE, "response body is not JSON", status=200
            ) from error
        if not isinstance(data, dict):
            raise ProviderError(ErrorKind.INVALID_RESPONSE, "response body is not an object")
        return data

    @asynccontextmanager
    async def open_stream(
        self, url: str, body: dict[str, Any]
    ) -> AsyncIterator[AsyncIterator[ServerSentEvent]]:
        """POST and yield the response as server-sent events. HTTP errors raise before the
        first event; transport errors mid-stream raise as ProviderError."""
        try:
            async with self.http.post(
                url, data=orjson.dumps(body), headers=self.headers()
            ) as response:
                if response.status >= 400:
                    content = await response.read()
                    raise error_from_response(response.status, content, response.headers)
                # Errors while the caller reads the stream come back through this context
                # manager, so the except clauses below classify them too.
                yield iter_sse(response.content.readany)
        except ProviderError:
            raise
        except Exception as error:
            raise error_from_exception(error) from error


def parse_json(data: bytes) -> dict[str, Any]:
    try:
        value = orjson.loads(data)
    except orjson.JSONDecodeError as error:
        raise ProviderError(
            ErrorKind.INVALID_RESPONSE, f"malformed chunk: {data[:200]!r}"
        ) from error
    if not isinstance(value, dict):
        raise ProviderError(ErrorKind.INVALID_RESPONSE, f"unexpected chunk: {data[:200]!r}")
    return value


def deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def ended_early() -> ProviderError:
    return ProviderError(ErrorKind.CONNECTION, "the stream ended before the answer finished")
