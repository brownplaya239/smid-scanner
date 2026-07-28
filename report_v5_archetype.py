#!/usr/bin/env python3
"""report_v5_archetype.py — archetype contracts + router (v5 slice 3).

THE DESIGN PRINCIPLE THIS MODULE ENFORCES
    Absence of evidence changes the report's SHAPE, not the number of
    blank fields. A six-week-old listing gets a listing fact sheet — a
    different document — not a mansion of "n/a". Each archetype declares
    which sections are REQUIRED, OPTIONAL and FORBIDDEN, and the
    validator holds the rendered document to that contract from both
    sides: a NEW_LISTING with a scenario table fails exactly as hard as
    a FULL report without one.

The router decides from snapshot facts only, and RECORDS its decision:
{archetype, reasons[], override} lands in the view and in the
validation JSON. A manual override is possible but never invisible —
it renders as a WARN in validation.
"""

FULL = "FULL"
FULL_THIN = "FULL_THIN"
THIN = "THIN"
NEW_LISTING = "NEW_LISTING"
DATA_HOLD = "DATA_HOLD"

ARCHETYPES = (FULL, FULL_THIN, THIN, NEW_LISTING, DATA_HOLD)

# Section vocabulary. Presentation renders sections; validation checks
# the rendered document against this contract by archetype.
SECTIONS = ("dashboard", "scenario_table", "argument", "financial_grid",
            "valuation_detail", "event_path", "technicals",
            "flow_positioning", "variant_risks", "listing_factsheet",
            "listing_timeline", "listing_trading", "flash")

CONTRACTS = {
    FULL: {
        "required": ("dashboard", "argument", "financial_grid",
                     "event_path", "technicals", "variant_risks"),
        # scenario_table required only when a band survived coverage —
        # the router refines this per name (see decide()).
        "optional": ("scenario_table", "valuation_detail",
                     "flow_positioning"),
        "forbidden": ("listing_factsheet", "listing_timeline",
                      "listing_trading", "flash"),
        "pages": (5, 8),
    },
    # FULL_THIN (§6): financially complete but framework-incomplete —
    # the quantitative record is strong while industry structure,
    # management, moat, unit economics or expectations remain
    # NOT_ASSESSED. Same page family as FULL; the label and the
    # framework-coverage display say what has NOT been underwritten.
    FULL_THIN: {
        "required": ("dashboard", "argument", "financial_grid",
                     "variant_risks"),
        "optional": ("scenario_table", "valuation_detail", "event_path",
                     "technicals", "flow_positioning"),
        "forbidden": ("listing_factsheet", "listing_timeline",
                      "listing_trading", "flash"),
        "pages": (4, 8),
    },
    THIN: {
        "required": ("dashboard", "argument", "financial_grid",
                     "variant_risks"),
        "optional": ("scenario_table", "valuation_detail", "event_path",
                     "technicals"),
        "forbidden": ("flow_positioning", "listing_factsheet",
                      "listing_timeline", "listing_trading", "flash"),
        "pages": (3, 5),
    },
    NEW_LISTING: {
        "required": ("listing_factsheet", "listing_timeline",
                     "listing_trading"),
        "optional": (),
        "forbidden": ("scenario_table", "argument", "financial_grid",
                      "valuation_detail", "flow_positioning", "flash"),
        "pages": (2, 4),
    },
    DATA_HOLD: {
        "required": ("flash",),
        "optional": (),
        "forbidden": tuple(s for s in SECTIONS if s != "flash"),
        "pages": (1, 1),
    },
}

# Thresholds, named so the reasons can cite them.
MIN_FULL_SESSIONS = 200        # a year of trading before FULL applies
MIN_FILED_QUARTERS = 4         # a filed year of quarters


