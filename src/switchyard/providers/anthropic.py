"""Anthropic's Messages API."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from switchyard.config import ModelConfig
from switchyard.core.types import (
    ChatRequest,
    Completion,
    Finish,
    FinishReason,
    Message,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallArgs,
    ToolCallStart,
    Usage,
    UsageReport,
)
from switchyard.providers.base import Adapter, deep_merge, ended_early, parse_json
from switchyard.providers.errors import ErrorKind, ProviderError

API_VERSION = "2023-06-01"

STOP_REASONS: dict[str, FinishReason] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "pause_turn": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}

ERROR_KINDS: dict[str, ErrorKind] = {
    "overloaded_error": ErrorKind.SERVER_ERROR,
    "api_error": ErrorKind.SERVER_ERROR,
    "timeout_error": ErrorKind.TIMEOUT,
    "rate_limit_error": ErrorKind.RATE_LIMITED,
    "authentication_error": ErrorKind.AUTH,
    "permission_error": ErrorKind.AUTH,
    "invalid_request_error": ErrorKind.CLIENT_ERROR,
    "not_found_error": ErrorKind.CLIENT_ERROR,
    "request_too_large": ErrorKind.CLIENT_ERROR,
}


class AnthropicAdapter(Adapter):
    def url(self) -> str:
        return f"{self.config.base_url.rstrip('/')}/v1/messages"

    def headers(self) -> dict[str, str]:
        headers = super().headers()
        headers["anthropic-version"] = API_VERSION
        key = self.config.api_key()
        if key:
            headers["x-api-key"] = key
        return headers

    def body(
        self, model: str, options: ModelConfig, request: ChatRequest, *, stream: bool
    ) -> dict[str, Any]:
        system = [m.content for m in request.messages if m.role == "system" and m.content]
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": request.max_tokens,
            "messages": convert_messages(request.messages),
            "stream": stream,
        }
        if system:
            body["system"] = "\n\n".join(system)
        if options.sampling_params:
            if request.temperature is not None:
                body["temperature"] = request.temperature
            if request.top_p is not None:
                body["top_p"] = request.top_p
        if request.stop:
            body["stop_sequences"] = list(request.stop)
        if request.tools:
            body["tools"] = [
                {
                    "name": tool.name,
                    **({"description": tool.description} if tool.description else {}),
                    "input_schema": tool.parameters or {"type": "object", "properties": {}},
                }
                for tool in request.tools
            ]
        choice = tool_choice(request.tool_choice, request.parallel_tool_calls)
        if choice is not None and (request.tools or choice["type"] == "none"):
            body["tool_choice"] = choice
        if not request.tools:
            # Anthropic rejects tool_use and tool_result blocks unless tools are defined,
            # while OpenAI accepts tool history without tools. Declare the tools the history
            # used, and forbid new calls, since the client didn't offer any.
            used = sorted({c.name for m in request.messages for c in m.tool_calls})
            if used:
                body["tools"] = [
                    {"name": name, "input_schema": {"type": "object"}} for name in used
                ]
                body["tool_choice"] = {"type": "none"}
        fmt = request.response_format
        if fmt is not None:
            if fmt.type != "json_schema" or fmt.schema is None:
                raise ProviderError(
                    ErrorKind.CLIENT_ERROR, "Anthropic supports JSON output only with a schema"
                )
            body["output_config"] = {"format": {"type": "json_schema", "schema": fmt.schema}}
        if request.user:
            body["metadata"] = {"user_id": request.user}
        return deep_merge(body, options.request_overrides)

    async def complete(self, model: str, options: ModelConfig, request: ChatRequest) -> Completion:
        data = await self.post_json(
            self.url_for(request), self.body(model, options, request, stream=False)
        )
        if data.get("type") == "error":
            raise event_error(data)
        text: list[str] = []
        calls: list[ToolCall] = []
        for block in data.get("content") or []:
            if block.get("type") == "text":
                text.append(str(block.get("text") or ""))
            elif block.get("type") == "tool_use":
                calls.append(
                    ToolCall(
                        id=str(block.get("id") or ""),
                        name=str(block.get("name") or ""),
                        arguments=json.dumps(block.get("input") or {}),
                    )
                )
        return Completion(
            text="".join(text),
            tool_calls=tuple(calls),
            finish_reason=STOP_REASONS.get(str(data.get("stop_reason")), "stop"),
            usage=usage(data.get("usage") or {}),
        )

    async def stream(
        self, model: str, options: ModelConfig, request: ChatRequest
    ) -> AsyncIterator[StreamEvent]:
        body = self.body(model, options, request, stream=True)
        tool_index: dict[int, int] = {}  # Anthropic block index -> OpenAI tool call index
        totals: dict[str, Any] = {}
        finished = False
        async with self.open_stream(self.url_for(request), body) as events:
            async for sse in events:
                event = parse_json(sse.data)
                kind = event.get("type") or sse.event
                if kind == "message_start":
                    _merge_usage(totals, (event.get("message") or {}).get("usage"))
                elif kind == "content_block_start":
                    block = event.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        index = len(tool_index)
                        tool_index[int(event.get("index", 0))] = index
                        yield ToolCallStart(
                            index, str(block.get("id") or ""), str(block.get("name") or "")
                        )
                    elif block.get("type") == "text" and block.get("text"):
                        yield TextDelta(str(block["text"]))
                elif kind == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        yield TextDelta(str(delta["text"]))
                    elif delta.get("type") == "input_json_delta" and delta.get("partial_json"):
                        block_index = int(event.get("index", 0))
                        if block_index in tool_index:
                            yield ToolCallArgs(tool_index[block_index], str(delta["partial_json"]))
                    # thinking and signature deltas are dropped: v1 has no reasoning models
                elif kind == "message_delta":
                    _merge_usage(totals, event.get("usage"))  # cumulative, so later wins
                    reason = (event.get("delta") or {}).get("stop_reason")
                    if reason and not finished:
                        finished = True
                        yield Finish(STOP_REASONS.get(str(reason), "stop"))
                elif kind == "message_stop":
                    continue  # read to the end so the connection can be reused
                elif kind == "error":
                    raise event_error(event)
                # ping and unknown event types are ignored, as Anthropic's docs ask
        if totals:
            yield UsageReport(usage(totals, complete=finished))
        if not finished:
            raise ended_early()


def convert_messages(messages: tuple[Message, ...]) -> list[dict[str, Any]]:
    """OpenAI messages to Anthropic's: tool calls become tool_use blocks, tool results
    become tool_result blocks in a user turn, and consecutive turns of one role merge."""
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            continue
        if message.role == "tool":
            role = "user"
            blocks: list[dict[str, Any]] = [
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id or "",
                    "content": message.content or "",
                }
            ]
        else:
            role = message.role
            blocks = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            for call in message.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": _arguments(call.arguments),
                    }
                )
        if not blocks:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})
    return out


def _arguments(arguments: str) -> Any:
    if not arguments.strip():
        return {}
    try:
        return json.loads(arguments)
    except ValueError as error:
        raise ProviderError(
            ErrorKind.CLIENT_ERROR, "tool call arguments are not valid JSON"
        ) from error


def tool_choice(choice: str | None, parallel: bool | None) -> dict[str, Any] | None:
    if choice is None and parallel is not False:
        return None
    if choice in (None, "auto"):
        out: dict[str, Any] = {"type": "auto"}
    elif choice == "required":
        out = {"type": "any"}
    elif choice == "none":
        return {"type": "none"}
    else:
        out = {"type": "tool", "name": choice}
    if parallel is False:
        out["disable_parallel_tool_use"] = True
    return out


def _merge_usage(totals: dict[str, Any], update: Any) -> None:
    """Merge usage fields, skipping nulls so they can't erase counts already known."""
    if isinstance(update, dict):
        totals.update({k: v for k, v in update.items() if v is not None})


def usage(raw: dict[str, Any], complete: bool = True) -> Usage:
    cached = int(raw.get("cache_read_input_tokens") or 0)
    written = int(raw.get("cache_creation_input_tokens") or 0)
    return Usage(
        input_tokens=int(raw.get("input_tokens") or 0) + cached + written,
        # Without a final message_delta, output_tokens is only the count at message_start,
        # so it is dropped and estimated from the text instead.
        output_tokens=int(raw.get("output_tokens") or 0) if complete else 0,
        cached_input_tokens=cached,
        cache_write_tokens=written,
        estimated=not complete,
    )


def event_error(event: dict[str, Any]) -> ProviderError:
    error = event.get("error") or {}
    kind = ERROR_KINDS.get(str(error.get("type")), ErrorKind.SERVER_ERROR)
    return ProviderError(kind, str(error.get("message") or "error event in stream"))
