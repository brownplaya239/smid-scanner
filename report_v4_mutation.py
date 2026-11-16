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
import pickle

import report_v4 as R4
import report_v4_event as EV
import report_v4_model as V4
import report_v4_validate as VV

FAIL, WARN_S = VV.FAIL, VV.WARN_S

# A free-tier-plus consensus feed, shaped like estimates_provider output,
# so the sourced-consensus and sourced-target checks are exercised.
EST = {
    "configured": True,
    "provider": "finnhub",
    "recommendation": {
        "strong_buy": 12,
        "buy": 20,
        "hold": 4,
        "sell": 1,
        "strong_sell": 0,
        "score": 1.8,
        "band": "Buy",
        "as_of": "2026-07-01",
    },
    "price_target": {
        "mean": 120.0,
        "high": 150.0,
        "low": 90.0,
        "n_analysts": 28,
        "as_of": "2026-07-18",
    },
    "coverage": {"price_target": "ok", "eps_estimate": "ok"},
    "surprises": [{"period": "2026-06-30", "actual": 3.9, "estimate": 3.75, "surprise_pct": 4.0}],
}


def _base():
    """A snapshot with a clean 52-week band and positive EPS injected, so
    the consensus, target, band and split checks all have inputs and all
    pass before mutation.

    fixtures_v4/mutation_base.pkl is the COMMITTED fixture and the one CI
    sees — .snapcache/ is gitignored, and depending on it silently made
    every CI run report "no ratable cached snapshot to mutate", which
    fails the gate and blocks the upload (the 2026-07-27 SG lookup). The
    local cache is only a fallback for development."""
    paths = sorted(glob.glob("fixtures_v4/*.pkl")) + sorted(glob.glob(".snapcache/*.pkl"))
    snap = None
    for p in paths:
        obj = pickle.load(open(p, "rb"))
        s = obj[0] if isinstance(obj, tuple) else obj
        cat = s.get("catalyst") or {}
        # a name the gate lets rate (not DATA HOLD), so ratings are present
        ev = EV.event_state(cat, s.get("exhibit"), report_time=s.get("report_time"))
        if ev.get("rating_allowed"):
            snap = s
            break
    if snap is None:
        return None, None, None
    snap = copy.deepcopy(snap)
    snap["levels"] = dict(
        snap.get("levels") or {},
        resistance_major={"v": 200.0},
        support_major={"v": 80.0},
        price_used={"v": 100.0},
    )
    snap["fundamentals"] = dict(snap.get("fundamentals") or {}, eps_ttm={"v": 2.0})
    snap["price"] = dict(snap.get("price") or {}, last={"v": 100.0})
    view = V4.build(
        snap, estimates=EST, peers={"rows": [{"ticker": "CRM", "pe": 40.0}], "source": "finnhub"}
    )
    return snap, view, EST


def _tiny_pdf(text):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

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


def grounded_take(view, snap):
    """A minimal analyst take that passes every ANALYST_* check on this
    view: each part prints one real fact through a placeholder and cites
    it, and the judge record clears every part. The mutations below each
    break exactly one rule."""
    import report_v4_analyst as AT

    facts = AT.fact_list(view, snap)
    pick = next(
        (f for f in facts if f["fact_id"] == "revenue_q"),
        next((f for f in facts if f["unit"] not in ("text", "date")), None),
    )
    if pick is None:
        return None
    fid = pick["fact_id"]

    def pt(t):
        return {"template": t, "fact_ids": [fid]}

    take = {
        "summary": pt("The report lists {{%s}} for this measure. It comes from the data." % fid),
        "bull_points": [pt("The figure on file is {{%s}}." % fid)],
        "bear_points": [pt("This is a single figure, {{%s}}." % fid)],
        "watch_next": [pt("The next filing will show where {{%s}} stands." % fid)],
    }
    rec = AT.render_take(take, facts)
    return {
        "available": True,
        **rec,
        "meta": {
            "status": "ok",
            "model_served": AT.MODEL,
            "judge": {
                "status": "ok",
                "model": AT.JUDGE_MODEL,
                "raw_verdicts": [
                    {"point_id": w, "verdict": "supported", "note": "ok"} for w, _ in AT.parts(rec)
                ],
                "verdicts": [
                    {"point_id": w, "verdict": "supported", "note": "ok"} for w, _ in AT.parts(rec)
                ],
            },
        },
    }


