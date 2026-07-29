#!/usr/bin/env python3
"""report_v5_adapters.py — sector / business-model adapters (v5.6 §7).

An adapter is selected from the ADMITTED vendor sector/industry
classification and the security's own profile — never from a ticker.
It contributes three things:

  * a KPI dashboard SPEC: the metrics an institutional reader of this
    business model expects, with human labels. Slots whose data has no
    admitted source render "no admitted source" with the reason — a
    restaurant never borrows software metrics and a broker's operating
    cash flow is never graded like an industrial's.
  * cash-conversion validity: financial platforms carry customer and
    balance-sheet flows through operating cash flow, so the industrial
    conversion read is explicitly disabled.
  * one-time-item identification: a universal XBRL scan for disposal /
    divestiture gains overlapping the latest filed quarter, so an
    outlier margin is attributed to its named filed cause instead of an
    anonymous "one-time item".

Missing sector data degrades gracefully: the slot stays, labelled
absent — the report's structure changes, not its honesty.
"""

SCHEMA = "v5-adapter/1"

# Slot kinds: "fact" (a fundamentals fact key), "absent" (a sector KPI
# with no ingestion path yet — always rendered as not admitted).
_GENERIC_SLOTS = [
    {"label": "Quarterly revenue", "kind": "fact", "key": "revenue_q",
     "fmt": "money"},
    {"label": "Revenue growth (y/y)", "kind": "fact",
     "key": "revenue_growth", "fmt": "pct"},
    {"label": "Gross margin", "kind": "fact", "key": "gross_margin",
     "fmt": "pct"},
    {"label": "Net margin", "kind": "fact", "key": "net_margin",
     "fmt": "pct"},
    {"label": "Quarterly cash flow", "kind": "fact",
     "key": "free_cash_flow", "alt_key": "operating_cash_flow",
     "fmt": "money"},
    {"label": "Cash / debt", "kind": "fact2", "key": "cash",
     "key2": "debt", "fmt": "money"},
]


def _absent(label, why):
    return {"label": label, "kind": "absent", "why": why}


# §2 (v5.7): the adapter governs the whole analytical pipeline, not
# just dashboard labels. Each policy names what the sector's economics
# permit; anything forbidden is suppressed to NOT_ASSESSED — never
# graded with another sector's logic.
#   quality_metrics_forbidden : business-quality dimensions that are
#       economically meaningless for the model (a bank's "revenue
#       growth", a REIT's industrial cash conversion, ...)
#   claims_forbidden          : claim ids the argument builder must not
#       generate for this model
#   valuation_allowed         : multiple kinds usable as the range
#       anchor; empty means no supportable method is ingested yet
#   valuation_reason          : stated when the allowed set is reduced
_POLICY_DEFAULT = {
    "quality_metrics_forbidden": (),
    "claims_forbidden": (),
    "valuation_allowed": ("pe", "ps"),
    "valuation_reason": None,
}

_POLICIES = {
    "bank_insurer": {
        "quality_metrics_forbidden": ("revenue_growth", "gross_margin",
                                      "cash_conversion", "net_cash"),
        "claims_forbidden": ("growth-above-bar", "revenue-decline",
                             "cash-conversion"),
        "valuation_allowed": ("pe",),
        "valuation_reason": "banks/insurers are graded on earnings and "
                            "book value; revenue multiples and "
                            "industrial cash-conversion logic do not "
                            "apply, and book-value methods are not "
                            "ingested yet",
        # §3 (v5.8): a bank's top line is net revenue (interest +
        # non-interest), never a generic contract-revenue tag — the
        # grid's revenue stream must come from these concepts or stay
        # suppressed
        "revenue_concepts": ("RevenuesNetOfInterestExpense",
                             "InterestAndDividendIncomeOperating",
                             "Revenues"),
    },
    "reit": {
        "quality_metrics_forbidden": ("cash_conversion", "net_cash",
                                      "gross_margin"),
        "claims_forbidden": ("cash-conversion",),
        "valuation_allowed": (),
        "valuation_reason": "REIT valuation requires FFO/AFFO, which "
                            "is not ingested — P/E on depreciation-"
                            "distorted GAAP earnings is not a "
                            "supportable primary method",
    },
    "financial_platform": {
        "quality_metrics_forbidden": ("cash_conversion",),
        "claims_forbidden": ("cash-conversion",),
    },
    "pre_revenue": {
        "quality_metrics_forbidden": ("revenue_growth", "net_margin",
                                      "gross_margin", "cash_conversion"),
        "claims_forbidden": ("growth-above-bar", "revenue-decline",
                             "cash-conversion", "valuation-vs-history"),
        "valuation_allowed": (),
        "valuation_reason": "no revenue or earnings base exists to "
                            "support a multiple",
    },
}

