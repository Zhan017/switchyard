"""Judge a scenario run against its checks, and write the JSON summary and Markdown report.

Shares (success, fast failure) pool every seed's requests and pass when the lower bound
of the 95% Wilson interval clears the target. Times (breaker opening and closing,
recovery) are judged on the worst seed. A run with too few requests for a perfect result
to clear the target is inconclusive, not a fail.
"""

from __future__ import annotations

import math
from typing import Any

from chaos.runner import Result, SeedRun
from chaos.scenario import Check, Scenario
from chaos.stats import Verdict, verdict_at_least, wilson

ORDER: dict[Verdict, int] = {"fail": 0, "inconclusive": 1, "pass": 2, "n/a": 3}


def in_window(scenario: Scenario, check: Check, result: Result) -> bool:
    end = check.window.to_s if check.window.to_s is not None else scenario.traffic.duration_s
    return check.window.from_s <= result.at_s < end


def first_time(
    samples: list[tuple[float, dict[str, str]]], model: str, state: str, after: float
) -> float | None:
    for at, states in samples:
        if at >= after and states.get(model) == state:
            return at
    return None


def evaluate_check(scenario: Scenario, check: Check, runs: list[SeedRun]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kind": check.kind,
        "description": check.description,
        "target": check.target,
        "window": check.window.model_dump(),
    }
    if check.kind in ("success_rate", "fast_failure"):
        hits = total = 0
        per_seed = []
        for run in runs:
            chosen = [r for r in run.results if in_window(scenario, check, r)]
            if check.kind == "success_rate":
                good = sum(r.ok for r in chosen)
            else:
                assert check.max_s is not None
                good = sum(
                    r.status == 503 and (r.latency_s or math.inf) <= check.max_s for r in chosen
                )
            hits += good
            total += len(chosen)
            per_seed.append({"seed": run.seed, "hits": good, "total": len(chosen)})
        interval = wilson(hits, total)
        assert check.min is not None
        out.update(
            measured=f"{interval.rate:.2%} ({hits} of {total}; 95% interval "
            f"{interval.low:.2%} to {interval.high:.2%})",
            interval={"hits": hits, "total": total, "low": interval.low, "high": interval.high},
            per_seed=per_seed,
            verdict=verdict_at_least(interval, check.min),
        )
        return out
    if check.kind == "attempt_ratio":
        # Prefer the gateway's own count: a connection the mock refused never reaches it.
        by_gateway = all(run.gateway_attempts is not None for run in runs)

        def count(run: SeedRun) -> int:
            if by_gateway and run.gateway_attempts is not None:
                return run.gateway_attempts
            return sum(run.upstream_requests.values())

        attempts = sum(count(run) for run in runs)
        requests = sum(len(run.results) for run in runs)
        ratio = attempts / requests if requests else math.inf
        source = (
            "counted by the gateway"
            if by_gateway
            else "counted at the mock servers, which can't see refused connections"
        )
        assert check.max is not None
        out.update(
            measured=f"{ratio:.3f}x ({attempts} upstream attempts for {requests} requests, "
            f"{source})",
            ratio=ratio,
            per_seed=[
                {"seed": run.seed, "ratio": count(run) / max(1, len(run.results))} for run in runs
            ],
            verdict="pass" if ratio <= check.max else "fail",
        )
        return out
    if check.kind == "recovery":
        end = scenario.fault_end
        times = []
        for run in runs:
            late = [r.at_s - end for r in run.results if not r.ok and r.at_s >= end]
            times.append(max(late) if late else 0.0)
        worst = max(times)
        assert check.max_s is not None
        out.update(
            measured=f"{worst:.1f} s on the worst seed (last failed request after the outage)",
            per_seed=[{"seed": run.seed, "s": t} for run, t in zip(runs, times, strict=True)],
            verdict="pass" if worst <= check.max_s else "fail",
        )
        return out
    # Breaker checks need Switchyard's admin API.
    if not all(run.observer for run in runs):
        out.update(measured="no admin API: not Switchyard, or no admin key", verdict="n/a")
        return out
    assert check.model is not None
    times_or_none: list[float | None] = []
    for run in runs:
        if check.kind == "breaker_opens":
            at = first_time(run.breakers, check.model, "open", scenario.fault_start)
            ok_at = None if at is None or at > scenario.fault_end else at - scenario.fault_start
        else:
            at = first_time(run.breakers, check.model, "closed", scenario.fault_end)
            ok_at = None if at is None else at - scenario.fault_end
        times_or_none.append(ok_at)
    missing = any(t is None for t in times_or_none)
    worst = max((t for t in times_or_none if t is not None), default=math.inf)
    limit = check.max_s if check.max_s is not None else math.inf
    verb = "opened" if check.kind == "breaker_opens" else "closed"
    if missing:
        measured = f"never {verb} on at least one seed"
    else:
        measured = f"{verb} after {worst:.2f} s on the worst seed (polled every 0.25 s)"
    out.update(
        measured=measured,
        per_seed=[{"seed": run.seed, "s": t} for run, t in zip(runs, times_or_none, strict=True)],
        verdict="fail" if missing or worst > limit else "pass",
    )
    return out


