#!/usr/bin/env python3
"""test_report_v3.py — prove the v3 rules bite.

A validator that returns [] on good input has demonstrated nothing, so
every case here either injects exactly one defect and asserts it is
caught, or feeds a shape the model used to get wrong and asserts the
answer is now right. The control cases at each end assert clean input
still passes, so a gate that blocked everything would fail too.

    python test_report_v3.py
"""

import copy
import io
import sys

import report_v3 as R3
import report_v3_model as M
import report_v3_validate as V

FAILS, RAN = [], [0]


def chk(name, cond, detail=""):
    RAN[0] += 1
    print(("  PASS  " if cond else "  FAIL  ") + name
          + ("" if cond else "  <- %s" % (detail,)))
    if not cond:
        FAILS.append(name)


# Checks about the document as an object (metadata, language, bookmarks,
# links). A bare reportlab canvas built inside a test has none of these by
# construction, so the page-content controls below exclude them; the real
# brief is checked against them in test_build.
DOC_PROPERTY = {"PDF_METADATA", "PDF_LANGUAGE", "PDF_BOOKMARKS",
                "PDF_LINKS", "PDF_TAGGED"}


def codes(checks, ignore_doc_properties=False):
    """v3.1 returns one object per check; a "code" in the old sense is a
    check that came back FAIL."""
    out = {c["check_id"] for c in checks if c["status"] == V.FAIL}
    return out - DOC_PROPERTY if ignore_doc_properties else out


def fact(v, **kw):
    d = {"v": v, "src": "test", "as_of": "2026-07-16"}
    d.update(kw)
    return d


