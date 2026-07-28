#!/usr/bin/env python3
"""report_v5_capability.py — CompanyProfile + EvidenceCapability routing
(v5.5 phase A).

THE UNIVERSAL-TICKER RULE THIS MODULE ENFORCES
    Routing depends on what the evidence CAN support, never on which
    ticker is being run. Pilot symbols exist only in fixtures and
    generated artifacts; test_v5_no_ticker_branches scans shared logic
    and fails the build if a pilot symbol leaks in.

CompanyProfile: descriptive facts about the security, all sourced from
admitted data (vendor profile, listing reference, filing history).
EvidenceCapability: per-category booleans-with-reasons recording whether
ADMITTED data is sufficient for each analysis the report might render.
route(): deterministic archetype selection from those two objects,
recording the categories present/absent and the full routing reason.
A manual override changes presentation but NEVER bypasses evidence
gates — forbidden sections stay forbidden if the capability is absent.
"""

from datetime import datetime, timezone

import report_v5_archetype as A

SCHEMA = "v5-capability/1"

# The seventeen evidence categories the spec names.
CATEGORIES = (
    "operating_history", "financial_statements", "cashflow_history",
    "balance_sheet_history", "segment_reporting", "company_guidance",
    "consensus_estimates", "historical_valuation", "peer_valuation",
    "management_assessment", "ownership", "insider_activity",
    "market_price_history", "technical_analysis", "catalysts",
    "valuation_range_construction", "expectations_analysis",
)


