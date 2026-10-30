"""Stage 4: the report. Seamless rates by strategy and pair with 95% intervals, the judges'
agreement with the human labels, and what each strategy costs in time and money."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chaos.stats import Interval, wilson
from eval.seam.records import Prompt
from eval.seam.v01.judge import verdicts
from eval.seam.v01.labels import cohen_kappa, human_verdicts, load_key
from eval.seam.v01.plan import Plan, pair_name
from eval.seam.v01.records import Judgment, Reference, Stitched

NAMES = {"claude": "Claude", "gpt": "GPT", "deepseek": "DeepSeek"}
STRATEGY_NAMES = {
    "R": "restart",
    "I": "instructed",
    "I+S": "instructed + seam repair",
    "A": "anchored",
    "N": "native (DeepSeek prefix)",
}
JUDGE_NAMES = {"sonnet": "Claude Sonnet 5.5", "deepseek": "DeepSeek Flash", "gpt": "GPT-6 Luna"}


def pair_label(pair: str) -> str:
    primary, fallback = pair.split("-")
    return f"{NAMES[primary]} to {NAMES[fallback]}"


@dataclass
class Gate:
    judge: str
    n: int
    kappa: float | None
    agreement: float | None
    passed: bool | None  # None: no labels yet


def gates(plan: Plan, out: Path, judgments: dict[str, Judgment]) -> dict[str, Gate]:
    try:
        human = human_verdicts(out)
    except SystemExit:
        return {name: Gate(name, 0, None, None, None) for name in plan.judges}
    found = {}
    for name in plan.judges:
        judged = verdicts(judgments, name, "local")
        common = [item for item in human if item in judged]
        h = [human[i] for i in common]
        j = [judged[i] for i in common]
        kappa = cohen_kappa(h, j)
        agreement = sum(x == y for x, y in zip(h, j, strict=True)) / len(h) if h else None
        passed = kappa is not None and kappa >= plan.kappa_gate
        found[name] = Gate(name, len(common), kappa, agreement, passed)
    return found


def headline_judge(
    plan: Plan, pair: str, gate: dict[str, Gate], enforce: bool = True
) -> tuple[str | None, str]:
    """The judge whose verdicts give the pair's headline, and a note on why. A dry run's
    labels are random, so it doesn't apply the gate."""
    neutral = plan.neutral[pair]
    if gate[neutral].passed is not False or not enforce:
        return neutral, ""
    if neutral != "sonnet" and gate["sonnet"].passed is not False:
        return "sonnet", "neutral judge failed the gate; judged by Sonnet, same family as a model"
    return None, "no judge passed the gate"


@dataclass
class Cell:
    seamless: int = 0
    total: int = 0  # finished items: continued and judged, or refused by the gateway
    refused: int = 0  # meta replies the gateway would not send
    unjudged: int = 0
    unfinished: int = 0  # still failing after every try

    @property
    def interval(self) -> Interval:
        return wilson(self.seamless, self.total)


def cell(items: list[Stitched], judged: dict[str, bool]) -> Cell:
    c = Cell()
    for item in items:
        if item.outcome == "meta_reply":
            c.total += 1  # a failed continuation is not a seamless one
            c.refused += 1
        elif item.outcome != "ok":
            c.unfinished += 1
        elif item.item_id not in judged:
            c.unjudged += 1
        else:
            c.total += 1
            c.seamless += not judged[item.item_id]
    return c