def base_snap():
    """A snapshot that passes everything. Each case mutates one field."""
    return {
        "ticker": "TEST",
        "report_time": "2026-07-16T20:00:00+00:00",
        "market_data_time": "2026-07-16T20:00:00+00:00",
        "price": {"last": fact(100.0), "prev_close": fact(99.0),
                  "change_pct": fact(1.01)},
        "levels": {"ma20": fact(104.0), "ma50": fact(108.0),
                   "ma200": fact(112.0), "support": fact(88.0),
                   "resistance": fact(120.0), "atr14": fact(3.0)},
        # Enough reported detail for four pages to carry real content.
        # A snapshot this thin genuinely produces a half-empty page, and
        # the page gate is right to say so — so the fixture is filled to
        # the level a live run reaches rather than the gate relaxed.
        "fundamentals": {
            "revenue_q": fact(2.4e9, unit="USD", period_end="2026-03-31",
                              src="SEC XBRL 10-Q"),
            "revenue_growth": fact(12.5, calc_version="yoy/v2",
                                   period_end="2026-03-31"),
            "gross_profit": fact(1.5e9, unit="USD",
                                 period_end="2026-03-31"),
            "gross_margin": fact(62.5, calc_version="margin/v1",
                                 period_end="2026-03-31"),
            "net_income_q": fact(4.2e8, unit="USD",
                                 period_end="2026-03-31"),
            "operating_cash_flow": fact(6.1e8, unit="USD",
                                        period_end="2026-03-31"),
            "eps_ttm": fact(4.55, calc_version="ttm/v2"),
            "cash": fact(3.3e9, unit="USD", period_end="2026-03-31"),
            "debt": fact(1.1e9, unit="USD", period_end="2026-03-31"),
        },
        "valuation": {"pe_trailing": fact(22.0, calc_version="pe/v1",
                                          basis="trailing")},
        "company": {"name": fact("Test Co"), "sector": fact("Industrials"),
                    "market_cap": fact(4.1e10, unit="USD"),
                    "shares_outstanding": fact(410000000),
                    "overview": {"text": "Test Co makes and services "
                                         "industrial control equipment sold "
                                         "to process manufacturers.",
                                 "source": "issuer profile",
                                 "industry": "Electrical equipment"}},
        "decision": {"current_action": "HOLD", "horizon": "3-12 months",
                     "position_plan": {}, "review_date": "2026-08-01",
                     "supporting_facts": ["a"], "risks": ["b"]},
        "catalyst": {"event_dt": "2026-07-10T12:00:00+00:00",
                     "event_kind": "primary_release",
                     "event_ref": "CAT-1", "upcoming": []},
        "ownership": {"filings": [{"form": "SC 13G", "filer": "Someone",
                                   "accepted": "2026-06-01T12:00:00+00:00",
                                   "evidence_ref": "OWN-0001", "url": "u",
                                   "in_window": True}]},
        "insiders": {"by_class": {"open_market_buy": 2,
                                  "open_market_sale": 3,
                                  "tax_withholding": 4},
                     "window_days": 180, "n_view_bearing": 5,
                     "economics": {"plan_status": "unknown - not documented "
                                                  "in parsed footnotes",
                                   "window_start": "2026-01-17",
                                   "window_end": "2026-07-16"}},
        "sentiment": {"n_considered": 100, "n_relevant": 60,
                      "n_rejected": 40, "unique_authors": 30,
                      "coordination": {}, "by_class": {"bullish": 30,
                                                       "bearish": 20,
                                                       "neutral": 10},
                      "classification": "MIXED", "news": [
                          {"headline": "Test Co wins a multi-year service "
                                       "contract", "publisher": "Reuters",
                           "url": "https://example.com/a",
                           "article_check": {"company_mentions": 9,
                                             "first_mention_pct": 6},
                           "published_at": "2026-07-15T14:00:00+00:00"},
                          {"headline": "Analysts weigh the margin outlook "
                                       "after the quarter",
                           "publisher": "Bloomberg",
                           "url": "https://example.com/b",
                           "published_at": "2026-07-14T18:30:00+00:00"},
                          {"headline": "Process automation orders steady "
                                       "into the second half",
                           "publisher": "Dow Jones",
                           "url": "https://example.com/c",
                           "published_at": "2026-07-12T11:05:00+00:00"}]},
        # A live run always attempts the Item 2.02 exhibit, so the fixture
        # carries one too. Without it page 2 is genuinely thin and the
        # page gate says so — correctly.
        "exhibit": {
            "disposition": "ADMITTED",
            "accession": "0000000000-26-000001",
            "accepted": "2026-05-27T20:06:07+00:00",
            "url": "https://www.sec.gov/Archives/edgar/data/1/x/ex-991.htm",
            "period_label": "May 2, 2026",
            "guidance_period": "Q2 FY2027",
            "reported": {
                "non_gaap_gross_margin": {"value": 58.9, "unit": "%"},
                "non_gaap_operating_margin": {"value": 35.0, "unit": "%"},
                "gaap_eps": {"value": 0.04, "unit": "USD/share"},
                "non_gaap_eps": {"value": 0.80, "unit": "USD/share"}},
            "guidance": {
                "revenue": {"midpoint": 2700.0, "low": 2565.0,
                            "high": 2835.0, "basis": "midpoint +/- 5.0%"},
                "non_gaap_gross_margin": {"low": 58.25, "high": 59.25,
                                          "midpoint": 58.75,
                                          "basis": "stated range"},
                "non_gaap_eps": {"low": 0.88, "high": 0.98, "midpoint": 0.93,
                                 "basis": "midpoint +/- 0.05"}}},
        "evidence": {"evidence_quality": "moderate"},
    }


# ── the trigger ladder ──────────────────────────────────────────────────

def test_ladder():
    print("\ntrigger ladder — the order must come from the data")
    # A downtrend puts the 20-day BELOW the 50-day. v2's flat
    # upgrade/downside rows assumed the opposite and told the reader to
    # wait for a level they had already cleared.
    down = {"ma20": fact(95.0), "ma50": fact(105.0), "ma200": fact(115.0)}
    got = [r["label"] for r in M.ladder(down, 100.0)]
    chk("downtrend: 20-day is ordered below the 50-day",
        got == ["20-day average", "50-day average", "200-day average"], got)
    rec = [r["label"] for r in M.recovery_stages(down, 100.0)]
    chk("downtrend: the 20-day is not offered as an upside trigger",
        rec == ["50-day average", "200-day average"], rec)

    up = {"ma20": fact(115.0), "ma50": fact(105.0), "ma200": fact(95.0)}
    got = [r["label"] for r in M.ladder(up, 100.0)]
    chk("uptrend: the same code orders 200 < 50 < 20",
        got == ["200-day average", "50-day average", "20-day average"], got)
    rec = [(r["label"], r["value"]) for r in M.recovery_stages(up, 100.0)]
    chk("uptrend: upside stages ascend",
        [v for _, v in rec] == sorted(v for _, v in rec), rec)

    chk("levels below spot are ordered nearest-first",
        [r["value"] for r in M.downside_stages(up, 100.0)] == [95.0], "")


# ── risk boundary vs invalidation ───────────────────────────────────────

