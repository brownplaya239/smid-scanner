#!/usr/bin/env python3
"""report_v4_mutation.py — prove every v4 check bites.

A validator that has never failed is a validator nobody has tested. This
takes a good v4 package — a view and rendered PDFs that pass every check —
and mutates it once per check so that exactly that check goes red, then
confirms it did. summary() reports which checks were proven and is folded
into the validation output, so a run cannot claim ok=True unless the gate
has just been shown to work.

    python report_v4_mutation.py
"""

import copy
import glob
import io
import os
import pickle

import report_v4 as R4
import report_v4_model as V4
import report_v4_validate as VV
import report_v4_event as EV

FAIL, WARN_S = VV.FAIL, VV.WARN_S

# A free-tier-plus consensus feed, shaped like estimates_provider output,
# so the sourced-consensus and sourced-target checks are exercised.
EST = {"configured": True, "provider": "finnhub",
       "recommendation": {"strong_buy": 12, "buy": 20, "hold": 4, "sell": 1,
                          "strong_sell": 0, "score": 1.8, "band": "Buy",
                          "as_of": "2026-07-01"},
       "price_target": {"mean": 120.0, "high": 150.0, "low": 90.0,
                        "n_analysts": 28, "as_of": "2026-07-18"},
       "coverage": {"price_target": "ok", "eps_estimate": "ok"},
       "surprises": [{"period": "2026-06-30", "actual": 3.9,
                      "estimate": 3.75, "surprise_pct": 4.0}]}


def _base():
    """A snapshot with a clean 52-week band and positive EPS injected, so
    the consensus, target, band and split checks all have inputs and all
    pass before mutation.

    fixtures_v4/mutation_base.pkl is the COMMITTED fixture and the one CI
    sees — .snapcache/ is gitignored, and depending on it silently made
    every CI run report "no ratable cached snapshot to mutate", which
    fails the gate and blocks the upload (the 2026-07-27 SG lookup). The
    local cache is only a fallback for development."""
    paths = (sorted(glob.glob("fixtures_v4/*.pkl"))
             + sorted(glob.glob(".snapcache/*.pkl")))
    snap = None
    for p in paths:
        obj = pickle.load(io.open(p, "rb"))
        s = obj[0] if isinstance(obj, tuple) else obj
        cat = s.get("catalyst") or {}
        # a name the gate lets rate (not DATA HOLD), so ratings are present
        ev = EV.event_state(cat, s.get("exhibit"),
                            report_time=s.get("report_time"))
        if ev.get("rating_allowed"):
            snap = s
            break
    if snap is None:
        return None, None, None
    snap = copy.deepcopy(snap)
    snap["levels"] = dict(snap.get("levels") or {},
                          resistance_major={"v": 200.0},
                          support_major={"v": 80.0},
                          price_used={"v": 100.0})
    snap["fundamentals"] = dict(snap.get("fundamentals") or {},
                                eps_ttm={"v": 2.0})
    snap["price"] = dict(snap.get("price") or {}, last={"v": 100.0})
    view = V4.build(snap, estimates=EST,
                    peers={"rows": [{"ticker": "CRM", "pe": 40.0}],
                           "source": "finnhub"})
    return snap, view, EST


def _tiny_pdf(text):
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter
    b = io.BytesIO()
    c = canvas.Canvas(b, pagesize=letter)
    c.drawString(72, 720, text)
    c.showPage()
    c.save()
    return b.getvalue()


def _view_fails(view, snap, est, cid):
    """True if check_view flags `cid` (FAIL, or WARN for the warn-only
    variant check) on this mutated view."""
    for c in VV.check_view(view, snap, est):
        if c["check_id"] == cid and c["status"] in (FAIL, WARN_S):
            return True
    return False


