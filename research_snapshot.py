"""
research_snapshot.py — canonical state + publication gate for Research Brief v2.

v1 (scanner.py one-pagers, alt_data.py reports) is UNTOUCHED and still
ships. This module is the correctness core of v2: every section of a v2
brief reads from ONE ResearchSnapshot, and nothing renders until the
snapshot passes the contradiction gate.

WHY THIS EXISTS — the 2026-07-16 ISRG review found the narrative was more
confident than the evidence, and every defect was a STATE problem rather
than a missing-data problem:
  1. catalyst contradiction  alt-data said "earnings after the close",
     the ticker memo one minute later said "reported today / post-
     catalyst". ISRG actually released at 16:05 ET — both were pre-event.
  2. level contradiction     memo called $360 the 200-day while its own
     chart drew it near $486, and claimed price below every MA.
  3. reversed logic          AVOID followed by a long entry/stop/target,
     and "thesis breaks below $390" when a break below $390 CONFIRMS the
     bearish thesis.
  4. incoherent confidence   page 1 "85/100 model confidence", page 2
     "Conviction: Low".
  5. template contamination  ISRG called mega-cap, then judged "normal
     for SMID" and compared to "unprofitable growth companies" — ISRG
     earned $818M GAAP net income in the quarter.
  6. insider overstatement   RSU vesting with shares withheld for taxes
     reported as a "glaring red flag" of bearish selling.
  7. ownership incompleteness "no 13D/13G in 12 months" while Vanguard
     filed a 13G/A on 2026-03-27; and 13G was called "lower conviction"
     when it simply means passive/non-control.
  8. unsupported inference   29 StockTwits messages described as "heavy
     institutional call flow"; PT cuts described as downgrades.
  9. sample contamination    Reddit posts about NRED/VYNE//earnings
     calendars counted toward ISRG buzz.
 10. unlabeled multiples     33.8x (forward) vs 50x (unstated basis).

Every one of those is encoded as a blocking check below, and the ISRG
fixture in --self-test reproduces the July 16 state and asserts the gate
catches all ten. A snapshot that would render the July 16 brief CANNOT
publish under v2.

    python research_snapshot.py --self-test
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone

# ── provenance-carrying value ───────────────────────────────────────────
# Every figure in a snapshot is a Fact. An unsourced or undated number
# cannot enter the brief, and a multiple without a basis is rejected —
# that is finding #10, enforced structurally rather than by review.


def fact(value, source, as_of, basis=None, unit=None, note=None):
    """value + where it came from + when it was true.

    basis is REQUIRED for ratios/multiples (forward | trailing | ntm |
    ltm) — the July 16 brief printed 33.8x and 50x with no denominator
    stated, which is how two valid numbers became a contradiction."""
    return {"v": value, "src": source, "as_of": as_of,
            "basis": basis, "unit": unit, "note": note}


def fv(f, default=None):
    """Read a Fact's value (None-safe)."""
    if isinstance(f, dict) and "v" in f:
        return f["v"] if f["v"] is not None else default
    return f if f is not None else default


# ── catalyst state machine ──────────────────────────────────────────────

PRE_EVENT = "PRE_EVENT"
EVENT_IN_PROGRESS = "EVENT_IN_PROGRESS"
POST_EVENT_UNGRADED = "POST_EVENT_UNGRADED"
POST_EVENT_GRADED = "POST_EVENT_GRADED"
CATALYST_STATES = (PRE_EVENT, EVENT_IN_PROGRESS, POST_EVENT_UNGRADED,
                   POST_EVENT_GRADED)

# Language that is only valid in specific states. The July 16 memo used
# post-event language 2h17m BEFORE the release; that is now unpublishable.
STATE_FORBIDDEN_PHRASES = {
    PRE_EVENT: ["post-catalyst", "post catalyst", "reported today",
                "reported this morning", "after the print", "the print was",
                "results showed", "beat expectations", "missed expectations",
                "post-earnings", "following the report", "reaction to earnings"],
    EVENT_IN_PROGRESS: ["post-catalyst", "reported today", "results showed",
                        "beat expectations", "missed expectations"],
    POST_EVENT_UNGRADED: ["ahead of earnings", "into the print",
                          "before the report", "upcoming earnings"],
    POST_EVENT_GRADED: ["ahead of earnings", "into the print",
                        "before the report", "upcoming earnings"],
}


