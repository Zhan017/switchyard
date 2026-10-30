"""Turn canonical stream events into OpenAI-format server-sent events for the client.

Rules from the spec (7.6) that apply from the first version on:

- The chunk ``id`` stays the same for the whole answer, and ``model`` shows the route
  alias, because the serving model can change mid-answer once failover exists.
- Tool calls are forwarded only once each call is complete, so a failover never has to
  take back half a call.
- JSON answers are buffered and sent only when complete and valid.
- When the provider dies after text went out, a continuer may finish the answer on
  another model (spec 7.6). The client sees a ``: switchyard failover`` comment, a
  ``: heartbeat`` comment every few seconds while the continuation starts, then more text
  in the same stream.
- Otherwise an error after the stream started is sent as one ``data: {"error": ...}``
  event and ``data: [DONE]``, which the official OpenAI SDKs raise as an error.
- The last chunk carries ``usage`` and a ``switchyard`` summary.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import orjson

from switchyard.core.types import (
    ChatRequest,
    Finish,
    FinishReason,
    RefusalDelta,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallArgs,
    ToolCallStart,
    Usage,
    UsageReport,
)
from switchyard.providers.errors import ErrorKind, ProviderError

log = logging.getLogger("switchyard.relay")
HEARTBEAT = b": heartbeat\n\n"


@dataclass
class Segment:
    """What one attempt produced: its output, and the usage it reported."""

    text: str
    usage: Usage | None


class Continuation(Protocol):
    """A continuation on another model, ready to forward."""

    head: list[StreamEvent]
    events: AsyncIterator[StreamEvent]

    @property
    def comment(self) -> str: ...


Resume = Callable[[ProviderError, str, Segment], Awaitable[Continuation | None]]


@dataclass
class StreamResult:
    """What the stream produced, for accounting once it ends."""

    text: str = ""
    refusal: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: FinishReason | None = None
    usage: Usage | None = None
    error: ProviderError | None = None
    # The provider failed after at least one complete tool call was sent, so the answer
    # ended as a tool call instead of an error (spec 7.6).
    recovered_from: ProviderError | None = None
    aborted: bool = False  # the client went away, or the gateway itself failed
    started_at: float | None = None  # time.monotonic() when the first chunk went out
    first_byte_at: float | None = None  # time.monotonic() when content first went out
    segment_text: str = ""  # output of the last attempt, for its usage
    midstream: int = 0  # continuations forwarded after a mid-stream failure


def sse(data: dict[str, Any]) -> bytes:
    return b"data: " + orjson.dumps(data) + b"\n\n"


DONE = b"data: [DONE]\n\n"


def usage_json(usage: Usage) -> dict[str, Any]:
    return {
        "prompt_tokens": usage.input_tokens,
        "completion_tokens": usage.output_tokens,
        "total_tokens": usage.input_tokens + usage.output_tokens,
        "prompt_tokens_details": {"cached_tokens": usage.cached_input_tokens},
    }


def error_json(error: ProviderError) -> dict[str, Any]:
    code, _ = error_code(error)
    return {"error": {"message": error.message, "type": "switchyard_error", "code": code}}


def error_code(error: ProviderError) -> tuple[str, int]:
    """The client-facing error code and HTTP status for a provider failure."""
    if error.kind == ErrorKind.CLIENT_ERROR:
        return "upstream_rejected", 400
    if error.kind == ErrorKind.DEADLINE:
        return "deadline_exceeded", 504
    if error.kind == ErrorKind.AUTH:
        return "upstream_auth_failed", 502
    return "no_healthy_provider", 503


def check_json(text: str, finish_reason: str | None, refused: bool) -> None:
    """Raise if a finished JSON-mode answer isn't valid JSON. A refusal or a cut-off answer
    (finish_reason "length") is passed through as it is, not treated as a failure."""
    if refused or finish_reason != "stop":
        return
    try:
        orjson.loads(text)
    except orjson.JSONDecodeError as error:
        raise ProviderError(
            ErrorKind.INVALID_RESPONSE, "the model returned invalid JSON in JSON mode"
        ) from error


class Relay:
    def __init__(
        self,
        *,
        response_id: str,
        alias: str,
        request: ChatRequest,
        finalize: Callable[[StreamResult], dict[str, Any]],
        resume: Resume | None = None,
        heartbeat_s: float = 3.0,
    ) -> None:
        """``finalize`` runs once the provider's stream ends, with or without an error. It
        does the accounting and returns the ``switchyard`` summary for the last chunk.
        ``resume`` is called when the provider dies after text went out, with the error,
        everything the client has received and what the failed attempt produced; it
        returns a continuation to forward, or None to end with the error event."""
        self.response_id = response_id
        self.alias = alias
        self.request = request
        self.finalize = finalize
        self.resume = resume
        self.heartbeat_s = heartbeat_s
        self._current: AsyncIterator[StreamEvent] | None = None
        self.created = int(time.time())
        self.result = StreamResult()
        self._summary: dict[str, Any] | None = None
        # Every chunk shares this prefix, so it is built once instead of once per token.
        self._prefix = (
            b'data: {"id":'
            + orjson.dumps(response_id)
            + b',"object":"chat.completion.chunk","created":'
            + str(self.created).encode()
            + b',"model":'
            + orjson.dumps(alias)
            + b',"choices":[{"index":0,"delta":'
        )

    def chunk(self, delta: dict[str, Any], finish_reason: str | None = None) -> bytes:
        finish = b"null" if finish_reason is None else orjson.dumps(finish_reason)
        return self._prefix + orjson.dumps(delta) + b',"finish_reason":' + finish + b"}]}\n\n"

    def _sent(self) -> None:
        if self.result.first_byte_at is None:
            self.result.first_byte_at = time.monotonic()

    def _finalize_once(self) -> dict[str, Any]:
        if self._summary is None:
            self._summary = self.finalize(self.result)
        return self._summary

    async def close(self, events: AsyncIterator[StreamEvent]) -> None:
        """Make sure accounting ran and the upstream stream is closed, even if the response
        never started (for example, the client left before the first byte)."""
        if self._summary is None:
            self.result.aborted = True
            self._finalize_once()
        for stream in {id(events): events, id(self._current): self._current}.values():
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                await aclose()

    async def _resume(
        self, error: ProviderError, emitted: str, failed: Segment
    ) -> Continuation | None:
        assert self.resume is not None
        try:
            return await self.resume(error, emitted, failed)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A bug in failover must not hang or crash the stream: end it with the error.
            log.exception("mid-stream failover failed")
            return None

    async def run(
        self, events: AsyncIterator[StreamEvent], head: Sequence[StreamEvent] = ()
    ) -> AsyncIterator[bytes]:
        """Relay ``head`` (events already read from the stream), then the rest of ``events``."""
        result = self.result
        pending = list(reversed(head))
        iterator = aiter(events)
        buffer_text = self.request.wants_json
        text: list[str] = []
        calls: dict[int, list[str]] = {}  # index -> [id, name, arguments]
        flushed: set[int] = set()
        completed = False

        def flush_calls(before: int | None = None) -> bytes | None:
            ready = sorted(i for i in calls if i not in flushed and (before is None or i < before))
            if not ready:
                return None
            out = []
            for index in ready:
                call_id, name, arguments = calls[index]
                arguments = (
                    arguments or "{}"
                )  # Anthropic sends nothing for a call without arguments
                flushed.add(index)
                result.tool_calls.append(ToolCall(call_id, name, arguments))
                out.append(
                    {
                        "index": index,
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": arguments},
                    }
                )
            return self.chunk({"tool_calls": out})

        segment: list[str] = []  # output of the current attempt, for its usage

        def can_continue() -> bool:
            partial_call = any(i not in flushed for i in calls)
            return (
                self.resume is not None
                and not buffer_text
                and not result.refusal
                and not partial_call
                and bool("".join(text))
            )

        try:
            result.started_at = time.monotonic()
            yield self.chunk({"role": "assistant", "content": ""})
            while True:  # one pass per attempt; a mid-stream failover starts another
                try:
                    while True:
                        if pending:
                            event = pending.pop()
                        else:
                            try:
                                event = await anext(iterator)
                            except StopAsyncIteration:
                                break
                        if isinstance(event, TextDelta):
                            text.append(event.text)
                            segment.append(event.text)
                            if not buffer_text:
                                self._sent()
                                yield self.chunk({"content": event.text})
                        elif isinstance(event, RefusalDelta):
                            result.refusal += event.text
                            segment.append(event.text)
                            self._sent()
                            yield self.chunk({"refusal": event.text})
                        elif isinstance(event, ToolCallStart):
                            # A new call starting means every earlier call is complete.
                            ready = flush_calls(before=event.index)
                            if ready:
                                self._sent()
                                yield ready
                            calls[event.index] = [event.id, event.name, ""]
                            segment.append(event.name)
                        elif isinstance(event, ToolCallArgs):
                            if event.index in calls:
                                calls[event.index][2] += event.delta
                            segment.append(event.delta)
                        elif isinstance(event, Finish):
                            result.finish_reason = event.reason
                        elif isinstance(event, UsageReport):
                            result.usage = event.usage
                    break
                except ProviderError as error:
                    continuation: Continuation | None = None
                    if not result.tool_calls and can_continue():
                        yield (
                            f": switchyard failover: {error.kind.value} after "
                            f"{len(''.join(text))} characters\n\n"
                        ).encode()
                        emitted = "".join(text)
                        failed = Segment("".join(segment), result.usage)
                        task = asyncio.ensure_future(self._resume(error, emitted, failed))
                        try:
                            while not task.done():
                                await asyncio.wait({task}, timeout=self.heartbeat_s)
                                if not task.done():
                                    yield HEARTBEAT
                            continuation = task.result()
                        finally:
                            if not task.done():
                                task.cancel()
                                with contextlib.suppress(asyncio.CancelledError):
                                    await task
                    if continuation is None:
                        raise
                    yield f": switchyard {continuation.comment}\n\n".encode()
                    result.midstream += 1
                    result.usage = None
                    segment = []
                    pending = list(reversed(continuation.head))
                    iterator = aiter(continuation.events)
                    self._current = continuation.events
            result.text = "".join(text)
            result.segment_text = "".join(segment)
            if buffer_text:
                check_json(result.text, result.finish_reason, bool(result.refusal))
                if result.text:
                    self._sent()
                    yield self.chunk({"content": result.text})
            ready = flush_calls()
            if ready:
                self._sent()
                yield ready
            yield self.chunk({}, result.finish_reason or "stop")
            completed = True
        except ProviderError as error:
            result.text = result.text or "".join(text)
            result.segment_text = "".join(segment)
            if result.tool_calls:
                # Complete tool calls already went out: end as a tool-call turn, dropping
                # any call still in progress. The client runs the tools and calls back.
                result.recovered_from = error
                result.finish_reason = "tool_calls"
            else:
                result.error = error
            completed = True
        finally:
            # Runs on success, on a provider error, and when the client disconnects.
            if not completed:
                result.text = result.text or "".join(text)
                result.segment_text = result.segment_text or "".join(segment)
                result.aborted = True
            summary = self._finalize_once()
        if result.error is not None:
            # The summary goes with the error too, so a client can see what was tried.
            yield sse({**error_json(result.error), "switchyard": summary})
            yield DONE
            return
        if result.recovered_from is not None:
            yield self.chunk({}, "tool_calls")
        yield sse(
            {
                "id": self.response_id,
                "object": "chat.completion.chunk",
                "created": self.created,
                "model": self.alias,
                "choices": [],
                "usage": usage_json(result.usage or Usage()),
                "switchyard": summary,
            }
        )
        yield DONE
