"""Config, pricing, usage estimates, request parsing and routing."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from switchyard.accounting.pricing import load_prices
from switchyard.accounting.usage import complete_usage, estimate_tokens
from switchyard.api.schemas import InvalidRequest, parse_body, to_chat_request
from switchyard.config import Settings, expand_env, hash_key, load_settings
from switchyard.core.router import RouteError, capable_candidates, resolve
from switchyard.core.types import ChatRequest, Message, ResponseFormat, ToolSpec, Usage

ROOT = Path(__file__).parent.parent.parent
FIXTURES = Path(__file__).parent.parent / "fixtures"


def raw_config() -> dict[str, Any]:
    text = expand_env((FIXTURES / "config.yaml").read_text().replace("${MOCKLLM_URL}", "http://m"))
    data: dict[str, Any] = yaml.safe_load(text)
    return data


# Config


def test_shipped_config_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load_settings(ROOT / "config" / "switchyard.yaml")
    assert "chat-fast" in settings.route_groups
    assert settings.tenant_for_key("sy-dev-local-key") is not None
    assert settings.is_admin_key("sy-admin-local-key")
    prices = load_prices(ROOT / "config" / "prices.yaml")
    for key in settings.models:
        assert key in prices.models, f"{key} has no price"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda c: c["models"].update({"nope/x": {}}), "unknown provider"),
        (lambda c: c["route_groups"]["mock"]["models"].append("mock/zzz"), "unknown model"),
        (lambda c: c["tenants"].append(dict(c["tenants"][0])), "unique"),
        (lambda c: c["models"].update({"mock": {}}), "provider/model"),
        (lambda c: c["models"]["mock/mock-a"].update({"typo_field": 1}), "Extra inputs"),
    ],
)
def test_bad_config_is_refused(change: Any, message: str) -> None:
    config = raw_config()
    change(config)
    with pytest.raises(ValidationError, match=message):
        Settings.model_validate(config)


def test_expand_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SY_TEST_URL", "http://x")
    assert expand_env("${SY_TEST_URL}/v1 ${SY_MISSING:-dflt}") == "http://x/v1 dflt"
    with pytest.raises(ValueError, match="SY_MISSING"):
        expand_env("${SY_MISSING}")


def test_keys_are_hashed() -> None:
    assert hash_key("sy-dev-local-key").startswith("5cbec0ae")


# Pricing

PRICES = load_prices(ROOT / "config" / "prices.yaml")
TUESDAY_PEAK = datetime(2026, 10, 27, 7, 0, tzinfo=UTC)
TUESDAY_OFF_PEAK = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
SATURDAY_PEAK_HOUR = datetime(2026, 10, 31, 7, 0, tzinfo=UTC)


def test_cost_with_cached_input() -> None:
    usage = Usage(input_tokens=50_000, output_tokens=10_000, cached_input_tokens=20_000)
    # 30K uncached at 0.10, 20K cached at 0.01, 10K output at 0.50, per million
    expected = (30_000 * 0.10 + 20_000 * 0.01 + 10_000 * 0.50) / 1e6
    assert PRICES.cost("anthropic/claude-haiku-5-5", usage) == pytest.approx(expected)


def test_long_context_tier_applies_to_every_token() -> None:
    usage = Usage(input_tokens=200_000, output_tokens=1_000)
    expected = (200_000 * 0.50 + 1_000 * 2.50) / 1e6
    assert PRICES.cost("anthropic/claude-haiku-5-5", usage) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("at", "output_rate"),
    [(TUESDAY_PEAK, 1.20), (TUESDAY_OFF_PEAK, 0.60), (SATURDAY_PEAK_HOUR, 0.60)],
)
def test_deepseek_peak_and_off_peak(at: datetime, output_rate: float) -> None:
    usage = Usage(input_tokens=0, output_tokens=1_000_000)
    assert PRICES.cost("deepseek/deepseek-flash", usage, at) == pytest.approx(output_rate)


def test_unknown_model_has_no_cost() -> None:
    assert PRICES.cost("nobody/nothing", Usage(10, 10)) is None


# Usage estimates

REQ = ChatRequest(messages=(Message("user", "x" * 350),), max_tokens=10)


def test_reported_usage_is_kept() -> None:
    assert complete_usage(Usage(10, 20), REQ, "text") == Usage(10, 20)


def test_missing_usage_is_estimated_and_flagged() -> None:
    usage = complete_usage(None, REQ, "y" * 70)
    assert usage.estimated
    assert usage.output_tokens == estimate_tokens("y" * 70) == 23  # 70 / 3.5 * 1.15
    assert usage.input_tokens > 100


def test_partial_usage_keeps_exact_input() -> None:
    usage = complete_usage(Usage(42, 0, estimated=True), REQ, "y" * 35)
    assert usage.input_tokens == 42 and usage.output_tokens == 12 and usage.estimated


# Request parsing


def parse(**body: Any) -> ChatRequest:
    base: dict[str, Any] = {"model": "mock", "messages": [{"role": "user", "content": "hi"}]}
    base.update(body)
    return to_chat_request(parse_body(base), default_max_tokens=64)


def test_defaults_and_max_tokens() -> None:
    assert parse().max_tokens == 64
    assert parse(max_tokens=10).max_tokens == 10
    assert parse(max_tokens=10, max_completion_tokens=20).max_tokens == 20


def test_content_parts_and_developer_role() -> None:
    req = parse(
        messages=[
            {"role": "developer", "content": "rules"},
            {
                "role": "user",
                "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
            },
        ]
    )
    assert req.messages == (Message("system", "rules"), Message("user", "ab"))


def test_tools_choice_and_json() -> None:
    req = parse(
        tools=[{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}],
        tool_choice={"type": "function", "function": {"name": "f"}},
        response_format={"type": "json_schema", "json_schema": {"name": "o", "schema": {}}},
        stop="END",
    )
    assert req.tools == (ToolSpec("f", None, {"type": "object"}),)
    assert req.tool_choice == "f"
    assert req.response_format == ResponseFormat("json_schema", {}, "o", None)
    assert req.stop == ("END",)
    assert parse(response_format={"type": "text"}).response_format is None


@pytest.mark.parametrize(
    "body",
    [
        {"n": 2},
        {"logprobs": True},
        {"modalities": ["text", "audio"]},
        {"messages": [{"role": "user", "content": [{"type": "image_url"}]}]},
        {"messages": [{"role": "tool", "content": "x"}]},
        {"messages": []},
        {"tool_choice": "sometimes"},
        {"tool_choice": {"type": "function", "function": "now"}},
        {"temperature": 5},
        {"response_format": {"type": "json_schema"}},
    ],
)
def test_unsupported_requests_are_rejected(body: dict[str, Any]) -> None:
    with pytest.raises(InvalidRequest):
        parse(**body)


# Routing


def settings() -> Settings:
    return Settings.model_validate(raw_config())


def test_route_group_and_concrete_model() -> None:
    route = resolve(settings(), "mock")
    assert [c.key for c in route.candidates] == ["mock/mock-a", "mock/mock-b"]
    assert route.limits.ttft_timeout_s == 1.0
    concrete = resolve(settings(), "mock/mock-b")
    assert [c.key for c in concrete.candidates] == ["mock/mock-b"]


def test_unknown_model_is_404() -> None:
    with pytest.raises(RouteError) as caught:
        resolve(settings(), "gpt-9")
    assert caught.value.status == 404


def test_capabilities_filter_candidates() -> None:
    route = resolve(settings(), "mock")
    tools = ChatRequest(messages=REQ.messages, max_tokens=5, tools=(ToolSpec("f", None, {}),))
    assert [c.key for c in capable_candidates(route, tools)] == ["mock/mock-a"]
    plain = resolve(settings(), "plain")
    with pytest.raises(RouteError) as caught:
        capable_candidates(plain, tools)
    assert caught.value.status == 400 and "tools" in caught.value.message


# Fixes from the week 2 review


def test_json_modes_default_off_where_the_provider_type_lacks_them() -> None:
    config = raw_config()
    config["providers"]["ds"] = {"type": "deepseek", "base_url": "https://d"}
    config["models"].update(
        {"anthropic/plain": {}, "ds/plain": {}, "anthropic/forced": {"json_object": True}}
    )
    settings = Settings.model_validate(config)
    assert settings.models["anthropic/plain"].json_object is False
    assert settings.models["anthropic/plain"].json_schema is True
    assert settings.models["ds/plain"].json_schema is False
    assert settings.models["anthropic/forced"].json_object is True  # explicit wins


def test_models_without_a_key_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    config = raw_config()
    config["route_groups"]["mixed"] = {"models": ["anthropic/claude-test", "mock/mock-a"]}
    settings = Settings.model_validate(config)
    monkeypatch.delenv("TEST_ANTHROPIC_KEY", raising=False)
    route = resolve(settings, "mixed")
    assert [c.key for c in capable_candidates(route, REQ)] == ["mock/mock-a"]
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "k")
    route = resolve(settings, "mixed")
    assert capable_candidates(route, REQ)[0].key == "anthropic/claude-test"


def test_no_key_anywhere_is_a_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_ANTHROPIC_KEY", raising=False)
    with pytest.raises(RouteError) as caught:
        capable_candidates(resolve(settings(), "anthropic/claude-test"), REQ)
    assert caught.value.status == 503 and caught.value.code == "provider_not_configured"


def test_a_model_without_a_price_is_refused(tmp_path: Path) -> None:
    import asyncio

    import aiohttp

    from switchyard.api.state import load_state

    config = raw_config()
    config["models"]["mock/unpriced"] = {}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
    (tmp_path / "prices.yaml").write_text((FIXTURES / "prices.yaml").read_text())

    async def go() -> None:
        async with aiohttp.ClientSession() as http:
            load_state(tmp_path / "config.yaml", http)

    with pytest.raises(ValueError, match="mock/unpriced"):
        asyncio.run(go())


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("7", 7.0),
        ("0.5", 0.5),
        ("inf", None),
        ("1e400", None),
        ("nan", None),
        ("99999", 3600.0),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),
        ("-3", 0.0),
    ],
)
def test_retry_after_parsing(value: str, seconds: float | None) -> None:
    from switchyard.providers.errors import parse_retry_after

    assert parse_retry_after(value) == seconds