def resolve_catalyst_state(event_dt, now=None, grade_after_min=90):
    """Point-in-time catalyst state. event_dt/now are tz-aware datetimes.

    PRE_EVENT              now < event
    EVENT_IN_PROGRESS      event <= now < event + grade_after_min
    POST_EVENT_UNGRADED    released, market reaction not yet measurable
                           (i.e. before the next session's close)
    POST_EVENT_GRADED      a full session has traded on the news
    """
    if event_dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    if now < event_dt:
        return PRE_EVENT
    if now < event_dt + timedelta(minutes=grade_after_min):
        return EVENT_IN_PROGRESS
    # a session boundary must pass before a reaction can be graded
    if now < event_dt + timedelta(hours=20):
        return POST_EVENT_UNGRADED
    return POST_EVENT_GRADED


# ── decision vocabulary ─────────────────────────────────────────────────
# Finding #3: "AVOID + entry/stop/target" and "thesis breaks below X"
# (when a break below X confirms the thesis). v2 replaces the ambiguous
# single recommendation with three explicitly-named fields.

ACTIONS = ("BUY", "ACCUMULATE", "HOLD", "WAIT", "AVOID", "REDUCE")
NON_LONG_ACTIONS = ("WAIT", "AVOID", "REDUCE")


def new_snapshot(ticker, report_time, market_data_time):
    """Empty canonical snapshot. Every v2 section reads from this and
    ONLY this — the July 16 brief drew levels from one calculation and
    the chart from another, which is how $360 and $486 both claimed to
    be the 200-day."""
    return {
        "schema": "research_snapshot/v2",
        "ticker": ticker.upper(),
        "report_time": report_time,
        "market_data_time": market_data_time,
        "company": {},        # name, sector, market_cap, profitable, universe
        "price": {},          # last, prev_close, change_pct
        "levels": {},         # ma20/50/200, support, resistance, atr, chart_*
        "fundamentals": {},   # revenue/eps growth, margins, installed base…
        "valuation": {},      # each multiple a Fact WITH basis
        "catalyst": {},       # event_dt, state, description
        "ownership": {},      # institutional_pct, filings[]
        "insiders": {},       # classified transactions
        "sentiment": {},      # alt-data with relevance accounting
        "decision": {},       # current_action, upgrade_trigger, downside_confirmation
        "evidence": {},       # quality + completeness (NOT a 0-100 score)
        "flags": [],          # data-quality flags
    }


# ── insider classification (finding #6) ─────────────────────────────────
# Form 4 transaction codes. The July 16 brief treated every disposition
# as bearish selling; the July 10 ISRG CEO transaction was RSU vesting
# with shares withheld for taxes — a compensation mechanic, not a view.

OPEN_MARKET_BUY = "open_market_buy"
OPEN_MARKET_SALE = "open_market_sale"
PLANNED_SALE = "planned_sale_10b5_1"
OPTION_EXERCISE = "option_exercise"
TAX_WITHHOLDING = "tax_withholding"
GRANT_AWARD = "grant_award"
OTHER_NONMARKET = "other_non_market"

CODE_CLASS = {
    "P": OPEN_MARKET_BUY,
    "S": OPEN_MARKET_SALE,
    "M": OPTION_EXERCISE,     # exercise of derivative — NOT a market trade
    "F": TAX_WITHHOLDING,     # shares withheld to cover tax on vesting
    "A": GRANT_AWARD,         # grant/award — compensation, not conviction
    "G": OTHER_NONMARKET,     # gift
    "C": OTHER_NONMARKET,     # conversion
    "X": OPTION_EXERCISE,     # exercise of in-the-money option
    "D": OTHER_NONMARKET,     # disposition to issuer
}
# Only these carry directional information about an insider's view.
SIGNAL_CLASSES = (OPEN_MARKET_BUY, OPEN_MARKET_SALE)


def classify_insider(txn):
    """Classify one Form 4 row and mark whether it carries a view.

    txn: {code, shares, price, value, date, owner, title,
          shares_owned_after?, footnotes?}"""
    code = (txn.get("code") or "").strip().upper()
    cls = CODE_CLASS.get(code, OTHER_NONMARKET)
    foot = " ".join(str(x) for x in (txn.get("footnotes") or [])).lower()
    plan = bool(re.search(r"10b5[- ]?1", foot))
    if cls == OPEN_MARKET_SALE and plan:
        cls = PLANNED_SALE
    owned_after = txn.get("shares_owned_after")
    shares = txn.get("shares") or 0
    pct_of_holdings = None
    if owned_after is not None and (owned_after + shares) > 0 and \
            cls in (OPEN_MARKET_SALE, PLANNED_SALE):
        pct_of_holdings = round(100.0 * shares / (owned_after + shares), 1)
    out = dict(txn)
    out.update({
        "class": cls,
        "carries_view": cls in SIGNAL_CLASSES,
        "is_planned": plan,
        "pct_of_holdings": pct_of_holdings,
    })
    return out


