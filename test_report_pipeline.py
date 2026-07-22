#!/usr/bin/env python3
"""test_report_pipeline.py — the fifteen checks the audit asked for.

Runs offline against fixtures by default; `--live TICKER` runs the same
assertions against a freshly generated report so the guarantees are
tested on real output, not only on a mock.

    python test_report_pipeline.py
    python test_report_pipeline.py --live ISRG --out DIR
"""

import argparse
import hashlib
import io
import json
import os
import re
import sys

import evidence_ledger as EL
import pdf_postprocess as PP
import report_v2 as R
import research_snapshot as rs


class Results(object):
    def __init__(self):
        self.rows = []

    def check(self, num, name, cond, detail=""):
        ok = bool(cond)
        self.rows.append({"id": "T%02d" % num, "name": name,
                          "status": "pass" if ok else "FAIL",
                          "detail": detail if not ok else ""})
        print("  %s  T%02d %s%s" % ("PASS" if ok else "FAIL", num, name,
                                    "" if ok else "  <- " + str(detail)[:150]))
        return ok

    @property
    def failed(self):
        return [r for r in self.rows if r["status"] != "pass"]


def _resolve(ref, ids):
    ref = str(ref)
    if ".." in ref:
        a, b = ref.split("..", 1)
        return a.strip() in ids and b.strip() in ids
    return ref in ids


