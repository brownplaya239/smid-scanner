#!/usr/bin/env python3
"""brief_compose.py — what goes in the brief, in what order, and why.

Separated from rendering on purpose. The email's hard problems are
editorial, not visual: which of a user's names actually changed, whether
a ticker's flow agrees with itself, whether a historical hit rate is an
edge or a coin flip, and whether a headline is really about the ticker it
was filed under. Those are decided here and unit-tested without building
a single table.

    python brief_compose.py --self-test
"""

import re
import sys
from datetime import timedelta as _timedelta


def _now_et():
    from datetime import datetime
    import brief_time as BT
    return datetime.now(BT.ET)

# ── watchlist ranking ───────────────────────────────────────────────────
# "ACT NOW" overstated certainty and sat badly beside an educational-
# research disclaimer. These states describe EVIDENCE, not instructions.
TRIGGER_REACHED = "TRIGGER REACHED"
STRENGTHENING = "MATERIAL STRENGTHENING"
WEAKENING = "MATERIAL WEAKENING"
# When the evidence points both ways, saying only the louder half is a
# distortion. A grade upgrade sitting next to short-dated bearish flow the
# day before earnings is not "strengthening" -- it is a disagreement, and
# the disagreement is the information.
MIXED_SETUP = "MIXED SETUP"
# Bearish flow whose confirmation test has not run is a reason to look, not
# a finding that the name deteriorated. Calling it MATERIAL WEAKENING
# borrows certainty from a test nobody has performed yet.
BEARISH_FLOW_ALERT = "BEARISH FLOW ALERT · OI PENDING"
BULLISH_FLOW_ALERT = "BULLISH FLOW ALERT · OI PENDING"
REVIEW = "REVIEW NOW"
MONITOR = "MONITOR"
# Not everything that moved is material. A second tier keeps the count
# honest instead of inflating "13 of 16 changed materially".
NOTABLE = "NOTABLE"
NO_CHANGE = "NO MATERIAL CHANGE"
ACT_NOW, WATCH, NO_ACTION, QUIET = (REVIEW, MONITOR, MONITOR, NO_CHANGE)
RANK_ORDER = {TRIGGER_REACHED: 0, REVIEW: 1, MIXED_SETUP: 2, WEAKENING: 3,
              STRENGTHENING: 4, BEARISH_FLOW_ALERT: 5, BULLISH_FLOW_ALERT: 6,
              MONITOR: 7, NOTABLE: 8, NO_CHANGE: 9}
MATERIAL_STATES = (TRIGGER_REACHED, REVIEW, MIXED_SETUP, WEAKENING,
                   STRENGTHENING, BEARISH_FLOW_ALERT, BULLISH_FLOW_ALERT)
# REVIEW NOW requires the full decision context; without it we say MONITOR
REVIEW_FIELDS = ("trigger", "invalidation", "evidence")
MAX_CHANGED_SHOWN = 5

# ── materiality, frozen ─────────────────────────────────────────────────
# Each code is one rule. A name is MATERIAL only if at least one fires;
# anything else that merely moved is NOTABLE. The codes travel with the
# record so the displayed explanation can be checked against the rule that
# admitted it.
RULES = {
    "GRADE_TRANSITION": "setup grade moved at least one notch",
    "TRIGGER_CROSS": "price crossed a stated trigger or invalidation",
    "PRICE_MOVE": "single-session move of 4% or more",
    "FLOW_HQ": "new options flow at institutional size or top tier",
    "FLOW_PRESENT": "new options flow below the high-quality threshold",
    "EARNINGS_CONFIRMED": "confirmed earnings date within two sessions",
    "TECH_TRANSITION": "moving-average state changed",
    "NEWS_MATERIAL": "verified material news on the name",
}
# Which fields each code is entitled to read. The test that walks this map
# is what stops a code from being asserted on evidence it never saw --
# FLOW_HQ was appearing on names whose displayed flow quality was C.
RULE_FIELDS = {
    "GRADE_TRANSITION": ("grade_delta", "grade_from", "grade_to"),
    "TRIGGER_CROSS": ("trigger_hit", "crossed_level"),
    "PRICE_MOVE": ("price_change_pct",),
    "FLOW_HQ": ("flow_hq",),
    "FLOW_PRESENT": ("has_flow",),
    "EARNINGS_CONFIRMED": ("earnings_in_days", "earnings_confirmed"),
    "TECH_TRANSITION": ("tech_transition",),
    "NEWS_MATERIAL": ("news_material",),
}
# Only these codes make a name MATERIAL. FLOW_PRESENT is real evidence and
# is reported, but a stray print is not a material change.
MATERIAL_CODES = ("GRADE_TRANSITION", "TRIGGER_CROSS", "PRICE_MOVE",
                  "FLOW_HQ", "EARNINGS_CONFIRMED", "TECH_TRANSITION",
                  "NEWS_MATERIAL")
# below these, flow is noise on a watch list
FLOW_HQ_PREMIUM = 1_000_000
FLOW_HQ_TIERS = ("A+", "A", "A-")


def flow_quality_is_hq(quality):
    """The displayed flow-quality tier and the FLOW_HQ code must agree.
    A row reading 'Flow quality C' beside 'Rules: FLOW_HQ' asks the reader
    to believe both that the flow was top-tier and that it was not."""
    return str(quality or "").strip() in FLOW_HQ_TIERS


def materiality(ch):
    """Which frozen rules this name satisfies, in priority order."""
    codes = []
    gd = ch.get("grade_delta") or 0
    if gd and ch.get("grade_from") and ch.get("grade_to"):
        codes.append("GRADE_TRANSITION")
    if ch.get("trigger_hit") or ch.get("crossed_level"):
        codes.append("TRIGGER_CROSS")
    pc = ch.get("price_change_pct")
    if pc is not None and abs(pc) >= 4:
        codes.append("PRICE_MOVE")
    # FLOW_HQ requires the configured threshold to have actually passed AND
    # the displayed quality tier to agree with it. Flow that exists but is
    # not high quality is still evidence; it just gets its own code.
    if ch.get("flow_hq") and (ch.get("signal_strength") is None
                              or flow_quality_is_hq(ch.get("signal_strength"))):
        codes.append("FLOW_HQ")
    elif ch.get("has_flow"):
        codes.append("FLOW_PRESENT")
    e = ch.get("earnings_in_days")
    if e is not None and e <= 2 and ch.get("earnings_confirmed"):
        codes.append("EARNINGS_CONFIRMED")
    if ch.get("tech_transition"):
        codes.append("TECH_TRANSITION")
    if ch.get("news_material"):
        codes.append("NEWS_MATERIAL")
    return codes


def synthesize(ch):
    """Separate the evidence into its dimensions and say whether they agree.

    Five axes, kept apart because they answer different questions and can
    legitimately disagree:

      structural  did the technical/fundamental read improve or degrade
      flow        which way the options tape leans, if it leans
      event       is there a scheduled catalyst inside the horizon
      evidence    how much of this rests on measured data
      edge        does the historical record support the setup at all

    Returns the dimensions plus `conflict`, which is True when structural
    and flow point opposite ways. That is what turns MATERIAL
    STRENGTHENING into MIXED SETUP.
    """
    gd = ch.get("grade_delta") or 0
    pc = ch.get("price_change_pct")
    structural = 0
    if gd:
        structural = 1 if gd > 0 else -1
    elif pc is not None and abs(pc) >= 4:
        structural = 1 if pc > 0 else -1

    flow = 0
    fd = (ch.get("flow_direction") or "").upper()
    if fd == BULLISH:
        flow = 1
    elif fd == BEARISH:
        flow = -1

    days = ch.get("earnings_in_days")
    event = ("earnings in %dd" % days) if days is not None and days <= 5 \
        else None
    # short-dated positioning into a scheduled event is the case worth
    # calling out; flow that expires after the print is a different animal
    short_dated = bool(ch.get("flow_short_dated"))

    conflict = bool(structural and flow and structural != flow)
    return {
        "structural": structural,
        "flow": flow,
        "event": event,
        "event_days": days,
        "short_dated": short_dated,
        "evidence": ch.get("evidence"),
        "edge": ch.get("edge"),
        "conflict": conflict,
    }


def synthesis_line(ch, syn=None):
    """One sentence naming both halves of a disagreement, in that order:
    what improved, what argues against it, and when the question resolves."""
    syn = syn or synthesize(ch)
    if not syn["conflict"]:
        return ""
    gf, gt = ch.get("grade_from"), ch.get("grade_to")
    if syn["structural"] > 0:
        first = ("grade improved %s → %s" % (gf, gt)) if gf and gt \
            else "structural read improved"
        second = "%s%s flow" % ("short-dated " if syn["short_dated"] else "",
                                "bearish")
    else:
        first = ("grade fell %s → %s" % (gf, gt)) if gf and gt \
            else "structural read weakened"
        second = "%s%s flow" % ("short-dated " if syn["short_dated"] else "",
                                "bullish")
    tail = (" ahead of %s" % syn["event"]) if syn["event"] else ""
    return "%s; %s%s" % (first, second, tail)


# A reading that rounds to zero is not "below". Without a stated tolerance
# the summary counted an index sitting exactly on its average as beneath
# it, while the table printed 0.0% beside it.
AT_TOLERANCE_PCT = 0.05
ABOVE, AT, BELOW = "above", "at", "below"


def ma_state(dist_pct, tol=AT_TOLERANCE_PCT):
    if dist_pct is None:
        return None
    if abs(dist_pct) <= tol:
        return AT
    return ABOVE if dist_pct > 0 else BELOW