def _analyst_mutations(snap, view, proven, unproven):
    """Each mutation renders a REAL core PDF and runs the whole analyst
    gate on it (the stored record plus the printed pages)."""
    import report_v4_analyst as AT

    names = list(VV.ANALYST_CHECKS) + ["ANALYST_TAKE_RENDERED"]
    base = grounded_take(view, snap)
    if base is None:
        unproven.extend(names)
        return
    facts = AT.fact_list(view, snap)
    by_id = {f["fact_id"]: f for f in facts}
    fid = base["bull_points"][0]["fact_ids"][0]
    shown = AT.fmt_value(by_id[fid])

    def fails(printed, checked, cid):
        """Render `printed` into a real core PDF, then gate it as if the
        record were `checked`. True if `cid` fails."""
        v = copy.deepcopy(view)
        v["analyst_take"] = printed
        pages = VV._pdf_page_texts(R4.build_core(snap, v))
        v["analyst_take"] = checked
        return any(
            c["check_id"] == cid and c["status"] == FAIL
            for c in VV.check_analyst_take(v, snap, pages)
        )

    # The unmutated take must pass every check, or a "proof" below would
    # only show that a broken baseline stays broken.
    if any(fails(base, base, cid) for cid in names):
        unproven.extend(names)
        return

    results = {}

    def prove(cid, mutate, retext=True, printed=None, checked=None):
        t = copy.deepcopy(base)
        mutate(t)
        if retext:  # keep the printed text consistent with the template
            for _, p in AT.parts(t):
                try:
                    p["text"] = AT.render(p["template"], by_id)
                except Exception:
                    pass
        ok = fails(printed or t, checked or t, cid)
        results[cid] = results.get(cid, True) and ok

    def tpl(where, text):
        def m(t):
            t[where][0]["template"] = text

        return m

    # A free digit, a number word, a superlative, a free direction word.
    prove("ANALYST_NO_FREE_NUMBERS", tpl("bull_points", "The figure is {{%s}} over 3 lines." % fid))
    prove("ANALYST_NO_FREE_NUMBERS", tpl("bull_points", "The figure {{%s}} doubled." % fid))
    prove(
        "ANALYST_NO_FREE_DIRECTION",
        tpl("bull_points", "The figure, {{%s}}, is the highest on record." % fid),
    )
    prove("ANALYST_NO_FREE_DIRECTION", tpl("bear_points", "The figure fell to {{%s}}." % fid))
    # Movement verbs by stem, a size word and a judgement word.
    prove(
        "ANALYST_NO_FREE_DIRECTION", tpl("bear_points", "The figure skyrocketed to {{%s}}." % fid)
    )
    prove("ANALYST_NO_FREE_NUMBERS", tpl("bull_points", "The figure {{%s}} is several-fold." % fid))
    prove(
        "ANALYST_NO_FREE_DIRECTION", tpl("bull_points", "At {{%s}}, the outlook is bright." % fid)
    )
    # Advice.
    prove(
        "ANALYST_NO_ADVICE",
        tpl("watch_next", "Investors should buy before {{%s}} is updated." % fid),
    )
    prove("ANALYST_NO_ADVICE", tpl("watch_next", "Invest your savings while it is {{%s}}." % fid))
    # A token using a fact the point does not cite; a fact that does not exist.
    other = next((f["fact_id"] for f in facts if f["fact_id"] != fid), None)
    if other:
        prove(
            "ANALYST_FACT_IDS",
            tpl("bull_points", "The figure is {{%s}} next to {{%s}}." % (fid, other)),
        )

    def m_unknown(t):
        t["bear_points"][0]["fact_ids"].append("made_up_fact")

    prove("ANALYST_FACT_IDS", m_unknown)
    # A ticker that is nowhere in the data.
    prove("ANALYST_NO_FOREIGN_TICKERS", tpl("bull_points", "ZQZX lists {{%s}} too." % fid))

    def m_length(t):
        t["bull_points"] = t["bull_points"] * 6
        more = [
            {"point_id": "bull_points[%d]" % i, "verdict": "supported", "note": "ok"}
            for i in range(2, 7)
        ]
        t["meta"]["judge"]["verdicts"] += more
        t["meta"]["judge"]["raw_verdicts"] += copy.deepcopy(more)

    prove("ANALYST_LENGTH_CAPS", m_length)

    # The printed text no longer equals what the data renders: a sign
    # reversal, a unit substitution, and (when a change fact exists) a
    # direction reversal.
    def m_sign(t):
        t["bull_points"][0]["text"] = t["bull_points"][0]["text"].replace(shown, "-" + shown)

    prove("ANALYST_RENDER_MATCHES_DATA", m_sign, retext=False)

    def m_unit(t):
        swapped = shown.replace("$", "") + "%" if "$" in shown else "$" + shown.rstrip("%x")
        t["bull_points"][0]["text"] = t["bull_points"][0]["text"].replace(shown, swapped)

    prove("ANALYST_RENDER_MATCHES_DATA", m_unit, retext=False)
    chg = next(
        (f for f in facts if "change" in AT.forms_for(f, by_id) and f["value"] != 0),
        None,
    )
    if chg is not None:
        cid_ = chg["fact_id"]

        def m_direction(t):
            p = t["bear_points"][0]
            p["template"] = "The measure {{%s:change}}." % cid_
            p["fact_ids"] = [cid_]
            good = AT.render(p["template"], by_id)
            p["text"] = (
                good.replace("rose", "fell") if "rose" in good else good.replace("fell", "rose")
            )

        prove("ANALYST_RENDER_MATCHES_DATA", m_direction, retext=False)

    # A value the source states only as a bound ("crossed $1 billion")
    # printed as an exact figure ("$1.00B" instead of "at least $1.00B").
    bnd = next((f for f in facts if f.get("bound", "exact") != "exact"), None)
    if bnd is not None:
        bid = bnd["fact_id"]

        def m_bound(t):
            p = t["bull_points"][0]
            p["template"] = "The company reports {{%s}} for this metric." % bid
            p["fact_ids"] = [bid]
            p["text"] = AT.render(p["template"], by_id).replace(
                AT.fmt_value(bnd), AT.fmt_value(bnd, bnd["value"])
            )

        prove("ANALYST_RENDER_MATCHES_DATA", m_bound, retext=False)

    # The judge did not clear a point.
    def m_judge(t):
        t["meta"]["judge"]["verdicts"][1]["verdict"] = "unsupported"
        t["meta"]["judge"]["raw_verdicts"][1]["verdict"] = "unsupported"

    prove("ANALYST_JUDGE_PASSED", m_judge)

    # The judge answered twice for one point ("supported", then
    # "unsupported"); the cleaned list kept only the first. The gate reads
    # the raw answer.
    def m_judge_contradiction(t):
        t["meta"]["judge"]["raw_verdicts"].append(
            {"point_id": "summary", "verdict": "unsupported", "note": "not shown"}
        )

    prove("ANALYST_JUDGE_PASSED", m_judge_contradiction)

    # A verdict for a point that does not exist; a missing raw list.
    def m_judge_unexpected(t):
        t["meta"]["judge"]["raw_verdicts"].append(
            {"point_id": "bull_points[9]", "verdict": "supported", "note": "ok"}
        )

    prove("ANALYST_JUDGE_PASSED", m_judge_unexpected)

    def m_judge_no_raw(t):
        del t["meta"]["judge"]["raw_verdicts"]

    prove("ANALYST_JUDGE_PASSED", m_judge_no_raw)

    # Printed pages that differ from the record: unchecked text appended to
    # the page; stale analyst content printed when the take was withheld;
    # the unavailable note missing.
    appended = copy.deepcopy(base)
    appended["bull_points"][0]["text"] += " Unchecked extra text."
    withheld = {"available": False, "note": AT.UNAVAILABLE_NOTE, "meta": {"status": "invalid"}}
    for printed, checked in ((appended, base), (base, withheld), (None, withheld)):
        ok = fails(printed, checked, "ANALYST_TAKE_RENDERED")
        results["ANALYST_TAKE_RENDERED"] = results.get("ANALYST_TAKE_RENDERED", True) and ok

    for cid in names:
        (proven if results.get(cid) else unproven).append(cid)