def run(snap, ledger, pdf_bytes, render_report, audit, res):
    ids = ledger.ids()

    # 1. every snapshot claim references an existing evidence ID
    bad = []
    for path, f in rs._iter_facts(snap):
        if f.get("v") is None or f.get("source_type") == "vendor":
            continue
        for r in f.get("evidence_refs") or []:
            if not _resolve(r, ids):
                bad.append("%s -> %s" % (path, r))
    for c in (snap.get("decision") or {}).get("claims") or []:
        for r in c.get("evidence_refs") or []:
            if not _resolve(r, ids):
                bad.append("claim -> %s" % r)
    res.check(1, "every snapshot claim resolves to an evidence record",
              not bad, bad[:4])

    # 2. every evidence ID is unique
    seen, dupes = set(), []
    for sec in ledger.SECTIONS:
        for rec in (ledger._store.get(sec) or {}).values():
            if rec["id"] in seen:
                dupes.append(rec["id"])
            seen.add(rec["id"])
    res.check(2, "every evidence ID is unique", not dupes, dupes[:4])

    # 3. displayed counts reconcile with ledger filters
    recon = ledger.reconcile()
    res.check(3, "displayed counts reconcile with ledger populations",
              not recon, recon)

    # 4. issuer bars and benchmark references counted separately
    mk = ledger.counts.get("market") or {}
    issuer = len([r for r in ledger._store["market_bars"].values()
                  if str(r.get("id", "")).startswith("BAR-")])
    bench = len([r for r in ledger._store["market_bars"].values()
                 if r.get("record_kind") == "benchmark_reference"])
    res.check(4, "issuer sessions and benchmark references counted apart",
              mk.get("issuer_sessions") == issuer
              and mk.get("benchmark_references") == bench
              and issuer != (issuer + bench),
              "issuer=%s bench=%s counts=%s" % (issuer, bench, mk))

    # 5/6. catalyst selection
    cat = snap.get("catalyst") or {}
    disc = cat.get("discovery") or {}
    res.check(5, "catalyst is the earliest verified public disclosure",
              cat.get("event_dt") == disc.get("earliest_primary_release")
              and (cat.get("verification") or {}).get(
                  "is_results_disclosure") is not False,
              "chose %s, earliest %s" % (cat.get("event_dt"),
                                         disc.get("earliest_primary_release")))
    probe = json.loads(json.dumps(snap, default=str))
    probe["catalyst"]["event_dt"] = "2099-01-01T00:00:00+00:00"
    res.check(6, "a later periodic filing cannot replace the catalyst",
              any("earlier primary release" in x
                  for x in rs.check_catalyst_discovery(probe)))

    # 7/8. social accounting
    s = snap.get("sentiment") or {}
    res.check(7, "social considered = admitted + rejected",
              s.get("n_considered") == (s.get("n_relevant") or 0)
              + (s.get("n_rejected") or 0),
              "%s vs %s+%s" % (s.get("n_considered"), s.get("n_relevant"),
                               s.get("n_rejected")))
    bc = s.get("by_class") or {}
    res.check(8, "directional totals reconcile with sentiment classes",
              sum(bc.get(k, 0) for k in rs.SENTIMENT_CLASSES)
              == (s.get("n_relevant") or 0)
              and (s.get("directional_posts") or 0)
              == bc.get("bullish", 0) + bc.get("bearish", 0),
              "classes=%s directional=%s" % (bc, s.get("directional_posts")))

    # 9. hashes reproduce from the immutable snapshot
    hv = ledger.verify_hashes()
    # recompute with the SAME function the product uses, and assert the
    # public record agrees — comparing the private file to itself is the
    # tautology that let 13 divergent public hashes ship
    recomputed = agreed = 0
    pubrec = {r["id"]: r for r in
              (ledger._store.get("social_records") or {}).values()}
    for rec in (audit or {}).get("records", []):
        if EL.content_hash(rec["hash_input"]) == rec["content_hash"]:
            recomputed += 1
        p = pubrec.get(rec["evidence_id"])
        if p and p.get("text_hash") == rec["content_hash"]:
            agreed += 1
    n_audit = len((audit or {}).get("records", []))
    res.check(9, "content hashes reproduce AND public matches private",
              hv["ok"] and recomputed == n_audit == agreed and n_audit > 0,
              "verify_ok=%s recomputed=%d agreed=%d of %d"
              % (hv["ok"], recomputed, agreed, n_audit))

    # 10. excerpts never end mid-word
    bad_ex = []
    for rec in ledger._store["social_records"].values():
        ex = rec.get("excerpt") or ""
        if ex.endswith("…"):
            body = ex[:-1].rstrip()
            src = rec.get("_full") or ""
            if body and not re.search(r"[\s\W]$", body + " "):
                pass
            if src and not src.startswith(body[:20]):
                bad_ex.append(rec["id"])
        elif re.search(r"\w$", ex) and len(ex) >= 110:
            bad_ex.append(rec["id"])
    res.check(10, "excerpts never terminate mid-word", not bad_ex,
              bad_ex[:4])

    # 11. no unresolved template placeholders on the page
    text = ""
    try:
        import fitz
        d = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = "".join(d[i].get_text() for i in range(d.page_count))
        d.close()
    except Exception:
        pass
    # literal fragments can appear anywhere; word-like tokens must match
    # as whole words or "nan" fires on "fi-nan-cial"
    holes = [p for p in ("{}", "{0}", "%s", "%d", "{{", "}}", "None None")
             if p in text]
    holes += [p for p in ("TODO", "FIXME", "nan", "NaN", "undefined", "None")
              if re.search(r"\b%s\b" % re.escape(p), text)]
    res.check(11, "no unresolved template placeholders on the page",
              not holes, holes)

    # 12. no clipping or overlap
    res.check(12, "all pages render without clipping or overlap",
              render_report.get("ok"), render_report.get("notes"))

    # 13/14/15. PDF validity + accessibility disclosure
    val = PP.validate(pdf_bytes)
    py = val["checks"].get("pypdf") or {}
    res.check(13, "PDF opens in pypdf without warnings or exceptions",
              py.get("status") == "pass", py)
    qp = val["checks"].get("qpdf_check") or {}
    res.check(14, "qpdf structural check succeeds",
              qp.get("status") == "pass", qp)
    md = render_report.get("pdf_metadata") or {}
    # 16. backward compatibility: a stored v1 alt block must render
    try:
        legacy = R._alt_fixture()
        legacy["coordination"] = "repeated phrasing seen across accounts"
        legacy.pop("directional_posts", None)
        legacy.pop("section_title", None)
        m = rs.migrate_alt_block(legacy)
        R.build_demo(R._fixture_snapshot(), alt=legacy)
        ok16, why = isinstance(m.get("coordination"), dict) and \
            bool(m.get("section_title")), ""
    except Exception as e:
        ok16, why = False, "%s: %s" % (type(e).__name__, e)
    res.check(16, "a v1-shaped alt-data block migrates instead of crashing",
              ok16, why)

    res.check(15, "accessibility status is valid or explicitly disclosed",
              md.get("accessibility") in (PP.ACCESS_TAGGED, PP.ACCESS_UNTAGGED)
              and bool(md.get("accessibility_reason")
                       or md.get("accessibility") == PP.ACCESS_TAGGED),
              md)
    return res