def summary():
    snap, view, est = _base()
    if view is None:
        return {"all_checks_proven": False,
                "note": "no ratable cached snapshot to mutate"}

    proven, unproven = [], []

    def model(cid, mutate):
        v = copy.deepcopy(view)
        s = copy.deepcopy(snap)
        s2 = mutate(v, s)
        (proven if _view_fails(v, s2 or s, est, cid)
         else unproven).append(cid)

    # ── each mutation trips exactly one model check ─────────────────────
    def m_event(v, s):
        v["event"] = dict(v["event"], rating_allowed=False)
        v["ratings"]["fundamental"]["available"] = True
    model("EVENT_RATING_CONSISTENCY", m_event)

    def m_flash(v, s):
        v["event"] = dict(v["event"], state=EV.DATA_HOLD,
                          rating_allowed=False)
        v["ratings"]["fundamental"] = {"available": False, "reason": "x"}
        v["ratings"]["target"] = {"available": False, "reason": "x"}
        v["flash"] = None
    model("FLASH_ON_DATA_HOLD", m_flash)

    def m_next(v, s):
        v["event"] = dict(v["event"], state=EV.RESULTS_RELEASED,
                          rating_allowed=True, next_earnings_is_pending=True)
    model("NO_NEXT_AFTER_RELEASE", m_next)

    def m_consensus(v, s):
        v["ratings"]["fundamental"]["grade"] = "DERIVED"     # not observed
    model("CONSENSUS_SOURCED", m_consensus)

    def m_target(v, s):
        v["ratings"]["target"]["as_of"] = None               # undated
    model("TARGET_SOURCED", m_target)

    def m_withheld(v, s):
        v["financials"]["saas_kpis"] = {"available": False}  # no reason
    model("WITHHELD_LABELLED", m_withheld)

    def m_split(v, s):
        # price/eps = 50; set trailing pe to 100 -> a clean 2x split gap
        v["valuation"]["pe_trailing"] = 100.0
    model("SPLIT_MAGNITUDE", m_split)

    def m_circular(v, s):
        # reintroduce the v4.0 tautology: scenario prices ARE the 52-week
        # price range, which is what SPLIT the range by EPS and back.
        lv = s.setdefault("levels", {})
        lv["resistance_major"] = {"v": 200.0}
        lv["support_major"] = {"v": 80.0}
        v["valuation"]["forward_scenarios"] = {
            "available": True, "bull": {"price": 200.0},
            "bear": {"price": 80.0}, "base": {"price": 100.0}}
    model("VALUATION_NON_CIRCULAR", m_circular)

    def m_uningested(v, s):
        # a full post-release report whose FRESH release exhibit was never
        # parsed. The check only blocks inside HOLD_WINDOW_DAYS (a stale
        # unparsed exhibit is a labelled gap the gate allows), so the
        # mutation stamps acceptance "now" to be unambiguously fresh.
        import datetime as _d
        v["event"] = dict(v["event"], state=EV.POST_CALL_UNVERIFIED)
        v["flash"] = None
        s["exhibit"] = {"disposition": "AVAILABLE_NOT_INGESTED",
                        "reason": "tables not parsed",
                        "accepted": _d.datetime.now(
                            _d.timezone.utc).isoformat()}
    model("PRIMARY_RELEASE_INGESTED", m_uningested)

    def m_variant(v, s):
        v["variant"] = {"available": True, "text": "x", "grade": "OBSERVED"}
    model("VARIANT_IS_DERIVED", m_variant)

    # ── pdf checks ──────────────────────────────────────────────────────
    good_core = R4.build_core(snap, view)
    good_apx = R4.build_appendix(snap, view, estimates=est)

    # CORE_PAGE_COUNT: a full 6-page core presented as a flash (want 1)
    flashy = copy.deepcopy(view)
    flashy["flash"] = {"headline": "x", "body": "y"}
    if any(c["check_id"] == "CORE_PAGE_COUNT" and c["status"] == FAIL
           for c in VV.check_pdfs(good_core, good_apx, flashy)):
        proven.append("CORE_PAGE_COUNT")
    else:
        unproven.append("CORE_PAGE_COUNT")

    # HTML_ENTITIES + GLYPH_INTEGRITY: a one-page core carrying each defect
    ent = VV.check_pdfs(_tiny_pdf("Revenue rose 5 &amp; margins held"),
                        good_apx, {"flash": True})
    (proven if any(c["check_id"] == "HTML_ENTITIES" and c["status"] == FAIL
                   for c in ent) else unproven).append("HTML_ENTITIES")

    # mojibake the extractor surfaces verbatim (reportlab renders these
    # Latin-1 bytes, unlike the replacement char, which it silently drops)
    gly = VV.check_pdfs(_tiny_pdf("the issuerâ€™s guidance"),
                        good_apx, {"flash": True})
    (proven if any(c["check_id"] == "GLYPH_INTEGRITY" and c["status"] == FAIL
                   for c in gly) else unproven).append("GLYPH_INTEGRITY")

    # APPENDIX_PRESENT: a full core with an empty appendix
    if any(c["check_id"] == "APPENDIX_PRESENT" and c["status"] == FAIL
           for c in VV.check_pdfs(good_core, b"", view)):
        proven.append("APPENDIX_PRESENT")
    else:
        unproven.append("APPENDIX_PRESENT")

    # HTML_ENTITIES_APPENDIX: a real 6-page core, a defective appendix
    ea = VV.check_pdfs(good_core,
                       _tiny_pdf("filed 10-K &amp; 10-Q"), view)
    (proven if any(c["check_id"] == "HTML_ENTITIES_APPENDIX"
                   and c["status"] == FAIL for c in ea)
     else unproven).append("HTML_ENTITIES_APPENDIX")

    # ── v4.1 checks: rendered document + metric period ──────────────────
    def model_period(cid, mutate):
        s = copy.deepcopy(snap)
        mutate(s)
        (proven if any(c["check_id"] == cid and c["status"] == FAIL
                       for c in VV.check_view(view, s, est))
         else unproven).append(cid)

    def m_period(s):
        # cash flow from a different quarter than revenue — the v4.0 defect.
        # The metric must CARRY a value: a value-less fact renders "n/a",
        # asserts no period, and is exempt from the check by design.
        s["fundamentals"] = dict(s.get("fundamentals") or {},
                                 operating_cash_flow={"v": 1e9, "value": 1e9,
                                                      "period_end": "2020-01-01"})
    model_period("METRIC_PERIOD_CONSISTENCY", m_period)

    def rendered(cid, ctext, snap_over):
        s = dict(snap)
        s.update(snap_over or {})
        (proven if any(c["check_id"] == cid and c["status"] == FAIL
                       for c in VV.check_rendered(ctext, "", view, s))
         else unproven).append(cid)

    body = "x" * 1200                                # enough to pass length
    rendered("NO_PAST_NEXT_EVENT_RENDERED",
             body + " Next earnings are on 2020-05-01 (vendor estimate).",
             {"report_time": "2026-07-23T12:00:00+00:00"})
    rendered("INTERNAL_CONFIG_NOT_EXPOSED",
             body + " estimate feed FINNHUB_API_KEY not set for this run",
             {"report_time": "2026-07-23T12:00:00+00:00"})
    rendered("PDF_TEXT_ROUNDTRIP", "tiny stub",   # < 800 chars
             {"report_time": "2026-07-23T12:00:00+00:00"})

    return {"all_checks_proven": not unproven,
            "proven": sorted(proven), "unproven": sorted(unproven),
            "n_proven": len(proven)}


if __name__ == "__main__":
    import json
    import sys
    r = summary()
    print(json.dumps(r, indent=1))
    sys.exit(0 if r.get("all_checks_proven") else 1)