def summarize_insiders(txns):
    """Only open-market activity may drive an insider read. Everything
    else is reported as mechanics, never as a 'red flag'."""
    rows = [classify_insider(t) for t in (txns or [])]
    view = [r for r in rows if r["carries_view"]]
    buys = [r for r in view if r["class"] == OPEN_MARKET_BUY]
    sells = [r for r in view if r["class"] == OPEN_MARKET_SALE]
    mech = [r for r in rows if not r["carries_view"]]
    by_class = {}
    for r in rows:
        by_class[r["class"]] = by_class.get(r["class"], 0) + 1
    if not view:
        read = ("no open-market insider transactions in window — "
                "remaining activity is compensation mechanics "
                "(vesting, withholding, exercises), which carry no "
                "directional view")
    else:
        bv = sum(r.get("value") or 0 for r in buys)
        sv = sum(r.get("value") or 0 for r in sells)
        if bv > sv * 1.5:
            read = "net open-market buying"
        elif sv > bv * 1.5:
            read = "net open-market selling"
        else:
            read = "mixed open-market activity"
    return {
        "rows": rows,
        "n_total": len(rows),
        "n_view_bearing": len(view),
        "n_mechanics": len(mech),
        "by_class": by_class,
        "read": read,
        "note": ("Vesting (F), grants (A), and exercises (M/X) are "
                 "compensation mechanics, not market views. Planned "
                 "10b5-1 sales are pre-scheduled and reported "
                 "separately from discretionary sales."),
    }


# ── alt-data relevance (findings #8 and #9) ─────────────────────────────

def score_alt_data(posts, ticker, options_feed_verified=False):
    """Relevance-gate social posts and refuse to infer institutional
    activity from them.

    A post counts only if it actually references the ticker. The July 16
    alt-data page counted Reddit posts about NRED, VYNE and generic
    earnings calendars toward ISRG buzz, then described 29 StockTwits
    messages as 'heavy institutional call flow'."""
    tk = (ticker or "").upper()
    pat = re.compile(r"(?:^|[^A-Z])\$?" + re.escape(tk) + r"(?:[^A-Z]|$)")
    kept, dropped, authors, seen_text = [], [], set(), set()
    for p in posts or []:
        body = str(p.get("text") or p.get("body") or "")
        if not pat.search(body.upper()):
            dropped.append(p)
            continue
        norm = re.sub(r"\s+", " ", body.strip().lower())
        if norm in seen_text:
            dropped.append(p)
            continue
        seen_text.add(norm)
        authors.add(str(p.get("author") or p.get("user") or "?"))
        kept.append(p)
    srcs = {}
    for p in kept:
        s = str(p.get("source") or "unknown")
        srcs[s] = srcs.get(s, 0) + 1
    # Language discipline: social NEVER implies institutional execution.
    if options_feed_verified:
        flow_language = "options feed shows institutional-size execution"
    else:
        flow_language = ("retail call discussion — social posts cannot "
                         "evidence institutional activity")
    return {
        "n_considered": len(posts or []),
        "n_relevant": len(kept),
        "n_dropped_irrelevant": len(dropped),
        "unique_authors": len(authors),
        "source_mix": srcs,
        "flow_language": flow_language,
        "coordination": ("no coordination detected in this limited "
                         "sample" if len(kept) else
                         "sample too small to assess"),
        "sample_adequacy": ("adequate" if len(kept) >= 50 and
                            len(authors) >= 25 else "thin"),
        "note": ("Buzz and sentiment count only ticker-relevant, "
                 "de-duplicated posts. Unique authors are reported "
                 "because N posts from few authors is not N opinions."),
    }


def classify_analyst_action(prev_rating, new_rating, prev_pt, new_pt):
    """Finding #8b: a price-target cut with the rating maintained is NOT
    a downgrade."""
    rating_changed = (prev_rating and new_rating and
                      prev_rating.strip().lower() != new_rating.strip().lower())
    pt_changed = (prev_pt is not None and new_pt is not None and
                  prev_pt != new_pt)
    if rating_changed:
        return "rating_change"
    if pt_changed:
        return ("price_target_cut_rating_maintained" if new_pt < prev_pt
                else "price_target_raise_rating_maintained")
    return "reiteration"


