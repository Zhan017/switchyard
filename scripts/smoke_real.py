"""Smoke test the gateway against the real provider APIs.

Starts the gateway on a free local port with config/switchyard.yaml and the keys in .env,
then sends each model in the chat-fast route group a small set of requests through it:
streamed and non-streamed text, a streamed tool call, JSON in the mode the model supports,
a request with a temperature, and the exact probe the breaker uses to recover.
It also checks prompt caching on OpenAI and DeepSeek, as information only, and one
request to the chat-fast route itself.

Prints a table and writes a Markdown report and JSON results. Exit code 0 when nothing
failed, 1 when a check failed, 2 for a usage error, missing keys or a budget refusal.

    uv run python scripts/smoke_real.py                  # real APIs, keys in .env
    uv run python scripts/smoke_real.py --only openai    # one provider
    uv run python scripts/smoke_real.py --dry-run        # against mockllm, no keys, no cost
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import aiohttp
import httpx
import orjson
import uvicorn
import yaml

from mockllm.app import create_mock_app
from switchyard.accounting.pricing import PriceTable, load_prices
from switchyard.accounting.usage import estimate_tokens
from switchyard.api.app import create_app
from switchyard.api.state import GatewayState, load_state
from switchyard.config import Settings, expand_env, load_settings
from switchyard.core.types import ChatRequest, Message, Usage, UsageReport
from switchyard.observability.metrics import Metrics
from switchyard.providers.errors import ProviderError
from switchyard.resilience.breaker import Breakers
from switchyard.resilience.prober import Prober

ROOT = Path(__file__).parent.parent
CONFIG = ROOT / "config" / "switchyard.yaml"
ROUTE = "chat-fast"
KEY = "sy-dev-local-key"  # the development tenant key; the gateway only listens on 127.0.0.1
DEFAULT_BUDGET_USD = 0.05
CACHE_PROVIDERS = ("openai", "deepseek")  # cache automatically; Anthropic needs cache_control

Result = Literal["pass", "warn", "fail", "info", "skip"]

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Current weather for a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}
ANSWER_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "answer",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    },
}


@dataclass
class Check:
    model: str
    name: str
    result: Result
    detail: str
    cost_usd: float = 0.0
    ttft_ms: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


# Requests


def user(text: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": text}]


def json_request(settings: Settings, model: str) -> dict[str, Any] | None:
    options = settings.models[model]
    if options.json_schema:
        return {
            "messages": user("What is the capital of Kazakhstan? Answer in one word."),
            "response_format": ANSWER_SCHEMA,
            "max_tokens": 60,
        }
    if options.json_object:
        return {
            "messages": user(
                'Reply with a JSON object with one key, "answer": the capital of Kazakhstan.'
            ),
            "response_format": {"type": "json_object"},
            "max_tokens": 60,
        }
    return None


def plan(settings: Settings, model: str) -> list[tuple[str, dict[str, Any]]]:
    """The API checks for one model, as (name, request body without the model)."""
    primes = user("List the first five prime numbers, separated by commas. Nothing else.")
    checks: list[tuple[str, dict[str, Any]]] = [
        ("stream text", {"messages": primes, "max_tokens": 60, "stream": True}),
        ("unary text", {"messages": primes, "max_tokens": 60}),
        (
            "stream tool call",
            {
                "messages": user("What's the weather in Astana? Use the tool."),
                "tools": [WEATHER_TOOL],
                "tool_choice": "required",
                "max_tokens": 100,
                "stream": True,
            },
        ),
    ]
    body = json_request(settings, model)
    if body is not None:
        checks.append(("json", body))
    hello = user("Say hello in one word.")
    checks.append(("temperature", {"messages": hello, "max_tokens": 20, "temperature": 0.3}))
    return checks


def cache_prompt(run_id: str) -> str:
    """A prompt long enough to be cached (OpenAI caches from 1,024 tokens), with a run id
    first so an earlier run's cache can't answer for this one."""
    lines = [f"Run {run_id}. Reference notes for a reliability review follow."]
    for i in range(1, 91):
        lines.append(
            f"Note {i}: the gateway routes each request to a healthy provider, retries once "
            f"before the first token, and records cost per tenant and feature."
        )
    lines.append("Reply with the single word OK.")
    return "\n".join(lines)


# Budget


def estimate_cost(settings: Settings, prices: PriceTable, models: list[str]) -> float:
    """Worst case: every request uses all its output tokens, at peak prices."""
    total = 0.0
    run_prompt = cache_prompt("x")
    for model in models:
        for _, body in plan(settings, model):
            text = " ".join(str(m["content"]) for m in body["messages"])
            usage = Usage(estimate_tokens(text) + 50, int(body["max_tokens"]))
            total += prices.cost(model, usage) or 0.0
        probe = Usage(10, settings.models[model].probe_max_tokens)
        total += prices.cost(model, probe) or 0.0
        if settings.provider_of(model)[0] in CACHE_PROVIDERS:
            total += 2 * (prices.cost(model, Usage(estimate_tokens(run_prompt) + 50, 5)) or 0.0)
    total += prices.cost(models[0], Usage(60, 60)) or 0.0  # the route check
    return total


# Running the gateway


@contextmanager
def serve(app: Any) -> Iterator[int]:
    """Serve an ASGI app on a free local port in a background thread."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("server did not start")
        time.sleep(0.01)
    try:
        yield int(server.servers[0].sockets[0].getsockname()[1])
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def dry_config(mock_url: str, out_dir: Path) -> Path:
    """The real config with every provider pointed at mockllm, which speaks OpenAI's
    format, so the script can be tested without keys."""
    raw = yaml.safe_load(expand_env(CONFIG.read_text()))
    for provider in raw["providers"].values():
        provider["type"] = "openai"
        provider["base_url"] = f"{mock_url}/v1"
        provider.pop("api_key_env", None)
    raw["prices_file"] = str(CONFIG.parent / raw.get("prices_file", "prices.yaml"))
    path = out_dir / "dry-config.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return path


