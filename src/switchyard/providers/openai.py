"""OpenAI's chat completions API, and any server compatible with it (mockllm included)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from switchyard.config import ModelConfig
from switchyard.core.types import (
    ChatRequest,
    Completion,
    Finish,
    FinishReason,
    Message,
    RefusalDelta,
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

FINISH_REASONS: dict[str, FinishReason] = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "content_filter": "content_filter",
}


class OpenAIAdapter(Adapter):
    max_tokens_field = "max_completion_tokens"

    def url(self) -> str:
        return f"{self.config.base_url.rstrip('/')}/chat/completions"

    def headers(self) -> dict[str, str]:
        headers = super().headers()
        key = self.config.api_key()
        if key:
            headers["authorization"] = f"Bearer {key}"
        return headers

    def body(
        self, model: str, options: ModelConfig, request: ChatRequest, *, stream: bool
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": [self.message(m) for m in request.messages],
            self.max_tokens_field: request.max_tokens,
            "stream": stream,
        }
        if stream:
            body["stream_options"] = {"include_usage": True}
        if options.sampling_params:
            if request.temperature is not None:
                body["temperature"] = request.temperature
            if request.top_p is not None:
                body["top_p"] = request.top_p
        if request.stop:
            body["stop"] = list(request.stop)
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        **({"description": tool.description} if tool.description else {}),
                        "parameters": tool.parameters,
                    },
                }
                for tool in request.tools
            ]
        if request.tool_choice is not None:
            body["tool_choice"] = (
                request.tool_choice
                if request.tool_choice in ("auto", "none", "required")
                else {"type": "function", "function": {"name": request.tool_choice}}
            )
        if request.parallel_tool_calls is not None:
            body["parallel_tool_calls"] = request.parallel_tool_calls
        fmt = request.response_format
        if fmt is not None:
            if fmt.type == "json_object":
                body["response_format"] = {"type": "json_object"}
            else:
                schema: dict[str, Any] = {"name": fmt.name or "response", "schema": fmt.schema}
                if fmt.strict is not None:
                    schema["strict"] = fmt.strict
                body["response_format"] = {"type": "json_schema", "json_schema": schema}
        if request.user:
            body["user"] = request.user
        return deep_merge(body, options.request_overrides)

    @staticmethod
    def message(message: Message) -> dict[str, Any]:
        out: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_calls:
            out["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in message.tool_calls
            ]
        if message.tool_call_id:
            out["tool_call_id"] = message.tool_call_id
        if message.name and message.role != "tool":
            out["name"] = message.name
        return out

    def usage(self, raw: dict[str, Any]) -> Usage:
        # prompt_tokens includes cached and cache-written tokens; each is billed at its
        # own rate (GPT-5.6 and later charge for cache writes).
        details = raw.get("prompt_tokens_details") or {}
        return Usage(
            input_tokens=int(raw.get("prompt_tokens") or 0),
            output_tokens=int(raw.get("completion_tokens") or 0),
            cached_input_tokens=int(details.get("cached_tokens") or 0),
            cache_write_tokens=int(details.get("cache_write_tokens") or 0),
        )

    async def complete(self, model: str, options: ModelConfig, request: ChatRequest) -> Completion:
        data = await self.post_json(self.url(), self.body(model, options, request, stream=False))
        if "error" in data and not data.get("choices"):
            raise chunk_error(data["error"])
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(ErrorKind.INVALID_RESPONSE, "response has no choices")
        choice = choices[0]
        message = choice.get("message") or {}
        calls = tuple(
            ToolCall(
                id=str(call.get("id") or ""),
                name=str((call.get("function") or {}).get("name") or ""),
                arguments=str((call.get("function") or {}).get("arguments") or ""),
            )
            for call in message.get("tool_calls") or []
        )
        return Completion(
            text=str(message.get("content") or ""),
            tool_calls=calls,
            finish_reason=FINISH_REASONS.get(str(choice.get("finish_reason")), "stop"),
            usage=self.usage(data.get("usage") or {}),
            refusal=message.get("refusal"),
        )

    async def stream(
        self, model: str, options: ModelConfig, request: ChatRequest
    ) -> AsyncIterator[StreamEvent]:
        body = self.body(model, options, request, stream=True)
        finished = False
        started_calls: set[int] = set()
        async with self.open_stream(self.url(), body) as events:
            async for sse in events:
                if sse.data.strip() == b"[DONE]":
                    # Keep reading to the end of the response (normally nothing is left)
                    # so httpx can reuse the connection. Stopping here would close it.
                    continue
                chunk = parse_json(sse.data)
                if chunk.get("error"):
                    raise chunk_error(chunk["error"])
                for choice in (chunk.get("choices") or [])[:1]:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        yield TextDelta(str(delta["content"]))
                    if delta.get("refusal"):
                        yield RefusalDelta(str(delta["refusal"]))
                    for call in delta.get("tool_calls") or []:
                        index = int(call.get("index", 0))
                        function = call.get("function") or {}
                        if index not in started_calls:
                            started_calls.add(index)
                            yield ToolCallStart(
                                index, str(call.get("id") or ""), str(function.get("name") or "")
                            )
                        if function.get("arguments"):
                            yield ToolCallArgs(index, str(function["arguments"]))
                    reason = choice.get("finish_reason")
                    if reason:
                        finished = True
                        yield Finish(FINISH_REASONS.get(str(reason), "stop"))
                if chunk.get("usage"):
                    yield UsageReport(self.usage(chunk["usage"]))
        if not finished:
            raise ended_early()


def chunk_error(error: Any) -> ProviderError:
    """An error object inside a 200 response or stream."""
    if not isinstance(error, dict):
        return ProviderError(ErrorKind.SERVER_ERROR, str(error)[:500])
    message = str(error.get("message") or "error in stream")
    code = str(error.get("code") or "") + " " + str(error.get("type") or "")
    if "rate_limit" in code:
        kind = ErrorKind.RATE_LIMITED
    elif "invalid_request" in code:
        kind = ErrorKind.CLIENT_ERROR
    else:
        kind = ErrorKind.SERVER_ERROR
    return ProviderError(kind, message)


class DeepSeekAdapter(OpenAIAdapter):
    """DeepSeek's API is OpenAI-compatible with a few differences."""

    max_tokens_field = "max_tokens"

    def usage(self, raw: dict[str, Any]) -> Usage:
        return Usage(
            input_tokens=int(raw.get("prompt_tokens") or 0),
            output_tokens=int(raw.get("completion_tokens") or 0),
            cached_input_tokens=int(raw.get("prompt_cache_hit_tokens") or 0),
        )
