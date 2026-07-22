#!/usr/bin/env python3
"""report_v3_evidence.py — the reproducible evidence package (v3.1).

v3 shipped an `evidence_index` that was 693 bare strings: identifiers
with nothing behind them. You could see that a fact had been cited and
had no way to check it. This module replaces that with one record per
evidence id, each carrying where it came from, when, what it said, and
whether we used it.

Two record kinds:

  records[id]       an observation — a bar, an XBRL fact, a filing, a
                    news item, a post. Carries source, timestamps, value,
                    a hash of the raw record, and the admitted/rejected
                    disposition WITH its reason.

  calculations[id]  a computed figure. Carries the formula, its version,
                    every operand WITH the evidence id it came from, the
                    unrounded result, the displayed result and the rule
                    that turned one into the other.

The displayed result matters: the renderer formats numbers separately,
so `validate` can compare the two and fail when the page and the
evidence disagree. That cross-check only works because the two are
computed independently — do not "simplify" it by having one read the
other.

Dispositions are a closed vocabulary. In particular ABSENT and
AVAILABLE_NOT_INGESTED are different claims: the first says a source
published nothing, the second says it published something we cannot yet
read. Collapsing them lets a parser gap masquerade as an absence of
evidence.
"""

import hashlib
import json

import report_v3_model as M
import research_snapshot as rs

SCHEMA = "stock_research_brief_evidence/v3.1"

# disposition vocabulary
ADMITTED = "ADMITTED"
REJECTED = "REJECTED"
DEFERRED = "DEFERRED"                 # filed after the point-in-time gate
ABSENT = "ABSENT"                     # the source published nothing
AVAILABLE_NOT_INGESTED = "AVAILABLE_NOT_INGESTED"   # public, unread by us
SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"           # the source is down/absent

DISPOSITIONS = (ADMITTED, REJECTED, DEFERRED, ABSENT,
                AVAILABLE_NOT_INGESTED, SOURCE_UNAVAILABLE)

# id prefix -> evidence type
PREFIX_TYPE = [
    ("BAR-", "market_bar"),
    ("XBRL-", "xbrl_fact"),
    ("SHR-", "shares_outstanding"),
    ("F4TXN-", "form4_transaction"),
    ("F4-", "form4_filing"),
    ("OWN-", "ownership_filing"),
    ("CAT-", "catalyst_disclosure"),
    ("NEWS-", "news_item"),
    ("SOC-", "social_post"),
    ("CALC-", "calculation"),
    ("REC-", "recommendation_input"),
    ("EXT-", "external_reference"),
]

# Bars needed to reproduce every indicator the brief displays. The
# 200-day average is the longest window; everything shorter is a subset.
BARS_FOR_INDICATORS = 200


def ev_type(eid):
    for p, t in PREFIX_TYPE:
        if str(eid).startswith(p):
            return t
    return "unknown"


def _hash(obj):
    """Stable hash of a record's content, so a reader can tell whether
    the row they are looking at is the row we admitted."""
    if isinstance(obj, (str, bytes)):
        b = obj.encode("utf-8") if isinstance(obj, str) else obj
    else:
        b = json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(b).hexdigest()


def payload_hash(obj):
    return _hash(obj)


def _rec(eid, etype, source=None, url=None, accession=None, value=None,
         unit=None, period=None, timestamps=None, disposition=ADMITTED,
         reason=None, grade=M.OBSERVED, raw=None, immutable=None,
         extra=None):
    r = {
        "evidence_id": eid,
        "evidence_type": etype,
        "source": {"name": source, "url": url, "accession": accession},
        "timestamps": {k: v for k, v in (timestamps or {}).items()
                       if v is not None} or None,
        "value": value,
        "unit": unit,
        "period": period,
        "raw_hash": _hash(raw if raw is not None else
                          {"id": eid, "v": value, "p": period}),
        "disposition": disposition,
        "reason": reason,
        "grade": grade,
        # A SEC accession is immutable: the same string always resolves to
        # the same document. A news URL is not, so those carry a content
        # hash instead and say so.
        "source_reference": (immutable if immutable is not None else
                             ({"kind": "sec_accession", "ref": accession,
                               "immutable": True} if accession else
                              {"kind": "url_with_content_hash", "ref": url,
                               "immutable": False})),
    }
    if extra:
        r.update(extra)
    return r


# ── calculations ────────────────────────────────────────────────────────

def _round_half_up(x, places):
    from decimal import Decimal, ROUND_HALF_UP
    q = Decimal(10) ** -places
    return float(Decimal(repr(float(x))).quantize(q, rounding=ROUND_HALF_UP))


