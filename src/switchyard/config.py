"""Gateway configuration: providers, models, route groups and tenants, from one YAML file.

The file is validated as a whole before it is used, so a bad edit is refused instead of
half-applied. See config/switchyard.yaml for a commented example.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_CONFIG = Path("config/switchyard.yaml")


# Capabilities each provider type lacks: Anthropic takes JSON only with a schema, and
# DeepSeek only without one.
UNSUPPORTED_BY_TYPE: dict[str, tuple[str, ...]] = {
    "openai": (),
    "anthropic": ("json_object",),
    "deepseek": ("json_schema",),
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderConfig(_Strict):
    type: Literal["openai", "anthropic", "deepseek"]
    base_url: str
    api_key_env: str | None = None
    # Extra headers sent on every request, for example an organization id.
    headers: dict[str, str] = Field(default_factory=dict)

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None


class ModelConfig(_Strict):
    tools: bool = True
    json_object: bool = True
    json_schema: bool = True
    # The newest Claude models reject temperature and top_p; the adapter drops them.
    sampling_params: bool = True
    continuation: Literal["native", "instructed", "none"] = "instructed"
    # Merged into every request body for this model, for example to turn thinking off.
    request_overrides: dict[str, Any] = Field(default_factory=dict)


class RouteGroup(_Strict):
    models: list[str] = Field(min_length=1)
    default_max_tokens: int = Field(default=1024, gt=0)
    request_deadline_s: float = Field(default=120.0, gt=0)
    ttft_timeout_s: float = Field(default=5.0, gt=0)
    stall_timeout_s: float = Field(default=5.0, gt=0)


class Tenant(_Strict):
    name: str
    key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    features: list[str] = Field(default_factory=list)


class Settings(_Strict):
    providers: dict[str, ProviderConfig]
    models: dict[str, ModelConfig]
    route_groups: dict[str, RouteGroup]
    tenants: list[Tenant]
    admin_key_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    prices_file: str = "prices.yaml"  # relative to the config file

    @model_validator(mode="after")
    def _check_references(self) -> Settings:
        for key, options in list(self.models.items()):
            provider, _, model = key.partition("/")
            if not model:
                raise ValueError(f"model '{key}' must be written as provider/model")
            if provider not in self.providers:
                raise ValueError(f"model '{key}' uses unknown provider '{provider}'")
            # JSON modes a provider type never supports default to off, so requests that
            # need them are routed elsewhere instead of being sent and rejected.
            unsupported = UNSUPPORTED_BY_TYPE[self.providers[provider].type]
            defaults = {flag: False for flag in unsupported if flag not in options.model_fields_set}
            if defaults:
                self.models[key] = options.model_copy(update=defaults)
        for name, group in self.route_groups.items():
            if "/" in name:
                raise ValueError(f"route group '{name}' must not contain '/'")
            for key in group.models:
                if key not in self.models:
                    raise ValueError(f"route group '{name}' uses unknown model '{key}'")
        names = [t.name for t in self.tenants]
        if len(set(names)) != len(names):
            raise ValueError("tenant names must be unique")
        keys = [t.key_sha256 for t in self.tenants]
        if len(set(keys)) != len(keys):
            raise ValueError("tenant keys must be unique")
        return self

    def tenant_for_key(self, api_key: str) -> Tenant | None:
        digest = hash_key(api_key)
        return next((t for t in self.tenants if t.key_sha256 == digest), None)

    def is_admin_key(self, api_key: str) -> bool:
        return self.admin_key_sha256 is not None and hash_key(api_key) == self.admin_key_sha256

    def provider_of(self, model_key: str) -> tuple[str, str]:
        provider, _, model = model_key.partition("/")
        return provider, model


# Hashes of the development keys in the shipped config (sy-dev-local-key and
# sy-admin-local-key). The gateway warns at startup while either is configured.
DEV_KEY_HASHES = frozenset(
    {
        "5cbec0ae7e5d32e771ce65b153958d2da52b3f702fdc2867bae3de9e66b8ea9b",
        "7f9ce6a78a65dd67a21999c968266dc87302d59ece921c36a7bf82373df37ee8",
    }
)


def uses_dev_keys(settings: Settings) -> bool:
    hashes = {t.key_sha256 for t in settings.tenants} | {settings.admin_key_sha256}
    return bool(hashes & DEV_KEY_HASHES)


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


_ENV_REF = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")


def expand_env(text: str) -> str:
    """Replace ``${NAME}`` and ``${NAME:-default}`` with environment values."""

    def value(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        raise ValueError(f"config uses ${{{name}}}, which is not set and has no default")

    return _ENV_REF.sub(value, text)


def load_settings(path: Path | None = None) -> Settings:
    path = path or Path(os.environ.get("SWITCHYARD_CONFIG", DEFAULT_CONFIG))
    raw = yaml.safe_load(expand_env(path.read_text()))
    return Settings.model_validate(raw)


def main() -> None:
    """``python -m switchyard.config hash <key>`` prints the hash to put in the config."""
    import sys

    if len(sys.argv) == 3 and sys.argv[1] == "hash":
        print(hash_key(sys.argv[2]))
        return
    print("usage: python -m switchyard.config hash <api-key>", file=sys.stderr)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
