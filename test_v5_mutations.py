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

# ── v5.7 §1: point-in-time integrity ─────────────────────────────────
import report_v5_multiples as MU

_now = date(2026, 7, 28)
_stale_events = [{"end": e, "val": 1.0,
                  "available_from": "2014-%02d-01" % (i + 3)}
                 for i, e in enumerate(("2013-09-30", "2013-12-31",
                                        "2014-03-31", "2014-06-30"))]
_rec_stale = MU.ttm_integrity(_stale_events, _now)
prove("M31-2014-quarter-in-ttm", "a 2014 quarter series presented as "
      "the current TTM", "TTM_END_DATE_IS_CURRENT",
      "stale trailing year detected",
      ["not current"] if not _rec_stale["current"] else [])
prove("M31b-ttm-suppressed", "ttm_at() must refuse the stale series",
      "TTM_END_DATE_IS_CURRENT", "value suppressed",
      ["suppressed"] if MU.ttm_at(_stale_events, _now) is None else [])
_gap_events = [{"end": e, "val": 1.0, "available_from": "2026-01-01"}
               for e in ("2024-03-31", "2024-06-30", "2025-12-31",
                         "2026-03-31")]
prove("M32-noncontiguous-quarters", "a filing gap inside the four "
      "quarters", "TTM_HAS_FOUR_CONTIGUOUS_QUARTERS",
      "non-contiguous quarters detected",
      MU.ttm_integrity(_gap_events, _now)["reasons"])
prove("M33-mixed-concepts", "band published with no recorded concept "
      "(mixed-source series)", "TTM_CONCEPT_IS_CONSISTENT",
      "missing concept detected",
      CK.ttm_integrity_issues({"pe": {"available": True},
                               "ttm_integrity": {"pe": {
                                   "ok": True, "concept": None}}}))
prove("M34-stale-debt-current-cash", "a 2017 debt instant netted "
      "against current cash", "BALANCE_SHEET_FACTS_SHARE_PERIOD",
      "cross-period netting detected",
      CK.balance_sheet_period_issues(
          {"cash": {"v": 1e9, "period_end": "2026-03-31"},
           "debt": {"v": 2e9, "period_end": "2017-12-31"}},
          {"metrics_used": ["net_cash"]}))
prove("M35-freshness-from-newest", "conclusion freshness copied from "
      "the newest reference", "FRESHNESS_MATCHES_OLDEST_MATERIAL_REF",
      "wrong freshness basis detected",
      CK.freshness_basis_issues(
          {"metrics_used": ["net_margin", "net_cash"],
           "freshness_basis": "2026-03-31"},
          {"net_margin": {"period_end": "2026-03-31"},
           "cash": {"period_end": "2026-03-31"},
           "debt": {"period_end": "2021-12-31"}}))
prove("M36-wrong-issuer", "ledger stamped with a different issuer's "
      "CIK", "EVIDENCE_BELONGS_TO_ISSUER", "issuer mismatch detected",
      CK.issuer_issues({"issuer_cik": "0000320193"}, "0000789019"))
prove("M36b-stale-support", "a published claim resting on stale "
      "critical evidence", "STALE_EVIDENCE_CANNOT_SUPPORT_CONCLUSION",
      "stale support detected",
      CK.stale_support_issues([{"claim_id": "c1",
                                "freshness": {"stale": True}}]))
prove("M36c-mixed-claim-periods", "one claim citing facts from two "
      "different years", "MATERIAL_EVIDENCE_PERIODS_COMPATIBLE",
      "incompatible periods detected",
      CK.claim_period_issues([{
          "claim_id": "c1", "claim_type": "fundamental",
          "evidence_refs": ["XBRL-1-us-gaap:Revenues-2026-03-31",
                            "XBRL-2-us-gaap:NetIncomeLoss-2021-12-31"],
          "counterevidence_refs": []}]))

