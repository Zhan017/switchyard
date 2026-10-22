"""Seam repair.

When a stream dies mid-answer, Switchyard asks another model to continue it. The client
already has ``emitted``. This module cleans up the start of the continuation so the join
reads as one answer: no preface, no repeated words, no restarted code fence, no broken
whitespace.

Each fix is recorded as an action name, so the evaluation can count how often each one
fires and the gateway can export it as a metric.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MIN_OVERLAP_TOKENS = 3
MIN_OVERLAP_WORDS = 2
TAIL_TOKENS = 400
PARAPHRASE_RATIO = 0.6
FIRST_SENTENCE_MAX_CHARS = 400

_TOKEN = re.compile(r"\w+|[^\w\s]")
_WORD = re.compile(r"\w+")
_WORD_TAIL = re.compile(r"\w+\Z")
_WORD_HEAD = re.compile(r"\A\w+")
_FENCE_LINE = re.compile(r"(?m)^[ \t]*```")
_FENCE_OPEN = re.compile(r"\A\s*```[\w+#.-]*[ \t]*\n")
_SENTENCE_END = re.compile(r"[.!?](?=\s)|\n")
_JOIN_PUNCTUATION = ".,;:!?)"

_ACK = r"(?:Sure|Certainly|Of course|Okay|OK|Absolutely|Got it)[,!.:]"
_REST = r"Here(?:'s| is) the (?:rest|continuation)[^\n:]*[:.]"
_CONTINUING = r"Continuing(?: from where (?:it|I|you) (?:stopped|left off))?\s*[:.]"
_PREFACE = re.compile(
    rf"\A\s*(?:{_ACK}(?:\s*{_REST})?|{_REST}|{_CONTINUING}|\((?:continued|cont\.)\)"
    r"|(?:\.\.\.|…)(?=\S))[ \t]*\n?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SeamResult:
    """The continuation to send after ``emitted``, and the fixes applied to it."""

    text: str
    actions: tuple[str, ...]


def has_preface(continuation: str) -> bool:
    """True if the continuation opens with a preface such as "Sure, here's the rest:"."""
    return _PREFACE.match(continuation) is not None


def repair(emitted: str, continuation: str, context: str = "") -> SeamResult:
    """Clean up the start of ``continuation`` so it joins ``emitted`` seamlessly.

    ``context`` is extra text that shares the answer's vocabulary, usually the prompt. It
    helps decide whether a word cut in half was completed or a new word started.
    """
    actions: list[str] = []
    text = continuation

    preface = _PREFACE.match(text)
    if preface:
        text = text[preface.end() :]
        actions.append("preface_stripped")

    in_code = _inside_code_fence(emitted)
    if in_code:
        fence = _FENCE_OPEN.match(text)
        if fence:
            text = text[fence.end() :]
            actions.append("fence_stripped")

    # Each trim returns (text, action, joined). joined means the trim already lined the
    # continuation up with a word cut in half, so the whitespace fix must not touch it.
    trim = _trim_exact_overlap(emitted, text) or _trim_paraphrase(emitted, text)
    joined = False
    if trim is not None:
        text, action, joined = trim
        actions.append(action)

    if not joined:
        text, join_action = _fix_join(emitted, text, in_code, context)
        if join_action:
            actions.append(join_action)

    return SeamResult(text=text, actions=tuple(actions))


def _inside_code_fence(text: str) -> bool:
    return len(_FENCE_LINE.findall(text)) % 2 == 1


def _is_word_char(char: str) -> bool:
    return char.isalnum() or char == "_"


def _trim_exact_overlap(emitted: str, text: str) -> tuple[str, str, bool] | None:
    """Drop the longest start of ``text`` that repeats the end of ``emitted``.

    Also handles an answer cut mid-word: if ``emitted`` ends with "photosyn" and the
    continuation repeats the words before it and then writes "photosynthesis", only
    "thesis" is kept.
    """
    tail = [m for m in _TOKEN.finditer(emitted)][-TAIL_TOKENS:]
    head = [m for m in _TOKEN.finditer(text)][:TAIL_TOKENS]
    tail_norm = [m.group().lower() for m in tail]
    head_norm = [m.group().lower() for m in head]
    ends_mid_word = bool(emitted) and _is_word_char(emitted[-1])

    for k in range(min(len(tail_norm), len(head_norm)), MIN_OVERLAP_TOKENS - 1, -1):
        if _count_words(head_norm[:k]) < MIN_OVERLAP_WORDS:
            continue
        if tail_norm[-k:] == head_norm[:k]:
            return text[head[k - 1].end() :], "overlap_trimmed", False
        partial = tail_norm[-1]
        if (
            ends_mid_word
            and tail_norm[-k:-1] == head_norm[: k - 1]
            and head_norm[k - 1].startswith(partial)
            and len(head_norm[k - 1]) > len(partial)
        ):
            return text[head[k - 1].start() + len(tail[-1].group()) :], "overlap_trimmed", True
    return None


def _count_words(tokens: list[str]) -> int:
    return sum(1 for token in tokens if _is_word_char(token[0]))


def _trim_paraphrase(emitted: str, text: str) -> tuple[str, str, bool] | None:
    """Handle a continuation that restates the interrupted sentence in other words.

    If the continuation's first sentence shares most of its words with the sentence that
    was cut, find where the restatement reaches the cut point (the last two or three words
    the client saw) and keep only what comes after it. If no such point is found, keep the
    text and report it as suspected.
    """
    fragment = _last_sentence_fragment(emitted)
    fragment_words = _WORD.findall(fragment.lower())
    if len(set(fragment_words)) < 4:
        return None

    # If the answer was cut mid-word, the last word may be partial, so it doesn't count
    # toward the overlap.
    partial = fragment_words[-1] if _is_word_char(emitted[-1]) else ""
    compared = set(fragment_words[:-1] if partial else fragment_words)
    first = _first_sentence(text)
    first_words = [(m.group().lower(), m.end()) for m in _WORD.finditer(first)]
    shared = compared & {word for word, _ in first_words}
    if len(shared) / len(compared) < PARAPHRASE_RATIO:
        return None

    words_only = [word for word, _ in first_words]
    # Try the last words as complete first. If that fails and the last word may be
    # partial, try the words before it and then finish the cut word.
    attempts = [(fragment_words, "")]
    if partial:
        attempts.append((fragment_words[:-1], partial))
    for anchor_words, cut_word in attempts:
        for size in (3, 2):
            if len(anchor_words) < size:
                continue
            anchor = anchor_words[-size:]
            for start in range(len(words_only) - size, -1, -1):
                if words_only[start : start + size] != anchor:
                    continue
                rest = text[first_words[start + size - 1][1] :]
                if not cut_word:
                    return rest, "paraphrase_trimmed", False
                rest = rest.lstrip()
                if rest.lower().startswith(cut_word) and len(rest) > len(cut_word):
                    return rest[len(cut_word) :], "paraphrase_trimmed", True
    return text, "paraphrase_suspected", False


def _last_sentence_fragment(text: str) -> str:
    ends = list(_SENTENCE_END.finditer(text))
    return text[ends[-1].end() :] if ends else text


def _first_sentence(text: str) -> str:
    match = _SENTENCE_END.search(text, pos=1)
    end = match.end() if match else len(text)
    return text[: min(end, FIRST_SENTENCE_MAX_CHARS)]


def _fix_join(emitted: str, text: str, in_code: bool, context: str) -> tuple[str, str | None]:
    """Fix whitespace where the two texts meet."""
    if not emitted or not text:
        return text, None
    last, first = emitted[-1], text[0]

    if last == "\n" and first == "\n":
        return text.lstrip("\n"), "whitespace_fixed"
    if last in " \t" and first in " \t":
        return text.lstrip(" \t"), "whitespace_fixed"

    if _is_word_char(last) and _is_word_char(first):
        return _join_words(emitted, text, context)

    if not in_code and last in _JOIN_PUNCTUATION and first.isalpha():
        return " " + text, "space_inserted"
    return text, None


def _join_words(emitted: str, text: str, context: str) -> tuple[str, str | None]:
    """Decide what happens when a word character meets a word character.

    Three cases: the continuation rewrote the cut word in full ("photosyn" then
    "photosynthesis"), it finished the cut word ("photosyn" then "thesis"), or it started
    a new word without a leading space ("quick" then "brown").
    """
    partial_match = _WORD_TAIL.search(emitted)
    head_match = _WORD_HEAD.match(text)
    if not partial_match or not head_match:
        return text, None
    partial = partial_match.group()
    head = head_match.group()
    before = emitted[: partial_match.start()]
    seen_elsewhere = set(_WORD.findall(f"{before} {context}".lower()))
    vocabulary = seen_elsewhere | set(_WORD.findall(text.lower()))

    if (
        head.lower().startswith(partial.lower())
        and len(head) > len(partial)
        and partial.lower() not in seen_elsewhere
    ):
        return text[len(partial) :], "word_rejoined"
    if (partial + head).lower() in vocabulary:
        return text, None
    return " " + text, "space_inserted"
