"""Week 1 seam test: how well does another model finish a broken answer?

For each prompt, a primary model writes a full answer. The answer is cut at a seeded
random point between 20% and 80% of its length, as if the stream had died there. A
fallback model then finishes it with each strategy:

    R    restart: the fallback answers again from the top
    I    instructed: the fallback is told the answer was cut off and asked to continue
    I+S  the same continuation as I, after seam repair
    N    native: DeepSeek's beta prefix completion continues the partial answer

Real run (needs keys in .env):  uv run python -m eval.seam.spike
Offline dry run:                uv run python -m eval.seam.spike --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from eval.seam.clients import PROVIDERS, Client, Completion, Message, Price, ProviderError
from eval.seam.fake import FakeClient
from eval.seam.metrics import approx_tokens, repeated_span
from eval.seam.records import Join, Prompt, Reference
from eval.seam.report import write_outputs
from switchyard.streaming.seam import has_preface, repair

HERE = Path(__file__).parent
INSTRUCTION = (
    "Your previous message was cut off. Continue from exactly where it stopped. "
    "The last characters were: «{tail}». Output only the continuation."
)
# A rough stand-in for token boundaries: a word with its leading space, long words split
# in two, punctuation on its own. Streams die between tokens, so cuts land on these.
_PIECE = re.compile(r"\s*(?:\w{1,8}|[^\w\s])")


@dataclass
class Model:
    family: str
    provider: str
    model: str
    price: Price


@dataclass
class Settings:
    seed: int
    max_tokens: int
    concurrency: int
    budget_usd: float
    tail_chars: int
    models: dict[str, Model]
    pairs: list[tuple[str, str]]


def load_settings(path: Path) -> Settings:
    raw = yaml.safe_load(path.read_text())
    models = {
        family: Model(family, spec["provider"], spec["model"], Price(**spec["price"]))
        for family, spec in raw["models"].items()
    }
    pairs = [(p, f) for p, f in raw["pairs"]]
    for primary, fallback in pairs:
        for family in (primary, fallback):
            if family not in models:
                raise SystemExit(f"config: pair uses unknown model family '{family}'")
    return Settings(
        seed=int(raw["seed"]),
        max_tokens=int(raw["max_tokens"]),
        concurrency=int(raw["concurrency"]),
        budget_usd=float(raw["budget_usd"]),
        tail_chars=int(raw["tail_chars_in_instruction"]),
        models=models,
        pairs=pairs,
    )


def load_prompts(path: Path, limit: int | None) -> list[Prompt]:
    prompts = [Prompt(**item) for item in yaml.safe_load(path.read_text())]
    return prompts[:limit] if limit else prompts


def load_dotenv(path: Path) -> None:
    """Read KEY=VALUE lines into the environment without overriding what is already set."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def cut_point(text: str, rng: random.Random) -> int:
    """A token-like boundary between 20% and 80% of the text."""
    ends = [m.end() for m in _PIECE.finditer(text)]
    low, high = 0.2 * len(text), 0.8 * len(text)
    candidates = [end for end in ends if low <= end <= high]
    return rng.choice(candidates) if candidates else len(text) // 2


def strategies_for(client: Client) -> list[str]:
    return ["R", "I", "I+S", *(["N"] if client.supports_prefix else [])]


def estimate_cost(settings: Settings, prompts: list[Prompt]) -> float:
    """Worst case: every call uses its full max_tokens."""
    out = settings.max_tokens
    emitted = int(0.8 * out)
    total = 0.0
    primaries = {primary for primary, _ in settings.pairs}
    for prompt in prompts:
        prompt_tokens = approx_tokens(prompt.text)
        for family in primaries:
            price = settings.models[family].price
            total += (prompt_tokens * price.input + out * price.output) / 1e6
        for _, fallback in settings.pairs:
            price = settings.models[fallback].price
            calls = 3 if settings.models[fallback].provider == "deepseek" else 2
            continued_input = prompt_tokens + emitted + 120
            total += (prompt_tokens * price.input + out * price.output) / 1e6  # R
            total += (calls - 1) * (continued_input * price.input + out * price.output) / 1e6
    return total