# ── v5.7 §2: sector compatibility ────────────────────────────────────
import report_v5_adapters as ADP2

_reit = {"key": "reit", "policy": ADP2.policy_for("reit")}
_bank = {"key": "bank_insurer",
         "policy": ADP2.policy_for("bank_insurer")}
prove("M37-reit-cash-conversion-claim", "an OCF self-funding claim "
      "published for a REIT", "CLAIM_IS_COMPATIBLE_WITH_SECTOR",
      "sector-incompatible claim detected",
      CK.claim_sector_issues(
          {"claims": [{"claim_id": "cash-conversion"}]}, _reit))
prove("M38-reit-pe-without-ffo", "P/E used as a REIT's valuation "
      "anchor with no FFO",
      "VALUATION_METHOD_IS_COMPATIBLE_WITH_SECTOR",
      "incompatible method detected",
      CK.valuation_sector_issues({"available": True,
                                  "metric_kind": "pe"}, _reit))
prove("M39-bank-generic-revenue", "a bank graded on generic revenue "
      "growth", "BUSINESS_QUALITY_USES_PERMITTED_METRICS",
      "forbidden quality metric detected",
      CK.quality_metric_issues({"metrics_used": ["revenue_growth"]},
                               _bank))
prove("M39b-ungoverned-builder", "argument builder ran without the "
      "adapter policy", "ADAPTER_GOVERNS_ARGUMENT_BUILDER",
      "governance gap detected",
      CK.adapter_governance_issues({"adapter_key": None},
                                   {"valuation_policy":
                                    {"adapter": "reit"}}, _reit))

# ── v5.7 §4: whole-word vocabulary + JSON surface ────────────────────
prove("M40-scenario-probabilities", "'scenario probabilities' in a "
      "historical-mode surface",
      "NO_SCENARIO_LANGUAGE_IN_HISTORICAL_RANGE",
      "whole-word scenario token detected",
      CK.scenario_language_issues(
          "the range carries no scenario probabilities",
          "historical_range"))
prove("M40b-json-surface", "'scenario' leaked into the validation "
      "JSON", "NO_SCENARIO_LANGUAGE_IN_VALIDATION_JSON",
      "token detected in serialized record",
      CK.json_scenario_issues({"router": {"reasons":
                                          ["scenario table required"]},
                               "checks": []}, "historical_range"))

# ── v5.7 §5: rendered layout ─────────────────────────────────────────
_sd_fix = {"questions": [{"question": "Q1",
                          "answer": "the full answer that must render "
                                    "without clipping anywhere"},
                         {"question": "Q2",
                          "answer": "a second substantial answer that "
                                    "also flows onto the next page"}]}
prove("M41-clipped-sundheim-row", "a Sundheim answer clipped from the "
      "rendered appendix", "APPENDIX_TABLE_CELL_NOT_CLIPPED",
      "clipped cell detected",
      CK.sundheim_render_issues("Question Answer (sourced) Q1 the "
                                "full ans", _sd_fix))
prove("M42-missing-repeated-header", "a continuation page with rows "
      "but no repeated header", "TABLE_HEADER_REPEATED_AFTER_BREAK",
      "missing header detected",
      CK.sundheim_header_issues(
          ["Sundheim decision record\nQuestion\nQ1",
           "Q2\nthe full answer that must render without clipping "
           "anywhere\na second substantial answer that also flows "
           "onto the next page"],
          _sd_fix))
prove("M43-sparse-final-page", "a 6%-occupancy final appendix page",
      "NO_LOW_DENSITY_FINAL_PAGE", "sparse tail detected",
      [b for b in CK.page_occupancy_issues([0.8, 0.7, 0.06])
       if "final" in b])
prove("M43b-stranded-tail", "a page opening mid-sentence",
      "NO_STRANDED_SECTION_TAIL", "stranded continuation detected",
      CK.stranded_tail_issues(["TICK Equity Research v5\nnormal page",
                               "TICK Equity Research v5\ncontinued "
                               "sentence fragment here"]))