def summary():
    snap, view, est = _base()
    if view is None:
        return {"all_checks_proven": False, "note": "no ratable cached snapshot to mutate"}

    proven, unproven = [], []

    def model(cid, mutate):
        v = copy.deepcopy(view)
        s = copy.deepcopy(snap)
        s2 = mutate(v, s)
        (proven if _view_fails(v, s2 or s, est, cid) else unproven).append(cid)

    # ── each mutation trips exactly one model check ─────────────────────
    def m_event(v, s):
        v["event"] = dict(v["event"], rating_allowed=False)
        v["ratings"]["fundamental"]["available"] = True

    model("EVENT_RATING_CONSISTENCY", m_event)

    def m_flash(v, s):
        v["event"] = dict(v["event"], state=EV.DATA_HOLD, rating_allowed=False)
        v["ratings"]["fundamental"] = {"available": False, "reason": "x"}
        v["ratings"]["target"] = {"available": False, "reason": "x"}
        v["flash"] = None

    model("FLASH_ON_DATA_HOLD", m_flash)

    def m_next(v, s):
        v["event"] = dict(
            v["event"],
            state=EV.RESULTS_RELEASED,
            rating_allowed=True,
            next_earnings_is_pending=True,
        )

    model("NO_NEXT_AFTER_RELEASE", m_next)

    def m_consensus(v, s):
        v["ratings"]["fundamental"]["grade"] = "DERIVED"  # not observed

    model("CONSENSUS_SOURCED", m_consensus)

    def m_target(v, s):
        v["ratings"]["target"]["as_of"] = None  # undated

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
            "available": True,
            "bull": {"price": 200.0},
            "bear": {"price": 80.0},
            "base": {"price": 100.0},
        }

    model("VALUATION_NON_CIRCULAR", m_circular)

    def m_uningested(v, s):
        # a full post-release report whose FRESH release exhibit was never
        # parsed. The check only blocks inside HOLD_WINDOW_DAYS (a stale
        # unparsed exhibit is a labelled gap the gate allows), so the
        # mutation stamps acceptance "now" to be unambiguously fresh.
        import datetime as _d

        v["event"] = dict(v["event"], state=EV.POST_CALL_UNVERIFIED)
        v["flash"] = None
        s["exhibit"] = {
            "disposition": "AVAILABLE_NOT_INGESTED",
            "reason": "tables not parsed",
            "accepted": _d.datetime.now(_d.UTC).isoformat(),
        }

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
    if any(
        c["check_id"] == "CORE_PAGE_COUNT" and c["status"] == FAIL
        for c in VV.check_pdfs(good_core, good_apx, flashy)
    ):
        proven.append("CORE_PAGE_COUNT")
    else:
        unproven.append("CORE_PAGE_COUNT")

    # HTML_ENTITIES + GLYPH_INTEGRITY: a one-page core carrying each defect
    ent = VV.check_pdfs(_tiny_pdf("Revenue rose 5 &amp; margins held"), good_apx, {"flash": True})
    (
        proven
        if any(c["check_id"] == "HTML_ENTITIES" and c["status"] == FAIL for c in ent)
        else unproven
    ).append("HTML_ENTITIES")

    # mojibake the extractor surfaces verbatim (reportlab renders these
    # Latin-1 bytes, unlike the replacement char, which it silently drops)
    gly = VV.check_pdfs(_tiny_pdf("the issuerâ€™s guidance"), good_apx, {"flash": True})
    (
        proven
        if any(c["check_id"] == "GLYPH_INTEGRITY" and c["status"] == FAIL for c in gly)
        else unproven
    ).append("GLYPH_INTEGRITY")

    # APPENDIX_PRESENT: a full core with an empty appendix
    if any(
        c["check_id"] == "APPENDIX_PRESENT" and c["status"] == FAIL
        for c in VV.check_pdfs(good_core, b"", view)
    ):
        proven.append("APPENDIX_PRESENT")
    else:
        unproven.append("APPENDIX_PRESENT")

    # HTML_ENTITIES_APPENDIX: a real 6-page core, a defective appendix
    ea = VV.check_pdfs(good_core, _tiny_pdf("filed 10-K &amp; 10-Q"), view)
    (
        proven
        if any(c["check_id"] == "HTML_ENTITIES_APPENDIX" and c["status"] == FAIL for c in ea)
        else unproven
    ).append("HTML_ENTITIES_APPENDIX")

    # ── v4.1 checks: rendered document + metric period ──────────────────
    def model_period(cid, mutate):
        s = copy.deepcopy(snap)
        mutate(s)
        (
            proven
            if any(
                c["check_id"] == cid and c["status"] == FAIL for c in VV.check_view(view, s, est)
            )
            else unproven
        ).append(cid)

    def m_period(s):
        # cash flow from a different quarter than revenue — the v4.0 defect.
        # The metric must CARRY a value: a value-less fact renders "n/a",
        # asserts no period, and is exempt from the check by design.
        s["fundamentals"] = dict(
            s.get("fundamentals") or {},
            operating_cash_flow={"v": 1e9, "value": 1e9, "period_end": "2020-01-01"},
        )

    model_period("METRIC_PERIOD_CONSISTENCY", m_period)

    def rendered(cid, ctext, snap_over):
        s = dict(snap)
        s.update(snap_over or {})
        (
            proven
            if any(
                c["check_id"] == cid and c["status"] == FAIL
                for c in VV.check_rendered(ctext, "", view, s)
            )
            else unproven
        ).append(cid)

    body = "x" * 1200  # enough to pass length
    rendered(
        "NO_PAST_NEXT_EVENT_RENDERED",
        body + " Next earnings are on 2020-05-01 (vendor estimate).",
        {"report_time": "2026-07-23T12:00:00+00:00"},
    )
    rendered(
        "INTERNAL_CONFIG_NOT_EXPOSED",
        body + " estimate feed FINNHUB_API_KEY not set for this run",
        {"report_time": "2026-07-23T12:00:00+00:00"},
    )
    rendered(
        "PDF_TEXT_ROUNDTRIP",
        "tiny stub",  # < 800 chars
        {"report_time": "2026-07-23T12:00:00+00:00"},
    )

    # ── AI analyst take: a grounded take passes, each defect fails ──────
    _analyst_mutations(snap, view, proven, unproven)

    return {
        "all_checks_proven": not unproven,
        "proven": sorted(proven),
        "unproven": sorted(unproven),
        "n_proven": len(proven),
    }


if __name__ == "__main__":
    import json
    import sys

    r = summary()
    print(json.dumps(r, indent=1))
    sys.exit(0 if r.get("all_checks_proven") else 1)
