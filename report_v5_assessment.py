#!/usr/bin/env python3
"""report_v5_assessment.py — Business Quality vs Investment
Attractiveness (v5.5 phase D).

Two SEPARATE categorical assessments, never averaged into a composite:

  Business Quality:        EXCEPTIONAL / STRONG / AVERAGE / WEAK /
                           NOT_ESTABLISHED
  Investment Attractiveness: HIGH / MODERATE / LOW / UNATTRACTIVE /
                           NOT_UNDERWRITTEN

Each is mechanical — named thresholds over admitted facts, reasons
attached — and the tension between them is displayed, not resolved
("Strong business quality; low investment attractiveness at the
current price"). A great business at a silly price and a mediocre
business at a give-away price are DIFFERENT information; averaging
them destroys both.
"""

import research_snapshot as rs

BQ_LEVELS = ("EXCEPTIONAL", "STRONG", "AVERAGE", "WEAK",
             "NOT_ESTABLISHED")
IA_LEVELS = ("HIGH", "MODERATE", "LOW", "UNATTRACTIVE",
             "NOT_UNDERWRITTEN")

# Named thresholds (cited in reasons).
GROWTH_STRONG = 20.0
MARGIN_GOOD = 10.0
MARGIN_OUTLIER = 40.0
GM_STRONG = 60.0
FCF_CONV_GOOD = 15.0
FCF_CONV_OUTLIER = 60.0
DISCOUNT_BIG = 25.0          # % gap to base scenario
ASYM_GOOD = 2.0              # upside/downside ratio


def _fv(x):
    return rs.fv(x) if isinstance(x, dict) else x


def business_quality(snap, grid=None):
    """Filed-facts-only quality read. Scores four dimensions the filed
    record can actually speak to; management, moat and industry
    structure stay out until a source exists (they are named in the
    not-assessed list rather than silently skipped)."""
    fu = snap.get("fundamentals") or {}
    growth = _fv(fu.get("revenue_growth"))
    net_m = _fv(fu.get("net_margin"))
    gross_m = _fv(fu.get("gross_margin"))
    rev = _fv(fu.get("revenue_q"))
    fcf = _fv(fu.get("free_cash_flow")) \
        or _fv(fu.get("operating_cash_flow"))
    cash = _fv(fu.get("cash"))
    debt = _fv(fu.get("debt"))

    pos, neg, reasons = 0, 0, []
    assessed = 0

    if growth is not None:
        assessed += 1
        if growth >= GROWTH_STRONG:
            pos += 1
            reasons.append("filed growth %.1f%% clears %.0f%%"
                           % (growth, GROWTH_STRONG))
        elif growth < 0:
            neg += 1
            reasons.append("filed revenue shrinking (%.1f%%)" % growth)
    if net_m is not None:
        assessed += 1
        if MARGIN_GOOD <= net_m <= MARGIN_OUTLIER:
            pos += 1
            reasons.append("net margin %.1f%%" % net_m)
        elif net_m < 0:
            neg += 1
            reasons.append("unprofitable quarter (net margin %.1f%%)"
                           % net_m)
        elif net_m > MARGIN_OUTLIER:
            reasons.append("net margin %.0f%% treated as one-time, not "
                           "quality" % net_m)
    if gross_m is not None:
        assessed += 1
        if gross_m >= GM_STRONG:
            pos += 1
            reasons.append("gross margin %.0f%% (pricing power proxy)"
                           % gross_m)
    if fcf is not None and rev:
        conv = 100.0 * fcf / rev
        if conv <= FCF_CONV_OUTLIER:
            assessed += 1
            if conv >= FCF_CONV_GOOD:
                pos += 1
                reasons.append("cash conversion %.0f%% of revenue"
                               % conv)
            elif conv < 0:
                neg += 1
                reasons.append("cash-burning quarter")
    if cash is not None and debt is not None:
        assessed += 1
        if cash > debt:
            pos += 1
            reasons.append("net cash balance sheet")
        elif debt > 3 * max(cash, 1):
            neg += 1
            reasons.append("debt more than 3x cash")

    not_assessed = ["industry structure", "moat direction",
                    "management record", "unit economics"]

    if assessed < 2:
        level = "NOT_ESTABLISHED"
        reasons = ["fewer than two quality dimensions had admitted "
                   "facts"] + reasons
    elif neg == 0 and pos >= 4:
        level = "EXCEPTIONAL"
    elif neg == 0 and pos >= 3:
        level = "STRONG"
    elif pos > neg:
        level = "AVERAGE"
    else:
        level = "WEAK"
    return {"level": level, "reasons": reasons[:4],
            "not_assessed": not_assessed,
            "basis": "filed facts only; %d dimensions assessed"
                     % assessed}


def investment_attractiveness(scenarios, expectations, event,
                              confidence_level=None):
    """Price-relative read, separate from quality. NOT_UNDERWRITTEN
    whenever the scenario table itself was withheld — attractiveness
    without a valuation basis would be a mood."""
    sc = scenarios or {}
    if not sc.get("available"):
        return {"level": "NOT_UNDERWRITTEN",
                "reasons": [sc.get("reason")
                            or "no valuation basis survived"],
                "basis": "no scenario table"}
    rows = {r["leg"]: r for r in sc.get("rows") or []}
    base, bear, bull = rows.get("base"), rows.get("bear"), rows.get("bull")
    pos, neg, reasons = 0, 0, []

    if base:
        gap = base["vs_spot_pct"]
        if gap >= DISCOUNT_BIG:
            pos += 1
            reasons.append("base scenario %+.0f%% vs spot" % gap)
        elif gap <= -DISCOUNT_BIG:
            neg += 1
            reasons.append("base scenario %+.0f%% vs spot" % gap)
    asym = sc.get("asymmetry") or {}
    ratio = asym.get("up_down_ratio")
    if ratio is not None:
        if ratio >= ASYM_GOOD:
            pos += 1
            reasons.append("upside/downside %.1fx" % ratio)
        elif ratio < 1.0:
            neg += 1
            reasons.append("downside exceeds upside (%.1fx)" % ratio)
    var = (expectations or {}).get("variant") or {}
    if var.get("available"):
        pos += 1
        reasons.append("sourced expectations gap %+.1f%%"
                       % var.get("gap_pct", 0))
    else:
        reasons.append("no sourced expectations gap")
    if confidence_level == "Low":
        neg += 1
        reasons.append("low data confidence")

    if pos >= 2 and neg == 0:
        level = "HIGH"
    elif pos > neg:
        level = "MODERATE"
    elif neg > pos:
        level = "UNATTRACTIVE"
    else:
        level = "LOW"
    return {"level": level, "reasons": reasons[:4],
            "basis": "price-relative only; quality assessed separately"}


def tension(bq, ia):
    """The one-line display of the two reads together — never a blend."""
    q = {"EXCEPTIONAL": "exceptional", "STRONG": "strong",
         "AVERAGE": "average", "WEAK": "weak",
         "NOT_ESTABLISHED": "not-established"}[bq["level"]]
    a = {"HIGH": "high", "MODERATE": "moderate", "LOW": "low",
         "UNATTRACTIVE": "unattractive",
         "NOT_UNDERWRITTEN": "not underwritten"}[ia["level"]]
    return ("%s business quality; %s investment attractiveness at the "
            "current price." % (q.capitalize(), a))
