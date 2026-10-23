"""Records written by the week 1 seam test."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Prompt:
    id: str
    kind: str
    text: str


@dataclass
class Reference:
    """A primary model's full answer, before it is cut."""

    prompt_id: str
    family: str
    model: str
    text: str = ""
    finish_reason: str = ""
    cost_usd: float = 0.0
    error: str | None = None


@dataclass
class Join:
    """One cut answer finished by one fallback model with one strategy."""

    item_id: str
    prompt_id: str
    kind: str
    primary: str
    fallback: str
    strategy: str
    cut_fraction: float
    emitted: str
    continuation_raw: str = ""
    continuation: str = ""
    actions: list[str] = field(default_factory=list)
    repeated_text: bool = False
    preface: bool = False
    meta_reply: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    finish_reason: str = ""
    error: str | None = None

    @property
    def pair(self) -> str:
        return f"{self.primary}-{self.fallback}"


def as_dict(item: Prompt | Reference | Join) -> dict[str, Any]:
    data = asdict(item)
    if isinstance(item, Join):
        data["pair"] = item.pair
    return data
