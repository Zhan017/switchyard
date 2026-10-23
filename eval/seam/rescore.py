"""Re-apply the current seam repair to a finished run, without any API calls.

Seam repair only post-processes text, so its rules can be improved against the joins a
run already collected. This writes a fresh report next to the original and lists every
join whose repaired text changed.

    uv run python -m eval.seam.rescore eval/seam/results/<run>
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from eval.seam.metrics import repeated_span
from eval.seam.records import Join, Prompt, Reference
from eval.seam.report import write_outputs
from switchyard.streaming.seam import has_preface, is_meta_reply, repair

HERE = Path(__file__).parent


def _load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def rescore(run: Path) -> list[tuple[Join, str]]:
    prompts = [Prompt(**item) for item in yaml.safe_load((HERE / "prompts.yaml").read_text())]
    prompt_text = {p.id: p.text for p in prompts}
    references = [Reference(**row) for row in _load(run / "references.jsonl")]
    joins = [
        Join(**{k: v for k, v in row.items() if k != "pair"}) for row in _load(run / "joins.jsonl")
    ]
    meta: dict[str, Any] = json.loads((run / "summary.json").read_text())["meta"]

    changed: list[tuple[Join, str]] = []
    for join in joins:
        if join.error:
            continue
        before = join.continuation
        if join.strategy in ("I+S", "A"):
            fixed = repair(
                join.emitted,
                join.continuation_raw,
                context=prompt_text[join.prompt_id],
                anchored=join.strategy == "A",
            )
            join.continuation, join.actions = fixed.text, list(fixed.actions)
        join.repeated_text = repeated_span(join.emitted, join.continuation)
        join.preface = has_preface(join.continuation)
        join.meta_reply = is_meta_reply(join.continuation_raw)
        if join.continuation != before:
            changed.append((join, before))

    meta["rescored"] = datetime.now(UTC).isoformat(timespec="seconds")
    used_ids = {j.prompt_id for j in joins}
    used = [p for p in prompts if p.id in used_ids]
    write_outputs(run / "rescored", meta, used, references, joins)
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-apply seam repair to a finished run.")
    parser.add_argument("run", type=Path, help="a results folder")
    args = parser.parse_args()
    changed = rescore(args.run)
    for join, before in changed:
        print(f"{join.item_id}: {', '.join(join.actions) or 'none'}")
        print(f"  was: {before[:100]!r}")
        print(f"  now: {join.continuation[:100]!r}")
    print(f"{len(changed)} joins changed. Report: {args.run / 'rescored' / 'report.md'}")


if __name__ == "__main__":
    main()
