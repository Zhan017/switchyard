"""Token prices and cost per attempt."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from switchyard.core.types import Usage


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Rates(_Strict):
    """USD per million tokens."""

    input: float = Field(ge=0)
    cached_input: float | None = Field(default=None, ge=0)
    cache_write: float | None = Field(default=None, ge=0)
    output: float = Field(ge=0)


class LongContext(Rates):
    above_input_tokens: int = Field(gt=0)


class OffPeak(Rates):
    # Peak windows as [start_hour, end_hour) pairs in UTC, Monday to Friday.
    peak_hours_utc: list[tuple[int, int]]


class ModelPrice(Rates):
    long_context: LongContext | None = None
    off_peak: OffPeak | None = None


class PriceTable(_Strict):
    checked: date
    models: dict[str, ModelPrice]

    def rates_for(self, model_key: str, usage: Usage, at: datetime | None = None) -> Rates | None:
        price = self.models.get(model_key)
        if price is None:
            return None
        if price.long_context and usage.input_tokens > price.long_context.above_input_tokens:
            return price.long_context
        if price.off_peak and not _is_peak(at or datetime.now(UTC), price.off_peak):
            return price.off_peak
        return price

    def cost(self, model_key: str, usage: Usage, at: datetime | None = None) -> float | None:
        """Cost in USD, or None when the model has no price in the table."""
        rates = self.rates_for(model_key, usage, at)
        if rates is None:
            return None
        cached = usage.cached_input_tokens
        written = usage.cache_write_tokens
        uncached = max(0, usage.input_tokens - cached - written)
        cached_rate = rates.cached_input if rates.cached_input is not None else rates.input
        write_rate = rates.cache_write if rates.cache_write is not None else rates.input
        total = (
            uncached * rates.input
            + cached * cached_rate
            + written * write_rate
            + usage.output_tokens * rates.output
        )
        return total / 1_000_000


def _is_peak(at: datetime, off_peak: OffPeak) -> bool:
    at = at.astimezone(UTC)
    if at.weekday() >= 5:
        return False
    return any(start <= at.hour < end for start, end in off_peak.peak_hours_utc)


def load_prices(path: Path) -> PriceTable:
    return PriceTable.model_validate(yaml.safe_load(path.read_text()))
