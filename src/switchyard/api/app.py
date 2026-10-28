"""The FastAPI application."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiohttp
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from switchyard import __version__
from switchyard.api.chat import RequestContext, handle_chat, new_request_id
from switchyard.api.errors import error_response
from switchyard.api.schemas import InvalidRequest
from switchyard.api.state import GatewayState, Services, load_state
from switchyard.config import DEFAULT_CONFIG, Tenant, uses_dev_keys
from switchyard.core.router import RouteError
from switchyard.observability.logging import configure_logging
from switchyard.observability.metrics import Metrics
from switchyard.resilience.breaker import Breakers, Transition
from switchyard.resilience.prober import Prober
from switchyard.resilience.shared import SharedState
from switchyard.storage.records import NoRecords, PostgresRecords, Records

log = logging.getLogger("switchyard")

# Read timeouts are enforced per request by the stream watcher, not by the HTTP client.
UPSTREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=5.0, sock_connect=5.0)


def upstream_session() -> aiohttp.ClientSession:
    """aiohttp, not httpx: on identical streams it used about a twelfth of the CPU
    (decision 4). trust_env stays off: with it on, aiohttp re-reads proxy and netrc
    settings on every request, which took a fifth of the gateway's CPU under load."""
    connector = aiohttp.TCPConnector(limit=1000, ttl_dns_cache=300)
    return aiohttp.ClientSession(connector=connector, timeout=UPSTREAM_TIMEOUT)


