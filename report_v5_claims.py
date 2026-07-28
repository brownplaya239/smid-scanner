#!/usr/bin/env python3
"""report_v5_claims.py — argument engine, contract v2 (v5.5 phase B).

Every claim is a full argument object:

    claim_id, claim, claim_type, thesis_role, horizon,
    market_expectation, market_expectation_source, differentiated_view,
    mechanism, evidence_refs, counterevidence_refs,
    financial_implication, valuation_implication, catalyst, breaks_if,
    confidence, evidence_grade, freshness, status,
    thesis_horizon, expected_recognition_window, thesis_type,
    next_checkpoint, reunderwrite_when, maximum_valid_until

Statuses: SUPPORTED / PARTIALLY_SUPPORTED / CONFLICTED / STALE /
NOT_ESTABLISHED.

PUBLICATION GATE — a claim renders only when ALL hold:
  * >= 2 admitted evidence references
  * a causal mechanism (fact -> mechanism -> KPI -> earnings ->
    valuation)
  * counterevidence, or the explicit statement of its absence with the
    domains searched
  * a financial or valuation implication
  * a measurable invalidation condition
  * no stale critical evidence (latest filed quarter within
    STALE_DAYS)
  * "variant"/"misperception" language only with a SOURCED market
    expectation — otherwise the claim is typed a business insight

A name whose candidates all fail produces ZERO published claims plus
the per-candidate failed-gate explanations — never a manufactured
argument. Generation is pattern-based over admitted facts; no company
conclusions live in templates and no ticker identity is consulted.
"""

from datetime import datetime, timedelta, timezone

import research_snapshot as rs

HIGH, MEDIUM, LOW = "high", "medium", "low"
SUPPORTED = "SUPPORTED"
PARTIAL = "PARTIALLY_SUPPORTED"
CONFLICTED = "CONFLICTED"
STALE = "STALE"
NOT_ESTABLISHED = "NOT_ESTABLISHED"

# thesis types (spec vocabulary)
COMPOUNDER = "COMPOUNDER"
EXPECTATIONS_GAP = "EXPECTATIONS_GAP"
TACTICAL = "TACTICAL"
TYPE_NOT_ESTABLISHED = "NOT_ESTABLISHED"

# Named thresholds, cited in claim text.
GROWTH_STRONG = 20.0
GROWTH_WEAK = 0.0
MARGIN_STRONG = 15.0
MARGIN_OUTLIER = 40.0
FCF_CONV_STRONG = 20.0
FCF_CONV_OUTLIER = 60.0
RSI_STRETCH = 70.0
STALE_DAYS = 135          # a filed quarter older than this is stale
                          # evidence for a fundamental claim


def _fv(x):
    return rs.fv(x) if isinstance(x, dict) else x


def _refs(fact):
    if isinstance(fact, dict):
        return list(fact.get("evidence_refs") or [])
    return []


def _freshness(period_end, today):
    if not period_end:
        return {"basis_date": None, "age_days": None, "stale": True}
    try:
        d = datetime.strptime(str(period_end)[:10], "%Y-%m-%d").date()
    except ValueError:
        return {"basis_date": None, "age_days": None, "stale": True}
    age = (today - d).days
    return {"basis_date": str(period_end)[:10], "age_days": age,
            "stale": age > STALE_DAYS}


def _gate(cand):
    """-> (published: bool, failed: [reasons]). The publication gate."""
    failed = []
    if len(cand.get("evidence_refs") or []) < 2:
        failed.append("fewer than 2 admitted evidence references (%d)"
                      % len(cand.get("evidence_refs") or []))
    if not cand.get("mechanism"):
        failed.append("no causal mechanism")
    if cand.get("counterevidence_refs") is None:
        failed.append("counterevidence neither found nor explicitly "
                      "declared absent")
    if not (cand.get("financial_implication")
            or cand.get("valuation_implication")):
        failed.append("no financial or valuation implication")
    if not cand.get("breaks_if"):
        failed.append("no measurable invalidation condition")
    if (cand.get("freshness") or {}).get("stale") \
            and cand.get("claim_type") != "technical":
        failed.append("critical evidence is stale (basis %s, %s days old)"
                      % ((cand.get("freshness") or {}).get("basis_date"),
                         (cand.get("freshness") or {}).get("age_days")))
    if cand.get("market_expectation") \
            and not cand.get("market_expectation_source"):
        failed.append("expectation language without a sourced market "
                      "expectation")
    return (not failed), failed


