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


def _period_of(fact):
    return str(fact.get("period_end"))[:10] \
        if isinstance(fact, dict) and fact.get("period_end") else None


def business_quality(snap, grid=None, adapter=None):
    """Filed-facts-only quality read. Scores the dimensions the filed
    record can actually speak to AND the sector adapter's policy
    permits (§2) — a bank is never graded on generic revenue growth, a
    broker never on industrial cash conversion. Balance-sheet pairings
    require matching reporting dates (§1); a 2017 debt instant is never
    netted against current cash. Management, moat and industry
    structure stay out until a source exists."""
    fu = snap.get("fundamentals") or {}
    pol = ((adapter or {}).get("policy") or {})
    banned = set(pol.get("quality_metrics_forbidden") or ())
    growth = _fv(fu.get("revenue_growth")) \
        if "revenue_growth" not in banned else None
    net_m = _fv(fu.get("net_margin"))
    gross_m = _fv(fu.get("gross_margin")) \
        if "gross_margin" not in banned else None
    rev = _fv(fu.get("revenue_q"))
    fcf = (_fv(fu.get("free_cash_flow"))
           or _fv(fu.get("operating_cash_flow"))) \
        if "cash_conversion" not in banned else None
    cash = _fv(fu.get("cash"))
    debt = _fv(fu.get("debt"))
    metrics_used = []

    # §1 same-period rule for the net-cash read
    bs_period_ok = True
    cp, dp = _period_of(fu.get("cash")), _period_of(fu.get("debt"))
    if cash is not None and debt is not None and cp and dp and cp != dp:
        from datetime import date
        try:
            gap = abs((date.fromisoformat(cp)
                       - date.fromisoformat(dp)).days)
        except ValueError:
            gap = 9999
        bs_period_ok = gap <= 100
    # §2 (v5.8): same-period relics still cannot grade a CURRENT
    # position — instants older than the recency floor are not netted
    if bs_period_ok and (cp or dp):
        from datetime import date as _d, timedelta as _t
        import report_v5_checks as _CK8
        _newest = max(p for p in (cp, dp) if p)
        if _newest < (_d.today() - _t(
                days=_CK8.BS_LATEST_MAX_AGE_DAYS)).isoformat():
            bs_period_ok = False

    pos, neg, reasons = 0, 0, []
    assessed = 0

    if growth is not None:
        assessed += 1
        metrics_used.append("revenue_growth")
        if growth >= GROWTH_STRONG:
            pos += 1
            reasons.append("filed growth %.1f%% clears %.0f%%"
                           % (growth, GROWTH_STRONG))
        elif growth < 0:
            neg += 1
            reasons.append("filed revenue shrinking (%.1f%%)" % growth)
    if net_m is not None:
        assessed += 1
        metrics_used.append("net_margin")
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
        metrics_used.append("gross_margin")
        if gross_m >= GM_STRONG:
            pos += 1
            reasons.append("gross margin %.0f%%; pricing power not independently assessed"
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
    not_assessed = ["industry structure", "moat direction",
                    "management record", "unit economics"]
    if cash is not None and debt is not None \
            and "net_cash" not in banned:
        if not bs_period_ok:
            # §1/§2 (v5.8): instants from different reporting dates —
            # or instants too old to describe a current position —
            # never net; the canonical sentence renders verbatim
            import report_v5_checks as _CK
            not_assessed.append(
                "balance-sheet position — %s (cash instant %s vs debt "
                "instant %s%s)"
                % (_CK.BS_NOT_ASSESSED_MSG.rstrip("."), cp, dp,
                   "; instants predate the recency floor"
                   if cp == dp else ""))
        else:
            assessed += 1
            metrics_used.append("net_cash")
            if cash > debt:
                pos += 1
                reasons.append("net cash balance sheet")
            elif debt > 3 * max(cash, 1):
                neg += 1
                reasons.append("debt more than 3x cash")
    if banned:
        not_assessed.append(
            "sector-inapplicable metrics excluded by the %s adapter: %s"
            % ((adapter or {}).get("label") or "sector",
               ", ".join(sorted(m.replace("_", " ") for m in banned))))

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
    # The filed record can grade REPORTED financial quality; it cannot
    # grade moat, management, industry structure or unit economics. A
    # STRONG with all four unassessed is overreach — the overall read
    # is "partially underwritten" until those have admitted sources.
    return {"level": level,
            "reported_financial_quality": level,
            "overall": "PARTIALLY_UNDERWRITTEN",
            "display": "%s / Partially underwritten" % level.title(),
            "reasons": reasons[:4],
            "not_assessed": not_assessed,
            "metrics_used": metrics_used,
            "adapter_key": (adapter or {}).get("key"),
            # §1: a conclusion is only as fresh as its OLDEST material
            # support — the flow facts share the latest quarter, and
            # the balance-sheet pairing is date-matched above, so the
            # oldest period among the facts actually used is binding
            "freshness_basis": min(
                [p for p in (_period_of(fu.get(k)) for k in
                             ("revenue_growth", "revenue_q",
                              "net_margin", "gross_margin",
                              "free_cash_flow", "operating_cash_flow",
                              "cash", "debt")
                             if (k in ("cash", "debt")
                                 and "net_cash" in metrics_used)
                             or (k not in ("cash", "debt")))
                 if p], default=None),
            "basis": "filed facts only; %d dimensions assessed; "
                     "sector policy applied; qualitative dimensions "
                     "not underwritten" % assessed}


def investment_attractiveness(scenarios, expectations, event,
                              confidence_level=None):
    """Price-relative read, separate from quality. NOT_UNDERWRITTEN
    whenever the scenario table itself was withheld — attractiveness
    without a valuation basis would be a mood."""
    sc = scenarios or {}
    if not sc.get("available"):
        return {"level": "NOT_UNDERWRITTEN",
                "missing": ["valuation basis", "forecasts",
                            "expectations", "downside assumptions"],
                "reasons": [sc.get("reason")
                            or "no valuation basis survived"],
                "basis": "no valuation basis"}

    # Attractiveness must rest on ACTUAL underwriting. The historical
    # range holds the trailing metric constant with no operating
    # assumptions — mean reversion toward a median is context, never an
    # expected return, so it contributes NOTHING to the level here.
    if sc.get("mode") != "underwritten":
        missing = ["operating forecasts (assumptions file)",
                   "leg probabilities (assumptions file)"]
        var = (expectations or {}).get("variant") or {}
        if not var.get("available"):
            missing.append("sourced KPI-level market expectations")
        return {"level": "PROVISIONAL",
                "qualifier": "PROVISIONAL",
                "display": "PROVISIONAL",
                "missing": missing,
                "reasons": ["not underwritten: the historical valuation "
                            "range is descriptive context only",
                            "missing inputs: %s" % "; ".join(missing)],
                "basis": "no forecasts, expectations or downside "
                         "assumptions admitted"}

    rows = {r["leg"]: r for r in sc.get("rows") or []}
    base = rows.get("base")
    pos, neg, reasons = 0, 0, []
    if base:
        gap = base["vs_spot_pct"]
        if gap >= DISCOUNT_BIG:
            pos += 1
            reasons.append("underwritten base case %+.0f%% vs spot"
                           % gap)
        elif gap <= -DISCOUNT_BIG:
            neg += 1
            reasons.append("underwritten base case %+.0f%% vs spot"
                           % gap)
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
    return {"level": level, "qualifier": None, "display": level,
            "missing": [],
            "reasons": reasons[:4],
            "basis": "underwritten scenarios with stated assumptions"}


def tension(bq, ia):
    """The one-line display of the two reads together — never a blend."""
    q = {"EXCEPTIONAL": "exceptional", "STRONG": "strong",
         "AVERAGE": "average", "WEAK": "weak",
         "NOT_ESTABLISHED": "not-established"}[bq["level"]]
    a = {"HIGH": "high", "MODERATE": "moderate", "LOW": "low",
         "UNATTRACTIVE": "unattractive",
         "PROVISIONAL": "provisional (not underwritten)",
         "NOT_UNDERWRITTEN": "not underwritten"}[ia["level"]]
    return ("Reported financial quality %s (overall: partially "
            "underwritten); investment attractiveness %s at the "
            "current price." % (q, a))
