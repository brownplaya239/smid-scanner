#!/usr/bin/env python3
"""report_v4_analyst.py — the "Analyst take" section, written by Claude
Opus 5.5 from the report's own data and checked against it.

The v4 report's promise is that every number on the page reconciles to a
source. An AI-written section must not weaken that, so this module is
built around three rules:

  1. Claude sees ONLY the evidence pack: the already-validated view the
     PDF renders, flattened into short items with stable ids
     (evidence_pack). Nothing is fetched for it. The Street rating, the
     consensus price target and the scenario prices are deliberately left
     out, so the section cannot echo a recommendation.
  2. Every point Claude writes cites evidence ids, and take_problems()
     checks the text against exactly those items: every number and date
     must appear in a cited item (rounding allowed), every cited id must
     exist, no ticker outside the data, no buy/sell/hold or position
     advice, and every part has a length cap. The gate in
     report_v4_validate re-runs the same checks on the shipped view, and
     report_v4_mutation proves each one can fail.
  3. The section can never stop a report shipping. An invalid draft gets
     one retry with the checker's complaints; a second failure, a refusal,
     an API error or a missing key leaves the section out and the report
     carries one line instead: "Analyst take unavailable for this report".
     Unvalidated AI text never reaches the page.

    generate(view, snap) -> dict   # the view["analyst_take"] record
"""

import json
import math
import os
import re

MODEL = "claude-opus-5-5"
# medium: the job is a short, grounded summary of ~100 facts, not open
# research. Opus 5.5 at medium already beats Opus 5 at high on knowledge
# work (Anthropic's migration notes) and is its API default; low risks a
# careless number (= a wasted retry), high costs more for no visible gain
# on a 4,000-character section.
EFFORT = "medium"
# Thinking counts toward max_tokens. The JSON answer is ~1.5K tokens; the
# rest is room to think. Large enough that the SDK wants streaming.
MAX_TOKENS = 16000
FALLBACK_BETA = "server-side-fallback-2026-07-01"
PRICE_IN_PER_M = 4.0  # USD per million input tokens (Opus 5.5)
PRICE_OUT_PER_M = 20.0  # USD per million output tokens (thinking included)

UNAVAILABLE_NOTE = "Analyst take unavailable for this report."
LABEL_FMT = "Analyst take, written by %s from the data in this report. Not investment advice."
# What the rendered-text check looks for; present in every label.
LABEL_MARK = "from the data in this report. Not investment advice."

# Length caps (characters / counts). Enforced by take_problems, stated in
# the prompt, and sized so the section fits one page.
SUMMARY_MAX_CHARS = 600
SUMMARY_SENTENCES = (2, 3)
POINT_MAX_CHARS = 220
BULL_RANGE = (1, 3)
BEAR_RANGE = (1, 3)
WATCH_RANGE = (1, 3)
ITEM_MAX_CHARS = 500

# ── text normalisation ──────────────────────────────────────────────────

_ASCII = {
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": " - ",
    "−": "-",
    "…": "...",
    " ": " ",
    " ": " ",
    " ": " ",
    "•": "-",
}


def plain(text):
    """Plain latin-1 text with collapsed whitespace. Applied to evidence
    and to Claude's output BEFORE validation, so the text that is checked
    is byte-for-byte the text the renderer draws (report_v3.safe() then
    has nothing left to drop)."""
    s = str(text or "")
    for k, v in _ASCII.items():
        s = s.replace(k, v)
    s = s.encode("latin-1", "ignore").decode("latin-1")
    s = "".join(max(ch, " ") for ch in s)
    return re.sub(r"\s+", " ", s).strip()


# ── the evidence pack ───────────────────────────────────────────────────


def _num(x):
    try:
        f = float(x)
    except TypeError, ValueError:
        return None
    return f if math.isfinite(f) else None


def _money(v):
    v = _num(v)
    if v is None:
        return None
    a = abs(v)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return "%s$%.2f%s" % ("-" if v < 0 else "", a / div, suf)
    return "%s$%.2f" % ("-" if v < 0 else "", a)


def _fact_v(f):
    if isinstance(f, dict):
        return _num(f.get("v", f.get("value")))
    return _num(f)


