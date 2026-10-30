"""Continuation requests: how another model is asked to finish an answer (spec 7.6).

The wording here is shared with the seam evaluation (eval/seam/spike.py), so the gateway
sends exactly the requests that were measured.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

from switchyard.accounting.usage import estimate_tokens
from switchyard.core.types import ChatRequest, Message

Strategy = Literal["error", "restart", "native", "instructed", "instructed_repair", "anchored"]

# Strategies whose continuation goes through seam repair before it is sent.
REPAIRED = frozenset({"instructed_repair", "anchored"})

TAIL_CHARS = 200
INSTRUCTION = (
    "Your previous message was cut off. Continue from exactly where it stopped. "
    "The last characters were: «{tail}». Output only the continuation."
)
# The partial answer is quoted rather than presented as the model's own turn. In the week 1
# seam test, Claude refused 6 of 30 continuations of GPT's text, saying it had no record of
# writing it. Copying the last words gives seam repair an exact point to cut at.
ANCHORED_BODY = (
    "An earlier answer to this request was cut off partway through. Here it is:\n"
    "<partial_answer>\n{partial}\n</partial_answer>\n\n"
    "Continue that answer from exactly where it stops, keeping its style and formatting. "
    "Start your reply by copying the last few words of the partial answer exactly, then "
    "keep going. Output only the continuation, with no preface or comments."
)
ANCHORED = "{prompt}\n\n---\n" + ANCHORED_BODY
ANCHOR_EXTRA_TOKENS = 40  # room for the copied words
MIN_REMAINING_TOKENS = 16


def remaining_tokens(request: ChatRequest, emitted: str) -> int:
    """``max_tokens`` less what was already sent. The estimate leans high, so the whole
    answer stays near the client's limit; a small floor leaves room to finish a sentence."""
    return max(MIN_REMAINING_TOKENS, request.max_tokens - estimate_tokens(emitted))


def continuation_request(strategy: Strategy, request: ChatRequest, emitted: str) -> ChatRequest:
    """The request that asks another model to finish ``emitted``, an answer to ``request``
    that the client has already received."""
    remaining = remaining_tokens(request, emitted)
    messages = request.messages
    if strategy == "restart":
        return request
    if strategy == "native":
        partial = Message("assistant", emitted, prefix=True)
        return replace(request, messages=(*messages, partial), max_tokens=remaining)
    if strategy in ("instructed", "instructed_repair"):
        follow_up = (
            Message("assistant", emitted),
            Message("user", INSTRUCTION.format(tail=emitted[-TAIL_CHARS:])),
        )
        return replace(request, messages=(*messages, *follow_up), max_tokens=remaining)
    if strategy == "anchored":
        body = ANCHORED_BODY.format(partial=emitted)
        last = messages[-1] if messages else None
        if last is not None and last.role == "user" and isinstance(last.content, str):
            # One user turn, as the seam evaluation sent it.
            anchored = replace(last, content=ANCHORED.format(prompt=last.content, partial=emitted))
            new_messages = (*messages[:-1], anchored)
        else:
            new_messages = (*messages, Message("user", body))
        return replace(request, messages=new_messages, max_tokens=remaining + ANCHOR_EXTRA_TOKENS)
    raise ValueError(f"strategy {strategy!r} sends no continuation request")


def context_text(request: ChatRequest) -> str:
    """Text that shares the answer's vocabulary, for seam repair's word-join rule: the
    last user message."""
    for message in reversed(request.messages):
        if message.role == "user" and message.content:
            return message.content
    return ""
