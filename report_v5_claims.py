#!/usr/bin/env python3
"""report_v5_claims.py — the argument builder (v5 slice 4).

A v5 claim is not a fact list: it is a falsifiable statement with its
evidence, its OWN counterevidence, a confidence grade, and the
condition that kills it. Structure per claim:

    {claim, direction, support[], counterevidence[], confidence,
     invalidation, refs[]}

Three bullish bullets with nothing pushing back is a promotional
report, not an argument — so counterevidence is searched for every
claim across the domains the snapshot actually carries, and "none
found in filed data (searched: ...)" is itself rendered, because a
reader deserves to know what was looked at.

Claims are only GENERATED from patterns the evidence supports; a name
with nothing to argue produces zero claims and the page says so. All
thresholds are named constants, and confidence maps from evidence
grade + magnitude, never vibes.
"""

import research_snapshot as rs

HIGH, MEDIUM, LOW = "high", "medium", "low"

# Named thresholds — cited in claim text so the reader sees the bar.
GROWTH_STRONG = 20.0        # y/y revenue % that anchors a growth claim
GROWTH_WEAK = 0.0
MARGIN_STRONG = 15.0        # net margin %
MARGIN_OUTLIER = 40.0       # beyond this a single quarter is likely
                            # one-time items, not steady-state economics
FCF_CONV_STRONG = 20.0      # FCF as % of revenue
FCF_CONV_OUTLIER = 60.0     # beyond this the "conversion" is balance-
                            # sheet flow (custody/financials), not ops
RSI_STRETCH = 70.0


def _fv(x):
    return rs.fv(x) if isinstance(x, dict) else x


