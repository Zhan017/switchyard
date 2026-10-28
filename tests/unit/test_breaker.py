"""Breaker trip rules, probes, cooldowns, forced states and the retry budget (spec 7.3 to 7.5)."""

from __future__ import annotations

import itertools

from hypothesis import given, settings
from hypothesis import strategies as st

from switchyard.config import Resilience
from switchyard.resilience.breaker import Breakers, Transition
from switchyard.resilience.health import Bucket, ModelHealth

KEY = "p/m"
START = 1_000_020.0  # on a 30 s period boundary


class Clock:
    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make(policy: Resilience | None = None) -> tuple[Breakers, Clock, list[Transition]]:
    clock = Clock()
    breakers = Breakers(policy or Resilience(), clock=clock, wall=clock)
    seen: list[Transition] = []
    breakers.listeners.append(seen.append)
    return breakers, clock, seen


def is_open(breakers: Breakers) -> bool:
    return breakers.skip_reason(KEY) == "open"


# Trip rule 1: at least 10 requests in 60 s, at least 40% failed


def test_forty_percent_failures_over_ten_requests_trips() -> None:
    breakers, clock, seen = make()
    # Alternate so rule 2 (5 in a row) never fires: ok, fail, ok, fail, ...
    for i in range(9):
        breakers.record_outcome(KEY, "server_error" if i % 2 else "ok")
        clock.now += 1
    assert not is_open(breakers)  # 4 of 9: not enough requests yet
    breakers.record_outcome(KEY, "ok")
    assert not is_open(breakers)  # 4 of 10 is 40%, but the check runs on failures
    breakers.record_outcome(KEY, "timeout")
    assert is_open(breakers)  # 5 of 11
    assert seen[-1].to_state == "open" and "failed in 60 s" in seen[-1].reason


def test_thirty_percent_failures_do_not_trip() -> None:
    breakers, clock, _ = make()
    for i in range(100):
        breakers.record_outcome(KEY, "connection" if i % 10 in (0, 4, 7) else "ok")
        clock.now += 0.5
    assert not is_open(breakers)


def test_failures_older_than_the_window_are_forgotten() -> None:
    breakers, clock, _ = make()
    for _ in range(3):  # 6 of 9 failed, never 5 in a row
        for outcome in ("server_error", "server_error", "ok"):
            breakers.record_outcome(KEY, outcome)
    clock.now += 70
    for i in range(10):  # 3 of 10 now; 9 of 19 (47%) if the old ones still counted
        breakers.record_outcome(KEY, "server_error" if i in (0, 4, 8) else "ok")
    assert not is_open(breakers)


def test_failures_in_a_row_count_however_far_apart() -> None:
    """Rule 2 is about nothing succeeding in between, so sparse traffic can still trip."""
    breakers, clock, _ = make()
    for _ in range(5):
        breakers.record_outcome(KEY, "connection")
        clock.now += 120
    assert is_open(breakers)


# Trip rule 2: five retryable failures in a row


def test_five_failures_in_a_row_trip_at_low_traffic() -> None:
    breakers, clock, seen = make()
    for _ in range(4):
        breakers.record_outcome(KEY, "connection")
        clock.now += 5
    assert not is_open(breakers)
    breakers.record_outcome(KEY, "stall")
    assert is_open(breakers)
    assert seen[-1].reason == "5 failures in a row"


def test_a_success_resets_the_run() -> None:
    breakers, _, _ = make()
    for _ in range(4):
        breakers.record_outcome(KEY, "connection")
    breakers.record_outcome(KEY, "ok")
    for _ in range(4):
        breakers.record_outcome(KEY, "connection")
    assert not is_open(breakers)  # 8 of 9 failed, but rule 1 needs 10 requests


# Trip rule 3: p95 first token over budget in two consecutive 30 s periods