prove("M45-latest-label-stale-fact", "'latest filed quarter' rendered "
      "over a 2024 quarter", "LATEST_LABEL_REQUIRES_CURRENT_FACT",
      "stale 'latest' label detected",
      CK.latest_label_issues(
          "growth from the latest filed quarter",
          {"revenue_q": {"v": 1e9, "period_end": "2024-06-30"}},
          today=date(2026, 7, 28)))
prove("M39c-ungoverned-valuation", "valuation selection ran without "
      "the adapter policy", "ADAPTER_GOVERNS_VALUATION",
      "governance gap detected",
      [g for g in CK.adapter_governance_issues(
          {"adapter_key": "reit"},
          {"valuation_policy": {"adapter": None}}, _reit)
       if "valuation" in g])
prove("M46-sparse-interior-page", "a 10%-occupancy interior appendix "
      "page", "APPENDIX_PAGE_OCCUPANCY", "sparse interior page "
      "detected",
      [b for b in CK.page_occupancy_issues([0.8, 0.10, 0.7, 0.5])
       if "final" not in b])

# ── v5.7 §6: provenance ──────────────────────────────────────────────
prove("M44-wrong-source-commit", "artifact recording a commit that is "
      "not the generating commit", "PROVENANCE_VALID",
      "commit mismatch detected",
      CK.provenance_issues({"generator_version": "v5.8",
                            "source_commit_sha": "aaaa",
                            "git_tree_sha": "t", "generated_at": "x",
                            "report_id": "r",
                            "dirty_worktree": False},
                           head_sha="bbbb"))

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

# ── v5.8 §7: classification, balance-sheet, grid-TTM, ledger, layout ─

prove("M47-missing-revenue-called-pre-revenue",
      "mature issuer's stage rewritten PRE_REVENUE with a "
      "data-availability basis (revenue not parsed)",
      "MISSING_DATA_IS_NOT_PRE_REVENUE",
      "data-gap basis rejected",
      CK.missing_data_pre_revenue_issues({
          "business_stage": "PRE_REVENUE",
          "business_stage_basis": "revenue not parsed from the vendor "
                                  "snapshot; concept fetch unavailable",
          "classification_evidence_refs": []}))

prove("M47b-pre-revenue-without-evidence",
      "PRE_REVENUE stage with no resolvable evidence refs",
      "PRE_REVENUE_REQUIRES_ADMITTED_EVIDENCE",
      "missing admitted evidence detected",
      CK.pre_revenue_evidence_issues({
          "business_stage": "PRE_REVENUE",
          "business_stage_basis": "the issuer's full SEC concept index "
                                  "contains no revenue concept in any "
                                  "taxonomy",
          "classification_evidence_refs": []}, CLEAN_LEDGER))

prove("M48-pre-revenue-issuer-made-generic",
      "an actual pre-revenue biotech's stage flipped to OPERATING / "
      "generic adapter against its registered expectation",
      "EXPECTED_BUSINESS_STAGE_MATCHES_ACTUAL",
      "stage mismatch detected",
      [m for m in CK.expected_routing_issues(
          {"expected_adapter": "biotech",
           "expected_business_stage": "PRE_REVENUE",
           "expected_accounting_regime": "us-gaap"},
          {"business_stage": "OPERATING",
           "accounting_regime": "us-gaap"},
          {"key": "generic"})
       if m[0] == "expected_business_stage"])

prove("M49-adapter-mismatch",
      "energy fixture routed to the pre-revenue adapter",
      "EXPECTED_ADAPTER_MATCHES_ACTUAL",
      "adapter mismatch detected",
      [m for m in CK.expected_routing_issues(
          {"expected_adapter": "energy_materials",
           "expected_business_stage": "OPERATING",
           "expected_accounting_regime": "us-gaap"},
          {"business_stage": "OPERATING",
           "accounting_regime": "us-gaap"},
          {"key": "pre_revenue"})
       if m[0] == "expected_adapter"])