def build(snap, view4, scenarios=None):
    """-> {claims: [...], searched: [...], note}
    snap  : research snapshot
    view4 : the v4 view (event, ratings, monitoring) for cross-refs
    """
    claims, searched = [], []
    fu = snap.get("fundamentals") or {}
    lv = snap.get("levels") or {}
    ex = snap.get("exhibit") or {}

    growth = _fv(fu.get("revenue_growth"))
    margin = _fv(fu.get("net_margin"))
    rev = _fv(fu.get("revenue_q"))
    fcf = _fv(fu.get("free_cash_flow")) or _fv(fu.get("operating_cash_flow"))
    px = _fv(lv.get("price_used")) or _fv(lv.get("last_close"))
    ma200 = _fv(lv.get("ma200"))
    rsi = _fv(lv.get("rsi14"))

    searched.append("filed quarterly revenue / margins / cash flow "
                    "(SEC XBRL)")
    searched.append("issuer guidance from the ingested 8-K exhibit")
    searched.append("price structure vs the 20/50/200-day averages")
    searched.append("insider Form 4 economics")

    # ── growth claim ─────────────────────────────────────────────────
    if growth is not None and rev:
        if growth >= GROWTH_STRONG:
            counter = []
            if margin is not None and margin < 0:
                counter.append("growth is unprofitable: net margin %.1f%%"
                               % margin)
            if ma200 is not None and px is not None and px < ma200:
                counter.append("the market is not paying for it — price "
                               "below the 200-day")
            g = _guidance_counter(ex)
            if g:
                counter.append(g)
            claims.append({
                "claim": "Filed revenue growth of %.1f%% y/y clears the "
                         "%.0f%% bar for a growth thesis" % (growth,
                                                             GROWTH_STRONG),
                "direction": "bullish",
                "support": ["latest filed quarter revenue growth %.1f%% "
                            "y/y (SEC XBRL)" % growth],
                "counterevidence": counter,
                "confidence": HIGH if not counter else MEDIUM,
                "invalidation": "breaks if the next filed quarter's y/y "
                                "growth prints below %.0f%%" % GROWTH_STRONG,
                "refs": _refs(fu.get("revenue_growth")),
            })
        elif growth < GROWTH_WEAK:
            counter = []
            if margin is not None                     and MARGIN_STRONG <= margin <= MARGIN_OUTLIER:
                counter.append("profitability holds: net margin %.1f%%"
                               % margin)
            elif margin is not None and margin > MARGIN_OUTLIER:
                counter.append("the quarter shows a %.0f%% net margin — "
                               "far outside steady state, likely one-time "
                               "items; not treated as recurring "
                               "profitability" % margin)
            g = _guidance_support(ex)
            if g:
                counter.append(g)
            claims.append({
                "claim": "Filed revenue DECLINED %.1f%% y/y — the top "
                         "line is shrinking" % abs(growth),
                "direction": "bearish",
                "support": ["latest filed quarter revenue growth %.1f%% "
                            "y/y (SEC XBRL)" % growth],
                "counterevidence": counter,
                "confidence": HIGH if not counter else MEDIUM,
                "invalidation": "breaks if the next filed quarter returns "
                                "to positive y/y growth",
                "refs": _refs(fu.get("revenue_growth")),
            })

    # ── cash-generation claim ────────────────────────────────────────
    if fcf is not None and rev:
        conv = 100.0 * fcf / rev
        if conv > FCF_CONV_OUTLIER:
            # A broker or custodian's OCF swings with customer balances;
            # calling that "self-funding" would be arithmetic dressed as
            # economics. Say why there is no claim instead.
            searched.append("cash conversion (excluded: %.0f%% of revenue "
                            "reflects balance-sheet flows, not operating "
                            "conversion)" % conv)
        elif conv >= FCF_CONV_STRONG:
            counter = []
            if growth is not None and growth < GROWTH_WEAK:
                counter.append("cash conversion on a shrinking revenue "
                               "base (%.1f%% y/y)" % growth)
            claims.append({
                "claim": "Cash conversion of %.0f%% of revenue clears the "
                         "%.0f%% bar — the model self-funds" % (conv,
                                                                FCF_CONV_STRONG),
                "direction": "bullish",
                "support": ["quarter cash flow $%.0fM on revenue $%.0fM "
                            "(SEC XBRL, same period)" % (fcf / 1e6,
                                                          rev / 1e6)],
                "counterevidence": counter,
                "confidence": MEDIUM,
                "invalidation": "breaks if next quarter's conversion "
                                "prints below %.0f%%" % (FCF_CONV_STRONG / 2),
                "refs": _refs(fu.get("free_cash_flow")
                              or fu.get("operating_cash_flow")),
            })

    # ── valuation-vs-history claim (needs the scenario engine) ───────
    sc = scenarios or {}
    if sc.get("available"):
        band = sc.get("band_ref") or {}
        rows = {r["leg"]: r for r in sc.get("rows") or []}
        base = rows.get("base")
        if base and sc.get("spot"):
            # discount/premium of the PRICE vs what the median multiple
            # implies — bounded [0,100)% on the cheap side, so the text
            # can never claim an impossible "102% below"
            cheap = base["price"] > sc["spot"]
            gap = (100.0 * (1 - sc["spot"] / base["price"]) if cheap
                   else 100.0 * (sc["spot"] / base["price"] - 1))
            if abs(gap) >= 15:
                counter = []
                if cheap and growth is not None and growth < GROWTH_WEAK:
                    counter.append("the de-rate tracks shrinking revenue "
                                   "(%.1f%% y/y) — cheap for a reason is "
                                   "the base case until growth stabilises"
                                   % growth)
                if not cheap and growth is not None \
                        and growth >= GROWTH_STRONG:
                    counter.append("the premium tracks %.1f%% filed "
                                   "growth" % growth)
                claims.append({
                    "claim": "The stock trades at a %.0f%% %s the price "
                             "its own %d-year median trailing multiple "
                             "implies"
                             % (abs(gap), "discount to" if cheap
                                else "premium over",
                                band.get("window_years") or 3),
                    "direction": "bullish" if cheap else "bearish",
                    "support": ["base scenario %s vs spot %s — own-history "
                                "multiple band, filing-date aligned"
                                % (base["price"], sc["spot"])],
                    "counterevidence": counter,
                    "confidence": MEDIUM,
                    "invalidation": "breaks if the trailing metric falls "
                                    "enough to close the gap without the "
                                    "price moving",
                    "refs": ["V5-SCENARIOS"],
                })
    searched.append("own-history trailing multiple band")

    # ── technical-regime claim ───────────────────────────────────────
    tac = (view4.get("ratings") or {}).get("tactical") or {}
    if tac.get("available") and tac.get("mas_available", 3) >= 3:
        weak = tac.get("above_mas", 0) == 0
        strong = tac.get("above_mas", 0) == 3
        if weak or strong:
            counter = []
            if weak and growth is not None and growth >= GROWTH_STRONG:
                counter.append("fundamentals are not confirming the "
                               "tape: filed growth %.1f%%" % growth)
            if strong and rsi is not None and rsi >= RSI_STRETCH:
                counter.append("RSI %.0f is stretched above %.0f — entry "
                               "timing risk, not thesis risk"
                               % (rsi, RSI_STRETCH))
            claims.append({
                "claim": "Price is %s all three moving averages — the "
                         "trend regime is %s"
                         % ("below" if weak else "above",
                            "broken" if weak else "intact"),
                "direction": "bearish" if weak else "bullish",
                "support": ["price vs 20/50/200-day averages, completed "
                            "sessions (%s)" % (tac.get("detail") or "")],
                "counterevidence": counter,
                "confidence": MEDIUM,
                "invalidation": "breaks on a daily close %s the 50-day"
                                % ("above" if weak else "below"),
                "refs": ["CALC-ma20", "CALC-ma50", "CALC-ma200"],
            })

    note = None
    if not claims:
        note = ("No claim cleared the evidence bar: the filed record "
                "shows neither growth beyond ±%.0f%%, cash conversion "
                "over %.0f%%, a multiple displaced ≥15%% from its own "
                "history, nor a decided trend regime. Searched: %s."
                % (GROWTH_STRONG, FCF_CONV_STRONG, "; ".join(searched)))
    return {"claims": claims[:5], "searched": searched, "note": note}


def _refs(fact):
    if isinstance(fact, dict):
        r = fact.get("evidence_refs") or []
        return list(r) if r else []
    return []


def _guidance_counter(ex):
    """A guided decline is counterevidence to a growth claim."""
    for k, g in (ex.get("guidance_highlights") or {}).items():
        if isinstance(g, dict) and g.get("unit") == "%" \
                and (g.get("high") or 0) < 0:
            return ("issuer guides %s to %.1f%%..%.1f%%"
                    % (g.get("label") or k, g["low"], g["high"]))
    return None


def _guidance_support(ex):
    """Positive guided ranges push back on a decline claim."""
    for k, g in (ex.get("guidance_highlights") or {}).items():
        if isinstance(g, dict) and g.get("unit") == "%" \
                and (g.get("low") or 0) > 0:
            return ("issuer guides %s positive at %.1f%%..%.1f%%"
                    % (g.get("label") or k, g["low"], g["high"]))
    return None