# Checks through the gateway


async def api_check(
    client: httpx.AsyncClient, model: str, name: str, body: dict[str, Any], settings: Settings
) -> Check:
    payload = {"model": model, **body}
    stream = bool(body.get("stream"))
    started = time.perf_counter()
    try:
        if stream:
            return await _stream_check(client, model, name, payload, started)
        response = await client.post("/v1/chat/completions", json=payload)
    except httpx.HTTPError as error:
        return Check(model, name, "fail", f"client error: {type(error).__name__}: {error}")
    data = _json(response.content)
    if response.status_code != 200:
        return _error_check(model, name, response.status_code, data)
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    summary = data.get("switchyard") or {}
    check = _judge(
        model,
        name,
        finish=choice.get("finish_reason"),
        text=message.get("content") or "",
        tool_calls=[
            {"name": c["function"]["name"], "arguments": c["function"]["arguments"]}
            for c in message.get("tool_calls") or []
        ],
        refusal=message.get("refusal"),
        usage=data.get("usage"),
        summary=summary,
        body=body,
        settings=settings,
    )
    check.extra["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return check


async def _stream_check(
    client: httpx.AsyncClient, model: str, name: str, payload: dict[str, Any], started: float
) -> Check:
    text, finish, usage, summary, error = "", None, None, {}, None
    calls: dict[int, dict[str, str]] = {}
    done = False
    ttft: float | None = None
    async with client.stream("POST", "/v1/chat/completions", json=payload) as response:
        if response.status_code != 200:
            return _error_check(model, name, response.status_code, _json(await response.aread()))
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            data_text = line[6:]
            if data_text == "[DONE]":
                done = True
                continue
            chunk = _json(data_text.encode())
            if "error" in chunk:
                error = chunk["error"]
                continue
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    text += delta["content"]
                for call in delta.get("tool_calls") or []:
                    entry = calls.setdefault(call["index"], {"name": "", "arguments": ""})
                    function = call.get("function") or {}
                    entry["name"] += function.get("name") or ""
                    entry["arguments"] += function.get("arguments") or ""
                if ttft is None and (delta.get("content") or delta.get("tool_calls")):
                    ttft = (time.perf_counter() - started) * 1000
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
            if chunk.get("usage"):
                usage = chunk["usage"]
            if chunk.get("switchyard"):
                summary = chunk["switchyard"]
    if error is not None:
        return Check(model, name, "fail", f"error event: {_short(error)}")
    if not done:
        return Check(model, name, "fail", "the stream ended without [DONE]")
    check = _judge(
        model,
        name,
        finish=finish,
        text=text,
        tool_calls=[calls[i] for i in sorted(calls)],
        refusal=None,
        usage=usage,
        summary=summary,
        body=payload,
        settings=None,
    )
    check.ttft_ms = None if ttft is None else round(ttft, 1)
    return check


def _judge(
    model: str,
    name: str,
    *,
    finish: str | None,
    text: str,
    tool_calls: list[dict[str, str]],
    refusal: str | None,
    usage: dict[str, Any] | None,
    summary: dict[str, Any],
    body: dict[str, Any],
    settings: Settings | None,
) -> Check:
    cost = float(summary.get("cost_usd") or 0.0)
    prompt = int((usage or {}).get("prompt_tokens") or 0)
    output = int((usage or {}).get("completion_tokens") or 0)
    tokens = f"{prompt} in, {output} out"
    problems: list[str] = []
    warnings: list[str] = []
    if refusal:
        problems.append(f"refused: {refusal[:80]}")
    if body.get("tools"):
        if finish != "tool_calls" or not tool_calls:
            problems.append(f"no tool call (finish_reason {finish})")
        elif tool_calls[0]["name"] != "get_weather":
            problems.append(f"called {tool_calls[0]['name']!r}")
        else:
            try:
                arguments = orjson.loads(tool_calls[0]["arguments"] or "{}")
            except orjson.JSONDecodeError:
                problems.append("tool arguments are not JSON")
            else:
                if "city" not in arguments:
                    problems.append(f"tool arguments lack city: {arguments}")
    elif finish not in ("stop", "length"):
        problems.append(f"finish_reason {finish}")
    elif not text.strip():
        problems.append("empty answer")
    if body.get("response_format") and not problems:
        try:
            parsed = orjson.loads(text)
        except orjson.JSONDecodeError:
            problems.append("answer is not JSON")
        else:
            if not isinstance(parsed, dict) or "answer" not in parsed:
                problems.append(f"JSON lacks 'answer': {text[:80]}")
    if not usage or not prompt:
        warnings.append("no usage reported")
    if summary.get("usage_estimated"):
        warnings.append("usage estimated, the provider didn't report it")
    if summary.get("served_model") not in (None, model) and model != ROUTE:
        problems.append(f"served by {summary.get('served_model')}")
    attempts = int(summary.get("attempts") or 1)
    if attempts > 1:
        warnings.append(f"needed {attempts} attempts")
    options = settings.models.get(model) if settings is not None else None
    sampling_dropped = options is not None and not options.sampling_params
    if "temperature" in body and sampling_dropped:
        warnings.append("temperature dropped by config (sampling_params: false)")
    sample = text.strip().replace("\n", " ")[:60] if text else ""
    if tool_calls and not text:
        sample = f"{tool_calls[0]['name']}({tool_calls[0]['arguments'][:40]})"
    if problems:
        return Check(model, name, "fail", "; ".join(problems), cost)
    detail = f"{tokens}; {sample!r}"
    if warnings:
        return Check(model, name, "warn", f"{detail}; " + "; ".join(warnings), cost)
    return Check(model, name, "pass", detail, cost)


def _error_check(model: str, name: str, status: int, data: dict[str, Any]) -> Check:
    return Check(model, name, "fail", f"HTTP {status}: {_short(data.get('error', data))}")


def _short(value: Any) -> str:
    if isinstance(value, dict):
        message = value.get("message") or value.get("code") or value
        return str(message)[:300]
    return str(value)[:300]


def _json(raw: bytes) -> dict[str, Any]:
    try:
        value = orjson.loads(raw)
    except orjson.JSONDecodeError:
        return {"error": {"message": raw[:300].decode(errors="replace")}}
    return value if isinstance(value, dict) else {"error": {"message": str(value)[:300]}}


# Checks below the API: the breaker's probe, and caching


async def probe_check(state: GatewayState, model: str) -> Check:
    prober = Prober(Breakers(state.settings.resilience), lambda: state, Metrics())
    started = time.perf_counter()
    outcome = await prober.probe_once(model)
    took = (time.perf_counter() - started) * 1000
    if outcome == "ok":
        return Check(model, "breaker probe", "pass", f"probe answered in {took:.0f} ms")
    return Check(
        model,
        "breaker probe",
        "fail",
        f"probe outcome {outcome}: an open breaker on this model could never close",
    )


async def cache_check(state: GatewayState, model: str, run_id: str) -> Check:
    provider, name = state.settings.provider_of(model)
    adapter = state.adapters[provider]
    request = ChatRequest(messages=(Message("user", cache_prompt(run_id)),), max_tokens=5)
    usages: list[Usage] = []
    for attempt in range(2):
        if attempt:
            await asyncio.sleep(3)  # DeepSeek builds its cache in the background
        reported: Usage | None = None
        try:
            async for event in adapter.stream(name, state.settings.models[model], request):
                if isinstance(event, UsageReport):
                    reported = event.usage
        except ProviderError as error:
            detail = f"{error.kind.value}: {error.message[:200]}"
            return Check(model, "prompt cache", "fail", detail)
        usages.append(reported or Usage())
    cost = sum(state.prices.cost(model, u) or 0.0 for u in usages)
    first, second = usages
    detail = (
        f"first call {first.input_tokens} in, {first.cache_write_tokens} written to cache; "
        f"second call {second.cached_input_tokens} read from cache"
    )
    result: Result = "pass" if second.cached_input_tokens > 0 else "info"
    if result == "info":
        detail += " (caching is best effort, so no hit isn't a failure)"
    return Check(model, "prompt cache", result, detail, cost)


# The run


async def run(
    config: Path, models: list[str], route_check: bool, settings: Settings
) -> list[Check]:
    os.environ.setdefault("SWITCHYARD_LOG_LEVEL", "ERROR")
    app = create_app(config)
    checks: list[Check] = []
    run_id = uuid.uuid4().hex[:8]
    with serve(app) as port:
        headers = {"authorization": f"Bearer {KEY}", "x-switchyard-feature": "bench"}
        timeout = httpx.Timeout(60.0)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", headers=headers, timeout=timeout, trust_env=False
        ) as client:
            for model in models:
                for name, body in plan(settings, model):
                    check = await api_check(client, model, name, body, settings)
                    checks.append(check)
                    print_row(check)
            if route_check:
                body = {"messages": user("Say hello in one word."), "max_tokens": 60}
                check = await api_check(client, ROUTE, "route group", body, settings)
                checks.append(check)
                print_row(check)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as http:
        state = load_state(config, http)
        for model in models:
            check = await probe_check(state, model)
            checks.append(check)
            print_row(check)
            if settings.provider_of(model)[0] in CACHE_PROVIDERS:
                check = await cache_check(state, model, run_id)
                checks.append(check)
                print_row(check)
    return checks