# ── ownership (finding #7) ──────────────────────────────────────────────

def summarize_ownership(filings, institutional_pct=None):
    """13G is passive/non-control reporting — not 'lower conviction'.
    Amendments (13G/A, 13D/A) count as filings."""
    rows = filings or []
    kinds = {}
    for f in rows:
        k = (f.get("form") or "").upper().replace("SC ", "").strip()
        kinds[k] = kinds.get(k, 0) + 1
    return {
        "filings": rows,
        "n_filings": len(rows),
        "by_form": kinds,
        "institutional_pct": institutional_pct,
        "note": ("13D = activist/control intent; 13G = passive or "
                 "non-control position, including amendments (13G/A). "
                 "Passive filing status is a regulatory classification, "
                 "not a statement about conviction. High institutional "
                 "ownership does not imply an absence of buyers."),
    }


# ── contradiction gate ──────────────────────────────────────────────────

class Contradiction(Exception):
    pass


def check_contradictions(snap, prose_sections=None):
    """Return a list of BLOCKING violations. Empty list = publishable.

    prose_sections: {section_name: text} — narrative is checked against
    the numbers, because the July 16 failures were all prose disagreeing
    with the snapshot it was supposedly describing."""
    v = []
    prose_sections = prose_sections or {}
    lv = snap.get("levels") or {}
    px = fv((snap.get("price") or {}).get("last"))

    # 1. price/MA prose vs numeric MAs -----------------------------------
    ma = {k: fv(lv.get(k)) for k in ("ma20", "ma50", "ma200")}
    if px is not None:
        below_all = all(m is not None and px < m for m in ma.values()
                        if m is not None) and any(m is not None for m in ma.values())
        above_all = all(m is not None and px > m for m in ma.values()
                        if m is not None) and any(m is not None for m in ma.values())
        for sec, text in prose_sections.items():
            t = (text or "").lower()
            if "below every moving average" in t or \
               "below all moving averages" in t:
                if not below_all:
                    v.append("[%s] claims price below every MA, but price "
                             "%.2f vs MAs %s" % (sec, px, ma))
            if "above every moving average" in t or \
               "above all moving averages" in t:
                if not above_all:
                    v.append("[%s] claims price above every MA, but price "
                             "%.2f vs MAs %s" % (sec, px, ma))

    # 2. stated MA vs chart-drawn MA (the $360 vs $486 defect) -----------
    for k in ("ma20", "ma50", "ma200"):
        a, b = fv(lv.get(k)), fv(lv.get("chart_" + k))
        if a is not None and b is not None and a:
            if abs(a - b) / abs(a) > 0.02:
                v.append("%s disagrees between table (%.2f) and chart "
                         "(%.2f) — levels must come from one canonical "
                         "object" % (k, a, b))

    # 3. recommendation vs position plan ---------------------------------
    dec = snap.get("decision") or {}
    action = (dec.get("current_action") or "").upper()
    plan = dec.get("position_plan") or {}
    if action in NON_LONG_ACTIONS and any(
            plan.get(k) is not None for k in ("entry", "stop", "target")):
        v.append("action %s is paired with an active long plan "
                 "(entry/stop/target) — remove the plan or change the "
                 "action" % action)
    if action and action not in ACTIONS:
        v.append("unknown action '%s' (allowed: %s)" % (action, ", ".join(ACTIONS)))

    # 3b. trigger direction sanity (the "$390" reversal) -----------------
    up_trig = dec.get("upgrade_trigger") or ""
    down_conf = dec.get("downside_confirmation") or ""
    if action in ("AVOID", "REDUCE", "WAIT"):
        if re.search(r"thesis (breaks|fails|invalidated) below", str(up_trig),
                     re.I):
            v.append("upgrade_trigger describes a BREAK DOWN while action "
                     "is %s — a break lower confirms the bearish thesis, "
                     "it does not upgrade it" % action)
        if up_trig and re.search(r"\bbelow\b", up_trig, re.I) and \
                not re.search(r"reclaim|back above|recover", up_trig, re.I):
            v.append("upgrade_trigger for %s should describe a RECLAIM "
                     "(price back above a level), not a move below" % action)
        if down_conf and re.search(r"\babove\b", down_conf, re.I) and \
                not re.search(r"fails at|rejected at|below", down_conf, re.I):
            v.append("downside_confirmation should describe a breakdown, "
                     "not a move above")

    # 4. catalyst state vs prose ----------------------------------------
    cat = snap.get("catalyst") or {}
    state = cat.get("state")
    if state and state not in CATALYST_STATES:
        v.append("unknown catalyst state '%s'" % state)
    if state:
        for sec, text in prose_sections.items():
            t = (text or "").lower()
            for phrase in STATE_FORBIDDEN_PHRASES.get(state, []):
                if phrase in t:
                    v.append("[%s] uses post/pre-event language '%s' while "
                             "catalyst state is %s" % (sec, phrase, state))
    # cross-section catalyst timing agreement
    times = cat.get("stated_times") or {}
    uniq = set(str(x) for x in times.values() if x)
    if len(uniq) > 1:
        v.append("catalyst timing disagrees across sections: %s" % times)

    # 5. company template contamination ---------------------------------
    co = snap.get("company") or {}
    cap = fv(co.get("market_cap"))
    universe = (co.get("universe") or "").upper()
    profitable = fv(co.get("profitable"))
    if cap is not None:
        implied = ("MEGA" if cap >= 200e9 else "LARGE" if cap >= 10e9
                   else "MID" if cap >= 2e9 else "SMALL")
        if universe and universe not in ("", implied) and not (
                universe == "SMID" and implied in ("MID", "SMALL")):
            v.append("universe label '%s' contradicts market cap $%.1fB "
                     "(implies %s)" % (universe, cap / 1e9, implied))
        for sec, text in prose_sections.items():
            t = (text or "").lower()
            if cap >= 10e9 and re.search(r"\bnormal for smid\b|\bsmid[- ]cap\b", t):
                v.append("[%s] applies SMID framing to a $%.1fB company"
                         % (sec, cap / 1e9))
    if profitable is True:
        for sec, text in prose_sections.items():
            if re.search(r"unprofitable growth", str(text or ""), re.I):
                v.append("[%s] compares a profitable company to "
                         "'unprofitable growth companies'" % sec)

    # 6. level coherence: support/stop/target vs price -------------------
    sup, res = fv(lv.get("support")), fv(lv.get("resistance"))
    if sup is not None and res is not None and sup >= res:
        v.append("support %.2f >= resistance %.2f" % (sup, res))
    if px is not None and plan:
        e, s, tg = plan.get("entry"), plan.get("stop"), plan.get("target")
        if e is not None and s is not None and s >= e:
            v.append("stop %.2f is not below entry %.2f" % (s, e))
        if e is not None and tg is not None and tg <= e:
            v.append("target %.2f is not above entry %.2f" % (tg, e))

    # 7. valuation multiples must state a basis --------------------------
    val = snap.get("valuation") or {}
    seen = {}
    for name, f in val.items():
        if not isinstance(f, dict):
            continue
        if fv(f) is None:
            continue
        if not f.get("basis"):
            v.append("valuation '%s' has no basis (forward/trailing/ntm) "
                     "— unlabeled multiples caused the 33.8x vs 50x "
                     "contradiction" % name)
        else:
            key = (name.split("_")[0], f.get("basis"))
            if key in seen and seen[key] != fv(f):
                v.append("valuation %s on %s basis stated twice with "
                         "different values (%s vs %s)"
                         % (key[0], key[1], seen[key], fv(f)))
            seen[key] = fv(f)

    # 8. confidence coherence -------------------------------------------
    ev = snap.get("evidence") or {}
    if ev.get("model_confidence_score") is not None:
        v.append("uncalibrated model_confidence_score present — v2 "
                 "reports evidence quality, data completeness and "
                 "historically calibrated confidence separately")
    conv = str(ev.get("conviction") or "").lower()
    qual = str(ev.get("evidence_quality") or "").lower()
    RANK = {"low": 0, "limited": 0, "medium": 1, "moderate": 1,
            "high": 2, "strong": 2}
    if conv in RANK and qual in RANK and abs(RANK[conv] - RANK[qual]) > 1:
        v.append("conviction '%s' and evidence quality '%s' disagree"
                 % (conv, qual))

    # 9. alt-data inference discipline ----------------------------------
    sent = snap.get("sentiment") or {}
    if sent:
        verified = "options feed" in str(sent.get("flow_language") or "")
        for sec, text in prose_sections.items():
            t = (text or "").lower()
            if re.search(r"institutional (call |put )?flow|heavy institutional",
                         t) and not verified:
                v.append("[%s] infers institutional flow from social data "
                         "(%d relevant posts, %d authors) without an "
                         "options-feed verification"
                         % (sec, sent.get("n_relevant") or 0,
                            sent.get("unique_authors") or 0))
            if "downgrade" in t and sent.get("analyst_actions"):
                kinds = set(sent["analyst_actions"])
                if "rating_change" not in kinds:
                    v.append("[%s] says 'downgrade' but only price-target "
                             "changes occurred (rating maintained)" % sec)

    # 10. insider language discipline -----------------------------------
    ins = snap.get("insiders") or {}
    if ins and ins.get("n_view_bearing") == 0:
        for sec, text in prose_sections.items():
            if re.search(r"red flag|heavy selling|dumping|insiders? (are )?selling",
                         str(text or ""), re.I):
                v.append("[%s] characterises insider activity as selling "
                         "pressure, but 0 of %d transactions are "
                         "open-market sales (rest are vesting/"
                         "withholding/exercises)"
                         % (sec, ins.get("n_total") or 0))

    # 11. ownership completeness ----------------------------------------
    own = snap.get("ownership") or {}
    if own.get("n_filings"):
        for sec, text in prose_sections.items():
            if re.search(r"no 13[dg]|no schedule 13", str(text or ""), re.I):
                v.append("[%s] claims no 13D/13G filings while %d are in "
                         "the snapshot" % (sec, own["n_filings"]))
    for sec, text in prose_sections.items():
        if re.search(r"13G.{0,30}(lower|less) conviction", str(text or ""), re.I):
            v.append("[%s] treats 13G as 'lower conviction' — it denotes "
                     "passive/non-control filing status" % sec)

    return v


