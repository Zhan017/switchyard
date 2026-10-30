"""Stage 1: reference answers, then every stitched answer, all through the gateway."""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from pathlib import Path

from eval.seam.metrics import approx_tokens, repeated_span
from eval.seam.records import Prompt
from eval.seam.spike import cut_point
from eval.seam.v01.budget import Estimate, OverBudget
from eval.seam.v01.client import TRANSIENT, StreamRead, complete, stream
from eval.seam.v01.plan import STRATEGY_CODES, Plan, item_id
from eval.seam.v01.records import FINAL, Reference, Stitched, append, load
from eval.seam.v01.stack import Upstream, route_cont, route_ref
from eval.seam.v01.stage import Stage, check_estimate, run_all
from switchyard.streaming.continuation import ANCHOR_EXTRA_TOKENS
from switchyard.streaming.seam import has_preface, is_meta_reply

INSTRUCTION_TOKENS = 80  # the continuation instruction or the anchored wrapper


def references_path(out: Path) -> Path:
    return out / "references.jsonl"


def stitched_path(out: Path) -> Path:
    return out / "stitched.jsonl"


def _model(plan: Plan, family: str) -> str:
    return plan.families[family].partition("/")[2]


def _ref_key(plan: Plan, family: str) -> str:
    return f"{family}/{_model(plan, family)}"


def _cont_key(plan: Plan, fallback: str, strategy: str) -> str:
    return f"{fallback}-{STRATEGY_CODES[strategy]}/{_model(plan, fallback)}"


async def references(stage: Stage, prompts: list[Prompt], allow_peak: bool) -> dict[str, Reference]:
    plan, path = stage.plan, references_path(stage.out)
    found = load(path, Reference)
    todo = [
        (prompt, family)
        for prompt in prompts
        for family in plan.primaries
        if (r := found.get(f"{prompt.id}-{family}")) is None or r.outcome not in FINAL
    ]
    estimate = Estimate()
    for prompt, family in todo:
        key, tokens_in = _ref_key(plan, family), approx_tokens(prompt.text) + 20
        estimate.add(
            plan.provider_type(family),
            stage.prices.cost(key, tokens_in, 900),
            stage.prices.cost(key, tokens_in, plan.max_tokens),
        )
    check_estimate(stage, estimate, allow_peak, [])

    def job(prompt: Prompt, family: str) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            ref = await _reference(stage, prompt, family)
            if ref is not None:
                append(path, [ref])
                found[ref.key] = ref
            stage.progress()

        return run

    await run_all(stage, [job(p, f) for p, f in todo])
    return found


async def _reference(stage: Stage, prompt: Prompt, family: str) -> Reference | None:
    plan = stage.plan
    provider = plan.provider_type(family)
    ref = Reference(prompt.id, family, plan.families[family])
    worst = stage.prices.cost(
        _ref_key(plan, family), approx_tokens(prompt.text) + 20, plan.max_tokens
    )
    async with stage.limits[provider]:
        for attempt in range(plan.tries):
            try:
                async with stage.budget.hold(provider, worst):
                    reply = await complete(
                        stage.http, route_ref(family), prompt.text, plan.max_tokens
                    )
            except OverBudget:
                if not ref.tries:
                    raise
                return ref  # keep what the failed tries cost
            ref.tries += 1
            ref.spent_usd += reply.cost
            stage.budget.add(provider, reply.cost)
            if reply.status == 200 and reply.text.strip():
                ref.outcome, ref.error = "ok", None
                ref.text, ref.finish_reason = reply.text, reply.finish_reason
                ref.input_tokens = int(reply.usage.get("prompt_tokens") or 0)
                ref.output_tokens = int(reply.usage.get("completion_tokens") or 0)
                ref.cost_usd = reply.cost
                # The same cut for every pair and strategy from this reference.
                rng = random.Random(f"{plan.seed}:{prompt.id}:{family}")
                ref.cut = cut_point(ref.text, rng)
                stage.succeeded()
                return ref
            ref.error = reply.error or "empty answer"
            if not reply.retryable:
                stage.hard_failure(ref.error)
                break
            await stage.backoff(attempt)
    return ref


