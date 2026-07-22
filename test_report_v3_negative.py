#!/usr/bin/env python3
"""test_report_v3_negative.py — prove the v3.1 gates can fail.

A validation report that says PASS is worth nothing unless the same code
says FAIL when the package is wrong. Each fixture below takes a real,
passing package and breaks exactly one thing, then asserts that the
specific check which owns that defect reports it.

    python test_report_v3_negative.py

These are the seven failure modes hardest to notice by reading the PDF:
a baseline that is really a draft from the same session, a stale catalyst
laundered into an inferred driver, a market cap computed off the wrong
basis, a multiple whose operands cannot be produced, ownership rows old
enough to be history, a stated count the rendered rows contradict, and a
file edited after it was signed.

The control at the end matters as much as the fixtures: without it, a
validator that failed everything would score full marks here.
"""

import datetime as dt
import io as _io
import json
import os
import sys
import tempfile

import report_v3_evidence as EV
import report_v3_model as M
import report_v3_validate as V

FAILS, RAN = [], [0]


def chk(name, cond, detail=""):
    RAN[0] += 1
    print(("  PASS  " if cond else "  FAIL  ") + name
          + ("" if cond else "  <- %s" % (detail,)))
    if not cond:
        FAILS.append(name)


def status_of(checks, check_id):
    for c in checks:
        if c["check_id"] == check_id:
            return c["status"], c.get("observed")
    return None, "check %s not present" % check_id


def clean_snap():
    import test_report_v3 as T
    s = T.base_snap()
    s["exhibit"] = {
        "disposition": "ADMITTED",
        "reported": {"non_gaap_gross_margin": {"value": 58.9},
                     "gaap_eps": {"value": 0.04},
                     "non_gaap_eps": {"value": 0.80}},
        "guidance": {"revenue": {"midpoint": 2700.0, "low": 2565.0,
                                 "high": 2835.0,
                                 "basis": "midpoint +/- 5.0%"}},
        "guidance_period": "Q2 FY2027"}
    return s


def evidence_for(calcs=None, records=None):
    """A structurally real evidence package with resolvable operands."""
    base = {
        "CALC-market_cap": {
            "calculation_id": "CALC-market_cap",
            "formula": "last close x cover-page shares outstanding",
            "operands": [{"evidence_id": "CALC-last_close", "resolved": True,
                          "value": 100.0},
                         {"evidence_id": "SHR-0001", "resolved": True,
                          "value": 410000000}],
            "operands_complete": True, "result_unrounded": 4.1e10,
            "result_displayed": "$41.0B", "displayed": True},
        "CALC-pe_trailing": {
            "calculation_id": "CALC-pe_trailing",
            "formula": "last close / GAAP TTM diluted EPS",
            "operands": [{"evidence_id": "CALC-last_close", "resolved": True,
                          "value": 100.0},
                         {"evidence_id": "CALC-eps_ttm", "resolved": True,
                          "value": 4.55}],
            "operands_complete": True, "result_unrounded": 21.978,
            "result_displayed": "22.0x", "displayed": True},
    }
    base.update(calcs or {})
    return {"schema": EV.SCHEMA, "calculations": base,
            "records": records or {
                "OWN-0001": {"evidence_id": "OWN-0001",
                             "evidence_type": "ownership_filing",
                             "disposition": EV.AVAILABLE_NOT_INGESTED}},
            "populations": {}, "dispositions": {}}


# ── the seven fixtures ──────────────────────────────────────────────────

