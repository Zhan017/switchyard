"""What each stage writes, one JSON line per record. Files are appended to, and the last
line for a key wins, so a stage can be stopped and run again."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

# Outcomes a later run keeps. Anything else ("error") is tried again.
FINAL = {"ok", "meta_reply"}


@dataclass
class Reference:
    """A primary's full answer, and where it is cut."""

    prompt_id: str
    family: str
    model: str
    outcome: str = "error"
    text: str = ""
    finish_reason: str | None = None
    cut: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    spent_usd: float = 0.0  # every try, failed ones too
    tries: int = 0
    error: str | None = None

    @property
    def key(self) -> str:
        return f"{self.prompt_id}-{self.family}"

    @property
    def provider(self) -> str:
        return self.model.partition("/")[0]


@dataclass
class Stitched:
    """A reference cut at its cut point and finished by the gateway on a fallback."""

    item_id: str
    prompt_id: str
    kind: str
    primary: str
    fallback: str
    strategy: str
    provider: str  # the fallback's provider, which pays for the continuation
    cut: int = 0
    cut_fraction: float = 0.0
    outcome: str = "error"  # ok, meta_reply (refused by the gateway), error
    continuation: str = ""
    served_model: str | None = None
    gateway_strategy: str | None = None
    seam_actions: list[str] = field(default_factory=list)
    seam_buffer_ms: float | None = None  # the gateway holding the start for seam repair
    seam_ms: float | None = None  # failover comment to first continued text, client side
    heartbeats: int = 0
    finish_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0  # the continuation's cost, as the gateway reports it
    spent_usd: float = 0.0
    tries: int = 0
    not_continued: str | None = None
    retried_for: list[str] = field(default_factory=list)  # why earlier tries failed
    repeated_text: bool = False
    preface: bool = False
    meta_reply: bool = False
    error: str | None = None

    @property
    def key(self) -> str:
        return self.item_id

    @property
    def pair(self) -> str:
        return f"{self.primary}-{self.fallback}"


@dataclass
class Judgment:
    """One judge's answer on one item, in one view and one order."""

    item_id: str
    judge: str
    view: str  # local or full
    order: str  # RS: the reference is A; SR: the stitched answer is A
    provider: str
    outcome: str = "error"
    answer: str | None = None  # A or B
    correct: bool | None = None  # picked the stitched answer
    reply: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    spent_usd: float = 0.0
    tries: int = 0
    error: str | None = None

    @property
    def key(self) -> str:
        return f"{self.item_id}|{self.judge}|{self.view}|{self.order}"


def load[R: (Reference, Stitched, Judgment)](path: Path, cls: type[R]) -> dict[str, R]:
    """The last record per key."""
    found: dict[str, R] = {}
    if not path.exists():
        return found
    names = {f.name for f in fields(cls)}
    for line in path.read_text().splitlines():
        if line.strip():
            data = json.loads(line)
            record = cls(**{k: v for k, v in data.items() if k in names})
            found[record.key] = record
    return found


def append(path: Path, records: Iterable[Reference | Stitched | Judgment]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as file:
        for record in records:
            file.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


def spent_by_provider(*groups: Iterable[Any]) -> dict[str, float]:
    """USD spent so far per provider, failed tries included."""
    spent: dict[str, float] = {}
    for group in groups:
        for record in group:
            spent[record.provider] = spent.get(record.provider, 0.0) + record.spent_usd
    return spent
