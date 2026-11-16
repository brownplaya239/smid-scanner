#!/usr/bin/env python3
"""report_v4_lexicon_test.py — the analyst take's word lists.

Each list must catch every listed word in its common forms (matched by
stem) and must leave ordinary report wording alone, so a correct draft is
not rejected for vocabulary it needs.

    uv run pytest report_v4_lexicon_test.py
"""

import pytest

import report_v4_lexicon as LX

# ── direction and movement, by stem ─────────────────────────────────────

MOVEMENT = [
    "skyrocketed", "skyrockets", "soared", "soaring", "surged", "surge", "jumped", "jumps",
    "spiked", "spiking", "rallied", "rally", "rallies", "climbed", "climbing", "rocketed",
    "plunged", "plummeting", "crashed", "sinks", "sank", "sunk", "slid", "slides", "slump",
    "collapsed", "cratered", "tanked", "ballooned", "shrank", "shrinking", "swelled",
    "swollen", "rebounded", "recovering", "accelerated", "decelerating", "expanded",
    "contracted", "contracting", "widened", "narrowing", "slowed", "dropped", "dipped",
    "rose", "fell", "grew", "declined", "increased", "gained", "improved", "eroded",
]  # fmt: skip


@pytest.mark.parametrize("word", MOVEMENT)
def test_movement_words_are_caught_by_stem(word):
    assert LX.hits(LX.DIRECTION_RX, "Revenue %s." % word) == [word.lower()]


@pytest.mark.parametrize(
    "text",
    [
        "Revenue skyrocketed.",
        "Margins Soared in the latest quarter.",
        "Free cash flow TANKED.",
        "The gap widened.",
    ],
)
def test_reviewer_direction_examples_are_caught(text):
    assert LX.hits(LX.DIRECTION_RX, text)


# ── size and bound words ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Revenue is several-fold.",
        "Revenue is several fold the cost.",
        "Revenue grew tenfold.",
        "Revenue is many-fold larger.",
        "Revenue is multiple-fold.",
        "It is two-fold.",
        "It is three-fold.",
        "Cash is x times debt.",
        "Cash is many times debt.",
        "There are numerous deals.",
        "Cash is an order of magnitude larger.",
        "Revenue was exactly that.",
        "Revenue was approximately that.",
        "Revenue was roughly that.",
        "AI contract value is at least that.",
        "Revenue is in excess of that.",
        "Revenue was nearly that.",
    ],
)
def test_size_and_bound_words_are_caught(text):
    assert LX.hits(LX.NUMBER_RX, text), text


# ── judgement words ─────────────────────────────────────────────────────

EVALUATIVE = [
    "bright", "strong", "weak", "weakness", "robust", "solid", "impressive", "stellar",
    "dismal", "poor", "healthy", "excellent", "terrible", "promising", "concerning", "good",
    "bad", "great", "encouraging", "disappointing", "upbeat", "bleak", "resilient",
]  # fmt: skip


@pytest.mark.parametrize("word", EVALUATIVE)
def test_evaluative_words_are_caught(word):
    assert LX.hits(LX.EVALUATIVE_RX, "The result is %s." % word) == [word]


def test_the_outlook_is_bright_is_caught():
    assert LX.hits(LX.EVALUATIVE_RX, "The outlook is bright.") == ["bright"]


@pytest.mark.parametrize("word", ["cheap", "expensive", "attractive", "undervalued", "overvalued"])
def test_valuation_verdicts_are_advice(word):
    assert LX.hits(LX.ADVICE_RX, "The shares look %s." % word) == [word]


# ── ordinary report wording must pass ───────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "The company reports X for annual contract value.",
        "The AI annual contract value on file is X.",
        "Shares outstanding are X.",
        "The shares trade at X trailing earnings.",
        "Every figure here comes from the filing.",
        "The next listed event is on X.",
        "Operating cash flow was X in the latest quarter.",
        "Customers in this group number X.",
        "Share buybacks are not in this data.",
        "The company folds X into its guidance table.",
    ],
)
def test_plain_report_wording_passes_every_list(text):
    for rx in (LX.NUMBER_RX, LX.DIRECTION_RX, LX.EVALUATIVE_RX, LX.ADVICE_RX):
        assert not LX.hits(rx, text), (text, rx.pattern[:40], LX.hits(rx, text))


def test_strength_is_a_judgement_word():
    assert LX.hits(LX.EVALUATIVE_RX, "the strength of this measure") == ["strength"]


def test_contract_the_noun_in_a_kpi_name_is_allowed_but_the_verb_is_not():
    assert not LX.hits(LX.DIRECTION_RX, "annual contract value")
    assert LX.hits(LX.DIRECTION_RX, "margins contracted") == ["contracted"]
    assert LX.hits(LX.DIRECTION_RX, "margins contract") == ["contract"]


# ── re-check 3: honest set phrases; qualifiers before a placeholder ─────


@pytest.mark.parametrize(
    "text",
    [
        "The question is whether margins hold up.",
        "One risk is the renewal cycle.",
        "One question is how the segment mix changes.",
        "The second question is how the segment mix changes.",
        "The second risk is customer concentration.",
        "Most of the revenue comes from X.",
        "This is one of the figures to watch.",
    ],
)
def test_reviewer_honest_sentences_pass_every_list(text):
    for rx in (LX.NUMBER_RX, LX.DIRECTION_RX, LX.EVALUATIVE_RX, LX.ADVICE_RX):
        assert not LX.hits(rx, text), (text, rx.pattern[:40], LX.hits(rx, text))


@pytest.mark.parametrize(
    "text,rx",
    [
        ("Most of X is recurring.", "DIRECTION_RX"),
        ("One of X is recurring.", "NUMBER_RX"),
        ("It is one of the X segments.", "NUMBER_RX"),
        ("The second quarter is on file.", "NUMBER_RX"),
        ("This is the most in its group.", "DIRECTION_RX"),
        ("One customer accounts for X.", "NUMBER_RX"),
        ("Revenue is up.", "DIRECTION_RX"),
        ("Revenue held up.", "DIRECTION_RX"),
        ("Hold the shares.", "ADVICE_RX"),
        ("Keep holding them.", "ADVICE_RX"),
    ],
)
def test_set_phrase_exceptions_stay_narrow(text, rx):
    assert LX.hits(getattr(LX, rx), text), text


@pytest.mark.parametrize(
    "text,want",
    [
        ("over {{revenue_q}}", ["over"]),
        ("a mere {{cash}}", ["mere"]),
        ("about, {{cash}}", ["about"]),
        ("only ({{cash}})", ["only"]),
        ("close to {{cash}}", ["close to"]),
        ("as much as {{cash}}", ["as much as"]),
        ("at least {{cash}}", ["at least"]),
        ("up to {{cash}}", ["up to"]),
        ("barely {{cash}}", ["barely"]),
        ("just {{cash}}", ["just"]),
    ],
)
def test_qualifier_right_before_a_placeholder_is_caught(text, want):
    assert LX.hits(LX.BEFORE_PLACEHOLDER_RX, text) == want


@pytest.mark.parametrize(
    "text",
    [
        "Revenue of {{revenue_q}} is on file.",
        "Over the latest quarter, revenue was {{revenue_q}}.",
        "It is about the renewal cycle, with cash at {{cash}}.",
        "Turnover {{x}} is a word that ends in over.",
        "The handover {{x}} happened.",
    ],
)
def test_qualifier_words_elsewhere_are_left_alone(text):
    assert not LX.hits(LX.BEFORE_PLACEHOLDER_RX, text), text