_FIN_LABELS = {
    "revenue_q": ("Quarterly revenue", "money"),
    "revenue_growth": ("Revenue growth year over year, %", "num"),
    "gross_margin": ("Gross margin, %", "num"),
    "net_margin": ("Net margin, %", "num"),
    "net_income_q": ("Quarterly net income", "money"),
    "operating_cash_flow": ("Quarterly operating cash flow", "money"),
    "free_cash_flow": ("Quarterly free cash flow", "money"),
    "eps_ttm": ("Trailing twelve-month EPS, $", "num"),
    "cash": ("Cash and equivalents", "money"),
    "debt": ("Total debt", "money"),
}


def evidence_pack(view, snap=None):
    """Flatten the validated view into [{id, text}] — the only material
    Claude sees and the only material its numbers are checked against.

    Deterministic: the same view always yields the same pack, so the gate
    can rebuild it independently of the generator. The view's own
    analyst_take key is ignored. Excluded on purpose: the Street rating,
    the consensus target and the bull/base/bear scenario prices (they are
    recommendations or price targets by another name)."""
    snap = snap or {}
    items = []

    def add(eid, text):
        t = plain(text)
        if t:
            items.append({"id": eid, "text": t[:ITEM_MAX_CHARS]})

    tk = view.get("ticker") or snap.get("ticker") or ""
    co = snap.get("company") if isinstance(snap.get("company"), dict) else {}
    name = co.get("name")
    company = name.get("v") if isinstance(name, dict) else name
    add("COMPANY", "%s%s" % (tk, (" - %s" % company) if company else ""))
    if snap.get("report_time"):
        add("REPORT-DATE", "Report prepared %s" % str(snap["report_time"])[:10])
    px = _num(view.get("price"))
    if px is not None:
        add("PRICE", "Last price used in the report: $%.2f" % px)

    ev = view.get("event") or {}
    if ev.get("state_label"):
        add("EVENT", "Earnings event state: %s" % ev["state_label"])

    for i, b in enumerate(view.get("thesis") or [], 1):
        add("THESIS-%d" % i, (b or {}).get("text"))
    for i, b in enumerate(view.get("risks") or [], 1):
        add("RISK-%d" % i, (b or {}).get("text"))

    biz = view.get("business") or {}
    if biz.get("vendor_text"):
        add("BUSINESS", biz["vendor_text"])
    for i, s in enumerate(biz.get("sentences") or [], 1):
        add("BUSINESS-%d" % i, (s or {}).get("text"))
    for i, s in enumerate((view.get("business_analysis") or {}).get("sections") or [], 1):
        add(
            "ANALYSIS-%d" % i,
            "%s: %s" % ((s or {}).get("title") or "", (s or {}).get("text") or ""),
        )

    fin = view.get("financials") or {}
    rep = fin.get("reported") or {}
    for k, (label, kind) in _FIN_LABELS.items():
        v = _num(rep.get(k))
        if v is None:
            continue
        if kind == "money":
            add("FIN-%s" % k, "%s: %s (%.0f)" % (label, _money(v), v))
        else:
            add("FIN-%s" % k, "%s: %s" % (label, ("%.2f" % v).rstrip("0").rstrip(".")))
    for s in fin.get("surprises") or []:
        if not isinstance(s, dict):
            continue
        add(
            "SURPRISE-%s" % (s.get("period") or "?"),
            "EPS for the period ended %s: actual %s vs estimate %s, surprise %s%%"
            % (s.get("period"), s.get("actual"), s.get("estimate"), s.get("surprise_pct")),
        )
    kp = fin.get("saas_kpis") or {}
    for r in kp.get("rows") or [] if kp.get("available") else []:
        if not isinstance(r, dict):
            continue
        val = _num(r.get("value"))
        if val is None:
            shown = "n/a"
        elif "money" in str(r.get("kind") or "") or abs(val) >= 1e6:
            shown = _money(val)
        else:
            shown = ("%.2f" % val).rstrip("0").rstrip(".")
        g = r.get("growth_yoy_pct")
        add(
            "KPI-%s" % (r.get("key") or "x"),
            "%s: %s%s"
            % (
                r.get("label"),
                shown,
                (", growth %s%% year over year" % g) if g is not None else "",
            ),
        )

    val = view.get("valuation") or {}
    if val.get("available"):
        for k, label in (
            ("trailing_pe", "Trailing P/E"),
            ("ev_to_revenue", "EV / annualised revenue, x"),
            ("fcf_yield", "Free-cash-flow yield (run-rate), %"),
        ):
            f = val.get(k) or {}
            if f.get("available") and _num(f.get("value")) is not None:
                add("VAL-%s" % k, "%s: %s (%s)" % (label, f["value"], f.get("basis") or ""))
        f = val.get("enterprise_value") or {}
        if f.get("available") and _num(f.get("value")) is not None:
            add(
                "VAL-enterprise_value",
                "Enterprise value: %s; market cap %s"
                % (_money(f["value"]), _money(f.get("market_cap"))),
            )
        for r in (val.get("peers") or {}).get("rows") or []:
            if isinstance(r, dict) and r.get("ticker"):
                add(
                    "PEER-%s" % r["ticker"], "Peer %s trailing P/E: %s" % (r["ticker"], r.get("pe"))
                )

    tr = (view.get("ratings") or {}).get("tactical") or {}
    if tr.get("available"):
        add(
            "TECH-read",
            "Technical read of the price trend: %s. %s" % (tr.get("band"), tr.get("detail") or ""),
        )
    # The levels page 5 prints: its key-levels table and the moving
    # averages the technical read and the monitoring triggers refer to.
    lv = snap.get("levels") or {}
    sw = lv.get("swing_window") or (view.get("trading_history") or {}).get("swing_window") or 60
    for k, label in (
        ("resistance_major", "52-week closing high"),
        ("resistance", "%s-session closing high" % sw),
        ("support", "%s-session closing low" % sw),
        ("support_major", "52-week closing low"),
        ("ma20", "20-day moving average"),
        ("ma50", "50-day moving average"),
        ("ma200", "200-day moving average"),
    ):
        v = _fact_v(lv.get(k))
        if v is not None:
            add("LEVEL-%s" % k, "%s: $%.2f" % (label, v))

    cat = view.get("catalysts") or {}
    lr = cat.get("last_reported") or {}
    if lr.get("when"):
        add(
            "CAT-last",
            "Last reported: %s on %s" % (lr.get("what") or "earnings release", lr["when"]),
        )
    cd = cat.get("current_driver") or {}
    if cd.get("text"):
        add("CAT-driver", cd["text"])
    for i, n in enumerate(cat.get("next") or [], 1):
        if isinstance(n, dict) and n.get("when"):
            add("CAT-next-%d" % i, "Upcoming: %s on %s" % (n.get("what") or "event", n["when"]))

    var = view.get("variant") or {}
    if var.get("available"):
        add("VARIANT", var.get("text"))

    mon = view.get("monitoring") or {}
    for k, label in (
        ("upgrade_trigger", "Technical upgrade trigger"),
        ("downside_confirmation", "Downside confirmation"),
        ("monitor_next", "Next thing to monitor"),
    ):
        if mon.get(k):
            add("MON-%s" % k, "%s: %s" % (label, mon[k]))
    if mon.get("review_date"):
        add("MON-review_date", "Next scheduled review: %s" % mon["review_date"])

    ins = view.get("insiders") or {}
    for r in ins.get("rows") or []:
        if isinstance(r, dict) and r.get("n"):
            add(
                "INSIDER-%s" % r.get("key"),
                "%s in the last %s days: %s" % (r.get("label"), ins.get("window_days"), r["n"]),
            )

    dc = view.get("data_confidence") or {}
    if dc.get("level"):
        add(
            "DATA-confidence",
            "Data confidence %s: %s" % (dc["level"], "; ".join(dc.get("reasons") or [])),
        )
    return items