async def continuations(
    stage: Stage,
    prompts: list[Prompt],
    refs: dict[str, Reference],
    upstream: Upstream,
    allow_peak: bool,
) -> dict[str, Stitched]:
    plan, path = stage.plan, stitched_path(stage.out)
    found = load(path, Stitched)
    texts = {p.id: p.text for p in prompts}
    for done_ref in refs.values():
        if done_ref.outcome == "ok" and done_ref.prompt_id in texts:
            upstream.register(
                done_ref.family, texts[done_ref.prompt_id], done_ref.text, done_ref.cut
            )
    todo = []
    estimate = Estimate()
    peak_keys: list[str] = []
    for prompt, primary, fallback, strategy in plan.items(prompts):
        ref = refs.get(f"{prompt.id}-{primary}")
        done = found.get(item_id(prompt.id, primary, fallback, strategy))
        if ref is None or ref.outcome != "ok" or (done is not None and done.outcome in FINAL):
            continue
        todo.append((prompt, primary, fallback, strategy, ref))
        key = _cont_key(plan, fallback, strategy)
        tokens_in = approx_tokens(prompt.text) + approx_tokens(ref.text[: ref.cut])
        tokens_in += INSTRUCTION_TOKENS
        rest = ref.text if strategy == "R" else ref.text[ref.cut :]
        estimate.add(
            plan.provider_type(fallback),
            stage.prices.cost(key, tokens_in, approx_tokens(rest)),
            stage.prices.cost(key, tokens_in, plan.max_tokens + ANCHOR_EXTRA_TOKENS),
        )
        if plan.provider_type(fallback) == "deepseek":
            peak_keys.append(key)
    missing = sum(1 for r in refs.values() if r.outcome != "ok")
    if missing:
        print(f"continuations: {missing} references failed; their items are left out for now")
    check_estimate(stage, estimate, allow_peak, peak_keys[:1])

    def job(
        prompt: Prompt, primary: str, fallback: str, strategy: str, ref: Reference
    ) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            item = await _stitch(stage, prompt, primary, fallback, strategy, ref)
            if item is not None:
                append(path, [item])
                found[item.key] = item
            stage.progress()

        return run

    await run_all(stage, [job(*args) for args in todo])
    return found


async def _stitch(
    stage: Stage, prompt: Prompt, primary: str, fallback: str, strategy: str, ref: Reference
) -> Stitched | None:
    plan = stage.plan
    provider = plan.provider_type(fallback)
    emitted = ref.text[: ref.cut]
    item = Stitched(
        item_id=item_id(prompt.id, primary, fallback, strategy),
        prompt_id=prompt.id,
        kind=prompt.kind,
        primary=primary,
        fallback=fallback,
        strategy=strategy,
        provider=provider,
        cut=ref.cut,
        cut_fraction=round(ref.cut / len(ref.text), 3),
    )
    tokens_in = approx_tokens(prompt.text) + approx_tokens(emitted) + INSTRUCTION_TOKENS
    worst = stage.prices.cost(
        _cont_key(plan, fallback, strategy), tokens_in, plan.max_tokens + ANCHOR_EXTRA_TOKENS
    )
    route = route_cont(primary, fallback, strategy)
    async with stage.limits[provider]:
        for attempt in range(plan.tries):
            try:
                async with stage.budget.hold(provider, worst):
                    read = await stream(stage.http, route, prompt.text, plan.max_tokens)
            except OverBudget:
                if not item.tries:
                    raise
                return item  # keep what the failed tries cost
            item.tries += 1
            item.spent_usd += read.cost
            stage.budget.add(provider, read.cost)
            verdict, retry = _classify(read, emitted)
            _fill(item, read, verdict)
            if verdict in FINAL:
                stage.succeeded()
                return item
            item.retried_for.append(verdict)
            if not retry:
                stage.hard_failure(item.error or verdict)
                break
            await stage.backoff(attempt)
    return item


def _classify(read: StreamRead, emitted: str) -> tuple[str, bool]:
    """The item's outcome, and whether trying again could change it."""
    if read.status != 200:
        return f"http_{read.status}", read.status in (408, 429, 500, 502, 503, 504, 529, 599)
    if read.failover is None:
        if read.error is None:
            return "not_cut", False  # the fake primary didn't cut: a bug in the harness
        return "error_before_cut", True
    if read.before != emitted:
        return "emitted_differs", False  # the client got something other than the cut text
    outcomes = [str(t.get("outcome")) for t in read.midstream]
    if read.error is None:
        return "ok", False
    if "meta_reply" in outcomes:
        return "meta_reply", False
    if any(o in ("client_error", "auth") for o in outcomes):
        return f"continuation_{outcomes[-1]}", False
    reason = outcomes[-1] if outcomes else (read.summary.get("not_continued") or "unknown")
    return f"continuation_{reason}", reason in TRANSIENT or reason == "no_candidate"


def _fill(item: Stitched, read: StreamRead, verdict: str) -> None:
    item.outcome = verdict if verdict in ("ok", "meta_reply") else "error"
    item.error = None if verdict in ("ok", "meta_reply") else (read.error or verdict)
    item.not_continued = read.summary.get("not_continued")
    midstream = read.midstream
    item.input_tokens = sum(int(t.get("input_tokens") or 0) for t in midstream)
    item.output_tokens = sum(int(t.get("output_tokens") or 0) for t in midstream)
    item.cost_usd = sum(float(t.get("cost_usd") or 0.0) for t in midstream)
    if verdict != "ok":
        return
    seam = read.summary.get("seam") or {}
    item.continuation = read.after
    item.served_model = read.summary.get("served_model")
    item.gateway_strategy = read.summary.get("strategy")
    item.seam_actions = list(seam.get("actions") or [])
    item.seam_buffer_ms = seam.get("latency_ms")
    item.seam_ms = round(read.seam_ms, 1) if read.seam_ms is not None else None
    item.heartbeats = read.heartbeats
    item.finish_reason = read.finish_reason
    item.repeated_text = repeated_span(read.before, read.after)
    item.preface = has_preface(read.after)
    item.meta_reply = is_meta_reply(read.after)