def test_slow_first_tokens_in_two_periods_trip() -> None:
    breakers, clock, seen = make()
    for _period in range(2):
        for i in range(20):
            breakers.record_ttft(KEY, 0.5, over_budget=False)
            if i < 2:  # 2 of 22 over budget: more than 5%
                breakers.record_ttft(KEY, 5.0, over_budget=True)
            clock.now += 1.4
        clock.now += 2
    assert not is_open(breakers)  # the second period isn't complete yet
    clock.now = START + 60
    breakers.record_ttft(KEY, 5.0, over_budget=True)
    assert is_open(breakers)
    assert "time to first token" in seen[-1].reason


def test_one_slow_period_does_not_trip() -> None:
    breakers, clock, _ = make()
    for _ in range(20):
        breakers.record_ttft(KEY, 0.5, over_budget=False)
        breakers.record_ttft(KEY, 5.0, over_budget=True)
        clock.now += 1
    for _ in range(40):
        breakers.record_ttft(KEY, 0.5, over_budget=False)
        clock.now += 1
    clock.now = START + 60  # the last two whole periods: one slow, one fast
    breakers.record_ttft(KEY, 5.0, over_budget=True)
    assert not is_open(breakers)


# What doesn't count


def test_rate_limits_client_errors_and_filters_never_trip() -> None:
    breakers, _, _ = make()
    for outcome in ["rate_limited", "client_error", "content_filter", "auth", "deadline"] * 20:
        breakers.record_outcome(KEY, outcome)
    assert not is_open(breakers)
    assert breakers.snapshot(KEY)["health"]["requests"] == 0


def test_a_429_cools_down_until_retry_after() -> None:
    breakers, clock, seen = make()
    breakers.cool_down(KEY, 30, "rate_limited")
    assert breakers.skip_reason(KEY) == "cooldown"
    clock.now += 29
    assert breakers.skip_reason(KEY) == "cooldown"
    clock.now += 2
    assert breakers.skip_reason(KEY) is None
    assert seen == []  # a cooldown is not a breaker transition


def test_a_429_without_retry_after_uses_the_default() -> None:
    breakers, clock, _ = make()
    breakers.cool_down(KEY, None, "rate_limited")
    clock.now += 9
    assert breakers.skip_reason(KEY) == "cooldown"
    clock.now += 2
    assert breakers.skip_reason(KEY) is None


# Recovery through probes


def trip(breakers: Breakers) -> None:
    for _ in range(5):
        breakers.record_outcome(KEY, "connection")
    assert is_open(breakers)


def test_three_good_probes_in_a_row_close_the_breaker() -> None:
    breakers, _, seen = make()
    trip(breakers)
    breakers.probe_result(KEY, True)
    breakers.probe_result(KEY, True)
    breakers.probe_result(KEY, False)  # resets the run
    breakers.probe_result(KEY, True)
    breakers.probe_result(KEY, True)
    assert is_open(breakers)
    breakers.probe_result(KEY, True)
    assert not is_open(breakers)
    assert seen[-1].to_state == "closed"


def test_user_traffic_successes_do_not_close_an_open_breaker() -> None:
    breakers, _, _ = make()
    trip(breakers)
    for _ in range(50):
        breakers.record_outcome(KEY, "ok")
    assert is_open(breakers)


def test_closing_forgets_the_old_failures() -> None:
    breakers, _, _ = make()
    trip(breakers)
    for _ in range(3):
        breakers.probe_result(KEY, True)
    breakers.record_outcome(KEY, "connection")  # one failure must not reopen it
    assert not is_open(breakers)


def test_probe_schedule_starts_when_the_breaker_opens() -> None:
    breakers, clock, _ = make()
    clock.now += 3
    trip(breakers)
    assert breakers.open_models() == [(KEY, START + 3)]


# Forced states


def test_forced_open_and_closed() -> None:
    breakers, _, seen = make()
    breakers.force(KEY, "open")
    assert is_open(breakers)
    assert breakers.open_models() == []  # forced open is not probed
    breakers.force(KEY, "closed")
    for _ in range(20):
        breakers.record_outcome(KEY, "connection")
    assert not is_open(breakers)  # forced closed never trips
    breakers.force(KEY, "auto")
    trip(breakers)
    assert [t.to_state for t in seen] == ["open", "closed", "open"]


