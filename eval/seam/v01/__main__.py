"""The v0.1 seam evaluation (SPEC 12.3), stage by stage. Every stage is resumable: run it
again and it picks up what is left. Results go to eval/seam/results/v0.1/.

    uv run python -m eval.seam.v01 generate            # references and stitched answers
    uv run python -m eval.seam.v01 sample              # draw the 50 items and write label.html
    (label them, save labels.json into results/v0.1/labels/)
    uv run python -m eval.seam.v01 judge               # model judges; needs the labels first
    uv run python -m eval.seam.v01 report              # report.md and summary.json

    uv run python -m eval.seam.v01 dry-run             # every stage on fake models, no keys

Exit codes: 0 done, 1 done with items left to retry, 2 refused or stopped.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from pathlib import Path

from eval.seam.records import Prompt
from eval.seam.spike import load_dotenv
from eval.seam.v01 import analyze, generate, judge, labels
from eval.seam.v01.budget import Budget, Prices
from eval.seam.v01.client import Fatal, http_client
from eval.seam.v01.plan import ROOT, Plan, load_plan, load_prompts
from eval.seam.v01.records import FINAL, Judgment, Reference, Stitched, load, spent_by_provider
from eval.seam.v01.stack import stack
from eval.seam.v01.stage import Stage, log_run

DEFAULT_OUT = ROOT / "eval" / "seam" / "results" / "v0.1"
KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY")


def _records(out: Path) -> tuple[dict[str, Reference], dict[str, Stitched], dict[str, Judgment]]:
    return (
        load(generate.references_path(out), Reference),
        load(generate.stitched_path(out), Stitched),
        load(judge.judgments_path(out), Judgment),
    )


def _caps(plan: Plan, overrides: list[str]) -> dict[str, float]:
    caps = dict(plan.budget_usd)
    for item in overrides:
        provider, _, value = item.partition("=")
        if provider not in caps or not value:
            raise SystemExit(f"--cap takes provider=usd with a provider from the plan: {item}")
        caps[provider] = float(value)
    return caps


def _keys(dry_run: bool) -> None:
    if dry_run:
        return
    load_dotenv(ROOT / ".env")
    missing = [k for k in KEYS if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"Missing API keys: {', '.join(missing)}. Add them to .env.")


def _scope(plan: Plan, prompts: list[Prompt], items: dict[str, Stitched]) -> list[Stitched]:
    """Stitched items for the prompts in scope (all, or the first --limit)."""
    ids = {p.id for p in prompts}
    return [i for i in items.values() if i.prompt_id in ids]


async def _generate(plan: Plan, prompts: list[Prompt], out: Path, args: argparse.Namespace) -> int:
    refs, stitched, judged = _records(out)
    budget = Budget(
        _caps(plan, args.cap), spent_by_provider(refs.values(), stitched.values(), judged.values())
    )
    with stack(plan, out, args.dry_run) as (url, upstream, config):
        prices = Prices(config.parent / "gateway-prices.yaml")
        async with http_client(url) as http:
            stage = Stage("references", plan, out, http, budget, prices, args.dry_run)
            refs = await generate.references(stage, prompts, args.allow_peak)
            second = Stage("continuations", plan, out, http, budget, prices, args.dry_run)
            stitched = await generate.continuations(
                second, prompts, refs, upstream, args.allow_peak
            )
            skipped = stage.skipped + second.skipped
    items = _scope(plan, prompts, stitched)
    expected = len(plan.items(prompts))
    outcomes = {o: sum(i.outcome == o for i in items) for o in ("ok", "meta_reply", "error")}
    print(
        f"generate: {expected} items planned; {outcomes['ok']} continued, "
        f"{outcomes['meta_reply']} refused as meta replies, {outcomes['error']} failed, "
        f"{expected - len(items)} not started"
    )
    log_run(
        out,
        {
            "stage": "generate",
            "dry_run": args.dry_run,
            "caps": budget.caps,
            "spent": budget.spent,
            "outcomes": outcomes,
            "skipped": skipped,
        },
    )
    finished = outcomes["ok"] + outcomes["meta_reply"] == expected
    return 0 if finished else 1


def cmd_generate(args: argparse.Namespace) -> int:
    _keys(args.dry_run)
    plan, prompts = load_plan(), load_prompts(limit=args.limit)
    return asyncio.run(_generate(plan, prompts, args.out, args))


def cmd_sample(args: argparse.Namespace) -> int:
    plan, prompts = load_plan(), load_prompts(limit=args.limit)
    refs, stitched, _ = _records(args.out)
    items = _scope(plan, prompts, stitched)
    unfinished = len(plan.items(prompts)) - sum(i.outcome in FINAL for i in items)
    if unfinished and not args.force:
        raise SystemExit(
            f"{unfinished} items are not finished. Run generate again first, or pass --force "
            "to draw from what there is."
        )
    key = labels.labels_dir(args.out) / "key.json"
    if key.exists() and not args.force:
        raise SystemExit("The items are drawn already; drawing again would not be blind.")
    drawn = labels.sample(plan, items)
    page = labels.export(plan, args.out, drawn, refs, prompts)
    log_run(args.out, {"stage": "sample", "items": len(drawn)})
    print(f"sample: {len(drawn)} items. Label them in {page}")
    print(f"then save labels.json to {page.parent / 'labels.json'}")
    return 0


async def _judge(plan: Plan, prompts: list[Prompt], out: Path, args: argparse.Namespace) -> int:
    refs, stitched, judged = _records(out)
    items = [i for i in _scope(plan, prompts, stitched) if i.outcome == "ok"]
    labelled = analyze.labelled_ids(out)
    budget = Budget(
        _caps(plan, args.cap), spent_by_provider(refs.values(), stitched.values(), judged.values())
    )
    with stack(plan, out, args.dry_run) as (url, _, config):
        prices = Prices(config.parent / "gateway-prices.yaml")
        async with http_client(url) as http:
            stage = Stage("judges", plan, out, http, budget, prices, args.dry_run)
            found = await judge.run_judges(stage, prompts, refs, items, labelled, args.allow_peak)
    wanted = judge.tasks(plan, items, labelled)
    done = sum(1 for t in wanted if (j := found.get(t.key)) is not None and j.outcome == "ok")
    print(f"judge: {done} of {len(wanted)} judgments done")
    log_run(
        out,
        {
            "stage": "judge",
            "dry_run": args.dry_run,
            "caps": budget.caps,
            "spent": budget.spent,
            "done": done,
            "wanted": len(wanted),
        },
    )
    return 0 if done == len(wanted) else 1


def cmd_judge(args: argparse.Namespace) -> int:
    _keys(args.dry_run)
    plan, prompts = load_plan(), load_prompts(limit=args.limit)
    if not args.dry_run:
        labels.load_labels(args.out)  # the human labels come first, blind to every verdict
    return asyncio.run(_judge(plan, prompts, args.out, args))


def cmd_report(args: argparse.Namespace) -> int:
    plan, prompts = load_plan(), load_prompts(limit=args.limit)
    refs, stitched, judged = _records(args.out)
    spent = spent_by_provider(refs.values(), stitched.values(), judged.values())
    in_scope = {i.item_id: i for i in _scope(plan, prompts, stitched)}
    analyze.analyze(plan, args.out, prompts, refs, in_scope, judged, spent, args.dry_run)
    print(f"report: {args.out / 'report.md'}")
    return 0


def cmd_dry_run(args: argparse.Namespace) -> int:
    """Every stage on fake models, with random labels."""
    args.dry_run, args.force = True, True
    for step in (cmd_generate, cmd_sample):
        if step(args) == 2:
            return 2
    key = labels.load_key(args.out)
    rng = random.Random(0)
    fake = {k["id"]: rng.choice(labels.CHOICES) for k in key}
    (labels.labels_dir(args.out) / "labels.json").write_text(json.dumps({"labels": fake}))
    if cmd_judge(args) == 2:
        return 2
    return cmd_report(args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.seam.v01", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, func in (
        ("generate", cmd_generate),
        ("sample", cmd_sample),
        ("judge", cmd_judge),
        ("report", cmd_report),
        ("dry-run", cmd_dry_run),
    ):
        command = commands.add_parser(name)
        command.set_defaults(func=func)
        dry = name == "dry-run"
        default = ROOT / "eval" / "seam" / "results" / "v0.1-dry" if dry else DEFAULT_OUT
        command.add_argument("--out", type=Path, default=default)
        command.add_argument(
            "--limit", type=int, default=3 if dry else None, help="only the first N prompts"
        )
        command.add_argument("--dry-run", action="store_true", help="fake models, no keys")
        command.add_argument("--cap", action="append", default=[], help="provider=usd")
        command.add_argument("--allow-peak", action="store_true", help="run at DeepSeek's peak")
        command.add_argument("--force", action="store_true", help="sample: draw anyway")
    args = parser.parse_args(argv)
    try:
        code: int = args.func(args)
    except Fatal as error:
        print(f"Stopped: {error}", file=sys.stderr)
        return 2
    except SystemExit as stop:
        if isinstance(stop.code, str):
            print(stop.code, file=sys.stderr)
            return 2
        raise
    return code


if __name__ == "__main__":
    sys.exit(main())
