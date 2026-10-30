"""Stage 3: model judges. Each sees the request and two texts, the reference and the
stitched answer, in both orders, and says which one was continued by a different writer.
An item counts as detected only if the judge is right in both orders."""

from __future__ import annotations

import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from eval.seam.metrics import approx_tokens
from eval.seam.records import Prompt
from eval.seam.v01.budget import Estimate, OverBudget
from eval.seam.v01.client import complete
from eval.seam.v01.plan import Judge, Plan
from eval.seam.v01.records import FINAL, Judgment, Reference, Stitched, append, load
from eval.seam.v01.stack import JUDGE_MARKER, route_judge
from eval.seam.v01.stage import Stage, check_estimate, run_all

ORDERS = ("RS", "SR")  # RS: the reference is A. SR: the stitched answer is A.

LOCAL_PROMPT = """Below are a request and two excerpts, each from a different answer to it. \
Both excerpts are taken from the same point in their answers, so they may start the same way.

One answer was written in one go by one writer. The other was interrupted partway through, \
and a different writer continued it from where it stopped.

<request>
{request}
</request>

<excerpt_a>
{a}
</excerpt_a>

<excerpt_b>
{b}
</excerpt_b>

Which excerpt comes from the answer that was interrupted and continued by a different \
writer? Look at how the text flows: a change of voice or formatting, repeated or missing \
words, a broken sentence or a restart. In one or two sentences, say what you noticed, \
then {marker}."""

FULL_PROMPT = """Below are a request and two complete answers to it.

One answer was written in one go by one writer. The other was interrupted partway through, \
and a different writer continued it from where it stopped.

<request>
{request}
</request>

<answer_a>
{a}
</answer_a>

<answer_b>
{b}
</answer_b>

Which answer was interrupted and continued by a different writer? Look at how the text \
flows: a change of voice, style or formatting, repeated or missing words, a broken sentence \
or a restart. In one or two sentences, say what you noticed, then {marker}."""

_ANSWER = re.compile(r"answer\s*[:\uff1a]\s*[*_`\"'(\[]*\s*([AB])\b", re.IGNORECASE)


def judgments_path(out: Path) -> Path:
    return out / "judgments.jsonl"


def local_view(reference: str, stitched: str, cut: int, chars: int) -> tuple[str, str]:
    """``chars`` characters either side of the cut from each text, trimmed to whole
    words. Both start at the same place, since both texts are the same up to the cut."""
    start = max(0, cut - chars)
    if start > 0:
        space = reference.find(" ", start, cut)
        start = space + 1 if space != -1 else start

    def window(text: str) -> str:
        end = min(len(text), cut + chars)
        if end < len(text):
            space = text.rfind(" ", cut, end)
            end = space if space != -1 else end
        body = text[start:end]
        return ("…" if start > 0 else "") + body + ("…" if end < len(text) else "")

    return window(reference), window(stitched)


def judge_prompt(
    view: str, order: str, request: str, reference: str, stitched: str, cut: int, chars: int
) -> str:
    if view == "local":
        ref_text, stitched_text = local_view(reference, stitched, cut, chars)
        template = LOCAL_PROMPT
    else:
        ref_text, stitched_text = reference, stitched
        template = FULL_PROMPT
    a, b = (ref_text, stitched_text) if order == "RS" else (stitched_text, ref_text)
    return template.format(request=request, a=a, b=b, marker=JUDGE_MARKER)


def parse_answer(reply: str) -> str | None:
    found = _ANSWER.findall(reply)
    if found:
        return str(found[-1]).upper()
    bare = reply.strip().strip("*.").upper()
    return bare if bare in ("A", "B") else None


@dataclass(frozen=True)
class Task:
    item: Stitched
    judge: str
    view: str
    order: str

    @property
    def key(self) -> str:
        return f"{self.item.item_id}|{self.judge}|{self.view}|{self.order}"


def tasks(plan: Plan, items: list[Stitched], labelled: set[str]) -> list[Task]:
    """Every judgment the plan asks for, most important first, since calls start in this
    order and a stage stops starting calls at a cap:

    1. every judge on the local view of the labelled items, for the gate;
    2. each item's neutral judge on the local view, for the headline;
    3. Sonnet on the local view of the rest, for the self-preference check;
    4. Sonnet on the full view, in a seeded random order, so that if the money runs out
       the full view covers a random subset.
    """
    ordered = sorted(items, key=lambda i: i.item_id)
    groups: list[list[tuple[Stitched, str, str]]] = [
        [(i, name, "local") for i in ordered if i.item_id in labelled for name in plan.judges],
        [(i, plan.neutral[i.pair], "local") for i in ordered],
        [(i, "sonnet", "local") for i in ordered],
    ]
    shuffled = list(ordered)
    random.Random(plan.seed).shuffle(shuffled)
    groups.append([(i, "sonnet", "full") for i in shuffled])
    wanted: dict[str, Task] = {}
    for group in groups:
        for item, judge, view in group:
            for order in ORDERS:
                task = Task(item, judge, view, order)
                wanted.setdefault(task.key, task)
    return list(wanted.values())