def ma_state_summary(dists, tol=AT_TOLERANCE_PCT):
    """'1 above · 1 at · 2 below their 20-day averages' — the same
    tolerance the table uses, so the two cannot disagree."""
    states = [ma_state(d, tol) for d in dists if d is not None]
    if not states:
        return ""
    n = {s: states.count(s) for s in (ABOVE, AT, BELOW)}
    parts = ["%d %s" % (n[s], s) for s in (ABOVE, AT, BELOW) if n[s]]
    return " · ".join(parts) + " their 20-day averages"


# Where a displayed price came from. "All prices as of 07:20" is false the
# moment one of them is a prior close and another is a pre-market print.
BASIS_PREMARKET = "pre-market"
BASIS_CLOSE = "prior close"
BASIS_DETECT = "spot at flow detection"


def price_record(value, basis, as_of="", reason="", source="",
                 stale_after=""):
    """A price always renders as something, and always says when it was
    true. An empty cell reads as a rendering bug; 'Price unavailable'
    reads as the truth; and a bare number with no clock is the worst of
    the three, because it looks authoritative.
    """
    if value is None:
        return {"value": None, "text": "Price unavailable · %s"
                % (reason or "no source"), "basis": None,
                "session_basis": None, "as_of": "", "source": source,
                "stale_after": "", "unavailable_reason": reason or "no source"}
    # State the basis always; state the clock only when we have one. The
    # first cut printed "· pre-market " with a dangling space where the
    # timestamp should have been, which reads as a truncated field.
    as_of = (as_of or "").strip()
    # the record keeps the full stamp; the LABEL drops the date, which the
    # brief's own as-of already states two lines above
    short = re.sub(r"^\d{4}-\d{2}-\d{2}\s+", "", as_of)
    if basis == BASIS_PREMARKET:
        label = ("pre-market %s" % short) if short else "pre-market quote"
    elif basis == BASIS_CLOSE:
        label = ("%s close" % short) if short else "prior close"
    elif basis == BASIS_DETECT:
        label = ("spot at detection %s" % as_of) if as_of \
            else "spot at flow detection"
    else:
        label = short
    # the schema's session_basis enum; `basis` stays for the display label
    session_basis = {BASIS_PREMARKET: "pre_market", BASIS_CLOSE: "prior_close",
                     BASIS_DETECT: "flow_spot", "live": "live"}.get(basis)
    return {"value": round(float(value), 2), "basis": basis,
            "session_basis": session_basis, "as_of": as_of,
            "source": source or "unknown", "stale_after": stale_after,
            "text": ("$%.2f" % value) + (" · %s" % label if label else "")}


def _ma_levels(fact):
    """Absolute prices for the moving averages, back out of close and the
    stored percentage distance. Exact arithmetic on published numbers --
    nothing here is estimated."""
    close = (fact or {}).get("close")
    if not close:
        return {}
    out = {}
    for span in ("ema20", "ema50", "ema200"):
        dist = fact.get(span + "_dist")
        if dist is None:
            continue
        side = fact.get(span)
        signed = abs(dist) if side == "above" else -abs(dist)
        lvl = close / (1.0 + signed / 100.0)
        if lvl > 0:
            out[span] = lvl
    return out


def confirmation_invalidation(fact):
    """(next confirmation, invalidation) as concrete price conditions, or
    (None, None) when the inputs to state one honestly are absent.

    Confirmation is the nearest average the name has still to reclaim;
    when it is already above all of them, the condition is holding the
    fastest one. Invalidation is the nearest average below, falling back
    to one ATR when price sits above nothing.
    """
    lv = _ma_levels(fact)
    close = (fact or {}).get("close")
    if not lv or not close:
        return None, None
    label = {"ema20": "20-day", "ema50": "50-day", "ema200": "200-day"}
    above = sorted(((p, s) for s, p in lv.items() if p > close))
    below = sorted(((p, s) for s, p in lv.items() if p <= close), reverse=True)

    if below:
        p, s = below[0]
        inval = "a daily close below the %s at $%.2f" % (label[s], p)
        inval_at = p
    else:
        atr = (fact or {}).get("atr_pct")
        inval = ("a close below $%.2f (one ATR under today)"
                 % (close * (1 - atr / 100.0))) if atr else None
        inval_at = None

    if above:
        p, s = above[0]
        conf = "a daily close above the %s at $%.2f" % (label[s], p)
    elif inval_at is not None:
        # price is above every average, so there is no level left to
        # reclaim. Offering "hold the 20-day" here named the SAME price as
        # the invalidation, which reads as a contradiction and tells the
        # reader nothing they did not already have.
        conf = None
    else:
        p = lv.get("ema20")
        conf = ("holding the 20-day at $%.2f on a closing basis" % p) \
            if p else None
    return conf, inval


def rank_ticker(ch):
    """Bucket one watchlist name from what actually changed.

    `ch` is a change record: grade_delta, crossed_level, has_flow,
    catalyst, earnings_in_days, price_change_pct, trigger_hit.
    """
    reasons = []
    if ch.get("trigger_hit"):
        reasons.append("entry trigger reached")
    if ch.get("crossed_level"):
        reasons.append("crossed %s" % ch["crossed_level"])
    if ch.get("grade_delta"):
        # an interpretable transition, not an opaque delta
        gf, gt = ch.get("grade_from"), ch.get("grade_to")
        if gf and gt:
            reasons.append("grade %s → %s" % (gf, gt))
        else:
            reasons.append("conviction %s %d level%s"
                           % ("up" if ch["grade_delta"] > 0 else "down",
                              abs(ch["grade_delta"]),
                              "" if abs(ch["grade_delta"]) == 1 else "s"))
    if ch.get("has_flow"):
        reasons.append("new options flow")
    if ch.get("catalyst"):
        reasons.append(ch["catalyst"])
    e = ch.get("earnings_in_days")
    if e is not None and e <= 2:
        reasons.append("earnings in %dd" % e)
    pc = ch.get("price_change_pct")
    if pc is not None and abs(pc) >= 4:
        reasons.append("%+.1f%% move" % pc)

    codes = materiality(ch)
    material = any(c in MATERIAL_CODES for c in codes)
    soft = bool(ch.get("has_flow") or ch.get("catalyst")
                or (ch.get("grade_delta") or 0)
                or (pc is not None and abs(pc) >= 1.5))

    syn = synthesize(ch)
    gd = ch.get("grade_delta") or 0
    pcv = pc or 0
    # "Independent" means evidence that is not the pending flow reading:
    # a grade cut, a real price move, a technical break, or flow whose
    # open-interest test has actually completed against it.
    indep_down = bool(gd < 0 or pcv <= -4 or ch.get("tech_deterioration")
                      or (syn["flow"] < 0 and ch.get("flow_confirmed")))
    indep_up = bool(gd > 0 or pcv >= 4 or ch.get("tech_improvement")
                    or (syn["flow"] > 0 and ch.get("flow_confirmed")))
    support = []
    if gd:
        support.append("grade %s → %s" % (ch.get("grade_from"),
                                          ch.get("grade_to")))
    if pc is not None and abs(pc) >= 4:
        support.append("%+.1f%% session move" % pc)
    if ch.get("flow_confirmed"):
        support.append("open-interest confirmed flow")
    if ch.get("tech_deterioration") or ch.get("tech_improvement"):
        support.append("moving-average state change")

    if ch.get("trigger_hit"):
        bucket = TRIGGER_REACHED
    elif material and all(ch.get(f) for f in REVIEW_FIELDS):
        # REVIEW NOW only when the reader is given something to act on:
        # what changed, the next condition, and what invalidates it
        bucket = REVIEW
    elif syn["conflict"]:
        # the two halves disagree — report the disagreement, do not let
        # whichever half is louder speak for the whole name
        bucket = MIXED_SETUP
    elif indep_down:
        bucket = WEAKENING
    elif indep_up:
        bucket = STRENGTHENING
    elif syn["flow"] and not ch.get("flow_confirmed"):
        # flow is the ONLY directional evidence and its test has not run
        bucket = (BEARISH_FLOW_ALERT if syn["flow"] < 0
                  else BULLISH_FLOW_ALERT)
    elif material:
        bucket = MONITOR
    elif soft:
        bucket = NOTABLE
    else:
        bucket = NO_CHANGE
    if bucket in (MONITOR, MIXED_SETUP, NOTABLE) and not reasons:
        bucket = NO_CHANGE
    # a status that outranks MONITOR must rest on a rule that fired
    if bucket in MATERIAL_STATES and not material:
        bucket = NOTABLE if reasons else NO_CHANGE
    line = synthesis_line(ch, syn)
    if line:
        # the synthesis replaces the reasons it summarises, rather than
        # repeating "grade B → A-" twice on the same row
        low = line.lower()
        reasons = [line] + [r for r in reasons
                            if not r.lower().startswith("grade ")
                            and r != "new options flow"
                            and r.lower() not in low]
    # every status states the independent evidence that earned it, so a
    # reader can check the label against the facts rather than trust it
    if bucket in (WEAKENING, STRENGTHENING) and support:
        basis = "on " + " and ".join(support[:2])
    elif bucket in (BEARISH_FLOW_ALERT, BULLISH_FLOW_ALERT):
        basis = "flow only; no independent grade, price or technical change"
    elif bucket == MIXED_SETUP:
        basis = "evidence points both ways"
    else:
        basis = ""
    return {"bucket": bucket, "reasons": _dedupe(reasons), "synthesis": syn,
            "synthesis_line": line, "reason_codes": codes,
            "status_basis": basis, "material": material,
            "changed": bucket != NO_CHANGE}


