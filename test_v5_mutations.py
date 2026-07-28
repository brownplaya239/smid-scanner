#!/usr/bin/env python3
"""test_v5_mutations.py — mutation proofs for every v5.6 semantic check
(§16).

Each mutation constructs the exact defect its check exists to catch,
runs it through THE SAME function the validator calls, and records the
observed failure. The proofs are written to
data/mutation_proofs/MUTATIONS.json; report_v5_run embeds the
collection in every validation JSON as `mutation_tests` and FAILS
(MUTATION_PROOF_PRESENT) when a semantic check lacks a proven mutation.

Mutations operate on copies — the clean artifact is untouched, so
restoration is structural ("input discarded"); the clean run's own PASS
is the restored-state proof.
"""

import json
import os
import sys
import tempfile
from datetime import date, timedelta

import report_v5_checks as CK
import report_v5_ledger as LG
import test_v5_no_ticker_branches as TB

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "data", "mutation_proofs", "MUTATIONS.json")

RESULTS = []


def prove(mutation_id, changed, intended_check, expected, violations):
    proven = bool(violations)
    RESULTS.append({
        "mutation_id": mutation_id,
        "changed_field_or_artifact": changed,
        "intended_check": intended_check,
        "expected_failure": expected,
        "observed_failure": "; ".join(str(v) for v in violations[:3])
        or "NOT DETECTED",
        "proven": proven,
        "restoration_status": "mutation applied to a copy; clean "
                              "artifact untouched (clean-run PASS is "
                              "the restored proof)",
    })
    print("  %s  %-46s %s" % ("PROVEN" if proven else "MISSED",
                              intended_check, mutation_id))
    return proven


LONG = "x" * 400
CLEAN_LEDGER = {"ids": {"XBRL-1-us-gaap:Revenues-2026-03-31": "x",
                        "CALC-net_margin": "x"}, "hash": "h", "count": 2}

# ── presentation ─────────────────────────────────────────────────────
prove("M01-orphan-page", "appended a 12-char final page",
      "NO_ORPHAN_PAGE", "text-starved page detected",
      CK.orphan_pages([LONG, LONG, "Equity  v5"]))
prove("M02-orphan-bullet", "final page reduced to one bullet",
      "NO_ORPHAN_BULLET", "single stranded bullet detected",
      CK.orphan_bullets([LONG, "TICK  Equity Research v5\n"
                               "• the one stranded bullet line"]))
prove("M03-raw-identifier", "injected 'revenue_q' into core text",
      "HUMAN_READABLE_METRIC_LABELS", "raw schema identifier detected",
      CK.raw_identifiers(LONG + " revenue_q " + LONG))
prove("M04-overflow", "page text grown past the frame budget",
      "CORE_LAYOUT_NO_OVERFLOW", "overlong page detected",
      CK.overflow_pages(["y" * 6000]))

# ── evidence ledger ──────────────────────────────────────────────────
prove("M05-prose-ref", "free-form sentence used as an evidence ref",
      "EVIDENCE_REF_EXISTS_IN_LEDGER", "unresolved reference",
      LG.unresolved(["growth is strong because the market is big"],
                    CLEAN_LEDGER))
prove("M06-prose-counter-ref", "prose used as a counterevidence ref",
      "COUNTEREVIDENCE_REF_EXISTS_IN_LEDGER", "unresolved reference",
      LG.unresolved(["profitability holds: net margin 12%"],
                    CLEAN_LEDGER))
prove("M07-technical-counter", "CALC-ma200 attached as counterevidence "
      "to a fundamental claim", "COUNTEREVIDENCE_RELEVANT_TO_CLAIM",
      "same-proposition rule violated",
      LG.irrelevant_counters({"claim_type": "fundamental",
                              "counterevidence_refs": ["CALC-ma200"]}))

# ── framework / routing / adapter ────────────────────────────────────
import report_v5_framework as FW

_good_dims = {k: {"status": "NOT_ASSESSED"} for k in FW.TIGER_DIMENSIONS}
_mut = dict(_good_dims)
del _mut["unit_economics"]
prove("M08-missing-dimension", "unit_economics removed from coverage",
      "FRAMEWORK_COVERAGE_PRESENT", "missing dimension detected",
      CK.framework_issues({"dimensions": _mut}))
