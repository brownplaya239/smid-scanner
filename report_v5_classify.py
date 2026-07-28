#!/usr/bin/env python3
"""report_v5_classify.py — issuer classification (v5.8 §1).

Classification is a statement about the ISSUER, established from
admitted evidence; data availability is a statement about OUR ingestion.
v5.7 conflated them: an issuer whose revenue concept our tag shortlist
missed (XOM) or whose facts are filed under ifrs-full (SAP) routed
"pre_revenue", while an actual development-stage biotechnology company
(SANA) routed "generic" because it files a small collaboration-revenue
line. This module separates the two:

  business_stage      OPERATING | PRE_REVENUE | UNKNOWN
  accounting_regime   us-gaap | ifrs | unknown
  listing_type        domestic_listed | foreign_private_issuer |
                      new_listing
  sector_family       the vendor sector/industry family (adapter input)

Rules (each with the admitted evidence that establishes it):

* PRE_REVENUE requires ADMITTED evidence — either (a) the issuer's full
  SEC concept index contains facts but NO revenue-like concept in any
  taxonomy, or (b) a development-stage condition: the issuer's own filed
  facts show trailing losses that dwarf trailing revenue in a
  development-stage sector family (clinical-stage biotech filing token
  collaboration revenue). Missing or unparsed revenue NEVER implies
  PRE_REVENUE.
* IFRS / foreign-private-issuer status is an accounting regime, not a
  missing business: an ifrs-full Revenue concept is admitted evidence of
  an operating company even when no us-gaap fact parses.
* When nothing establishes the stage, the answer is UNKNOWN — rendered
  NOT_ASSESSED, never defaulted to a business model.

No ticker symbols, no company names, no hard-coded financial values —
thresholds are named universal policy constants.
"""

from datetime import datetime, timezone

SCHEMA = "v5-classification/1"

OPERATING = "OPERATING"
PRE_REVENUE = "PRE_REVENUE"
UNKNOWN = "UNKNOWN"

# Development-stage condition (rule b): trailing quarterly revenue below
# this fraction of the trailing quarterly net-loss magnitude, in a
# development-stage sector family, is the filed signature of a company
# whose reported revenue is collaboration/grant income rather than a
# commercial business. A commercial issuer's revenue always exceeds its
# losses by multiples; a clinical-stage issuer's losses exceed revenue
# by multiples.
DEV_STAGE_MAX_REV_TO_LOSS = 0.25

# Sector families in which the development-stage condition is a known,
# common corporate form (clinical-stage drug/biologics developers).
_DEV_STAGE_INDUSTRY_TOKENS = ("biotech", "drug manufacturers",
                              "pharmaceutic")

_FPI_FORMS = ("20-F", "40-F", "6-K")

# us-gaap concepts that report top-line revenue. Anything matching means
# the issuer HAS filed revenue; the list is a recognizer, not a fetch
# shortlist, so breadth is safe.
_REV_EXACT = {
    "Revenues", "SalesRevenueNet", "SalesRevenueGoodsNet",
    "SalesRevenueServicesNet", "RevenuesNetOfInterestExpense",
    "InterestAndDividendIncomeOperating",
    "RegulatedAndUnregulatedOperatingRevenue",
    "OperatingLeasesIncomeStatementLeaseRevenue",
    "HealthCareOrganizationRevenue", "OilAndGasRevenue",
    "RealEstateRevenueNet", "PremiumsEarnedNet",
}
_REV_PREFIXES = ("RevenueFromContract", "SalesRevenue")


def _revenue_like(concept):
    return concept in _REV_EXACT \
        or any(concept.startswith(p) for p in _REV_PREFIXES)


def _fv(fact):
    import research_snapshot as rs
    return rs.fv(fact) if isinstance(fact, dict) else None


def _refs(fact):
    if isinstance(fact, dict):
        return [r for r in fact.get("evidence_refs") or []
                if isinstance(r, str)]
    return []


