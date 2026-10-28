"""A TCP front door for mockllm that can refuse connections for real.

An HTTP app can't refuse a TCP connection: by the time it runs, the connection exists.
So in gate mode mockllm serves its app on a control port and puts this gate on the
public port. The gate forwards bytes both ways, and on ``refuse_connections`` it closes
its listening socket, so new connections get ECONNREFUSED, and resets every open
connection, so pooled keep-alive connections fail too. The control port stays up, so
faults can still be switched while the public port refuses.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import struct

log = logging.getLogger("mockllm.gate")


class Gate:
    def __init__(self, host: str, port: int, target_host: str, target_port: int) -> None:
        self.host = host
        self.port = port
        self.target = (target_host, target_port)
        self.server: asyncio.Server | None = None
        self.connections: set[asyncio.StreamWriter] = set()
        self._reopen: asyncio.TimerHandle | None = None
        self.refusing_until: float | None = None

    async def open(self) -> None:
        if self.server is not None:
            return
        self.server = await asyncio.start_server(
            self._handle, self.host, self.port, reuse_address=True
        )
        if self.port == 0:
            self.port = self.server.sockets[0].getsockname()[1]
        self.refusing_until = None
        log.info("gate open on %s:%d", self.host, self.port)

    def refuse(self, seconds: float | None) -> None:
        """Refuse connections now, and accept them again after ``seconds`` (or never)."""
        loop = asyncio.get_running_loop()
        if self._reopen is not None:
            self._reopen.cancel()
            self._reopen = None
        if self.server is not None:
            self.server.close()
            self.server = None
        for writer in list(self.connections):
            _reset(writer)
        self.connections.clear()
        if seconds is not None:
            self.refusing_until = loop.time() + seconds
            self._reopen = loop.call_later(seconds, lambda: loop.create_task(self.open()))
        else:
            self.refusing_until = float("inf")
        log.info("gate refusing connections for %s s", seconds)

    async def accept(self) -> None:
        if self._reopen is not None:
            self._reopen.cancel()
            self._reopen = None
        await self.open()

    @property
    def refusing(self) -> bool:
        return self.server is None

    async def close(self) -> None:
        if self._reopen is not None:
            self._reopen.cancel()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for writer in list(self.connections):
            _reset(writer)

    async def _handle(
        self, client_reader: asyncio.StreamReader, client: asyncio.StreamWriter
    ) -> None:
        self.connections.add(client)
        upstream: asyncio.StreamWriter | None = None
        try:
            upstream_reader, upstream = await asyncio.open_connection(*self.target)
            self.connections.add(upstream)
            await asyncio.gather(_pipe(client_reader, upstream), _pipe(upstream_reader, client))
        except OSError:
            pass
        finally:
            for writer in (client, upstream):
                if writer is not None:
                    self.connections.discard(writer)
                    with contextlib.suppress(OSError, RuntimeError):
                        writer.close()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (OSError, RuntimeError):
        pass
    finally:
        with contextlib.suppress(OSError, RuntimeError):
            if writer.can_write_eof():
                writer.write_eof()


def _reset(writer: asyncio.StreamWriter) -> None:
    """Close with a TCP reset rather than a clean FIN, as a dying server would."""
    sock = writer.get_extra_info("socket")
    if sock is not None:
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    writer.transport.abort()