class Runner:
    def __init__(self, settings: Settings, clients: dict[str, Client]) -> None:
        self.settings = settings
        self.clients = clients
        self.limits = {family: asyncio.Semaphore(settings.concurrency) for family in clients}
        self.done = 0
        self.total = 0

    async def call(
        self, family: str, messages: list[Message], max_tokens: int, prefix: bool = False
    ) -> Completion:
        async with self.limits[family]:
            try:
                return await self.clients[family].complete(messages, max_tokens, prefix=prefix)
            finally:
                self.done += 1
                if self.done % 10 == 0 or self.done == self.total:
                    print(f"  {self.done}/{self.total} calls", file=sys.stderr)

    async def reference(self, prompt: Prompt, family: str) -> Reference:
        model = self.settings.models[family]
        ref = Reference(prompt.id, family, self.clients[family].model)
        try:
            done = await self.call(family, [user(prompt.text)], self.settings.max_tokens)
        except ProviderError as error:
            ref.error = str(error)
            return ref
        ref.text, ref.finish_reason = done.text, done.finish_reason
        ref.cost_usd = done.cost(model.price)
        return ref

    async def continue_answer(
        self, prompt: Prompt, ref: Reference, fallback: str, cut_fraction: float, emitted: str
    ) -> list[Join]:
        client = self.clients[fallback]
        remaining = max(256, self.settings.max_tokens - approx_tokens(emitted))
        tail = emitted[-self.settings.tail_chars :]

        def join(strategy: str) -> Join:
            return Join(
                item_id=f"{prompt.id}-{ref.family}-{fallback}-{strategy}",
                prompt_id=prompt.id,
                kind=prompt.kind,
                primary=ref.family,
                fallback=fallback,
                strategy=strategy,
                cut_fraction=round(cut_fraction, 3),
                emitted=emitted,
            )

        restart = self.call(fallback, [user(prompt.text)], self.settings.max_tokens)
        instructed = self.call(
            fallback,
            [user(prompt.text), assistant(emitted), user(INSTRUCTION.format(tail=tail))],
            remaining,
        )
        calls: dict[str, Any] = {"R": restart, "I": instructed}
        if client.supports_prefix:
            calls["N"] = self.call(
                fallback, [user(prompt.text), assistant(emitted)], remaining, prefix=True
            )
        results = await asyncio.gather(*calls.values(), return_exceptions=True)
        by_strategy = dict(zip(calls.keys(), results, strict=True))

        joins: list[Join] = []
        price = self.settings.models[fallback].price
        for strategy in strategies_for(client):
            item = join(strategy)
            source = by_strategy["I" if strategy == "I+S" else strategy]
            if isinstance(source, BaseException):
                item.error = f"{type(source).__name__}: {source}"
                joins.append(item)
                continue
            assert isinstance(source, Completion)
            item.continuation_raw = source.text
            if strategy == "I+S":
                fixed = repair(emitted, source.text, context=prompt.text)
                item.continuation, item.actions = fixed.text, list(fixed.actions)
            else:
                item.continuation = source.text
            item.repeated_text = repeated_span(emitted, item.continuation)
            item.preface = has_preface(item.continuation)
            item.input_tokens, item.output_tokens = source.input_tokens, source.output_tokens
            # I and I+S share one call, so its cost is counted once, on I.
            item.cost_usd = 0.0 if strategy == "I+S" else source.cost(price)
            item.latency_s, item.finish_reason = source.latency_s, source.finish_reason
            joins.append(item)
        return joins


def user(text: str) -> Message:
    return {"role": "user", "content": text}


def assistant(text: str) -> Message:
    return {"role": "assistant", "content": text}