prove("M09-full-without-coverage", "archetype forced FULL with "
      "NOT_ASSESSED framework dimensions",
      "FULL_REQUIRES_DILIGENCE_COVERAGE", "missing dimensions listed",
      CK.full_coverage_issues("FULL", {"summary": {
          "missing_for_full": ["industry_structure",
                               "management_record"]}}))
prove("M10-adapter-dropped", "adapter record cleared before render",
      "SECTOR_ADAPTER_APPLIED", "no adapter selected",
      CK.adapter_issues({}, LONG, "FULL_THIN"))
prove("M10b-adapter-not-rendered", "dashboard heading missing from "
      "core text", "SECTOR_ADAPTER_APPLIED", "dashboard not rendered",
      CK.adapter_issues({"key": "restaurant",
                         "label": "Restaurant / multi-unit consumer"},
                        LONG, "FULL_THIN"))

# ── variant / event / historical semantics ───────────────────────────
prove("M11-our-variant", "'Our variant' injected with no sourced gap",
      "VARIANT_WORDING_REQUIRES_EXPECTATIONS_GAP",
      "banned variant wording detected",
      CK.variant_wording_issues("Our variant is that growth "
                                "re-accelerates.", False))
prove("M11b-affirmative-variant", "affirmative 'Variant perception:' "
      "without expectations",
      "VARIANT_WORDING_REQUIRES_EXPECTATIONS_GAP",
      "affirmative variant without gap",
      CK.variant_wording_issues("Variant perception: we are above "
                                "consensus on revenue.", False))
prove("M12-stale-event-state", "POST-CALL state one day before the "
      "next print", "EVENT_STATE_RESPECTS_UPCOMING_EVENT",
      "post-call inside pre-event window",
      CK.event_state_issues({"state": "POST-CALL VERIFIED",
                             "next_event_date":
                                 (date.today() + timedelta(days=1)
                                  ).isoformat()}))
prove("M13-scenario-language", "'base case' injected into historical "
      "text", "NO_SCENARIO_LANGUAGE_IN_HISTORICAL_RANGE",
      "banned vocabulary detected",
      CK.scenario_language_issues("our base case implies upside",
                                  "historical_range"))
prove("M14-window-label", "'3.0-year' label with actual_years 2.0",
      "HISTORICAL_WINDOW_LABEL_MATCHES_ACTUAL", "label mismatch",
      CK.window_label_issues("over the available 3.0-year history",
                             2.0))
prove("M15-expected-return", "'expected return' rendered in historical "
      "mode", "HISTORICAL_RANGE_NOT_USED_AS_EXPECTED_RETURN",
      "return language on a historical range",
      CK.historical_expected_return_issues(
          "the expected return to the median is 25%",
          "historical_range", None))
prove("M16-graded-ia", "IA graded MODERATE in historical mode",
      "INVESTMENT_ATTRACTIVENESS_HAS_UNDERWRITING",
      "grade without underwriting",
      CK.ia_underwriting_issues("MODERATE", "historical_range"))

# ── separation / ordering / confidence / checkpoints ─────────────────
prove("M17-merged-invalidation", "tactical invalidation line removed",
      "FUNDAMENTAL_AND_TACTICAL_INVALIDATION_SEPARATE",
      "missing invalidation line",
      CK.invalidation_separation_issues(
          "Fundamental invalidation: growth below 20%."))
prove("M18-shuffled-stages", "monitoring stages shuffled out of order",
      "TECHNICAL_STAGES_ORDERED", "non-monotonic thresholds",
      CK.stage_order_issues([{"condition": "close above $30"},
                             {"condition": "close above $20"},
                             {"condition": "close above $25"}]))
prove("M19-four-axes", "one confidence axis removed",
      "DATA_CONFIDENCE_DECOMPOSED", "missing axis detected",
      CK.confidence_axes_issues({"axes": {
          "source_integrity": {}, "quantitative_coverage": {},
          "qualitative_coverage": {}, "expectations_coverage": {}}}))
