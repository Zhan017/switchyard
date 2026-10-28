"""Scenario files: traffic, faults on a schedule, and the checks a run is judged by.

Scenario files and their targets are committed and tagged before the first run (spec 10),
so the history shows they were fixed in advance. See chaos/scenarios/ for the files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Traffic(_Strict):
    model: str = "mock"  # what the client asks for: a route group or provider/model
    rate_rps: float = Field(gt=0)
    duration_s: float = Field(gt=0)
    stream_share: float = Field(default=0.5, ge=0, le=1)
    # Answer lengths: each request draws max_tokens from this list. mockllm ignores prompt
    # content, so only lengths matter here; real prompts matter only for the seam test.
    max_tokens: list[int] = Field(default_factory=lambda: [16, 32, 48, 64], min_length=1)
    timeout_s: float = Field(default=130.0, gt=0)  # client side, above the gateway deadline

    @property
    def requests(self) -> int:
        return round(self.rate_rps * self.duration_s)


class Fault(_Strict):
    upstream: str  # a name from the topology, mapped to a mockllm control URL by the CLI
    mode: str
    params: dict[str, Any] = Field(default_factory=dict)
    model: str | None = None  # limit the fault to one upstream model name
    start_s: float = Field(ge=0)
    duration_s: float = Field(gt=0)

    @property
    def end_s(self) -> float:
        return self.start_s + self.duration_s


class Window(_Strict):
    """Requests are judged by when they arrived, in seconds from the start of traffic."""

    from_s: float = 0.0
    to_s: float | None = None  # the end of traffic


CheckKind = Literal[
    "success_rate",  # share of requests that completed; lower bound >= min
    "fast_failure",  # share answered with 503 within max_s; lower bound >= min
    "breaker_opens",  # the model's breaker opens within max_s of the first fault starting
    "breaker_closes",  # and closes within max_s of the last fault ending
    "attempt_ratio",  # upstream attempts (probes excluded) per client request <= max
    "recovery",  # the last failed request arrives within max_s of the last fault ending
]


class Check(_Strict):
    kind: CheckKind
    description: str
    min: float | None = Field(default=None, gt=0, le=1)
    max: float | None = Field(default=None, gt=0)
    max_s: float | None = Field(default=None, gt=0)
    model: str | None = None  # provider/model, for breaker checks
    window: Window = Field(default_factory=Window)

    @model_validator(mode="after")
    def _fields_for_kind(self) -> Check:
        needs: dict[str, tuple[str, ...]] = {
            "success_rate": ("min",),
            "fast_failure": ("min", "max_s"),
            "breaker_opens": ("model",),
            "breaker_closes": ("model", "max_s"),
            "attempt_ratio": ("max",),
            "recovery": ("max_s",),
        }
        missing = [f for f in needs[self.kind] if getattr(self, f) is None]
        if missing:
            raise ValueError(f"check {self.kind} needs {', '.join(missing)}")
        return self

    @property
    def target(self) -> str:
        """The target in words, for reports."""
        if self.kind == "success_rate":
            return f"lower bound >= {self.min:.0%}"
        if self.kind == "fast_failure":
            return f"lower bound >= {self.min:.0%} within {self.max_s:g} s"
        if self.kind == "attempt_ratio":
            return f"<= {self.max:g}x requests"
        if self.kind == "breaker_opens":
            return "opens" if self.max_s is None else f"within {self.max_s:g} s"
        return f"within {self.max_s:g} s"


class Scenario(_Strict):
    id: str
    title: str
    release: str
    description: str
    topology: dict[str, str]
    traffic: Traffic
    seeds: list[int] = Field(min_length=1)
    faults: list[Fault]
    checks: list[Check] = Field(min_length=1)

    @model_validator(mode="after")
    def _upstreams_exist(self) -> Scenario:
        for fault in self.faults:
            if fault.upstream not in self.topology:
                raise ValueError(f"fault on unknown upstream '{fault.upstream}'")
            if fault.end_s > self.traffic.duration_s:
                raise ValueError(f"a {fault.mode} fault outlasts the traffic")
        return self

    @property
    def upstreams(self) -> list[str]:
        return sorted({f.upstream for f in self.faults})

    @property
    def fault_start(self) -> float:
        return min(f.start_s for f in self.faults)

    @property
    def fault_end(self) -> float:
        return max(f.end_s for f in self.faults)


def load(path: Path) -> Scenario:
    return Scenario.model_validate(yaml.safe_load(path.read_text()))
