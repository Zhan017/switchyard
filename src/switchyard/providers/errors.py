"""Provider failures, classified once so retries, breakers and the API agree on them."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from enum import StrEnum

import aiohttp

MAX_RETRY_AFTER_S = 3600.0


class ErrorKind(StrEnum):
    CONNECTION = "connection"  # refused, reset, or the stream ended before the answer did
    TIMEOUT = "timeout"  # no first token within the budget
    STALL = "stall"  # tokens stopped arriving mid-answer
    DEADLINE = "deadline"  # the whole request ran out of time
    SERVER_ERROR = "server_error"  # 5xx, 529, or an error event inside a 200 stream
    RATE_LIMITED = "rate_limited"  # 429: a quota signal, not a health signal
    CLIENT_ERROR = "client_error"  # the provider says the request itself is invalid
    AUTH = "auth"  # 401 or 403: a bad or unauthorized key
    INVALID_RESPONSE = "invalid_response"  # malformed chunk, or invalid JSON in JSON mode


# Kinds worth trying again on the same provider. Rate limits move to another provider
# instead, and client errors would fail anywhere.
RETRYABLE = frozenset(
    {
        ErrorKind.CONNECTION,
        ErrorKind.TIMEOUT,
        ErrorKind.STALL,
        ErrorKind.SERVER_ERROR,
        ErrorKind.INVALID_RESPONSE,
    }
)


class ProviderError(Exception):
    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE

    def __repr__(self) -> str:
        return f"ProviderError({self.kind.value}, {self.message!r}, status={self.status})"


def kind_for_status(status: int) -> ErrorKind:
    if status == 429:
        return ErrorKind.RATE_LIMITED
    if status in (401, 403):
        return ErrorKind.AUTH
    if status == 408:
        return ErrorKind.TIMEOUT
    if status >= 500:  # includes Anthropic's 529 overloaded
        return ErrorKind.SERVER_ERROR
    return ErrorKind.CLIENT_ERROR


def error_from_response(status: int, body: bytes, headers: Mapping[str, str]) -> ProviderError:
    return ProviderError(
        kind_for_status(status),
        error_message(body) or f"HTTP {status}",
        status=status,
        retry_after=parse_retry_after(headers.get("retry-after")),
    )


def error_message(body: bytes) -> str:
    text = body.decode(errors="replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        return text[:500]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return str(error["message"])[:500]
        if isinstance(error, str):
            return error[:500]
    return text[:500]


def parse_retry_after(value: str | None) -> float | None:
    """Seconds from a Retry-After header. HTTP-date values are ignored."""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    if not math.isfinite(seconds):
        return None
    return min(max(0.0, seconds), MAX_RETRY_AFTER_S)


def error_from_exception(error: Exception) -> ProviderError:
    if isinstance(error, ProviderError):
        return error
    message = f"{type(error).__name__}: {error}"
    if isinstance(error, TimeoutError):  # includes aiohttp's connect and read timeouts
        return ProviderError(ErrorKind.TIMEOUT, message)
    if isinstance(error, (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, OSError)):
        # Refused, reset, or a chunked body cut short.
        return ProviderError(ErrorKind.CONNECTION, message)
    return ProviderError(ErrorKind.INVALID_RESPONSE, message)
