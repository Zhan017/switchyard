"""switchyard-chaos: run outage scenarios against an OpenAI-compatible gateway.

    switchyard-chaos run chaos/scenarios/s1.yaml
    switchyard-chaos run chaos/scenarios/*.yaml --gateway http://localhost:4000 --no-admin
    switchyard-chaos table docs/results/scenarios/*/

Exit codes, shared with Dastar: 0 pass, 1 fail or invalid run, 2 usage error,
3 inconclusive.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
from pydantic import ValidationError

from chaos import report
from chaos.runner import SeedRun, Target, result_rows, run_seed
from chaos.scenario import Scenario, load

EXIT = {"pass": 0, "fail": 1, "invalid": 1, "inconclusive": 3, "n/a": 3}
DEFAULT_UPSTREAMS = {"primary": "http://localhost:8101", "secondary": "http://localhost:8102"}


class UsageError(Exception):
    pass


def environment(gateway: str) -> dict[str, Any]:
    def git(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args], capture_output=True, text=True, check=True, timeout=5
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return "unknown"

    dirty = git("status", "--porcelain", "--untracked-files=no")
    commit = git("rev-parse", "--short", "HEAD")
    return {
        "date": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "gateway": gateway,
        "commit": commit + (" with local changes" if dirty and dirty != "unknown" else ""),
        "host": f"{platform.system()} {platform.machine()}, {os.cpu_count()} CPUs",
        "python": platform.python_version(),
    }


def parse_upstreams(values: list[str]) -> dict[str, str]:
    upstreams = dict(DEFAULT_UPSTREAMS)
    for value in values:
        name, sep, url = value.partition("=")
        if not sep or not url:
            raise UsageError(f"--upstream takes NAME=URL, got {value!r}")
        upstreams[name] = url.rstrip("/")
    return upstreams


async def run_scenario(
    scenario: Scenario, target: Target, seeds: list[int], out: Path, quiet: bool = False
) -> dict[str, Any]:
    missing = [u for u in scenario.upstreams if u not in target.upstreams]
    if missing:
        raise UsageError(f"{scenario.id} needs a control URL for: {', '.join(missing)}")
    runs: list[SeedRun] = []
    for seed in seeds:
        if not quiet:
            print(f"{scenario.id} seed {seed}: {scenario.traffic.requests} requests", flush=True)
        runs.append(await run_seed(scenario, target, seed))
    summary = report.evaluate(scenario, runs)
    env = environment(target.gateway)
    summary["environment"] = env
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_bytes(orjson.dumps(summary, option=orjson.OPT_INDENT_2))
    (out / "report.md").write_text(report.markdown(summary, env))
    with gzip.open(out / "requests.jsonl.gz", "wb") as f:
        for run in runs:
            for row in result_rows(run):
                f.write(orjson.dumps(row) + b"\n")
    with gzip.open(out / "breakers.jsonl.gz", "wb") as f:
        for run in runs:
            for at, states in run.breakers:
                f.write(orjson.dumps({"seed": run.seed, "at_s": at, "states": states}) + b"\n")
    if not quiet:
        print(f"{scenario.id}: {summary['verdict']} -> {out}", flush=True)
        for check in summary["checks"]:
            print(f"  {check['verdict']:>12}  {check['description']}: {check['measured']}")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="switchyard-chaos", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run scenario files")
    run.add_argument("scenarios", nargs="+", type=Path)
    run.add_argument("--gateway", default="http://localhost:8000")
    run.add_argument("--key", default=os.environ.get("SWITCHYARD_KEY", "sy-dev-local-key"))
    run.add_argument(
        "--admin-key", default=os.environ.get("SWITCHYARD_ADMIN_KEY", "sy-admin-local-key")
    )
    run.add_argument(
        "--no-admin", action="store_true", help="not Switchyard: skip the breaker checks"
    )
    run.add_argument("--upstream", action="append", default=[], metavar="NAME=CONTROL_URL")
    run.add_argument("--seeds", help="comma-separated, instead of the scenario's seeds")
    run.add_argument("--out", type=Path, help="results folder (one scenario only)")
    tab = sub.add_parser("table", help="one Markdown table from several results folders")
    tab.add_argument("folders", nargs="+", type=Path)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return 2 if exit_.code else 0

    try:
        if args.command == "table":
            summaries = [orjson.loads((f / "summary.json").read_bytes()) for f in args.folders]
            sys.stdout.write(report.table(summaries))
            return 0
        scenarios = [load(path) for path in args.scenarios]
        if args.out and len(scenarios) > 1:
            raise UsageError("--out takes one scenario at a time")
        target = Target(
            gateway=args.gateway.rstrip("/"),
            upstreams=parse_upstreams(args.upstream),
            key=args.key,
            admin_key=None if args.no_admin else args.admin_key,
        )
        worst = 0
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        for scenario in scenarios:
            seeds = [int(s) for s in args.seeds.split(",")] if args.seeds else scenario.seeds
            out = args.out or Path("docs/results/scenarios") / f"{scenario.id.lower()}-{stamp}"
            summary = asyncio.run(run_scenario(scenario, target, seeds, out))
            code = EXIT[summary["verdict"]]
            # Fail beats inconclusive beats pass.
            worst = max(worst, code, key=lambda c: {0: 0, 3: 1, 1: 2}[c])
        return worst
    except (UsageError, ValidationError, FileNotFoundError, ValueError) as error:
        print(f"switchyard-chaos: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