# §1 (v5.8): the restrictions a PRE_REVENUE business stage adds ON TOP
# of whatever sector adapter was selected — stage is an overlay, never
# the selector. A development-stage biotech keeps the biotech adapter
# (its dashboard, its KPIs) and gains these restrictions; missing data
# never triggers them.
_PRE_REVENUE_OVERLAY = {
    "quality_metrics_forbidden": ("revenue_growth", "net_margin",
                                  "gross_margin", "cash_conversion"),
    "claims_forbidden": ("growth-above-bar", "revenue-decline",
                         "cash-conversion", "valuation-vs-history"),
    "valuation_allowed": (),
    "valuation_reason": "no commercial revenue or earnings base exists "
                        "to support a multiple (development-stage "
                        "issuer)",
}


def policy_for(key):
    p = dict(_POLICY_DEFAULT)
    p.update(_POLICIES.get(key) or {})
    return p


_KPI_WHY = "requires segment/KPI exhibit parsing — not implemented"
_PROXY_WHY = "requires proxy (DEF 14A) parsing — not implemented"

ADAPTERS = {
    "subscription_software": {
        "label": "Subscription software",
        "cash_conversion_industrial_valid": True,
        "slots": _GENERIC_SLOTS + [
            _absent("Subscription revenue (vs total)", _KPI_WHY),
            _absent("cRPO / RPO", _KPI_WHY),
            _absent("Net-new ACV and large-deal activity", _KPI_WHY),
            _absent("Renewal / retention rate", _KPI_WHY),
            _absent("AI ACV", _KPI_WHY),
            _absent("SBC and dilution", _PROXY_WHY),
        ],
        "notes": ["Free-cash-flow conversion and constant-currency "
                  "guided growth are the decision metrics for this "
                  "model."],
    },
    "financial_platform": {
        "label": "Financial platform / brokerage",
        "cash_conversion_industrial_valid": False,
        "slots": _GENERIC_SLOTS + [
            _absent("Transaction-based revenue mix", _KPI_WHY),
            _absent("Net interest revenue", _KPI_WHY),
            _absent("Funded customers / investment accounts", _KPI_WHY),
            _absent("Platform assets and net deposits", _KPI_WHY),
            _absent("ARPU and subscription (premium-tier) members",
                    _KPI_WHY),
            _absent("Trading volumes and margin balances", _KPI_WHY),
            _absent("Regulatory capital", _KPI_WHY),
        ],
        "notes": ["Operating cash flow carries customer and "
                  "balance-sheet flows in this model; it is NOT an "
                  "industrial cash-conversion measure and is not "
                  "graded as one.",
                  "Interest-rate sensitivity and event-driven volume "
                  "exposure are structural to the revenue mix."],
    },
    "restaurant": {
        "label": "Restaurant / multi-unit consumer",
        "cash_conversion_industrial_valid": True,
        "slots": _GENERIC_SLOTS + [
            _absent("Same-store sales / traffic / ticket", _KPI_WHY),
            _absent("Average unit volume (AUV)", _KPI_WHY),
            _absent("Restaurant count and openings", _KPI_WHY),
            _absent("Restaurant-level profit margin", _KPI_WHY),
            _absent("Digital mix", _KPI_WHY),
            _absent("New-unit investment and payback", _KPI_WHY),
        ],
        "notes": ["Unit-level economics (AUV, restaurant-level margin, "
                  "payback) decide this model; consolidated margins "
                  "blend corporate overhead into them.",
                  "One-time gains and impairments must be attributed "
                  "before any margin is graded."],
    },
    "bank_insurer": {
        "label": "Bank / insurer",
        "cash_conversion_industrial_valid": False,
        "slots": [
            {"label": "Quarterly revenue", "kind": "fact",
             "key": "revenue_q", "fmt": "money"},
            {"label": "Net margin", "kind": "fact", "key": "net_margin",
             "fmt": "pct"},
            _absent("Net interest margin", _KPI_WHY),
            _absent("Provision for credit losses", _KPI_WHY),
            _absent("Book value and regulatory capital ratios",
                    _KPI_WHY),
            _absent("Combined ratio (insurers)", _KPI_WHY),
        ],
        "notes": ["Revenue growth and cash-flow conversion are not the "
                  "decision metrics for a balance-sheet business; book "
                  "value, capital and credit costs are — none are "
                  "ingested yet."],
    },
    "reit": {
        "label": "REIT / real estate",
        "cash_conversion_industrial_valid": False,
        "slots": [
            {"label": "Quarterly revenue", "kind": "fact",
             "key": "revenue_q", "fmt": "money"},
            {"label": "Cash / debt", "kind": "fact2", "key": "cash",
             "key2": "debt", "fmt": "money"},
            _absent("FFO / AFFO per share", _KPI_WHY),
            _absent("Occupancy and same-store NOI", _KPI_WHY),
            _absent("Cap rates and debt maturity ladder", _KPI_WHY),
        ],
        "notes": ["GAAP net income understates a REIT through "
                  "depreciation; FFO/AFFO are the decision metrics and "
                  "are not ingested yet."],
    },
    "energy_materials": {
        "label": "Energy / materials",
        "cash_conversion_industrial_valid": True,
        "slots": _GENERIC_SLOTS + [
            _absent("Production volumes and realized prices", _KPI_WHY),
            _absent("Reserves and finding costs", _KPI_WHY),
            _absent("Unit cash costs", _KPI_WHY),
        ],
        "notes": ["Commodity price exposure makes trailing multiples "
                  "cycle-dependent; the historical range is descriptive "
                  "only."],
    },
    "industrial": {
        "label": "Industrial",
        "cash_conversion_industrial_valid": True,
        "slots": _GENERIC_SLOTS + [
            _absent("Backlog and book-to-bill", _KPI_WHY),
            _absent("Segment margins", _KPI_WHY),
        ],
        "notes": [],
    },
    "biotech": {
        "label": "Biotechnology / drug development",
        "cash_conversion_industrial_valid": False,
        "slots": [
            {"label": "Cash / debt", "kind": "fact2", "key": "cash",
             "key2": "debt", "fmt": "money"},
            {"label": "Quarterly cash flow (burn)", "kind": "fact",
             "key": "operating_cash_flow", "fmt": "money"},
            {"label": "Collaboration / grant revenue", "kind": "fact",
             "key": "revenue_q", "fmt": "money"},
            _absent("Runway at current burn", "derivable only when "
                    "burn and cash are both admitted"),
            _absent("Clinical pipeline and trial milestones", _KPI_WHY),
            _absent("Partnership economics and milestone payments",
                    _KPI_WHY),
        ],
        "notes": ["Solvency, burn and clinical milestones decide a "
                  "development-stage biotech; any reported revenue is "
                  "collaboration or grant income unless a commercial "
                  "launch is filed."],
    },
    "pre_revenue": {
        "label": "Pre-revenue company",
        "cash_conversion_industrial_valid": True,
        "slots": [
            {"label": "Cash / debt", "kind": "fact2", "key": "cash",
             "key2": "debt", "fmt": "money"},
            {"label": "Quarterly cash flow (burn)", "kind": "fact",
             "key": "operating_cash_flow", "fmt": "money"},
            _absent("Runway at current burn", "derivable only when "
                    "burn and cash are both admitted"),
            _absent("Milestones to first revenue", _KPI_WHY),
        ],
        "notes": ["No revenue-based metric applies; solvency, burn and "
                  "milestone progress are the whole dashboard."],
    },
    "new_listing": {
        "label": "New listing",
        "cash_conversion_industrial_valid": True,
        "slots": [],
        "notes": ["No filed periodic history: the fact sheet replaces "
                  "the dashboard."],
    },
    "generic": {
        "label": "General operating company",
        "cash_conversion_industrial_valid": True,
        "slots": _GENERIC_SLOTS,
        "notes": [],
    },
}


