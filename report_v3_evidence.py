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

SCHEMA = "stock_research_brief_evidence/v3.2"

# How raw_hash is produced. Named and versioned so a reader can
# reproduce it: sha256 over UTF-8 JSON with sorted keys, no
# whitespace between separators, non-JSON values coerced with str().
HASH_VERSION = "sha256/json-sorted-compact/v2"
HASH_CANONICALIZATION = (
    "record_hash = sha256(json.dumps({k: v for k, v in record.items() "
    "if k != 'record_hash'}, sort_keys=True, separators=(',', ':'), "
    "default=str).encode('utf-8')).hexdigest()")

# v1 documented hashing the exported record but actually hashed the
# upstream source object, so not one of 497 published hashes reproduced
# under its own stated method. The two are now separate fields with
# separate meanings, and validation recomputes the first.
SOURCE_HASH_NOTE = (
    "source_raw_hash is sha256 over the UPSTREAM payload this record was "
    "built from - the ledger row, filing row or API object - canonicalised "
    "the same way. It answers 'is this the row we ingested'. record_hash "
    "answers 'is this the record we published'. Only record_hash is "
    "recomputable from evidence.json alone.")

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

# Completed sessions the package must carry. The 52-week closing
# high and low
# declare a 252-session window, which is longer than the 200-day average,
# so 252 is the floor. v3.1 exported 200 *records* — but the SPY
# placeholder sorted after "BAR-" and took one of those slots, leaving
# 199 issuer bars behind a formula that declared 200.
BARS_FOR_INDICATORS = 252

# Declared window length per calculation slug. The formula string says
# "last N sessions"; this is that N, checked against the bars actually
# delivered. A mismatch is a fatal defect, not a rounding difference.
DECLARED_WINDOW = {
    "ma9": 9, "ma21": 21, "ma20": 20, "ma50": 50, "ma200": 200, "atr14": 15,
    "rsi14": 250, "base_tightness_pct": 20,
    "support60": 60, "resistance60": 60, "hi52": 252, "lo52": 252,
    "rel_volume": 21, "rs_vs_spy": 61,
}


def ev_type(eid):
    for p, t in PREFIX_TYPE:
        if str(eid).startswith(p):
            return t
    return "unknown"


def _hash(obj):
    """Stable hash of a record's content, so a reader can tell whether
    the row they are looking at is the row we admitted.

    Canonicalisation is fixed and versioned (HASH_VERSION): sorted keys,
    compact separators, str() for anything JSON cannot represent. Any
    change to this function is a new hash version, because otherwise
    every previously published hash silently stops verifying."""
    if isinstance(obj, (str, bytes)):
        b = obj.encode("utf-8") if isinstance(obj, str) else obj
    else:
        b = json.dumps(obj, sort_keys=True, separators=(",", ":"),
                       default=str).encode("utf-8")
    return hashlib.sha256(b).hexdigest()


def payload_hash(obj):
    return _hash(obj)


def record_hash(rec):
    """The published record's own hash: sha256 over the record with the
    hash field itself removed. Recomputable from evidence.json alone."""
    return _hash({k: v for k, v in rec.items() if k != "record_hash"})


