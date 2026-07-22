#!/usr/bin/env python3
"""report_v3_mutation.py — prove each numeric check can actually fail.

A validation report that says PASS is only worth what its checks are
worth, and a check that never fires is indistinguishable from a check
that is not there. This module takes a clean synthetic package, corrupts
exactly one thing, and asserts that the check which owns that defect
reports FAIL — then puts the results inside the validation report, so a
reader can see the gates were exercised on the same run that passed them.

Every fixture below is a defect v3.1 shipped or would have shipped:

  BAR_CARDINALITY              a declared window wider than the bars sent
  BAR_RANGE_PRESENT            a declared range whose endpoint is absent
  BENCHMARK_OPERANDS           RS vs SPY citing a null placeholder
  PARTIAL_SESSION_NOT_A_CLOSE  an open session published as a close
  LEVEL_WORDING_BASIS          min(low) described as "the lowest close"
  GUIDANCE_PRECISION           $2.565B rendered as $2.56B
  GUIDANCE_PDF_MATCH           a guidance line absent from the page
  COVERAGE_CONSISTENCY         "non-GAAP unavailable" beside parsed non-GAAP
  DISPLAY_COUNT_SCOPE          a bare "displayed" count with no artifact
  CALC_REPRODUCIBLE            a published figure the bars do not give
  HASH_COVERAGE                records shipped without a canonical hash

    python report_v3_mutation.py
"""

import copy
import json
import sys

import report_v3_evidence as EV
import report_v3_validate as V


def _bar(day, c):
    return {"evidence_id": "BAR-%s" % day, "evidence_type": "market_bar",
            "value": {"o": c, "h": c + 1, "l": c - 1, "c": c, "v": 1000},
            "raw_hash": "0" * 64, "disposition": EV.ADMITTED,
            "period": {"session": day, "complete": True}}


def clean_package():
    """A small package that passes every numeric check."""
    days = ["2026-01-%02d" % i for i in range(1, 21)]
    recs = {}
    for i, d in enumerate(days):
        recs["BAR-%s" % d] = _bar(d, 100.0 + i)
    for i, d in enumerate(days):
        recs["SPY-%s" % d] = {"evidence_id": "SPY-%s" % d,
                              "evidence_type": "benchmark_bar",
                              "value": 400.0 + i, "raw_hash": "0" * 64,
                              "disposition": EV.ADMITTED}
    for i, cls in enumerate(("bullish", "bearish", "neutral")):
        recs["SOC-x%d" % i] = {"evidence_id": "SOC-x%d" % i,
                               "evidence_type": "social_post",
                               "classification": cls, "value": "h%d" % i,
                               "disposition": EV.ADMITTED}
    recs["EXH-GUI-revenue"] = {
        "evidence_id": "EXH-GUI-revenue", "evidence_type": "exhibit_guidance",
        "value": {"low": 2565.0, "midpoint": 2700.0, "high": 2835.0},
        "unit": "USD_M", "display": "$2.565B - $2.835B",
        "raw_hash": "0" * 64, "disposition": EV.ADMITTED}
    recs["EXH-REP-non_gaap_gross_margin"] = {
        "evidence_id": "EXH-REP-non_gaap_gross_margin",
        "evidence_type": "exhibit_reported", "value": 58.9, "unit": "%",
        "display": "58.9%", "raw_hash": "0" * 64,
        "disposition": EV.ADMITTED}

    closes = [100.0 + i for i in range(20)]
    calcs = {
        "CALC-ma20": {
            "calculation_id": "CALC-ma20",
            "formula": "mean(close, last 20 completed sessions)",
            "operands": [{"evidence_id": "BAR-%s..BAR-%s"
                          % (days[0], days[-1]), "resolved": True,
                          "members": 20, "expected_members": 20}],
            "operands_complete": True,
            "result_unrounded": round(sum(closes) / 20.0, 2),
            "window_declared": 20, "window_delivered": 20,
            "recomputed": round(sum(closes) / 20.0, 2), "reproducible": True,
            "displayed": True, "result_displayed": "109.50"},
        "CALC-support60": {
            "calculation_id": "CALC-support60",
            "formula": "min(close, last 60 completed sessions)",
            "operands": [], "operands_complete": True,
            "result_unrounded": 100.0, "recomputed": 100.0,
            "reproducible": True, "displayed": True},
        "CALC-rs_vs_spy": {
            "calculation_id": "CALC-rs_vs_spy",
            "formula": "12w return minus SPY",
            "operands": [{"evidence_id": "BAR-%s..BAR-%s"
                          % (days[0], days[-1]), "resolved": True},
                         {"evidence_id": "SPY-%s..SPY-%s"
                          % (days[0], days[-1]), "resolved": True}],
            "operands_complete": True, "result_unrounded": 4.0,
            "benchmark_sessions_delivered": 20, "recomputed": 4.0,
            "reproducible": True, "displayed": True},
        "CALC-intraday_last": {
            "calculation_id": "CALC-intraday_last",
            "formula": "last trade of the open session",
            "operands": [], "operands_complete": True,
            "result_unrounded": 119.0, "recomputed": 119.0,
            "reproducible": True, "displayed": True},
    }
    for _r in recs.values():
        _r.pop("record_hash", None)
        _r["record_hash"] = EV.record_hash(_r)
    return {
        "schema": EV.SCHEMA, "records": recs, "calculations": calcs,
        "calculation_coverage": {"numeric_reproduced": 4, "numeric_failed": 0,
                                 "nonnumeric_exempt": 0, "total": 4,
                                 "failed_detail": [], "exempt_detail": []},
        "populations": {"news": {"admitted": 6, "shown_core": 3,
                                 "shown_appendix": 6}},
        "source_coverage": {"non_gaap_margin": "ADMITTED - parsed from 8-K"},
        "hash_verification": {"coverage_pct": 100.0, "records_total":
                              len(recs), "hash_version": EV.HASH_VERSION},
    }