# Retry budget


def test_retry_budget_is_twenty_percent_plus_a_floor() -> None:
    breakers, _, _ = make()
    for _ in range(50):
        breakers.note_attempt(KEY, retry=False)
    allowed = 0
    while breakers.allow_retry(KEY):
        breakers.note_attempt(KEY, retry=True)
        allowed += 1
    assert allowed == 13  # 20% of 50, plus 3


def test_retry_budget_refills_as_the_window_moves() -> None:
    breakers, clock, _ = make()
    for _ in range(3):
        breakers.note_attempt(KEY, retry=True)
    assert not breakers.allow_retry(KEY)
    clock.now += 61
    assert breakers.allow_retry(KEY)


def test_retry_after_reports_the_soonest_recovery() -> None:
    breakers, _, _ = make()
    breakers.cool_down("a/x", 42.2, "rate_limited")
    breakers.cool_down("b/y", 12.0, "rate_limited")
    assert breakers.retry_after(["a/x", "b/y"]) == 12
    breakers.force("c/z", "open")
    assert breakers.retry_after(["a/x", "c/z"]) == 10  # an open breaker is probed every 10 s


def test_reload_drops_removed_models() -> None:
    breakers, _, _ = make()
    trip(breakers)
    breakers.retain(["other/model"])
    assert not is_open(breakers)


# Property tests

OUTCOMES = ["ok", "connection", "timeout", "server_error", "stall", "rate_limited", "client_error"]
steps = st.lists(
    st.tuples(
        st.sampled_from([*OUTCOMES, "ttft_slow", "ttft_fast", "probe_ok", "probe_fail"]),
        st.floats(min_value=0, max_value=8),
    ),
    max_size=200,
)


@settings(max_examples=200, deadline=None)
@given(steps)
def test_the_breaker_opens_only_when_a_rule_holds(script: list[tuple[str, float]]) -> None:
    breakers, clock, seen = make()
    for action, advance in script:
        clock.now += advance
        was_open = is_open(breakers)
        state = breakers.model(KEY)
        if action == "probe_ok":
            breakers.probe_result(KEY, True)
        elif action == "probe_fail":
            breakers.probe_result(KEY, False)
        elif action.startswith("ttft"):
            breakers.record_ttft(KEY, 5.0 if action == "ttft_slow" else 0.2, action == "ttft_slow")
        else:
            breakers.record_outcome(KEY, action)
        if not was_open and is_open(breakers):
            # It opened on this step: a trip rule must hold right now.
            assert breakers.trip_reason(state) is not None
            assert action in ("connection", "timeout", "server_error", "stall", "ttft_slow")
        if was_open and not is_open(breakers):
            assert action == "probe_ok"  # only probes close a breaker
    for earlier, later in itertools.pairwise(seen):
        assert earlier.to_state != later.to_state  # transitions alternate


@settings(max_examples=200, deadline=None)
@given(
    st.lists(st.tuples(st.integers(0, 8), st.sampled_from(["ok", "timeout"])), max_size=60),
    st.lists(st.booleans(), max_size=10),
)
def test_unsent_counts_survive_failed_syncs(
    records: list[tuple[int, str]], sync_ok: list[bool]
) -> None:
    """Health counts are never lost or doubled, whatever happens to the Redis sync."""
    health = ModelHealth(keep=9)
    remote: dict[int, Bucket] = {}
    total = Bucket()
    syncs = iter(sync_ok)
    for index, outcome in records:
        health.current(index).outcomes[outcome] += 1
        total.outcomes[outcome] += 1
        ok = next(syncs, None)
        if ok is None:
            continue
        sent = health.take_pending()
        if ok:
            for i, bucket in sent.items():
                remote.setdefault(i, Bucket()).add(bucket)
            merged = {i: Bucket.from_fields(b.fields()) for i, b in remote.items()}
            health.sync_done(merged, now_index=8)
        else:
            health.sync_failed()
    seen = health.total(range(9))
    assert seen.outcomes == total.outcomes