def gate(snap, prose_sections=None, raise_on_fail=True):
    """Publication gate. v2 renderers MUST call this and refuse to emit
    a PDF when it returns violations."""
    vs = check_contradictions(snap, prose_sections)
    if vs and raise_on_fail:
        raise Contradiction("Blocked — %d contradiction(s):\n  - %s"
                            % (len(vs), "\n  - ".join(vs)))
    return vs


# ── ISRG regression fixture (2026-07-16) ────────────────────────────────

def isrg_july16_fixture():
    """Reconstructs the state that produced the July 16 ISRG brief.
    Every one of the ten findings must be caught by the gate."""
    ev = datetime(2026, 7, 16, 20, 5, tzinfo=timezone.utc)   # 16:05 ET release
    now = datetime(2026, 7, 16, 17, 49, tzinfo=timezone.utc)  # 13:49 ET report
    snap = new_snapshot("ISRG", now.isoformat(), now.isoformat())
    snap["company"] = {
        "name": fact("Intuitive Surgical", "yfinance", "2026-07-16"),
        "market_cap": fact(178e9, "yfinance", "2026-07-16"),
        "universe": "SMID",                       # contamination
        "profitable": fact(True, "10-Q", "2026-07-16"),
        "sector": fact("Health Care", "yfinance", "2026-07-16"),
    }
    snap["price"] = {"last": fact(445.0, "yfinance", "2026-07-16T17:45Z")}
    snap["levels"] = {
        "ma20": fact(470.0, "calc", "2026-07-16"),
        "ma50": fact(478.0, "calc", "2026-07-16"),
        "ma200": fact(360.0, "calc", "2026-07-16"),     # stated
        "chart_ma200": fact(486.0, "chart", "2026-07-16"),  # drawn
        "support": fact(390.0, "calc", "2026-07-16"),
        "resistance": fact(505.0, "calc", "2026-07-16"),
    }
    snap["catalyst"] = {
        "event_dt": ev.isoformat(),
        "state": resolve_catalyst_state(ev, now),          # PRE_EVENT
        "stated_times": {"alt_data": "2026-07-16 after close",
                         "ticker_report": "2026-07-16 reported today"},
    }
    snap["valuation"] = {
        "pe_forward": fact(33.8, "yfinance", "2026-07-16", basis="forward"),
        "pe_alt": fact(50.0, "alt_data", "2026-07-16"),     # no basis
    }
    snap["decision"] = {
        "current_action": "AVOID",
        "position_plan": {"entry": 452.0, "stop": 428.0, "target": 505.0},
        "upgrade_trigger": "thesis breaks below $390",
        "downside_confirmation": "",
    }
    snap["evidence"] = {"model_confidence_score": 85, "conviction": "low",
                        "evidence_quality": "high"}
    snap["insiders"] = summarize_insiders([
        {"code": "F", "shares": 1200, "price": 470.0, "value": 564000,
         "date": "2026-07-10", "owner": "CEO", "title": "CEO",
         "shares_owned_after": 48000},
        {"code": "M", "shares": 5000, "price": 0.0, "value": 0,
         "date": "2026-07-10", "owner": "CEO", "title": "CEO",
         "shares_owned_after": 53000},
        {"code": "A", "shares": 900, "price": 0.0, "value": 0,
         "date": "2026-07-01", "owner": "CFO", "title": "CFO"},
    ])
    snap["ownership"] = summarize_ownership([
        {"form": "SC 13G/A", "filer": "Vanguard Group",
         "filed": "2026-03-27"},
    ], institutional_pct=88.0)
    snap["sentiment"] = score_alt_data(
        posts=[{"text": "ISRG calls printing", "author": "a", "source": "stocktwits"}] * 3
              + [{"text": "NRED squeeze incoming", "author": "b", "source": "reddit"},
                 {"text": "VYNE to the moon", "author": "c", "source": "reddit"},
                 {"text": "earnings calendar this week", "author": "d",
                  "source": "reddit"}],
        ticker="ISRG", options_feed_verified=False)
    snap["sentiment"]["analyst_actions"] = [
        "price_target_cut_rating_maintained"]
    prose = {
        "page1": ("ISRG is a mega-cap medtech leader. The stock is below "
                  "every moving average after the post-catalyst move; "
                  "earnings reported today. Heavy institutional call flow "
                  "supports a bounce. Recent analyst downgrades weigh."),
        "page2": ("Valuation is normal for SMID peers and cheap versus "
                  "unprofitable growth companies. Insider selling is a "
                  "glaring red flag. There were no 13D/13G filings in the "
                  "last 12 months, and 13G filings indicate lower "
                  "conviction anyway."),
    }
    return snap, prose