# metric -> (format string, decimal places, human rounding rule)
DISPLAY = {
    "pe_trailing": ("%.1fx", 1, "half-up to 1 decimal place"),
    # one decimal, matching the page-1 metrics grid: the package
    # documents the rounding the report applies, not a different one
    "market_cap": ("$%.1fB", 1, "divide by 1e9, half-up to 1 decimal"),
    "revenue_yoy": ("%+.1f%%", 1, "half-up to 1 decimal place"),
    "net_margin": ("%.1f%%", 1, "half-up to 1 decimal place"),
    "gross_margin": ("%.1f%%", 1, "half-up to 1 decimal place"),
    "eps_ttm": ("$%.2f", 2, "half-up to 2 decimal places"),
    "ma20": ("%.2f", 2, "half-up to 2 decimal places"),
    "ma50": ("%.2f", 2, "half-up to 2 decimal places"),
    "ma200": ("%.2f", 2, "half-up to 2 decimal places"),
    "atr14": ("%.2f", 2, "half-up to 2 decimal places"),
    "rs_vs_spy": ("%+.1f%%", 1, "half-up to 1 decimal place"),
    "rel_volume": ("%.2fx", 2, "half-up to 2 decimal places"),
    "support": ("%.2f", 2, "half-up to 2 decimal places"),
    "resistance": ("%.2f", 2, "half-up to 2 decimal places"),
}


def _calc(cid, rec, records, calcs):
    """Turn a ledger CALC row into a full calculation record: resolve
    every operand to its evidence id AND its value, and state how the
    unrounded result became the number on the page.

    An operand that cannot be resolved is kept and marked, because a
    calculation whose inputs cannot be produced is exactly what the
    operand-completeness check exists to catch."""
    slug = cid[5:] if cid.startswith("CALC-") else cid
    fmt, places, rule = DISPLAY.get(slug, ("%s", None, "no rounding applied"))
    raw = rec.get("output")
    operands, complete = [], True
    for ref in (rec.get("inputs") or []):
        op = {"evidence_id": ref, "value": None, "resolved": False}
        if ".." in str(ref):
            # a bar range, e.g. BAR-2026-01-02..BAR-2026-06-30
            lo, hi = str(ref).split("..", 1)
            member = [k for k in records
                      if k.startswith("BAR-") and lo <= k <= hi]
            op.update({"kind": "range", "members": len(member),
                       "resolved": bool(member),
                       "value": ("%d bars" % len(member)) if member else None})
            if not member:
                complete = False
        elif ref in records:
            op.update({"value": records[ref].get("value"), "resolved": True,
                       "kind": ev_type(ref)})
        elif ref in calcs:
            op.update({"value": calcs[ref].get("result_unrounded"),
                       "resolved": True, "kind": "calculation"})
        else:
            op["kind"] = ev_type(ref)
            op["note"] = ("operand not present in this evidence package; "
                          "the figure cannot be reproduced from it")
            complete = False
        operands.append(op)
    shown = None
    if isinstance(raw, (int, float)):
        v = raw / 1e9 if slug == "market_cap" else raw
        try:
            shown = fmt % (_round_half_up(v, places) if places is not None
                           else v)
        except Exception:
            shown = str(raw)
    return {
        "calculation_id": cid,
        "formula_version": rec.get("calc_version") or slug,
        "formula": rec.get("formula"),
        "operands": operands,
        "operands_complete": complete,
        "result_unrounded": raw,
        "result_displayed": shown,
        "rounding_rule": rule,
        "unit": rec.get("unit"),
        "note": rec.get("note"),
        "grade": M.DERIVED,
    }


# ── build ───────────────────────────────────────────────────────────────

