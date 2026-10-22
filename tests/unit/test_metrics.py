from eval.seam.metrics import repeated_span

SENTENCE = "the quick brown fox jumps over the lazy dog near the river bank"


def test_repeated_span_found() -> None:
    emitted = f"Intro words here. {SENTENCE}"
    assert repeated_span(emitted, "fox jumps over the lazy dog near the river and then")


def test_repeated_span_needs_eight_words() -> None:
    emitted = f"Intro words here. {SENTENCE}"
    assert not repeated_span(emitted, "over the lazy dog near the city")


def test_repeated_span_ignores_case() -> None:
    assert repeated_span(SENTENCE, SENTENCE.upper())


def test_repeated_span_only_looks_at_the_tail() -> None:
    emitted = SENTENCE + " filler" * 400
    assert not repeated_span(emitted, SENTENCE)