def classify_security(ticker, snap, profile):
    """-> the v5.8 classification record. Never raises: an index fetch
    failure degrades to vendor-profile evidence or UNKNOWN, with the
    degradation stated."""
    import research_live as RL
    fu = snap.get("fundamentals") or {}
    co = snap.get("company") or {}
    ov = co.get("overview") or {}
    sector = (profile or {}).get("sector") or co.get("sector") or ""
    industry = (profile or {}).get("industry") or ov.get("industry") or ""

    idx = None
    idx_err = None
    cik = None
    resolution = None
    try:
        cik, resolution = RL.cik_for_filed(ticker)
        idx = RL.concept_index(cik)
    except Exception as e:                       # noqa: BLE001
        idx_err = str(e)[:120]

    taxes = (idx or {}).get("taxonomies") or {}
    gaap = taxes.get("us-gaap") or []
    ifrs = taxes.get("ifrs-full") or []
    forms = (idx or {}).get("forms") or []

    # accounting regime: from the taxonomies the issuer actually files
    if gaap:
        regime = "us-gaap"
    elif ifrs:
        regime = "ifrs"
    else:
        regime = "unknown"

    fpi = any(f in _FPI_FORMS or f.startswith("20-F")
              or f.startswith("40-F") for f in forms) or (
        regime == "ifrs")

    rev_concepts = sorted(c for c in gaap if _revenue_like(c))
    if "Revenue" in ifrs or "RevenueFromContractsWithCustomers" in ifrs:
        rev_concepts += [c for c in ("Revenue",
                                     "RevenueFromContractsWithCustomers")
                         if c in ifrs]

    refs = []
    idx_ref = None
    if idx:
        idx_ref = "SEC-CONCEPT-INDEX-%s" % cik
        refs.append(idx_ref)

    rev_v = _fv(fu.get("revenue_q"))
    ni_v = _fv(fu.get("net_income_q"))

    stage = UNKNOWN
    basis = None
    confidence = "low"
    ind_l = industry.lower()

    if idx and (gaap or ifrs) and not rev_concepts:
        # rule (a): the issuer files facts, and in its ENTIRE concept
        # index no revenue-like concept has ever appeared
        stage, confidence = PRE_REVENUE, "high"
        basis = ("the issuer's full SEC concept index (%d us-gaap / %d "
                 "ifrs-full concepts) contains no revenue concept in "
                 "any taxonomy — admitted evidence that no revenue has "
                 "been reported"
                 % (len(gaap), len(ifrs)))
    elif rev_concepts and any(t in ind_l
                              for t in _DEV_STAGE_INDUSTRY_TOKENS) \
            and rev_v is not None and ni_v is not None and ni_v < 0 \
            and rev_v < DEV_STAGE_MAX_REV_TO_LOSS * abs(ni_v):
        # rule (b): development-stage condition from the issuer's own
        # filed quarter — token revenue against development-scale losses
        stage, confidence = PRE_REVENUE, "high"
        basis = ("development-stage condition: filed quarterly revenue "
                 "$%.1fM is below %.0f%% of the filed quarterly net "
                 "loss $%.1fM in a development-stage sector family "
                 "(%s) — the revenue line is collaboration/grant "
                 "income, not a commercial business"
                 % (rev_v / 1e6, DEV_STAGE_MAX_REV_TO_LOSS * 100,
                    abs(ni_v) / 1e6, industry))
        refs += _refs(fu.get("revenue_q")) + _refs(fu.get("net_income_q"))
    elif rev_concepts:
        stage, confidence = OPERATING, "high"
        basis = ("revenue concepts filed with the SEC: %s"
                 % ", ".join(rev_concepts[:4]))
        refs += _refs(fu.get("revenue_q"))
    elif rev_v is not None:
        stage, confidence = OPERATING, "medium"
        basis = ("no SEC concept index available (%s) but a filed "
                 "revenue fact was admitted from the snapshot"
                 % (idx_err or "fetch degraded"))
        refs += _refs(fu.get("revenue_q"))
    elif idx is None:
        stage, confidence = UNKNOWN, "low"
        basis = ("no concept index (%s) and no admitted revenue fact — "
                 "the business stage cannot be established and is NOT "
                 "ASSESSED; missing data never implies pre-revenue"
                 % (idx_err or "unavailable"))
    else:
        stage, confidence = UNKNOWN, "low"
        basis = ("concept index present but empty of financial "
                 "taxonomies — stage NOT ASSESSED")

    if (profile or {}).get("listing_date") \
            and not (profile or {}).get("full_price_history"):
        listing = "new_listing"
    elif fpi:
        listing = "foreign_private_issuer"
    else:
        listing = "domestic_listed"

    return {
        "schema": SCHEMA,
        "business_stage": stage,
        "business_stage_basis": basis,
        "accounting_regime": regime,
        "listing_type": listing,
        "sector": sector or None,
        "industry": industry or None,
        "revenue_concepts_filed": rev_concepts[:12],
        "forms_observed": forms[:12],
        "classification_evidence_refs": sorted(set(refs)),
        "classification_confidence": confidence,
        "issuer_cik": cik,
        "issuer_resolution": resolution,
        "concept_index_ref": idx_ref,
        "concept_index_error": idx_err,
        "concept_index_url": (idx or {}).get("url"),
        "concept_counts": (idx or {}).get("concept_counts") or {},
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def ledger_records(classification):
    """The ledger entries that make the classification reproducible."""
    out = {}
    ref = classification.get("concept_index_ref")
    if ref:
        out[ref] = {
            "kind": "sec_concept_index",
            "metric": "issuer concept index (classification evidence)",
            "url": classification.get("concept_index_url"),
            "source_type": "SEC EDGAR companyfacts",
            "concept_counts": classification.get("concept_counts"),
            "revenue_concepts_filed":
                classification.get("revenue_concepts_filed"),
            "forms_observed": classification.get("forms_observed"),
        }
    return out