# ── checks ──────────────────────────────────────────────────────────────

_DATE_RX = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
# A number: optional $, digits with optional thousands commas and
# decimals, then an optional unit. Digits glued to letters (Q3, 10b5-1,
# 13G) are labels, not quantities, and are not matched.
_NUM_RX = re.compile(
    r"(?<![\w.$])(?<!\w-)\$?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s?(%|x\b|[KMBT]\b|thousand|million|billion|trillion))?(?![\w])",
    re.IGNORECASE,
)
_MULT = {
    "x": 1.0,
    "k": 1e3,
    "thousand": 1e3,
    "m": 1e6,
    "million": 1e6,
    "b": 1e9,
    "billion": 1e9,
    "t": 1e12,
    "trillion": 1e12,
}
# Periods and filing names, not quantities: "200-day", "52-week",
# "12-month", "10-K", "8-K", "10-Q".
_EXEMPT_RX = re.compile(
    r"\b\d+[- ](?:day|week|month|year|quarter|session)s?\b|\b\d{1,2}-[KQ]\b", re.IGNORECASE
)


def _numbers(text):
    """[(token, value, tolerance)] for every quantity in text. Tolerance is
    half a unit of the last digit shown, so 24% grounds 24.0 or 24.4 but
    not 25, and $3.99B grounds 3,987,000,000."""
    t = _EXEMPT_RX.sub(" ", _DATE_RX.sub(" ", text or ""))
    out = []
    for m in _NUM_RX.finditer(t):
        digits, unit = m.group(1), (m.group(2) or "").lower()
        mult = _MULT.get(unit, 1.0)
        v = float(digits.replace(",", ""))
        dec = len(digits.split(".")[1]) if "." in digits else 0
        out.append((m.group(0).strip(), v * mult, 0.5 * (10**-dec) * mult))
    return out