def build(snap, view, prov=None, led=None, artifacts=None,
          suppressed=None):
    prov = prov or {}
    suppressed = set(suppressed or [])
    records, calcs, notes = {}, {}, []
    ld = led.to_dict() if led is not None else {}

    def put(r):
        records[r["evidence_id"]] = r

    # market bars — only the window the displayed indicators actually
    # need, so the package is reproducible without carrying two years
    bars = sorted([b for b in (ld.get("market_bars") or [])],
                  key=lambda b: b.get("id", ""))
    kept = bars[-BARS_FOR_INDICATORS:]
    for b in kept:
        put(_rec(b["id"], "market_bar", source="Yahoo Finance daily bars",
                 value={"o": b.get("open"), "h": b.get("high"),
                        "l": b.get("low"), "c": b.get("close"),
                        "v": b.get("volume")},
                 unit="USD", period={"session": b["id"][4:]},
                 timestamps={"observed": b["id"][4:]},
                 raw=b, immutable={"kind": "vendor_series", "immutable": False,
                                   "ref": snap.get("levels", {}).get(
                                       "series_id")}))
    if len(bars) > len(kept):
        notes.append("%d of %d daily bars are carried: the last %d, which is "
                     "every bar the displayed indicators consume (the 200-day "
                     "average is the longest window)."
                     % (len(kept), len(bars), BARS_FOR_INDICATORS))

    for x in (ld.get("xbrl_facts") or []):
        put(_rec(x["id"], "xbrl_fact", source="SEC XBRL companyconcept",
                 url=x.get("url"), accession=x.get("accn"),
                 value=x.get("value"), unit="USD",
                 period={"start": x.get("start"), "end": x.get("end"),
                         "form": x.get("form")},
                 timestamps={"accepted": x.get("accepted"),
                             "period_end": x.get("end")},
                 raw=x,
                 extra={"tag": x.get("tag"),
                        "xbrl_context": {"tag": x.get("tag"),
                                         "period_start": x.get("start"),
                                         "period_end": x.get("end"),
                                         "form": x.get("form"),
                                         "accession": x.get("accn")},
                        "reconstructed_q4": x.get("reconstructed_q4"),
                        "reconstruction": x.get("reconstruction")}))

    for s in (ld.get("shares_outstanding") or []):
        put(_rec(s["id"], "shares_outstanding", source="SEC filing cover page",
                 url=s.get("url"), accession=s.get("accn"),
                 value=s.get("value"), unit="shares",
                 timestamps={"accepted": s.get("accepted")}, raw=s))

    for f in (ld.get("form4_records") or []):
        put(_rec(f["id"], ev_type(f["id"]), source="SEC Form 4",
                 url=f.get("url"), accession=f.get("accn"),
                 value={k: f.get(k) for k in
                        ("code", "shares", "price", "value", "class",
                         "owner", "title", "date") if f.get(k) is not None}
                 or None,
                 timestamps={"accepted": f.get("accepted"),
                             "transaction_date": f.get("date")},
                 raw=f,
                 extra={"transaction_class": f.get("class"),
                        "carries_view": f.get("carries_view"),
                        "planned_10b5_1": f.get("is_planned")}))

    for o in (ld.get("ownership_filings") or []):
        put(_rec(o["id"], "ownership_filing", source="SEC Schedule 13D/13G",
                 url=o.get("url"), accession=o.get("accn"),
                 value={"form": o.get("form"), "filer": o.get("filer"),
                        "stake": o.get("stake")},
                 timestamps={"accepted": o.get("accepted")}, raw=o,
                 disposition=(ADMITTED if o.get("filer")
                              else AVAILABLE_NOT_INGESTED),
                 reason=(None if o.get("filer") else
                         "the filing is public and fetchable; our parser "
                         "does not read filer identity or stake size from "
                         "the document body"),
                 extra={"filer_parsed": bool(o.get("filer")),
                        "stake_parsed": o.get("stake") is not None}))

    for c in (ld.get("catalyst_records") or []):
        put(_rec(c["id"], "catalyst_disclosure", source="SEC 8-K / periodic",
                 url=c.get("url"), accession=c.get("accn"),
                 value={k: c.get(k) for k in ("form", "item", "kind")
                        if c.get(k)} or None,
                 timestamps={"accepted": c.get("accepted")}, raw=c))

    # news: admitted items carry their relevance decision; rejected items
    # carry the reason they failed it
    for n in (ld.get("news_records") or []):
        put(_rec(n["id"], "news_item", source=n.get("publisher") or "media",
                 url=n.get("url"), value=n.get("headline"),
                 timestamps={"published": n.get("published_at"),
                             "retrieved": n.get("retrieved_at")},
                 grade=M.OBSERVED, raw=n,
                 disposition=ADMITTED,
                 reason=(n.get("relevance") or {}).get("reason")
                 if isinstance(n.get("relevance"), dict) else n.get("reason"),
                 extra={"relevance_decision": n.get("article_check")
                        or n.get("relevance"), "tier": n.get("tier")}))
    import evidence_ledger as EL
    for r in (prov.get("news_rejected") or []):
        nid = EL.news_id(r.get("url") or r.get("headline") or "")
        put(_rec(nid, "news_item", source=r.get("publisher") or "media",
                 url=r.get("url"), value=r.get("headline"),
                 timestamps={"published": r.get("published_at")},
                 disposition=REJECTED, reason=r.get("reason"), raw=r,
                 extra={"relevance_decision": r.get("article_check")}))

    for s in (ld.get("social_records") or []):
        put(_rec(s["id"], "social_post", source=s.get("source") or "stocktwits",
                 url=s.get("url"), value=s.get("text_hash"),
                 timestamps={"published": s.get("published_at"),
                             "retrieved": s.get("retrieved_at")},
                 disposition=(ADMITTED if s.get("disposition") == "counted"
                              else REJECTED),
                 reason=s.get("reason"), grade=M.OBSERVED, raw=s,
                 immutable={"kind": "content_hash", "immutable": False,
                            "ref": s.get("text_hash")},
                 extra={"classification": s.get("sentiment"),
                        "relevance": s.get("relevance"),
                        "duplicate_group": s.get("dup_group"),
                        "author_hash": s.get("author_hash")}))

    # filing facts the point-in-time gate held back
    for d in (prov.get("deferred") or []):
        did = "DEFER-%s-%s" % (d.get("metric"), d.get("period_end"))
        put(_rec(did, "xbrl_fact", source="SEC XBRL companyconcept",
                 value=d.get("value"), period={"end": d.get("period_end"),
                                               "form": d.get("form")},
                 timestamps={"accepted": d.get("accepted")},
                 disposition=DEFERRED,
                 reason="accepted after this report's point-in-time gate",
                 raw=d))

    # calculations last: operands resolve against the records above
    lv = snap.get("levels") or {}
    for c in (ld.get("technical_calculations") or []):
        rec = _calc(c["id"], c, records, calcs)
        slug = c["id"][5:] if c["id"].startswith("CALC-") else c["id"]
        # A calculation the snapshot withheld is recorded with its reason
        # rather than dropped: the reader can still see it was computed
        # and why it is not on the page.
        withheld = slug in suppressed or (slug in DISPLAY
                                          and slug in ("rel_volume",)
                                          and slug not in lv)
        rec["displayed"] = not withheld
        if withheld:
            rec["not_displayed_reason"] = (
                (prov.get("coverage") or {}).get(slug)
                or "withheld from the report for this run")
        calcs[c["id"]] = rec
    for c in (ld.get("recommendation_inputs") or []):
        # A recommendation input cites facts AND derived figures, so its
        # references resolve against both stores. Checking only `records`
        # marked every one of them incomplete.
        def _res(r):
            if r in records:
                return {"evidence_id": r, "resolved": True,
                        "kind": ev_type(r), "value": records[r].get("value")}
            if r in calcs:
                return {"evidence_id": r, "resolved": True,
                        "kind": "calculation",
                        "value": calcs[r].get("result_unrounded")}
            return {"evidence_id": r, "resolved": False, "kind": ev_type(r),
                    "note": "not present in this evidence package"}
        ops = [_res(r) for r in (c.get("evidence_refs") or [])]
        calcs[c["id"]] = {
            "calculation_id": c["id"], "formula_version": "rec/v1",
            "formula": c.get("rationale"),
            "operands": ops,
            "operands_complete": all(o["resolved"] for o in ops),
            "result_unrounded": c.get("value"), "result_displayed":
                str(c.get("value")), "rounding_rule": "none",
            "unit": None, "note": c.get("name"), "grade": M.INFERRED}

    coverage = dict((prov.get("coverage") or {}))
    pkg = {
        "schema": SCHEMA,
        "ticker": snap.get("ticker"),
        "report_time_utc": view.get("report_time_utc"),
        "market_data_time_utc": view.get("quote_time_utc"),
        "display_timezone": M.ET,
        "grades": M.GRADE_NOTE,
        "dispositions": {
            ADMITTED: "used in the report",
            REJECTED: "fetched, failed a stated test; the reason is on the "
                      "record",
            DEFERRED: "filed after the point-in-time gate",
            ABSENT: "the source published nothing for this",
            AVAILABLE_NOT_INGESTED: "public and fetchable, but our parser "
                                    "does not read it yet — a gap in this "
                                    "software, not in the evidence",
            SOURCE_UNAVAILABLE: "the source itself could not be reached or "
                                "does not offer this data",
        },
        "records": records,
        "calculations": calcs,
        "populations": ld.get("counts") or {},
        "count_statements": ld.get("count_statements") or {},
        "source_coverage": coverage,
        "hash_verification": ld.get("hash_verification"),
        "notes": notes,
        "view": _jsonable(view),
        "snapshot_schema": snap.get("schema"),
        "record_count": len(records),
        "calculation_count": len(calcs),
    }
    if artifacts:
        pkg["artifacts"] = artifacts
    return pkg


def _jsonable(o):
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (str, int, float, bool)) or o is None:
        return o
    return str(o)


def displayed(pkg, calculation_id):
    """The string this package says should appear on the page."""
    return (pkg.get("calculations", {}).get(calculation_id) or {}) \
        .get("result_displayed")


def by_type(pkg, etype):
    return {k: v for k, v in (pkg.get("records") or {}).items()
            if v.get("evidence_type") == etype}


def by_disposition(pkg, etype, disp):
    return {k: v for k, v in by_type(pkg, etype).items()
            if v.get("disposition") == disp}