def fx_draft_baseline():
    """A baseline published sixty seconds ago is this session's draft."""
    tmp = tempfile.mkdtemp()
    old, M.STATE_DIR = M.STATE_DIR, tmp
    try:
        now = dt.datetime.now(dt.timezone.utc)
        recent = (now - dt.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(os.path.join(tmp, "TEST.json"), "w") as fh:
            json.dump({"ticker": "TEST", "published": True,
                       "validation_ok": True, "published_at": recent,
                       "artifacts": {"core_pdf": {"sha256": "a" * 64}},
                       "price": 90.0, "action": "WAIT"}, fh)
        st, why = M.prior_state("TEST")
        return ("one-minute draft baseline is refused as a comparison",
                st is None and "draft" in (why or ""), why)
    finally:
        M.STATE_DIR = old


def fx_stale_driver():
    """A 90-day-old filing may not become an inferred current driver."""
    s = clean_snap()
    v = M.build(s)
    v["catalysts"]["last_reported"]["age_days"] = 90
    v["catalysts"]["current_driver"] = {
        "grade": M.INFERRED, "references_catalyst": True,
        "text": "the print from three months ago still explains the tape"}
    st, obs = status_of(V.check_model(v, s, evidence_for()),
                        "STALE_CATALYST_NO_INFERENCE")
    return ("stale catalyst cannot be laundered into an inferred driver",
            st == V.FAIL, obs)


def fx_market_cap_basis():
    """Market cap taken from a vendor field, not price x filed shares."""
    s = clean_snap()
    v = M.build(s)
    ev = evidence_for(calcs={"CALC-market_cap": {
        "calculation_id": "CALC-market_cap",
        "formula": "market cap as published by the data vendor",
        "operands": [{"evidence_id": "REC-vendor_cap", "resolved": True,
                      "value": 4.1e10}],
        "operands_complete": True, "result_unrounded": 4.1e10,
        "result_displayed": "$41.0B", "displayed": True}})
    st, obs = status_of(V.check_model(v, s, ev), "MARKET_CAP_BASIS")
    return ("market cap without an observed-price and filed-share operand",
            st == V.FAIL, obs)


def fx_pe_operands():
    """A multiple whose operands cannot be produced from the package."""
    s = clean_snap()
    v = M.build(s)
    ev = evidence_for(calcs={"CALC-pe_trailing": {
        "calculation_id": "CALC-pe_trailing",
        "formula": "last close / GAAP TTM diluted EPS",
        "operands": [{"evidence_id": "CALC-last_close", "resolved": False},
                     {"evidence_id": "CALC-eps_ttm", "resolved": False}],
        "operands_complete": False, "result_unrounded": 22.0,
        "result_displayed": "22.0x", "displayed": True}})
    checks = V.check_model(v, s, ev)
    st, _ = status_of(checks, "PE_OPERANDS_SHOWN")
    st2, _ = status_of(checks, "CALC_OPERAND_COMPLETENESS")
    return ("P/E shown with operands that cannot be reproduced",
            st == V.FAIL and st2 == V.FAIL,
            "PE_OPERANDS_SHOWN=%s CALC_OPERAND_COMPLETENESS=%s" % (st, st2))


def fx_stale_ownership():
    """Filing rows old enough to be history rather than positioning."""
    s = clean_snap()
    v = M.build(s)
    v["ownership"] = {"rows": [{"form": "SC 13G", "filer": None,
                                "accession": "0001", "stake": None}],
                      "n": 1, "shown_count": 1, "oldest_age_days": 1628,
                      "filers_parsed": False,
                      "interpretation": "interpretation unavailable"}
    st, obs = status_of(V.check_model(v, s, evidence_for()),
                        "OWNERSHIP_FILING_AGE")
    return ("ownership filings past the staleness threshold are reported",
            st in (V.FAIL, V.WARN_S), obs)


def fx_news_count():
    """The document states a count the rendered rows contradict."""
    import report_v3 as R3
    from reportlab.pdfgen import canvas
    buf = _io.BytesIO()
    cv = canvas.Canvas(buf, pagesize=R3.LETTER)
    cv.setFont(R3.FONT, 10)
    cv.drawString(50, 720, "2 of 2 items admitted after the relevance check")
    for i, d in enumerate(("2026-07-21 16:11", "2026-07-21 14:43",
                           "2026-07-20 15:15", "2026-07-19 09:02")):
        cv.drawString(50, 700 - i * 14, "A headline  · %s ET" % d)
    cv.showPage()
    cv.save()
    s = clean_snap()
    v = M.build(s)
    st, obs = status_of(
        V.check_agreement(buf.getvalue(), evidence_for(), v, s),
        "NEWS_COUNT_RECONCILE")
    return ("stated admitted count below the number of rows rendered",
            st == V.FAIL, obs)


def fx_modified_after_validation():
    """A PDF edited after its manifest was signed."""
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "core.pdf")
    original = b"%PDF-1.4 the bytes that were validated"
    with open(path, "wb") as fh:
        fh.write(original)
    manifest = {"core_pdf": {"sha256": V._sha(original)}}
    before = V.verify_delivered({"core_pdf": path}, manifest)
    with open(path, "ab") as fh:
        fh.write(b" ...and one more line added afterwards")
    after = V.verify_delivered({"core_pdf": path}, manifest)
    return ("a PDF modified after validation no longer matches its manifest",
            all(c["status"] == V.PASS for c in before)
            and any(c["status"] == V.FAIL for c in after),
            "before=%s after=%s" % ([c["status"] for c in before],
                                    [c["status"] for c in after]))


FIXTURES = [fx_draft_baseline, fx_stale_driver, fx_market_cap_basis,
            fx_pe_operands, fx_stale_ownership, fx_news_count,
            fx_modified_after_validation]


def main():
    print("negative fixtures - each must be caught by the check that owns it\n")
    for f in FIXTURES:
        name, ok, detail = f()
        chk(name, ok, detail)

    print("\ncontrol")
    s = clean_snap()
    v = M.build(s)
    bad = [c for c in V.check_model(v, s, evidence_for())
           if c["status"] == V.FAIL]
    chk("CONTROL: a clean package raises no model failure", not bad,
        [c["check_id"] for c in bad])

    print("\n%d/%d checks passed" % (RAN[0] - len(FAILS), RAN[0]))
    if FAILS:
        print("FAILED: " + "; ".join(FAILS))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
