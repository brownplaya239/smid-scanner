#!/usr/bin/env python3
"""report_v5_framework.py — Tiger diligence matrix + Sundheim decision
object (v5.6 §4/§5).

FrameworkCoverage is the structural answer to "what has actually been
underwritten": twenty-six named diligence dimensions, each carrying a
status (UNDERWRITTEN / PARTIAL / NOT_ASSESSED / NOT_APPLICABLE), the
conclusion the admitted evidence supports, its refs, its unknowns and
the next evidence that would move it. Nothing here is inferred: a
dimension with no admitted source says NOT_ASSESSED and names what a
source would look like. The router consumes this object — FULL is
earned by coverage, not by the mere existence of financial statements.

The Sundheim decision object answers the standing question set from
sourced inputs only, and stores the underwriting decision fields the
spec names. Both objects are universal: they read capability categories,
claims, expectations and assessments — never a ticker identity.
"""

from datetime import datetime, timezone

SCHEMA = "v5-framework/1"

UNDERWRITTEN = "UNDERWRITTEN"
PARTIAL = "PARTIAL"
NOT_ASSESSED = "NOT_ASSESSED"
NOT_APPLICABLE = "NOT_APPLICABLE"
STATUSES = (UNDERWRITTEN, PARTIAL, NOT_ASSESSED, NOT_APPLICABLE)

TIGER_DIMENSIONS = (
    "industry_structure", "market_opportunity", "customer_power",
    "supplier_power", "competitor_power", "regulatory_power",
    "barriers_to_entry", "pricing_power", "business_model",
    "customer_value", "unit_economics", "organic_vs_acquired_growth",
    "management_record", "incentives_and_alignment",
    "culture_and_execution", "capital_allocation", "accounting_quality",
    "balance_sheet", "working_capital", "cash_conversion",
    "reinvestment_requirements", "earnings_quality", "valuation",
    "ownership_and_sentiment", "catalysts_and_timeline",
    "total_loss_risks",
)

# Human labels for reader-facing tables (no raw schema identifiers).
DIM_LABELS = {
    "industry_structure": "Industry structure",
    "market_opportunity": "Market opportunity",
    "customer_power": "Customer power",
    "supplier_power": "Supplier power",
    "competitor_power": "Competitor power",
    "regulatory_power": "Regulatory power",
    "barriers_to_entry": "Barriers to entry",
    "pricing_power": "Pricing power",
    "business_model": "Business model",
    "customer_value": "Customer value",
    "unit_economics": "Unit economics",
    "organic_vs_acquired_growth": "Organic vs acquired growth",
    "management_record": "Management record",
    "incentives_and_alignment": "Incentives and alignment",
    "culture_and_execution": "Culture and execution",
    "capital_allocation": "Capital allocation",
    "accounting_quality": "Accounting quality",
    "balance_sheet": "Balance sheet",
    "working_capital": "Working capital",
    "cash_conversion": "Cash conversion",
    "reinvestment_requirements": "Reinvestment requirements",
    "earnings_quality": "Earnings quality",
    "valuation": "Valuation",
    "ownership_and_sentiment": "Ownership and sentiment",
    "catalysts_and_timeline": "Catalysts and timeline",
    "total_loss_risks": "Total-loss risks",
}

# The dimensions FULL demands beyond financial sufficiency (§6): a FULL
# label without these is exactly the overreach the spec bans.
FULL_REQUIRED_DIMS = (
    "industry_structure", "business_model", "unit_economics",
    "management_record", "earnings_quality", "valuation",
    "catalysts_and_timeline",
)


def _dim(status, conclusion, ev=None, ce=None, confidence="low",
         freshness=None, unknowns=None, need=None):
    return {"status": status, "conclusion": conclusion,
            "evidence_refs": ev or [], "counterevidence_refs": ce or [],
            "confidence": confidence, "freshness": freshness,
            "material_unknowns": unknowns or [],
            "next_evidence_needed": need or []}


def _fv(x):
    import research_snapshot as rs
    return rs.fv(x) if isinstance(x, dict) else x


def _refs(fact):
    if isinstance(fact, dict):
        return list(fact.get("evidence_refs") or [])
    return []