def create_app(
    config_path: Path | None = None, *, http: aiohttp.ClientSession | None = None
) -> FastAPI:
    """``http`` lets tests route upstream calls to an in-process mock provider."""
    path = config_path or Path(os.environ.get("SWITCHYARD_CONFIG", DEFAULT_CONFIG))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = http or upstream_session()
        app.state.http = client
        state: GatewayState = load_state(path, client)
        app.state.gateway = state
        if uses_dev_keys(state.settings):
            log.warning(
                "The config uses the development keys from the repository. Replace them "
                "before exposing this gateway: anyone with them can spend your provider keys."
            )
        metrics: Metrics = app.state.metrics
        breakers = Breakers(state.settings.resilience)
        breakers.retain(state.settings.models)
        records: Records = NoRecords()
        if database_url := os.environ.get("SWITCHYARD_DATABASE_URL"):
            records = PostgresRecords(database_url, metrics)
        _watch_breakers(breakers, metrics, records)
        shared = None
        if redis_url := os.environ.get("SWITCHYARD_REDIS_URL"):
            shared = SharedState.from_url(redis_url, breakers, metrics)
        else:
            metrics.shared_state_up.set(0)
        prober = Prober(breakers, lambda: app.state.gateway, metrics, shared)
        app.state.services = Services(breakers, metrics, records)
        app.state.shared = shared
        await records.start()
        if shared is not None:
            await shared.start()
        await prober.start()
        try:
            yield
        finally:
            await prober.stop()
            if shared is not None:
                await shared.stop()
            await records.stop()
            if http is None:
                await client.close()

    configure_logging(os.environ.get("SWITCHYARD_LOG_LEVEL", "INFO"))
    app = FastAPI(title="Switchyard", version=__version__, lifespan=lifespan)
    app.state.metrics = Metrics()
    app.state.config_path = path

    def gateway(request: Request) -> GatewayState:
        state: GatewayState = request.app.state.gateway
        return state

    def services(request: Request) -> Services:
        found: Services = request.app.state.services
        return found

    def bearer(request: Request) -> str:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        return token.strip() if scheme.lower() == "bearer" else ""

    def tenant_of(request: Request) -> Tenant | None:
        return gateway(request).settings.tenant_for_key(bearer(request))

    @app.post("/v1/chat/completions", response_model=None)
    async def chat_completions(request: Request) -> Response:
        state = gateway(request)
        tenant = tenant_of(request)
        if tenant is None:
            return error_response(
                401, "Invalid API key", "invalid_api_key", error_type="invalid_request_error"
            )
        feature = request.headers.get("x-switchyard-feature", "").strip()
        if not feature:
            return error_response(
                400,
                "The X-Switchyard-Feature header is required, for cost attribution",
                "missing_feature",
                error_type="invalid_request_error",
            )
        if feature not in tenant.features:
            feature = "other"
        ctx = RequestContext(
            request_id=new_request_id(request.headers.get("x-request-id")),
            tenant=tenant,
            feature=feature,
        )
        try:
            raw: Any = await request.json()
        except ValueError:
            return error_response(
                400,
                "The body is not valid JSON",
                "invalid_json",
                error_type="invalid_request_error",
            )
        try:
            response = await handle_chat(raw, ctx, state, services(request))
        except InvalidRequest as error:
            response = error_response(
                400,
                error.message,
                "invalid_request",
                error_type="invalid_request_error",
                param=error.param,
            )
        except RouteError as error:
            response = error_response(
                error.status, error.message, error.code, error_type="invalid_request_error"
            )
        response.headers["x-request-id"] = ctx.request_id
        return response

    @app.get("/v1/models", response_model=None)
    async def list_models(request: Request) -> Response:
        if tenant_of(request) is None:
            return error_response(
                401, "Invalid API key", "invalid_api_key", error_type="invalid_request_error"
            )
        settings = gateway(request).settings
        names = [*settings.route_groups, *settings.models]
        return JSONResponse(
            {
                "object": "list",
                "data": [{"id": n, "object": "model", "owned_by": "switchyard"} for n in names],
            }
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request) -> dict[str, str]:
        gateway(request)
        return {"status": "ready"}

    @app.get("/metrics")
    async def metrics_endpoint(request: Request) -> Response:
        metrics: Metrics = request.app.state.metrics
        return Response(metrics.render(), media_type="text/plain; version=0.0.4")

    def is_admin(request: Request) -> bool:
        return gateway(request).settings.is_admin_key(bearer(request))

    @app.get("/admin/providers", response_model=None)
    async def providers(request: Request) -> Response:
        if not is_admin(request):
            return error_response(401, "Admin key required", "invalid_admin_key")
        settings = gateway(request).settings
        breakers = services(request).breakers
        return JSONResponse(
            {
                pid: {
                    "type": cfg.type,
                    "base_url": cfg.base_url,
                    "key_configured": bool(cfg.api_key()) or cfg.api_key_env is None,
                    "models": {
                        key: breakers.snapshot(key)
                        for key in settings.models
                        if settings.provider_of(key)[0] == pid
                    },
                }
                for pid, cfg in settings.providers.items()
            }
        )

    @app.post("/admin/providers/{key:path}/breaker", response_model=None)
    async def force_breaker(key: str, request: Request) -> Response:
        """Force a breaker open or closed, or hand it back to the trip rules ("auto")."""
        if not is_admin(request):
            return error_response(401, "Admin key required", "invalid_admin_key")
        if key not in gateway(request).settings.models:
            return error_response(404, f"Unknown model '{key}'", "model_not_found")
        try:
            body = await request.json()
        except ValueError:
            body = None
        force = body.get("force") if isinstance(body, dict) else None
        if force not in ("open", "closed", "auto"):
            return error_response(
                400, 'Send {"force": "open" | "closed" | "auto"}', "invalid_request"
            )
        breakers = services(request).breakers
        breakers.force(key, force)
        shared: SharedState | None = request.app.state.shared
        if shared is not None:
            shared.wake.set()
        return JSONResponse({key: breakers.snapshot(key)})

    @app.post("/admin/config/reload", response_model=None)
    async def reload_config(request: Request) -> Response:
        if not is_admin(request):
            return error_response(401, "Admin key required", "invalid_admin_key")
        try:
            new_state = load_state(request.app.state.config_path, request.app.state.http)
        except Exception as error:  # a bad file is refused and the old config stays
            return error_response(400, f"Config not reloaded: {error}", "invalid_config")
        request.app.state.gateway = new_state
        breakers = services(request).breakers
        breakers.policy = new_state.settings.resilience
        breakers.retain(new_state.settings.models)
        return JSONResponse({"status": "reloaded"})

    return app


def _watch_breakers(breakers: Breakers, metrics: Metrics, records: Records) -> None:
    for key in breakers.models:
        metrics.breaker_state.labels(key).set(0)

    def on_transition(transition: Transition) -> None:
        metrics.breaker_state.labels(transition.key).set(1 if transition.to_state == "open" else 0)
        if transition.origin == "local":
            metrics.breaker_transitions.labels(transition.key, transition.to_state).inc()
            records.add_breaker_event(transition)

    def on_cooldown(key: str, reason: str) -> None:
        metrics.cooldowns.labels(key, reason).inc()
        if reason == "auth":
            log.error("the provider rejected Switchyard's key for %s; check its API key", key)

    breakers.listeners.append(on_transition)
    breakers.on_cooldown.append(on_cooldown)
    breakers.on_budget_exhausted.append(
        lambda key: metrics.retry_budget_exhausted.labels(key).inc()
    )


def app_from_env() -> FastAPI:
    """Entry point for ``uvicorn --factory switchyard.api.app:app_from_env``."""
    return create_app()