def _grounded(value, tol, ev_values):
    for e in ev_values:
        if abs(abs(value) - abs(e)) <= tol * (1 + 1e-9) + 1e-12:
            return True
    return False


_ADVICE_RX = re.compile(
    r"\b(?:buy|buys|buying|sell|sells|selling|hold|holding)\b(?![- ]?backs?\b)"
    r"|\bstrong[- ]buy\b|\b(?:outperform|underperform|overweight|underweight|accumulate|market[- ]perform)\b"
    r"|\bprice[- ]target|\btarget price|\bprice objective|\bfair value\b"
    r"|\b(?:undervalued|overvalued)\b"
    r"|\brecommend\w*"
    r"|\b(?:should|must|consider)\s+(?:\w+\s+)?(?:buying|selling|owning|adding|trimming|shorting|"
    r"buy|sell|own|add|trim|short|exit|enter|avoid|invest)\b"
    r"|\b(?:go|going)\s+(?:long|short)\b|\bshort\s+(?:the\s+)?(?:stock|shares|name)\b"
    r"|\btak(?:e|ing)\s+profits?\b|\bstop[- ]loss|\bposition[- ]siz\w*"
    r"|\bentry\s+(?:point|price|level)\b"
    r"|\b(?:add\s+to|trim|initiate|open|build|exit)\s+(?:a\s+|the\s+|your\s+)?position\b",
    re.IGNORECASE,
)

# Capitalised abbreviations that are vocabulary, not tickers.
_ACRONYMS = frozenset(
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
        "USD",
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
        "SG",
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
    ]
)
_TICKER_RX = re.compile(r"\$[A-Z]{1,5}(?:\.[A-Z])?\b|\b[A-Z]{2,5}(?:\.[A-Z])?\b")


def _allowed_upper(pack, ticker):
    ok = set(_ACRONYMS)
    if ticker:
        ok.add(ticker.upper())
    for it in pack:
        ok.update(re.findall(r"\b[A-Z][A-Z.]{1,6}\b", it["text"]))
        ok.update(re.findall(r"\b[A-Z][A-Z.]{1,6}\b", it["id"]))
    return ok


