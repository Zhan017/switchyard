"""The attempt runner: retries, failover, commit points, and the S1 replay (spec 7.5)."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from chaos.stats import wilson
from switchyard.config import ModelConfig, Resilience, RouteGroup
from switchyard.core.attempts import Exhausted, NoCandidate, Runner, assemble
from switchyard.core.router import Candidate
from switchyard.core.types import (
    ChatRequest,
    Completion,
    Finish,
    Message,
    RefusalDelta,
    ResponseFormat,
    StreamEvent,
    TextDelta,
    ToolCallArgs,
    ToolCallStart,
)
from switchyard.providers.base import Adapter
from switchyard.providers.errors import ErrorKind, ProviderError
from switchyard.resilience.breaker import Breakers

pytestmark = pytest.mark.anyio

LIMITS = RouteGroup(models=["fake/a"], ttft_timeout_s=0.2, stall_timeout_s=0.2)
REQUEST = ChatRequest(messages=(Message("user", "hi"),), max_tokens=16)
OK = [TextDelta("hello"), TextDelta(" there"), Finish("stop")]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def err(kind: ErrorKind, retry_after: float | None = None) -> ProviderError:
    return ProviderError(kind, f"injected {kind.value}", retry_after=retry_after)


Script = Callable[[int], list[Any]]  # call number on this model -> events, delays, errors


class Fake(Adapter):
    """An adapter that plays a script per model: events, float delays, or an error."""

    def __init__(self, scripts: dict[str, Script]) -> None:
        self.provider_id = "fake"
        self.scripts = scripts
        self.calls: list[str] = []

    def url(self) -> str:
        return ""

    def body(self, model: str, options: ModelConfig, request: ChatRequest, *, stream: bool) -> Any:
        return {}

    async def complete(self, model: str, options: ModelConfig, request: ChatRequest) -> Completion:
        raise NotImplementedError

    def stream(
        self, model: str, options: ModelConfig, request: ChatRequest
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append(model)
        return self._play(self.scripts[model](self.calls.count(model)))

    async def _play(self, items: list[Any]) -> AsyncIterator[StreamEvent]:
        for item in items:
            if isinstance(item, ProviderError):
                raise item
            if isinstance(item, float):
                await asyncio.sleep(item)
                continue
            yield item


def always(*items: Any) -> Script:
    return lambda _call: list(items)


def candidates(*models: str) -> list[Candidate]:
    return [Candidate(f"fake/{m}", "fake", m, ModelConfig()) for m in models]


def runner(
    fake: Fake,
    models: tuple[str, ...] = ("a", "b"),
    *,
    breakers: Breakers | None = None,
    mode: str = "stream",
    request: ChatRequest = REQUEST,
    policy: Resilience | None = None,
) -> Runner:
    async def no_wait(_seconds: float) -> None:
        return None

    return Runner(
        breakers=breakers or Breakers(policy or Resilience()),
        adapters={"fake": fake},
        limits=LIMITS,
        request=request,
        candidates=candidates(*models),
        deadline=asyncio.get_running_loop().time() + 30,
        mode=mode,  # type: ignore[arg-type]
        rng=random.Random(1),
        sleep=no_wait,
    )


def trail(run: Runner) -> list[tuple[str, str, str]]:
    return [(a.candidate.model, a.role, a.outcome) for a in run.attempts]


async def drain(events: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [event async for event in events]


async def test_first_attempt_succeeds() -> None:
    run = runner(Fake({"a": always(*OK), "b": always(*OK)}))
    opened = await run.open()
    assert [type(e) for e in await drain(opened.all_events())] == [TextDelta, TextDelta, Finish]
    assert trail(run) == [("a", "primary", "pending")]  # the relay records the outcome


async def test_a_5xx_is_retried_once_then_fails_over() -> None:
    fake = Fake({"a": always(err(ErrorKind.SERVER_ERROR)), "b": always(*OK)})
    run = runner(fake)
    await run.open()
    assert trail(run) == [
        ("a", "primary", "server_error"),
        ("a", "retry", "server_error"),
        ("b", "failover", "pending"),
    ]


async def test_a_retry_can_succeed() -> None:
    fake = Fake({"a": lambda n: [err(ErrorKind.CONNECTION)] if n == 1 else OK, "b": always(*OK)})
    run = runner(fake)
    await run.open()
    assert trail(run) == [("a", "primary", "connection"), ("a", "retry", "pending")]


@pytest.mark.parametrize(
    "kind", [ErrorKind.INVALID_RESPONSE, ErrorKind.RATE_LIMITED, ErrorKind.AUTH]
)
async def test_failover_without_a_retry(kind: ErrorKind) -> None:
    fake = Fake({"a": always(err(kind)), "b": always(*OK)})
    breakers = Breakers(Resilience())
    run = runner(fake, breakers=breakers)
    await run.open()
    assert [a.role for a in run.attempts] == ["primary", "failover"]
    expected = {ErrorKind.RATE_LIMITED: "cooldown", ErrorKind.AUTH: "cooldown"}.get(kind)
    assert breakers.skip_reason("fake/a") == expected


@pytest.mark.parametrize("kind", [ErrorKind.CLIENT_ERROR, ErrorKind.DEADLINE])
async def test_no_failover_for_errors_that_would_repeat_anywhere(kind: ErrorKind) -> None:
    fake = Fake({"a": always(err(kind)), "b": always(*OK)})
    run = runner(fake)
    with pytest.raises(Exhausted) as caught:
        await run.open()
    assert caught.value.error.kind == kind
    assert fake.calls == ["a"]


async def test_three_attempts_at_most() -> None:
    fail = always(err(ErrorKind.SERVER_ERROR))
    fake = Fake({m: fail for m in "abcd"})
    run = runner(fake, ("a", "b", "c", "d"))
    with pytest.raises(Exhausted) as caught:
        await run.open()
    assert len(caught.value.attempts) == 3
    assert fake.calls == ["a", "a", "b"]


async def test_spent_retry_budget_fails_over_at_once() -> None:
    policy = Resilience(retry_budget_ratio=0, retry_budget_floor=0)
    fake = Fake({"a": always(err(ErrorKind.SERVER_ERROR)), "b": always(*OK)})
    run = runner(fake, policy=policy)
    await run.open()
    assert [a.role for a in run.attempts] == ["primary", "failover"]


async def test_open_breakers_are_skipped_without_an_attempt() -> None:
    breakers = Breakers(Resilience())
    breakers.force("fake/a", "open")
    fake = Fake({"a": always(*OK), "b": always(*OK)})
    run = runner(fake, breakers=breakers)
    opened = await run.open()
    assert opened.attempt.candidate.model == "b"
    assert opened.attempt.role == "primary"  # skipping is not an attempt
    assert run.skipped == [("fake/a", "open")]


async def test_everything_open_is_no_candidate() -> None:
    breakers = Breakers(Resilience())
    breakers.force("fake/a", "open")
    breakers.cool_down("fake/b", 25, "rate_limited")
    run = runner(Fake({}), breakers=breakers)
    with pytest.raises(NoCandidate) as caught:
        await run.open()
    assert caught.value.retry_after == 10
    assert run.attempts == []


async def test_a_hang_is_a_first_token_timeout_and_a_slow_sample() -> None:
    breakers = Breakers(Resilience())
    fake = Fake({"a": always(5.0), "b": always(*OK)})
    run = runner(fake, breakers=breakers)
    await run.open()
    assert trail(run)[0] == ("a", "primary", "timeout")
    health = breakers.snapshot("fake/a")["health"]
    assert health["outcomes"] == {"timeout": 2}  # the primary and its retry


# Commit points


async def test_failure_inside_the_first_tool_call_is_an_ordinary_failover() -> None:
    partial = [ToolCallStart(0, "call_1", "search"), ToolCallArgs(0, '{"q": '), 0.3]
    fake = Fake({"a": always(*partial), "b": always(*OK)})
    run = runner(fake)
    opened = await run.open()
    assert opened.attempt.candidate.model == "b"
    assert trail(run)[0] == ("a", "primary", "stall")


async def test_the_first_complete_tool_call_commits() -> None:
    events = [
        ToolCallStart(0, "call_1", "search"),
        ToolCallArgs(0, "{}"),
        ToolCallStart(1, "call_2", "fetch"),
        err(ErrorKind.CONNECTION),
    ]
    run = runner(Fake({"a": always(*events), "b": always(*OK)}))
    opened = await run.open()
    assert opened.attempt.candidate.model == "a"
    with pytest.raises(ProviderError):  # after the commit, the relay sees the failure
        await drain(opened.all_events())


async def test_text_commits_and_later_failures_are_mid_stream() -> None:
    run = runner(Fake({"a": always(TextDelta("hi"), err(ErrorKind.CONNECTION)), "b": always()}))
    opened = await run.open()
    assert opened.attempt.candidate.model == "a"
    with pytest.raises(ProviderError):
        await drain(opened.all_events())


async def test_a_refusal_is_an_answer_not_a_failure() -> None:
    fake = Fake({"a": always(RefusalDelta("I can't help"), Finish("stop")), "b": always(*OK)})
    run = runner(fake, mode="buffered")
    opened = await run.open()
    assert opened.attempt.outcome == "content_filter"
    assert fake.calls == ["a"]
    assert opened.collected is not None
    assert assemble(opened.collected).refusal == "I can't help"


async def test_invalid_json_fails_over_in_buffered_mode() -> None:
    request = ChatRequest(
        messages=REQUEST.messages, max_tokens=16, response_format=ResponseFormat("json_object")
    )
    bad = always(TextDelta('{"a": '), Finish("stop"))
    good = always(TextDelta('{"a": 1}'), Finish("stop"))
    run = runner(Fake({"a": bad, "b": good}), mode="buffered", request=request)
    opened = await run.open()
    assert opened.attempt.candidate.model == "b"
    assert trail(run)[0] == ("a", "primary", "invalid_response")
    assert run.attempts[0].usage.output_tokens > 0  # the bad answer was billed


# The S1 replay (spec 7.5): S1's arrival pattern against the policy, before any networking.


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


async def test_s1_replay_reaches_its_target() -> None:
    """S1: the primary returns 500 on half its requests, at 10 requests a second for 120 s.
    Target: the lower bound of the 95% interval on success is at least 99%, and the
    breaker opens."""
    clock = Clock()
    policy = Resilience()
    breakers = Breakers(policy, clock=clock, wall=clock)
    opened_states: list[str] = []
    breakers.listeners.append(lambda t: opened_states.append(t.to_state))
    rng = random.Random(1)

    def flaky(_call: int) -> list[Any]:
        return [err(ErrorKind.SERVER_ERROR)] if rng.random() < 0.5 else OK

    fake = Fake({"a": flaky, "b": always(*OK)})

    async def advance(seconds: float) -> None:
        clock.now += seconds

    successes = 0
    next_probe: dict[str, float] = {}
    for i in range(1200):
        clock.now = 1_000_000.0 + i * 0.1
        # The prober, on its fixed schedule.
        for key, opened_at in breakers.open_models():
            due = next_probe.setdefault(key, opened_at + policy.probe_interval_s)
            while due <= clock.now:
                breakers.probe_result(key, rng.random() >= 0.5)
                due += policy.probe_interval_s
            next_probe[key] = due
        next_probe = {k: v for k, v in next_probe.items() if breakers.skip_reason(k) == "open"}
        run = Runner(
            breakers=breakers,
            adapters={"fake": fake},
            limits=LIMITS,
            request=REQUEST,
            candidates=candidates("a", "b"),
            deadline=asyncio.get_running_loop().time() + 30,
            mode="stream",
            rng=rng,
            sleep=advance,
        )
        try:
            opened = await run.open()
        except (Exhausted, NoCandidate):
            continue
        await drain(opened.all_events())
        run.finish(opened.attempt, "ok")
        successes += 1
    interval = wilson(successes, 1200)
    assert interval.low >= 0.99, interval
    assert "open" in opened_states