def company_profile(snap, multiples=None, sector=None, industry=None):
    """Descriptive facts only — no judgments, no ticker branches."""
    th = snap.get("trading_history") or {}
    biz = snap.get("business") or {}
    co = snap.get("company") or {}
    ov = co.get("overview") or {}
    m = multiples or {}
    nq = max(m.get("n_eps_quarters") or 0, m.get("n_rev_quarters") or 0)
    fu = snap.get("fundamentals") or {}
    import research_snapshot as rs
    rev = rs.fv(fu.get("revenue_q"))
    return {
        "schema": SCHEMA,
        "security_type": "common_equity",
        "listing_status": "listed",
        "listing_date": th.get("listing_date"),
        "reporting_history_quarters": nq,
        "sessions": th.get("sessions") or 0,
        "full_price_history": bool(th.get("full_history")),
        "sector": sector or co.get("sector") or biz.get("sector"),
        "industry": industry or ov.get("industry")
        or biz.get("industry"),
        "pre_revenue_status": rev is None and nq == 0,
        "business_model_tags": [],          # adapter phase populates
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def evidence_capability(snap, multiples=None, estimates=None,
                        has_options=None):
    """Per-category sufficiency, each with the reason — never a bare
    boolean, so the routing record can say WHY a category is absent."""
    import research_snapshot as rs
    th = snap.get("trading_history") or {}
    fu = snap.get("fundamentals") or {}
    m = multiples or {}
    est = estimates or {}
    ex = snap.get("exhibit") or {}
    cat = snap.get("catalyst") or {}
    ins = snap.get("insiders") or {}
    own = snap.get("ownership") or {}

    def has_fact(*keys):
        return any(isinstance(fu.get(k), dict)
                   and rs.fv(fu.get(k)) is not None for k in keys)

    nq = max(m.get("n_eps_quarters") or 0, m.get("n_rev_quarters") or 0)
    band_ok = any((m.get(k) or {}).get("available") for k in ("pe", "ps"))
    sessions = th.get("sessions") or 0

    caps = {}

    def put(name, ok, why):
        caps[name] = {"sufficient": bool(ok), "reason": why}

    put("operating_history", nq >= A.MIN_FILED_QUARTERS,
        "%d filed quarters (need %d)" % (nq, A.MIN_FILED_QUARTERS))
    put("financial_statements", has_fact("revenue_q", "net_income_q"),
        "latest-quarter filed facts %s" %
        ("admitted" if has_fact("revenue_q", "net_income_q") else "absent"))
    put("cashflow_history", has_fact("operating_cash_flow",
                                     "free_cash_flow"),
        "filed cash-flow facts %s" %
        ("admitted" if has_fact("operating_cash_flow", "free_cash_flow")
         else "absent"))
    put("balance_sheet_history", has_fact("cash", "debt"),
        "balance-sheet instants %s" %
        ("admitted" if has_fact("cash", "debt") else "absent"))
    put("segment_reporting", False,
        "segment ingestion not implemented — never inferred")
    put("company_guidance", ex.get("disposition") == "ADMITTED"
        and bool(ex.get("guidance_highlights")),
        "exhibit %s" % (ex.get("disposition") or "absent"))
    put("consensus_estimates", bool(est.get("recommendation")),
        "consensus %s" % ("dated %s" % (est.get("recommendation") or {}
                                        ).get("as_of")
                          if est.get("recommendation") else "absent"))
    put("historical_valuation", band_ok,
        "multiple band %s" % ("survived coverage" if band_ok else
                              "withheld"))
    put("peer_valuation", bool(((est or {}).get("peers") or {}).get("rows"))
        if isinstance(est.get("peers"), dict) else False,
        "vendor peer set %s" % ("admitted" if isinstance(
            est.get("peers"), dict) else "absent"))
    put("management_assessment", False,
        "no admitted management-record source — never inferred")
    put("ownership", bool(own), "ownership block %s"
        % ("present" if own else "absent"))
    put("insider_activity", bool(ins), "Form 4 rows %s"
        % ("parsed" if ins else "absent"))
    put("market_price_history", sessions >= A.MIN_FULL_SESSIONS,
        "%d sessions (need %d)" % (sessions, A.MIN_FULL_SESSIONS))
    put("technical_analysis", sessions >= 30,
        "%d sessions (chart floor 30)" % sessions)
    put("catalysts", bool(cat.get("event_dt")
                          or cat.get("next_event_date")),
        "catalyst discovery %s" % ("dated" if cat.get("event_dt")
                                   or cat.get("next_event_date")
                                   else "empty"))
    put("valuation_range_construction", band_ok,
        "requires a surviving multiple band")
    put("expectations_analysis",
        bool(est.get("recommendation"))
        or (ex.get("disposition") == "ADMITTED"
            and bool(ex.get("guidance_highlights"))),
        "needs sourced consensus or admitted guidance")
    return {"schema": SCHEMA, "categories": caps}


def route(profile, capability, event, multiples=None, has_options=None,
          override=None, override_author=None, override_reason=None,
          framework=None):
    """Deterministic archetype from capabilities AND framework coverage
    (§6). FULL is earned by diligence coverage — financial statements,
    price history and a multiple band alone route FULL_THIN, with the
    missing framework dimensions recorded."""
    caps = capability["categories"]

    def ok(name):
        return caps.get(name, {}).get("sufficient")

    missing_dims = list(((framework or {}).get("summary") or {})
                        .get("missing_for_full") or [])
    reasons = []
    if (event or {}).get("flash"):
        decision = A.DATA_HOLD
        reasons.append("event gate holding a fresh unread release")
    elif profile.get("listing_date") and not profile["full_price_history"] \
            and not ok("operating_history") \
            and not ok("financial_statements"):
        decision = A.NEW_LISTING
        reasons.append("listed %s: no filed periodic report and %d "
                       "sessions — insufficient public history for "
                       "normalized underwriting"
                       % (profile["listing_date"], profile["sessions"]))
    elif ok("operating_history") and ok("financial_statements") \
            and ok("market_price_history") and ok("historical_valuation"):
        if framework is None or not missing_dims:
            decision = A.FULL
            reasons.append("operating history, filed statements, price "
                           "history, valuation support and framework "
                           "coverage all sufficient")
        else:
            decision = A.FULL_THIN
            reasons.append("financial record sufficient, but FULL "
                           "requires underwritten framework coverage — "
                           "NOT_ASSESSED: %s" % ", ".join(missing_dims))
    else:
        decision = A.THIN
        missing = [n for n in ("operating_history", "financial_statements",
                               "market_price_history",
                               "historical_valuation") if not ok(n)]
        reasons.append("FULL requires %s — insufficient: %s"
                       % (", ".join(("operating history",
                                     "filed statements", "price history",
                                     "valuation support")),
                           ", ".join(missing)))

    # Contract from phase-3 machinery, refined by capabilities.
    contract = {k: list(v) if isinstance(v, tuple) else v
                for k, v in A.CONTRACTS[decision].items()}
    if decision in (A.FULL, A.FULL_THIN, A.THIN):
        if ok("valuation_range_construction"):
            A._promote(contract, "valuation_table")
            A._promote(contract, "valuation_detail")
            reasons.append("a multiple band survived coverage — the "
                           "valuation table is required (historical "
                           "percentiles; forward legs only when "
                           "underwritten)")
        else:
            reasons.append("valuation table stays optional: %s"
                           % caps["valuation_range_construction"]["reason"])
        if decision in (A.FULL, A.FULL_THIN):
            if ok("technical_analysis"):
                A._promote(contract, "technicals")
                A._promote(contract, "event_path")
            if has_options is False:
                if "flow_positioning" in contract["optional"]:
                    contract["optional"].remove("flow_positioning")
                contract["forbidden"].append("flow_positioning")
                reasons.append("no listed options — flow page forbidden")

    rec = {
        "schema": SCHEMA,
        "selected_archetype": decision,
        "archetype": decision,               # phase-3 compatibility
        "routing_reason": reasons[0],
        "reasons": reasons,
        "contract": contract,
        "categories_present": sorted(n for n in CATEGORIES
                                     if caps.get(n, {}).get("sufficient")),
        "categories_absent": sorted(n for n in CATEGORIES
                                    if not caps.get(n, {}).get(
                                        "sufficient")),
        "missing_framework_dimensions": missing_dims,
        "capability": capability,
        "override": None,
    }

    if override and override in A.ARCHETYPES and override != decision:
        # Presentation may change; evidence gates may NOT. The overridden
        # contract keeps every forbidden section whose capability is
        # absent, so an override can never conjure a scenario table out
        # of a name with no band.
        oc = {k: list(v) if isinstance(v, tuple) else v
              for k, v in A.CONTRACTS[override].items()}
        if not ok("valuation_range_construction"):
            for sec in ("valuation_table", "valuation_detail"):
                if sec in oc["required"]:
                    oc["required"].remove(sec)
                if sec not in oc["forbidden"] and sec not in oc["optional"]:
                    oc["optional"].append(sec)
        rec["override"] = {
            "from": decision, "to": override,
            "author": override_author or "unattributed",
            "timestamp": datetime.now(timezone.utc
                                      ).isoformat(timespec="seconds"),
            "reason": override_reason or "not stated",
        }
        rec["selected_archetype"] = rec["archetype"] = override
        rec["contract"] = oc
        rec["reasons"].append("OVERRIDDEN %s -> %s by %s: %s"
                              % (decision, override,
                                 rec["override"]["author"],
                                 rec["override"]["reason"]))
    return rec
