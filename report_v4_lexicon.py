#!/usr/bin/env python3
"""report_v4_lexicon.py — the word lists the analyst take's free text is
checked against (report_v4_analyst.take_problems).

The model writes text with {{placeholders}}; code fills every number, unit
and direction word from the data. These patterns run on the model's OWN
words only (placeholders blanked) and reject anything that would let it
state a number, a direction, a size, a judgement or advice on its own.

Words are matched by stem (``soar\\w*`` covers soar, soars, soared,
soaring), case-insensitively, as whole words. One category per list; the
tests in report_v4_lexicon_test.py hold examples that must hit and
ordinary sentences that must not. Erring towards rejection only costs a
retry or an omitted section, never a wrong page.

To add a word: put its stem in the right list below and add a hit (and,
if the stem is short, a near-miss that must still pass) to the tests.
"""

import re

# A whole word (or stem) bounded by non-letters on both sides.
W = r"(?<![A-Za-z])(?:%s)(?![A-Za-z])"


def _rx(*alts, flags=re.IGNORECASE):
    return re.compile(W % "|".join(alts), flags)


# report_v4_analyst.free_text() replaces each placeholder with this letter,
# so a pattern can tell "one of the segments" (plain wording) from "one of
# {{x}}" (a quantity claim on a value). Matched case-sensitively.
SLOT = "X"
_NOT_SLOT = r"(?!(?-i:%s)(?![A-Za-z]))" % SLOT
# A few set phrases use "one", "second" and "most" as plain wording, not a
# count or a ranking: "one risk is", "the second question is", "one of the
# segments", "most of the revenue". Next to a placeholder they stay banned.
_DET = r"(?:the|its|their|this|these|those|our)"
_SET_ONE = r"(?!\s+(?:risk|question)s?(?![A-Za-z]))(?!\s+of\s+%s\s+%s)" % (_DET, _NOT_SLOT)
_SET_SECOND = r"(?!\s+(?:risk|question)(?![A-Za-z]))"
_SET_MOST = r"(?!\s+of\s+%s\s+%s)" % (_DET, _NOT_SLOT)


# ── numbers, units, sizes ───────────────────────────────────────────────

NUMBER_WORDS = (
    r"zero|one"
    + _SET_ONE
    + r"|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen",
    r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty",
    r"sixty|seventy|eighty|ninety|hundreds?|thousands?|millions?|billions?|trillions?",
    r"dozens?|percent|percents|percentage|percentages|pct|bps|basis\s+points?",
    r"half|halves|halved|halving|thirds?",
    r"doubl\w*|twice|tripl\w*|quadrupl\w*|times",
    r"first|second"
    + _SET_SECOND
    + r"|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth",
    r"\w+teenth|twentieth",
    r"dollars?|cents?|usd",
    r"(?:a|one|two|three)[\s-]+quarters?|quarters?\s+of",
)
# Size words that state a multiple or an amount without a digit:
# "several-fold", "tenfold", "multiple-fold", "x times", "numerous".
MAGNITUDE = (
    r"\w*[\s-]?fold",
    r"several|numerous|countless|manifold|multifold|umpteen",
    r"x\s+times|many\s+times|multiple\s+times",
    r"order\s+of\s+magnitude|orders\s+of\s+magnitude",
)
# Words that set a bound or precision on a value. A placeholder prints its
# own qualifier ("at least $1.00B") from the data; the model may not add,
# remove or contradict one.
QUALIFIERS = (
    r"exactly|precisely|approximately|approx|roughly|nearly|almost",
    r"at\s+least|at\s+most|no\s+less\s+than|no\s+more\s+than|upwards\s+of",
    r"in\s+excess\s+of|north\s+of|south\s+of|just\s+(?:over|under|shy\s+of)",
)
NUMBER_RX = _rx(*NUMBER_WORDS, *MAGNITUDE, *QUALIFIERS)
# A qualifier or put-down written right before a placeholder: "over
# {{revenue}}" prints "over $5.00B" for an exact figure, "a mere {{cash}}"
# adds an opinion. The placeholder prints its own bound; the model may not.
# Run on the TEMPLATE (placeholders intact); a little whitespace or
# punctuation may sit between the word and the "{{".
BEFORE_PLACEHOLDER = (
    r"over|under|about|around|approximately|approx|circa|close\s+to|as\s+much\s+as",
    r"as\s+little\s+as|as\s+many\s+as|as\s+few\s+as|up\s+to|at\s+least|at\s+most",
    r"more\s+than|less\s+than|fewer\s+than|nearly|almost|mere|merely|only|barely|just",
    r"roughly|some|fully|well|a\s+full|a\s+whopping|whopping|scant|paltry|modest",
)
BEFORE_PLACEHOLDER_RX = re.compile(
    r"(?<![A-Za-z])(?:%s)(?![A-Za-z])(?=[\s,.:;\"'()\[\]~-]{0,4}\{\{)"
    % "|".join(BEFORE_PLACEHOLDER),
    re.IGNORECASE,
)
DIGIT_RX = re.compile(r"[0-9$%£¥€¼½¾²³¹]")
# Month and weekday names are matched capitalised (so the verb "may" and
# the noun "march" in lower case are not), plus relative day words.
MONTH_RX = re.compile(
    r"(?<![A-Za-z])(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|"
    r"Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)(?![A-Za-z])"
    r"|(?<![A-Za-z])(?:yesterday|tomorrow)(?![A-Za-z])",
    0,
)
DATE_RX = re.compile(r"\b\d{1,4}[-/.]\d{1,2}(?:[-/.]\d{1,4})?\b")

