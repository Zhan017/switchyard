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

    def render(self) -> bytes:
        return generate_latest(self.registry)