prove("M49b-regime-mismatch",
      "IFRS filer recorded as us-gaap against its expectation",
      "EXPECTED_ACCOUNTING_REGIME_MATCHES_ACTUAL",
      "regime mismatch detected",
      [m for m in CK.expected_routing_issues(
          {"expected_adapter": "subscription_software",
           "expected_business_stage": "OPERATING",
           "expected_accounting_regime": "ifrs"},
          {"business_stage": "OPERATING",
           "accounting_regime": "us-gaap"},
          {"key": "subscription_software"})
       if m[0] == "expected_accounting_regime"])

prove("M50-ifrs-absence-called-pre-revenue",
      "issuer with unparsed IFRS facts labelled PRE_REVENUE",
      "MISSING_DATA_IS_NOT_PRE_REVENUE",
      "ifrs data gap rejected as a stage basis",
      CK.missing_data_pre_revenue_issues({
          "business_stage": "PRE_REVENUE",
          "business_stage_basis": "no us-gaap facts parsed (ifrs "
                                  "filer) — fetch degraded, revenue "
                                  "missing",
          "classification_evidence_refs": ["SEC-CONCEPT-INDEX-1"]}))

_M51_FU = {"cash": {"v": 2.0e9, "period_end": "2026-03-31"},
           "debt": {"v": 1.5e9, "period_end": "2025-09-30"}}
prove("M51-cross-period-balance-sheet",
      "cash (2026-03-31) and debt (2025-09-30, two quarters older) "
      "joined in a framework conclusion",
      "SERIALIZED_BALANCE_SHEET_FACTS_SHARE_PERIOD",
      "cross-period pairing detected on a serialized surface",
      CK.serialized_bs_issues(
          {"fundamentals": _M51_FU},
          {"framework": {"dimensions": {"balance_sheet": {
              "status": "PARTIAL",
              "conclusion": "cash $2.00B vs debt $1.50B from the "
                            "latest filed balance sheet"}}}},
          []))

prove("M51b-dashboard-cross-period",
      "dashboard renders both instants despite incompatible periods",
      "DASHBOARD_CONCLUSIONS_RESPECT_FRESHNESS",
      "dashboard pairing violation detected",
      CK.dashboard_bs_issues(
          {"fundamentals": _M51_FU},
          [["Cash / debt", "$2.00B / $1.50B", "filed (SEC XBRL)"]]))

prove("M52-latest-with-stale-fact",
      "framework prose says 'latest' while a supporting fact is dated "
      "2021",
      "LATEST_LABEL_MATCHES_EACH_SUPPORTING_FACT",
      "stale fact behind 'latest' wording detected",
      [b for b in CK.framework_bs_freshness_issues(
          {"dimensions": {"balance_sheet": {
              "status": "PARTIAL",
              "conclusion": "cash $2.00B from the latest filed "
                            "balance sheet",
              "evidence_refs": [
                  "XBRL-1-us-gaap:CashAndCashEquivalentsAtCarrying"
                  "Value-2021-12-31"]}}},
          today=date(2026, 7, 28)) if "latest" in b])

prove("M52b-stale-supported-conclusion",
      "graded dimension quotes figures whose newest fact is from 2021",
      "STALE_EVIDENCE_CANNOT_SUPPORT_ANY_RENDERED_CONCLUSION",
      "stale-supported conclusion detected",
      CK.stale_rendered_support_issues(
          {"dimensions": {"balance_sheet": {
              "status": "PARTIAL",
              "conclusion": "cash $2.00B on hand",
              "evidence_refs": [
                  "XBRL-1-us-gaap:CashAndCashEquivalentsAtCarrying"
                  "Value-2021-12-31"]}}},
          today=date(2026, 7, 28)))