def build_coverage(profile, capability, snap, grid=None, multiples=None,
                   adapter=None, claims=None, expectations=None,
                   assessment=None):
    """-> the FrameworkCoverage object. Filed facts carry the financial
    dimensions to PARTIAL at best (reported figures, not forensic
    review); qualitative dimensions stay NOT_ASSESSED until an admitted
    source exists — and each names the evidence that would change it."""
    caps = (capability or {}).get("categories") or {}
    fu = snap.get("fundamentals") or {}
    growth = _fv(fu.get("revenue_growth"))
    net_m = _fv(fu.get("net_margin"))
    rev = _fv(fu.get("revenue_q"))
    fcf = _fv(fu.get("free_cash_flow")) or _fv(
        fu.get("operating_cash_flow"))
    cash = _fv(fu.get("cash"))
    debt = _fv(fu.get("debt"))
    q_end = (fu.get("revenue_q") or {}).get("period_end") \
        if isinstance(fu.get("revenue_q"), dict) else None

    def cap_ok(name):
        return (caps.get(name) or {}).get("sufficient")

    def cap_why(name):
        return (caps.get(name) or {}).get("reason") or "not evaluated"

    ad = adapter or {}
    one_time = (ad.get("one_time_items") or [])
    d = {}

    # ── qualitative dimensions: honest NOT_ASSESSED with named needs ──
    NEEDS = {
        "industry_structure": ["industry study or expert/channel input",
                               "competitor filings comparison"],
        "market_opportunity": ["sized TAM from a filed or licensed "
                               "source"],
        "customer_power": ["customer-concentration disclosure from "
                           "10-K risk factors"],
        "supplier_power": ["supplier-concentration disclosure"],
        "competitor_power": ["named-competitor share data"],
        "regulatory_power": ["regulatory-exposure disclosures parsed "
                             "from filings"],
        "barriers_to_entry": ["moat evidence: retention, switching "
                              "costs, unit-cost advantage"],
        "pricing_power": ["like-for-like price/mix disclosure across "
                          "periods"],
        "customer_value": ["retention/NPS/cohort disclosure"],
        "unit_economics": ["per-unit revenue and cost disclosure "
                           "(segment or KPI exhibit parsing)"],
        "organic_vs_acquired_growth": ["acquisition contribution "
                                       "disclosure from filings"],
        "management_record": ["tenure and prior-outcome record from an "
                              "admitted source"],
        "incentives_and_alignment": ["proxy (DEF 14A) compensation "
                                     "parsing"],
        "culture_and_execution": ["guidance-vs-delivery history over "
                                  "multiple periods"],
        "capital_allocation": ["multi-year buyback/M&A/capex record "
                               "with returns"],
        "working_capital": ["receivables/payables/inventory series "
                            "ingestion"],
        "reinvestment_requirements": ["maintenance-vs-growth capex "
                                      "split"],
    }
    for k, needs in NEEDS.items():
        d[k] = _dim(NOT_ASSESSED,
                    "no admitted source for this dimension; it is not "
                    "inferred", need=needs,
                    unknowns=["the dimension itself"])

    # business model: the adapter classification is a vendor-profile
    # fact, so the dimension reaches PARTIAL — classification without
    # underwritten economics.
    if ad.get("key"):
        d["business_model"] = _dim(
            PARTIAL,
            "classified as %s from the vendor sector/industry profile; "
            "revenue-model economics not underwritten" % (ad.get("label")
                                                          or ad["key"]),
            ev=["REC-profile"], confidence="low",
            unknowns=["revenue mix by stream", "durability of the model"],
            need=["segment/KPI disclosures parsed from filings"])
    else:
        d["business_model"] = _dim(
            NOT_ASSESSED, "no vendor or filed classification admitted",
            need=["company profile or filed segment data"])

    # ── financial dimensions: filed facts, PARTIAL by design ─────────
    d["accounting_quality"] = _dim(
        PARTIAL if cap_ok("financial_statements") else NOT_ASSESSED,
        ("as-first-reported XBRL facts parsed; screens limited to "
         "outlier margins and one-time items%s"
         % ("; one-time item identified: %s"
            % "; ".join(o["label"] for o in one_time)
            if one_time else "")) if cap_ok("financial_statements")
        else cap_why("financial_statements"),
        ev=_refs(fu.get("net_income_q")) or _refs(fu.get("revenue_q")),
        confidence="medium" if cap_ok("financial_statements") else "low",
        freshness=q_end,
        unknowns=["revenue-recognition policy detail",
                  "non-GAAP adjustment quality"],
        need=["full accounting-policy review of the 10-K"])
    # §2 (v5.8): balance-sheet instants join only from one reporting
    # date; each displayed value carries its own period_end, and an
    # incompatible pairing is NOT_ASSESSED with the canonical sentence
    import report_v5_checks as _CK
    _pair = _CK.balance_sheet_pairing(fu)
    _cp = _CK._fact_period(fu.get("cash"))
    _dp = _CK._fact_period(fu.get("debt"))
    # §2 (v5.8): a stale instant can DESCRIBE history but never support
    # a current-position grade — an insurer whose generic cash/debt
    # concepts died years ago is NOT_ASSESSED, not graded on relics
    from datetime import date as _date, timedelta as _td
    _bs_stale_limit = (_date.today() - _td(
        days=_CK.BS_LATEST_MAX_AGE_DAYS)).isoformat()
    _newest_instant = max((p for p in (_cp, _dp) if p), default=None)
    if _newest_instant and _newest_instant < _bs_stale_limit:
        _stale_msg = ("balance-sheet instants last filed under these "
                      "concepts on %s — too stale to support a current "
                      "position read" % _newest_instant)
        d["balance_sheet"] = _dim(
            NOT_ASSESSED, _stale_msg,
            ev=_refs(fu.get("cash")) + _refs(fu.get("debt")),
            confidence="low", freshness=_newest_instant,
            unknowns=["current balance-sheet position"],
            need=["parsing of the issuer's current balance-sheet "
                  "concepts"])
        d["total_loss_risks"] = _dim(
            NOT_ASSESSED, _stale_msg,
            ev=_refs(fu.get("cash")) + _refs(fu.get("debt")),
            confidence="low", freshness=_newest_instant,
            unknowns=["covenants", "contingent liabilities",
                      "dilution capacity"],
            need=["debt-agreement and legal-proceedings parsing"])
        cash = debt = None       # downstream reads never grade relics
        _bs_done = True
    else:
        _bs_done = False
    if _bs_done:
        pass                     # stale branch already graded both dims
    elif cash is not None and debt is not None and not _pair["ok"]:
        d["balance_sheet"] = _dim(
            NOT_ASSESSED,
            "%s (cash instant %s vs debt instant %s)"
            % (_CK.BS_NOT_ASSESSED_MSG, _cp, _dp),
            ev=_refs(fu.get("cash")) + _refs(fu.get("debt")),
            confidence="low", freshness=min(p for p in (_cp, _dp) if p),
            unknowns=["off-balance-sheet commitments",
                      "debt maturities"],
            need=["a filed balance sheet carrying both instants at one "
                  "reporting date"])
    else:
        _bs_concl = None
        if cash is not None and debt is not None:
            _bs_concl = ("cash %s (as of %s) vs debt %s (as of %s), "
                         "same reporting basis"
                         % ("$%.1fB" % (cash / 1e9), _cp or "n/a",
                            "$%.1fB" % (debt / 1e9), _dp or "n/a"))
        elif cash is not None or debt is not None:
            _v, _p, _n = ((cash, _cp, "cash") if cash is not None
                          else (debt, _dp, "debt"))
            _bs_concl = ("%s %s (as of %s); the other instant was not "
                         "admitted" % (_n, "$%.1fB" % (_v / 1e9),
                                       _p or "n/a"))
        d["balance_sheet"] = _dim(
            PARTIAL if (cap_ok("balance_sheet_history") and _bs_concl)
            else NOT_ASSESSED,
            _bs_concl if (cap_ok("balance_sheet_history") and _bs_concl)
            else cap_why("balance_sheet_history"),
            ev=_refs(fu.get("cash")) + _refs(fu.get("debt")),
            confidence="medium" if cap_ok("balance_sheet_history")
            else "low",
            freshness=min((p for p in (_cp, _dp) if p), default=q_end),
            unknowns=["off-balance-sheet commitments",
                      "debt maturities"],
            need=["debt-footnote parsing"])
    conv_note = None
    if ad.get("cash_conversion_industrial_valid") is False:
        conv_note = ("operating cash flow is distorted by customer and "
                     "balance-sheet flows in this business model — it is "
                     "not an industrial cash-conversion measure")
    d["cash_conversion"] = _dim(
        PARTIAL if cap_ok("cashflow_history") else NOT_ASSESSED,
        (conv_note or
         ("quarterly cash flow %s on revenue %s, filed statements"
          % ("$%.0fM" % (fcf / 1e6) if fcf is not None else "n/a",
             "$%.0fM" % (rev / 1e6) if rev is not None else "n/a")))
        if cap_ok("cashflow_history") else cap_why("cashflow_history"),
        ev=_refs(fu.get("free_cash_flow"))
        or _refs(fu.get("operating_cash_flow")),
        confidence="low" if conv_note else (
            "medium" if cap_ok("cashflow_history") else "low"),
        freshness=q_end,
        unknowns=(["split of customer flows vs operating flows"]
                  if conv_note else ["working-capital seasonality"]),
        need=["multi-year cash-flow decomposition"])
    d["earnings_quality"] = _dim(
        PARTIAL if cap_ok("financial_statements") else NOT_ASSESSED,
        ("net margin %s%s"
         % ("%.1f%%" % net_m if net_m is not None else "n/a",
            "; includes %s — excluded from quality"
            % "; ".join(o["label"] for o in one_time) if one_time
            else "")) if cap_ok("financial_statements")
        else cap_why("financial_statements"),
        ev=_refs(fu.get("net_margin")),
        confidence="medium" if cap_ok("financial_statements") else "low",
        freshness=q_end,
        unknowns=["stock-compensation add-back quality",
                  "accrual vs cash earnings gap"],
        need=["normalized-earnings bridge"])
    band_ok = any(((multiples or {}).get(k) or {}).get("available")
                  for k in ("pe", "ps"))
    d["valuation"] = _dim(
        PARTIAL if band_ok else NOT_ASSESSED,
        "own-history trailing multiple band, filing-date aligned — "
        "descriptive range, no forward valuation underwritten"
        if band_ok else cap_why("historical_valuation"),
        ev=(["V5-BAND-pe"] if ((multiples or {}).get("pe")
                               or {}).get("available")
            else ["V5-BAND-ps"] if band_ok else []),
        confidence="medium" if band_ok else "low",
        unknowns=["forward operating trajectory"],
        need=["operating forecasts via an admitted assumptions file"])
    d["ownership_and_sentiment"] = _dim(
        PARTIAL if (cap_ok("ownership") or cap_ok("insider_activity"))
        else NOT_ASSESSED,
        "13D/13G and Form 4 records parsed"
        if (cap_ok("ownership") or cap_ok("insider_activity"))
        else "no ownership or insider records in the window",
        confidence="low",
        unknowns=["full institutional holder base (13F not parsed)"],
        need=["13F aggregation"])
    d["catalysts_and_timeline"] = _dim(
        PARTIAL if cap_ok("catalysts") else NOT_ASSESSED,
        cap_why("catalysts"), confidence="medium"
        if cap_ok("catalysts") else "low",
        unknowns=["issuer-confirmed date vs vendor estimate"],
        need=["issuer press release confirming the date"])
    # §2 (v5.8): a net-cash / net-debt read is a comparison — it needs
    # both instants from one reporting date
    _tlr_ok = cash is not None and debt is not None and _pair["ok"]
    d["total_loss_risks"] = _dim(
        PARTIAL if _tlr_ok else NOT_ASSESSED,
        ("near-term solvency observable from the balance sheet dated "
         "%s (%s)" % (_cp or _dp or "n/a",
                      "net cash" if (cash or 0) > (debt or 0)
                      else "net debt")) if _tlr_ok
        else (("%s (cash instant %s vs debt instant %s)"
               % (_CK.BS_NOT_ASSESSED_MSG, _cp, _dp))
              if cash is not None and debt is not None
              else "no balance-sheet instants admitted"),
        ev=_refs(fu.get("cash")) + _refs(fu.get("debt")),
        confidence="low",
        freshness=min((p for p in (_cp, _dp) if p), default=q_end),
        unknowns=["covenants", "contingent liabilities", "dilution "
                  "capacity"],
        need=["debt-agreement and legal-proceedings parsing"])

    counts = {s: 0 for s in STATUSES}
    for k in TIGER_DIMENSIONS:
        counts[d[k]["status"]] += 1
    missing_for_full = [k for k in FULL_REQUIRED_DIMS
                        if d[k]["status"] == NOT_ASSESSED]
    unanswered = []
    for k in TIGER_DIMENSIONS:
        if d[k]["status"] == NOT_ASSESSED:
            for need in d[k]["next_evidence_needed"][:1]:
                unanswered.append("%s: %s" % (DIM_LABELS[k], need))
    return {"schema": SCHEMA,
            "as_of": datetime.now(timezone.utc
                                  ).isoformat(timespec="seconds"),
            "dimensions": d,
            "summary": {"counts": counts,
                        "assessed": counts[UNDERWRITTEN]
                        + counts[PARTIAL],
                        "total": len(TIGER_DIMENSIONS),
                        "missing_for_full": missing_for_full,
                        "unanswered": unanswered}}