def decide(snap, event, multiples=None, has_options=None, override=None):
    """-> {archetype, reasons[], contract, override}

    snap      : the research snapshot (trading_history, fundamentals)
    event     : the resolved event-gate record (state, flash)
    multiples : slice-1 record, if computed (refines scenario_table)
    has_options : bool|None — whether a listed chain exists
    override  : force an archetype (recorded, WARNed, never silent)
    """
    reasons = []
    th = snap.get("trading_history") or {}
    fu = snap.get("fundamentals") or {}

    # 1. DATA HOLD outranks everything: the gate already decided no
    #    report ships while a fresh release is unread.
    if (event or {}).get("flash"):
        decision = DATA_HOLD
        reasons.append("event gate is holding: %s"
                       % ((event.get("reasons") or ["fresh release "
                          "not ingested"])[-1]))
    else:
        sessions = th.get("sessions") or 0
        listed = th.get("listing_date")
        filed_q = _filed_quarters(fu, multiples)

        if listed and not th.get("full_history") \
                and sessions < MIN_FULL_SESSIONS and filed_q == 0:
            decision = NEW_LISTING
            reasons.append("listed %s with %d sessions and no filed "
                           "periodic report — a listing fact sheet, not "
                           "an equity research note, is the honest shape"
                           % (listed, sessions))
        elif filed_q < MIN_FILED_QUARTERS or sessions < MIN_FULL_SESSIONS:
            decision = THIN
            if filed_q < MIN_FILED_QUARTERS:
                reasons.append("only %d filed quarter(s) — under the %d a "
                               "full note needs" % (filed_q,
                                                    MIN_FILED_QUARTERS))
            if sessions < MIN_FULL_SESSIONS:
                reasons.append("%d trading sessions — under the %d a full "
                               "technical read needs"
                               % (sessions, MIN_FULL_SESSIONS))
        else:
            decision = FULL
            reasons.append("%d filed quarters and %d sessions support the "
                           "full note" % (filed_q, sessions))

    # 2. Per-name contract refinements, with reasons.
    contract = {k: list(v) if isinstance(v, tuple) else v
                for k, v in CONTRACTS[decision].items()}
    if decision in (FULL, THIN):
        band_ok = any((multiples or {}).get(k, {}).get("available")
                      for k in ("pe", "ps"))
        if band_ok:
            _promote(contract, "scenario_table")
            _promote(contract, "valuation_detail")
            reasons.append("a multiple band survived coverage — scenario "
                           "table required")
        else:
            reasons.append("no multiple band survived coverage — "
                           "valuation withheld, not faked")
        if decision == FULL and has_options is False:
            contract["optional"] = [s for s in contract["optional"]
                                    if s != "flow_positioning"]
            contract["forbidden"].append("flow_positioning")
            reasons.append("no listed options — flow page forbidden "
                           "rather than empty")

    rec = {"archetype": decision, "reasons": reasons,
           "contract": contract, "override": None}

    # 3. Override: allowed, recorded, WARNed by validation.
    if override and override != decision and override in ARCHETYPES:
        rec["override"] = {"from": decision, "to": override}
        rec["archetype"] = override
        rec["contract"] = {k: list(v) if isinstance(v, tuple) else v
                           for k, v in CONTRACTS[override].items()}
        rec["reasons"].append("OVERRIDDEN to %s by flag — router chose %s"
                              % (override, decision))
    return rec


def _promote(contract, section):
    if section in contract["optional"]:
        contract["optional"].remove(section)
        contract["required"].append(section)


def _filed_quarters(fu, multiples=None):
    """Distinct filed quarterly periods. The authoritative count comes
    from the multiples engine's point-in-time event streams (the same
    facts the financial grid renders); the snapshot's single latest-
    quarter fact only proves a floor of one."""
    m = multiples or {}
    n = max(m.get("n_eps_quarters") or 0, m.get("n_rev_quarters") or 0)
    f = fu.get("revenue_q")
    if n == 0 and isinstance(f, dict) and f.get("period_end"):
        n = 1
    return n


def check_rendered_sections(rendered, contract):
    """Validation hook: rendered is {section: bool}; returns the list of
    violations, empty when the document honours the contract."""
    bad = []
    for s in contract["required"]:
        if not rendered.get(s):
            bad.append("missing required section: %s" % s)
    for s in contract["forbidden"]:
        if rendered.get(s):
            bad.append("forbidden section rendered: %s" % s)
    return bad
