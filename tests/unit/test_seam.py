import pytest

from switchyard.streaming.seam import has_preface, repair

FENCE = "```"

CASES = [
    # (name, emitted, continuation, context, expected_text, expected_actions)
    ("clean join", "The cat sat on the", " mat.", "", " mat.", ()),
    ("missing space", "The cat sat on the", "mat.", "", " mat.", ("space_inserted",)),
    ("exact overlap", "The cat sat on the", "sat on the mat.", "", " mat.", ("overlap_trimmed",)),
    (
        "overlap ignores case and spacing",
        "The cat  sat On the",
        "Sat on the mat.",
        "",
        " mat.",
        ("overlap_trimmed",),
    ),
    (
        "overlap after a word cut in half",
        "Plants make sugar by photosyn",
        "make sugar by photosynthesis in the leaves.",
        "",
        "thesis in the leaves.",
        ("overlap_trimmed",),
    ),
    (
        "cut word rewritten in full",
        "Plants make sugar by photosyn",
        "photosynthesis in the leaves.",
        "",
        "thesis in the leaves.",
        ("word_rejoined",),
    ),
    (
        "cut word completed",
        "Plants make sugar by photosyn",
        "thesis in the leaves.",
        "Explain photosynthesis.",
        "thesis in the leaves.",
        (),
    ),
    (
        "short repeat is left alone",
        "I like the",
        "the mat.",
        "",
        " the mat.",
        ("space_inserted",),
    ),
    (
        "preface stripped",
        "First, preheat the oven.",
        "Sure, here's the rest:\nThen mix the flour.",
        "",
        " Then mix the flour.",
        ("preface_stripped", "space_inserted"),
    ),
    (
        "continuing preface stripped",
        "First, preheat the oven. ",
        "Continuing: Then mix the flour.",
        "",
        "Then mix the flour.",
        ("preface_stripped",),
    ),
    (
        "restarted code fence stripped",
        f"Here is the code:\n{FENCE}python\ndef double(x):\n",
        f"{FENCE}python\n    return x * 2\n{FENCE}",
        "",
        f"    return x * 2\n{FENCE}",
        ("fence_stripped",),
    ),
    (
        "no space added after a dot in code",
        f"{FENCE}python\nresult = client.",
        "fetch(url)\n",
        "",
        "fetch(url)\n",
        (),
    ),
    (
        "paraphrased restart trimmed",
        "Paris is the capital of France. It is famous for the Eiffel Tower, which was built "
        "for the",
        "The Eiffel Tower was built for the 1889 World's Fair. Today it draws millions.",
        "",
        " 1889 World's Fair. Today it draws millions.",
        ("paraphrase_trimmed",),
    ),
    (
        "paraphrased restart after a word cut in half",
        "It is famous for the Eiffel Tower, which was built for the Exposi",
        "The Eiffel Tower was built for the Exposition Universelle of 1889.",
        "",
        "tion Universelle of 1889.",
        ("paraphrase_trimmed",),
    ),
    (
        "paraphrase without an anchor is flagged",
        "Rivers carry sediment from the mountains down to the",
        "Sediment is carried by rivers from the mountains. It settles in deltas.",
        "",
        " Sediment is carried by rivers from the mountains. It settles in deltas.",
        ("paraphrase_suspected", "space_inserted"),
    ),
    ("double space", "Hello ", " world.", "", "world.", ("whitespace_fixed",)),
    ("double newline", "Line one.\n", "\nLine two.", "", "Line two.", ("whitespace_fixed",)),
    ("empty continuation", "Done.", "", "", "", ()),
]


@pytest.mark.parametrize(
    ("emitted", "continuation", "context", "expected_text", "expected_actions"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
def test_repair(
    emitted: str,
    continuation: str,
    context: str,
    expected_text: str,
    expected_actions: tuple[str, ...],
) -> None:
    result = repair(emitted, continuation, context)
    assert result.text == expected_text
    assert result.actions == expected_actions


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Sure, here is the rest:\nmore", True),
        ("Certainly! more", True),
        ("Continuing from where it stopped: more", True),
        ("...the mat", True),
        ("sure enough, it rained", False),
        ("Surely this works", False),
        ("the mat.", False),
    ],
)
def test_has_preface(text: str, expected: bool) -> None:
    assert has_preface(text) is expected
