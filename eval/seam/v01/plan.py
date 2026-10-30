"""The evaluation plan (plan.yaml) and prompts (prompts.yaml), loaded and checked."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from eval.seam.records import Prompt

HERE = Path(__file__).parent
ROOT = HERE.parent.parent.parent
GATEWAY_CONFIG = ROOT / "config" / "switchyard.yaml"
PRICES = ROOT / "config" / "prices.yaml"
STRATEGY_CODES = {"R": "R", "I": "I", "I+S": "IS", "A": "A", "N": "N"}  # safe in names
KINDS = ("prose", "code", "list")


@dataclass(frozen=True)
class Judge:
    name: str
    model: str  # provider/model in the gateway's config
    options: dict[str, Any]  # model options, when not taken from the gateway's config
    max_tokens: int
    expected_output_tokens: int

    @property
    def provider_type(self) -> str:
        return self.model.partition("/")[0]


@dataclass(frozen=True)
class Plan:
    seed: int
    max_tokens: int
    families: dict[str, str]
    primaries: list[str]
    pairs: list[tuple[str, str]]
    strategies: dict[str, str]
    local_chars: int
    judges: dict[str, Judge]
    neutral: dict[str, str]
    label_per_strategy: int
    label_seed: int
    kappa_gate: float
    budget_usd: dict[str, float]
    concurrency: dict[str, int]
    tries: int

    def provider_type(self, family: str) -> str:
        return self.families[family].partition("/")[0]

    def strategies_for(self, fallback: str) -> list[str]:
        native = self.provider_type(fallback) == "deepseek"
        return [s for s in self.strategies if s != "N" or native]

    def items(self, prompts: list[Prompt]) -> list[tuple[Prompt, str, str, str]]:
        """Every (prompt, primary, fallback, strategy) to generate, in a fixed order."""
        return [
            (prompt, primary, fallback, strategy)
            for prompt in prompts
            for primary, fallback in self.pairs
            for strategy in self.strategies_for(fallback)
        ]


def pair_name(primary: str, fallback: str) -> str:
    return f"{primary}-{fallback}"


def item_id(prompt_id: str, primary: str, fallback: str, strategy: str) -> str:
    return f"{prompt_id}-{primary}-{fallback}-{STRATEGY_CODES[strategy]}"


def load_plan(path: Path = HERE / "plan.yaml") -> Plan:
    raw = yaml.safe_load(path.read_text())
    families = dict(raw["families"])
    judges = {
        name: Judge(
            name=name,
            model=spec["model"],
            options=dict(spec.get("options") or {}),
            max_tokens=int(spec["max_tokens"]),
            expected_output_tokens=int(spec["expected_output_tokens"]),
        )
        for name, spec in raw["judges"].items()
    }
    plan = Plan(
        seed=int(raw["seed"]),
        max_tokens=int(raw["max_tokens"]),
        families=families,
        primaries=list(raw["primaries"]),
        pairs=[(p, f) for p, f in raw["pairs"]],
        strategies=dict(raw["strategies"]),
        local_chars=int(raw["local_chars"]),
        judges=judges,
        neutral=dict(raw["neutral"]),
        label_per_strategy=int(raw["labels"]["per_strategy"]),
        label_seed=int(raw["labels"]["seed"]),
        kappa_gate=float(raw["kappa_gate"]),
        budget_usd={k: float(v) for k, v in raw["budget_usd"].items()},
        concurrency={k: int(v) for k, v in raw["concurrency"].items()},
        tries=int(raw["tries"]),
    )
    _check(plan)
    return plan


def _check(plan: Plan) -> None:
    for primary, fallback in plan.pairs:
        for family in (primary, fallback):
            if family not in plan.families:
                raise SystemExit(f"plan: pair uses unknown family '{family}'")
        if primary not in plan.primaries:
            raise SystemExit(f"plan: '{primary}' is not a primary")
        judge = plan.neutral.get(pair_name(primary, fallback))
        if judge not in plan.judges:
            raise SystemExit(f"plan: pair {primary}-{fallback} has no known neutral judge")
    for strategy in plan.strategies:
        if strategy not in STRATEGY_CODES:
            raise SystemExit(f"plan: unknown strategy '{strategy}'")
    if "sonnet" not in plan.judges:
        raise SystemExit("plan: Sonnet judges every item and must be listed")


def load_prompts(path: Path = HERE / "prompts.yaml", limit: int | None = None) -> list[Prompt]:
    prompts = [Prompt(**item) for item in yaml.safe_load(path.read_text())]
    ids = [p.id for p in prompts]
    if len(set(ids)) != len(ids):
        raise SystemExit("prompts: ids must be unique")
    for prompt in prompts:
        if prompt.kind not in KINDS:
            raise SystemExit(f"prompts: {prompt.id} has unknown kind '{prompt.kind}'")
    return prompts[:limit] if limit else prompts
