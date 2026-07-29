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


# §4 (v5.8): every reference family carries a declared kind — a record
# whose kind is null is not reproducible and never registers as final.
_KIND_BY_PREFIX = (
    ("XBRL-", "xbrl_fact"),
    ("CALC-", "derived_figure"),
    ("BAR-", "market_bar"),
    ("REC-", "vendor_record"),
    ("EXHIBIT-", "filed_exhibit"),
    ("V5-", "derived_series"),
    ("STATE-", "research_state"),
    ("SEC-", "sec_index"),
    ("FORM4-", "sec_form4"),
    ("NEWS-", "news_item"),
    ("SOC-", "social_item"),
    ("EST-", "vendor_estimate"),
    ("PEER-", "vendor_record"),
)


def _kind_for(ref):
    for p, k in _KIND_BY_PREFIX:
        if str(ref).startswith(p):
            return k
    return "snapshot_record"


def _record_for(ref, fact, key=None):
    """§3 (v5.7) / §4 (v5.8): a reproducible evidence record, not a
    label. For an XBRL reference the accession, concept and period are
    parsed from the ID itself; the owning fact contributes value,
    units, source and timestamps so an external reviewer can
    reconstruct the number. Every record declares its kind."""
    rec = {"kind": _kind_for(ref)}
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
                     ("period_start", "period_start"),
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


def _merge(a, b):
    """§4 (v5.8): deterministic merge-and-enrich — registration order
    can never cost provenance. Non-null values win over null; a value
    already populated is NEVER replaced (first populated wins, so the
    owning fact's metadata is stable), only missing fields fill in."""
    out = dict(a)
    for k, v in (b or {}).items():
        if v is None:
            continue
        if out.get(k) is None:
            out[k] = v
    return out


def _register(ids, rid, rec):
    if rid in ids:
        ids[rid] = _merge(ids[rid], rec)
    else:
        ids[rid] = dict(rec)


def _walk(obj, found, key=None):
    if isinstance(obj, dict):
        refs = obj.get("evidence_refs")
        if isinstance(refs, list):
            for r in refs:
                if isinstance(r, str) and r:
                    # §4: merge-and-enrich — a later, richer owning
                    # fact fills fields an earlier sparse sighting left
                    # null; populated provenance is never overwritten
                    _register(found, r, _record_for(r, obj, key))
        for k, v in obj.items():
            _walk(v, found, key=k if isinstance(v, dict) else key)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _walk(v, found, key=key)


