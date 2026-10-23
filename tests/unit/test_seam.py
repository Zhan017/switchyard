import pytest

from switchyard.streaming.seam import has_preface, is_meta_reply, repair

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


PY = f"{FENCE}python\n"

# Real joins from the first week 1 run (eval/seam/results/20261023-050151), shortened.
REAL_CASES = [
    # (name, emitted, continuation, expected_text, expected_actions)
    (
        "p16 code: next line without its line break",
        f"{PY}        self._head.next = self._tail",
        "        self._tail.prev = self._head\n",
        "\n        self._tail.prev = self._head\n",
        ("newline_inserted",),
    ),
    (
        "p20 code: identifier cut after a capital",
        f"{FENCE}go\n\treturn http.HandlerF",
        "unc(func(w http.ResponseWriter",
        "unc(func(w http.ResponseWriter",
        (),
    ),
    (
        "p16 code: keyword then next word",
        f"{PY}            assert least_recent is",
        "not None",
        " not None",
        ("space_inserted",),
    ),
    (
        "p24 code: space after an arrow",
        f"{FENCE}rust\n    fn default() ->",
        "Self {",
        " Self {",
        ("space_inserted",),
    ),
    (
        "p21 code: comment text after #",
        f"{PY}        result.error = str(exc)\n\n        #",
        "If this was the last attempt, give up.",
        " If this was the last attempt, give up.",
        ("space_inserted",),
    ),
    (
        "p22 code: fence and line both restarted",
        f"{FENCE}javascript\n    // don't affect this dispatch.\n    for",
        f"{FENCE}javascript\n    for (const listener of [...listeners]) {{",
        " (const listener of [...listeners]) {",
        ("fence_stripped", "line_restarted"),
    ),
    (
        "p17 code: keyword line restarted",
        f"{PY}MonthlyTotals = Dict[str, int]\n\n\nclass",
        f"{PY}class TransactionsCSVError(ValueError):",
        " TransactionsCSVError(ValueError):",
        ("fence_stripped", "line_restarted"),
    ),
    (
        "p08 markdown: one star then a bold title",
        "the way you want.\n- *",
        "**A clear sync status.** Each note",
        "*A clear sync status.** Each note",
        ("markdown_fixed",),
    ),
    (
        "p10 markdown: star that completes a bold close",
        "- **Hidden workload:*",
        "* Employees may absorb the work",
        "* Employees may absorb the work",
        (),
    ),
    (
        "p23 list: bullet restarted",
        "**Argument validation**\n-",
        "- `if [[ $# -ne 2 ]]` checks the arguments",
        " `if [[ $# -ne 2 ]]` checks the arguments",
        ("line_restarted",),
    ),
    (
        "p04 table: row marker restarted",
        "commute or relocate. |\n|",
        "| **Mentoring** | Requires deliberate effort |",
        " **Mentoring** | Requires deliberate effort |",
        ("line_restarted",),
    ),
    (
        "p09 meta reply",
        "Since partitions can happen in",
        "I can't pick up mid-sentence from that earlier message, since I don't have it.",
        "I can't pick up mid-sentence from that earlier message, since I don't have it.",
        ("meta_reply",),
    ),
]


@pytest.mark.parametrize(
    ("emitted", "continuation", "expected_text", "expected_actions"),
    [case[1:] for case in REAL_CASES],
    ids=[case[0] for case in REAL_CASES],
)
def test_repair_real_joins(
    emitted: str, continuation: str, expected_text: str, expected_actions: tuple[str, ...]
) -> None:
    result = repair(emitted, continuation)
    assert result.text == expected_text
    assert result.actions == expected_actions


ANCHORED_CASES = [
    # (name, emitted, continuation, expected_text)
    (
        "copied words trimmed, model's spacing kept",
        "The message had been in the water",
        "in the water for fifty years.",
        " for fifty years.",
    ),
    ("one copied word is enough", "Mara walked", "walked up the hill.", " up the hill."),
    (
        "copied identifier finishes a cut word",
        f"{FENCE}go\n\treturn http.HandlerF",
        "return http.HandlerFunc(func(w",
        "unc(func(w",
    ),
    (
        "copied Markdown",
        "- **Headlamp with",
        "- **Headlamp with spare batteries**: keeps hands free",
        " spare batteries**: keeps hands free",
    ),
    (
        "copied code line keeps its line break",
        f"{PY}        self._head.next = self._tail",
        "self._head.next = self._tail\n        self._tail.prev = self._head",
        "\n        self._tail.prev = self._head",
    ),
]


@pytest.mark.parametrize(
    ("emitted", "continuation", "expected_text"),
    [case[1:] for case in ANCHORED_CASES],
    ids=[case[0] for case in ANCHORED_CASES],
)
def test_repair_anchored(emitted: str, continuation: str, expected_text: str) -> None:
    result = repair(emitted, continuation, anchored=True)
    assert result.text == expected_text
    assert result.actions == ("overlap_trimmed",)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("I can't continue from that exact point, because it was cut off.", True),
        ("Sorry, that earlier code was incomplete. Here is all of it.", True),
        ("I can't stress enough how much sleep matters.", False),
        ("Unfortunately, the bridge closed in 1990.", False),
        ("and seen. Her example reminds us.", False),
    ],
)
def test_is_meta_reply(text: str, expected: bool) -> None:
    assert is_meta_reply(text) is expected