def classify(profile, snap, archetype=None, stage=None):
    """Adapter selection from admitted classification facts only.
    Returns the adapter record + the classification reason.

    §1 (v5.8): the SECTOR selects the adapter; the business STAGE (an
    established fact from report_v5_classify, never inferred from
    missing data) is applied as a policy overlay on top. An issuer
    whose revenue our ingestion failed to parse keeps its sector
    adapter with NOT_ASSESSED metrics — it is never re-imagined as a
    pre-revenue company."""
    sector = (profile or {}).get("sector") or ""
    biz = snap.get("business") or {}
    ov = (snap.get("company") or {}).get("overview") or {}
    industry = (profile or {}).get("industry") or ov.get("industry") \
        or biz.get("industry") or ""
    s, i = sector.lower(), industry.lower()

    if archetype == "NEW_LISTING":
        key, why = "new_listing", "archetype NEW_LISTING: no filed " \
            "periodic history"
    elif "biotech" in i or ("drug manufacturers" in i
                            and stage == "PRE_REVENUE"):
        key, why = "biotech", "vendor industry %r" % industry
    elif "restaurant" in i:
        key, why = "restaurant", "vendor industry %r" % industry
    elif "reit" in i or s == "real estate":
        key, why = "reit", "vendor classification %r / %r" % (sector,
                                                              industry)
    elif "bank" in i or "insurance" in i:
        key, why = "bank_insurer", "vendor industry %r" % industry
    elif s == "financial services" or "capital markets" in i \
            or "financial data" in i or "broker" in i \
            or "exchange" in i:
        key, why = "financial_platform", \
            "vendor classification %r / %r" % (sector, industry)
    elif "software" in i or "information technology services" in i:
        key, why = "subscription_software", "vendor industry %r" \
            % industry
    elif s in ("energy", "basic materials"):
        key, why = "energy_materials", "vendor sector %r" % sector
    elif s == "industrials":
        key, why = "industrial", "vendor sector %r" % sector
    else:
        key, why = "generic", ("no sector adapter matches vendor "
                               "classification %r / %r — generic "
                               "dashboard, nothing borrowed"
                               % (sector, industry))

    # §1 (v5.8) stage overlay: an ESTABLISHED pre-revenue stage narrows
    # the policy on top of the sector adapter. A generic pre-revenue
    # issuer (no sector family) gets the dedicated pre_revenue
    # dashboard; a sector-classified one keeps its sector dashboard.
    stage_applied = False
    pol = policy_for(key)
    if stage == "PRE_REVENUE" and key not in ("new_listing",
                                              "pre_revenue"):
        if key == "generic":
            key = "pre_revenue"
            why += " — established pre-revenue stage selects the " \
                   "pre-revenue dashboard"
            pol = policy_for(key)
        else:
            merged = dict(pol)
            for f in ("quality_metrics_forbidden", "claims_forbidden"):
                merged[f] = tuple(sorted(
                    set(pol.get(f) or ())
                    | set(_PRE_REVENUE_OVERLAY[f])))
            merged["valuation_allowed"] = ()
            merged["valuation_reason"] = \
                _PRE_REVENUE_OVERLAY["valuation_reason"]
            pol = merged
            why += " — established pre-revenue stage restricts the " \
                   "policy (no revenue-based grading or valuation)"
        stage_applied = True

    a = ADAPTERS[key]
    return {"schema": SCHEMA, "key": key, "label": a["label"],
            "reason": why, "sector": sector or None,
            "industry": industry or None,
            "business_stage": stage,
            "stage_policy_applied": stage_applied,
            "cash_conversion_industrial_valid":
                a["cash_conversion_industrial_valid"],
            "policy": pol,
            "notes": list(a["notes"])}