def fmt(c: Cell) -> str:
    if c.total == 0:
        return "n/a"
    i = c.interval
    return f"{c.seamless}/{c.total} = {i.rate:.0%} ({i.low:.0%} to {i.high:.0%})"


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def analyze(
    plan: Plan,
    out: Path,
    prompts: list[Prompt],
    refs: dict[str, Reference],
    stitched: dict[str, Stitched],
    judgments: dict[str, Judgment],
    spent: dict[str, float],
    dry_run: bool,
) -> dict[str, Any]:
    items = list(stitched.values())
    gate = gates(plan, out, judgments)
    local = {name: verdicts(judgments, name, "local") for name in plan.judges}
    full = verdicts(judgments, "sonnet", "full")
    pairs = [pair_name(p, f) for p, f in plan.pairs]

    def group(strategy: str, pair: str | None = None) -> list[Stitched]:
        return [i for i in items if i.strategy == strategy and (pair is None or i.pair == pair)]

    headline: dict[str, dict[str, Any]] = {}
    for pair in pairs:
        judge, note = headline_judge(plan, pair, gate, enforce=not dry_run)
        row: dict[str, Any] = {"judge": judge, "note": note}
        for strategy in plan.strategies:
            members = group(strategy, pair)
            if members:
                row[strategy] = cell(members, local[judge]) if judge else None
        headline[pair] = row

    pooled = {}
    for strategy in plan.strategies:
        c = Cell()
        for pair in pairs:
            found = headline[pair].get(strategy)
            if isinstance(found, Cell):
                for name in ("seamless", "total", "refused", "unjudged", "unfinished"):
                    setattr(c, name, getattr(c, name) + getattr(found, name))
        pooled[strategy] = c

    full_view = {
        pair: {s: cell(group(s, pair), full) for s in plan.strategies if group(s, pair)}
        for pair in pairs
    }
    claude_pairs = [p for p in pairs if plan.neutral[p] != "sonnet"]
    self_check = {
        strategy: {
            "sonnet": cell([i for i in group(strategy) if i.pair in claude_pairs], local["sonnet"]),
            "neutral": _neutral_cell(plan, group(strategy), claude_pairs, local),
        }
        for strategy in plan.strategies
    }

    checks = {}
    for pair in pairs:
        for strategy in plan.strategies:
            members = group(strategy, pair)
            ok = [i for i in members if i.outcome == "ok"]
            if not members:
                continue
            checks[(pair, strategy)] = {
                "items": len(members),
                "continued": len(ok),
                "refused": sum(i.outcome == "meta_reply" for i in members),
                "unfinished": sum(i.outcome not in ("ok", "meta_reply") for i in members),
                "retried": sum(i.tries > 1 for i in members),
                "repeated_text": sum(i.repeated_text for i in ok),
                "preface": sum(i.preface for i in ok),
                "meta_reply_sent": sum(i.meta_reply for i in ok),
            }

    timing = {}
    for fallback in plan.families:
        for strategy in plan.strategies:
            ok = [
                i
                for i in items
                if i.fallback == fallback and i.strategy == strategy and i.outcome == "ok"
            ]
            if not ok:
                continue
            seam = [i.seam_ms for i in ok if i.seam_ms is not None]
            buffer = [i.seam_buffer_ms for i in ok if i.seam_buffer_ms is not None]
            costs = [i.cost_usd for i in ok]
            timing[(fallback, strategy)] = {
                "n": len(ok),
                "seam_p50_ms": pct(seam, 0.5),
                "seam_p95_ms": pct(seam, 0.95),
                "buffer_p50_ms": pct(buffer, 0.5),
                "buffer_p95_ms": pct(buffer, 0.95),
                "cost_mean_usd": sum(costs) / len(costs),
            }

    report = _markdown(
        plan,
        prompts,
        refs,
        items,
        gate,
        headline,
        pooled,
        full_view,
        self_check,
        checks,
        timing,
        spent,
        dry_run,
        len(labelled_ids(out)),
    )
    (out / "report.md").write_text(report)
    summary = {
        "dry_run": dry_run,
        "gates": {k: vars(v) for k, v in gate.items()},
        "headline": {
            pair: {
                "judge": row["judge"],
                "note": row["note"],
                **{s: _cell_json(c) for s, c in row.items() if isinstance(c, Cell)},
            }
            for pair, row in headline.items()
        },
        "pooled": {s: _cell_json(c) for s, c in pooled.items()},
        "full_view_sonnet": {
            pair: {s: _cell_json(c) for s, c in row.items()} for pair, row in full_view.items()
        },
        "self_preference": {
            s: {k: _cell_json(c) for k, c in row.items()} for s, row in self_check.items()
        },
        "checks": {f"{p}|{s}": v for (p, s), v in checks.items()},
        "timing": {f"{f}|{s}": v for (f, s), v in timing.items()},
        "spent_usd": spent,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def _neutral_cell(
    plan: Plan, members: list[Stitched], pairs: list[str], local: dict[str, dict[str, bool]]
) -> Cell:
    c = Cell()
    for item in members:
        if item.pair in pairs:
            one = cell([item], local[plan.neutral[item.pair]])
            for name in ("seamless", "total", "refused", "unjudged", "unfinished"):
                setattr(c, name, getattr(c, name) + getattr(one, name))
    return c


def _cell_json(c: Cell) -> dict[str, Any]:
    i = c.interval
    return {
        "seamless": c.seamless,
        "total": c.total,
        "rate": round(i.rate, 4),
        "low": round(i.low, 4),
        "high": round(i.high, 4),
        "refused": c.refused,
        "unjudged": c.unjudged,
        "unfinished": c.unfinished,
    }


def _ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.0f}"