# ── direction, comparison, records ──────────────────────────────────────

# Movement verbs, by stem. A direction only ever comes from a placeholder
# ({{x:change}} prints "rose"/"fell" from the sign).
DIRECTION_STEMS = (
    r"rose|ris(?:e|es|ing|en)|fell|fall(?:s|ing|en)?",
    r"grew|grow\w*|declin\w*|increas\w*|decreas\w*|gain\w*",
    r"lost|los(?:e|es|ing)|loss|losses",
    r"skyrocket\w*|rocket\w*|soar\w*|surg(?:e|es|ed|ing)|jump\w*|spik\w*|leap\w*|leapt",
    r"ralli\w*|rally|climb\w*|balloon\w*|swell\w*|swollen|boom\w*",
    r"plung\w*|plummet\w*|crash\w*|sink\w*|sank|sunk|slid\w*|slide\w*|slump\w*",
    r"collaps\w*|crater\w*|tank\w*|tumbl\w*|drop\w*|dip|dips|dipp\w*|slip\w*|shed\w*",
    r"shrink\w*|shrank|shrunk|swing\w*|swung",
    r"rebound\w*|recover\w*|bounc\w*|accelerat\w*|decelerat\w*|slow(?:s|ed|ing|down\w*)?",
    r"expand\w*|expansion\w*|widen\w*|narrow\w*",
    # "contract" the verb, not "annual contract value" (a KPI's name).
    r"contract(?:ed|ing|ion|ions|ionary)|contracts?(?![\s-]+value)",
    r"improv\w*|worsen\w*|deteriorat\w*|erod\w*|erosion",
    # "whether margins hold up" says nothing about a direction.
    r"(?<!hold\s)(?<!holds\s)(?<!holding\s)up|down|upward\w*|downward\w*|upturn\w*|downturn\w*|uptick\w*|downtick\w*",
    r"beat|beats|beating|missed|miss|misses|outperform\w*|underperform\w*",
    r"exceed\w*|outpac\w*|surpass\w*|trail|trails|trailed|lag|lags|lagged|lagging",
    r"topp(?:ed|ing)",
)
COMPARISON = (
    r"higher|lower|above|below|ahead|behind",
    r"more\s+than|less\s+than|fewer\s+than|greater|smaller|larger|bigger|cheaper|pricier",
    r"profit|profits|profitab\w*|unprofitab\w*|deficits?|surplus\w*|positive|negative",
)
RECORDS = (
    r"highest|lowest|largest|smallest|biggest|fastest|slowest|strongest|weakest",
    r"best|worst|greatest|record\w*|all[\s-]time|ever|peak\w*|most" + _SET_MOST + r"|least",
    r"unprecedented|never|always",
)
DIRECTION_RX = _rx(*DIRECTION_STEMS, *COMPARISON, *RECORDS)

# ── judgement words ─────────────────────────────────────────────────────

