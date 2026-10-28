"""Parse an OpenAI chat completions request into the canonical ChatRequest."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from switchyard.core.types import ChatRequest, Message, ResponseFormat, ToolCall, ToolSpec


class InvalidRequest(Exception):
    def __init__(self, message: str, param: str | None = None) -> None:
        super().__init__(message)
        self.message, self.param = message, param


class _Lenient(BaseModel):
    # Unknown fields are ignored, as most OpenAI-compatible servers do. Fields Switchyard
    # can't honor are listed explicitly below and rejected.
    model_config = ConfigDict(extra="ignore")


class ContentPart(_Lenient):
    type: str
    text: str | None = None


class FunctionCallIn(_Lenient):
    name: str
    arguments: str = ""


class ToolCallIn(_Lenient):
    id: str
    type: Literal["function"] = "function"
    function: FunctionCallIn


class MessageIn(_Lenient):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[ContentPart] | None = None
    tool_calls: list[ToolCallIn] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class FunctionDef(_Lenient):
    name: str
    description: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolIn(_Lenient):
    type: Literal["function"]
    function: FunctionDef


class JsonSchemaIn(_Lenient):
    name: str | None = None
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")
    strict: bool | None = None


class ResponseFormatIn(_Lenient):
    type: Literal["text", "json_object", "json_schema"]
    json_schema: JsonSchemaIn | None = None


class StreamOptionsIn(_Lenient):
    include_usage: bool = False


class ChatCompletionIn(_Lenient):
    model: str
    messages: list[MessageIn] = Field(min_length=1)
    stream: bool = False
    stream_options: StreamOptionsIn | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    max_completion_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    stop: str | list[str] | None = None
    tools: list[ToolIn] | None = None
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    response_format: ResponseFormatIn | None = None
    user: str | None = None
    seed: int | None = None  # accepted and ignored: a failover changes the model anyway
    # Rejected below:
    n: int | None = None
    logprobs: bool | None = None
    top_logprobs: int | None = None
    audio: dict[str, Any] | None = None
    modalities: list[str] | None = None


def parse_body(raw: Any) -> ChatCompletionIn:
    try:
        body = ChatCompletionIn.model_validate(raw)
    except ValidationError as error:
        first = error.errors()[0]
        param = ".".join(str(part) for part in first["loc"])
        raise InvalidRequest(f"{param}: {first['msg']}", param) from error
    if body.n not in (None, 1):
        raise InvalidRequest("n greater than 1 is not supported", "n")
    if body.logprobs or body.top_logprobs:
        raise InvalidRequest("logprobs are not supported", "logprobs")
    if body.audio or (body.modalities and "audio" in body.modalities):
        raise InvalidRequest("audio is not supported", "audio")
    return body


def to_chat_request(body: ChatCompletionIn, default_max_tokens: int) -> ChatRequest:
    messages = tuple(_message(m, i) for i, m in enumerate(body.messages))
    stop = (body.stop,) if isinstance(body.stop, str) else tuple(body.stop or ())
    tools = tuple(
        ToolSpec(t.function.name, t.function.description, t.function.parameters)
        for t in body.tools or []
    )
    return ChatRequest(
        messages=messages,
        max_tokens=body.max_completion_tokens or body.max_tokens or default_max_tokens,
        temperature=body.temperature,
        top_p=body.top_p,
        stop=stop,
        tools=tools,
        tool_choice=_tool_choice(body.tool_choice),
        parallel_tool_calls=body.parallel_tool_calls,
        response_format=_response_format(body.response_format),
        user=body.user,
    )


def _message(message: MessageIn, index: int) -> Message:
    content: str | None
    if isinstance(message.content, list):
        parts = []
        for part in message.content:
            if part.type != "text":
                raise InvalidRequest(
                    f"only text content is supported, got '{part.type}'",
                    f"messages.{index}.content",
                )
            parts.append(part.text or "")
        content = "".join(parts)
    else:
        content = message.content
    role = "system" if message.role == "developer" else message.role
    if role == "tool" and not message.tool_call_id:
        raise InvalidRequest("tool messages need tool_call_id", f"messages.{index}")
    calls = tuple(
        ToolCall(c.id, c.function.name, c.function.arguments) for c in message.tool_calls or []
    )
    return Message(
        role=role,
        content=content,
        tool_calls=calls,
        tool_call_id=message.tool_call_id,
        name=message.name,
    )


def _tool_choice(choice: str | dict[str, Any] | None) -> str | None:
    if choice is None or isinstance(choice, str):
        if choice not in (None, "auto", "none", "required"):
            raise InvalidRequest(f"unknown tool_choice '{choice}'", "tool_choice")
        return choice
    function = choice.get("function")
    name = function.get("name") if isinstance(function, dict) else None
    if choice.get("type") != "function" or not isinstance(name, str):
        raise InvalidRequest("tool_choice must name a function", "tool_choice")
    return name


def _response_format(fmt: ResponseFormatIn | None) -> ResponseFormat | None:
    if fmt is None or fmt.type == "text":
        return None
    if fmt.type == "json_object":
        return ResponseFormat("json_object")
    if fmt.json_schema is None or fmt.json_schema.schema_ is None:
        raise InvalidRequest("json_schema needs a schema", "response_format.json_schema")
    return ResponseFormat(
        "json_schema", fmt.json_schema.schema_, fmt.json_schema.name, fmt.json_schema.strict
    )