def _markdown(
    plan: Plan,
    prompts: list[Prompt],
    refs: dict[str, Reference],
    items: list[Stitched],
    gate: dict[str, Gate],
    headline: dict[str, dict[str, Any]],
    pooled: dict[str, Cell],
    full_view: dict[str, dict[str, Cell]],
    self_check: dict[str, dict[str, Cell]],
    checks: dict[tuple[str, str], dict[str, int]],
    timing: dict[tuple[str, str], dict[str, Any]],
    spent: dict[str, float],
    dry_run: bool,
    labelled: int,
) -> str:
    strategies = list(plan.strategies)
    lines = ["# Seam evaluation v0.1", ""]
    if dry_run:
        lines += ["**Dry run: fake models and random judges. These numbers mean nothing.**", ""]
    gated = all(g.passed is not None for g in gate.values())
    if not gated:
        lines += ["**Provisional: no human labels yet, so no judge has passed the gate.**", ""]
    ok_refs = [r for r in refs.values() if r.outcome == "ok"]
    lines += [
        f"{len(prompts)} prompts, {len(ok_refs)} reference answers, {len(items)} stitched "
        f"answers ({sum(i.outcome == 'ok' for i in items)} continued, "
        f"{sum(i.outcome == 'meta_reply' for i in items)} refused as meta replies, "
        f"{sum(i.outcome not in ('ok', 'meta_reply') for i in items)} unfinished). "
        "An item is seamless when the judge fails to pick the stitched answer in at least one "
        "of the two orders. A continuation the gateway refused counts as not seamless.",
        "",
        "## Seamless rate, local view (headline)",
        "",
        "Each pair is judged by its neutral judge, from neither family, unless noted. "
        "95% Wilson intervals.",
        "",
        "| Pair | Judge | " + " | ".join(strategies) + " |",
        "| --- | --- | " + " | ".join("---" for _ in strategies) + " |",
    ]
    for pair, row in headline.items():
        judge = JUDGE_NAMES.get(row["judge"] or "", "none")
        if row["note"]:
            judge += f" ({row['note']})"
        values = [fmt(row[s]) if isinstance(row.get(s), Cell) else "" for s in strategies]
        lines.append(f"| {pair_label(pair)} | {judge} | " + " | ".join(values) + " |")
    values = [fmt(pooled[s]) for s in strategies]
    lines += ["| All pairs | headline judges | " + " | ".join(values) + " |", ""]
    lines += ["Strategies: " + "; ".join(f"{s} {STRATEGY_NAMES[s]}" for s in strategies), ""]

    lines += [
        "## Judges against the human labels",
        "",
        f"Cohen's kappa on the {labelled} labelled items, local view. "
        f"A judge's numbers count only at {plan.kappa_gate} or above.",
        "",
        "| Judge | Items | Agreement | Kappa | Gate |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, g in gate.items():
        kappa = "n/a" if g.kappa is None else f"{g.kappa:.2f}"
        agreement = "n/a" if g.agreement is None else f"{g.agreement:.0%}"
        verdict = {True: "pass", False: "fail", None: "no labels"}[g.passed]
        lines.append(f"| {JUDGE_NAMES[name]} | {g.n} | {agreement} | {kappa} | {verdict} |")
    lines.append("")

    lines += [
        "## Seamless rate, full view (Sonnet)",
        "",
        "The whole reference against the whole stitched answer, so a change of style counts too.",
        "",
        "| Pair | " + " | ".join(strategies) + " |",
        "| --- | " + " | ".join("---" for _ in strategies) + " |",
    ]
    for pair, row in full_view.items():
        values = [fmt(row[s]) if s in row else "" for s in strategies]
        lines.append(f"| {pair_label(pair)} | " + " | ".join(values) + " |")
    lines.append("")

    lines += [
        "## Self-preference check",
        "",
        "On the four pairs with a Claude model, Sonnet against the neutral judge, local view. "
        "A higher rate from Sonnet would mean it misses Claude's joins more often.",
        "",
        "| Strategy | Sonnet | Neutral judges |",
        "| --- | --- | --- |",
    ]
    for strategy, row in self_check.items():
        lines.append(f"| {strategy} | {fmt(row['sonnet'])} | {fmt(row['neutral'])} |")
    lines.append("")

    lines += [
        "## Automatic checks",
        "",
        "Counts of continued items. Repeated text: 8 words of the continuation already in the "
        "last 300 words sent. Refused: the gateway caught a meta reply and sent nothing. "
        "Unfinished: still failing after every try.",
        "",
        "| Pair | Strategy | Items | Continued | Refused | Unfinished | Retried | Repeated "
        "text | Preface | Meta reply sent |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for (pair, strategy), c in checks.items():
        lines.append(
            f"| {pair_label(pair)} | {strategy} | {c['items']} | {c['continued']} | "
            f"{c['refused']} | {c['unfinished']} | {c['retried']} | {c['repeated_text']} | "
            f"{c['preface']} | {c['meta_reply_sent']} |"
        )
    lines.append("")

    lines += [
        "## Seam latency and extra cost",
        "",
        "Seam latency: from the failover comment to the first continued text, as the client "
        "saw it (the fallback's time to first token, plus seam repair's buffering for I+S and "
        "A). Buffer: the gateway holding the start for seam repair. Extra cost: the "
        "continuation call, per failover.",
        "",
        "| Fallback | Strategy | Items | Seam p50 ms | Seam p95 ms | Buffer p50 ms | "
        "Buffer p95 ms | Extra cost per failover |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for (fallback, strategy), t in timing.items():
        lines.append(
            f"| {NAMES[fallback]} | {strategy} | {t['n']} | {_ms(t['seam_p50_ms'])} | "
            f"{_ms(t['seam_p95_ms'])} | {_ms(t['buffer_p50_ms'])} | {_ms(t['buffer_p95_ms'])} | "
            f"${t['cost_mean_usd']:.5f} |"
        )
    lines.append("")

    lines += ["## Spend", "", "| Provider | Spent | Cap |", "| --- | --- | --- |"]
    for provider in sorted(set(spent) | set(plan.budget_usd)):
        lines.append(
            f"| {provider} | ${spent.get(provider, 0.0):.3f} | "
            f"${plan.budget_usd.get(provider, 0.0):.2f} |"
        )
    lines.append("")
    lengths = [len(r.text) for r in ok_refs]
    if lengths:
        cut_off = sum(r.finish_reason == "length" for r in ok_refs)
        lines += [
            "## References",
            "",
            f"{len(ok_refs)} answers, {min(lengths):,} to {max(lengths):,} characters "
            f"(mean {sum(lengths) / len(lengths):,.0f}); {cut_off} hit the token limit. "
            "Each was cut once, at a seeded point between 20% and 80% of its length.",
            "",
        ]
    return "\n".join(lines)


def labelled_ids(out: Path) -> set[str]:
    try:
        return {k["item_id"] for k in load_key(out)}
    except SystemExit:
        return set()
