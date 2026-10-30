"""Mid-stream failover pieces: continuation requests, the continuer's choices, the seam
buffer, and when the relay continues at all (spec 7.6)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from switchyard.config import ModelConfig, ProviderConfig, Resilience, RouteGroup, Settings
from switchyard.core.attempts import Runner
from switchyard.core.midstream import SEAM_MAX_CHARS, SEAM_MIN_CHARS, Continuer, _seam_ready
from switchyard.core.router import Candidate
from switchyard.core.types import (
    ChatRequest,
    Finish,
    Message,
    StreamEvent,
    TextDelta,
    ToolCallArgs,
    ToolCallStart,
    Usage,
    UsageReport,
)
from switchyard.providers.errors import ErrorKind, ProviderError
from switchyard.resilience.breaker import Breakers
from switchyard.streaming.continuation import (
    ANCHORED,
    INSTRUCTION,
    MIN_REMAINING_TOKENS,
    continuation_request,
)
from switchyard.streaming.relay import Relay, Segment, StreamResult
from switchyard.streaming.seam import repair
from tests.unit.test_attempts import Fake, always, err

pytestmark = pytest.mark.anyio
ROOT = Path(__file__).parent.parent.parent
JOINS = ROOT / "eval" / "seam" / "results" / "20261023-050151" / "joins.jsonl"
REQUEST = ChatRequest(messages=(Message("user", "Why is the sky blue?"),), max_tokens=200)
EMITTED = "The sky looks blue because air molecules scatter"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# Continuation requests


def test_instructed_appends_the_partial_answer_and_the_instruction() -> None:
    out = continuation_request("instructed_repair", REQUEST, EMITTED)
    assert [m.role for m in out.messages] == ["user", "assistant", "user"]
    assert out.messages[1].content == EMITTED
    assert out.messages[2].content == INSTRUCTION.format(tail=EMITTED[-200:])
    assert out.max_tokens < REQUEST.max_tokens  # what was already sent is taken off


def test_anchored_quotes_the_partial_answer_in_the_user_turn() -> None:
    out = continuation_request("anchored", REQUEST, EMITTED)
    assert len(out.messages) == 1
    content = out.messages[0].content
    assert content == ANCHORED.format(prompt="Why is the sky blue?", partial=EMITTED)


def test_anchored_after_a_tool_result_adds_a_user_turn() -> None:
    request = ChatRequest(
        messages=(Message("user", "Weather?"), Message("tool", "sunny", tool_call_id="c1")),
        max_tokens=100,
    )
    out = continuation_request("anchored", request, "It is")
    assert [m.role for m in out.messages] == ["user", "tool", "user"]
    assert "<partial_answer>\nIt is\n</partial_answer>" in (out.messages[-1].content or "")


def test_native_continues_a_prefix_assistant_message() -> None:
    out = continuation_request("native", REQUEST, EMITTED)
    assert out.messages[-1] == Message("assistant", EMITTED, prefix=True)


def test_restart_sends_the_original_request() -> None:
    assert continuation_request("restart", REQUEST, EMITTED) == REQUEST


def test_remaining_tokens_never_drop_below_the_floor() -> None:
    out = continuation_request("instructed", REQUEST, "word " * 1000)
    assert out.max_tokens == MIN_REMAINING_TOKENS


def test_native_continuation_needs_deepseek() -> None:
    def settings(provider_type: str) -> Settings:
        return Settings(
            providers={"p": ProviderConfig(type=provider_type, base_url="http://x")},  # type: ignore[arg-type]
            models={"p/m": ModelConfig(continuation="native")},
            route_groups={"r": RouteGroup(models=["p/m"])},
            tenants=[],
        )

    settings("deepseek")
    for other in ("openai", "anthropic"):
        with pytest.raises(ValidationError, match="native continuation needs a deepseek"):
            settings(other)


# The seam buffer


def test_seam_buffer_rule() -> None:
    assert not _seam_ready("x. " * 10)  # too short, even with sentence ends
    assert not _seam_ready("x" * SEAM_MIN_CHARS)  # long enough, no sentence end yet
    assert _seam_ready("x" * SEAM_MIN_CHARS + " end. more")
    assert _seam_ready("x" * SEAM_MAX_CHARS)


def test_buffered_repair_matches_repair_of_the_whole_continuation() -> None:
    """On every real instructed join from the week 1 seam test, repairing only the buffered
    start, then sending the rest as it is, gives the same text as repairing the whole
    continuation, which is what the evaluation scored."""
    joins = [json.loads(line) for line in JOINS.read_text().splitlines()]
    items = [j for j in joins if j["strategy"] == "I+S" and j["continuation_raw"]]
    assert len(items) == 90
    for item in items:
        raw, emitted = item["continuation_raw"], item["emitted"]
        cut = next((n for n in range(1, len(raw) + 1) if _seam_ready(raw[:n])), len(raw))
        buffered = repair(emitted, raw[:cut]).text + raw[cut:]
        assert buffered == repair(emitted, raw).text, item["item_id"]


# The continuer


def make(
    scripts: dict[str, Any],
    models: tuple[str, ...] = ("a", "b", "c"),
    options: dict[str, ModelConfig] | None = None,
    request: ChatRequest = REQUEST,
    breakers: Breakers | None = None,
) -> tuple[Continuer, Fake]:
    fake = Fake(scripts)
    opts = options or {}
    candidates = [Candidate(f"fake/{m}", "fake", m, opts.get(m, ModelConfig())) for m in models]
    runner = Runner(
        breakers=breakers or Breakers(Resilience()),
        adapters={"fake": fake},
        limits=RouteGroup(models=["fake/a"], ttft_timeout_s=0.2, stall_timeout_s=0.2),
        request=request,
        candidates=candidates,
        deadline=asyncio.get_running_loop().time() + 30,
        mode="stream",
    )
    return Continuer(runner, candidates, request, current=_primary(runner, candidates[0])), fake


def _primary(runner: Runner, candidate: Candidate) -> Any:
    from switchyard.core.attempts import Attempt

    attempt = Attempt(1, "primary", candidate)
    runner.attempts.append(attempt)
    return attempt


async def drain(events: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [e async for e in events]


def text(events: list[StreamEvent]) -> str:
    return "".join(e.text for e in events if isinstance(e, TextDelta))


FAILED = Segment(EMITTED, None)
CUT = err(ErrorKind.CONNECTION)


async def test_resume_continues_on_the_next_model_and_repairs_the_join() -> None:
    # The model repeats the last three words, as continuations often do.
    reply = always(
        TextDelta("air molecules scatter blue light more than red light."), Finish("stop")
    )
    continuer, fake = make({"b": reply})
    resumed = await continuer.resume(CUT, EMITTED, FAILED)
    assert resumed is not None
    assert resumed.strategy == "instructed_repair"
    joined = text(resumed.head) + text(await drain(resumed.events))
    assert joined == " blue light more than red light."  # the repeated words were trimmed
    assert resumed.seam_actions == ("overlap_trimmed",)
    assert fake.calls == ["b"]
    primary, midstream = continuer.runner.attempts
    assert primary.outcome == "connection" and primary.delivered
    assert primary.usage.output_tokens > 0  # what the dead stream sent is billed
    assert midstream.role == "midstream" and midstream.emitted_chars_before == len(EMITTED)
    assert continuer.current is midstream


async def test_a_meta_reply_is_never_sent() -> None:
    meta = always(TextDelta("I can't continue an earlier message I didn't write."), Finish("stop"))
    good = always(TextDelta(" blue light."), Finish("stop"))
    continuer, fake = make({"b": meta, "c": good})
    resumed = await continuer.resume(CUT, EMITTED, FAILED)
    assert resumed is not None and resumed.attempt.key == "fake/c"
    assert continuer.runner.attempts[1].outcome == "meta_reply"
    assert fake.calls == ["b", "c"]


async def test_at_most_two_continuation_attempts() -> None:
    dead = always(err(ErrorKind.SERVER_ERROR))
    continuer, fake = make({"b": dead, "c": dead, "d": dead}, models=("a", "b", "c", "d"))
    assert await continuer.resume(CUT, EMITTED, FAILED) is None
    assert fake.calls == ["b", "c"]
    assert continuer.refusal == "midstream_limit"


async def test_a_failure_while_buffering_moves_on() -> None:
    partial = always(TextDelta(" more"), err(ErrorKind.CONNECTION))
    good = always(TextDelta(" blue light."), Finish("stop"))
    continuer, _ = make({"b": partial, "c": good})
    resumed = await continuer.resume(CUT, EMITTED, FAILED)
    assert resumed is not None and resumed.attempt.key == "fake/c"
    assert continuer.runner.attempts[1].outcome == "connection"


async def test_models_that_failed_or_are_open_are_skipped() -> None:
    breakers = Breakers(Resilience())
    breakers.force("fake/b", "open")
    continuer, fake = make({"c": always(TextDelta(" light."), Finish("stop"))}, breakers=breakers)
    resumed = await continuer.resume(CUT, EMITTED, FAILED)
    assert resumed is not None and fake.calls == ["c"]  # never a, which just failed


async def test_the_error_strategy_ends_the_stream() -> None:
    continuer, fake = make({}, options={"b": ModelConfig(continuation="error")})
    assert await continuer.resume(CUT, EMITTED, FAILED) is None
    assert continuer.refusal == "strategy_error" and fake.calls == []


async def test_stop_sequences_are_never_continued() -> None:
    request = ChatRequest(messages=REQUEST.messages, max_tokens=200, stop=("END",))
    continuer, fake = make({}, request=request)
    assert await continuer.resume(CUT, EMITTED, FAILED) is None
    assert continuer.refusal == "stop_sequences" and fake.calls == []


async def test_unrepaired_strategies_forward_as_they_come() -> None:
    reply = always(TextDelta(" scatter blue light."), Finish("stop"))
    continuer, _ = make({"b": reply}, options={"b": ModelConfig(continuation="instructed")})
    resumed = await continuer.resume(CUT, EMITTED, FAILED)
    assert resumed is not None
    assert text(resumed.head) + text(await drain(resumed.events)) == " scatter blue light."
    assert resumed.seam_actions == ()


# When the relay continues


async def relay_run(events: list[Any], resume: Any) -> tuple[str, StreamResult]:
    async def source() -> AsyncIterator[StreamEvent]:
        for event in events:
            if isinstance(event, ProviderError):
                raise event
            yield event

    results: list[StreamResult] = []

    def finalize(result: StreamResult) -> dict[str, Any]:
        results.append(result)
        return {}

    relay = Relay(response_id="r", alias="a", request=REQUEST, finalize=finalize, resume=resume)
    out = b"".join([chunk async for chunk in relay.run(source())])
    return out.decode(), results[0]


class Recorder:
    def __init__(self, continuation: Any = None, fail: bool = False) -> None:
        self.calls: list[tuple[str, Segment]] = []
        self.continuation = continuation
        self.fail = fail

    async def __call__(self, error: ProviderError, emitted: str, segment: Segment) -> Any:
        self.calls.append((emitted, segment))
        if self.fail:
            raise RuntimeError("bug")
        return self.continuation


class Cont:
    def __init__(self, events: list[StreamEvent]) -> None:
        self.head: list[StreamEvent] = events[:1]

        async def rest() -> AsyncIterator[StreamEvent]:
            for event in events[1:]:
                yield event

        self.events = rest()

    @property
    def comment(self) -> str:
        return "continuing on x with y"


async def test_the_relay_hands_over_what_the_client_has() -> None:
    later = Cont([TextDelta(" more."), Finish("stop"), UsageReport(Usage(9, 2))])
    resume = Recorder(later)
    out, result = await relay_run([TextDelta("Hello"), UsageReport(Usage(3, 1)), CUT], resume)
    assert resume.calls == [("Hello", Segment("Hello", Usage(3, 1)))]
    assert ": switchyard failover: connection after 5 characters" in out
    assert ": switchyard continuing on x with y" in out
    assert result.text == "Hello more." and result.segment_text == " more."
    assert result.midstream == 1 and result.error is None
    assert result.usage == Usage(9, 2)  # the last attempt's; earlier ones are settled apart


async def test_a_partial_tool_call_is_not_continued() -> None:
    resume = Recorder(Cont([TextDelta("x")]))
    events = [
        TextDelta("Let me check."),
        ToolCallStart(0, "c1", "search"),
        ToolCallArgs(0, '{"q'),
        CUT,
    ]
    _, result = await relay_run(events, resume)
    assert resume.calls == [] and result.error is CUT


async def test_a_bug_in_failover_ends_with_the_error_event() -> None:
    out, result = await relay_run([TextDelta("Hello"), CUT], Recorder(fail=True))
    assert result.error is CUT
    assert '"error"' in out