# Evaluative adjectives: a verdict on the business the data does not
# state. ("cheap", "expensive", "attractive", "undervalued", "overvalued"
# are valuation verdicts and live in ADVICE.) "outstanding" is left out:
# "shares outstanding" is a plain fact.
EVALUATIVE = (
    r"bright\w*|strong|stronger|strongly|strength\w*|weak\w*|robust\w*|solid\w*",
    r"impressive\w*|stellar|dismal\w*|poor|poorly|healthy|healthier|unhealthy",
    r"excellent|excellence|terribl\w*|promising|concerning|concerns?|worr(?:y|ies|ied|ying)",
    r"worrisome|alarming\w*|troubl\w*|encourag\w*|disappoint\w*|remarkabl\w*",
    r"exceptional\w*|lacklust(?:er|re)|sluggish\w*|great|good|bad|badly",
    r"favou?rabl\w*|unfavou?rabl\w*|tremendous\w*|superb|stunning|spectacular|awful",
    r"rosy|bleak|grim|gloomy|upbeat|optimis\w*|pessimis\w*|bullish|bearish",
    r"stable|stability|resilien\w*|fragile|risky|safe|safer|dangerous\w*",
)
EVALUATIVE_RX = _rx(*EVALUATIVE)

# ── advice ──────────────────────────────────────────────────────────────

ADVICE = (
    r"buy|buys|buying|buyers?|sell|sells|selling|sellers?|sell[\s-]side",
    # "whether margins hold up" is not advice to hold the shares.
    r"(?:hold|holds|holding)(?!\s+up(?![A-Za-z]))|invest|invests|invested|investing",
    r"accumulat\w*|allocat\w*|position|positions|positioned|positioning",
    r"portfolios?|savings?|should|must|ought|recommend\w*|advis\w*|advice",
    r"consider\w*|entry|entries|exit|exits|exiting|targets?|targeted|targeting",
    r"stop|stops|stop[\s-]loss|trim\w*|add(?:s|ed|ing)?\s+(?:to|more)",
    r"upside|downside|ratings?|rated|fair\s+value|undervalu\w*|overvalu\w*",
    r"cheap|expensive|bargain\w*|attractive\w*|compelling|opportunit\w*|worth",
    r"overweight|underweight|market[\s-]perform",
    r"short(?:s|ed|ing)?(?![\s-](?:term|run|sentences?))|go(?:es|ing)?\s+long|long\s+position",
    r"owning|own\s+(?:the|these|this|its)\s+(?:stock|shares)",
    r"you|your|yours|yourself",
    r"time\s+to|act\s+now|avoid\w*|guarantee\w*|speculat\w*|risk[\s-]free",
    r"take\s+profits?|bet|bets|betting|wager\w*",
)
# "buyback(s)" is a corporate action, not advice.
ADVICE_RX = re.compile(
    r"(?<![A-Za-z])(?:%s)(?![A-Za-z])(?![\s-]?backs?(?![A-Za-z]))" % "|".join(ADVICE),
    re.IGNORECASE,
)

# ── tickers ─────────────────────────────────────────────────────────────

# Capitalised abbreviations that are vocabulary, not tickers.
ACRONYMS = frozenset(
    [
        "AI",
        "ACV",
        "ARR",
        "CRPO",
        "RPO",
        "EPS",
        "GAAP",
        "SEC",
        "XBRL",
        "FCF",
        "EV",
        "PE",
        "YOY",
        "QOQ",
        "TTM",
        "LTM",
        "RSI",
        "SMA",
        "EMA",
        "ETF",
        "US",
        "UK",
        "EU",
        "CEO",
        "CFO",
        "COO",
        "CTO",
        "IPO",
        "FDA",
        "EBIT",
        "EBITDA",
        "SAAS",
        "IT",
        "AND",
        "OR",
        "OK",
        "FY",
        "NA",
        "EMEA",
        "APAC",
        "GA",
        "ROE",
        "ROIC",
        "ROA",
        "ESG",
        "ET",
        "UTC",
        "NYSE",
        "NASDAQ",
        "NOTE",
        "II",
        "III",
        "IV",
        "DATA",
        "P/E",
    ]
)
TICKER_RX = re.compile(r"\$?(?<![A-Za-z])[A-Z]{2,5}(?:\.[A-Z])?(?![A-Za-z])")


def hits(rx, text, lower=True):
    """The distinct matches of rx in text, sorted."""
    return sorted({(m.group(0).lower() if lower else m.group(0)) for m in rx.finditer(text)})