def _sentences(text):
    return [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", text.strip()) if s.strip()]


def _parts(take):
    """[(where, {text, evidence_ids})] for every cited part of the take."""
    out = []
    s = take.get("summary")
    if isinstance(s, dict):
        out.append(("summary", s))
    for key in ("bull_points", "bear_points", "watch_next"):
        for i, p in enumerate(take.get(key) or [], 1):
            if isinstance(p, dict):
                out.append(("%s[%d]" % (key, i), p))
    return out


def take_problems(take, pack, ticker):
    """Every way the take breaks the rules, as [(check_id, message)].
    Empty means it may ship. Shared by the generator (to decide and to
    tell Claude what to fix) and by the gate in report_v4_validate."""
    probs = []
    by_id = {it["id"]: it["text"] for it in pack}
    allowed_upper = _allowed_upper(pack, ticker)

    # Shape and lengths.
    if not isinstance(take, dict) or not isinstance(take.get("summary"), dict):
        return [("ANALYST_LENGTH_CAPS", "the response is missing the summary object")]
    summ = plain(take["summary"].get("text"))
    n_sent = len(_sentences(summ))
    if not (SUMMARY_SENTENCES[0] <= n_sent <= SUMMARY_SENTENCES[1]):
        probs.append(
            ("ANALYST_LENGTH_CAPS", "summary has %d sentences; it must have 2 or 3" % n_sent)
        )
    if len(summ) > SUMMARY_MAX_CHARS:
        probs.append(
            (
                "ANALYST_LENGTH_CAPS",
                "summary is %d characters; the cap is %d" % (len(summ), SUMMARY_MAX_CHARS),
            )
        )
    for key, (lo, hi) in (
        ("bull_points", BULL_RANGE),
        ("bear_points", BEAR_RANGE),
        ("watch_next", WATCH_RANGE),
    ):
        n = len(take.get(key) or [])
        if not (lo <= n <= hi):
            probs.append(
                ("ANALYST_LENGTH_CAPS", "%s has %d items; it must have %d to %d" % (key, n, lo, hi))
            )

    for where, p in _parts(take):
        text = plain(p.get("text"))
        ids = [str(i) for i in (p.get("evidence_ids") or [])]
        if not text:
            probs.append(("ANALYST_LENGTH_CAPS", "%s is empty" % where))
            continue
        if where != "summary" and len(text) > POINT_MAX_CHARS:
            probs.append(
                (
                    "ANALYST_LENGTH_CAPS",
                    "%s is %d characters; the cap is %d" % (where, len(text), POINT_MAX_CHARS),
                )
            )

        # Cited ids exist, and something is cited.
        if not ids:
            probs.append(("ANALYST_EVIDENCE_IDS", "%s cites no evidence id" % where))
        unknown = [i for i in ids if i not in by_id]
        if unknown:
            probs.append(
                (
                    "ANALYST_EVIDENCE_IDS",
                    "%s cites ids that do not exist: %s" % (where, ", ".join(unknown)),
                )
            )
        cited = " ".join(by_id[i] for i in ids if i in by_id)

        # Every number and date appears in a cited item.
        ev_vals = [v for _, v, _ in _numbers(cited)]
        ev_dates = set(_DATE_RX.findall(cited))
        # a date's year/month/day ground "Q3 2026" or "June 30"
        ev_vals += [float(x) for d in ev_dates for x in d.split("-")]
        for d in _DATE_RX.findall(text):
            if d not in ev_dates:
                probs.append(
                    (
                        "ANALYST_NUMBERS_GROUNDED",
                        "%s: date %s is not in the evidence it cites" % (where, d),
                    )
                )
        for tok, v, tol in _numbers(text):
            if not _grounded(v, tol, ev_vals):
                probs.append(
                    (
                        "ANALYST_NUMBERS_GROUNDED",
                        "%s: the number %s does not appear in the evidence it cites (%s)"
                        % (where, tok, ", ".join(ids) or "none"),
                    )
                )

        # No ticker outside the data.
        foreign = sorted(
            {m.group(0).lstrip("$") for m in _TICKER_RX.finditer(text)} - allowed_upper
        )
        if foreign:
            probs.append(
                (
                    "ANALYST_NO_FOREIGN_TICKERS",
                    "%s names %s, which is not in this report's data" % (where, ", ".join(foreign)),
                )
            )

        # No advice.
        adv = sorted({m.group(0).lower() for m in _ADVICE_RX.finditer(text)})
        if adv:
            probs.append(
                (
                    "ANALYST_NO_ADVICE",
                    "%s uses recommendation or advice wording: %s" % (where, ", ".join(adv)),
                )
            )
    return probs


def plain_ai(text):
    """plain(), minus the three characters the renderer treats as markup:
    report_v2._clean() unescapes entities and strips anything shaped like
    <tag>, so "margin < 5% and > 2%" would print differently from what was
    checked. Spelled out instead, before validation."""
    s = plain(text)
    s = s.replace("&", " and ").replace("<", " less than ").replace(">", " more than ")
    return re.sub(r"\s+", " ", s).strip()


def normalise_take(take):
    """The take with every text field passed through plain_ai() and ids as
    strings — the exact form that is validated and rendered."""
    out = {}
    s = take.get("summary") or {}
    out["summary"] = {
        "text": plain_ai(s.get("text")),
        "evidence_ids": [str(i) for i in s.get("evidence_ids") or []],
    }
    for key in ("bull_points", "bear_points", "watch_next"):
        out[key] = [
            {
                "text": plain_ai(p.get("text")),
                "evidence_ids": [str(i) for i in p.get("evidence_ids") or []],
            }
            for p in take.get(key) or []
            if isinstance(p, dict)
        ]
    return out


def model_display_name(model_id):
    """claude-opus-5-5 -> Claude Opus 5.5. Used for the page label, so a
    server-side fallback to another model is named honestly."""
    parts = str(model_id or "").split("-")
    if len(parts) < 2 or parts[0] != "claude":
        return str(model_id or "Claude")
    words, nums = [], []
    for p in parts[1:]:
        (nums if p.isdigit() else words).append(p)
    return " ".join(
        ["Claude"] + [w.capitalize() for w in words] + ([".".join(nums)] if nums else [])
    )


def label_for(take_record):
    return LABEL_FMT % model_display_name(
        (take_record.get("meta") or {}).get("model_served") or MODEL
    )


# ── the Claude call ─────────────────────────────────────────────────────

_POINT = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["text", "evidence_ids"],
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {
        "summary": _POINT,
        "bull_points": {"type": "array", "items": _POINT},
        "bear_points": {"type": "array", "items": _POINT},
        "watch_next": {"type": "array", "items": _POINT},
    },
    "required": ["summary", "bull_points", "bear_points", "watch_next"],
    "additionalProperties": False,
}