async def run(
    settings: Settings, prompts: list[Prompt], clients: dict[str, Client]
) -> tuple[list[Reference], list[Join]]:
    runner = Runner(settings, clients)
    primaries = sorted({primary for primary, _ in settings.pairs})
    continued_calls = sum(
        2 + int(clients[fallback].supports_prefix) for _, fallback in settings.pairs
    )
    runner.total = len(prompts) * (len(primaries) + continued_calls)

    ref_tasks = [runner.reference(p, family) for p in prompts for family in primaries]
    references = await asyncio.gather(*ref_tasks)
    by_key = {(r.prompt_id, r.family): r for r in references}

    join_tasks = []
    for prompt in prompts:
        for primary, fallback in settings.pairs:
            ref = by_key[(prompt.id, primary)]
            if ref.error or not ref.text.strip():
                continue
            # The same cut for every fallback of one reference, so fallbacks are compared
            # on identical joins.
            rng = random.Random(f"{settings.seed}:{prompt.id}:{primary}")
            cut = cut_point(ref.text, rng)
            join_tasks.append(
                runner.continue_answer(prompt, ref, fallback, cut / len(ref.text), ref.text[:cut])
            )
    joins = [join for group in await asyncio.gather(*join_tasks) for join in group]
    return references, joins


def build_clients(settings: Settings, http: httpx.AsyncClient, dry_run: bool) -> dict[str, Client]:
    used = {family for pair in settings.pairs for family in pair}
    clients: dict[str, Client] = {}
    missing = []
    for family in sorted(used):
        model = settings.models[family]
        if model.provider not in PROVIDERS:
            raise SystemExit(f"config: unknown provider '{model.provider}'")
        cls, key_name = PROVIDERS[model.provider]
        if dry_run:
            clients[family] = FakeClient(family, model.model, cls.supports_prefix)
            continue
        key = os.environ.get(key_name, "")
        if not key:
            missing.append(key_name)
            continue
        clients[family] = cls(http, key, model.model, family)
    if missing:
        raise SystemExit(f"Missing API keys: {', '.join(missing)}. Add them to .env.")
    return clients


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="use a fake provider, no keys")
    parser.add_argument("--limit", type=int, help="only the first N prompts")
    parser.add_argument("--budget", type=float, help="override budget_usd from the config")
    parser.add_argument("--config", type=Path, default=HERE / "config.yaml")
    parser.add_argument("--out", type=Path, help="output folder")
    args = parser.parse_args()

    load_dotenv(Path.cwd() / ".env")
    settings = load_settings(args.config)
    prompts = load_prompts(HERE / "prompts.yaml", args.limit)
    budget = args.budget if args.budget is not None else settings.budget_usd

    estimate = estimate_cost(settings, prompts)
    print(f"Worst-case cost estimate: ${estimate:.2f} (budget ${budget:.2f})")
    if not args.dry_run and estimate > budget:
        raise SystemExit("Estimate is over budget. Lower --limit or raise --budget.")

    started = datetime.now(UTC)
    stamp = started.strftime("%Y%m%d-%H%M%S") + ("-dry" if args.dry_run else "")
    out = args.out or HERE / "results" / stamp

    async def go() -> tuple[list[Reference], list[Join], dict[str, str]]:
        timeout = httpx.Timeout(120.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as http:
            clients = build_clients(settings, http, args.dry_run)
            models = {family: client.model for family, client in clients.items()}
            references, joins = await run(settings, prompts, clients)
            return references, joins, models

    references, joins, models = asyncio.run(go())
    meta: dict[str, Any] = {
        "started": started.isoformat(timespec="seconds"),
        "dry_run": args.dry_run,
        "seed": settings.seed,
        "prompts": len(prompts),
        "pairs": [f"{p}-{f}" for p, f in settings.pairs],
        "models": models,
        "estimate_usd": round(estimate, 4),
    }
    summary = write_outputs(out, meta, prompts, references, joins)
    print(json.dumps(summary["totals"], indent=2))
    print(f"Report: {out / 'report.md'}")


if __name__ == "__main__":
    main()