def evaluate(scenario: Scenario, runs: list[SeedRun]) -> dict[str, Any]:
    checks = [evaluate_check(scenario, check, runs) for check in scenario.checks]
    verdicts = [c["verdict"] for c in checks if c["verdict"] != "n/a"]
    invalid = [e for run in runs for e in run.fault_errors]
    unreachable = all(r.status == 0 for run in runs for r in run.results)
    if invalid or unreachable:
        overall = "invalid"
    elif not verdicts:
        overall = "n/a"
    else:
        overall = min(verdicts, key=lambda v: ORDER[v])
    return {
        "scenario": scenario.model_dump(),
        "verdict": overall,
        "invalid": invalid + (["the gateway was unreachable"] if unreachable else []),
        "checks": checks,
        "seeds": [_seed_summary(scenario, run) for run in runs],
    }


def _seed_summary(scenario: Scenario, run: SeedRun) -> dict[str, Any]:
    results = run.results
    ok = sum(r.ok for r in results)
    errors: dict[str, int] = {}
    for r in results:
        if not r.ok:
            errors[r.error or f"HTTP {r.status}"] = errors.get(r.error or f"HTTP {r.status}", 0) + 1
    failed_latency = sorted(r.latency_s or 0.0 for r in results if not r.ok)
    return {
        **run.to_json(),
        "requests": len(results),
        "succeeded": ok,
        "errors": errors,
        "failed_latency_p50_s": _pct(failed_latency, 50),
        "failed_latency_max_s": failed_latency[-1] if failed_latency else None,
        "with_failover": sum(1 for r in results if (r.failovers or 0) > 0),
    }


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    index = min(len(values) - 1, max(0, math.ceil(p / 100 * len(values)) - 1))
    return values[index]


def markdown(summary: dict[str, Any], environment: dict[str, Any]) -> str:
    scenario = Scenario.model_validate(summary["scenario"])
    seeds = summary["seeds"]
    total = sum(s["requests"] for s in seeds)
    lines = [
        f"# {scenario.id}: {scenario.title}",
        "",
        f"Verdict: **{summary['verdict']}** · {len(seeds)} seeds · {total:,} requests · "
        f"{environment.get('date', '')} · gateway {environment.get('gateway', '')}",
        "",
        scenario.description.strip(),
        "",
    ]
    if summary["invalid"]:
        lines += ["**Invalid run:** " + "; ".join(summary["invalid"]), ""]
    lines += ["| Check | Target | Measured | Verdict |", "| --- | --- | --- | --- |"]
    for check in summary["checks"]:
        window = check["window"]
        scope = ""
        if window["from_s"] or window["to_s"] is not None:
            to = window["to_s"] if window["to_s"] is not None else scenario.traffic.duration_s
            scope = f" (requests sent {window['from_s']:g} to {to:g} s)"
        lines.append(
            f"| {check['description']}{scope} | {check['target']} | {check['measured']} "
            f"| {check['verdict']} |"
        )
    lines += [
        "",
        "## Per seed",
        "",
        "| Seed | Requests | Succeeded | Attempts seen by the mocks | Attempts counted by the "
        "gateway | Probes seen by the mocks | Failed requests: p50, max latency | Errors |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for seed in seeds:
        attempts = sum(seed["upstream_requests"].values())
        probes = sum(seed["upstream_probes"].values())
        latency = "none failed"
        if seed["failed_latency_p50_s"] is not None:
            latency = f"{seed['failed_latency_p50_s']:.2f} s, {seed['failed_latency_max_s']:.2f} s"
        errors = ", ".join(f"{k}: {v}" for k, v in sorted(seed["errors"].items())) or "none"
        by_gateway = seed.get("gateway_attempts")
        gateway = "n/a" if by_gateway is None else str(by_gateway)
        lines.append(
            f"| {seed['seed']} | {seed['requests']} | {seed['succeeded']} | {attempts} "
            f"| {gateway} | {probes} | {latency} | {errors} |"
        )
    traffic = scenario.traffic
    lines += [
        "",
        "## Setup",
        "",
        f"- Traffic: {traffic.rate_rps:g} requests a second for {traffic.duration_s:g} s, open "
        f"loop, to `{traffic.model}`; {traffic.stream_share:.0%} streaming; `max_tokens` drawn "
        f"from {traffic.max_tokens}.",
    ]
    for name, description in scenario.topology.items():
        lines.append(f"- {name}: {description}")
    for fault in scenario.faults:
        params = ", ".join(f"{k}={v}" for k, v in fault.params.items())
        lines.append(
            f"- Fault: `{fault.mode}`{f' ({params})' if params else ''} on {fault.upstream} "
            f"from {fault.start_s:g} s to {fault.end_s:g} s."
        )
    lines += ["", "## Environment", ""]
    lines += [f"- {key}: {value}" for key, value in environment.items()]
    lines.append("")
    return "\n".join(lines)


def table(summaries: list[dict[str, Any]]) -> str:
    """One table across several runs, for the README."""
    lines = [
        "| Scenario | Check | Target | Measured | Verdict |",
        "| --- | --- | --- | --- | --- |",
    ]
    for summary in summaries:
        scenario = summary["scenario"]
        for check in summary["checks"]:
            lines.append(
                f"| {scenario['id']} {scenario['title']} | {check['description']} "
                f"| {check['target']} | {check['measured']} | {check['verdict']} |"
            )
    return "\n".join(lines) + "\n"
