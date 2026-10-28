"""Short outage scenarios end to end: the chaos runner, two gated mockllm servers, and a
real gateway server. The full-length runs are in docs/results/scenarios."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from chaos.cli import main, run_scenario
from chaos.runner import Target
from chaos.scenario import Scenario
from mockllm.__main__ import GatedMock
from switchyard.api.app import create_app

pytestmark = pytest.mark.anyio
CONFIG = Path(__file__).parent.parent / "fixtures" / "chaos.yaml"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module")
def stack() -> Iterator[Target]:
    loop = asyncio.new_event_loop()
    mocks = [GatedMock("127.0.0.1", 0, "127.0.0.1", 0) for _ in range(2)]
    ready = threading.Event()

    def serve_mocks() -> None:
        asyncio.set_event_loop(loop)
        for mock in mocks:
            loop.run_until_complete(mock.start())
        ready.set()
        loop.run_forever()

    threading.Thread(target=serve_mocks, daemon=True).start()
    assert ready.wait(10)
    saved = {k: os.environ.get(k) for k in ("MOCKLLM_A_URL", "MOCKLLM_B_URL")}
    os.environ["MOCKLLM_A_URL"] = f"http://127.0.0.1:{mocks[0].port}"
    os.environ["MOCKLLM_B_URL"] = f"http://127.0.0.1:{mocks[1].port}"
    config = uvicorn.Config(create_app(CONFIG), host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "gateway did not start"
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield Target(
        gateway=f"http://127.0.0.1:{port}",
        upstreams={
            "primary": f"http://127.0.0.1:{mocks[0].control_port}",
            "secondary": f"http://127.0.0.1:{mocks[1].control_port}",
        },
        key="sy-dev-local-key",
        admin_key="sy-admin-local-key",
    )
    server.should_exit = True
    for mock in mocks:
        asyncio.run_coroutine_threadsafe(mock.stop(), loop).result(10)
    loop.call_soon_threadsafe(loop.stop)
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def scenario(**overrides: Any) -> Scenario:
    data: dict[str, Any] = {
        "id": "T",
        "title": "test",
        "release": "test",
        "description": "test",
        "topology": {"primary": "mock-a", "secondary": "mock-b"},
        "traffic": {"model": "mock", "rate_rps": 10, "duration_s": 12},
        "seeds": [1],
        "faults": [],
        "checks": [{"kind": "success_rate", "description": "ok", "min": 0.9}],
    }
    data.update(overrides)
    return Scenario.model_validate(data)


def by_kind(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {check["kind"]: check for check in summary["checks"]}


async def test_primary_outage_fails_over_and_recovers(stack: Target, tmp_path: Path) -> None:
    s4 = scenario(
        faults=[
            {"upstream": "primary", "mode": "refuse_connections", "start_s": 2, "duration_s": 4}
        ],
        checks=[
            {"kind": "success_rate", "description": "ok", "min": 0.9},
            {"kind": "breaker_opens", "description": "opens", "model": "mock-a/mock", "max_s": 2},
            # Probes every 1 s, 3 needed, 0.5 s timeout: closes within 3.5 s, plus polling.
            {"kind": "breaker_closes", "description": "closes", "model": "mock-a/mock", "max_s": 5},
        ],
    )
    summary = await run_scenario(s4, stack, [1], tmp_path, quiet=True)
    checks = by_kind(summary)
    # Streams already sending when the connections were reset end with an error event;
    # finishing them on another provider is mid-stream failover (week 4). Everything that
    # hadn't started streaming fails over.
    assert checks["success_rate"]["interval"]["hits"] >= 115
    assert checks["breaker_opens"]["verdict"] == "pass"
    assert checks["breaker_closes"]["verdict"] == "pass"
    assert (tmp_path / "report.md").read_text().startswith("# T: test")
    assert summary["seeds"][0]["with_failover"] > 0


async def test_total_outage_fails_fast_without_piling_on(stack: Target, tmp_path: Path) -> None:
    faults = [
        {"upstream": u, "mode": "refuse_connections", "start_s": 2, "duration_s": 5}
        for u in ("primary", "secondary")
    ]
    s8 = scenario(
        traffic={"model": "mock", "rate_rps": 10, "duration_s": 14},
        faults=faults,
        checks=[
            {
                "kind": "fast_failure",
                "description": "503 fast",
                "min": 0.9,
                "max_s": 1,
                "window": {"from_s": 2.2, "to_s": 7},
            },
            {"kind": "attempt_ratio", "description": "attempts", "max": 1.2},
            {"kind": "recovery", "description": "recovers", "max_s": 6},
        ],
    )
    summary = await run_scenario(s8, stack, [1], tmp_path, quiet=True)
    checks = by_kind(summary)
    fast = checks["fast_failure"]["interval"]
    assert fast["hits"] == fast["total"] > 40
    assert checks["attempt_ratio"]["verdict"] == "pass"
    assert checks["recovery"]["verdict"] == "pass"


def test_usage_errors_exit_2(tmp_path: Path) -> None:
    assert main(["run", str(tmp_path / "missing.yaml")]) == 2
    bad = tmp_path / "bad.yaml"
    bad.write_text("id: X\n")
    assert main(["run", str(bad)]) == 2
    assert main(["nonsense"]) == 2