# ── one-time (disposal) gain identification — universal XBRL scan ────

_DISPOSAL_TAGS = (
    "GainLossOnDispositionOfBusiness",
    "GainLossOnSaleOfBusiness",
    "DisposalGroupNotDiscontinuedOperationGainLossOnDisposal",
    "GainLossOnDispositionOfAssets1",
    "GainLossOnDispositionOfAssets",
    "GainLossOnSaleOfOtherAssets",
)


def one_time_items(ticker, snap):
    """Named one-time items overlapping the latest filed quarter, from
    the issuer's own XBRL. Returns [] when none — never a guess."""
    import research_snapshot as rs
    fu = snap.get("fundamentals") or {}
    rq = fu.get("revenue_q") if isinstance(fu.get("revenue_q"), dict) \
        else None
    if not rq or not rq.get("period_end"):
        return []
    period_end = str(rq["period_end"])[:10]
    rev = rs.fv(rq)
    out = []
    try:
        import research_live as RL
        cik = RL.cik_for_filed(ticker)[0]
        for tag in _DISPOSAL_TAGS:
            try:
                rows = RL.concept(cik, tag)
            except Exception:
                rows = []
            for r in rows or []:
                if str(r.get("end") or "")[:10] != period_end:
                    continue
                val = r.get("val")
                if val is None or not rev \
                        or abs(val) < 0.05 * abs(rev):
                    continue
                out.append({
                    "label": "$%.1fM gain on disposal of a business/"
                             "asset (us-gaap:%s, period ended %s)"
                             % (val / 1e6, tag, period_end),
                    "concept": tag, "value": val,
                    "period_end": period_end,
                    "accession": r.get("accn"),
                    "evidence_refs": ["XBRL-%s-us-gaap:%s-%s"
                                      % (r.get("accn") or "na", tag,
                                         period_end)],
                })
                break            # one entry per concept is enough
    except Exception:
        return out
    return out


