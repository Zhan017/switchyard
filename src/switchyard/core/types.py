"""Canonical types shared by the API, the router and the provider adapters.

Requests arrive in OpenAI's format and are converted to these types once. Adapters
translate them to each provider's wire format and translate responses back into the same
small set of stream events, so the rest of the gateway never sees provider differences.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]
FinishReason = Literal["stop", "length", "tool_calls", "content_filter"]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # a JSON string, as in OpenAI's format


@dataclass(frozen=True)
class Message:
    role: Role
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None  # set on role "tool"
    name: str | None = None
    # A trailing assistant message the model must continue (DeepSeek's prefix completion).
    prefix: bool = False


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str | None
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ResponseFormat:
    type: Literal["json_object", "json_schema"]
    schema: dict[str, Any] | None = None
    name: str | None = None
    strict: bool | None = None


# "auto", "none", "required", or the name of one tool the model must call.
ToolChoice = str


@dataclass(frozen=True)
class ChatRequest:
    messages: tuple[Message, ...]
    max_tokens: int
    temperature: float | None = None
    top_p: float | None = None
    stop: tuple[str, ...] = ()
    tools: tuple[ToolSpec, ...] = ()
    tool_choice: ToolChoice | None = None
    parallel_tool_calls: bool | None = None
    response_format: ResponseFormat | None = None
    user: str | None = None

    @property
    def wants_json(self) -> bool:
        return self.response_format is not None


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0  # all prompt tokens, including cached ones
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    estimated: bool = False

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            estimated=self.estimated or other.estimated,
        )


@dataclass(frozen=True)
class Completion:
    text: str
    tool_calls: tuple[ToolCall, ...]
    finish_reason: FinishReason
    usage: Usage
    refusal: str | None = None


# Stream events. Adapters yield only these; pings, keep-alives and other provider noise
# never leave the adapter.


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class RefusalDelta:
    text: str


@dataclass(frozen=True)
class ToolCallStart:
    index: int
    id: str
    name: str


@dataclass(frozen=True)
class ToolCallArgs:
    index: int
    delta: str


@dataclass(frozen=True)
class Finish:
    reason: FinishReason


@dataclass(frozen=True)
class UsageReport:
    usage: Usage


StreamEvent = TextDelta | RefusalDelta | ToolCallStart | ToolCallArgs | Finish | UsageReport
CONTENT_EVENTS = (TextDelta, RefusalDelta, ToolCallStart, ToolCallArgs)
