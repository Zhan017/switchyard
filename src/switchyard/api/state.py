"""Everything a request needs, built from config. Reloading swaps the whole object, so a
request in flight keeps the config it started with."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import aiohttp

from switchyard.accounting.pricing import PriceTable, load_prices
from switchyard.config import ProviderConfig, Settings, load_settings
from switchyard.providers.anthropic import AnthropicAdapter
from switchyard.providers.base import Adapter
from switchyard.providers.openai import DeepSeekAdapter, OpenAIAdapter

ADAPTERS: dict[str, type[Adapter]] = {
    "openai": OpenAIAdapter,
    "anthropic": AnthropicAdapter,
    "deepseek": DeepSeekAdapter,
}


@dataclass(frozen=True)
class GatewayState:
    settings: Settings
    prices: PriceTable
    adapters: dict[str, Adapter]


def build_adapter(provider_id: str, config: ProviderConfig, http: aiohttp.ClientSession) -> Adapter:
    return ADAPTERS[config.type](provider_id, config, http)


def build_state(
    settings: Settings, prices: PriceTable, http: aiohttp.ClientSession
) -> GatewayState:
    adapters = {pid: build_adapter(pid, cfg, http) for pid, cfg in settings.providers.items()}
    return GatewayState(settings=settings, prices=prices, adapters=adapters)


def load_state(config_path: Path, http: aiohttp.ClientSession) -> GatewayState:
    settings = load_settings(config_path)
    prices_path = Path(settings.prices_file)
    if not prices_path.is_absolute():
        prices_path = config_path.parent / prices_path
    prices = load_prices(prices_path)
    unpriced = sorted(key for key in settings.models if key not in prices.models)
    if unpriced:
        # Better to refuse the config than to report these models as free.
        raise ValueError(f"no price in {prices_path.name} for: {', '.join(unpriced)}")
    return build_state(settings, prices, http)
