"""Write the week 1 seam test results: a readable report, raw JSONL and a labels sheet."""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from eval.seam.records import Join, Prompt, Reference, as_dict

STRATEGY_ORDER = ["R", "I", "I+S", "N"]
STRATEGY_NAMES = {
    "R": "restart",
    "I": "instructed",
    "I+S": "instructed + seam repair",
    "N": "native",
}
SHOW_CHARS = 240
CUT_MARK = "⟦CUT⟧"


def family_name(family: str) -> str:
    return {"claude": "Claude", "gpt": "GPT", "deepseek": "DeepSeek"}.get(family, family)


def pair_name(primary: str, fallback: str) -> str:
    return f"{family_name(primary)} to {family_name(fallback)}"


def write_outputs(
    out: Path,
    meta: dict[str, Any],
    prompts: list[Prompt],
    references: list[Reference],
    joins: list[Join],
) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    _write_jsonl(out / "references.jsonl", [as_dict(r) for r in references])
    _write_jsonl(out / "joins.jsonl", [as_dict(j) for j in joins])
    _write_labels(out / "labels.csv", joins)

    table = summarize(joins)
    reference_cost = sum(r.cost_usd for r in references)
    join_cost = sum(j.cost_usd for j in joins)
    totals = {
        "references": len(references),
        "reference_errors": sum(1 for r in references if r.error),
        "joins": len(joins),
        "join_errors": sum(1 for j in joins if j.error),
        "cost_usd": round(reference_cost + join_cost, 4),
    }
    summary = {"meta": meta, "totals": totals, "table": table}
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (out / "report.md").write_text(render(meta, totals, table, prompts, references, joins))
    return summary


def summarize(joins: list[Join]) -> list[dict[str, Any]]:
    rows = []
    pairs = sorted({(j.primary, j.fallback) for j in joins}, key=lambda p: (p[0], p[1]))
    for primary, fallback in pairs:
        for strategy in STRATEGY_ORDER:
            group = [
                j
                for j in joins
                if (j.primary, j.fallback, j.strategy) == (primary, fallback, strategy)
            ]
            if not group:
                continue
            ok = [j for j in group if not j.error]
            actions = Counter(action for j in ok for action in j.actions)
            rows.append(
                {
                    "pair": pair_name(primary, fallback),
                    "strategy": strategy,
                    "joins": len(group),
                    "errors": len(group) - len(ok),
                    "repeated_text_pct": _pct(sum(j.repeated_text for j in ok), len(ok)),
                    "preface_pct": _pct(sum(j.preface for j in ok), len(ok)),
                    "mean_cost_usd": round(sum(j.cost_usd for j in ok) / len(ok), 6) if ok else 0,
                    "mean_latency_s": round(sum(j.latency_s for j in ok) / len(ok), 2) if ok else 0,
                    "repair_actions": dict(actions.most_common()),
                }
            )
    return rows


def _pct(count: int, total: int) -> float:
    return round(100 * count / total, 1) if total else 0.0


