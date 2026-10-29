"""Adapters against real provider streams, recorded on 2026-10-29 with
scripts/record_stream.py. The hand-written fixtures pin the documented formats; these pin
what the providers actually sent, including what the docs don't show: Anthropic's
stop_details and whitespace padding inside events, OpenAI's obfuscation field, and
DeepSeek's usage: null on every chunk with the real usage on the finish chunk."""

from __future__ import annotations

import json

import pytest

from switchyard.core.types import (
    Finish,
    StreamEvent,
    TextDelta,
    ToolCallArgs,
    ToolCallStart,
    Usage,
    UsageReport,
)
from tests.contract.test_adapters import WEATHER, collect, request

PROVIDERS = ["anthropic", "openai", "deepseek"]

# Characters of answer text and the usage each provider reported.
TEXT = {
    "anthropic": (468, Usage(20, 153)),
    "openai": (339, Usage(16, 67)),
    "deepseek": (398, Usage(14, 75)),
}
TOOLS = {
    "anthropic": Usage(506, 88),
    "openai": Usage(140, 49),
    "deepseek": Usage(286, 64, cached_input_tokens=128),
}


def text_of(events: list[StreamEvent]) -> str:
    return "".join(e.text for e in events if isinstance(e, TextDelta))


@pytest.mark.parametrize("kind", PROVIDERS)
def test_recorded_text(kind: str) -> None:
    events, _, error = collect(kind, f"recorded/{kind}_text.sse")
    assert error is None
    length, usage = TEXT[kind]
    text = text_of(events)
    assert len(text) == length
    assert text.startswith("Sunlight") and text.rstrip().endswith(".")
    assert [e for e in events if isinstance(e, Finish)] == [Finish("stop")]
    assert [e for e in events if isinstance(e, UsageReport)] == [UsageReport(usage)]


@pytest.mark.parametrize("kind", PROVIDERS)
def test_recorded_parallel_tool_calls(kind: str) -> None:
    req = request(tools=(WEATHER,), tool_choice="required")
    events, _, error = collect(kind, f"recorded/{kind}_tools.sse", req)
    assert error is None
    starts = [e for e in events if isinstance(e, ToolCallStart)]
    assert [(s.index, s.name) for s in starts] == [(0, "get_weather"), (1, "get_weather")]
    assert all(s.id for s in starts)
    arguments: dict[int, str] = {}
    for event in events:
        if isinstance(event, ToolCallArgs):
            arguments[event.index] = arguments.get(event.index, "") + event.delta
    assert [json.loads(arguments[i])["city"] for i in (0, 1)] == ["Astana", "Oslo"]
    assert text_of(events) == ""
    assert [e for e in events if isinstance(e, Finish)] == [Finish("tool_calls")]
    assert [e for e in events if isinstance(e, UsageReport)] == [UsageReport(TOOLS[kind])]