_M53_GRID = {"years": ["2025-12-31"],
             "gaps": ["TTM suppressed for revenue: revenue — newest "
                      "quarter end 2014-09-30 is stale"],
             "ttm": {"revenue": 5.1e9, "net_income": None,
                     "eps": None, "ocf": None, "fcf": None,
                     "net_margin": None, "through": "2026-06-30",
                     "cells": {"revenue": {"ok": False, "value": None,
                                           "through": "2014-09-30",
                                           "reasons": ["newest quarter "
                                                       "end stale"]}},
                     "suppressed": [{"metric": "revenue",
                                     "reasons": ["stale"]}]}}
prove("M53-suppressed-ttm-still-populated",
      "'TTM suppressed' footnote printed while the revenue TTM cell "
      "keeps a value",
      "TTM_FOOTNOTE_MATCHES_RENDERED_VALUES",
      "populated cell beside a suppression note detected",
      CK.grid_ttm_footnote_issues(_M53_GRID, "TTM suppressed"))

prove("M54-grid-cell-invalid-but-displayed",
      "the JPM defect: net-income TTM displayed although its own "
      "stream failed the four-quarter test (only the P/E stream was "
      "checked)",
      "EVERY_DISPLAYED_TTM_CELL_VALID",
      "per-cell validation catches the displayed invalid cell",
      CK.grid_ttm_cell_issues(
          {"ttm": {"net_income": 4.2e9,
                   "cells": {"net_income": {
                       "ok": False,
                       "reasons": ["quarter ends are not contiguous"],
                       "through": "2014-09-30"}}}}))

prove("M54b-mixed-ttm-endpoints",
      "two populated TTM cells with endpoints twelve years apart in "
      "one unlabeled column",
      "TTM_GRID_ENDPOINTS_ARE_CONSISTENT",
      "mixed endpoints detected",
      CK.grid_ttm_endpoint_issues(
          {"ttm": {"revenue": 9e9, "net_income": 4e9,
                   "through": "2026-06-30",
                   "cells": {"revenue": {"ok": True,
                                         "through": "2026-06-30"},
                             "net_income": {"ok": True,
                                            "through": "2014-09-30"}
                             }}}))

# sparse-first registration: the legacy first-write-wins path leaves a
# null-kind record; the v5.8 merge path enriches it. Both halves are
# asserted — the defect detected AND the fix effective.
_legacy = {"CALC-fcf_margin": {"kind": None}}
_rich = {"kind": "derived_figure", "metric": "fcf_margin",
         "calculation": "fcf / revenue",
         "input_evidence_ids": ["CALC-fcf", "XBRL-1-us-gaap:"
                                "Revenues-2026-03-31"]}
_legacy.setdefault("CALC-fcf_margin", _rich)      # first-write-wins
_merged = {"CALC-fcf_margin": {"kind": None}}
LG._register(_merged, "CALC-fcf_margin", _rich)
assert _merged["CALC-fcf_margin"]["kind"] == "derived_figure", \
    "merge failed to enrich"
prove("M55-sparse-record-blocks-rich",
      "sparse registration first; richer record discarded by "
      "first-write-wins (v5.7 behaviour)",
      "LEDGER_MERGE_PRESERVES_RICHEST_RECORD",
      "null-kind survivor detected; merge path proven to enrich",
      CK.ledger_kind_issues({"ids": _legacy}))

_M56_LEDGER = {"ids": {
    "XBRL-9-us-gaap:Revenues-2026-03-31": {
        "kind": "xbrl_fact", "accession": "9",
        "taxonomy": "us-gaap", "concept": "Revenues",
        "period_end": "2026-03-31"},          # no value/units/url/cik
    "CALC-revenue_growth": {"kind": "derived_figure",
                            "metric": "revenue_growth"},
}}
_M56_CLAIMS = {"schema": "v5-claims/2", "claims": [
    {"claim_id": "growth-above-bar", "claim": "revenue grew",
     "evidence_refs": ["XBRL-9-us-gaap:Revenues-2026-03-31",
                       "CALC-revenue_growth"]}]}
