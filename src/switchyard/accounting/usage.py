"""Token estimates for when a provider doesn't report usage.

A stream cut mid-answer usually ends before the provider sends its usage numbers. The
gateway still has to account for what it was charged, so it estimates from the text it
received: about 3.5 characters per token, plus a 15% margin so estimates lean high.
Estimated usage is always flagged, and reported apart from exact usage.
"""

from __future__ import annotations

import math

from switchyard.core.types import ChatRequest, Usage

CHARS_PER_TOKEN = 3.5
SAFETY_MARGIN = 0.15
PER_MESSAGE_OVERHEAD_TOKENS = 4


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return math.ceil(len(text) / CHARS_PER_TOKEN * (1 + SAFETY_MARGIN))


def estimate_input_tokens(request: ChatRequest) -> int:
    total = 0
    for message in request.messages:
        total += PER_MESSAGE_OVERHEAD_TOKENS + estimate_tokens(message.content or "")
        for call in message.tool_calls:
            total += estimate_tokens(call.name) + estimate_tokens(call.arguments)
    for tool in request.tools:
        total += estimate_tokens(tool.name) + estimate_tokens(str(tool.parameters))
        total += estimate_tokens(tool.description or "")
    return total


def complete_usage(reported: Usage | None, request: ChatRequest, output_text: str) -> Usage:
    """Fill in whatever the provider didn't report, and flag the result as estimated."""
    input_tokens = reported.input_tokens if reported else 0
    output_tokens = reported.output_tokens if reported else 0
    estimated = bool(reported and reported.estimated)
    if not input_tokens:
        input_tokens, estimated = estimate_input_tokens(request), True
    if not output_tokens and output_text:
        output_tokens, estimated = estimate_tokens(output_text), True
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=reported.cached_input_tokens if reported else 0,
        cache_write_tokens=reported.cache_write_tokens if reported else 0,
        estimated=estimated,
    )