def verify_record_hashes(pkg):
    """Recompute every record_hash and report what did not match."""
    recs = (pkg or {}).get("records") or {}
    bad = []
    for rid, r in recs.items():
        want = r.get("record_hash")
        if not want or want != record_hash(r):
            bad.append(rid)
    return {"total": len(recs), "matched": len(recs) - len(bad),
            "mismatched": sorted(bad),
            "all_match": not bad and bool(recs)}


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
        # the upstream object, not this record; see SOURCE_HASH_NOTE
        "source_raw_hash": _hash(raw if raw is not None else
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


# How a guidance figure is displayed, by unit. This is a SPECIFICATION,
# implemented independently here and in the renderer, so comparing the
# two is a real check rather than one module reading the other. Precision
# is whatever the issuer stated: 58.25% stays 58.25%, and $2,565M is
# $2.565B, not $2.56B.
GUIDANCE_DISPLAY = {
    "%": (1.0, "", "%"),
    "USD/share": (1.0, "$", ""),
    "USD_M": (1.0, "$", "M"),
}
GUIDANCE_DISPLAY_OVERRIDE = {"revenue": (1000.0, "$", "B")}


def guidance_display(key, unit, x):
    """Exact-precision rendering of a guidance value."""
    if x is None:
        return None
    scale, pre, suf = GUIDANCE_DISPLAY_OVERRIDE.get(
        key, GUIDANCE_DISPLAY.get(unit or "", (1.0, "", "")))
    v = float(x) / scale
    return "%s%s%s" % (pre, ("%.4f" % v).rstrip("0").rstrip("."), suf)


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
            # a bar range, e.g. BAR-2026-01-02..BAR-2026-06-30, or the
            # benchmark equivalent SPY-...
            lo, hi = str(ref).split("..", 1)
            pre = "SPY-" if lo.startswith("SPY-") else "BAR-"
            member = [k for k in records
                      if k.startswith(pre) and lo <= k <= hi]
            want = DECLARED_WINDOW.get(
                cid[5:] if cid.startswith("CALC-") else cid)
            # Cardinality, not mere presence. "Some bars matched" is how a
            # 200-session window validated against 199 delivered bars.
            enough = bool(member) and (want is None or len(member) == want)
            op.update({"kind": "range", "members": len(member),
                       "expected_members": want, "resolved": enough,
                       "value": ("%d %s" % (len(member),
                                            "benchmark sessions"
                                            if pre == "SPY-" else "bars"))
                       if member else None})
            if not enough:
                complete = False
                op["note"] = ("window declares %s sessions; %d delivered"
                              % (want, len(member)))
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


# ── independent recomputation ───────────────────────────────────────────
#
# The whole point of shipping the bars is that a reader can redo the
# arithmetic. This does exactly that, from the records in this package
# and nothing else, and compares the answer with what the pipeline
# published. v3.1 copied the pipeline's output into the package and
# called it evidence, so a 200-day average built on 199 bars validated
# cleanly.

def _series(members, key):
    out = []
    for m in members:
        v = m.get("value")
        if not isinstance(v, dict) or v.get(key) is None:
            return None
        out.append(float(v[key]))
    return out


def _mean(xs):
    return sum(xs) / float(len(xs))


def _recompute(slug, members, bench=None):
    """Return (value, note) recomputed from delivered bars, or (None, why)."""
    n = DECLARED_WINDOW.get(slug)
    if n and len(members) != n:
        return None, ("window declares %d sessions, package delivers %d"
                      % (n, len(members)))
    closes = _series(members, "c")
    if slug in ("ma9", "ma21", "ma20", "ma50", "ma200"):
        if closes is None:
            return None, "a close is missing from the delivered window"
        return round(_mean(closes), 2), None
    if slug in ("support60", "lo52"):
        if closes is None:
            return None, "a close is missing from the delivered window"
        return round(min(closes), 2), None
    if slug in ("resistance60", "hi52"):
        if closes is None:
            return None, "a close is missing from the delivered window"
        return round(max(closes), 2), None
    if slug == "atr14":
        hs, ls = _series(members, "h"), _series(members, "l")
        if None in (hs, ls, closes):
            return None, "an OHLC field is missing from the delivered window"
        trs = [max(hs[i] - ls[i], abs(hs[i] - closes[i - 1]),
                   abs(ls[i] - closes[i - 1])) for i in range(1, len(closes))]
        return round(_mean(trs[-14:]), 2), None
    if slug == "rsi14":
        if closes is None:
            return None, "a close is missing from the delivered window"
        d = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [x if x > 0 else 0.0 for x in d]
        losses = [-x if x < 0 else 0.0 for x in d]
        if len(d) < 14:
            return None, "fewer than 14 changes in the delivered window"
        ag, al = _mean(gains[:14]), _mean(losses[:14])
        for i in range(14, len(d)):
            ag = (ag * 13 + gains[i]) / 14.0
            al = (al * 13 + losses[i]) / 14.0
        return (100.0 if al == 0 else
                round(100.0 - 100.0 / (1.0 + ag / al), 1)), None
    if slug == "base_tightness_pct":
        if closes is None:
            return None, "a close is missing from the delivered window"
        lo = min(closes)
        if not lo:
            return None, "the window floor is zero"
        return round(100.0 * (max(closes) - lo) / lo, 1), None
    if slug == "rel_volume":
        vs = _series(members, "v")
        if vs is None or not sum(vs[:-1]):
            return None, "volumes missing from the delivered window"
        return round(vs[-1] / _mean(vs[:-1]), 2), None
    if slug in ("last_close", "intraday_last"):
        if closes is None or len(closes) != 1:
            return None, "the named bar is not in the package"
        return round(closes[0], 2), None
    if slug == "rs_vs_spy":
        if closes is None:
            return None, "a close is missing from the issuer window"
        if not bench:
            return None, ("the benchmark window is not embedded, so the "
                          "second leg cannot be reproduced")
        b = [r.get("value") for r in bench]
        if any(x is None for x in b) or len(b) < 2:
            return None, "benchmark closes are missing or null"
        mine = 100.0 * (closes[-1] / closes[0] - 1)
        bench_r = 100.0 * (float(b[-1]) / float(b[0]) - 1)
        return round(mine - bench_r, 1), None
    return None, None                      # no rule; not a failure


def _recompute_operands(slug, ops):
    """Redo the arithmetic of a two- or four-operand figure from the
    operand values already resolved in this package."""
    vals = [o.get("value") for o in ops if o.get("resolved")]
    nums = [float(v) for v in vals if isinstance(v, (int, float))]
    if slug == "market_cap" and len(nums) == 2:
        return round(nums[0] * nums[1], 2), None
    if slug == "pe_trailing" and len(nums) == 2 and nums[1]:
        return round(nums[0] / nums[1], 1), None
    if slug in ("net_margin", "gross_margin") and len(nums) >= 2:
        # operands arrive as (numerator, revenue) after the CALC ref
        num = [n for n in nums]
        if len(num) >= 2 and num[-1]:
            return round(100.0 * num[-2] / num[-1], 1), None
    if slug == "revenue_yoy" and len(nums) >= 2 and nums[-1]:
        return round(100.0 * (nums[-2] / nums[-1] - 1), 1), None
    if slug == "eps_ttm" and len(nums) >= 4:
        return round(sum(nums[-4:]), 2), None
    if slug == "fcf" and len(nums) == 2:
        return round(nums[0] - nums[1], 0), None
    if slug == "atr14_pct" and len(nums) == 2 and nums[1]:
        return round(100.0 * nums[0] / nums[1], 2), None
    if slug == "pct_below_hi52" and len(nums) == 2 and nums[0]:
        return round(100.0 * (nums[0] - nums[1]) / nums[0], 1), None
    if slug == "session_change" and len(ops) == 2:
        # The second operand is a whole bar, so its close has to be dug
        # out rather than read off the operand value like a scalar.
        last, prev = ops[0].get("value"), ops[1].get("value")
        if isinstance(prev, dict):
            prev = prev.get("c")
        if isinstance(last, (int, float)) and isinstance(prev, (int, float)) \
                and prev:
            return round(100.0 * (float(last) - float(prev)) / float(prev),
                         2), None
    return None, None


def _reproduce(cid, rec, records, calcs, benchmark, operands=None):
    """Attach the independent recomputation to a calculation record."""
    slug = cid[5:] if cid.startswith("CALC-") else cid
    members, bench, ranges = [], [], 0
    for ref in (rec.get("inputs") or []):
        r = str(ref)
        if ".." in r:
            ranges += 1
            lo, hi = r.split("..", 1)
            pre = "SPY-" if lo.startswith("SPY-") else "BAR-"
            pool = benchmark if pre == "SPY-" else records
            got = sorted(k for k in pool if k.startswith(pre)
                         and lo <= k <= hi)
            if pre == "SPY-":
                bench = [pool[k] for k in got]
            else:
                members = [pool[k] for k in got]
        elif r.startswith("BAR-") and r in records:
            members.append(records[r])
        elif r.startswith("INTRADAY-") and r in records:
            members.append(records[r])
    got, why = _recompute(slug, members, bench)
    if got is None and why is None:
        got, why = _recompute_operands(slug, operands or [])
    published = rec.get("output")
    out = {"window_declared": DECLARED_WINDOW.get(slug),
           "window_delivered": len(members) or None,
           "benchmark_sessions_delivered": len(bench) or None,
           "recomputed": got, "recompute_note": why}
    if got is None:
        out["reproducible"] = None if why is None else False
        if why is None:
            out["recompute_note"] = ("no independent rule for this slug; "
                                     "operands are named but the arithmetic "
                                     "is not re-derived here")
    else:
        tol = 0.011 if isinstance(published, float) else 0.011
        ok = (isinstance(published, (int, float))
              and abs(float(published) - got) <= tol)
        out["reproducible"] = bool(ok)
        if not ok:
            out["recompute_note"] = (
                "published %s but the delivered bars give %s"
                % (published, got))
    return out


# ── build ───────────────────────────────────────────────────────────────

def build(snap, view, prov=None, led=None, artifacts=None,
          suppressed=None):
    prov = prov or {}
    suppressed = set(suppressed or [])
    records, calcs, notes = {}, {}, []
    ld = led.to_dict() if led is not None else {}

    def put(r):
        records[r["evidence_id"]] = r

    # Issuer bars ONLY. The benchmark reference used to live in this
    # same ledger section, and because "EXT-" sorts after "BAR-" the
    # tail slice swallowed it — 199 issuer bars behind formulas that
    # declared 200 and 252.
    bars = sorted([b for b in (ld.get("market_bars") or [])
                   if str(b.get("id", "")).startswith("BAR-")],
                  key=lambda b: b["id"])
    partial_session = (snap.get("levels") or {}).get("partial_session")         or (prov.get("_mk") or {}).get("partial_session")
    last_completed = (prov.get("_mk") or {}).get("last_completed_session")
    completed = [b for b in bars
                 if not (last_completed and b["id"][4:] > last_completed)]
    kept = completed[-BARS_FOR_INDICATORS:]
    for b in kept:
        put(_rec(b["id"], "market_bar", source="Yahoo Finance daily bars",
                 value={"o": b.get("open"), "h": b.get("high"),
                        "l": b.get("low"), "c": b.get("close"),
                        "v": b.get("volume")},
                 unit="USD", period={"session": b["id"][4:],
                                     "complete": True},
                 timestamps={"observed": b["id"][4:]},
                 raw=b, immutable={"kind": "vendor_series",
                                   "immutable": False,
                                   "ref": (prov.get("_mk") or {}).get(
                                       "series_id")}))
    if len(completed) > len(kept):
        notes.append("%d of %d completed sessions carried: the last %d, "
                     "which satisfies the longest declared window (252 "
                     "sessions, for the 52-week closing high and low)."
                     % (len(kept), len(completed), BARS_FOR_INDICATORS))

    # The open session is an observation, not a bar. It is carried under
    # its own id so nothing can fold it into a completed-session window.
    mk = prov.get("_mk") or {}
    intr = mk.get("intraday")
    if intr:
        put(_rec("INTRADAY-%s" % intr["session"], "intraday_observation",
                 source="Yahoo Finance intraday",
                 value={"o": intr.get("open"), "h": intr.get("high"),
                        "l": intr.get("low"), "c": intr.get("last"),
                        "v": intr.get("volume")},
                 unit="USD",
                 period={"session": intr["session"], "complete": False},
                 timestamps={"observed": view.get("quote_time_utc")},
                 raw=intr,
                 extra={"partial": True,
                        "note": "the session was open when this was read; "
                                "close, high, low and volume are not final"}))

    # Benchmark closes, embedded. RS vs SPY cannot be checked against a
    # label, and the placeholder this replaces carried null OHLCV while
    # the validator marked its operand resolved.
    benchmark = {}
    for b in (ld.get("benchmark_bars") or []):
        r = _rec(b["id"], "benchmark_bar", source="Yahoo Finance daily bars",
                 value=b.get("close"), unit="USD",
                 period={"session": b.get("session"), "complete": True},
                 timestamps={"observed": b.get("session")}, raw=b,
                 immutable={"kind": "vendor_series", "immutable": False,
                            "ref": b.get("series_id")})
        benchmark[b["id"]] = r
        put(r)

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

    # Earnings-exhibit figures are evidence, not decoration: each becomes
    # its own record with the accession and URL behind it.
    ex = snap.get("exhibit") or {}
    for kind, block in (("reported", ex.get("reported") or {}),
                        ("guidance", ex.get("guidance") or {})):
        for key, val in block.items():
            eid = "EXH-%s-%s" % (kind[:3].upper(), key)
            if kind == "reported":
                value, unit = val.get("value"), val.get("unit")
                period = {"label": ex.get("period_label"),
                          "kind": "reported quarter"}
            else:
                value = {"low": val.get("low"), "midpoint": val.get("midpoint"),
                         "high": val.get("high")}
                unit = val.get("unit")
                period = {"label": ex.get("guidance_period"),
                          "kind": "guided quarter"}
            disp = None
            if kind == "guidance":
                lo = guidance_display(key, unit, val.get("low"))
                hi = guidance_display(key, unit, val.get("high"))
                disp = lo if lo == hi else "%s - %s" % (lo, hi)
            else:
                disp = guidance_display(key, unit, val.get("value"))
            put(_rec(eid, "exhibit_%s" % kind,
                     source="SEC 8-K Item 2.02 Exhibit 99.1",
                     url=ex.get("url"), accession=ex.get("accession"),
                     value=value, unit=unit, period=period,
                     timestamps={"accepted": ex.get("accepted")},
                     disposition=(ADMITTED
                                  if ex.get("disposition") == "ADMITTED"
                                  else AVAILABLE_NOT_INGESTED),
                     reason=ex.get("reason"), raw=val,
                     extra={"issuer_label": val.get("label"),
                            "issuer_raw": val.get("raw"),
                            "basis": val.get("basis"),
                            "display": disp,
                            "display_rule": "issuer-stated precision; "
                                            "trailing zeros stripped"}))

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
        rec.update(_reproduce(c["id"], c, records, calcs, benchmark,
                              operands=rec.get("operands")))
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

    # record_hash is stamped last, over the finished record, so it can be
    # recomputed from evidence.json with nothing else in hand.
    for _rid, _r in records.items():
        _r.pop("record_hash", None)
        _r["record_hash"] = record_hash(_r)

    coverage = dict((prov.get("coverage") or {}))
    # The exhibit was parsed, so the coverage line that says non-GAAP is
    # unavailable is now contradicted by the report itself.
    if (snap.get("exhibit") or {}).get("disposition") == "ADMITTED":
        coverage["non_gaap_margin"] = (
            "ADMITTED - parsed from 8-K Item 2.02 Exhibit 99.1 (%s)"
            % (snap.get("exhibit") or {}).get("accession"))

    # "displayed" meant three different things depending on who was
    # asking. Each population now says which artifact it counts.
    def _scope(domain, pop):
        core = {"news": M.CORE_NEWS_SHOWN,
                "ownership": 0}.get(domain)
        apx = {"news": 0,
               "ownership": pop.get("records_in_window")
               or pop.get("records_admitted"),
               "social": len(M.presentable_samples(
                   (snap.get("sentiment") or {})
                   .get("sample_records") or []))}.get(domain)
        out = dict(pop)
        legacy = out.pop("records_displayed", None)
        adm = out.get("records_admitted")
        out.update({
            "admitted": adm,
            "shown_core": (min(core, adm) if (core is not None
                                              and adm is not None) else core),
            "shown_appendix": apx,
            "available_evidence": len([r for r in records.values()
                                       if r.get("evidence_type", "")
                                       .startswith(domain[:4])]) or None,
            "scope_note": ("shown_core = rows in the four-page brief; "
                           "shown_appendix = rows in the appendix PDF; "
                           "available_evidence = records in this file; "
                           "admitted = rows that passed their gate"),
            "legacy_records_displayed": legacy,
        })
        return out

    populations = {d: _scope(d, p) for d, p in
                   (ld.get("counts") or {}).items()}

    # Every record is hash-verified, not a sample of thirty.
    _hashed = len([r for r in records.values() if r.get("record_hash")])
    _selfcheck = {"total": len(records),
                  "matched": len([1 for r in records.values()
                                  if r.get("record_hash")
                                  == record_hash(r)]),
                  "mismatched": sorted(rid for rid, r in records.items()
                                       if r.get("record_hash")
                                       != record_hash(r))}
    hash_report = {
        "algorithm": "sha256",
        "hash_version": HASH_VERSION,
        "canonicalization": HASH_CANONICALIZATION,
        "source_hash_note": SOURCE_HASH_NOTE,
        "records_total": len(records),
        "records_hashed": _hashed,
        "coverage_pct": round(100.0 * _hashed / max(1, len(records)), 1),
        "recompute": _selfcheck,
        "recompute_pct": round(100.0 * _selfcheck["matched"]
                               / max(1, len(records)), 1),
        "note": ("record_hash is recomputable from this file alone. "
                 "source_raw_hash is not: it covers the upstream payload. "
                 "v3.2 documented one and published the other, so none of "
                 "the 497 hashes reproduced under its own stated method."),
        "ledger_audit_sample": (ld.get("hash_verification") or {}),
    }
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
        "populations": populations,
        "count_statements": ld.get("count_statements") or {},
        "source_coverage": coverage,
        "hash_verification": hash_report,
        "benchmark_sessions": len(benchmark),
        "completed_sessions_carried": len(kept),
        "intraday_observation": bool(intr),
        "notes": notes,
        "view": _jsonable(view),
        "snapshot_schema": snap.get("schema"),
        "record_count": len(records),
        "calculation_count": len(calcs),
        "calculation_coverage": _calc_coverage(calcs),
    }
    if artifacts:
        pkg["artifacts"] = artifacts
    return pkg