def render(
    meta: dict[str, Any],
    totals: dict[str, Any],
    table: list[dict[str, Any]],
    prompts: list[Prompt],
    references: list[Reference],
    joins: list[Join],
) -> str:
    lines = ["# Seam test, week 1", ""]
    if meta["dry_run"]:
        lines += [
            "**Dry run with a fake provider. These numbers mean nothing; they only show "
            "that the pipeline works.**",
            "",
        ]
    models = ", ".join(f"{family_name(f)} `{m}`" for f, m in meta["models"].items())
    lines += [
        f"Run {meta['started']} · seed {meta['seed']} · {meta['prompts']} prompts · "
        f"{len(meta['pairs'])} pairs · total cost ${totals['cost_usd']:.4f}",
        "",
        f"Models: {models}",
        "",
        "## How to read this",
        "",
        f"Each join shows the last {SHOW_CHARS} characters the reader already had, then "
        f"`{CUT_MARK}`, then the first {SHOW_CHARS} characters the fallback model wrote. "
        "Ask of each one: would a reader notice the answer was interrupted?",
        "",
        "- **R, restart:** the fallback answers again from the top, so the reader sees the "
        "start twice. This is the floor.",
        "- **I, instructed:** the fallback gets the partial answer and is told to continue "
        "from exactly where it stopped.",
        "- **I+S:** the same text as I, after seam repair. The actions say what was fixed.",
        "- **N, native:** DeepSeek's beta prefix completion continues the partial answer directly.",
        "",
        "*Repeated text* means 8 or more words in a row that the reader already saw in the "
        "last 300 words. *Preface* means the continuation opens with something like "
        '"Sure, here is the rest".',
        "",
        "## Summary",
        "",
        "| Pair | Strategy | Joins | Errors | Repeated text | Preface | Cost per join "
        "| Latency | Repair actions |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in table:
        cost = "same call as I" if row["strategy"] == "I+S" else f"${row['mean_cost_usd']:.5f}"
        actions = ", ".join(f"{k} {v}" for k, v in row["repair_actions"].items()) or "none"
        lines.append(
            f"| {row['pair']} | {row['strategy']} | {row['joins']} | {row['errors']} "
            f"| {row['repeated_text_pct']}% | {row['preface_pct']}% | {cost} "
            f"| {row['mean_latency_s']} s | {actions} |"
        )
    lines += [
        "",
        f"References: {totals['references']} ({totals['reference_errors']} errors). "
        f"Joins: {totals['joins']} ({totals['join_errors']} errors).",
        "",
        "## Go or no-go",
        "",
        "_To fill in after reading the joins: does I+S read cleanly on most joins? Which "
        "failures repeat, and does seam repair need another rule?_",
        "",
    ]

    errors = [r for r in references if r.error]
    if errors:
        lines += ["## Reference errors", ""]
        lines += [f"- {r.prompt_id} {family_name(r.family)}: {r.error}" for r in errors]
        lines.append("")

    lines += ["## Joins", ""]
    prompt_text = {p.id: p.text for p in prompts}
    order = {s: i for i, s in enumerate(STRATEGY_ORDER)}
    keyed = sorted(joins, key=lambda j: (j.primary, j.fallback, j.prompt_id, order[j.strategy]))
    current: tuple[str, str, str] | None = None
    for join in keyed:
        key = (join.primary, join.fallback, join.prompt_id)
        if key != current:
            current = key
            short = prompt_text[join.prompt_id]
            short = short if len(short) <= 200 else short[:200] + "..."
            lines += [
                f"### {join.prompt_id} · {pair_name(join.primary, join.fallback)} · "
                f"{join.kind} · cut at {round(join.cut_fraction * 100)}%",
                "",
                f"> {short}",
                "",
            ]
        lines += _render_join(join)
    return "\n".join(lines) + "\n"


def _render_join(join: Join) -> list[str]:
    title = f"**{join.strategy}** ({STRATEGY_NAMES[join.strategy]})"
    if join.error:
        return [f"{title} · error: `{join.error[:300]}`", ""]
    flags = []
    if join.repeated_text:
        flags.append("repeated text")
    if join.preface:
        flags.append("preface")
    if join.strategy == "I+S":
        flags.append("actions: " + (", ".join(join.actions) or "none"))
    if join.finish_reason not in ("stop", "end_turn", "length", "max_tokens"):
        flags.append(f"finish: {join.finish_reason}")
    header = title + (" · " + " · ".join(flags) if flags else "")
    tail = join.emitted[-SHOW_CHARS:]
    head = join.continuation[:SHOW_CHARS]
    body = f"...{tail}{CUT_MARK}{head}..."
    fence = "`" * max(4, _longest_backtick_run(body) + 1)
    return [header, "", f"{fence}text", body, fence, ""]


def _longest_backtick_run(text: str) -> int:
    return max((len(run) for run in re.findall(r"`+", text)), default=0)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_labels(path: Path, joins: list[Join]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["item_id", "pair", "prompt_id", "strategy", "seamless_y_n", "notes"])
        for join in sorted(joins, key=lambda j: j.item_id):
            if join.error:
                continue
            writer.writerow([join.item_id, join.pair, join.prompt_id, join.strategy, "", ""])
