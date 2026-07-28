#!/usr/bin/env python3
"""report_v5_ledger.py — the registered evidence ledger (v5.6 §8).

Every evidence or counterevidence reference a claim carries must
resolve to an ID registered here. Free-form prose is not a reference.

The ledger is built by walking the snapshot for every ID that already
appears in an `evidence_refs` list (XBRL-*, REC-*, BAR-*, CALC-*, ...),
then registering the v5-generated derivation IDs (bands, scenario rows,
research states) and the calc IDs for every calc-versioned figure. The
ledger hash — sha256 over the sorted IDs — binds the appendix, the
research state and the validation JSON to the same evidence universe.
"""

import hashlib
import re

SCHEMA = "v5-ledger/1"


_XBRL_RX = re.compile(
    r"^XBRL-([\d.-]+)-([a-z-]+):([A-Za-z\d]+)-(\d{4}-\d{2}-\d{2})$")


def _record_for(ref, fact, key=None):
    """§3 (v5.7): a reproducible evidence record, not a label. For an
    XBRL reference the accession, concept and period are parsed from
    the ID itself; the owning fact contributes value, units, source and
    timestamps so an external reviewer can reconstruct the number."""
    rec = {"kind": None}
    m = _XBRL_RX.match(str(ref))
    if m:
        rec.update({"kind": "xbrl_fact", "accession": m.group(1),
                    "taxonomy": m.group(2), "concept": m.group(3),
                    "period_end": m.group(4),
                    "source_type": "SEC EDGAR companyfacts"})
    f = fact if isinstance(fact, dict) else {}
    for src, dst in (("v", "value"), ("unit", "units"),
                     ("source", "source"), ("source_url", "url"),
                     ("period_end", "period_end"),
                     ("published_at", "accepted_at"),
                     ("retrieved_at", "retrieved_at"),
                     ("basis", "calculation"),
                     ("calc_version", "calc_version"),
                     ("quality", "quality")):
        if f.get(src) is not None and dst not in rec:
            rec[dst] = f.get(src)
    if key and "metric" not in rec:
        rec["metric"] = key
    return rec


def _walk(obj, found, key=None):
    if isinstance(obj, dict):
        refs = obj.get("evidence_refs")
        if isinstance(refs, list):
            for r in refs:
                if isinstance(r, str) and r and r not in found:
                    found[r] = _record_for(r, obj, key)
        for k, v in obj.items():
            _walk(v, found, key=k if isinstance(v, dict) else key)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _walk(v, found, key=key)


def build(snap, view5, report_id=None, issuer_cik=None):
    """-> {schema, ids: {id: record}, hash, issuer_cik}"""
    ids = {}
    _walk(snap, ids)
    _walk(view5.get("v4") or {}, ids)

    # calc IDs for every calc-versioned figure (CALC-<key>) — the
    # record carries the formula (basis) and the input evidence IDs
    for domain in ("levels", "fundamentals", "valuation"):
        for k, f in (snap.get(domain) or {}).items():
            if isinstance(f, dict) and f.get("calc_version"):
                ids.setdefault("CALC-%s" % k, {
                    "kind": "derived_figure", "metric": k,
                    "calculation": f.get("basis"),
                    "calc_version": f.get("calc_version"),
                    "value": f.get("v"),
                    "input_evidence_ids": list(f.get("evidence_refs")
                                               or []),
                })

    m = view5.get("multiples") or {}
    for kind in ("pe", "ps"):
        b = m.get(kind) or {}
        if b.get("available"):
            integ = (m.get("ttm_integrity") or {}).get(kind) or {}
            ids.setdefault("V5-BAND-%s" % kind, {
                "kind": "derived_series",
                "metric": "own-history %s band" % kind,
                "calculation": b.get("basis"),
                "concept": integ.get("concept"),
                "window": [b.get("window_start"), b.get("window_end")],
                "sessions": b.get("sessions_computable"),
                "market_data_vendor": m.get("bar_source"),
                "adjustment_basis": "split-adjusted closes; per-share "
                                    "facts rebased by filing date",
            })
    sc = view5.get("scenarios") or {}
    # ID vocabulary follows the mode (§2): a historical range registers
    # V5-RANGE-* rows and a metric anchor; only underwritten scenarios
    # register V5-SCENARIO-* rows.
    _under = sc.get("mode") == "underwritten"
    for r in sc.get("rows") or []:
        ids.setdefault("V5-%s-%s" % ("SCENARIO" if _under else "RANGE",
                                     str(r["leg"]).upper()),
                       {"kind": "derived_row",
                        "metric": "%s row (%s)"
                        % ("forward" if _under else "historical-range",
                           r.get("label")),
                        "calculation": "multiple x trailing metric",
                        "value": r.get("price")})
    if sc.get("available"):
        ids.setdefault("V5-SCENARIO-ANCHOR" if _under
                       else "V5-HISTORICAL-METRIC-ANCHOR",
                       {"kind": "derived_row",
                        "metric": "central row of the valuation table",
                        "calculation": "median multiple x trailing "
                                       "metric"})
    ex = snap.get("exhibit") or {}
    if ex.get("disposition") == "ADMITTED":
        ids.setdefault("EXHIBIT-GUIDANCE",
                       {"kind": "filed_exhibit",
                        "source_type": "8-K earnings exhibit",
                        "metric": "issuer guidance"})
    for o in (view5.get("adapter") or {}).get("one_time_items") or []:
        for r in o.get("evidence_refs") or []:
            ids.setdefault(r, {"kind": "xbrl_fact",
                               "metric": "one-time item",
                               "concept": o.get("concept"),
                               "value": o.get("value"),
                               "period_end": o.get("period_end"),
                               "accession": o.get("accession")})
    if report_id:
        ids.setdefault("STATE-%s" % report_id,
                       {"kind": "research_state",
                        "metric": "this research state"})
    prior = (view5.get("changeset") or {}).get("prior_report_id")
    if prior:
        ids.setdefault("STATE-%s" % prior,
                       {"kind": "research_state",
                        "metric": "prior research state"})

    # every record belongs to the intended issuer (§1): the XBRL facts
    # were fetched from this CIK's companyfacts, and the identifier is
    # stamped on the ledger so the binding is checkable
    retrieved = snap.get("report_time")
    for rec in ids.values():
        if isinstance(rec, dict):
            rec.setdefault("issuer_cik", issuer_cik)
            rec.setdefault("retrieved_at", retrieved)

    digest = hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()
    return {"schema": SCHEMA, "ids": ids, "hash": digest,
            "issuer_cik": issuer_cik, "count": len(ids)}


def unresolved(refs, ledger):
    """The refs that do NOT resolve — prose, typos, unregistered IDs."""
    known = ledger.get("ids") or {}
    bad = []
    for r in refs or []:
        if not isinstance(r, str) or r not in known:
            bad.append(str(r)[:60])
    return bad


TECHNICAL_REF_PREFIXES = ("CALC-ma", "CALC-rsi", "BAR-")


def irrelevant_counters(claim):
    """Same-proposition rule, machine-checkable: technical/price refs
    cannot counter a fundamental or valuation claim."""
    if claim.get("claim_type") not in ("fundamental", "valuation"):
        return []
    return [r for r in claim.get("counterevidence_refs") or []
            if isinstance(r, str)
            and r.startswith(TECHNICAL_REF_PREFIXES)]