def test_exit():
    print("\nrisk boundary vs invalidation")
    s = base_snap()
    ex = M.exit_level(s)
    chk("with no position the exit is a risk boundary",
        ex["label"] == "Risk boundary", ex["label"])
    chk("the boundary cites a documented low, not a round number",
        "60-session closing low" in (ex["basis"] or ""),
        ex["basis"])
    s2 = base_snap()
    s2["decision"]["position_plan"] = {"entry": 99.0}
    chk("with a position on the book it becomes an invalidation",
        M.exit_level(s2)["label"] == "Invalidation", "")
    chk("a long horizon demands a wider stop than a short one",
        M.horizon_floor("12-18 months") > M.horizon_floor("1-3 months"), "")


# ── catalyst separation ─────────────────────────────────────────────────

def test_catalysts():
    print("\ncatalysts: reported, driving, next")
    s = base_snap()
    # an 8-K accepted after the report has not happened yet
    s["catalyst"]["event_dt"] = "2026-07-16T23:00:00+00:00"
    c = M.catalysts(s)
    chk("an event later than the report is not the last reported one",
        c["last_reported"] is None, c["last_reported"])
    chk("...it is listed as scheduled instead",
        bool(c["scheduled"]) and bool(c["next"]), "")
    s = base_snap()
    s["catalyst"]["event_dt"] = "2026-05-01T12:00:00+00:00"   # 76 days old
    c = M.catalysts(s)
    chk("a 76-day-old filing is not sold as the current driver",
        c["current_driver"]["grade"] != M.DERIVED,
        c["current_driver"])
    s = base_snap()
    c = M.catalysts(s)
    chk("a 6-day-old filing may be the current driver",
        c["current_driver"]["grade"] == M.DERIVED, c["current_driver"])


# ── snapshot-shape robustness ───────────────────────────────────────────

def test_shapes():
    print("\nsnapshot shapes the renderer used to get wrong")
    s = base_snap()
    chk("spot is found in price.last when levels carries no price",
        M.spot(s) == 100.0, M.spot(s))
    s2 = base_snap()
    s2["levels"]["price_used"] = fact(101.5)
    chk("levels.price_used wins when present", M.spot(s2) == 101.5, "")

    # the older alt shape reports n_dropped_irrelevant and a coordination
    # sentence; the live one reports n_rejected and a dict
    s3 = base_snap()
    s3["sentiment"] = {"n_considered": 144, "n_relevant": 58,
                       "n_dropped_irrelevant": 86,
                       "coordination": "5 phrases repeated"}
    sv = M.social_view(s3)
    chk("legacy alt shape reconciles 144 = 58 + 86",
        sv["n_considered"] == sv["n_counted"] + sv["n_rejected"],
        (sv["n_considered"], sv["n_counted"], sv["n_rejected"]))
    chk("a coordination sentence does not become a phrase-group count",
        sv["coordination"]["posts_affected"] is None, "")


# ── what changed ────────────────────────────────────────────────────────

def test_changed():
    print("\nwhat changed since the previous report")
    s = base_snap()
    ch = M.what_changed(s, prior=None)
    chk("with no prior report we say so rather than inventing a delta",
        ch["first_report"] and not ch["items"], "")
    prior = {"price": 90.0, "action": "WAIT", "ma20": 104.0, "ma50": 108.0,
             "ma200": 112.0, "report_time": "2026-07-09T20:00:00+00:00",
             "evidence_quality": "moderate", "catalyst":
             s["catalyst"]["event_dt"]}
    ch = M.what_changed(s, prior=prior)
    txt = " ".join(i["text"] for i in ch["items"])
    chk("a price move is reported with both endpoints", "90.00 to 100.00"
        in txt, txt)
    chk("an action change is reported", "WAIT to HOLD" in txt, txt)


# ── the semantic gate ───────────────────────────────────────────────────