def _dedupe(reasons):
    """Drop repeats and clauses already contained in another.

    The scanner's catalyst prose and the generated horizon are the same
    fact in two registers -- "Earnings in 1d" beside "earnings in 1d" is
    the row telling the reader the same thing twice.
    """
    out = []
    for r in reasons:
        k = re.sub(r"[^a-z0-9]+", " ", str(r).lower()).strip()
        if not k:
            continue
        if any(k == o or k in o or o in k for o in out):
            continue
        out.append(k)
    # rebuild in original casing, keeping first occurrence of each key
    seen, final = set(), []
    for r in reasons:
        k = re.sub(r"[^a-z0-9]+", " ", str(r).lower()).strip()
        if k in out and k not in seen:
            seen.add(k)
            final.append(r)
    return final


def rank_watchlist(changes, facts=None, detail_top=3):
    """Rank, cap the displayed set, and account for every name.

    The arithmetic the reader can do in their head must hold:
    changed + unchanged = eligible. When the display cap hides some of the
    changed names, that is stated as a number and a link, never by
    quietly dropping them.
    """
    ranked = []
    for ch in changes:
        r = rank_ticker(ch)
        row = {**ch, **r}
        if not row.get("reasons"):
            # a row with nothing to say is not a row; it is whitespace with
            # a ticker on it
            row["bucket"] = NO_CHANGE
            row["changed"] = False
        ranked.append(row)
    ranked.sort(key=lambda x: (RANK_ORDER[x["bucket"]],
                               -abs(x.get("price_change_pct") or 0)))
    # MATERIAL and NOTABLE are different claims. Pooling them is how "13 of
    # 16 changed materially" happened when most of those names had only a
    # pending flow print against them.
    changed = [x for x in ranked if x["bucket"] in MATERIAL_STATES]
    notable = [x for x in ranked if x["bucket"] in (MONITOR, NOTABLE)]
    quiet = [x for x in ranked if x["bucket"] == NO_CHANGE]
    shown = changed[:MAX_CHANGED_SHOWN]
    hidden = changed[MAX_CHANGED_SHOWN:]

    # decision context only for the names at the top, and only where the
    # underlying levels actually exist
    for i, x in enumerate(shown[:detail_top]):
        f = (facts or {}).get(x.get("ticker")) or {}
        conf, inval = confirmation_invalidation(f)
        if conf:
            x["next_confirmation"] = conf
        if inval:
            x["invalidation"] = inval

    over = ""
    if hidden:
        over = ("Showing the top %d of %d material changes · %d more on "
                "your desk." % (len(shown), len(changed), len(hidden)))
    notable_line = ""
    if notable:
        notable_line = ("Notable but not material: %s"
                        % ", ".join(x["ticker"] for x in notable[:12])
                        + ("…" if len(notable) > 12 else ""))
    return {
        "shown": shown,
        "overflow": hidden,
        "notable": notable,
        "quiet": quiet,
        "n_total": len(ranked),
        "n_changed": len(changed),
        "n_notable": len(notable),
        "n_quiet": len(quiet),
        "n_shown": len(shown),
        "n_hidden": len(hidden),
        "overflow_line": over,
        "notable_line": notable_line,
        "alert_line": ("%d of your %d watch list names changed materially%s"
                       % (len(changed), len(ranked),
                          (": " + "; ".join(
                              "%s %s" % (x["ticker"],
                                         (x.get("reasons") or ["changed"])[0])
                              for x in shown[:3]) + ".") if shown else ".")),
        "quiet_line": ("No material change: %s"
                       % (", ".join(x["ticker"] for x in quiet[:12])
                          + ("…" if len(quiet) > 12 else ""))) if quiet else "",
    }


# ── options-flow classification ─────────────────────────────────────────
# Direction and confirmation are SEPARATE dimensions. Collapsing them was
# a real error: two call buys where only one earned next-session OI
# confirmation is partially confirmed BULLISH, not MIXED. MIXED means the
# contracts point opposite ways.
BULLISH, BEARISH, MIXED_DIR, NO_DIR = "BULLISH", "BEARISH", "MIXED", "NONE"
# NOTE: prefixed because the news section below binds its own
# UNCONFIRMED ("unconfirmed") at module level and silently clobbered
# this one, rendering "unconfirmed BEARISH" in mixed case.
CONF_YES, CONF_PARTIAL, CONF_NO, CONF_CONFLICT = (
    "CONFIRMED", "PARTIALLY CONFIRMED", "UNCONFIRMED",
    "CONFLICTING CONFIRMATION")
# Absence of a confirmation is not a failed confirmation. Open interest for
# a session is published by OCC the FOLLOWING morning, so a contract printed
# today cannot be "UNCONFIRMED" today -- the test has not run yet. Saying
# UNCONFIRMED there reads as "we looked and it did not confirm", which is a
# claim about evidence that does not exist.
CONF_PENDING = "OI PENDING"
MIXED = MIXED_DIR          # kept: MIXED still means opposing directions

_BULL_SIDES = ("call_buy", "call_buyer", "put_sell", "put_seller", "bullish")
_BEAR_SIDES = ("put_buy", "put_buyer", "call_sell", "call_seller", "bearish")
# The scanner's own verdict when buyer/seller could not be resolved from the
# tape. It is the single most common value in the feed, so folding it into
# "no side given" let one directional print out of three decide a ticker's
# whole verdict. It is evidence of ambiguity, not absence of evidence.
_AMBIG_SIDES = ("mixed", "both", "two-sided", "two_sided")

# (singular, plural) — read aloud in a sentence, not printed in a cell
_SIDE_PHRASE = {"call_buy": ("call purchase", "call purchases"),
                "call_buyer": ("call purchase", "call purchases"),
                "put_buy": ("put purchase", "put purchases"),
                "put_buyer": ("put purchase", "put purchases"),
                "call_sell": ("call sale", "call sales"),
                "call_seller": ("call sale", "call sales"),
                "put_sell": ("put sale", "put sales"),
                "put_seller": ("put sale", "put sales"),
                "bullish": ("bullish print", "bullish prints"),
                "bearish": ("bearish print", "bearish prints")}


_WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven",
          "eight", "nine", "ten")


def _count(n, noun):
    """'two prints' / 'a single print' — numerals under ten read as
    digits in a table and as prose in a sentence; this is prose."""
    word = _WORDS[n] if 0 <= n < len(_WORDS) else str(n)
    return "%s %s%s" % (word, noun, "" if n == 1 else "s")


def _side(c):
    return (c.get("side") or c.get("flow_side") or c.get("direction")
            or "").strip().lower()


def contract_direction(c):
    s = _side(c)
    if s in _BULL_SIDES:
        return BULLISH
    if s in _BEAR_SIDES:
        return BEARISH
    return NO_DIR


def oi_state(contract, now_et=None):
    """The confirmation state of one contract, and why it is in it.

    Three states, one rule each:

      CONFIRMED   the next-session open-interest test ran and open
                  interest rose by the required amount
      UNCONFIRMED the test ran and it did not
      OI PENDING  the test has not run: the session's open interest is
                  not published yet, or the confirmation window is still
                  open

    Returns the state plus the four timestamps that justify it, so a
    reader can see WHY a contract is in the state it is in rather than
    having to trust the label.
    """
    import brief_time as BT
    now = now_et or _now_et()
    flow_dt = BT.parse_iso(contract.get("printed_at")
                           or contract.get("last_print_ts") or "")
    oi_dt = BT.parse_iso(contract.get("oi_as_of") or "")
    # OCC publishes prior-session open interest in the morning; the test is
    # eligible from the next session's open
    eligible = None
    if flow_dt is not None and flow_dt.tzinfo is not None:
        et = BT.to_et(flow_dt)
        eligible = (et + _timedelta(days=1)).replace(
            hour=9, minute=30, second=0, microsecond=0)
        while eligible.weekday() >= 5:
            eligible = eligible + _timedelta(days=1)

    delta = contract.get("oi_delta")
    info = {
        "flow_at": BT.fmt_stamp(flow_dt) if flow_dt else None,
        "eligible_at": BT.fmt_stamp(eligible) if eligible else None,
        "oi_as_of": BT.fmt_stamp(oi_dt) if oi_dt else None,
        "oi_delta": delta,
    }
    if delta is None:
        if eligible is not None and now < eligible:
            info.update(state=CONF_PENDING,
                        reason="next-session open interest is not published "
                               "until %s" % BT.fmt_stamp(eligible))
        else:
            info.update(state=CONF_PENDING,
                        reason="no open-interest reading has been recorded "
                               "for this contract yet")
        return info
    if oi_dt is not None and eligible is not None and oi_dt < eligible:
        info.update(state=CONF_PENDING,
                    reason="the open-interest reading predates the "
                           "confirmation window")
        return info
    info.update(state=CONF_YES if delta > 0 else CONF_NO,
                reason=("open interest rose %+d after the print" % delta)
                if delta > 0 else
                ("open interest did not rise (%+d) after the print" % delta))
    return info


