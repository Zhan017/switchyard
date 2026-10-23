"""A fake provider for dry runs.

It costs nothing and needs no keys. Its continuations misbehave in the ways real models
do (prefaces, repeated words, restarted sentences, restarted code fences), so a dry run
exercises seam repair and the report end to end. Its numbers mean nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import re

from eval.seam.clients import Completion, Message
from eval.seam.metrics import approx_tokens

_WORDS = [
    "system",
    "request",
    "answer",
    "model",
    "provider",
    "stream",
    "token",
    "cost",
    "latency",
    "budget",
    "failure",
    "service",
    "user",
    "data",
    "result",
    "value",
    "example",
    "reason",
    "method",
    "process",
    "change",
    "limit",
    "state",
    "network",
    "window",
    "record",
    "metric",
    "signal",
    "plan",
    "step",
    "team",
    "issue",
    "design",
    "choice",
]
_CODE_HINTS = ("Python", "TypeScript", "JavaScript", "Go ", "Rust", "SQL", "Bash")


class FakeClient:
    def __init__(self, family: str, model: str, supports_prefix: bool) -> None:
        self.family, self.model, self.supports_prefix = family, f"fake-{model}", supports_prefix

    async def complete(
        self, messages: list[Message], max_tokens: int, prefix: bool = False
    ) -> Completion:
        await asyncio.sleep(0)
        seed = hashlib.sha256(
            (self.family + "|".join(m["content"] for m in messages)).encode()
        ).hexdigest()
        rng = random.Random(seed)
        prompt = messages[0]["content"]
        quoted = re.search(r"<partial_answer>\n(.*)\n</partial_answer>", prompt, re.DOTALL)
        if quoted:
            # Anchored: copy the last few words, as asked, then continue.
            partial = quoted.group(1)
            copied = " ".join(partial.split()[-rng.randint(1, 5) :])
            text = copied + _continuation(rng, partial, prompt, native=True)
        elif len(messages) == 1:
            text = _answer(rng, prompt)
        else:
            text = _continuation(rng, messages[1]["content"], prompt, native=prefix)
        text = text[: max_tokens * 4]
        input_chars = sum(len(m["content"]) for m in messages)
        return Completion(
            text=text,
            input_tokens=approx_tokens("x" * input_chars),
            cached_input_tokens=0,
            output_tokens=approx_tokens(text),
            finish_reason="stop",
            latency_s=0.0,
        )


def _sentence(rng: random.Random) -> str:
    words = [rng.choice(_WORDS) for _ in range(rng.randint(8, 16))]
    return " ".join(words).capitalize() + "."


def _paragraph(rng: random.Random) -> str:
    return " ".join(_sentence(rng) for _ in range(rng.randint(3, 5)))


def _code(rng: random.Random) -> str:
    lines = [f"def {rng.choice(_WORDS)}_{rng.choice(_WORDS)}(value):"]
    lines += [f"    {rng.choice(_WORDS)} = value + {rng.randint(1, 9)}" for _ in range(6)]
    lines.append("    return value")
    return "```python\n" + "\n".join(lines) + "\n```"


def _answer(rng: random.Random, prompt: str) -> str:
    parts = [_paragraph(rng) for _ in range(4)]
    if any(hint in prompt for hint in _CODE_HINTS):
        parts.insert(1, _code(rng))
    return "\n\n".join(parts)


def _continuation(rng: random.Random, emitted: str, prompt: str, native: bool) -> str:
    rest = " " + " ".join(_sentence(rng) for _ in range(3)) + "\n\n" + _paragraph(rng)
    inside_code = emitted.count("```") % 2 == 1
    if inside_code:
        rest = "\n    return value\n```\n\n" + _paragraph(rng)
    if native:
        return rest
    style = rng.choice(["clean", "no_space", "overlap", "preface", "paraphrase"])
    if inside_code and rng.random() < 0.5:
        return "```python" + rest
    words = emitted.split()
    if style == "no_space":
        return rest.lstrip()
    if style == "overlap":
        return " ".join(words[-5:]) + rest
    if style == "preface":
        return "Sure, here is the rest:\n" + rest.lstrip()
    if style == "paraphrase":
        restated = words[-6:]
        restated[0], restated[1] = restated[1], restated[0]
        return " ".join(restated).capitalize() + rest
    return rest
