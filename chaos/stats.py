"""Confidence intervals and verdicts for the outage scenarios (spec 10)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

Z95 = 1.959963984540054

Verdict = Literal["pass", "fail", "inconclusive", "n/a"]


@dataclass(frozen=True)
class Interval:
    successes: int
    total: int
    low: float
    high: float

    @property
    def rate(self) -> float:
        return self.successes / self.total if self.total else 0.0


def wilson(successes: int, total: int, z: float = Z95) -> Interval:
    """The Wilson score interval for a proportion. Good at rates near 0 and 1, where the
    normal approximation breaks down."""
    if total == 0:
        return Interval(0, 0, 0.0, 1.0)
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    spread = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return Interval(successes, total, max(0.0, centre - spread), min(1.0, centre + spread))


def minimum_samples(target: float, z: float = Z95) -> int:
    """The fewest samples for which a perfect run would clear ``target``. Below this a
    run can't pass whatever happens, so the verdict is inconclusive, not a fail."""
    n = 1
    while wilson(n, n, z).low < target:
        n += 1
    return n


def verdict_at_least(interval: Interval, target: float) -> Verdict:
    """Pass when the lower bound of the 95% interval clears the target."""
    if interval.total < minimum_samples(target):
        return "inconclusive"
    return "pass" if interval.low >= target else "fail"