def _side_breakdown(contracts, want):
    """'a put sale' / 'two put purchases and one call sale' — the sides
    that actually voted, so the sentence can never name a side no
    contract carried. Largest group first: that is the story."""
    counts = {}
    for c in contracts:
        if contract_direction(c) != want:
            continue
        p = _SIDE_PHRASE.get(_side(c))
        if p:
            counts[p] = counts.get(p, 0) + 1
    parts = ["%s %s" % (("one" if n == 1 else
                         _WORDS[n] if n < len(_WORDS) else str(n)),
                        p[0] if n == 1 else p[1])
             for p, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def classify_flow(contracts):
    """Return direction, confirmation, and a sentence that explains both.

    Unconfirmed open interest does NOT mean bearish — it means the
    directional reading lacks that confirmation layer.
    """
    contracts = list(contracts or [])
    if not contracts:
        return {"direction": NO_DIR, "confirmation": CONF_NO,
                "label": "NO FLOW", "n": 0, "explain": "no contracts"}
    dirs = [contract_direction(c) for c in contracts]
    bull = sum(1 for d in dirs if d == BULLISH)
    bear = sum(1 for d in dirs if d == BEARISH)
    voted = bull + bear
    amb = sum(1 for c in contracts if _side(c) in _AMBIG_SIDES)
    nodata = len(contracts) - voted - amb
    if bull and bear:
        direction = MIXED_DIR
    elif voted * 2 < len(contracts):
        # fewer than half the prints carry a resolvable side. One directional
        # contract among three does not make a ticker directional, and saying
        # so out loud is the whole point of this section.
        direction = NO_DIR
    elif bull:
        direction = BULLISH
    elif bear:
        direction = BEARISH
    else:
        direction = NO_DIR

    # `status` is the adapter's shorthand; `oi_state` is the state machine.
    # Prefer whichever the contract carries, but never read a missing
    # reading as a failed one.
    states = []
    for c in contracts:
        st = c.get("oi_state")
        if not st:
            s = c.get("status")
            st = ({"confirmed": CONF_YES, "failed": CONF_NO}.get(s)
                  or CONF_PENDING)
        states.append(st)
    conf = states.count(CONF_YES)
    fail = states.count(CONF_NO)
    pend = states.count(CONF_PENDING)
    unk = pend
    tested = conf + fail
    if not tested:
        # nothing has been through the test yet
        confirmation = CONF_PENDING
    elif conf and fail and direction != MIXED_DIR:
        # the confirmation evidence itself disagrees
        confirmation = CONF_CONFLICT
    elif conf == len(contracts):
        confirmation = CONF_YES
    elif conf:
        confirmation = CONF_PARTIAL
    else:
        confirmation = CONF_NO

    if direction == MIXED_DIR:
        label = MIXED_DIR
    elif direction == NO_DIR:
        label = "DIRECTION UNCLEAR"
    else:
        label = "%s %s" % (confirmation, direction)

    n = len(contracts)
    amb_tail = ""
    if amb or nodata:
        bits = []
        if amb:
            bits.append("%s the tape could not resolve to a buyer or seller"
                        % (_count(amb, "print")))
        if nodata:
            bits.append("%s arrived without a side" % _count(nodata, "print"))
        amb_tail = ", " + " and ".join(bits)

    # A natural sentence, not a field dump. "1 put buy of 1 print(s)"
    # is a data structure read aloud; nobody scanning an inbox parses it.
    if direction == MIXED_DIR:
        explain = ("%s %s bullish, %s bearish%s"
                   % (_count(bull, "print").capitalize(),
                      "points" if bull == 1 else "point",
                      _WORDS[bear] if bear < len(_WORDS) else bear, amb_tail))
    elif direction == NO_DIR:
        seen = (_side_breakdown(contracts, BULLISH)
                or _side_breakdown(contracts, BEARISH))
        explain = ("Only %s of %s %s a direction%s%s"
                   % (_WORDS[voted] if voted < len(_WORDS) else str(voted),
                      (_WORDS[n] if n < len(_WORDS) else str(n)) + " prints"
                      if n != 1 else "the one print",
                      "carries" if voted == 1 else "carry",
                      (" (%s)" % seen) if seen else "", amb_tail))
    else:
        lead = _side_breakdown(contracts, direction) or \
            "%s" % _count(voted, "directional print")
        explain = lead[0].upper() + lead[1:] + amb_tail
        if confirmation == CONF_PENDING:
            explain += ("; open interest for the session has not posted yet"
                        if pend == n else
                        "; the confirmation window is still open")
        elif conf == n:
            explain += ", each confirmed by a rise in open interest"
        elif conf and fail:
            explain += ("; open interest rose behind %s and fell behind %s"
                        % (_WORDS[conf] if conf < len(_WORDS) else conf,
                           _WORDS[fail] if fail < len(_WORDS) else fail))
        elif conf:
            explain += ("; open interest rose behind %s of them"
                        % (_WORDS[conf] if conf < len(_WORDS) else conf))
        elif fail:
            explain += ("; open interest did not rise behind %s"
                        % ("either" if fail == 2 else
                           "it" if fail == 1 else "any of them"))
        if pend and pend < n:
            explain += ", %d still pending" % pend
    if not explain.endswith("."):
        explain += "."

    score = "%d of %d confirmed" % (conf, n)
    if pend:
        score += " · %d pending" % pend
    return {"direction": direction, "confirmation": confirmation,
            "label": label, "n": n, "confirmed": conf,
            "failed": fail, "unknown": unk, "pending": pend,
            "score": score, "explain": explain}


def reconcile_ticker_flow(contracts):
    """Back-compat shim over classify_flow()."""
    r = classify_flow(contracts)
    return {"verdict": r["label"], "confirmed": r.get("confirmed", 0),
            "failed": r.get("failed", 0), "total": r["n"],
            "score": r.get("score", ""), "explain": r["explain"],
            "direction": r["direction"], "confirmation": r["confirmation"]}


# ── measured edge ───────────────────────────────────────────────────────
POSITIVE_EDGE = "POSITIVE MEASURED EDGE"
NO_EDGE = "NO MEASURED EDGE"
NEGATIVE_EDGE = "NEGATIVE MEASURED EDGE"
ACCRUING = "ACCRUING"
MIN_N = 30


def translate_edge(stats):
    """Turn a cohort record into a claim a reader can act on.

    A 49-50% hit rate with a near-zero excess return is not conviction,
    and must never be printed as though it were.
    """
    if not stats:
        return {"label": ACCRUING, "why": "no graded history yet"}
    n = stats.get("n") or 0
    if n < MIN_N:
        return {"label": ACCRUING,
                "why": "%d of %d graded outcomes needed" % (n, MIN_N)}
    excess = stats.get("excess_pct")
    hit = stats.get("hit_rate_pct")
    if excess is None:
        return {"label": ACCRUING, "why": "no excess-return measurement"}
    if abs(excess) < 0.5 or (hit is not None and 48 <= hit <= 52):
        return {"label": NO_EDGE,
                "why": "%.2f%% excess over %d trades%s — inside noise"
                       % (excess, n,
                          (", %.0f%% hit rate" % hit) if hit is not None else "")}
    if excess > 0:
        return {"label": POSITIVE_EDGE,
                "why": "%+.2f%% excess over %d trades" % (excess, n)}
    return {"label": NEGATIVE_EDGE,
            "why": "%+.2f%% excess over %d trades" % (excess, n)}


# ── news relevance and dedup ────────────────────────────────────────────
MAX_MARKET_NEWS = 5
MAX_WATCHLIST_NEWS = 5
_TIER_PRIMARY = ("company_ir", "sec", "regulator", "exchange")


def _norm_headline(h):
    s = re.sub(r"[^a-z0-9 ]", "", (h or "").lower())
    return " ".join(sorted(set(s.split())))[:120]


VERIFIED, MISATTRIBUTED, UNCONFIRMED = ("verified", "misattributed",
                                        "unconfirmed")


def verify_relevance(item, ticker, aliases=None):
    """Three outcomes, not two.

    A feed files an Nvidia story under six symbols, so attribution has to
    be checked. But most headlines name the COMPANY, not the ticker —
    "Micron guides Q4 above consensus" is a real MU story and rejecting
    it for lacking the letters "MU" throws away the news the user came
    for. So:

      verified      the ticker or one of its names appears
      misattributed another company is clearly the subject and this one
                    is absent — the real defect
      unconfirmed   neither; kept, but labelled rather than asserted
    """
    hay = " ".join([item.get("headline") or "", item.get("summary") or ""])
    names = list(item.get("company_words") or [])
    names += list((aliases or {}).get(ticker) or [])
    if re.search(r"\b%s\b" % re.escape(ticker), hay, re.I):
        return VERIFIED
    for w in names:
        if len(w) > 3 and re.search(r"\b%s" % re.escape(w), hay, re.I):
            return VERIFIED
    # does the headline name some OTHER covered company instead?
    for other, alist in (aliases or {}).items():
        if other == ticker:
            continue
        if re.search(r"\b%s\b" % re.escape(other), hay, re.I):
            return MISATTRIBUTED
        for w in alist or []:
            if len(w) > 3 and re.search(r"\b%s" % re.escape(w), hay, re.I):
                return MISATTRIBUTED
    return UNCONFIRMED


def select_news(items, watch_tickers, aliases=None,
                max_market=MAX_MARKET_NEWS, max_watch=MAX_WATCHLIST_NEWS):
    """Split into market-wide and watchlist news, dedupe syndication, and
    drop any item whose ticker attribution cannot be verified."""
    seen, market, watch, rejected = set(), [], [], []
    for it in items or []:
        key = _norm_headline(it.get("headline"))
        if key in seen:
            rejected.append({**it, "reason": "duplicate of a syndicated story"})
            continue
        seen.add(key)
        tks = [t for t in (it.get("tickers") or []) if t in watch_tickers]
        verdicts = {t: verify_relevance(it, t, aliases) for t in tks}
        keep = [t for t, v in verdicts.items()
                if v in (VERIFIED, UNCONFIRMED)]
        wrong = [t for t, v in verdicts.items() if v == MISATTRIBUTED]
        if wrong:
            it = {**it, "dropped_tickers": wrong}
        if keep:
            watch.append({**it, "tickers": keep,
                          "attribution": {t: verdicts[t] for t in keep}})
        elif not tks:
            market.append(it)
        else:
            rejected.append({**it, "reason":
                             "another company is the subject; %s not "
                             "mentioned" % ", ".join(wrong)})
    rank = lambda x: (0 if x.get("source_type") in _TIER_PRIMARY else 1,
                      -(x.get("impact") or 0))
    market.sort(key=rank)
    watch.sort(key=rank)
    return {"market": market[:max_market], "watchlist": watch[:max_watch],
            "rejected": rejected}


# ── subject + preheader ─────────────────────────────────────────────────
SUBJECT_MAX = 65
PREHEADER_MAX = 140


def _fit(parts, limit, sep=" · "):
    """Join while it fits. Dropping a whole clause beats truncating one
    mid-word — 'Semis lea' is worse than not mentioning sectors."""
    out = []
    for p in parts:
        if not p:
            continue
        cand = sep.join(out + [p])
        if len(cand) <= limit:
            out.append(p)
    return sep.join(out)


def build_subject(market, wl, flow_headline=None, sections=()):
    """Market first, then the user's names — the same order as the body.

    Every clause must correspond to something the reader will actually
    SEE. The sector clause is the one that went wrong: the subject read
    "Semis leads" while no sector line existed anywhere in the email, so
    the claim could be neither checked nor acted on. `sections` is the set
    of blocks the renderer will emit, and a clause is only offered when
    its evidence is among them.
    """
    sections = set(sections or ())
    reg = (market.get("regime") or {}).get("label") or "Market"
    tone = {"RISK-ON": "Risk-on tape", "RISK-OFF": "Risk-off tape",
            "BALANCED": "Balanced tape",
            "TRANSITION": "Tape in transition"}.get(reg, reg)
    parts = [tone]

    if "sectors" in sections:
        secs = (market.get("sectors") or {})
        if secs.get("leaders"):
            parts.append("%s leads" % secs["leaders"][0]["name"])
        elif secs.get("laggards"):
            parts.append("%s lags" % secs["laggards"][0]["name"])

    tail = []
    if flow_headline and "flow" in sections:
        tail.append(flow_headline)
    n = wl.get("n_changed") or 0
    if n and "watchlist" in sections:
        tail.append("%d watchlist change%s" % (n, "" if n == 1 else "s"))
    if tail:
        parts.append(" + ".join(tail))
    return _fit(parts, SUBJECT_MAX)


def build_preheader(market, wl, lines, sections=()):
    """Up to 140 chars of the numbers first, then what they did to the
    user's names. Gmail shows this beside the subject, so it must not
    repeat it, must use the body's units, and must end on a whole word --
    a preheader cut mid-token reads as a broken send."""
    sections = set(sections or ())
    a = lines.get("indices") or ""
    b = " ".join(x for x in (lines.get("vix"), lines.get("ten_year")) if x)
    head = " ".join(x for x in (a, b) if x)
    ev = (market.get("top_event") or {})
    ev_s = ""
    if ev.get("title") and "calendar" in sections:
        ev_s = "%s %s" % (ev.get("title"), ev.get("time_et") or "")
    tail = ""
    shown = (wl.get("shown") or []) if "watchlist" in sections else []
    if shown:
        tail = ", ".join(
            "%s %s" % (x["ticker"], (x.get("reasons") or ["changed"])[0])
            for x in shown[:2])
    s = _fit([head.strip(), ev_s.strip(), tail.strip()], PREHEADER_MAX)
    return _end_whole(s, PREHEADER_MAX)


def _end_whole(s, limit):
    """Trim to the last sentence or word boundary inside the limit."""
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    cut = s[:limit]
    for stop in (". ", "; ", " · ", ", ", " "):
        i = cut.rfind(stop)
        if i > limit * 0.5:
            return cut[:i].rstrip(" ·,;")
    return cut.rstrip(" ·,;")


# ── self-test ───────────────────────────────────────────────────────────
def self_test():
    fails = []

    ran = [0]

    def chk(name, cond, detail=""):
        ran[0] += 1
        print(("  PASS  " if cond else "  FAIL  ") + name +
              ("" if cond else "  <- %s" % detail))
        if not cond:
            fails.append(name)

    # ranking
    r = rank_ticker({"ticker": "MU", "trigger_hit": True, "has_flow": True})
    chk("trigger + flow -> TRIGGER REACHED", r["bucket"] == TRIGGER_REACHED, r)
    r = rank_ticker({"ticker": "X", "grade_delta": 1})
    chk("a delta with no readable transition is NOTABLE, not material",
        r["bucket"] == NOTABLE and not r["material"], r)
    r = rank_ticker({"ticker": "X", "grade_delta": 1, "grade_from": "B",
                     "grade_to": "B+"})
    chk("a stated grade transition IS material",
        r["material"] and "GRADE_TRANSITION" in r["reason_codes"], r)
    r = rank_ticker({"ticker": "Y"})
    chk("nothing changed -> QUIET", r["bucket"] == QUIET, r)
    out = rank_watchlist([{"ticker": "A", "trigger_hit": True, "has_flow": True},
                          {"ticker": "B", "grade_delta": 1},
                          {"ticker": "C"}, {"ticker": "D"}])
    chk("alert line counts only MATERIAL names",
        out["alert_line"].startswith(
            "1 of your 4 watch list names changed materially:"),
        out["alert_line"])
    chk("sub-material movement is reported separately",
        out["n_notable"] == 1 and "B" in (out["notable_line"] or ""),
        (out["n_notable"], out["notable_line"]))
    chk("material + notable + quiet = eligible",
        out["n_changed"] + out["n_notable"] + out["n_quiet"] == 4,
        (out["n_changed"], out["n_notable"], out["n_quiet"]))
    chk("alert line says WHY each name changed",
        "entry trigger reached" in out["alert_line"], out["alert_line"])
    chk("quiet names summarised, not listed in full",
        out["quiet_line"].startswith("No material change:")
        and "C" in out["quiet_line"], out["quiet_line"])
    many = [{"ticker": "T%d" % i, "trigger_hit": True, "has_flow": True}
            for i in range(9)]
    chk("displayed changed names capped at 5",
        len(rank_watchlist(many)["shown"]) == 5)

    # flow reconciliation — direction and confirmation are separate axes
    f = classify_flow([{"side": "call_buy", "status": "confirmed"},
                       {"side": "call_buy", "status": "failed"}])
    chk("same-direction split confirmation stays BULLISH",
        f["direction"] == BULLISH, f)
    chk("disagreeing OI evidence -> CONFLICTING CONFIRMATION",
        f["confirmation"] == CONF_CONFLICT, f)
    chk("score denominator counts every displayed contract",
        f["score"] == "1 of 2 confirmed", f["score"])
    f2 = classify_flow([{"side": "call_buy", "status": "confirmed"},
                        {"side": "put_buy", "status": "confirmed"}])
    chk("opposing directions -> MIXED", f2["direction"] == MIXED_DIR, f2)
    chk("MIXED explains both sides",
        "points bullish" in f2["explain"] and "bearish" in f2["explain"],
        f2)
    f3 = classify_flow([{"side": "call_buy", "status": "confirmed"}])
    chk("single confirmed bullish -> CONFIRMED BULLISH",
        f3["label"] == "CONFIRMED BULLISH", f3)

    # the TSLA bug: the scanner's own "mixed" verdict is ambiguity, not
    # absence. Folding it into "no side" let one put-seller carry three
    # prints to UNCONFIRMED BULLISH.
    f4 = classify_flow([{"side": "mixed"}, {"side": "put_seller"},
                        {"side": "mixed"}])
    chk("two-sided majority is not a direction",
        f4["direction"] == NO_DIR, f4)
    chk("two-sided prints are counted, not hidden",
        "two prints the tape could not resolve" in f4["explain"],
        f4["explain"])
    chk("unclear flow never claims a side",
        "BULLISH" not in f4["label"] and "call-buy" not in f4["explain"], f4)
    f5 = classify_flow([{"side": "mixed"}, {"side": "put_seller"},
                        {"side": "call_buyer"}])
    chk("directional majority still reads through the noise",
        f5["direction"] == BULLISH, f5)
    chk("explain names the sides actually present, not a generic noun",
        "one call purchase" in f5["explain"].lower()
        and "one put sale" in f5["explain"].lower(), f5["explain"])
    f6 = classify_flow([{"side": "put_buyer"}, {"side": "call_seller"}])
    chk("put-buy + call-sell both read bearish",
        f6["direction"] == BEARISH, f6)
    chk("bearish explain does not mislabel a call sell as a put buy",
        "call sale" in f6["explain"], f6["explain"])

    # the OI state machine: absence of a reading is not a failed reading
    import brief_time as _BT
    from datetime import datetime as _dt
    now = _dt(2026, 7, 22, 9, 0, tzinfo=_BT.ET)
    pend = oi_state({"printed_at": "2026-07-21T19:44:00Z"}, now)
    chk("today's print with no OI reading is OI PENDING",
        pend["state"] == CONF_PENDING, pend)
    chk("pending state names when the test becomes eligible",
        "ET" in (pend["reason"] or ""), pend)
    done = oi_state({"printed_at": "2026-07-20T19:44:00Z",
                     "oi_as_of": "2026-07-21T14:00:00Z", "oi_delta": 900}, now)
    chk("a real OI rise confirms", done["state"] == CONF_YES, done)
    miss = oi_state({"printed_at": "2026-07-20T19:44:00Z",
                     "oi_as_of": "2026-07-21T14:00:00Z", "oi_delta": -5}, now)
    chk("a real OI fall is UNCONFIRMED", miss["state"] == CONF_NO, miss)
    early = oi_state({"printed_at": "2026-07-20T19:44:00Z",
                      "oi_as_of": "2026-07-20T20:00:00Z", "oi_delta": 5}, now)
    chk("an OI reading inside the print session cannot confirm",
        early["state"] == CONF_PENDING, early)
    fp = classify_flow([{"side": "call_buy"}])
    chk("untested contracts read OI PENDING, never UNCONFIRMED",
        fp["confirmation"] == CONF_PENDING, fp)
    chk("premature UNCONFIRMED is a blocking finding",
        check_oi_states({"X": [{"oi_state": CONF_NO}]}), "no problem raised")

    # conflicting evidence is synthesised, not flattened
    gev = {"ticker": "GEV", "grade_delta": 1, "grade_from": "B",
           "grade_to": "A-", "has_flow": True, "flow_direction": BEARISH,
           "flow_short_dated": True, "earnings_in_days": 1,
           "price_change_pct": 0.4}
    g = rank_ticker(gev)
    chk("upgrade + bearish flow into earnings -> MIXED SETUP",
        g["bucket"] == MIXED_SETUP, g["bucket"])
    chk("synthesis names both halves and the event",
        all(k in g["synthesis_line"]
            for k in ("grade improved", "bearish", "earnings")),
        g["synthesis_line"])
    chk("agreeing evidence is not called mixed",
        rank_ticker(dict(gev, flow_direction=BULLISH))["bucket"]
        == STRENGTHENING)
    chk("the synthesis is not repeated as a separate reason",
        sum(1 for r in g["reasons"] if "grade" in r) == 1, g["reasons"])
    chk("the catalyst is not echoed beside the horizon it states",
        sum(1 for r in rank_ticker(
            {"ticker": "X", "has_flow": True, "earnings_in_days": 1,
             "catalyst": "Earnings in 1d"})["reasons"]
            if "earnings" in r.lower()) == 1,
        rank_ticker({"ticker": "X", "has_flow": True, "earnings_in_days": 1,
                     "catalyst": "Earnings in 1d"})["reasons"])
    # bearish flow as the ONLY evidence must not read as strengthening
    bear = rank_ticker({"ticker": "T", "has_flow": True, "flow_hq": True,
                        "flow_direction": BEARISH,
                        "price_change_pct": -0.2})
    chk("bearish OI-pending flow alone is an ALERT, not WEAKENING",
        bear["bucket"] == BEARISH_FLOW_ALERT, bear["bucket"])
    chk("the alert states that flow is the only evidence",
        "flow only" in bear["status_basis"], bear["status_basis"])
    conf = rank_ticker({"ticker": "T", "has_flow": True, "flow_hq": True,
                        "flow_direction": BEARISH, "flow_confirmed": True,
                        "price_change_pct": -0.2})
    chk("once open interest confirms it, WEAKENING is earned",
        conf["bucket"] == WEAKENING, conf["bucket"])
    chk("WEAKENING names its independent evidence",
        "open-interest confirmed" in conf["status_basis"],
        conf["status_basis"])
    cut = rank_ticker({"ticker": "T", "has_flow": True, "flow_hq": True,
                       "flow_direction": BEARISH, "grade_delta": -1,
                       "grade_from": "A", "grade_to": "A-"})
    chk("an independent grade cut also earns WEAKENING",
        cut["bucket"] == WEAKENING, cut["bucket"])
    # and nothing directional at all must not read as either
    none_dir = rank_ticker({"ticker": "TSLA", "has_flow": True,
                            "flow_hq": True, "flow_direction": NO_DIR,
                            "price_change_pct": -0.2})
    chk("unresolved evidence claims no direction",
        none_dir["bucket"] == MONITOR, none_dir["bucket"])
    chk("a status above MONITOR always carries a rule code",
        all(rank_ticker(c)["reason_codes"]
            for c in ({"ticker": "A", "grade_delta": -1, "grade_from": "A",
                       "grade_to": "B"},
                      {"ticker": "B", "price_change_pct": 9.0})),
        "a material status was issued with no rule")
    # levels must not name one price as both the target and the stop
    c2, i2 = confirmation_invalidation(
        {"close": 47.2, "ema20": "above", "ema20_dist": 16.3,
         "ema50": "above", "ema50_dist": 20.0, "atr_pct": 5.0})
    chk("no confirmation that repeats the invalidation level",
        c2 is None or (i2 and c2.split("$")[-1] != i2.split("$")[-1]),
        (c2, i2))

    # levels are derived from published numbers, or not stated at all
    conf, inval = confirmation_invalidation(
        {"close": 100.0, "ema20": "above", "ema20_dist": 5.0,
         "ema50": "below", "ema50_dist": 10.0, "atr_pct": 3.0})
    chk("invalidation quotes the nearest average below",
        bool(inval) and "95.24" in inval, inval)
    chk("confirmation quotes the average still to reclaim",
        bool(conf) and "111.11" in conf, conf)
    chk("no levels invented when the facts are absent",
        confirmation_invalidation({}) == (None, None))

    # reconciliation
    many = [{"ticker": "T%d" % i, "grade_delta": 2, "has_flow": True,
             "grade_from": "B", "grade_to": "A"} for i in range(13)]
    many += [{"ticker": "Q%d" % i} for i in range(3)]
    w = rank_watchlist(many)
    chk("changed + unchanged = eligible",
        w["n_changed"] + w["n_quiet"] == w["n_total"] == 16, w["n_total"])
    chk("hidden changed names are accounted for in one line",
        w["overflow_line"] == "Showing the top 5 of 13 material changes "
        "\u00b7 8 more on your desk.", w["overflow_line"])
    chk("count checks pass on a reconciled watch list",
        check_watchlist_counts(w, 16) == [])
    chk("a hidden name with no overflow line is blocked",
        check_watchlist_counts({**w, "overflow_line": ""}, 16))
    chk("a displayed row with no reason is blocked",
        check_watchlist_counts(
            {"shown": [{"ticker": "Z", "reasons": []}], "overflow": [],
             "quiet": [], "n_changed": 1}, 1))

    # document, subject, unsubscribe, negative zero, plain text
    chk("a doubly-wrapped document is blocked",
        check_document('<!doctype html><html lang="en"><head></head><body>'
                       '<!doctype html><html lang="en"><head></head>'
                       '<body>x</body></html></body></html>'))
    ok_doc = ('<!doctype html><html lang="en"><head></head><body>x'
              '</body></html>')
    chk("a single well-formed document passes", check_document(ok_doc) == [])
    chk("missing lang is blocked",
        check_document(ok_doc.replace(' lang="en"', "")))
    chk("negative zero is blocked", check_no_negative_zero("QQQ -0.0%"))
    # a ceremony has no "actual" to publish; demanding one blocked a whole
    # brief in production on an arrival ceremony that had simply finished
    chk("a completed data release with no actual is blocked",
        check_temporal([], [{"title": "Unemployment Claims",
                             "status": "COMPLETED", "forecast": "211K",
                             "anchor_et": "08:30"}], None))
    chk("a completed ceremony needs no actual",
        check_temporal([], [{"title": "State Arrival Ceremony",
                             "status": "COMPLETED"}], None) == [])
    chk("a released number with its actual passes",
        check_temporal([], [{"title": "CPI", "status": "COMPLETED",
                             "forecast": "3.1%", "actual": "3.0%"}],
                       None) == [])
    chk("plain zero passes", check_no_negative_zero("QQQ 0.0%") == [])
    chk("unsupported sector claim in the subject is blocked",
        check_subject_supported("Risk-off tape - Semis leads", "no sectors",
                                ("watchlist",)))
    chk("supported subject passes",
        check_subject_supported("Risk-off tape - 2 watchlist changes",
                                "2 of your 9 watch list names changed",
                                ("watchlist",)) == [])
    chk("over-long subject is blocked",
        check_subject_supported("x" * 80, "", ()))
    chk("watch-list-page unsubscribe is blocked",
        check_unsubscribe('<a href="https://t.io/#watchlist">Unsubscribe</a>',
                          "https://t.io/#watchlist"))
    chk("missing unsubscribe is blocked", check_unsubscribe("<p>x</p>", ""))
    chk("mismatched visible unsubscribe is blocked",
        check_unsubscribe('<a href="https://x.io/#watchlist">Unsubscribe</a>'
                          "https://api.t.io/unsubscribe?u=1",
                          "https://api.t.io/unsubscribe?u=1"))
    chk("matching signed unsubscribe passes",
        check_unsubscribe('<a href="https://api.t.io/unsubscribe?u=1">'
                          "Unsubscribe</a>",
                          "https://api.t.io/unsubscribe?u=1") == [])
    chk("plain text missing a URL is blocked",
        check_plain_text("see the desk", ["https://tickerdesk.io/#desk"]))
    chk("plain text carrying the URL passes",
        check_plain_text("desk: https://tickerdesk.io/#desk",
                         ["https://tickerdesk.io/#desk"]) == [])
    chk("a preheader cut mid-token is blocked",
        check_preheader("SPY +0.8% QQQ +1.9% - GEV grade B, TXG +9.2 -"))
    chk("mismatched yield units are blocked",
        check_preheader("SPY +0.8% 10Y 4.63% +0.65%",
                        "10-year yield 4.63% (+3 bp)"))
    chk("matching yield units pass",
        check_preheader("SPY +0.8% 10Y 4.63% (+3 bp)",
                        "10-year yield 4.63% (+3 bp)") == [])
    chk("a whole-word preheader passes",
        check_preheader("SPY +0.8% QQQ +1.9% - GEV grade improved.") == [])

    # edge
    chk("n below floor -> ACCRUING",
        translate_edge({"n": 12, "excess_pct": 3.0})["label"] == ACCRUING)
    chk("49% hit + tiny excess -> NO MEASURED EDGE",
        translate_edge({"n": 400, "excess_pct": 0.1,
                        "hit_rate_pct": 49.4})["label"] == NO_EDGE)
    chk("real positive excess -> POSITIVE",
        translate_edge({"n": 400, "excess_pct": 2.4,
                        "hit_rate_pct": 58})["label"] == POSITIVE_EDGE)
    chk("negative excess -> NEGATIVE",
        translate_edge({"n": 400, "excess_pct": -2.6,
                        "hit_rate_pct": 41})["label"] == NEGATIVE_EDGE)

    # news
    items = [
        {"headline": "Nvidia returns to NZS Growth Fund", "tickers": ["MU"],
         "source_type": "media"},
        {"headline": "Micron guides Q4 above consensus", "tickers": ["MU"],
         "source_type": "company_ir"},
        {"headline": "Micron guides Q4 above consensus!", "tickers": ["MU"],
         "source_type": "media"},
        {"headline": "Fed holds rates steady", "tickers": [],
         "source_type": "regulator"},
    ]
    sel = select_news(items, {"MU"},
                      aliases={"MU": ["Micron"], "NVDA": ["Nvidia"]})
    chk("syndicated duplicate removed",
        any("duplicate" in (r.get("reason") or "") for r in sel["rejected"]))
    chk("misattributed story rejected (Nvidia filed under MU)",
        any("another company is the subject" in (r.get("reason") or "")
            for r in sel["rejected"]), sel["rejected"])
    chk("verified watchlist story kept",
        any("Micron" in i["headline"] for i in sel["watchlist"]))
    chk("market-wide story separated from watchlist",
        any("Fed" in i["headline"] for i in sel["market"]))
    chk("market news capped at 3", len(sel["market"]) <= 3)

    # subject + preheader
    mk = {"regime": {"label": "RISK-OFF"},
          "sectors": {"leaders": [{"name": "Energy"}],
                      "laggards": [{"name": "Semis"}]},
          "top_event": {"title": "UoM Sentiment", "time": "2:00pm"}}
    wl = rank_watchlist([{"ticker": "MU", "trigger_hit": True,
                          "has_flow": True},
                         {"ticker": "ASML", "grade_delta": -2}])
    SECTIONS = ("watchlist", "flow", "calendar")
    subj = build_subject(mk, wl, flow_headline="MU flow", sections=SECTIONS)
    chk("subject leads with regime", subj.startswith("Risk-off tape"), subj)
    chk("subject names the watchlist change count",
        "1 watchlist change" in subj, subj)
    chk("subject within 65 chars", len(subj) <= SUBJECT_MAX, len(subj))
    # the "Semis lags" clause must not appear: this fixture displays no
    # sector block, and an unsupported subject claim is the bug
    chk("sector claim withheld when no sector section renders",
        "lags" not in subj and "leads" not in subj, subj)
    chk("sector claim allowed once the section is displayed",
        "Energy leads" in build_subject(mk, wl,
                                        sections=SECTIONS + ("sectors",)),
        build_subject(mk, wl, sections=SECTIONS + ("sectors",)))
    chk("subject survives its own gate",
        check_subject_supported(subj, "1 of your 4 watch list names changed",
                                SECTIONS) == [], subj)

    lines = {"indices": "SPY +1.2% QQQ +1.6%", "vix": "VIX -1.0%",
             "ten_year": "10Y 4.63% (+3 bp)"}
    pre = build_preheader(mk, wl, lines, sections=SECTIONS)
    chk("preheader within 140 chars", len(pre) <= PREHEADER_MAX, len(pre))
    chk("preheader leads with market numbers", pre.startswith("SPY"), pre)
    chk("preheader mentions the user's names", "MU" in pre, pre)
    chk("preheader carries the body's yield units", "bp" in pre, pre)
    chk("preheader does not repeat the subject verbatim", pre != subj)
    chk("preheader passes its own gate", check_preheader(pre) == [], pre)
    chk("calendar clause withheld when no calendar renders",
        "UoM" not in build_preheader(mk, wl, lines, sections=("watchlist",)))

    # count what actually ran — a hardcoded total silently under-reports
    # every check added after it was written
    print("\n%d/%d checks passed" % (ran[0] - len(fails), ran[0]))
    return 1 if fails else 0


# ── send-time invariants ────────────────────────────────────────────────
# A brief that renders is not the same as a brief that is true. These run
# on the assembled payload immediately before send and BLOCK delivery.

_PLACEHOLDER_URL = re.compile(
    r"https?://(x|y|z|example\.(com|org)|test|localhost|placeholder)[/\s\"']",
    re.I)
_TOKEN = re.compile(r"\{\{|\}\}|%\(|\{\d*\}|__[A-Z_]+__|TODO|FIXME|lorem")


def check_index_count(market):
    """The prose count and the table must agree.

    Saying "1 of 4 above their 20-day" while every row shows a negative
    vs-20d is the exact contradiction that erodes trust in every other
    number on the page.
    """
    v = []
    idx = (market or {}).get("indices") or {}
    if not idx:
        return v
    above = 0
    for t, d in idx.items():
        dist = d.get("dist_ma20_pct")
        flag = d.get("above_ma20")
        if dist is not None and flag is not None and (dist >= 0) != bool(flag):
            v.append("%s: vs-20d %+.2f%% contradicts above_ma20=%s"
                     % (t, dist, flag))
        if flag:
            above += 1
    why = ((market.get("regime") or {}).get("why") or "")
    m = re.search(r"(\d+)\s+of\s+(\d+)\s+major indices", why)
    if m:
        said, total = int(m.group(1)), int(m.group(2))
        if said != above or total != len(idx):
            v.append("summary says %d of %d indices above the 20-day but the "
                     "table shows %d of %d" % (said, total, above, len(idx)))
    return v


def check_breadth_defined(market):
    b = (market or {}).get("breadth") or {}
    if not b:
        return []
    if b.get("breadth_pct") is None:
        return []
    if not b.get("universe"):
        return ["breadth percentage published with no universe size — "
                "'breadth 33%' of what?"]
    return []


def check_no_placeholders(payload_text):
    """URLs are checked against the raw markup (they live in attributes);
    template tokens are checked against the RENDERED TEXT only.

    Scanning raw HTML for tokens matched the "}}" that closes a CSS media
    query and would have blocked every brief — a validator that fails
    closed on itself is an outage, not a safeguard.
    """
    v = []
    raw = payload_text or ""
    for m in set(_PLACEHOLDER_URL.findall(raw)):
        v.append("placeholder URL host %r reached the email" % (m[0],))
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</" + chr(92) + "1>", " ", raw)
    text = re.sub(r"<[^>]+>", " ", text)
    for m in set(_TOKEN.findall(text)):
        v.append("unresolved template token %r reached the email" % m)
    return v


def check_temporal(news, events, as_of):
    """Nothing published after the send time may be described in the past
    tense, and no future event may be marked complete."""
    v = []
    for it in (news or []):
        p = it.get("published_at") or it.get("published_ts")
        if p and as_of and str(p) > str(as_of):
            v.append("news %r is timestamped after the brief's as-of time"
                     % str(it.get("headline"))[:48])
    for e in (events or []):
        st = (e.get("status") or "").upper()
        # Only a DATA RELEASE publishes an "actual". A ceremony, a speech
        # or a bank holiday completes by the clock passing and has no
        # number to print, so demanding one blocked the whole brief on an
        # 8:00 a.m. arrival ceremony that had simply finished.
        is_release = bool(e.get("forecast") or e.get("previous")
                          or e.get("anchor_et"))
        if st in ("COMPLETED", "RELEASED") and is_release \
                and not e.get("actual"):
            v.append("release %r marked %s with no published actual"
                     % (str(e.get("title"))[:40], st))
    return v


def check_watchlist_counts(wl, eligible_total):
    """changed + unchanged = eligible, and nothing changed is dropped."""
    v = []
    shown = wl.get("shown") or []
    over = wl.get("overflow") or []
    quiet = wl.get("quiet") or []
    n = len(shown) + len(over) + len(wl.get("notable") or []) + len(quiet)
    if eligible_total is not None and n != eligible_total:
        v.append("watch list counts do not reconcile: %d ranked vs %d eligible"
                 % (n, eligible_total))
    changed = len(shown) + len(over)
    if (wl.get("n_changed") or 0) != changed:
        v.append("changed count %d disagrees with %d ranked as changed"
                 % (wl.get("n_changed") or 0, changed))
    if over and not (wl.get("overflow_line") or "").strip():
        v.append("%d changed name(s) hidden by the display cap with no "
                 "overflow line to account for them" % len(over))
    for x in shown:
        if not (x.get("reasons") or []):
            v.append("%s is displayed with no reason" % x.get("ticker"))
    return v


def check_no_negative_zero(text):
    """'-0.0%' is a rounding artefact, never a real reading."""
    hits = re.findall(r"-0\.0+\s*%", text or "")
    return (["negative zero rendered %d time(s), e.g. %r"
             % (len(hits), hits[0])] if hits else [])


def check_subject_supported(subject, text, sections=()):
    """Every claim in the subject must be visible in the body.

    A subject is a promise about the contents. "Semis leads" with no
    sector line anywhere is a promise the email does not keep, and the
    reader cannot tell whether the claim is wrong or merely missing.
    """
    v = []
    body = (text or "").lower()
    sections = set(sections or ())
    m = re.search(r"([A-Za-z][\w &/-]*?) (leads|lags)\b", subject or "")
    if m and "sectors" not in sections:
        v.append("subject claims %r but no sector line is displayed"
                 % m.group(0))
    elif m and m.group(1).lower() not in body:
        v.append("subject claims %r but %r does not appear in the body"
                 % (m.group(0), m.group(1)))
    if re.search(r"\bflow\b", subject or "") and "flow" not in sections:
        v.append("subject references flow but no flow section is displayed")
    m = re.search(r"(\d+) watchlist change", subject or "")
    if m and ("%s of your" % m.group(1)) not in body:
        v.append("subject says %s watchlist changes but the body does not "
                 "state that count" % m.group(1))
    if len(subject or "") > SUBJECT_MAX:
        v.append("subject is %d chars, over the %d limit"
                 % (len(subject), SUBJECT_MAX))
    return v


def check_preheader(pre, body=""):
    v = []
    pre = pre or ""
    if len(pre) > PREHEADER_MAX:
        v.append("preheader is %d chars, over the %d limit"
                 % (len(pre), PREHEADER_MAX))
    # ")" closes a parenthetical like "(+3 bp)" and is a complete ending;
    # a dangling separator is not
    if pre and pre[-1] not in ".!?%)" and not pre[-1].isalnum():
        v.append("preheader ends mid-token: %r" % pre[-12:])
    # a yield move must be quoted in the same unit on both surfaces. The
    # preheader once read "10Y 4.63% +0.65%" beside a body that said
    # "+3 bp" -- the same move in two units, one of them meaningless to a
    # reader who does not know it is a percentage OF a percentage.
    if re.search(r"\b10Y\b|10-year", pre):
        pre_bp = bool(re.search(r"[-+]?\d+\s*bp", pre))
        body_bp = bool(re.search(r"[-+]?\d+\s*bp", body or ""))
        if body_bp and not pre_bp:
            v.append("preheader quotes the yield change in percent while "
                     "the body uses basis points")
    return v


def check_unsubscribe(html_doc, unsub):
    """The visible link and the List-Unsubscribe header must be the same
    subscriber-specific endpoint, and it must not be a site page."""
    v = []
    if not unsub:
        return ["no subscriber-specific unsubscribe URL was generated"]
    if re.search(r"#watchlist", unsub):
        v.append("unsubscribe URL points at the watch-list page")
    # compare against the UNESCAPED markup: a signed URL carries query
    # parameters, so its "&" is written "&amp;" in the href and a raw
    # substring test would report a mismatch that does not exist
    doc = _unescape(html_doc or "")
    if unsub not in doc:
        v.append("the unsubscribe URL in the headers does not appear as a "
                 "visible link in the body")
    # any OTHER href sitting under the word Unsubscribe is a mismatch
    for href in re.findall(r'href="([^"]+)"[^>]*>\s*Unsubscribe', doc, re.I):
        if href != unsub:
            v.append("visible Unsubscribe links to %r, not the signed "
                     "endpoint" % href)
    return v


def check_oi_states(watch_flow, market_flow=()):
    """UNCONFIRMED may only be claimed after the test has actually run."""
    v = []
    groups = list((watch_flow or {}).items()) + [("market", list(market_flow))]
    for tk, group in groups:
        for c in group or []:
            st = c.get("oi_state")
            if st == CONF_NO and c.get("oi_delta") is None:
                v.append("%s marks a contract UNCONFIRMED with no "
                         "open-interest reading" % tk)
    return v


def check_plain_text(text, urls=()):
    """The text part must carry the links, not just their labels."""
    v = []
    t = text or ""
    if not t.strip():
        return ["plain-text alternative is empty"]
    for u in urls:
        if u and u not in t:
            v.append("plain-text body is missing the %s URL"
                     % ("unsubscribe" if "unsub" in u else u[:48]))
    return v


def check_document(html_doc):
    """Exactly one document. A nested second <html> is invisible in most
    clients and silently changes what any validator is looking at."""
    v = []
    d = html_doc or ""
    for tag, pat in (("<!doctype>", r"<!doctype"), ("<html>", r"<html[\s>]"),
                     ("<head>", r"<head[\s>]"), ("<body>", r"<body[\s>]")):
        n = len(re.findall(pat, d, re.I))
        if n != 1:
            v.append("document contains %d %s (expected exactly 1)" % (n, tag))
    if d and not re.search(r'<html[^>]*\blang="en"', d, re.I):
        v.append('<html> is missing lang="en"')
    return v


def check_flow_labels(watch_flow):
    """MIXED is reserved for opposing directions."""
    v = []
    for tk, group in (watch_flow or {}).items():
        r = classify_flow(group)
        if r["label"] == MIXED_DIR and r["direction"] != MIXED_DIR:
            v.append("%s labelled MIXED without opposing directional "
                     "evidence" % tk)
    return v


def visible_text(html_doc):
    """The words a reader actually sees — markup, styles and the hidden
    preheader removed. Every content check runs against THIS, not the
    markup, so a claim buried in an attribute cannot satisfy one."""
    t = re.sub(r"(?is)<(script|style|title)[^>]*>.*?</" + chr(92) + "1>",
               " ", html_doc or "")
    t = re.sub(r"(?is)<div[^>]*mso-hide:all.*?</div>", " ", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", _unescape(t))


def _unescape(s):
    import html as _h
    return _h.unescape(s)


def check_model(model):
    """Structural checks on the canonical view, before either rendering.

    These are the claims a reader cannot verify from the page but which
    the model can prove: that a flow-based ranking has its contracts
    somewhere in the email, that no contract omits its side, and that a
    ticker is not shown twice for one idea.
    """
    if not model:
        return []
    v = []
    secs = {s["id"]: s for s in model["sections"]}
    wl = secs.get("watchlist") or {"records": []}
    flow_names = set()
    for sid in ("flow_driving", "flow_other", "flow_market"):
        for r in (secs.get(sid) or {}).get("records") or []:
            flow_names.add(r.get("ticker"))
            for c in r.get("contracts") or []:
                flow_names.add(c.get("ticker"))

    # a name ranked on flow must show the flow
    for r in wl["records"]:
        codes = r.get("reason_codes") or []
        blob = " ".join(r.get("reasons") or []).lower()
        if ("FLOW_HQ" in codes or "flow" in blob) \
                and r["ticker"] not in flow_names:
            v.append("%s is ranked on options flow but no contract for it "
                     "appears anywhere in the email" % r["ticker"])

    # every contract states a side and a direction
    for sid, sec in secs.items():
        for r in sec.get("records") or []:
            for c in ([r] if r.get("action") else []) + (
                    r.get("contracts") or []):
                if not c.get("action") or c["action"] == "UNSPECIFIED":
                    v.append("%s contract %s has no buy/sell side"
                             % (sid, c.get("key")))
                if not c.get("direction"):
                    v.append("%s contract %s has no inferred direction"
                             % (sid, c.get("key")))

    # discovery must not repeat market-wide flow verbatim
    mkt = {c.get("ticker") for c in
           (secs.get("flow_market") or {}).get("records") or []}
    for d in (secs.get("discovery") or {}).get("records") or []:
        if d["ticker"] in mkt:
            v.append("%s appears in both market-wide flow and discovery with "
                     "no additional context" % d["ticker"])

    # a price cell is never blank
    for r in wl["records"]:
        p = r.get("price") or {}
        if not (p.get("text") or "").strip():
            v.append("%s renders an empty price" % r["ticker"])
        if p.get("value") is None and "unavailable" not in \
                (p.get("text") or "").lower():
            v.append("%s has no price and does not say so" % r["ticker"])
        # A basis word sitting immediately after the separator means the
        # timestamp that belongs between them is missing: "· pre-market "
        # is truncated, while "· Jul 21 close" and the explicit no-clock
        # forms ("· prior close", "· pre-market quote") are complete.
        if re.search(r"·\s*(pre-market|close|detection)\s*$",
                     p.get("text") or ""):
            v.append("%s price label ends where its timestamp should be: %r"
                     % (r["ticker"], p.get("text")))

    # a status must be traceable to a rule that fired
    for r in wl["records"]:
        if r.get("status") in MATERIAL_STATES and not r.get("reason_codes"):
            v.append("%s is %s with no materiality rule recorded"
                     % (r["ticker"], r["status"]))
    return v


def validate_send(*, market=None, wl=None, news=None, events=None,
                  watch_flow=None, market_flow=None, html_doc="", text_doc="",
                  as_of=None, eligible_total=None, subject="", preheader="",
                  unsub="", sections=(), urls=(), calendar_problems=(),
                  model=None):
    """Every blocking check in one call. Empty list = safe to send."""
    v = []
    body = visible_text(html_doc)
    v += check_model(model)
    v += check_document(html_doc)
    v += check_index_count(market)
    v += check_breadth_defined(market)
    v += check_no_placeholders(html_doc)
    v += check_temporal(news, events, as_of)
    v += check_flow_labels(watch_flow)
    v += check_oi_states(watch_flow, market_flow or ())
    v += check_no_negative_zero(body)
    v += list(calendar_problems or ())
    if subject:
        v += check_subject_supported(subject, body, sections)
    if preheader:
        v += check_preheader(preheader, body)
    if html_doc:
        v += check_unsubscribe(html_doc, unsub)
    if text_doc:
        v += check_plain_text(text_doc, urls)
    if wl is not None:
        v += check_watchlist_counts(wl, eligible_total)
    return v


if __name__ == "__main__":
    raise SystemExit(self_test() if "--self-test" in sys.argv else self_test())
