"""Automatic checks on a join. They flag obvious problems; reading the joins decides."""

from __future__ import annotations

import re

_WORD = re.compile(r"\w+")

REPEAT_SPAN_WORDS = 8
REPEAT_TAIL_WORDS = 300


def repeated_span(
    emitted: str,
    continuation: str,
    span: int = REPEAT_SPAN_WORDS,
    tail_words: int = REPEAT_TAIL_WORDS,
) -> bool:
    """True if any run of ``span`` words in the continuation already appears in the last
    ``tail_words`` words the client received. This is the spec's "repeated text" check."""
    tail = _WORD.findall(emitted.lower())[-tail_words:]
    seen = {tuple(tail[i : i + span]) for i in range(len(tail) - span + 1)}
    words = _WORD.findall(continuation.lower())
    return any(tuple(words[i : i + span]) in seen for i in range(len(words) - span + 1))


def approx_tokens(text: str) -> int:
    """A rough token count for budgeting: about 3.5 characters per token."""
    return max(1, round(len(text) / 3.5))