prove("M56-incomplete-xbrl-claim-ref",
      "cited XBRL record stripped of value, units, URL and issuer CIK",
      "XBRL_CLAIM_REF_IS_COMPLETE",
      "incomplete provenance detected",
      CK.xbrl_claim_ref_issues(_M56_CLAIMS, _M56_LEDGER))

prove("M56b-calc-without-formula",
      "cited CALC record stripped of formula and inputs",
      "CALC_RECORD_HAS_FORMULA_AND_INPUTS",
      "formula-less CALC record detected",
      CK.calc_record_issues(_M56_CLAIMS, _M56_LEDGER))

prove("M56c-claim-figure-not-reproducible",
      "claim quotes 23.5% while its only cited record stores 11.2",
      "CLAIM_EVIDENCE_IS_REPRODUCIBLE",
      "irreproducible quoted figure detected",
      CK.claim_reproduction_issues(
          {"schema": "v5-claims/2",
           "claims": [{"claim_id": "growth-above-bar",
                       "claim": "revenue grew 23.5% year over year",
                       "evidence_refs": ["CALC-revenue_growth"]}]},
          {"ids": {"CALC-revenue_growth": {
              "kind": "derived_figure", "value": 11.2}}}))

_m57 = []
for _r in (0.29, 0.37, 0.44):
    _m57 += [b for b in CK.page_occupancy_issues([0.9, 0.9, _r])
             if "final" in b]
assert not [b for b in CK.page_occupancy_issues([0.9, 0.9, 0.46])
            if "final" in b], "45% threshold must pass 46%"
prove("M57-sparse-final-pages",
      "final appendix pages at 29%, 37% and 44% occupancy (each below "
      "the 45% floor; 46% passes)",
      "NO_LOW_DENSITY_FINAL_PAGE",
      "all three sparse finals detected",
      _m57 if len(_m57) == 3 else [])

prove("M58-stranded-validation-summary",
      "final page holding only the section-14 binding table at 30%",
      "NO_STRANDED_VALIDATION_SUMMARY",
      "isolated hash block detected",
      CK.stranded_validation_summary_issues(
          ["1. Framework coverage ...", "14. Validation summary and "
           "artifact hashes Binding record for this package sha256 "
           "abc123"],
          [0.9, 0.30]))



prove("M59-wrong-claims-schema",
      "the v5.7 defect itself: claims delivered under a key the "
      "validators never read ('published') — five checks inspected "
      "ZERO claims and passed",
      "XBRL_CLAIM_REF_IS_COMPLETE",
      "unrecognized claims shape is an explicit failure, never a "
      "silent PASS",
      CK.xbrl_claim_ref_issues(
          {"schema": "v5-claims/2",
           "published": [{"claim_id": "x", "claim": "y",
                          "evidence_refs": [
                              "XBRL-9-us-gaap:Revenues-2026-03-31"]}]},
          _M56_LEDGER))

prove("M60-stale-dashboard-value",
      "the JPM defect: a 2014 quarterly-revenue value displayed on the "
      "sector dashboard as if current",
      "DASHBOARD_VALUES_ARE_CURRENT_AND_SOURCED",
      "stale or undated displayed value detected",
      CK.dashboard_currency_issues(
          [["Quarterly revenue", "$23.42B (as of 2014-09-30)",
            "filed (SEC XBRL)"],
           ["Net margin", "31.0%", "filed (SEC XBRL)"]]))

prove("M61-sparse-core-page",
      "a core report reserving a 9%-occupancy page instead of "
      "collapsing",
      "CORE_PAGE_OCCUPANCY",
      "sparse core page detected",
      CK.page_occupancy_issues([0.9, 0.09, 0.5], final_min=0.30,
                               body_min=0.30))


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