def output_tokens(found: dict[str, Judgment], judge: Judge) -> int:
    """Output tokens per call for the estimate: the measured mean once there are 20
    judgments to go on, else the plan's guess."""
    measured = [j.output_tokens for j in found.values() if j.judge == judge.name and j.tries]
    if len(measured) >= 20:
        return round(sum(measured) / len(measured))
    return judge.expected_output_tokens


async def run_judges(
    stage: Stage,
    prompts: list[Prompt],
    refs: dict[str, Reference],
    items: list[Stitched],
    labelled: set[str],
    allow_peak: bool,
) -> dict[str, Judgment]:
    plan, path = stage.plan, judgments_path(stage.out)
    found = load(path, Judgment)
    by_prompt = {p.id: p for p in prompts}
    todo = [t for t in tasks(plan, items, labelled) if _pending(found.get(t.key))]
    # The full view may stop at the cap, so only the rest must fit to start.
    required, full_view = Estimate(), Estimate()
    peak_keys: list[str] = []
    for task in todo:
        judge = plan.judges[task.judge]
        tokens_in = approx_tokens(_prompt_for(plan, task, by_prompt, refs))
        key = _judge_key(judge)
        (full_view if task.view == "full" else required).add(
            judge.provider_type,
            stage.prices.cost(key, tokens_in, output_tokens(found, judge)),
            stage.prices.cost(key, tokens_in, judge.max_tokens),
        )
        if judge.provider_type == "deepseek" and not peak_keys:
            peak_keys.append(key)
    check_estimate(stage, required, allow_peak, peak_keys)
    for provider, cost in full_view.expected.items():
        left = stage.budget.left(provider) - required.expected.get(provider, 0.0)
        note = "" if cost <= left else "; over what is left, so a random part of it will run"
        print(f"  full view: {full_view.calls[provider]} {provider} calls, about ${cost:.2f}{note}")

    def job(task: Task) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            text = _prompt_for(plan, task, by_prompt, refs)
            judgment = await _judge(stage, task, text)
            if judgment is not None:
                append(path, [judgment])
                found[judgment.key] = judgment
            stage.progress()

        return run

    await run_all(stage, [job(t) for t in todo])
    return found


def _pending(judgment: Judgment | None) -> bool:
    return judgment is None or judgment.outcome not in FINAL


def _judge_key(judge: Judge) -> str:
    return f"judge-{judge.name}/{judge.model.partition('/')[2]}"


def _prompt_for(
    plan: Plan, task: Task, prompts: dict[str, Prompt], refs: dict[str, Reference]
) -> str:
    item = task.item
    ref = refs[f"{item.prompt_id}-{item.primary}"]
    stitched = ref.text[: item.cut] + item.continuation
    return judge_prompt(
        task.view,
        task.order,
        prompts[item.prompt_id].text,
        ref.text,
        stitched,
        item.cut,
        plan.local_chars,
    )


async def _judge(stage: Stage, task: Task, text: str) -> Judgment | None:
    plan = stage.plan
    judge = plan.judges[task.judge]
    provider = judge.provider_type
    judgment = Judgment(task.item.item_id, task.judge, task.view, task.order, provider)
    worst = stage.prices.cost(_judge_key(judge), approx_tokens(text), judge.max_tokens)
    async with stage.limits[provider]:
        for attempt in range(plan.tries):
            try:
                async with stage.budget.hold(provider, worst):
                    reply = await complete(
                        stage.http, route_judge(task.judge), text, judge.max_tokens
                    )
            except OverBudget:
                if not judgment.tries:
                    raise
                return judgment
            judgment.tries += 1
            judgment.spent_usd += reply.cost
            stage.budget.add(provider, reply.cost)
            if reply.status == 200:
                answer = parse_answer(reply.text)
                judgment.reply = reply.text[-600:]
                judgment.input_tokens = int(reply.usage.get("prompt_tokens") or 0)
                judgment.output_tokens = int(reply.usage.get("completion_tokens") or 0)
                judgment.cost_usd = reply.cost
                if answer is not None:
                    stitched_letter = "B" if task.order == "RS" else "A"
                    judgment.outcome, judgment.error = "ok", None
                    judgment.answer, judgment.correct = answer, answer == stitched_letter
                    stage.succeeded()
                    return judgment
                judgment.error = "no answer in the reply"  # tried again, as sampling varies
            else:
                judgment.error = reply.error
                if not reply.retryable:
                    stage.hard_failure(reply.error or "error")
                    break
            await stage.backoff(attempt)
    return judgment


def verdicts(judgments: dict[str, Judgment], judge: str, view: str) -> dict[str, bool]:
    """Item id -> detected (right in both orders), for items judged in both orders."""
    both: dict[str, list[bool]] = {}
    for j in judgments.values():
        if j.judge == judge and j.view == view and j.outcome == "ok" and j.correct is not None:
            both.setdefault(j.item_id, []).append(j.correct)
    return {item: all(marks) for item, marks in both.items() if len(marks) == len(ORDERS)}
