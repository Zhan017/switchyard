"""The v0.1 seam evaluation's harness (eval/seam/v01): the parts that decide results."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from eval.seam.v01 import __main__ as cli
from eval.seam.v01.budget import Budget, OverBudget
from eval.seam.v01.client import StreamRead
from eval.seam.v01.generate import _classify
from eval.seam.v01.judge import ORDERS, local_view, parse_answer, tasks, verdicts
from eval.seam.v01.labels import cohen_kappa, sample
from eval.seam.v01.plan import load_plan, load_prompts
from eval.seam.v01.records import Judgment, Stitched
from eval.seam.v01.stack import pieces, write_gateway_config
from switchyard.config import load_settings
from switchyard.core.types import ChatRequest, Message
from switchyard.providers.anthropic import AnthropicAdapter

PLAN = load_plan()


def test_the_plan_and_prompts_match_the_spec() -> None:
    prompts = load_prompts()
    assert Counter(p.kind for p in prompts) == {"prose": 30, "code": 18, "list": 12}
    assert len(PLAN.items(prompts)) == 1560  # SPEC 12.3
    assert PLAN.strategies_for("deepseek") == ["R", "I", "I+S", "A", "N"]
    assert PLAN.strategies_for("claude") == ["R", "I", "I+S", "A"]


@pytest.mark.parametrize("dry_run", [False, True])
def test_gateway_config_is_valid_and_routes_each_strategy(tmp_path: Path, dry_run: bool) -> None:
    path = write_gateway_config(PLAN, "http://127.0.0.1:9", tmp_path, dry_run)
    settings = load_settings(path)
    route = settings.route_groups["cont-gpt-deepseek-N"]
    assert route.models == ["primary-gpt/replay", "deepseek-N/deepseek-flash"]
    assert settings.models["deepseek-N/deepseek-flash"].continuation == "native"
    assert settings.models["claude-IS/claude-haiku-5-5"].continuation == "instructed_repair"
    assert settings.models["gpt-R/gpt-6-luna"].continuation == "restart"
    # Model options come from the shipped config: what the gateway sends in production.
    assert settings.models["gpt-A/gpt-6-luna"].probe_max_tokens == 16
    assert settings.route_groups["cont-claude-gpt-IS"].ttft_timeout_s == 5.0
    assert settings.resilience.trip_consecutive_failures > 10**6  # breakers never open
    sonnet = settings.models["judge-sonnet/claude-sonnet-5-5"]
    assert sonnet.request_overrides["output_config"] == {"effort": "low"}
    assert sonnet.sampling_params is False
    assert (settings.providers["claude-I"].type == "anthropic") is not dry_run


def test_the_sonnet_judge_request(tmp_path: Path) -> None:
    """Low effort, no sampling parameters, and the thinking default left alone."""
    settings = load_settings(write_gateway_config(PLAN, "http://127.0.0.1:9", tmp_path, False))
    adapter = AnthropicAdapter("judge-sonnet", settings.providers["judge-sonnet"], None)  # type: ignore[arg-type]
    request = ChatRequest(messages=(Message("user", "Which?"),), max_tokens=4096, temperature=0)
    options = settings.models["judge-sonnet/claude-sonnet-5-5"]
    body = adapter.body("claude-sonnet-5-5", options, request, stream=True)
    assert body["output_config"] == {"effort": "low"}
    assert "temperature" not in body and "thinking" not in body
    assert body["max_tokens"] == 4096


def test_pieces_cover_the_text() -> None:
    for text in ("Hello, world.", "def f(x):\n    return x  ", "", "  lead"):
        assert "".join(pieces(text)) == text


def test_local_view_shares_the_start_and_trims_to_words() -> None:
    reference = "word " * 100 + "ORIGINAL ending goes on " * 20
    cut = 500
    stitched = reference[:cut] + "NEW ending from elsewhere " * 20
    a, b = local_view(reference, stitched, cut, 300)
    assert a.startswith("…") and b.startswith("…")
    shared = a.index("ORIGINAL")
    assert a[:shared] == b[:shared]  # the same start, since both texts match up to the cut
    assert "NEW" in b and "ORIGINAL" not in b
    assert a.endswith("…") and not a[:-1].endswith(" ")
    assert len(a) <= 2 * 300 + 2


def test_local_view_at_the_edges() -> None:
    a, b = local_view("short answer", "short reply", 6, 300)
    assert (a, b) == ("short answer", "short reply")


@pytest.mark.parametrize(
    ("reply", "answer"),
    [
        ("B reads unevenly.\nAnswer: B", "B"),
        ("Answer: **A**", "A"),
        ("answer: (b)", "B"),
        ("Answer: A at first, but on reflection\nAnswer: B", "B"),
        ("A", "A"),
        ("Both seem fine.", None),
        ("Answer: neither", None),
    ],
)
def test_parse_answer(reply: str, answer: str | None) -> None:
    assert parse_answer(reply) == answer


def test_cohen_kappa() -> None:
    assert cohen_kappa([True, False, True, False], [True, False, True, False]) == 1.0
    assert cohen_kappa([True, False, True, False], [False, True, False, True]) == -1.0
    assert cohen_kappa([True, True], [True, True]) is None  # chance agreement is total
    value = cohen_kappa([True, True, False, False], [True, False, True, False])
    assert value == pytest.approx(0.0)


def _item(prompt: str, primary: str, fallback: str, strategy: str, **kw: object) -> Stitched:
    code = {"I+S": "IS"}.get(strategy, strategy)
    return Stitched(
        item_id=f"{prompt}-{primary}-{fallback}-{code}",
        prompt_id=prompt,
        kind="prose",
        primary=primary,
        fallback=fallback,
        strategy=strategy,
        provider="x",
        outcome=str(kw.get("outcome", "ok")),
    )


def _all_items(n_prompts: int) -> list[Stitched]:
    prompts = [f"e{i:02d}" for i in range(1, n_prompts + 1)]
    return [
        _item(p, primary, fallback, s)
        for p in prompts
        for primary, fallback in PLAN.pairs
        for s in PLAN.strategies_for(fallback)
    ]


def test_the_label_sample_is_10_per_strategy_spread_over_pairs() -> None:
    items = _all_items(60)
    drawn = sample(PLAN, items)
    assert len(drawn) == 50
    assert Counter(i.strategy for i in drawn) == dict.fromkeys(PLAN.strategies, 10)
    for strategy in PLAN.strategies:
        pairs = Counter(i.pair for i in drawn if i.strategy == strategy)
        assert max(pairs.values()) - min(pairs.values()) <= 1  # round robin
    assert len({i.prompt_id for i in drawn}) == 50  # no prompt twice while there are spares
    assert [i.item_id for i in sample(PLAN, list(reversed(items)))] == [
        i.item_id for i in drawn
    ]  # the input order doesn't matter: the seed decides


def test_the_label_sample_skips_failed_continuations() -> None:
    items = [*_all_items(2), _item("e09", "claude", "gpt", "I", outcome="meta_reply")]
    assert all(i.outcome == "ok" for i in sample(PLAN, items))


def test_judge_tasks_put_the_gate_and_headline_first() -> None:
    items = _all_items(2)
    labelled = {items[0].item_id}
    found = tasks(PLAN, items, labelled)
    keys = [t.key for t in found]
    assert len(keys) == len(set(keys))
    first = found[: 2 * len(PLAN.judges)]
    assert {t.item.item_id for t in first} == labelled
    assert {t.judge for t in first} == set(PLAN.judges)
    views = [t.view for t in found]
    assert "local" not in views[views.index("full") :]  # the full view comes last
    sonnet_local = sum(t.judge == "sonnet" and t.view == "local" for t in found)
    assert sonnet_local == 2 * len(items)
    neutral = {t.judge for t in found if t.view == "local" and t.item.pair == "claude-gpt"}
    assert neutral >= {"deepseek", "sonnet"}


def test_an_item_counts_as_detected_only_if_right_in_both_orders() -> None:
    def j(item: str, order: str, correct: bool) -> Judgment:
        found = Judgment(item, "sonnet", "local", order, "anthropic", outcome="ok")
        found.correct = correct
        return found

    rows = [j("x", "RS", True), j("x", "SR", True), j("y", "RS", True), j("y", "SR", False)]
    rows.append(j("z", "RS", True))  # only one order: no verdict
    assert verdicts({r.key: r for r in rows}, "sonnet", "local") == {"x": True, "y": False}
    assert ORDERS == ("RS", "SR")


def _read(status: int = 200, **kw: object) -> StreamRead:
    read = StreamRead(status)
    for name, value in kw.items():
        setattr(read, name, value)
    return read


def test_classify_stream_outcomes() -> None:
    emitted = "The answer starts here"
    note = "switchyard failover: connection after 22 characters"
    ok = _read(before=emitted, after=" and goes on.", failover=note)
    assert _classify(ok, emitted) == ("ok", False)
    meta = _read(
        before=emitted,
        failover=note,
        error="cut",
        summary={"trail": [{"role": "primary"}, {"role": "midstream", "outcome": "meta_reply"}]},
    )
    assert _classify(meta, emitted) == ("meta_reply", False)
    limited = _read(
        before=emitted,
        failover=note,
        error="cut",
        summary={"trail": [{"role": "midstream", "outcome": "rate_limited"}]},
    )
    assert _classify(limited, emitted) == ("continuation_rate_limited", True)
    cooling = _read(before=emitted, failover=note, error="cut", summary={"not_continued": "x"})
    assert _classify(cooling, emitted)[1] is False
    skipped = _read(
        before=emitted, failover=note, error="cut", summary={"not_continued": "no_candidate"}
    )
    assert _classify(skipped, emitted) == ("continuation_no_candidate", True)
    assert _classify(_read(before="other", failover=note), emitted) == ("emitted_differs", False)
    assert _classify(_read(before=emitted), emitted) == ("not_cut", False)
    assert _classify(_read(status=503), emitted) == ("http_503", True)


@pytest.mark.anyio
async def test_budget_refuses_a_call_that_could_go_over_the_cap() -> None:
    budget = Budget({"openai": 1.0}, {"openai": 0.7})
    async with budget.hold("openai", 0.2):
        with pytest.raises(OverBudget):
            async with budget.hold("openai", 0.2):  # 0.7 spent + 0.2 held + 0.2 > 1.0
                pass
    with pytest.raises(OverBudget):  # stopped for the rest of the stage
        async with budget.hold("openai", 0.01):
            pass


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_dry_run_end_to_end_and_resume(tmp_path: Path) -> None:
    out = tmp_path / "dry"
    assert cli.main(["dry-run", "--limit", "1", "--out", str(out)]) == 0
    report = (out / "report.md").read_text()
    assert "Dry run" in report and "Seamless rate, local view" in report
    lines = (out / "stitched.jsonl").read_text().count("\n")
    assert lines == 26  # 4 strategies on four pairs, 5 on the two DeepSeek pairs
    # A second run finds nothing left to do and calls nobody.
    assert cli.main(["generate", "--dry-run", "--limit", "1", "--out", str(out)]) == 0
    assert (out / "stitched.jsonl").read_text().count("\n") == lines
    # Drawing the labels again would not be blind.
    assert cli.main(["sample", "--limit", "1", "--out", str(out)]) == 2