def _status(cand, published):
    if not published:
        return STALE if any("stale" in f for f in cand.get(
            "_failed", [])) else NOT_ESTABLISHED
    ce = cand.get("counterevidence_refs") or []
    if not ce:
        return SUPPORTED
    if cand.get("_conflicted"):
        return CONFLICTED
    return PARTIAL


def _lifecycle(cat, today):
    """Shared horizon/checkpoint fields from the catalyst calendar."""
    nxt = (cat or {}).get("next_event_date") or (cat or {}).get("event_dt")
    nxt = str(nxt)[:10] if nxt else None
    return {
        "next_checkpoint": nxt or "next filed quarter",
        "reunderwrite_when": [
            "next 10-Q/10-K or earnings release",
            "guidance revision in a filed exhibit",
            "price crossing a scenario boundary",
            "invalidation condition breaching",
            "evidence passing %d days old" % STALE_DAYS,
        ],
        "maximum_valid_until": (today + timedelta(days=STALE_DAYS)
                                ).isoformat(),
    }


def build(snap, view4, scenarios=None, estimates=None):
    """-> {claims: [published], rejected: [{claim, failed_gates}],
           searched: [...], note}"""
    today = datetime.now(timezone.utc).date()
    fu = snap.get("fundamentals") or {}
    lv = snap.get("levels") or {}
    ex = snap.get("exhibit") or {}
    cat = snap.get("catalyst") or {}
    est = estimates or {}

    growth = _fv(fu.get("revenue_growth"))
    margin = _fv(fu.get("net_margin"))
    rev = _fv(fu.get("revenue_q"))
    fcf = _fv(fu.get("free_cash_flow")) or _fv(
        fu.get("operating_cash_flow"))
    px = _fv(lv.get("price_used")) or _fv(lv.get("last_close"))
    ma200 = _fv(lv.get("ma200"))
    rsi = _fv(lv.get("rsi14"))
    q_end = (fu.get("revenue_q") or {}).get("period_end") \
        if isinstance(fu.get("revenue_q"), dict) else None
    fresh_fund = _freshness(q_end, today)
    life = _lifecycle(cat, today)

    # The one sourced market-expectation proxy available pre-phase-C:
    # the dated consensus recommendation. It supports directional
    # expectation framing only — KPI-level expectations wait for the
    # expectations engine.
    rec = est.get("recommendation") or {}
    consensus_src = ("finnhub /stock/recommendation as of %s"
                     % rec.get("as_of")) if rec.get("as_of") else None
    consensus_view = ("consensus %s (%d analysts)"
                      % (rec.get("band"),
                         sum(rec.get(k, 0) for k in
                             ("strong_buy", "buy", "hold", "sell",
                              "strong_sell")))) if rec else None

    searched = [
        "filed quarterly revenue / margins / cash flow (SEC XBRL)",
        "issuer guidance from the ingested 8-K exhibit",
        "price structure vs the 20/50/200-day averages",
        "insider Form 4 economics",
        "dated consensus recommendation",
        "own-history trailing multiple band",
    ]
    cands = []

    # ── growth ───────────────────────────────────────────────────────
    if growth is not None and rev:
        if growth >= GROWTH_STRONG:
            ce, conflicted = [], False
            if margin is not None and margin < 0:
                ce.append("growth is unprofitable: net margin %.1f%%"
                          % margin)
            if ma200 is not None and px is not None and px < ma200:
                ce.append("the market is not paying for it — price below "
                          "the 200-day")
                conflicted = True
            g = _guidance_counter(ex)
            if g:
                ce.append(g)
            cands.append({
                "claim_id": "growth-above-bar",
                "claim": "Filed revenue growth of %.1f%% y/y clears the "
                         "%.0f%% bar for a growth thesis"
                         % (growth, GROWTH_STRONG),
                "claim_type": "fundamental",
                "thesis_role": "core",
                "direction": "bullish",
                "thesis_type": COMPOUNDER,
                "horizon": "3-5y underwriting; quarterly checkpoints",
                "thesis_horizon": "3-5y",
                "expected_recognition_window": "2-4 quarters of "
                                               "sustained prints",
                "market_expectation": consensus_view,
                "market_expectation_source": consensus_src,
                "differentiated_view": None,   # phase C decides gaps
                "mechanism": "revenue compounding at this rate grows the "
                             "earnings base faster than the multiple "
                             "de-rates in the base scenario",
                "support": ["latest filed quarter revenue growth %.1f%% "
                            "y/y (SEC XBRL)" % growth],
                "evidence_refs": _refs(fu.get("revenue_growth"))
                + _refs(fu.get("revenue_q")),
                "counterevidence": ce,
                "counterevidence_refs": ce if ce else [],
                "_conflicted": conflicted,
                "financial_implication": "sustaining >%.0f%% growth "
                                         "compounds the trailing metric "
                                         "the scenario table prices"
                                         % GROWTH_STRONG,
                "valuation_implication": "base-case multiple applies to "
                                         "a growing metric",
                "catalyst": life["next_checkpoint"],
                "breaks_if": "the next filed quarter's y/y growth prints "
                             "below %.0f%%" % GROWTH_STRONG,
                "confidence": HIGH if not ce else MEDIUM,
                "evidence_grade": "OBS",
                "freshness": fresh_fund,
            })
        elif growth < GROWTH_WEAK:
            ce = []
            if margin is not None \
                    and MARGIN_STRONG <= margin <= MARGIN_OUTLIER:
                ce.append("profitability holds: net margin %.1f%%"
                          % margin)
            elif margin is not None and margin > MARGIN_OUTLIER:
                ce.append("the quarter shows a %.0f%% net margin — far "
                          "outside steady state, likely one-time items; "
                          "not treated as recurring profitability"
                          % margin)
            g = _guidance_support(ex)
            if g:
                ce.append(g)
            cands.append({
                "claim_id": "revenue-decline",
                "claim": "Filed revenue DECLINED %.1f%% y/y — the top "
                         "line is shrinking" % abs(growth),
                "claim_type": "fundamental",
                "thesis_role": "core",
                "direction": "bearish",
                "thesis_type": TYPE_NOT_ESTABLISHED,
                "horizon": "2-4 quarters",
                "thesis_horizon": "1y",
                "expected_recognition_window": "next 1-2 prints",
                "market_expectation": consensus_view,
                "market_expectation_source": consensus_src,
                "differentiated_view": None,
                "mechanism": "a shrinking revenue base compresses the "
                             "trailing metric every scenario leg is "
                             "priced on",
                "support": ["latest filed quarter revenue growth %.1f%% "
                            "y/y (SEC XBRL)" % growth],
                "evidence_refs": _refs(fu.get("revenue_growth"))
                + _refs(fu.get("revenue_q")),
                "counterevidence": ce,
                "counterevidence_refs": ce if ce else [],
                "financial_implication": "each further quarter of "
                                         "decline lowers the trailing "
                                         "metric under the scenario "
                                         "multiples",
                "valuation_implication": "cheap-vs-history readings "
                                         "shrink as the metric falls",
                "catalyst": life["next_checkpoint"],
                "breaks_if": "the next filed quarter returns to positive "
                             "y/y growth",
                "confidence": HIGH if not ce else MEDIUM,
                "evidence_grade": "OBS",
                "freshness": fresh_fund,
            })

    # ── cash generation ──────────────────────────────────────────────
    if fcf is not None and rev:
        conv = 100.0 * fcf / rev
        if conv > FCF_CONV_OUTLIER:
            searched.append("cash conversion (excluded: %.0f%% of "
                            "revenue reflects balance-sheet flows, not "
                            "operating conversion)" % conv)
        elif conv >= FCF_CONV_STRONG:
            ce = []
            if growth is not None and growth < GROWTH_WEAK:
                ce.append("cash conversion on a shrinking revenue base "
                          "(%.1f%% y/y)" % growth)
            cands.append({
                "claim_id": "cash-conversion",
                "claim": "Cash conversion of %.0f%% of revenue clears "
                         "the %.0f%% bar — the model self-funds"
                         % (conv, FCF_CONV_STRONG),
                "claim_type": "fundamental",
                "thesis_role": "supporting",
                "direction": "bullish",
                "thesis_type": COMPOUNDER,
                "horizon": "3-5y underwriting",
                "thesis_horizon": "3-5y",
                "expected_recognition_window": "cumulative — no single "
                                               "print recognises it",
                "market_expectation": consensus_view,
                "market_expectation_source": consensus_src,
                "differentiated_view": None,
                "mechanism": "internally funded growth avoids dilution "
                             "and debt, so per-share economics track "
                             "business economics",
                "support": ["quarter cash flow $%.0fM on revenue $%.0fM "
                            "(SEC XBRL, same period)"
                            % (fcf / 1e6, rev / 1e6)],
                "evidence_refs": _refs(fu.get("free_cash_flow")
                                       or fu.get("operating_cash_flow"))
                + _refs(fu.get("revenue_q")),
                "counterevidence": ce,
                "counterevidence_refs": ce if ce else [],
                "financial_implication": "FCF accrues to the share "
                                         "count the trailing metric is "
                                         "measured over",
                "valuation_implication": "supports FCF-based cross-"
                                         "checks of the multiple band",
                "catalyst": life["next_checkpoint"],
                "breaks_if": "next quarter's conversion prints below "
                             "%.0f%%" % (FCF_CONV_STRONG / 2),
                "confidence": MEDIUM,
                "evidence_grade": "OBS",
                "freshness": fresh_fund,
            })

    # ── valuation vs own history ─────────────────────────────────────
    sc = scenarios or {}
    if sc.get("available"):
        band = sc.get("band_ref") or {}
        rows = {r["leg"]: r for r in sc.get("rows") or []}
        base = rows.get("base")
        if base and sc.get("spot"):
            cheap = base["price"] > sc["spot"]
            gap = (100.0 * (1 - sc["spot"] / base["price"]) if cheap
                   else 100.0 * (sc["spot"] / base["price"] - 1))
            if abs(gap) >= 15:
                ce = []
                if cheap and growth is not None and growth < GROWTH_WEAK:
                    ce.append("the de-rate tracks shrinking revenue "
                              "(%.1f%% y/y) — cheap for a reason is the "
                              "base case until growth stabilises"
                              % growth)
                if not cheap and growth is not None \
                        and growth >= GROWTH_STRONG:
                    ce.append("the premium tracks %.1f%% filed growth"
                              % growth)
                cands.append({
                    "claim_id": "valuation-vs-history",
                    "claim": "The stock trades at a %.0f%% %s the price "
                             "its own %d-year median trailing multiple "
                             "implies"
                             % (abs(gap), "discount to" if cheap
                                else "premium over",
                                band.get("window_years") or 3),
                    "claim_type": "valuation",
                    "thesis_role": "core",
                    "direction": "bullish" if cheap else "bearish",
                    "thesis_type": TYPE_NOT_ESTABLISHED,
                    "horizon": "re-rating window unknowable; band is "
                               "descriptive",
                    "thesis_horizon": "1-3y",
                    "expected_recognition_window": "not established",
                    "market_expectation": consensus_view,
                    "market_expectation_source": consensus_src,
                    "differentiated_view": None,
                    "mechanism": "mean reversion toward the name's own "
                                 "multiple distribution, if the metric "
                                 "holds",
                    "support": ["base scenario $%.2f vs spot $%.2f — "
                                "own-history band, filing-date aligned"
                                % (base["price"], sc["spot"])],
                    "evidence_refs": ["V5-BAND-%s" % (band.get("kind")
                                                      or "pe"),
                                      "V5-SCENARIO-BASE"],
                    "counterevidence": ce,
                    "counterevidence_refs": ce if ce else [],
                    "financial_implication": "none claimed — this is a "
                                             "pricing observation, not "
                                             "an earnings driver",
                    "valuation_implication": "%.0f%% %s to the base "
                                             "scenario at an unchanged "
                                             "metric"
                                             % (abs(100.0 * (
                                                 base["price"]
                                                 / sc["spot"] - 1)),
                                                "upside" if cheap
                                                else "downside"),
                    "catalyst": life["next_checkpoint"],
                    "breaks_if": "the trailing metric falls enough to "
                                 "close the gap without the price "
                                 "moving",
                    "confidence": MEDIUM,
                    "evidence_grade": "DER",
                    "freshness": _freshness(band.get("window_end"),
                                            today),
                })

    # ── technical regime ─────────────────────────────────────────────
    tac = (view4.get("ratings") or {}).get("tactical") or {}
    if tac.get("available") and tac.get("mas_available", 3) >= 3:
        weak = tac.get("above_mas", 0) == 0
        strong = tac.get("above_mas", 0) == 3
        if weak or strong:
            ce = []
            if weak and growth is not None and growth >= GROWTH_STRONG:
                ce.append("fundamentals are not confirming the tape: "
                          "filed growth %.1f%%" % growth)
            if strong and rsi is not None and rsi >= RSI_STRETCH:
                ce.append("RSI %.0f is stretched above %.0f — entry "
                          "timing risk, not thesis risk"
                          % (rsi, RSI_STRETCH))
            cands.append({
                "claim_id": "trend-regime",
                "claim": "Price is %s all three moving averages — the "
                         "trend regime is %s"
                         % ("below" if weak else "above",
                            "broken" if weak else "intact"),
                "claim_type": "technical",
                "thesis_role": "context",
                "direction": "bearish" if weak else "bullish",
                "thesis_type": TACTICAL,
                "horizon": "swing (2-8 weeks)",
                "thesis_horizon": "2-8w",
                "expected_recognition_window": "continuous",
                "market_expectation": None,
                "market_expectation_source": None,
                "differentiated_view": None,
                "mechanism": "trend regimes persist more often than "
                             "they reverse over the swing window; the "
                             "read is context for entries, never a "
                             "fundamental claim",
                "support": ["price vs 20/50/200-day averages, completed "
                            "sessions (%s)" % (tac.get("detail") or "")],
                "evidence_refs": ["CALC-ma20", "CALC-ma50", "CALC-ma200"],
                "counterevidence": ce,
                "counterevidence_refs": ce if ce else [],
                "financial_implication": "none — tactical context only",
                "valuation_implication": "affects entry, not value",
                "catalyst": "daily closes vs the 50-day",
                "breaks_if": "a daily close %s the 50-day"
                             % ("above" if weak else "below"),
                "confidence": MEDIUM,
                "evidence_grade": "DER",
                "freshness": {"basis_date": None, "age_days": 0,
                              "stale": False},
            })

    # ── the gate ─────────────────────────────────────────────────────
    published, rejected = [], []
    for c in cands:
        # explicit-absence handling: an empty list means "searched and
        # found none" and satisfies the gate; None means never searched
        ok, failed = _gate(c)
        c["_failed"] = failed
        c["status"] = _status(c, ok)
        c.update({k: life[k] for k in ("next_checkpoint",
                                       "reunderwrite_when",
                                       "maximum_valid_until")})
        c.pop("_conflicted", None)
        if ok:
            published.append(c)
        else:
            rejected.append({"claim_id": c["claim_id"],
                             "claim": c["claim"],
                             "status": c["status"],
                             "failed_gates": failed})
    for c in published + rejected:
        c.pop("_failed", None)

    note = None
    if not published:
        note = ("No claim cleared the publication gate. Candidates and "
                "their failed gates are listed; the filed record was "
                "searched across: %s." % "; ".join(searched))
    return {"claims": published[:5], "rejected": rejected,
            "searched": searched, "note": note,
            "schema": "v5-claims/2"}


def _guidance_counter(ex):
    for k, g in (ex.get("guidance_highlights") or {}).items():
        if isinstance(g, dict) and g.get("unit") == "%" \
                and (g.get("high") or 0) < 0:
            return ("issuer guides %s to %.1f%%..%.1f%%"
                    % (g.get("label") or k, g["low"], g["high"]))
    return None


def _guidance_support(ex):
    for k, g in (ex.get("guidance_highlights") or {}).items():
        if isinstance(g, dict) and g.get("unit") == "%" \
                and (g.get("low") or 0) > 0:
            return ("issuer guides %s positive at %.1f%%..%.1f%%"
                    % (g.get("label") or k, g["low"], g["high"]))
    return None
