"""Record a real provider stream as a contract-test fixture.

The fixtures in tests/fixtures/streams are written from each provider's documentation.
This records the real thing, byte for byte, so the parsers are also pinned to what the
providers actually send. Needs the provider's key in .env.

    uv run python scripts/record_stream.py anthropic text
    uv run python scripts/record_stream.py openai tools --tools
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import aiohttp
import orjson

from switchyard.api.state import build_adapter
from switchyard.config import load_settings
from switchyard.core.types import ChatRequest, Message, ToolSpec

ROOT = Path(__file__).parent.parent
OUT = ROOT / "tests" / "fixtures" / "streams" / "recorded"
MODELS = {
    "anthropic": "anthropic/claude-haiku-5-5",
    "openai": "openai/gpt-6-luna",
    "deepseek": "deepseek/deepseek-flash",
}
WEATHER = ToolSpec(
    "get_weather",
    "Current weather for a city",
    {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
)


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


async def record(provider: str, name: str, tools: bool) -> Path:
    settings = load_settings(ROOT / "config" / "switchyard.yaml")
    key = MODELS[provider]
    provider_id, model = settings.provider_of(key)
    if tools:
        prompt = "What's the weather in Astana and in Oslo? Use the tool for both."
    else:
        prompt = "In three sentences, why is the sky blue?"
    request = ChatRequest(
        messages=(Message("user", prompt),),
        max_tokens=300,
        tools=(WEATHER,) if tools else (),
        tool_choice="required" if tools else None,
    )
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as http:
        adapter = build_adapter(provider_id, settings.providers[provider_id], http)
        body = adapter.body(model, settings.models[key], request, stream=True)
        url = adapter.url()
        async with http.post(url, data=orjson.dumps(body), headers=adapter.headers()) as response:
            raw = await response.read()
            status = response.status
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{provider}_{name}.sse"
    path.write_bytes(raw)
    print(f"HTTP {status}, {len(raw)} bytes -> {path}")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("provider", choices=sorted(MODELS))
    parser.add_argument("name", help="fixture name, for example 'text'")
    parser.add_argument("--tools", action="store_true", help="ask for parallel tool calls")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    asyncio.run(record(args.provider, args.name, args.tools))


if __name__ == "__main__":
    main()