CLEAN_PDF = ("109.50 the lowest close 58.9% $2.565B $2.835B "
             "3 of 6 items are shown here "
             "10 evidence records - 6 admitted - 3 shown in core - "
             "0 shown in appendix "
             "screened representative excerpts "
             "60-session closing low 52-week closing high")


# ── one corruption each ─────────────────────────────────────────────────

def m_bar_cardinality(pkg, txt):
    pkg["calculations"]["CALC-ma20"]["window_delivered"] = 19
    return pkg, txt


def m_bar_range_present(pkg, txt):
    pkg["records"].pop("BAR-2026-01-01")
    return pkg, txt


def m_benchmark_operands(pkg, txt):
    for k, v in pkg["records"].items():
        if v.get("evidence_type") == "benchmark_bar":
            v["value"] = None
    return pkg, txt


def m_partial_session(pkg, txt):
    pkg["records"]["INTRADAY-2026-01-21"] = {
        "evidence_id": "INTRADAY-2026-01-21",
        "evidence_type": "intraday_observation",
        "value": {"c": 120.0}, "raw_hash": "0" * 64,
        "disposition": EV.ADMITTED}
    pkg["calculations"]["CALC-last_close"] = {
        "calculation_id": "CALC-last_close",
        "formula": "close of the most recent session",
        "operands": [{"evidence_id": "INTRADAY-2026-01-21",
                      "resolved": True}],
        "operands_complete": True, "result_unrounded": 120.0,
        "reproducible": True, "displayed": True}
    return pkg, txt


def m_level_wording(pkg, txt):
    pkg["calculations"]["CALC-support60"]["formula"] = \
        "min(low, last 60 completed sessions)"
    return pkg, txt


def m_guidance_precision(pkg, txt):
    return pkg, txt.replace("$2.565B", "$2.56B")


def m_guidance_pdf_match(pkg, txt):
    return pkg, txt.replace("$2.565B", "").replace("$2.835B", "")


def m_coverage_consistency(pkg, txt):
    pkg["source_coverage"]["non_gaap_margin"] = \
        "not available - non-GAAP measures are not XBRL-tagged"
    return pkg, txt


def m_display_count_scope(pkg, txt):
    pkg["populations"]["news"] = {"records_displayed": 6,
                                  "legacy_records_displayed": 6}
    return pkg, txt


def m_record_hash(pkg, txt):
    """Edit a value and leave the hash alone - the exact tamper the
    recompute check exists to catch."""
    k = "BAR-2026-01-05"
    pkg["records"][k]["value"]["c"] = 999.99
    return pkg, txt


def m_date_alignment(pkg, txt):
    c = pkg["calculations"]["CALC-rs_vs_spy"]
    c["operands"] = [
        {"evidence_id": "BAR-2026-01-01..BAR-2026-01-20", "resolved": True},
        {"evidence_id": "SPY-2026-01-02..SPY-2026-01-21", "resolved": True}]
    return pkg, txt


def m_calc_reproducible(pkg, txt):
    c = pkg["calculations"]["CALC-ma20"]
    c["reproducible"] = False
    c["recompute_note"] = "published 109.50 but the delivered bars give 131.87"
    pkg["calculation_coverage"] = {
        "numeric_reproduced": 3, "numeric_failed": 1, "nonnumeric_exempt": 0,
        "total": 4,
        "failed_detail": [{"calculation_id": "CALC-ma20",
                           "note": c["recompute_note"]}],
        "exempt_detail": []}
    return pkg, txt


def m_hash_coverage(pkg, txt):
    pkg["hash_verification"]["coverage_pct"] = 8.2
    return pkg, txt


def m_display_count_unscoped(pkg, txt):
    return pkg, txt + " 5 records displayed"


