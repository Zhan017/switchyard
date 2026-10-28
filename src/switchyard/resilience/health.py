"""Per-model health: outcome counts and first-token samples in 10-second buckets.

Bucket numbers come from the wall clock (``time // bucket_s``), so replicas that share
buckets through Redis agree on which bucket a moment belongs to.

Counts live in three layers, summed whenever they are read:

- ``base``: the merged counts last read from Redis (empty without Redis);
- ``sending``: counts handed to the Redis sync and not yet confirmed;
- ``pending``: counts recorded since then.

Without Redis everything stays in ``pending``, which is the whole local truth. With Redis,
a successful sync replaces ``base`` with the merged counts (which include what was just
sent) and clears ``sending``; a failed sync puts ``sending`` back into ``pending``, so
nothing is lost while Redis is away.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

# Outcomes that count against a provider. Rate limits, client errors, content filters,
# refusals, bad keys and request deadlines don't: they say nothing about its health.
FAILURES = frozenset({"connection", "timeout", "stall", "server_error", "invalid_response"})
COUNTED = FAILURES | {"ok"}

# Upper bounds in seconds of the first-token histogram buckets. The last bucket is open.
TTFT_EDGES = (0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 13.0)


@dataclass
class Bucket:
    outcomes: Counter[str] = field(default_factory=Counter)
    ttft_samples: int = 0
    ttft_over: int = 0  # samples over the route's budget, including first-token timeouts
    ttft_hist: list[int] = field(default_factory=lambda: [0] * (len(TTFT_EDGES) + 1))
    attempts: int = 0  # first attempts on this model (primary or failover), for the budget
    retries: int = 0

    def add(self, other: Bucket) -> None:
        self.outcomes.update(other.outcomes)
        self.ttft_samples += other.ttft_samples
        self.ttft_over += other.ttft_over
        self.ttft_hist = [a + b for a, b in zip(self.ttft_hist, other.ttft_hist, strict=True)]
        self.attempts += other.attempts
        self.retries += other.retries

    def empty(self) -> bool:
        return not (self.outcomes or self.ttft_samples or self.attempts or self.retries)

    @property
    def counted(self) -> int:
        return sum(n for outcome, n in self.outcomes.items() if outcome in COUNTED)

    @property
    def failures(self) -> int:
        return sum(n for outcome, n in self.outcomes.items() if outcome in FAILURES)

    # Flat fields for a Redis hash, and back.

    def fields(self) -> dict[str, int]:
        out = {f"o:{k}": v for k, v in self.outcomes.items() if v}
        out.update({f"h:{i}": v for i, v in enumerate(self.ttft_hist) if v})
        for name in ("ttft_samples", "ttft_over", "attempts", "retries"):
            value = getattr(self, name)
            if value:
                out[name] = value
        return out

    @classmethod
    def from_fields(cls, raw: dict[str, int]) -> Bucket:
        bucket = cls()
        for key, value in raw.items():
            if key.startswith("o:"):
                bucket.outcomes[key[2:]] += value
            elif key.startswith("h:"):
                index = int(key[2:])
                if 0 <= index < len(bucket.ttft_hist):
                    bucket.ttft_hist[index] += value
            elif key in ("ttft_samples", "ttft_over", "attempts", "retries"):
                setattr(bucket, key, getattr(bucket, key) + value)
        return bucket


def histogram_index(seconds: float) -> int:
    for index, edge in enumerate(TTFT_EDGES):
        if seconds <= edge:
            return index
    return len(TTFT_EDGES)


def percentile(hist: list[int], share: float) -> float | None:
    """The upper bound of the bucket holding the given share of samples, or None if empty.
    The open last bucket reports as infinity."""
    total = sum(hist)
    if total == 0:
        return None
    rank = share * total
    seen = 0
    for index, count in enumerate(hist):
        seen += count
        if seen >= rank:
            return TTFT_EDGES[index] if index < len(TTFT_EDGES) else float("inf")
    return float("inf")


class ModelHealth:
    """Buckets for one provider and model."""

    def __init__(self, keep: int) -> None:
        self.keep = keep  # buckets kept, enough for every rule's look-back
        self.base: dict[int, Bucket] = {}
        self.sending: dict[int, Bucket] = {}
        self.pending: dict[int, Bucket] = {}
        self.consecutive_failures = 0  # per replica, never shared

    def current(self, index: int) -> Bucket:
        self.prune(index)
        bucket = self.pending.get(index)
        if bucket is None:
            bucket = self.pending[index] = Bucket()
        return bucket

    def prune(self, index: int) -> None:
        oldest = index - self.keep + 1
        for layer in (self.base, self.sending, self.pending):
            for old in [i for i in layer if i < oldest]:
                del layer[old]

    def total(self, indexes: Iterable[int]) -> Bucket:
        out = Bucket()
        for index in indexes:
            for layer in (self.base, self.sending, self.pending):
                bucket = layer.get(index)
                if bucket is not None:
                    out.add(bucket)
        return out

    def reset(self) -> None:
        """Forget the window, for a breaker that just closed: old failures must not trip
        it again straight away."""
        self.base.clear()
        self.sending.clear()
        self.pending.clear()
        self.consecutive_failures = 0

    # Redis sync

    def take_pending(self) -> dict[int, Bucket]:
        for index, bucket in self.pending.items():
            self.sending.setdefault(index, Bucket()).add(bucket)
        self.pending = {}
        return {i: b for i, b in self.sending.items() if not b.empty()}

    def sync_failed(self) -> None:
        for index, bucket in self.sending.items():
            self.pending.setdefault(index, Bucket()).add(bucket)
        self.sending = {}

    def sync_done(self, merged: dict[int, Bucket], now_index: int) -> None:
        self.base = merged
        self.sending = {}
        self.prune(now_index)
