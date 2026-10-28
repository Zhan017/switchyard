"""A small server-sent events parser that works on raw bytes.

It splits the byte stream into events itself instead of decoding text line by line, since
this runs once per streamed token and is on the gateway's hot path.

Comment lines (starting with ":") are dropped here. That covers DeepSeek's
``: keep-alive`` lines and Switchyard's own heartbeats, so they can never count as
progress.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ServerSentEvent:
    event: str | None
    data: bytes


def parse_block(block: bytes) -> ServerSentEvent | None:
    event: str | None = None
    data: list[bytes] = []
    for line in block.split(b"\n"):
        if not line or line[:1] == b":":
            continue
        field, _, value = line.partition(b":")
        if value[:1] == b" ":
            value = value[1:]
        if field == b"data":
            data.append(value)
        elif field == b"event":
            event = value.decode()
    if not data:
        return None
    return ServerSentEvent(event, data[0] if len(data) == 1 else b"\n".join(data))


async def iter_sse(read: Callable[[], Awaitable[bytes]]) -> AsyncIterator[ServerSentEvent]:
    """Events from ``read``, which returns whatever bytes have arrived, or b"" at the end."""
    buffer = b""
    while chunk := await read():
        buffer += chunk
        if b"\r" in buffer:
            buffer = buffer.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        while (end := buffer.find(b"\n\n")) >= 0:
            event = parse_block(buffer[:end])
            buffer = buffer[end + 2 :]
            if event is not None:
                yield event
    if buffer.strip():
        event = parse_block(buffer.strip(b"\n"))
        if event is not None:
            yield event