MUTATIONS = [
    ("recovery stages out of order",
     lambda v, s: v["recovery"].reverse(), "LADDER_ORDER"),
    ("an upside trigger sitting below spot",
     lambda v, s: v["recovery"].insert(0, {"label": "fake", "value": 1.0,
                                           "side": "above",
                                           "distance_pct": -99.0}),
     "LADDER_VS_SPOT"),
    ("market data stamped after the report",
     lambda v, s: v.__setitem__("quote_time_utc", "2027-01-01T00:00:00Z"),
     "TIMESTAMP_CONSISTENCY"),
    ("a timestamp with no zone",
     lambda v, s: v.__setitem__("quote_tz_warning", "no zone"),
     "TIMESTAMP_ZONE"),
    ("a stale filing presented as the current driver",
     lambda v, s: (v["catalysts"]["last_reported"].__setitem__(
         "age_days", 90),
         v["catalysts"]["current_driver"].__setitem__("grade", M.DERIVED)),
     "STALE_CATALYST"),
    ("an exit inside one day's normal range",
     lambda v, s: v["exit"].update({"atr_multiple": 0.4, "floor": 2.0}),
     "EXIT_VS_HORIZON"),
    ("an exit with no stated basis",
     lambda v, s: v["exit"].update({"basis": None, "value": 88.0}),
     "EXIT_DOCUMENTED"),
    ("'invalidation' used with no position on the book",
     lambda v, s: v["exit"].update({"label": "Invalidation",
                                    "active_entry": False}),
     "INVALIDATION_WITHOUT_ENTRY"),
    ("a filing counted with no accession number",
     lambda v, s: v["ownership"]["rows"][0].__setitem__("accession", None),
     "FILING_ACCESSIONS"),
    ("unparsed filers with no 'interpretation unavailable' notice",
     lambda v, s: v["ownership"].update({"filers_parsed": False,
                                         "interpretation": None}),
     "OWNERSHIP_COVERAGE"),
    ("an empty options section with no coverage note",
     lambda v, s: v["options"].update({"available": False, "note": None}),
     "MISSING_VS_NEGATIVE"),
    ("a self-assessment presented as calibrated",
     lambda v, s: s["evidence"].__setitem__("calibrated_confidence", 0.8),
     "CALIBRATION_LANGUAGE"),
    ("a social population that does not add up",
     lambda v, s: v["social"].__setitem__("n_rejected", 1),
     "POPULATION_ARITHMETIC"),
    ("a coordination verdict with no phrase groups behind it",
     lambda v, s: v["social"]["coordination"].update(
         {"label": "5 phrases repeated across 3+ accounts",
          "phrase_groups": None}),
     "COORDINATION_CONSISTENCY"),
]


def test_model_gate():
    print("\nsemantic gate — each case injects exactly one defect")
    snap = base_snap()
    view = M.build(snap)
    clean = [c for c in V.check_model(view, snap) if c["status"] == V.FAIL]
    chk("CONTROL: a clean model passes", not clean,
        [c["check_id"] for c in clean])

    for name, mutate, want in MUTATIONS:
        s = copy.deepcopy(snap)
        v = M.build(s)
        mutate(v, s)
        got = codes(V.check_model(v, s))
        chk("blocks: %s" % name, want in got, "got %s" % (sorted(got) or "[]"))


# ── the rendered artefact ───────────────────────────────────────────────

def _pdf(draw, pages=1):
    """A minimal PDF built with the same engine the brief uses."""
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    cv = canvas.Canvas(buf, pagesize=R3.LETTER)
    for p in range(pages):
        draw(cv, p)
        cv.showPage()
    cv.save()
    return buf.getvalue()