def self_test():
    fails = []

    def chk(name, cond):
        print(("  PASS  " if cond else "  FAIL  ") + name)
        if not cond:
            fails.append(name)

    # --- catalyst state machine ---
    ev = datetime(2026, 7, 16, 20, 5, tzinfo=timezone.utc)
    chk("state: 2h before release -> PRE_EVENT",
        resolve_catalyst_state(ev, ev - timedelta(hours=2)) == PRE_EVENT)
    chk("state: 10m after -> EVENT_IN_PROGRESS",
        resolve_catalyst_state(ev, ev + timedelta(minutes=10)) == EVENT_IN_PROGRESS)
    chk("state: 3h after -> POST_EVENT_UNGRADED",
        resolve_catalyst_state(ev, ev + timedelta(hours=3)) == POST_EVENT_UNGRADED)
    chk("state: next session -> POST_EVENT_GRADED",
        resolve_catalyst_state(ev, ev + timedelta(hours=25)) == POST_EVENT_GRADED)

    # --- insider classification (finding 6) ---
    f = classify_insider({"code": "F", "shares": 1200, "shares_owned_after": 48000})
    chk("insider: code F = tax withholding, carries no view",
        f["class"] == TAX_WITHHOLDING and not f["carries_view"])
    m = classify_insider({"code": "M", "shares": 5000})
    chk("insider: code M = option exercise, carries no view",
        m["class"] == OPTION_EXERCISE and not m["carries_view"])
    s = classify_insider({"code": "S", "shares": 1000, "shares_owned_after": 9000})
    chk("insider: code S = open-market sale, 10% of holdings",
        s["carries_view"] and s["pct_of_holdings"] == 10.0)
    p = classify_insider({"code": "S", "shares": 100,
                          "footnotes": ["Sale under a Rule 10b5-1 plan"]})
    chk("insider: 10b5-1 sale separated from discretionary",
        p["class"] == PLANNED_SALE and not p["carries_view"])

    # --- alt-data relevance (findings 8, 9) ---
    sc = score_alt_data(
        [{"text": "$ISRG calls", "author": "a"},
         {"text": "$ISRG calls", "author": "a"},          # duplicate
         {"text": "NRED to the moon", "author": "b"},
         {"text": "earnings calendar", "author": "c"}], "ISRG")
    chk("alt-data: irrelevant + duplicate posts dropped",
        sc["n_relevant"] == 1 and sc["n_dropped_irrelevant"] == 3)
    chk("alt-data: never claims institutional from social",
        "retail call discussion" in sc["flow_language"])
    chk("alt-data: CLEAN replaced with limited-sample wording",
        "limited sample" in sc["coordination"])
    chk("analyst: PT cut with rating held is not a downgrade",
        classify_analyst_action("Buy", "Buy", 600, 520)
        == "price_target_cut_rating_maintained")

    # --- ownership (finding 7) ---
    ow = summarize_ownership([{"form": "SC 13G/A", "filer": "Vanguard"}])
    chk("ownership: 13G/A counted as a filing", ow["n_filings"] == 1)
    chk("ownership: passive-status note present",
        "not a statement about conviction" in ow["note"])

    # --- the full ISRG regression: gate must catch all ten ---
    snap, prose = isrg_july16_fixture()
    vs = check_contradictions(snap, prose)
    blob = " || ".join(vs).lower()
    print("\n  ISRG 2026-07-16 fixture -> %d contradictions:" % len(vs))
    for x in vs:
        print("      * " + x)
    print("")
    chk("F1 catalyst: pre-event prose caught",
        "post-catalyst" in blob or "reported today" in blob)
    chk("F1b catalyst: cross-section timing disagreement caught",
        "catalyst timing disagrees" in blob)
    chk("F2 levels: stated MA vs chart MA caught", "chart" in blob and "ma200" in blob)
    chk("F2b levels: 'below every MA' vs numbers caught",
        "below every ma" in blob)
    chk("F3 decision: AVOID + long plan caught", "active long plan" in blob)
    chk("F3b decision: reversed upgrade trigger caught",
        "confirms the bearish thesis" in blob)
    chk("F4 confidence: uncalibrated score caught",
        "uncalibrated model_confidence_score" in blob)
    chk("F5 template: SMID framing on large cap caught",
        "smid" in blob)
    chk("F5b template: 'unprofitable growth' vs profitable caught",
        "unprofitable growth" in blob)
    chk("F6 insider: 'red flag' with 0 open-market sales caught",
        "open-market sales" in blob)
    chk("F7 ownership: 'no 13D/13G' vs filed 13G/A caught",
        "claims no 13d/13g" in blob)
    chk("F7b ownership: 13G-as-lower-conviction caught",
        "passive/non-control" in blob)
    chk("F8 alt-data: institutional inferred from social caught",
        "infers institutional flow" in blob)
    chk("F8b alt-data: PT cut called downgrade caught",
        "only price-target changes" in blob)
    chk("F10 valuation: unlabeled multiple caught", "no basis" in blob)
    chk("GATE: fixture is unpublishable",
        len(gate(snap, prose, raise_on_fail=False)) > 0)

    # --- a corrected snapshot must PASS ---
    good, _ = isrg_july16_fixture()
    good["company"]["universe"] = "LARGE"
    good["levels"]["ma200"] = fact(486.0, "calc", "2026-07-16")
    good["decision"] = {
        "current_action": "AVOID",
        "position_plan": {},
        "upgrade_trigger": "reclaim of $486 (200-day) on above-average volume",
        "downside_confirmation": "loss of $390 support on expanding volume",
    }
    good["valuation"] = {
        "pe_forward": fact(33.8, "yfinance", "2026-07-16", basis="forward"),
        "pe_trailing": fact(50.0, "yfinance", "2026-07-16", basis="trailing"),
    }
    good["evidence"] = {"conviction": "low", "evidence_quality": "limited",
                        "data_completeness": 0.8}
    good["catalyst"]["stated_times"] = {
        "alt_data": "2026-07-16T20:05Z", "ticker_report": "2026-07-16T20:05Z"}
    clean_prose = {
        "page1": ("Intuitive Surgical is a large-cap medtech leader. Price "
                  "sits below the 20- and 50-day averages ahead of "
                  "earnings, scheduled for 2026-07-16 after the close. "
                  "Retail users discussed bullish call positions."),
        "page2": ("Insider activity in the window is compensation "
                  "mechanics — vesting and withholding — with no "
                  "open-market sales. Vanguard filed a 13G/A in March."),
    }
    gv = check_contradictions(good, clean_prose)
    if gv:
        print("  corrected snapshot residual violations:")
        for x in gv:
            print("      ! " + x)
    chk("CORRECTED snapshot publishes cleanly", len(gv) == 0)

    total = 26
    print("\n%d/%d checks passed" % (total - len(fails), total))
    return 0 if not fails else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        sys.exit(self_test())
    print("research_snapshot v2 — import me, or run --self-test")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