# Narrative inputs are not arithmetic and cannot be "reproduced".
# Reporting "17 of 22" without saying what the other five were invited
# the reader to assume five figures had quietly failed.
NONNUMERIC_EXEMPT = {
    "CALC-social-summary": "a population summary object, not a scalar",
    "REC-profile": "vendor profile text carried as a recommendation input",
    "REC-business_overview": "narrative business description",
    "REC-business_quality": "qualitative assessment, INFERRED",
    "REC-setup_quality": "qualitative assessment, INFERRED",
}


def _calc_coverage(calcs):
    numeric_ok, numeric_bad, exempt = [], [], []
    for cid, c in sorted((calcs or {}).items()):
        if cid in NONNUMERIC_EXEMPT or not isinstance(
                c.get("result_unrounded"), (int, float)):
            exempt.append({"calculation_id": cid,
                           "reason": NONNUMERIC_EXEMPT.get(
                               cid, "result is not a scalar")})
        elif c.get("reproducible"):
            numeric_ok.append(cid)
        else:
            numeric_bad.append({"calculation_id": cid,
                                "note": c.get("recompute_note")})
    return {"numeric_reproduced": len(numeric_ok),
            "numeric_failed": len(numeric_bad),
            "nonnumeric_exempt": len(exempt),
            "total": len(calcs or {}),
            "failed_detail": numeric_bad,
            "exempt_detail": exempt,
            "reproduced_ids": numeric_ok}


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
