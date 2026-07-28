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

SCHEMA = "v5-ledger/1"


def _walk(obj, found):
    if isinstance(obj, dict):
        refs = obj.get("evidence_refs")
        if isinstance(refs, list):
            for r in refs:
                if isinstance(r, str) and r:
                    found.setdefault(r, "snapshot evidence_refs")
        for v in obj.values():
            _walk(v, found)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _walk(v, found)


def build(snap, view5, report_id=None):
    """-> {schema, ids: {id: kind}, hash}"""
    ids = {}
    _walk(snap, ids)
    _walk(view5.get("v4") or {}, ids)

    # calc IDs for every calc-versioned figure (CALC-<key>)
    for domain in ("levels", "fundamentals", "valuation"):
        for k, f in (snap.get(domain) or {}).items():
            if isinstance(f, dict) and f.get("calc_version"):
                ids.setdefault("CALC-%s" % k, "derived figure (%s)" % k)

    m = view5.get("multiples") or {}
    for kind in ("pe", "ps"):
        if (m.get(kind) or {}).get("available"):
            ids.setdefault("V5-BAND-%s" % kind,
                           "own-history multiple band (%s)" % kind)
    sc = view5.get("scenarios") or {}
    for r in sc.get("rows") or []:
        ids.setdefault("V5-SCENARIO-%s" % str(r["leg"]).upper(),
                       "range/scenario row (%s)" % r.get("label"))
    if sc.get("available"):
        ids.setdefault("V5-SCENARIO-ANCHOR",
                       "median/base row of the range")
    ex = snap.get("exhibit") or {}
    if ex.get("disposition") == "ADMITTED":
        ids.setdefault("EXHIBIT-GUIDANCE",
                       "issuer guidance from the admitted 8-K exhibit")
    for o in (view5.get("adapter") or {}).get("one_time_items") or []:
        for r in o.get("evidence_refs") or []:
            ids.setdefault(r, "one-time item (XBRL)")
    if report_id:
        ids.setdefault("STATE-%s" % report_id, "this research state")
    prior = (view5.get("changeset") or {}).get("prior_report_id")
    if prior:
        ids.setdefault("STATE-%s" % prior, "prior research state")

    digest = hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()
    return {"schema": SCHEMA, "ids": ids, "hash": digest,
            "count": len(ids)}


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