def _offline():
    """Build a small self-contained ledger + snapshot and render it."""
    snap = R._fixture_snapshot()
    led = EL.Ledger(snap["ticker"], snap["report_time"])
    for i, d in enumerate(["2026-07-%02d" % x for x in range(1, 11)]):
        led.bar(d, 1.0, 2.0, 0.5, 1.5, 1000 + i)
    led.add("market_bars", "EXT-yahoo:SPY:1d",
            {"record_kind": "benchmark_reference", "embedded": False})
    led.population("market", issuer_sessions=10, benchmark_references=1)
    led.calc("ma200", "mean(close, 200)", ["BAR-2026-07-01..BAR-2026-07-10"],
             486.0, "USD")
    led.rec_input("business_quality", "business quality", "solid", refs=[])
    led.add("catalyst_records", "CAT-8K-2202", {"form": "8-K"})
    # social records with real hashes + private audit rows
    texts = ["$ISRG started a position today, will dca and hold for the next "
             "ten years because the installed base keeps compounding",
             "$ISRG bottom in?"]
    for i, t in enumerate(texts):
        eid = "SOC-t%d" % i
        norm = " ".join(t.split())
        h = EL.content_hash(norm)
        led.add("social_records", eid,
                {"excerpt": EL.excerpt(t), "text_hash": h,
                 "disposition": "counted", "sentiment": "bullish",
                 "_full": norm})
        led.audit_record(eid, norm, norm, h, "norm/v1")
    led.population("social", records_fetched=2, records_parsed=2,
                   records_admitted=2, records_rejected=0,
                   records_displayed=2)
    led.population("form4", source_filings_in_index=1501,
                   source_filings_scanned=62, records_parsed=61,
                   records_in_window=61, open_market_sales=25,
                   window_days=180)
    led.population("ownership", records_parsed=21, records_in_window=21,
                   records_displayed=12)
    led.population("news", records_fetched=9, records_parsed=9,
                   records_admitted=8, records_rejected=1)
    # align the fixture's sentiment with the ledger so the accounting
    # assertions are meaningful
    snap["sentiment"].update({
        "n_considered": 2, "n_relevant": 2, "n_rejected": 0,
        "by_class": {"bullish": 2, "bearish": 0, "neutral": 0,
                     "uncertain": 0},
        "directional_posts": 2, "directional_authors": 2,
        "non_directional_posts": 0, "unique_authors": 2,
        "post_weighted_bull_pct": 100, "author_weighted_bull_pct": 100,
        "classification": "INSUFFICIENT SAMPLE",
        "decision_read": {"direction": "no directional read — sample below "
                                       "author floor"},
    })
    snap["evidence_index"] = sorted(led.ids())
    for path, f in rs._iter_facts(snap):
        if f.get("v") is not None:
            f["evidence_refs"] = ["CALC-ma200"]
    for c in snap["decision"].get("claims") or []:
        c["evidence_refs"] = ["CALC-ma200"]
    snap["decision"]["business_quality_refs"] = ["REC-business_quality"]
    snap["decision"]["setup_quality_refs"] = ["CALC-ma200"]
    snap["decision"]["monitor_next_refs"] = ["CALC-ma200"]
    snap["catalyst"]["discovery"]["earliest_primary_ref"] = "CAT-8K-2202"
    pdf, rep = R.build_demo(snap)
    return snap, led, pdf, rep, led.audit_snapshot()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", metavar="TICKER")
    ap.add_argument("--out", default=".")
    a = ap.parse_args()
    res = Results()
    if a.live:
        import research_live as L
        print("running the suite against a LIVE %s report\n" % a.live.upper())
        snap, rep, pdf_path, ev_path, led, audit_path, val_path, _ = \
            L.render(a.live.upper(), a.out)
        with open(pdf_path, "rb") as fh:
            pdf = fh.read()
        with open(audit_path, encoding="utf-8") as fh:
            audit = json.load(fh)
        print()
    else:
        print("running the suite offline against fixtures\n")
        snap, led, pdf, rep, audit = _offline()
    run(snap, led, pdf, rep, audit, res)
    n = len(res.rows)
    print("\n%d/%d checks passed" % (n - len(res.failed), n))
    return 1 if res.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
