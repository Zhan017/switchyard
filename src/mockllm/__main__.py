"""Run mockllm behind a gate, so it can refuse connections for real:

    python -m mockllm --port 8001 --control-port 8101

Clients (the gateway) use the public port. Faults and stats use the control port, which
stays up while the public port refuses. ``uvicorn --factory mockllm.app:app_from_env``
still runs mockllm without a gate.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib

import uvicorn

from mockllm.app import create_mock_app
from mockllm.gate import Gate


class GatedMock:
    def __init__(self, host: str, port: int, control_host: str, control_port: int) -> None:
        self.app = create_mock_app()
        config = uvicorn.Config(
            self.app,
            host=control_host,
            port=control_port,
            log_level="warning",
            lifespan="off",
            access_log=False,
            # Keep idle connections longer than the gateway's pool does (15 s), as real
            # providers do. With uvicorn's 5 s default, a pooled connection could be closed
            # just as the gateway reuses it, and show up as a provider failure.
            timeout_keep_alive=60,
            # A hang fault holds requests for an hour; don't wait for them on shutdown.
            timeout_graceful_shutdown=1,
        )
        self.server = uvicorn.Server(config)
        self.host = host
        self.port = port
        self.control_host = control_host
        self.gate: Gate | None = None
        self.control_port = control_port

    async def start(self) -> None:
        self.task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            if self.task.done():
                self.task.result()  # raises the startup error
            await asyncio.sleep(0.01)
        self.control_port = self.server.servers[0].sockets[0].getsockname()[1]
        target = "127.0.0.1" if self.control_host in ("0.0.0.0", "") else self.control_host
        self.gate = Gate(self.host, self.port, target, self.control_port)
        self.app.state.mock.gate = self.gate
        await self.gate.open()
        self.port = self.gate.port

    async def stop(self) -> None:
        if self.gate is not None:
            await self.gate.close()
        self.server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await self.task

    async def run(self) -> None:
        await self.start()
        await self.task
        if self.gate is not None:
            await self.gate.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m mockllm", description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--control-host", default="0.0.0.0")
    parser.add_argument("--control-port", type=int, default=8101)
    args = parser.parse_args()
    mock = GatedMock(args.host, args.port, args.control_host, args.control_port)
    print(f"mockllm: clients on :{args.port}, faults and stats on :{args.control_port}")
    asyncio.run(mock.run())


if __name__ == "__main__":
    main()