def print_row(check: Check) -> None:
    ttft = f" ttft {check.ttft_ms:.0f} ms" if check.ttft_ms is not None else ""
    row = f"  {check.result:>4}  {check.model:<28} {check.name:<17} {check.detail}{ttft}"
    print(row, flush=True)


def git_commit() -> str:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return commit + (" with local changes" if dirty else "")


def report(checks: list[Check], meta: dict[str, Any]) -> str:
    counts = {r: sum(c.result == r for c in checks) for r in ("pass", "warn", "fail", "info")}
    dry = bool(meta["dry_run"])
    title = "Smoke test dry run" if dry else "Real-API smoke test"
    cost_note = " (what these token counts would cost; mockllm is free)" if dry else ""
    where = "every provider pointed at mockllm" if dry else "calling the real APIs"
    lines = [
        f"# {title}, {meta['date']}",
        "",
        f"{counts['pass']} passed, {counts['warn']} passed with warnings, {counts['fail']} failed, "
        f"{counts['info']} for information. Commit {meta['commit']}. "
        f"Cost ${meta['cost_usd']:.4f}{cost_note}.",
        "",
        f"The gateway ran locally with `config/switchyard.yaml`, {where}. Each model was called "
        "by name, so a failure shows instead of failing over. `breaker probe` is the tiny "
        "request an open breaker sends to recover; `prompt cache` calls the adapter directly "
        "with the same long prompt twice.",
        "",
        "| Model | Check | Result | Time to first token | Detail |",
        "| --- | --- | --- | --- | --- |",
    ]
    for c in checks:
        ttft = f"{c.ttft_ms:.0f} ms" if c.ttft_ms is not None else ""
        detail = c.detail.replace("|", "\\|")
        lines.append(f"| {c.model} | {c.name} | {c.result} | {ttft} | {detail} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="run against mockllm")
    parser.add_argument("--only", help="comma-separated providers, for example openai,deepseek")
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET_USD, help="USD cap")
    parser.add_argument("--out", type=Path, help="results folder")
    args = parser.parse_args(argv)

    load_dotenv(ROOT / ".env")
    settings = load_settings(CONFIG)
    models = list(settings.route_groups[ROUTE].models)
    if args.only:
        wanted = {p.strip() for p in args.only.split(",")}
        unknown = wanted - {settings.provider_of(m)[0] for m in models}
        if unknown:
            print(f"no model in {ROUTE} from: {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        models = [m for m in models if settings.provider_of(m)[0] in wanted]
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")

    with tempfile.TemporaryDirectory() as scratch, ExitStack() as stack:
        config = CONFIG
        if args.dry_run:
            out = args.out or Path(scratch) / "out"
            mock_port = stack.enter_context(serve(create_mock_app()))
            config = dry_config(f"http://127.0.0.1:{mock_port}", Path(scratch))
        else:
            out = args.out or ROOT / "docs" / "results" / "smoke" / stamp
            providers = sorted({settings.provider_of(m)[0] for m in models})
            missing = [
                f"{p} ({settings.providers[p].api_key_env})"
                for p in providers
                if not settings.providers[p].api_key()
            ]
            if missing:
                print(f"Missing API keys: {', '.join(missing)}. Add them to .env.", file=sys.stderr)
                return 2
            prices = load_prices(CONFIG.parent / settings.prices_file)
            estimate = estimate_cost(settings, prices, models)
            print(f"Worst-case cost: ${estimate:.4f} (budget ${args.budget:.2f})")
            if estimate > args.budget:
                print("Over budget; raise --budget to run anyway.", file=sys.stderr)
                return 2
        suffix = " (dry run)" if args.dry_run else ""
        print(f"Smoke test of {', '.join(models)}{suffix}")
        checks = asyncio.run(run(config, models, route_check=not args.only, settings=settings))
        cost = sum(c.cost_usd for c in checks)
        meta = {
            "date": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            "commit": git_commit(),
            "dry_run": args.dry_run,
            "models": models,
            "cost_usd": round(cost, 6),
        }
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.md").write_text(report(checks, meta))
        results = {**meta, "checks": [asdict(c) for c in checks]}
        (out / "results.json").write_bytes(orjson.dumps(results, option=orjson.OPT_INDENT_2))
        failed = sum(c.result == "fail" for c in checks)
        print(f"{len(checks)} checks, {failed} failed, cost ${cost:.4f} -> {out}")
        return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