prove("M20-untyped-checkpoint", "checkpoint replaced by 'someday'",
      "NEXT_CHECKPOINT_TYPE_VALID", "untyped checkpoint detected",
      CK.checkpoint_type_issues([{"claim_id": "c1",
                                  "next_checkpoint": "someday"}]))

# ── appendix binding ─────────────────────────────────────────────────
prove("M21-v4-appendix", "appendix headed 'Equity Research v4'",
      "APPENDIX_VERSION_MATCH", "v4 metadata detected",
      CK.appendix_version_issues(
          "Equity Research v4 — Appendix\naudit trail"))
prove("M22-wrong-report-id", "appendix carries a different report ID",
      "APPENDIX_REPORT_ID_MATCH", "report ID absent",
      CK.appendix_report_id_issues("report ID TT-2026-01-01T00:00:00",
                                   "TT-2026-07-28T12:00:00"))
prove("M23-band-denial", "appendix claims the band was not produced "
      "while the core renders one", "APPENDIX_METHOD_MATCHES_CORE",
      "method contradiction detected",
      CK.appendix_method_issues(
          "a historical multiple band was deliberately not produced",
          True, "pe"))
prove("M24-hash-flip", "core hash stripped from the appendix",
      "APPENDIX_HASH_MATCH", "hash absent",
      CK.appendix_hash_issues("no hashes recorded here", "ab12cd34"))
prove("M25-ledger-hash-flip", "ledger hash stripped from the appendix",
      "SOURCE_LEDGER_HASH_MATCH", "ledger hash absent",
      CK.ledger_hash_issues("nothing bound", "deadbeef01"))

# ── Sundheim completeness + appendix scenario language ───────────────
_good_sd = {k: "x" for k in CK.SUNDHEIM_REQUIRED_FIELDS}
_good_sd["questions"] = [{"question": "q%d" % i, "answer": "a"}
                         for i in range(12)]
_mut_sd = dict(_good_sd)
_mut_sd["questions"] = _mut_sd["questions"][:11]
prove("M29-sundheim-question-dropped", "one of the twelve questions "
      "removed from the decision object", "SUNDHEIM_DECISION_COMPLETE",
      "incomplete question set detected",
      CK.sundheim_issues(_mut_sd))
prove("M29b-sundheim-field-dropped", "reunderwrite_when removed from "
      "the decision object", "SUNDHEIM_DECISION_COMPLETE",
      "missing stored field detected",
      CK.sundheim_issues({k: v for k, v in _good_sd.items()
                          if k != "reunderwrite_when"}))
prove("M30-appendix-scenario-language", "'scenario table' injected "
      "into a historical-mode appendix",
      "NO_SCENARIO_LANGUAGE_IN_HISTORICAL_RANGE_APPENDIX",
      "banned vocabulary detected in the appendix surface",
      CK.scenario_language_issues("methodology: the scenario table "
                                  "was computed", "historical_range"))

# ── universal-ticker scan ────────────────────────────────────────────
_tmp = tempfile.mkdtemp()
_mod = os.path.join(_tmp, "report_v5_claims.py")
with open(_mod, "w", encoding="utf-8") as fh:
    fh.write('def f(t):\n    if t == "HO' + 'OD":\n'
             '        return "special"\n')
_hits, _branches = TB.scan_module(_mod, "report_v5_claims.py")
prove("M26-ticker-branch", "pilot-symbol comparison injected into a "
      "shared module", "NO_TICKER_SPECIFIC_BRANCH",
      "ticker-keyed branch detected", _hits + _branches)
os.remove(_mod)
os.rmdir(_tmp)

# ── write the proofs file ────────────────────────────────────────────
os.makedirs(os.path.dirname(OUT), exist_ok=True)
proven = [r for r in RESULTS if r["proven"]]
doc = {"schema": "v5-mutation-proofs/1",
       "mutation_tests": RESULTS,
       "proven": len(proven), "total": len(RESULTS)}
with open(OUT, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, indent=1, sort_keys=True)

print("\n%d/%d mutations proven -> %s" % (len(proven), len(RESULTS),
                                          OUT))
sys.exit(0 if len(proven) == len(RESULTS) else 1)