SYSTEM = """You write the "Analyst take" section of a stock research report. Readers are \
individual investors, many without a finance background. The report has already been \
built and checked; your section summarises what its data says.

You get a list of evidence items, each with an id. They are the only facts you may use.

Write:
- summary: 2 or 3 short sentences (under 600 characters) on what the data says about \
the business and the stock right now.
- bull_points: 2 or 3 points from the data that support a positive view.
- bear_points: 2 or 3 points from the data that support a negative view.
- watch_next: 1 to 3 things to watch next. Give a date only when an evidence item \
states it, written exactly as there (YYYY-MM-DD).
Every point is one or two plain-English sentences under 220 characters, and lists in \
evidence_ids the ids of every item it relies on.

Rules. An automatic checker rejects the whole section if any is broken:
- Every number and date you write must appear in an evidence item that the same point \
cites. You may round (24.4% -> 24%), but do not calculate new numbers: no differences, \
ratios, sums or percentage changes that are not already in the evidence.
- Cite only ids from the list.
- Do not mention any company or ticker other than those in the evidence. Avoid \
all-caps abbreviations that do not appear in the evidence.
- This is not advice. Never use the words buy, sell or hold, never give or mention a \
rating, price target, fair value or "undervalued"/"overvalued" verdict, and never say \
what a reader should do with a position.
- Explain jargon briefly the first time (for example, "cRPO, revenue already under \
contract for the next 12 months"), but keep numbers exactly as given.
If the evidence is thin, write less rather than stretch it."""


def _user_message(ticker, pack):
    lines = ["Ticker: %s" % ticker, "", "Evidence items:"]
    lines += ["[%s] %s" % (it["id"], it["text"]) for it in pack]
    return "\n".join(lines)


def _retry_message(previous_json, complaints):
    return (
        "Your previous draft was rejected by the checker:\n%s\n\n"
        "Previous draft:\n%s\n\n"
        "Write the whole section again, fixing every problem. Remove a point rather than "
        "keep a number you cannot cite."
        % ("\n".join("- %s" % c for c in complaints), previous_json)
    )


def _cost(inp, out):
    return round(inp / 1e6 * PRICE_IN_PER_M + out / 1e6 * PRICE_OUT_PER_M, 4)


def _make_client():
    import anthropic

    return anthropic.Anthropic(timeout=300.0, max_retries=2)


