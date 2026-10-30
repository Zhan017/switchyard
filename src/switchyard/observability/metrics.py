"""Prometheus metrics. Labels are bounded: tenants and features come from config, and an
unknown feature is recorded as "other", so a buggy client can't create unbounded series."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89)
OVERHEAD_BUCKETS = (
    0.0001,
    0.00025,
    0.0005,
    0.001,
    0.002,
    0.003,
    0.005,
    0.0075,
    0.01,
    0.015,
    0.025,
    0.05,
    0.1,
)


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        r = self.registry
        self.requests = Counter(
            "switchyard_requests_total",
            "Requests by outcome",
            ["tenant", "feature", "route_group", "status"],
            registry=r,
        )
        self.attempts = Counter(
            "switchyard_attempts_total",
            "Attempts against providers by outcome",
            ["provider", "model", "role", "outcome"],
            registry=r,
        )
        self.ttft = Histogram(
            "switchyard_ttft_seconds",
            "Upstream time to first token",
            ["provider"],
            buckets=LATENCY_BUCKETS,
            registry=r,
        )
        self.request_seconds = Histogram(
            "switchyard_request_seconds",
            "Total request time seen by the gateway",
            ["provider"],
            buckets=LATENCY_BUCKETS,
            registry=r,
        )
        self.overhead = Histogram(
            "switchyard_gateway_overhead_seconds",
            "Time the gateway adds: before the first byte when streaming, end to end otherwise",
            ["mode"],
            buckets=OVERHEAD_BUCKETS,
            registry=r,
        )
        self.tokens = Counter(
            "switchyard_tokens_total",
            "Tokens by direction",
            ["tenant", "feature", "provider", "model", "direction", "estimated"],
            registry=r,
        )
        self.cost = Counter(
            "switchyard_cost_usd_total",
            "Cost in USD",
            ["tenant", "feature", "provider", "model", "cost_class", "estimated"],
            registry=r,
        )
        self.in_flight = Gauge("switchyard_requests_in_flight", "Requests being served", registry=r)
        self.breaker_state = Gauge(
            "switchyard_breaker_state",
            "Breaker state per provider and model: 0 closed, 1 open",
            ["provider_model"],
            registry=r,
        )
        self.breaker_transitions = Counter(
            "switchyard_breaker_transitions_total",
            "Breaker transitions",
            ["provider_model", "to"],
            registry=r,
        )
        self.cooldowns = Counter(
            "switchyard_cooldowns_total",
            "Cooldowns started: rate_limited for a 429, auth for a rejected provider key",
            ["provider_model", "reason"],
            registry=r,
        )
        self.skipped = Counter(
            "switchyard_candidates_skipped_total",
            "Candidates skipped while routing, because the breaker is open or in cooldown",
            ["provider_model", "reason"],
            registry=r,
        )
        self.failovers = Counter(
            "switchyard_failovers_total",
            "Moves to another model; phase is before_first_token or midstream",
            ["from", "to", "phase", "strategy"],
            registry=r,
        )
        self.seam_repair = Counter(
            "switchyard_seam_repair_total",
            "Seam repair fixes applied to mid-stream continuations, by action",
            ["action"],
            registry=r,
        )
        self.retry_budget_exhausted = Counter(
            "switchyard_retry_budget_exhausted_total",
            "Same-provider retries refused by the retry budget",
            ["provider_model"],
            registry=r,
        )
        self.records_dropped = Counter(
            "switchyard_records_dropped_total",
            "Request, attempt and breaker records not written to Postgres",
            ["table"],
            registry=r,
        )
        self.shared_state_up = Gauge(
            "switchyard_shared_state_up",
            "1 while the last sync with Redis succeeded, 0 while deciding from local state",
            registry=r,
        )
        self.shared_state_errors = Counter(
            "switchyard_shared_state_errors_total", "Failed syncs with Redis", registry=r
        )

    def render(self) -> bytes:
        return generate_latest(self.registry)
