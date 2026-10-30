"""What every stage shares: the plan, the gateway client, the budget and the run log."""

from __future__ import annotations

import asyncio
import json
import random
import subprocess
import sys
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from eval.seam.v01.budget import Budget, Estimate, OverBudget, Prices
from eval.seam.v01.client import Fatal
from eval.seam.v01.plan import ROOT, Plan

# This many calls in a row failing for a reason a retry won't fix stops the stage: the
# cause is almost certainly the setup, not the items.
MAX_HARD_FAILURES_IN_A_ROW = 5


@dataclass
class Stage:
    name: str
    plan: Plan
    out: Path
    http: httpx.AsyncClient
    budget: Budget
    prices: Prices
    dry_run: bool
    limits: dict[str, asyncio.Semaphore] = field(default_factory=dict)
    done: int = 0
    total: int = 0
    skipped: int = 0  # not started: a cap was reached
    hard_failures: int = 0

    def __post_init__(self) -> None:
        self.limits = {p: asyncio.Semaphore(n) for p, n in self.plan.concurrency.items()}

    def hard_failure(self, error: str) -> None:
        self.hard_failures += 1
        if self.hard_failures >= MAX_HARD_FAILURES_IN_A_ROW:
            raise Fatal(f"{self.hard_failures} calls in a row failed; the last with: {error}")

    def succeeded(self) -> None:
        self.hard_failures = 0

    def progress(self) -> None:
        self.done += 1
        if self.done % 25 == 0 or self.done == self.total:
            spent = ", ".join(f"{p} ${v:.3f}" for p, v in sorted(self.budget.spent.items()))
            print(f"  {self.name}: {self.done}/{self.total} ({spent})", file=sys.stderr)

    async def backoff(self, attempt: int) -> None:
        if not self.dry_run:
            await asyncio.sleep(random.uniform(0.5, 1.5) * 5 * 3**attempt)  # 5, 15, 45 s


async def run_all(stage: Stage, jobs: Iterable[Callable[[], Awaitable[None]]]) -> None:
    """Run jobs concurrently (each limits itself per provider). A Fatal error stops all."""
    queued = list(jobs)
    stage.total = len(queued)

    async def guarded(job: Callable[[], Awaitable[None]]) -> None:
        try:
            await job()
        except OverBudget:
            stage.skipped += 1

    try:
        async with asyncio.TaskGroup() as group:
            for job in queued:
                group.create_task(guarded(job))
    except* Fatal as errors:
        raise errors.exceptions[0] from None


def check_estimate(
    stage: Stage, estimate: Estimate, allow_peak: bool, peak_keys: list[str]
) -> None:
    """Print the estimate, and refuse to start over a cap or at DeepSeek's peak."""
    if not estimate.calls:
        print(f"{stage.name}: nothing left to do")
        return
    print(f"{stage.name}: estimated cost of what is left")
    for provider in sorted(estimate.calls):
        print(
            f"  {provider}: {estimate.calls[provider]} calls, about "
            f"${estimate.expected[provider]:.2f} (at most ${estimate.worst[provider]:.2f}); "
            f"${stage.budget.spent.get(provider, 0.0):.2f} spent of the "
            f"${stage.budget.caps.get(provider, 0.0):.2f} cap"
        )
    over = stage.budget.check(estimate)
    if over and not stage.dry_run:
        raise SystemExit(f"Refused: the estimate is over the cap for {', '.join(over)}.")
    peak = [key for key in peak_keys if stage.prices.is_peak(key)]
    if peak and not allow_peak and not stage.dry_run:
        raise SystemExit(
            "Refused: it is DeepSeek's peak time, when its prices are double. Run outside "
            "01:00-04:00 and 06:00-10:00 UTC on weekdays, or pass --allow-peak."
        )


def commit() -> str | None:
    try:
        found = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return found.stdout.strip() or None


def log_run(out: Path, entry: dict[str, Any]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    entry = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "commit": commit(), **entry}
    with (out / "runs.jsonl").open("a") as file:
        file.write(json.dumps(entry) + "\n")
