"""Spending caps per provider, checked before a stage and before every call."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from switchyard.accounting.pricing import PriceTable, load_prices
from switchyard.core.types import Usage


class OverBudget(Exception):
    pass


@dataclass
class Estimate:
    expected: dict[str, float] = field(default_factory=dict)
    worst: dict[str, float] = field(default_factory=dict)
    calls: dict[str, int] = field(default_factory=dict)

    def add(self, provider: str, expected: float, worst: float) -> None:
        self.expected[provider] = self.expected.get(provider, 0.0) + expected
        self.worst[provider] = self.worst.get(provider, 0.0) + worst
        self.calls[provider] = self.calls.get(provider, 0) + 1


class Prices:
    """Token prices from the gateway's generated table, at the current time of day."""

    def __init__(self, path: Path) -> None:
        self.table: PriceTable = load_prices(path)

    def cost(self, model_key: str, input_tokens: int, output_tokens: int) -> float:
        usage = Usage(input_tokens=input_tokens, output_tokens=output_tokens)
        return self.table.cost(model_key, usage) or 0.0

    def is_peak(self, model_key: str) -> bool:
        """True when the model has off-peak prices and it is now peak time."""
        price = self.table.models.get(model_key)
        if price is None or price.off_peak is None:
            return False
        rates = self.table.rates_for(model_key, Usage(1, 1), datetime.now(UTC))
        return rates is not price.off_peak


class Budget:
    def __init__(self, caps: dict[str, float], spent: dict[str, float]) -> None:
        self.caps = caps
        self.spent = dict(spent)
        self.reserved: dict[str, float] = {}
        self.stopped: set[str] = set()

    def left(self, provider: str) -> float:
        return self.caps.get(provider, 0.0) - self.spent.get(provider, 0.0)

    def check(self, estimate: Estimate) -> list[str]:
        """Providers whose expected cost would take them over their cap."""
        return [
            provider
            for provider, cost in estimate.expected.items()
            if cost > self.left(provider) + 1e-9
        ]

    @asynccontextmanager
    async def hold(self, provider: str, worst: float) -> AsyncIterator[None]:
        """Reserve a call's worst-case cost for as long as it runs. Raises OverBudget,
        and stops the provider for the rest of the stage, if it doesn't fit."""
        reserved = self.reserved.get(provider, 0.0)
        if provider in self.stopped or reserved + worst > self.left(provider):
            self.stopped.add(provider)
            raise OverBudget(provider)
        self.reserved[provider] = reserved + worst
        try:
            yield
        finally:
            self.reserved[provider] -= worst

    def add(self, provider: str, cost: float) -> None:
        self.spent[provider] = self.spent.get(provider, 0.0) + cost