def build(snap, view5, report_id=None, issuer_cik=None,
          el_ledger=None):
    """-> {schema, ids: {id: record}, hash, issuer_cik}

    `el_ledger` (v5.8 review fix): the snapshot's evidence ledger,
    whose technical_calculations records carry the FORMULA and exact
    input-bar ranges for every level/indicator CALC — provenance the
    v5 ledger previously dropped because the snapshot facts only carry
    refs, not the calc records themselves."""
    ids = {}
    _walk(snap, ids)
    _walk(view5.get("v4") or {}, ids)

    if el_ledger is not None:
        try:
            for rid, rec in (el_ledger._store.get(
                    "technical_calculations") or {}).items():
                inputs = rec.get("inputs")
                if not isinstance(inputs, list):
                    inputs = [inputs] if inputs else []
                _register(ids, rid, {
                    "kind": "derived_figure",
                    "metric": rid[5:] if rid.startswith("CALC-")
                    else rid,
                    "calculation": rec.get("formula"),
                    "input_evidence_ids": [str(x) for x in inputs],
                    "value": rec.get("output"),
                    "units": rec.get("unit"),
                    "note": rec.get("note"),
                })
        except Exception:
            pass

    # calc IDs for every calc-versioned figure (CALC-<key>) — the
    # record carries the formula (basis), the ORDERED input evidence
    # IDs, their periods, units and quality (§4: a calculated figure is
    # reproducible from this record alone)
    _date_rx = re.compile(r"-(\d{4}-\d{2}-\d{2})$")
    for domain in ("levels", "fundamentals", "valuation"):
        for k, f in (snap.get(domain) or {}).items():
            if isinstance(f, dict) and f.get("calc_version"):
                inputs = list(f.get("evidence_refs") or [])
                periods = sorted({m.group(1) for m in
                                  (_date_rx.search(str(r))
                                   for r in inputs) if m})
                _register(ids, "CALC-%s" % k, {
                    "kind": "derived_figure", "metric": k,
                    "calculation": f.get("basis"),
                    "calc_version": f.get("calc_version"),
                    "value": f.get("v"),
                    "units": f.get("unit"),
                    "quality": f.get("quality"),
                    "input_evidence_ids": inputs,
                    "input_periods": periods,
                })

    m = view5.get("multiples") or {}
    for kind in ("pe", "ps"):
        b = m.get(kind) or {}
        if b.get("available"):
            integ = (m.get("ttm_integrity") or {}).get(kind) or {}
            _register(ids, "V5-BAND-%s" % kind, {
                "kind": "derived_series",
                "metric": "own-history %s band" % kind,
                "calculation": b.get("basis"),
                "concept": integ.get("concept"),
                "window": [b.get("window_start"), b.get("window_end")],
                "sessions": b.get("sessions_computable"),
                # §4 (v5.8): the band is reproducible from this record
                # — observations, exclusions, percentile method and
                # the resulting percentiles are all stated
                "observations_used": b.get("sessions_computable"),
                "observations_in_window": b.get("sessions_in_window"),
                "exclusions": {
                    "negative_trailing_metric":
                        b.get("excluded_negative_ttm"),
                    "no_trailing_metric": b.get("excluded_no_ttm")},
                "percentile_method": "linear interpolation over the "
                                     "sorted daily trailing multiples "
                                     "(p25/p50/p75)",
                "percentiles": {"p25": b.get("p25"), "p50": b.get("p50"),
                                "p75": b.get("p75"),
                                "min": b.get("min"),
                                "max": b.get("max")},
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
        _register(ids, "V5-%s-%s" % ("SCENARIO" if _under else "RANGE",
                                     str(r["leg"]).upper()),
                       {"kind": "derived_row",
                        "metric": "%s row (%s)"
                        % ("forward" if _under else "historical-range",
                           r.get("label")),
                        "calculation": "multiple x trailing metric",
                        "value": r.get("price")})
    if sc.get("available"):
        _register(ids, "V5-SCENARIO-ANCHOR" if _under
                       else "V5-HISTORICAL-METRIC-ANCHOR",
                       {"kind": "derived_row",
                        "metric": "central row of the valuation table",
                        "calculation": "median multiple x trailing "
                                       "metric"})
    ex = snap.get("exhibit") or {}
    if ex.get("disposition") == "ADMITTED":
        _register(ids, "EXHIBIT-GUIDANCE",
                       {"kind": "filed_exhibit",
                        "source_type": "8-K earnings exhibit",
                        "metric": "issuer guidance"})
    for o in (view5.get("adapter") or {}).get("one_time_items") or []:
        for r in o.get("evidence_refs") or []:
            _register(ids, r, {"kind": "xbrl_fact",
                               "metric": "one-time item",
                               "concept": o.get("concept"),
                               "value": o.get("value"),
                               "period_end": o.get("period_end"),
                               "accession": o.get("accession")})
    # v5.8.1: derived figures quoted inside published claims register
    # as first-class CALC records (value + formula + inputs), so every
    # number a claim quotes resolves to a reproducible record
    for c in ((view5.get("claims") or {}).get("claims") or []) \
            + ((view5.get("claims") or {}).get("rejected") or []):
        for df in c.get("derived_figures") or []:
            if df.get("id"):
                _register(ids, df["id"], {
                    "kind": "derived_figure",
                    "metric": df.get("label"),
                    "calculation": df.get("formula"),
                    "value": df.get("value"),
                    "units": df.get("units"),
                    "input_evidence_ids":
                        list(df.get("input_evidence_ids") or []),
                    "note": df.get("note"),
                })

    # §1 (v5.8): classification evidence — the concept-index record
    # that establishes business stage is registered so the stage is
    # independently reproducible from the ledger
    cls = view5.get("classification") or {}
    if cls:
        try:
            import report_v5_classify as _CLS
            for rid, rec in _CLS.ledger_records(cls).items():
                _register(ids, rid, rec)
        except Exception:
            pass

    if report_id:
        _register(ids, "STATE-%s" % report_id,
                       {"kind": "research_state",
                        "metric": "this research state"})
    prior = (view5.get("changeset") or {}).get("prior_report_id")
    if prior:
        _register(ids, "STATE-%s" % prior,
                       {"kind": "research_state",
                        "metric": "prior research state"})

    # §4 (v5.8): an XBRL record's filing is addressable — derive the
    # EDGAR archive URL from the accession + issuer CIK when the owning
    # fact carried none
    if issuer_cik:
        try:
            _cik_int = int(str(issuer_cik).lstrip("0") or "0")
        except ValueError:
            _cik_int = None
        for rec in ids.values():
            if _cik_int and isinstance(rec, dict) \
                    and rec.get("kind") == "xbrl_fact" \
                    and rec.get("accession") \
                    and not rec.get("url"):
                rec["url"] = ("https://www.sec.gov/Archives/edgar/data/"
                              "%d/%s/" % (_cik_int,
                                          str(rec["accession"]
                                              ).replace("-", "")))

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