def m_social_sample_description(pkg, txt):
    return pkg, txt.replace("screened representative excerpts",
                            "neutral representative excerpts only")


def m_close_extrema_labelled(pkg, txt):
    return pkg, txt.replace("60-session closing low", "60-session low")


MUTATIONS = [
    ("BAR_CARDINALITY",
     "CALC-ma20 declaring a 20-session window with 19 bars delivered",
     m_bar_cardinality),
    ("BAR_RANGE_PRESENT", "a declared range whose start bar is absent",
     m_bar_range_present),
    ("BENCHMARK_OPERANDS", "benchmark observations that are null",
     m_benchmark_operands),
    ("PARTIAL_SESSION_NOT_A_CLOSE", "an open session published as a close",
     m_partial_session),
    ("LEVEL_WORDING_BASIS", "min(low) described on the page as the lowest "
                            "close", m_level_wording),
    ("GUIDANCE_PRECISION", "$2.565B rendered as $2.56B", m_guidance_precision),
    ("GUIDANCE_PDF_MATCH", "a guidance line missing from the page",
     m_guidance_pdf_match),
    ("COVERAGE_CONSISTENCY", "coverage claiming non-GAAP is unavailable "
                             "while exhibit figures are admitted",
     m_coverage_consistency),
    ("DISPLAY_COUNT_SCOPE", "a bare displayed count with no artifact scope",
     m_display_count_scope),
    ("CALC_REPRODUCIBLE",
     "a numeric calculation the delivered bars do not reproduce",
     m_calc_reproducible),
    ("HASH_COVERAGE", "records shipped without a canonical hash",
     m_hash_coverage),
    ("RECORD_HASH_RECOMPUTE",
     "one record value edited without updating its record_hash",
     m_record_hash),
    ("BENCHMARK_DATE_ALIGNMENT",
     "benchmark leg spanning different sessions from the issuer leg",
     m_date_alignment),
    ("DISPLAY_COUNT_UNSCOPED",
     "a rendered count reading '5 records displayed' with no artifact named",
     m_display_count_unscoped),
    ("SOCIAL_SAMPLE_DESCRIPTION",
     "a multi-classification sample described as neutral",
     m_social_sample_description),
    ("CLOSE_EXTREMA_LABELLED",
     "a close-derived 60-session low printed without 'closing'",
     m_close_extrema_labelled),
]


def run():
    """Return one result per mutation, plus the clean control."""
    results = []
    base = clean_package()
    ctrl = [c for c in (V.check_numerics(base, {}, {}, CLEAN_PDF)
                        + V.check_editorial(base, {}, {}, CLEAN_PDF))
            if c["status"] == V.FAIL]
    results.append({
        "check_id": "CONTROL", "mutation": "none - the clean package",
        "expected_status": V.PASS,
        "observed_status": V.FAIL if ctrl else V.PASS,
        "passed": not ctrl,
        "detail": ("unexpected failures: %s" % [c["check_id"] for c in ctrl])
        if ctrl else "every numeric check passes on clean input"})

    for check_id, description, mutate in MUTATIONS:
        pkg, txt = mutate(copy.deepcopy(base), CLEAN_PDF)
        got = None
        for c in (V.check_numerics(pkg, {}, {}, txt)
                  + V.check_editorial(pkg, {}, {}, txt)):
            if c["check_id"] == check_id:
                got = c
                break
        ok = bool(got and got["status"] == V.FAIL)
        results.append({
            "check_id": check_id, "mutation": description,
            "expected_status": V.FAIL,
            "observed_status": (got or {}).get("status", "NOT_RUN"),
            "passed": ok,
            "detail": (got or {}).get("observed")})
    return results


def summary(results=None):
    r = results if results is not None else run()
    return {"schema": "validator_mutation_tests/v1",
            "validator_version": V.VALIDATOR_VERSION,
            "validator_code_sha256": V.validator_code_hash(),
            "total": len(r),
            "passed": len([x for x in r if x["passed"]]),
            "failed": [x["check_id"] for x in r if not x["passed"]],
            "all_checks_proven": all(x["passed"] for x in r),
            "note": ("each fixture corrupts exactly one thing and asserts "
                     "the owning check reports FAIL; the control asserts "
                     "clean input still passes, so a validator that failed "
                     "everything would not score here"),
            "results": r}


def main():
    s = summary()
    for r in s["results"]:
        print("  %-6s %-30s %s"
              % ("PASS" if r["passed"] else "FAIL", r["check_id"],
                 r["mutation"]))
    print("\n%d/%d mutation fixtures caught" % (s["passed"], s["total"]))
    if s["failed"]:
        print("NOT PROVEN: " + ", ".join(s["failed"]))
    return 0 if s["all_checks_proven"] else 1


if __name__ == "__main__":
    sys.exit(main())