def build_dashboard(adapter, snap, grid=None):
    """-> rows for the sector KPI dashboard: [label, value-or-absent,
    provenance]. Values come only from admitted facts; absent slots keep
    their sector-correct label with the reason.

    v5.8 review fix: every displayed value is PERIOD-QUALIFIED and
    CURRENT. The revenue slot consumes the grid's validated,
    adapter-aware quarterly stream (a bank shows its net-revenue
    quarter, never a dead generic contract-revenue tag), and any fact
    older than the recency floor renders its staleness instead of its
    value — the dashboard can never show a 2014 quarter as if it were
    the latest."""
    import report_v5_checks as _CK
    import research_snapshot as rs
    from datetime import date, timedelta
    fu = snap.get("fundamentals") or {}
    _floor = (date.today() - timedelta(
        days=_CK.BS_LATEST_MAX_AGE_DAYS)).isoformat()
    _lq = (grid or {}).get("ttm", {}).get("latest_quarter") \
        if isinstance(grid, dict) else None

    def fv(key):
        f = fu.get(key)
        return rs.fv(f) if isinstance(f, dict) else None

    def fperiod(key):
        return _CK._fact_period(fu.get(key))

    def fmt(v, kind):
        if v is None:
            return None
        if kind == "pct":
            return "%.1f%%" % v
        a = abs(v)
        if a >= 1e9:
            return "$%.2fB" % (v / 1e9)
        if a >= 1e6:
            return "$%.1fM" % (v / 1e6)
        return "$%.2f" % v

    rows = []
    spec = ADAPTERS[adapter["key"]]["slots"]
    for slot in spec:
        if slot["kind"] == "fact":
            key = slot["key"]
            v, p = fv(key), fperiod(key)
            if v is None and slot.get("alt_key"):
                v, p = fv(slot["alt_key"]), fperiod(slot["alt_key"])
            # the revenue slot follows the grid's validated
            # adapter-aware stream when it is fresher than (or as
            # fresh as) the snapshot fact
            if key == "revenue_q" and _lq \
                    and (p is None or _lq["end"] >= p):
                rows.append([slot["label"],
                             "%s (as of %s)" % (fmt(_lq["value"],
                                                    "money"),
                                                _lq["end"]),
                             "filed (SEC XBRL, %s)" % _lq["concept"]])
                continue
            if v is None:
                rows.append([slot["label"], "no admitted source",
                             "not filed / not parsed"])
            elif p and p < _floor:
                # a stale fact renders its staleness, never its value
                rows.append([slot["label"],
                             "not current — last filed under this "
                             "concept %s" % p,
                             "concept no longer reported by the "
                             "issuer"])
            else:
                rows.append([slot["label"],
                             "%s (as of %s)" % (fmt(v, slot["fmt"]),
                                                p or "n/a"),
                             "filed (SEC XBRL)"])
        elif slot["kind"] == "fact2":
            # §2 (v5.8): paired balance-sheet instants render only from
            # one reporting date, each value dated; incompatible
            # periods render the canonical not-assessed sentence
            import report_v5_checks as _CK
            f1 = fu.get(slot["key"])
            f2 = fu.get(slot.get("key2"))
            v1, v2 = fv(slot["key"]), fv(slot.get("key2"))
            p1 = _CK._fact_period(f1)
            p2 = _CK._fact_period(f2)
            if v1 is None and v2 is None:
                rows.append([slot["label"], "no admitted source",
                             "not filed / not parsed"])
            elif v1 is not None and v2 is not None:
                pair = _CK.balance_sheet_pairing(
                    {"cash": f1, "debt": f2})
                if not pair["ok"]:
                    rows.append([slot["label"],
                                 _CK.BS_NOT_ASSESSED_MSG,
                                 "instants dated %s vs %s — different "
                                 "reporting dates, not comparable"
                                 % (p1, p2)])
                elif max(p for p in (p1, p2) if p) < _floor:
                    rows.append([slot["label"],
                                 "not current — instants last filed "
                                 "%s" % max(p for p in (p1, p2) if p),
                                 "concepts no longer reported by the "
                                 "issuer"])
                else:
                    rows.append([slot["label"],
                                 "%s (as of %s) / %s (as of %s)"
                                 % (fmt(v1, "money"), p1 or "n/a",
                                    fmt(v2, "money"), p2 or "n/a"),
                                 "filed (SEC XBRL)"])
            else:
                only_v = v1 if v1 is not None else v2
                only_p = p1 if v1 is not None else p2
                only_n = "cash" if v1 is not None else "debt"
                rows.append([slot["label"],
                             "%s %s (as of %s) / other side not "
                             "admitted" % (only_n,
                                           fmt(only_v, "money"),
                                           only_p or "n/a"),
                             "filed (SEC XBRL)"])
        else:
            rows.append([slot["label"], "no admitted source",
                         slot["why"]])
    return rows
