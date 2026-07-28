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


def classify(profile, snap, archetype=None):
    """Adapter selection from admitted classification facts only.
    Returns the adapter record + the classification reason."""
    sector = (profile or {}).get("sector") or ""
    biz = snap.get("business") or {}
    ov = (snap.get("company") or {}).get("overview") or {}
    industry = (profile or {}).get("industry") or ov.get("industry") \
        or biz.get("industry") or ""
    s, i = sector.lower(), industry.lower()

    if archetype == "NEW_LISTING":
        key, why = "new_listing", "archetype NEW_LISTING: no filed " \
            "periodic history"
    elif (profile or {}).get("pre_revenue_status"):
        key, why = "pre_revenue", "no filed revenue and no filed " \
            "quarters"
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
    a = ADAPTERS[key]
    return {"schema": SCHEMA, "key": key, "label": a["label"],
            "reason": why, "sector": sector or None,
            "industry": industry or None,
            "cash_conversion_industrial_valid":
                a["cash_conversion_industrial_valid"],
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
        cik = RL.cik_for(ticker)
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


def build_dashboard(adapter, snap):
    """-> rows for the sector KPI dashboard: [label, value-or-absent,
    provenance]. Values come only from admitted facts; absent slots keep
    their sector-correct label with the reason."""
    import research_snapshot as rs
    fu = snap.get("fundamentals") or {}

    def fv(key):
        f = fu.get(key)
        return rs.fv(f) if isinstance(f, dict) else None

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
            v = fv(slot["key"])
            if v is None and slot.get("alt_key"):
                v = fv(slot["alt_key"])
            rows.append([slot["label"],
                         fmt(v, slot["fmt"]) or "no admitted source",
                         "filed (SEC XBRL)" if v is not None
                         else "not filed / not parsed"])
        elif slot["kind"] == "fact2":
            v1, v2 = fv(slot["key"]), fv(slot.get("key2"))
            if v1 is None and v2 is None:
                rows.append([slot["label"], "no admitted source",
                             "not filed / not parsed"])
            else:
                rows.append([slot["label"], "%s / %s"
                             % (fmt(v1, "money") or "n/a",
                                fmt(v2, "money") or "n/a"),
                             "filed (SEC XBRL)"])
        else:
            rows.append([slot["label"], "no admitted source",
                         slot["why"]])
    return rows