def _has_key():
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _call(client, ticker, pack, retry_note=None):
    """One request. Returns (status, take_or_None, info). status is
    'ok' | 'refused' | 'empty' | 'max_tokens' | 'bad_json'."""
    content = _user_message(ticker, pack)
    if retry_note:
        content += "\n\n" + retry_note
    with client.beta.messages.stream(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM,
        thinking={"type": "adaptive"},
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
        betas=[FALLBACK_BETA],
        fallbacks="default",
        messages=[{"role": "user", "content": content}],
    ) as stream:
        msg = stream.get_final_message()
    usage = getattr(msg, "usage", None)
    info = {
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "stop_reason": getattr(msg, "stop_reason", None),
        "model_served": getattr(msg, "model", None) or MODEL,
    }
    if info["stop_reason"] == "refusal":
        det = getattr(msg, "stop_details", None)
        info["refusal_category"] = getattr(det, "category", None) if det else None
        return "refused", None, info
    if info["stop_reason"] == "max_tokens":
        return "max_tokens", None, info
    text = "".join(
        b.text for b in (getattr(msg, "content", None) or []) if getattr(b, "type", None) == "text"
    )
    if not text.strip():
        return "empty", None, info
    try:
        data = json.loads(text)
    except ValueError:
        info["raw"] = text[:2000]
        return "bad_json", None, info
    if not isinstance(data, dict):
        return "bad_json", None, info
    return "ok", data, info


def _unavailable(status, meta, reason):
    meta = dict(meta, status=status, reason=reason)
    return {"available": False, "note": UNAVAILABLE_NOTE, "meta": meta}


def generate(view, snap=None, client=None):
    """Build view["analyst_take"]. Never raises: every failure path returns
    an unavailable record, so the report always ships."""
    ticker = str(view.get("ticker") or "").upper()
    meta = {
        "model_requested": MODEL,
        "effort": EFFORT,
        "attempts": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost_usd": 0.0,
        "pricing": "USD %.2f / %.2f per million input / output tokens"
        % (PRICE_IN_PER_M, PRICE_OUT_PER_M),
    }
    if view.get("flash"):
        return _unavailable("skipped", meta, "flash report (data hold): no analyst take")
    try:
        pack = evidence_pack(view, snap)
    except Exception as e:  # pragma: no cover - a pack bug must not block the report
        return _unavailable("error", meta, "evidence pack failed: %s" % e)
    meta["evidence_items"] = len(pack)
    if client is None:
        if not _has_key():
            return _unavailable("no_key", meta, "ANTHROPIC_API_KEY not set; section skipped")
        try:
            client = _make_client()
        except Exception as e:
            return _unavailable("error", meta, "client setup failed: %s" % type(e).__name__)

    retry_note = None
    last_status, last_reason = "invalid", ""
    for attempt in (1, 2):
        try:
            status, data, info = _call(client, ticker, pack, retry_note)
        except Exception as e:
            # anthropic.APIError subclasses (rate limit, 5xx after the SDK's
            # own retries, connection, timeout) and anything unexpected.
            meta["attempts"].append(
                {"attempt": attempt, "error": "%s: %s" % (type(e).__name__, str(e)[:200])}
            )
            return _unavailable("api_error", meta, "API error: %s" % type(e).__name__)
        meta["input_tokens"] += info["input_tokens"]
        meta["output_tokens"] += info["output_tokens"]
        meta["estimated_cost_usd"] = _cost(meta["input_tokens"], meta["output_tokens"])
        meta["model_served"] = info["model_served"]
        rec = {k: v for k, v in info.items() if k != "raw"}
        rec["attempt"] = attempt
        meta["attempts"].append(rec)
        if status == "refused":
            return _unavailable(
                "refused", meta, "model declined (category %s)" % info.get("refusal_category")
            )
        if status != "ok" or data is None:
            complaints = [
                "the response was %s, not the JSON object asked for" % status.replace("_", " ")
            ]
            prev = info.get("raw") or "(none)"
        else:
            take = normalise_take(data)
            probs = take_problems(take, pack, ticker)
            if not probs:
                return {"available": True, **take, "meta": dict(meta, status="ok")}
            complaints = [m for _, m in probs]
            prev = json.dumps(take, indent=1)
        rec["problems"] = complaints[:20]
        last_status, last_reason = (
            ("invalid" if status == "ok" else status),
            "; ".join(complaints[:5]),
        )
        retry_note = _retry_message(prev, complaints[:20])
    return _unavailable(last_status, meta, "failed the checks twice: %s" % last_reason)