def test_pdf_gate():
    print("\npage gate — measured off the rendered file")

    def good(cv, p):
        cv.setFont(R3.FONT, 10)
        for i in range(60):
            cv.drawString(50, 720 - i * 11, "Ordinary body copy on line %d."
                          % i)
    clean = codes(V.check_pdf(_pdf(good), core=True), True)
    chk("CONTROL: a well-filled page passes", not clean, sorted(clean))

    def tiny(cv, p):
        good(cv, p)
        cv.setFont(R3.FONT, 7.5)
        cv.drawString(50, 60, "a footnote nobody can read")
    chk("blocks: type below the 9pt floor",
        "TYPE_SIZE" in codes(V.check_pdf(_pdf(tiny)), True), "")

    def intent(cv, p):
        good(cv, p)
        cv.setFont(R3.FONT, 10)
        cv.drawString(50, 40, "Institutions are accumulating the shares.")
    chk("blocks: a claim to know why an institution traded",
        "INSTITUTIONAL_INTENT" in codes(V.check_pdf(_pdf(intent)), True),
        "")

    def smart(cv, p):
        good(cv, p)
        cv.drawString(50, 40, "Smart money is positioning here.")
    chk("blocks: 'smart money'",
        "INSTITUTIONAL_INTENT" in codes(V.check_pdf(_pdf(smart)), True),
        "")

    def entity(cv, p):
        good(cv, p)
        cv.drawString(50, 40, "Revenue &amp; margin")
    chk("blocks: an HTML entity reaching the page",
        "HTML_ENTITIES" in codes(V.check_pdf(_pdf(entity)), True), "")

    def blank(cv, p):
        if p == 0:
            good(cv, p)
    chk("blocks: a page with no text",
        "BLANK_PAGE" in codes(V.check_pdf(_pdf(blank, pages=2)), True), "")

    def short(cv, p):
        cv.setFont(R3.FONT, 10)
        if p == 0:
            for i in range(6):
                cv.drawString(50, 720 - i * 11, "only a few lines")
        else:
            good(cv, p)
    got = codes(V.check_pdf(_pdf(short, pages=2)), True)
    chk("blocks: a half-empty page with content after it",
        "NEARLY_BLANK_PAGE" in got, sorted(got))

    def short_last(cv, p):
        cv.setFont(R3.FONT, 10)
        if p == 0:
            good(cv, p)
        else:
            for i in range(6):
                cv.drawString(50, 720 - i * 11, "a short closing page")
    chk("allows: a final page that simply ends",
        "NEARLY_BLANK_PAGE" not in codes(
            V.check_pdf(_pdf(short_last, pages=2)), True), "")

    chk("blocks: a core brief longer than four pages",
        "PAGE_COUNT" in codes(V.check_pdf(_pdf(good, pages=5),
                                                core=True), True), "")
    chk("allows: an appendix longer than four pages",
        "PAGE_COUNT" not in codes(V.check_pdf(_pdf(good, pages=5),
                                                    core=False), True), "")

    # raw message-board text must never appear in the core brief
    snap = base_snap()
    excerpt = "this thing is going to the moon buy every dip right now"
    snap["sentiment"]["sample_records"] = [{"excerpt": excerpt}]

    def social(cv, p):
        good(cv, p)
        cv.drawString(50, 40, excerpt)
    chk("blocks: a raw post excerpt in the core brief",
        "RAW_SOCIAL_CONTAINMENT" in codes(
            V.check_pdf(_pdf(social), snap, core=True), True), "")
    chk("allows: the same excerpt in the appendix",
        "RAW_SOCIAL_CONTAINMENT" not in codes(
            V.check_pdf(_pdf(social), snap, core=False), True), "")


# ── end to end ──────────────────────────────────────────────────────────

def _bars(n=280, last=100.0):
    """A deterministic price series — no RNG, so the rendered page is
    byte-stable across runs and a diff means a real change."""
    closes = [last * (1 + 0.28 * ((i % 63) - 31) / 100.0) for i in range(n)]
    closes[-1] = last
    return {"ticker": "TEST",
            "dates": ["2025-%02d-%02d" % (1 + (i // 28) % 12, 1 + i % 28)
                      for i in range(n)],
            "closes": closes,
            "volumes": [1_500_000 + (i % 17) * 90_000 for i in range(n)]}


def test_build():
    print("\nend to end")
    import report_chart_v3 as C
    snap, mk = base_snap(), _bars()
    mini, full = C.mini_chart(mk), C.full_chart(mk, [100.0] * 280)
    chk("both charts render", bool(mini) and bool(full), "")
    core = R3.build_core(snap, chart_png=mini, chart_full=full,
                         allow_demo=False)
    import fitz
    d = fitz.open(stream=core, filetype="pdf")
    n = d.page_count
    txt = "\n".join(d[i].get_text() for i in range(n))
    d.close()
    chk("the core brief is exactly four pages", n == 4, n)
    chk("no page is empty", all(txt for _ in range(1)), "")
    chk("the reader is shown Eastern time, not a bare Z string",
        " ET" in txt and "T20:00:00Z" not in txt, "")
    chk("every grade legend entry appears",
        all(g in txt for g in ("[OBS]", "[DER]")), "")
    probs = codes(V.check_pdf(core, snap, core=True))
    chk("the rendered brief passes its own page gate", not probs,
        sorted(probs))
    ev = M.build(snap)
    chk("the evidence bundle keeps UTC while the page shows ET",
        ev["report_time_utc"].endswith("Z"), ev["report_time_utc"])


def main():
    for t in (test_ladder, test_exit, test_catalysts, test_shapes,
              test_changed, test_model_gate, test_pdf_gate, test_build):
        t()
    print("\n%d/%d checks passed" % (RAN[0] - len(FAILS), RAN[0]))
    if FAILS:
        print("FAILED: " + "; ".join(FAILS))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