# ── Sundheim decision object ─────────────────────────────────────────

def sundheim_decision(view5, coverage, archetype=None):
    """The standing question set, answered only from sourced inputs, plus
    the stored decision fields. Questions without an admitted source
    answer 'not established' — the honest state, never filler."""
    cl = view5.get("claims") or {}
    pubs = cl.get("claims") or []
    exp = view5.get("expectations") or {}
    asx = view5.get("assessment") or {}
    bq = asx.get("business_quality") or {}
    ia = asx.get("investment_attractiveness") or {}
    sc = view5.get("scenarios") or {}
    var = exp.get("variant") or {}
    arch = archetype or (view5.get("archetype") or {}).get("archetype")

    fund = [c for c in pubs if c.get("claim_type") in ("fundamental",
                                                       "valuation")]
    tact = [c for c in pubs if c.get("claim_type") == "technical"]
    growth_claim = next((c for c in fund
                         if c["claim_id"] == "growth-above-bar"), None)
    dims = (coverage or {}).get("dimensions") or {}

    def dim_c(name):
        return (dims.get(name) or {}).get("conclusion") or \
            "not established"

    if arch == "NEW_LISTING" or not fund:
        uw_status = "NOT_UNDERWRITTEN"
    else:
        uw_status = "PARTIALLY_UNDERWRITTEN"

    thesis_type = next((c.get("thesis_type") for c in fund
                        if c.get("thesis_type")
                        and c["thesis_type"] != "NOT_ESTABLISHED"),
                       "NOT_ESTABLISHED")
    horizon = next((c.get("thesis_horizon") for c in fund), None)

    momentum = "not established"
    if growth_claim:
        momentum = growth_claim["claim"]
    elif fund:
        momentum = fund[0]["claim"]

    downside = ("not underwritten — no downside case has been "
                "constructed; the historical range is descriptive "
                "context only")
    if sc.get("mode") == "underwritten":
        downside = "the underwritten bear scenario (assumptions file)"

    questions = [
        ("Is the product valuable to customers?", dim_c("customer_value")),
        ("Can the company compound over three to five years?",
         growth_claim["claim"] if growth_claim else
         "not established — no admitted evidence of durable compounding"),
        ("What drives unit economics?", dim_c("unit_economics")),
        ("What does normalized earnings power look like?",
         "not underwritten — no normalized-earnings bridge exists"),
        ("What does consensus or valuation imply?",
         exp.get("justify_price") or "no sourced implication computed"),
        ("Where does TickerDesk differ from expectations?",
         ("a %+.1f%% gap on %s (%s)" % (var["gap_pct"], var["metric"],
                                        var["source"]))
         if var.get("available") else
         "nowhere sourced — no variant view is held"),
        ("What is current operating momentum?", momentum),
        ("What is attractive or unattractive at today's price?",
         "; ".join(ia.get("reasons") or []) or "not established"),
        ("What is the downside if the thesis fails?", downside),
        ("What kind of thesis is this?",
         thesis_type.replace("_", " ").lower()),
        ("What changed since the prior underwriting?",
         "see the research-state change log"),
        ("What evidence would change the conclusion?",
         "; ".join((coverage or {}).get("summary", {}).get(
             "unanswered", [])[:3]) or "see framework coverage"),
    ]

    fund_inv = next((c["breaks_if"] for c in fund), None)
    tact_inv = next((c["breaks_if"] for c in tact), None)
    need = [u for u in (coverage or {}).get("summary", {}).get(
        "unanswered", [])][:5]
    return {
        "schema": "v5-sundheim/1",
        "underwriting_status": uw_status,
        "thesis_type": thesis_type,
        "thesis_horizon": horizon,
        "business_quality": bq.get("display") or bq.get("level"),
        "investment_attractiveness": ia.get("display") or ia.get("level"),
        "expected_recognition_window": next(
            (c.get("expected_recognition_window") for c in fund), None),
        "principal_uncertainty": (
            "qualitative coverage: %s"
            % "; ".join(DIM_LABELS.get(d, d.replace("_", " "))
                        for d in (coverage or {}).get(
                            "summary", {}).get("missing_for_full",
                                               [])[:3])
            if (coverage or {}).get("summary", {}).get("missing_for_full")
            else "forward operating trajectory"),
        "next_evidence_needed": need,
        "fundamental_invalidation": fund_inv,
        "tactical_invalidation": tact_inv,
        "reunderwrite_when": next((c.get("reunderwrite_when")
                                   for c in fund), None),
        "maximum_valid_until": next((c.get("maximum_valid_until")
                                     for c in pubs), None),
        "questions": [{"question": q, "answer": a} for q, a in questions],
    }
