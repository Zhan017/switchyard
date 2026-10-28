"""Turn the requested model name into an ordered list of candidate models."""

from __future__ import annotations

from dataclasses import dataclass

from switchyard.config import ModelConfig, RouteGroup, Settings
from switchyard.core.types import ChatRequest


class RouteError(Exception):
    def __init__(self, message: str, status: int, code: str) -> None:
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


@dataclass(frozen=True)
class Candidate:
    key: str  # provider/model
    provider: str
    model: str
    options: ModelConfig
    has_key: bool = True  # False when the provider needs an API key that isn't set


@dataclass(frozen=True)
class Route:
    alias: str  # what the client asked for, shown back as the response's model
    limits: RouteGroup
    candidates: tuple[Candidate, ...]


def resolve(settings: Settings, name: str) -> Route:
    """A route group alias, or a concrete provider/model that is in the config."""
    if name in settings.route_groups:
        group = settings.route_groups[name]
        keys = group.models
    elif name in settings.models:
        group = RouteGroup(models=[name])
        keys = [name]
    else:
        raise RouteError(f"The model '{name}' does not exist", 404, "model_not_found")
    candidates = []
    for key in keys:
        provider, model = settings.provider_of(key)
        config = settings.providers[provider]
        has_key = config.api_key_env is None or bool(config.api_key())
        candidates.append(Candidate(key, provider, model, settings.models[key], has_key))
    return Route(alias=name, limits=group, candidates=tuple(candidates))


def missing_capability(candidate: Candidate, request: ChatRequest) -> str | None:
    options = candidate.options
    if request.tools and not options.tools:
        return "tools"
    fmt = request.response_format
    if fmt is not None:
        if fmt.type == "json_object" and not options.json_object:
            return "json_object"
        if fmt.type == "json_schema" and not options.json_schema:
            return "json_schema"
    return None


def capable_candidates(route: Route, request: ChatRequest) -> list[Candidate]:
    """Candidates that can serve this request, in order. Raises if none can.

    A model whose provider key isn't set is skipped, so a route group works with whichever
    provider keys are configured."""
    capable = [c for c in route.candidates if missing_capability(c, request) is None]
    if not capable:
        needs = {missing_capability(c, request) for c in route.candidates} - {None}
        raise RouteError(
            f"No model in '{route.alias}' supports {', '.join(sorted(n for n in needs if n))}",
            400,
            "unsupported_feature",
        )
    ready = [c for c in capable if c.has_key]
    if not ready:
        providers = ", ".join(sorted({c.provider for c in capable}))
        raise RouteError(
            f"No API key is configured for the providers that can serve this: {providers}",
            503,
            "provider_not_configured",
        )
    return ready
