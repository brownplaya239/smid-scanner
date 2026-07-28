#!/usr/bin/env python3
"""report_v5_run.py — v5 orchestrator (pilot).

    python report_v5_run.py TICKER [--out DIR] [--archetype FULL]

Pipeline: snapshot -> v4 view -> multiples -> scenarios (+assumptions)
-> claims -> grid -> router -> archetype-shaped render -> validation.

Pilot validation (slice 7 extends this): the archetype contract checked
against the actually-rendered sections, scenario arithmetic recomputed
from the extracted PDF text, the v4 PDF checks (glyphs, entities,
roundtrip), and the artifact hashes bound into the JSON.
"""

import argparse
import io
import json
import os
import re
import sys
from datetime import datetime, timezone

import report_v4_model as V4
import report_v5 as R5
import report_v5_archetype as A
import report_v5_claims as C5
import report_v5_grid as G5
import report_v5_multiples as M5
import report_v5_scenarios as S5
import research_live as RL

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def build_view(ticker, override=None):
    snap, alt, recs, prov = RL.build_snapshot(ticker)
    if alt and not snap.get("sentiment"):
        snap["sentiment"] = alt
    import estimates_provider as EP
    estimates = EP.fetch_estimates(ticker,
                                   report_time=snap.get("report_time"))
    peers = EP.fetch_peers(ticker)
    v4 = V4.build(snap, estimates=estimates, peers=peers)

    print("  [v5] historical multiples...")
    multiples = M5.build(ticker)
    mk = (prov or {}).get("_mk") or {}
    closes = mk.get("closes") or []
    spot = (closes[-2] if mk.get("partial_session") and len(closes) > 1
            else closes[-1]) if closes else None
    # §2 (v5.7): the adapter is selected BEFORE any analysis so its
    # policy governs valuation-method selection, the argument builder
    # and business-quality grading — not just dashboard labels.
    import report_v5_adapters as ADP
    import report_v5_capability as CAP
    profile = CAP.company_profile(snap, multiples)
    adapter = ADP.classify(profile, snap)
    try:
        adapter["one_time_items"] = ADP.one_time_items(ticker, snap)
    except Exception:
        adapter["one_time_items"] = []
    _pol = dict(adapter.get("policy") or {}, adapter=adapter.get("key"))
    asm, note = S5.load_assumptions(ticker)
    scenarios = S5.build(ticker, multiples, spot, asm, note,
                         valuation_policy=_pol)
    print("  [v5] claims + grid...")
    grid = G5.build(ticker)
    import report_v5_expectations as E5
    expectations = E5.build(snap, grid, multiples, scenarios, estimates,
                            asm)
    claims = C5.build(snap, v4, scenarios, estimates, adapter=adapter)
    import report_v5_assessment as AS
    import report_v5 as _R5
    bq = AS.business_quality(snap, grid, adapter=adapter)
    _conf = _R5.confidence({"v4": v4, "multiples": multiples})
    ia = AS.investment_attractiveness(scenarios, expectations,
                                      v4.get("event") or {},
                                      _conf.get("level"))
    assessment = {"business_quality": bq,
                  "investment_attractiveness": ia,
                  "tension": AS.tension(bq, ia)}
    has_options = None
    try:
        import polygon_data as PG
        has_options = bool(PG.option_chain(ticker, limit=10, max_pages=1))
    except Exception:
        pass
    # v5.5 capability routing: archetype from CompanyProfile +
    # EvidenceCapability + FrameworkCoverage (§6) — never ticker
    # identity. The record carries the categories present/absent, the
    # missing framework dimensions and the full reason chain.
    import report_v5_framework as FW
    capability = CAP.evidence_capability(snap, multiples, estimates,
                                         has_options)
    framework = FW.build_coverage(profile, capability, snap, grid,
                                  multiples, adapter, claims,
                                  expectations, assessment)
    arch = CAP.route(profile, capability, v4.get("event") or {},
                     multiples, has_options=has_options,
                     override=override,
                     override_author="cli" if override else None,
                     override_reason="--archetype flag" if override
                     else None, framework=framework)
    if arch["archetype"] == "NEW_LISTING" \
            and adapter.get("key") != "new_listing":
        adapter = dict(ADP.classify(profile, snap,
                                    archetype="NEW_LISTING"),
                       one_time_items=[])
    print("  [v5] archetype: %s (%s)" % (arch["archetype"],
                                         arch["routing_reason"][:70]))
    view5 = {"v4": v4, "archetype": arch, "multiples": multiples,
             "scenarios": scenarios, "claims": claims, "grid": grid,
             "has_options": has_options, "estimates": estimates,
             "profile": profile, "expectations": expectations,
             "assessment": assessment, "adapter": adapter,
             "framework": framework}
    view5["sundheim"] = FW.sundheim_decision(view5, framework,
                                             arch["archetype"])
    return snap, prov, view5


def _pdf_text(data):
    from pypdf import PdfReader
    return "\n".join(p.extract_text() or ""
                     for p in PdfReader(io.BytesIO(data)).pages)


# Checks whose PASS is meaningless without a recorded negative proof.
_MUTATION_REQUIRED = (
    "NO_ORPHAN_PAGE", "NO_ORPHAN_BULLET", "HUMAN_READABLE_METRIC_LABELS",
    "CORE_LAYOUT_NO_OVERFLOW", "EVIDENCE_REF_EXISTS_IN_LEDGER",
    "COUNTEREVIDENCE_REF_EXISTS_IN_LEDGER",
    "COUNTEREVIDENCE_RELEVANT_TO_CLAIM", "FRAMEWORK_COVERAGE_PRESENT",
    "FULL_REQUIRES_DILIGENCE_COVERAGE", "SECTOR_ADAPTER_APPLIED",
    "VARIANT_WORDING_REQUIRES_EXPECTATIONS_GAP",
    "EVENT_STATE_RESPECTS_UPCOMING_EVENT",
    "HISTORICAL_RANGE_NOT_USED_AS_EXPECTED_RETURN",
    "FUNDAMENTAL_AND_TACTICAL_INVALIDATION_SEPARATE",
    "TECHNICAL_STAGES_ORDERED", "NO_TICKER_SPECIFIC_BRANCH",
    "APPENDIX_VERSION_MATCH", "APPENDIX_REPORT_ID_MATCH",
    "APPENDIX_METHOD_MATCHES_CORE", "APPENDIX_HASH_MATCH",
    "SOURCE_LEDGER_HASH_MATCH",
    "NO_SCENARIO_LANGUAGE_IN_HISTORICAL_RANGE",
    "NO_SCENARIO_LANGUAGE_IN_HISTORICAL_RANGE_APPENDIX",
    "SUNDHEIM_DECISION_COMPLETE",
    "HISTORICAL_WINDOW_LABEL_MATCHES_ACTUAL",
    "INVESTMENT_ATTRACTIVENESS_HAS_UNDERWRITING",
    "DATA_CONFIDENCE_DECOMPOSED", "NEXT_CHECKPOINT_TYPE_VALID",
    # v5.7 §1/§2/§4/§5/§6
    "TTM_HAS_FOUR_CONTIGUOUS_QUARTERS", "TTM_END_DATE_IS_CURRENT",
    "TTM_CONCEPT_IS_CONSISTENT", "BALANCE_SHEET_FACTS_SHARE_PERIOD",
    "FRESHNESS_MATCHES_OLDEST_MATERIAL_REF",
    "LATEST_LABEL_REQUIRES_CURRENT_FACT",
    "MATERIAL_EVIDENCE_PERIODS_COMPATIBLE",
    "STALE_EVIDENCE_CANNOT_SUPPORT_CONCLUSION",
    "EVIDENCE_BELONGS_TO_ISSUER",
    "CLAIM_IS_COMPATIBLE_WITH_SECTOR",
    "VALUATION_METHOD_IS_COMPATIBLE_WITH_SECTOR",
    "BUSINESS_QUALITY_USES_PERMITTED_METRICS",
    "ADAPTER_GOVERNS_ARGUMENT_BUILDER", "ADAPTER_GOVERNS_VALUATION",
    "NO_SCENARIO_LANGUAGE_IN_VALIDATION_JSON",
    "APPENDIX_TABLE_CELL_NOT_CLIPPED",
    "TABLE_HEADER_REPEATED_AFTER_BREAK", "NO_STRANDED_SECTION_TAIL",
    "APPENDIX_PAGE_OCCUPANCY", "NO_LOW_DENSITY_FINAL_PAGE",
    "PROVENANCE_VALID",
)


def _page_texts(data):
    from pypdf import PdfReader
    return [p.extract_text() or ""
            for p in PdfReader(io.BytesIO(data)).pages]


def validate(view5, core_pdf, rendered, apx_pdf=None, ledger=None,
             report_id=None, snap=None):
    """The v5.6 semantic suite: rendered-content checks over the core
    AND appendix PDFs, evidence-ledger resolution, framework/adapter
    presence, and the appendix binding checks."""
    import report_v4_validate as VV
    checks = []
    arch = view5["archetype"]

    viol = A.check_rendered_sections(rendered, arch["contract"])
    checks.append(VV.chk("ARCHETYPE_TEMPLATE_MATCH", not viol, VV.ERROR,
                         "rendered sections honour the %s contract"
                         % arch["archetype"],
                         "; ".join(viol) or "all honoured"))
    checks.append(VV.chk("ROUTER_RECORDED", bool(arch.get("reasons")),
                         VV.ERROR, "router recorded its reasons",
                         "%d reason(s)" % len(arch.get("reasons") or [])))
    if arch.get("override"):
        checks.append(VV.chk("ROUTER_OVERRIDE", False, VV.WARN_S,
                             "no manual archetype override",
                             "%(from)s -> %(to)s" % arch["override"],
                             warn_only=True))

    text = _pdf_text(core_pdf)
    sc = view5.get("scenarios") or {}
    if sc.get("available") and rendered.get("valuation_table"):
        norm = re.sub(r"\s+", " ", text)
        ok_all, seen = True, []
        for r in sc["rows"]:
            want = "$%.2f" % r["price"]
            good = want in norm and abs(
                r["multiple"]["value"] * r["metric"]["value"]
                - r["price"]) < 0.01
            ok_all = ok_all and good
            seen.append("%s=%s%s" % (r["leg"], want,
                                     "" if good else "(MISSING)"))
        # the check id follows the mode (§2): no "scenario" vocabulary
        # attaches to a historical range, validation names included
        _arith_id = ("SCENARIO_ARITHMETIC"
                     if sc.get("mode") == "underwritten"
                     else "HISTORICAL_RANGE_ARITHMETIC")
        checks.append(VV.chk(_arith_id, ok_all, VV.ERROR,
                             "every rendered price recomputes from its "
                             "multiple x metric",
                             ", ".join(seen)))
        asm_rows = [r for r in sc["rows"]
                    if r["multiple"]["grade"] == "ASM"]
        if asm_rows or sc.get("weighted"):
            checks.append(VV.chk("ASM_LABELLED", "[ASM]" in text,
                                 VV.ERROR,
                                 "assumption-graded figures carry the "
                                 "ASM tag in the rendered text",
                                 "tag %s" % ("present" if "[ASM]" in text
                                             else "MISSING")))
        if not sc.get("weighted"):
            checks.append(VV.chk(
                "NO_UNSOURCED_PROBABILITY",
                "Probability-weighted" not in text, VV.ERROR,
                "no probability weights without a user assumptions file",
                "clean" if "Probability-weighted" not in text
                else "weighted value rendered without a source"))

    # ── dual assessment + EV checks (v5.5 phases D/E) ────────────────
    asx = view5.get("assessment") or {}
    bq, ia = asx.get("business_quality") or {},         asx.get("investment_attractiveness") or {}
    checks.append(VV.chk("BUSINESS_AND_STOCK_QUALITY_SEPARATE",
                         bool(bq.get("level")) and bool(ia.get("level"))
                         and bool(asx.get("tension")), VV.ERROR,
                         "two categorical assessments plus the tension "
                         "line; never a composite",
                         "%s / %s" % (bq.get("level"), ia.get("level"))))
    w = (view5.get("scenarios") or {}).get("weighted")
    if w:
        probs = w.get("probabilities") or {}
        ssum = sum(probs.values())
        checks.append(VV.chk("SCENARIO_PROBABILITIES_SUM_TO_100",
                             abs(ssum - 1.0) < 0.011, VV.ERROR,
                             "scenario probabilities sum to 100%",
                             "%.3f" % ssum))
        rows = {r["leg"]: r for r in view5["scenarios"]["rows"]}
        ev = sum(rows[l]["price"] * probs[l] for l in probs)
        checks.append(VV.chk("SCENARIO_EXPECTED_VALUE_ARITHMETIC",
                             abs(ev - w["price"]) < 0.02, VV.ERROR,
                             "expected value recomputes from "
                             "probability x price",
                             "%.2f vs %.2f" % (ev, w["price"])))
    ia_lvl = ia.get("level")
    checks.append(VV.chk("NO_PROBABILITY_WHEN_NOT_UNDERWRITTEN",
                         not (w and ia_lvl == "NOT_UNDERWRITTEN"),
                         VV.ERROR,
                         "no probability weighting on a NOT_UNDERWRITTEN "
                         "name", "weighted=%s, ia=%s"
                         % (bool(w), ia_lvl)))

    # ── historical-range semantics + IA underwriting (v5.6) ──────────
    import report_v5_checks as CK0
    sc6 = view5.get("scenarios") or {}
    if sc6.get("available") and sc6.get("mode") == "historical_range":
        banned = CK0.scenario_language_issues(text, sc6.get("mode"))
        checks.append(VV.chk("NO_SCENARIO_LANGUAGE_IN_HISTORICAL_RANGE",
                             not banned, VV.ERROR,
                             "historical mode renders no scenario/"
                             "forecast vocabulary",
                             ", ".join(banned) or "clean"))
        ay = (sc6.get("band_ref") or {}).get("actual_years")
        bad = CK0.window_label_issues(text, ay)
        checks.append(VV.chk("HISTORICAL_WINDOW_LABEL_MATCHES_ACTUAL",
                             not bad, VV.ERROR,
                             "the rendered window label equals "
                             "actual_years",
                             "; ".join(bad) or "labels match %.1f" % ay
                             if ay else "no label rendered"))
    ia6 = (view5.get("assessment") or {}).get(
        "investment_attractiveness") or {}
    bad = CK0.ia_underwriting_issues(ia6.get("level"), sc6.get("mode"))
    checks.append(VV.chk("INVESTMENT_ATTRACTIVENESS_HAS_UNDERWRITING",
                         not bad, VV.ERROR,
                         "graded attractiveness requires an "
                         "underwritten forward view; otherwise "
                         "PROVISIONAL/NOT_UNDERWRITTEN",
                         "; ".join(bad) or "%s (mode %s)"
                         % (ia6.get("level"), sc6.get("mode"))))
    conf6 = None
    try:
        import report_v5 as _R56
        conf6 = _R56.confidence(view5)
    except Exception:
        pass
    bad = CK0.confidence_axes_issues(conf6)
    checks.append(VV.chk("DATA_CONFIDENCE_DECOMPOSED",
                         bool(conf6) and not bad, VV.ERROR,
                         "confidence splits into five named axes",
                         "; ".join(bad) or
                         ", ".join((conf6 or {}).get("axes") or {})))

    # ── expectations checks (v5.5 phase C) ───────────────────────────
    exp = view5.get("expectations") or {}
    var = exp.get("variant") or {}
    if var.get("available"):
        checks.append(VV.chk("VARIANT_HAS_EXPECTATIONS_GAP",
                             var.get("gap_pct") is not None
                             and bool(var.get("source")), VV.ERROR,
                             "a rendered variant carries a sourced gap",
                             "gap %s%% vs %s" % (var.get("gap_pct"),
                                                 var.get("source"))))
    else:
        checks.append(VV.chk("VARIANT_HAS_EXPECTATIONS_GAP", True,
                             VV.ERROR,
                             "no variant claimed without sourced "
                             "expectations",
                             var.get("reason") or "unavailable"))
    bad = [k["metric"] for k in exp.get("kpis") or []
           if k.get("consensus") is not None
           and not (k.get("consensus_source") and k.get("consensus_as_of"))]
    checks.append(VV.chk("EXPECTATIONS_CANONICAL_AND_SOURCED", not bad,
                         VV.ERROR,
                         "every consensus figure carries source + as_of",
                         ", ".join(bad) or "all sourced"))

    # ── claim-contract checks (v5.5 phase B) ─────────────────────────
    cl = view5.get("claims") or {}
    pubs = cl.get("claims") or []
    if pubs:
        bad = [c["claim_id"] for c in pubs
               if len(c.get("evidence_refs") or []) < 2
               or not c.get("mechanism")
               or not (c.get("financial_implication")
                       or c.get("valuation_implication"))]
        checks.append(VV.chk("CLAIM_EVIDENCE_COMPLETE", not bad, VV.ERROR,
                             "every published claim has >=2 refs, a "
                             "mechanism and an implication",
                             ", ".join(bad) or "all complete"))
        bad = [c["claim_id"] for c in pubs
               if c.get("counterevidence_refs") is None]
        checks.append(VV.chk("CLAIM_HAS_COUNTEREVIDENCE", not bad,
                             VV.ERROR,
                             "counterevidence found or declared absent "
                             "per claim", ", ".join(bad) or "all"))
        bad = [c["claim_id"] for c in pubs if not c.get("breaks_if")]
        checks.append(VV.chk("CLAIM_HAS_INVALIDATION", not bad, VV.ERROR,
                             "every published claim carries breaks_if",
                             ", ".join(bad) or "all"))
        bad = [c["claim_id"] for c in pubs
               if c.get("market_expectation")
               and not c.get("market_expectation_source")]
        checks.append(VV.chk("VARIANT_EXPECTATION_SOURCED", not bad,
                             VV.ERROR,
                             "expectation language only with a sourced "
                             "market expectation",
                             ", ".join(bad) or "all sourced"))
        bad = [c["claim_id"] for c in pubs
               if not c.get("reunderwrite_when")
               or not c.get("next_checkpoint")]
        from datetime import datetime as _dt2, timezone as _tz2
        _today = _dt2.now(_tz2.utc).date().isoformat()
        bad_t = CK0.checkpoint_type_issues(pubs)
        checks.append(VV.chk("NEXT_CHECKPOINT_TYPE_VALID", not bad_t,
                             VV.ERROR,
                             "checkpoints are typed objects (dated "
                             "types carry a date)",
                             ", ".join(bad_t) or "all typed"))
        stale_cp = [c["claim_id"] for c in pubs
                    if isinstance(c.get("next_checkpoint"), dict)
                    and c["next_checkpoint"].get("date")
                    and str(c["next_checkpoint"]["date"]) <= _today]
        undated = sum(1 for c in pubs
                      if isinstance(c.get("next_checkpoint"), dict)
                      and c["next_checkpoint"].get("type")
                      == "unscheduled_event")
        checks.append(VV.chk("NEXT_CHECKPOINT_AFTER_AS_OF",
                             not stale_cp, VV.ERROR,
                             "every DATED checkpoint lies after the "
                             "report date (unscheduled: not applicable)",
                             (", ".join(stale_cp) or
                              "%d dated future, %d unscheduled (n/a)"
                              % (len(pubs) - undated, undated))))
        checks.append(VV.chk("THESIS_REUNDERWRITE_TRIGGER_PRESENT",
                             not bad, VV.ERROR,
                             "every claim carries re-underwriting "
                             "triggers and a checkpoint",
                             ", ".join(bad) or "all"))
    else:
        checks.append(VV.chk("CLAIM_GATE_EXPLAINED",
                             bool(cl.get("note"))
                             or not (cl.get("rejected")), VV.ERROR,
                             "zero published claims come with the gate "
                             "explanation",
                             "note %s, %d rejected"
                             % ("present" if cl.get("note") else "absent",
                                len(cl.get("rejected") or []))))

    # ── v5.6 semantic suite: rendered artifacts + ledger + binding ───
    import hashlib as _hl

    import report_v5_checks as CK
    import report_v5_ledger as LG
    pages = _page_texts(core_pdf)
    is_flash = bool((view5["v4"] or {}).get("flash"))
    cl6 = view5.get("claims") or {}
    pubs6 = cl6.get("claims") or []

    # presentation (§13)
    bad = CK.orphan_pages(pages)
    checks.append(VV.chk("NO_ORPHAN_PAGE", not bad, VV.ERROR,
                         "no text-starved orphan page",
                         "; ".join(bad) or "clean"))
    bad = CK.orphan_bullets(pages)
    checks.append(VV.chk("NO_ORPHAN_BULLET", not bad, VV.ERROR,
                         "no page reduced to a single stranded bullet",
                         "; ".join(bad) or "clean"))
    bad = CK.raw_identifiers(text)
    checks.append(VV.chk("HUMAN_READABLE_METRIC_LABELS", not bad,
                         VV.ERROR,
                         "no raw schema identifiers in reader-facing "
                         "core text", ", ".join(bad[:6]) or "clean"))
    over = CK.overflow_pages(pages)
    checks.append(VV.chk("CORE_LAYOUT_NO_OVERFLOW", not over, VV.ERROR,
                         "no page carries more text than its frame "
                         "holds legibly", "; ".join(over) or "clean"))

    # evidence-ledger resolution (§8)
    if ledger:
        bad = []
        for c in pubs6:
            bad += ["%s: %s" % (c["claim_id"], r) for r in
                    LG.unresolved(c.get("evidence_refs"), ledger)]
        checks.append(VV.chk("EVIDENCE_REF_EXISTS_IN_LEDGER", not bad,
                             VV.ERROR,
                             "every claim evidence ref resolves to a "
                             "registered ledger ID",
                             "; ".join(bad[:4]) or
                             "%d refs resolve" % sum(
                                 len(c.get("evidence_refs") or [])
                                 for c in pubs6)))
        bad = []
        for c in pubs6:
            bad += ["%s: %s" % (c["claim_id"], r) for r in
                    LG.unresolved(c.get("counterevidence_refs"),
                                  ledger)]
        checks.append(VV.chk("COUNTEREVIDENCE_REF_EXISTS_IN_LEDGER",
                             not bad, VV.ERROR,
                             "every counterevidence ref resolves to a "
                             "registered ledger ID",
                             "; ".join(bad[:4]) or "all resolve"))
    bad = []
    for c in pubs6:
        bad += ["%s: %s" % (c["claim_id"], r)
                for r in LG.irrelevant_counters(c)]
    checks.append(VV.chk("COUNTEREVIDENCE_RELEVANT_TO_CLAIM", not bad,
                         VV.ERROR,
                         "technical/price refs never counter a "
                         "fundamental or valuation claim",
                         "; ".join(bad[:4]) or "same-proposition rule "
                         "holds"))

    # ── §1 point-in-time financial integrity (v5.7) ──────────────────
    m7 = view5.get("multiples") or {}
    integ7 = m7.get("ttm_integrity") or {}
    fu7 = (snap or {}).get("fundamentals") or {}
    bq7 = (view5.get("assessment") or {}).get("business_quality") or {}
    _avail_streams = [k for k in ("pe", "ps")
                      if (m7.get(k) or {}).get("available")]
    for cid, field, expect in (
            ("TTM_HAS_FOUR_CONTIGUOUS_QUARTERS", "contiguous",
             "every published TTM uses four contiguous fiscal "
             "quarters"),
            ("TTM_END_DATE_IS_CURRENT", "current",
             "every published TTM ends within the normal reporting "
             "lag")):
        bad = ["%s: %s" % (k, "; ".join((integ7.get(k) or {}).get(
            "reasons") or ["no integrity record"]))
            for k in _avail_streams
            if not (integ7.get(k) or {}).get(field)]
        checks.append(VV.chk(cid, not bad, VV.ERROR, expect,
                             "; ".join(bad) or
                             ("streams %s ok" % _avail_streams
                              if _avail_streams else
                              "no stream published")))
    bad = [k for k in _avail_streams
           if not (integ7.get(k) or {}).get("concept")]
    checks.append(VV.chk("TTM_CONCEPT_IS_CONSISTENT", not bad,
                         VV.ERROR,
                         "each TTM stream uses one named accounting "
                         "concept",
                         ", ".join(bad) or ", ".join(
                             str((integ7.get(k) or {}).get("concept"))
                             for k in _avail_streams) or "n/a"))
    bad = CK.balance_sheet_period_issues(fu7, bq7)
    checks.append(VV.chk("BALANCE_SHEET_FACTS_SHARE_PERIOD", not bad,
                         VV.ERROR,
                         "balance-sheet instants are only netted from "
                         "the same reporting date",
                         "; ".join(bad) or "periods compatible"))
    bad = CK.freshness_basis_issues(bq7, fu7)
    checks.append(VV.chk("FRESHNESS_MATCHES_OLDEST_MATERIAL_REF",
                         not bad, VV.ERROR,
                         "conclusion freshness equals the oldest "
                         "material supporting period",
                         "; ".join(bad) or
                         str(bq7.get("freshness_basis"))))
    bad = CK.latest_label_issues(text, fu7)
    checks.append(VV.chk("LATEST_LABEL_REQUIRES_CURRENT_FACT", not bad,
                         VV.ERROR,
                         "'latest' language only with a current fact",
                         "; ".join(bad) or "clean"))
    bad = CK.claim_period_issues(pubs6)
    checks.append(VV.chk("MATERIAL_EVIDENCE_PERIODS_COMPATIBLE",
                         not bad, VV.ERROR,
                         "one claim's XBRL facts share a reporting "
                         "period", "; ".join(bad) or "compatible"))
    bad = CK.stale_support_issues(pubs6)
    checks.append(VV.chk("STALE_EVIDENCE_CANNOT_SUPPORT_CONCLUSION",
                         not bad, VV.ERROR,
                         "no published claim rests on stale critical "
                         "evidence", ", ".join(bad) or "none stale"))
    if ledger:
        _cik = (snap or {}).get("cik") or ledger.get("issuer_cik")
        bad = CK.issuer_issues(ledger, _cik)
        checks.append(VV.chk("EVIDENCE_BELONGS_TO_ISSUER", not bad,
                             VV.ERROR,
                             "every evidence record is bound to the "
                             "intended issuer",
                             "; ".join(bad) or
                             "issuer CIK %s" % ledger.get("issuer_cik")))

    # ── §2 sector-aware claims and valuation (v5.7) ──────────────────
    ad7 = view5.get("adapter") or {}
    sc7 = view5.get("scenarios") or {}
    bad = CK.claim_sector_issues(cl6, ad7)
    checks.append(VV.chk("CLAIM_IS_COMPATIBLE_WITH_SECTOR", not bad,
                         VV.ERROR,
                         "no published claim shape the sector's "
                         "economics cannot support",
                         ", ".join(bad) or "compatible"))
    bad = CK.valuation_sector_issues(sc7, ad7)
    checks.append(VV.chk("VALUATION_METHOD_IS_COMPATIBLE_WITH_SECTOR",
                         not bad, VV.ERROR,
                         "the valuation method is economically "
                         "applicable to the sector",
                         "; ".join(bad) or
                         "method %s" % sc7.get("metric_kind")))
    bad = CK.quality_metric_issues(bq7, ad7)
    checks.append(VV.chk("BUSINESS_QUALITY_USES_PERMITTED_METRICS",
                         not bad, VV.ERROR,
                         "quality grading uses only sector-permitted "
                         "metrics",
                         ", ".join(bad) or
                         "used: %s" % ", ".join(bq7.get("metrics_used")
                                                or []) or "none"))
    gov = CK.adapter_governance_issues(cl6, sc7, ad7)
    checks.append(VV.chk("ADAPTER_GOVERNS_ARGUMENT_BUILDER",
                         not any("argument" in g for g in gov),
                         VV.ERROR,
                         "the argument builder ran under the adapter "
                         "policy", "; ".join(g for g in gov
                                             if "argument" in g)
                         or "governed (%s)" % ad7.get("key")))
    checks.append(VV.chk("ADAPTER_GOVERNS_VALUATION",
                         not any("valuation" in g for g in gov),
                         VV.ERROR,
                         "valuation selection ran under the adapter "
                         "policy", "; ".join(g for g in gov
                                             if "valuation" in g)
                         or "governed (%s)" % ad7.get("key")))
    if not is_flash and arch["archetype"] in ("FULL", "FULL_THIN",
                                              "THIN"):
        _needs = any(s.get("kind") == "absent" for s in
                     __import__("report_v5_adapters").ADAPTERS.get(
                         ad7.get("key") or "generic",
                         {}).get("slots") or [])
        ok7 = (not _needs) or ("no admitted source" in text)
        checks.append(VV.chk("REQUIRED_SECTOR_METRIC_OR_NOT_ASSESSED",
                             ok7, VV.ERROR,
                             "required sector metrics render their "
                             "values or a visible no-admitted-source "
                             "state",
                             "absent slots disclosed" if ok7 else
                             "sector metric slots missing from the "
                             "rendered core"))

    # framework + adapter (§4/§6/§7)
    fw = view5.get("framework") or {}
    fw_bad = CK.framework_issues(fw)
    checks.append(VV.chk("FRAMEWORK_COVERAGE_PRESENT", not fw_bad,
                         VV.ERROR,
                         "all 26 Tiger dimensions present with valid "
                         "statuses",
                         ", ".join(fw_bad[:5]) or "26/26"))
    if arch["archetype"] == "FULL":
        miss = CK.full_coverage_issues(arch["archetype"], fw)
        checks.append(VV.chk("FULL_REQUIRES_DILIGENCE_COVERAGE",
                             not miss, VV.ERROR,
                             "FULL only with framework coverage on the "
                             "decision dimensions",
                             ", ".join(miss) or "coverage sufficient"))
    ad6 = view5.get("adapter") or {}
    if not is_flash:
        bad = CK.adapter_issues(ad6, text, arch["archetype"])
        checks.append(VV.chk("SECTOR_ADAPTER_APPLIED", not bad,
                             VV.ERROR,
                             "a sector adapter was selected and its "
                             "dashboard rendered",
                             ("; ".join(bad) or "%s (%s)"
                              % (ad6.get("key"),
                                 ad6.get("reason", "")))[:90]))

    # variant wording (§9)
    exp6 = view5.get("expectations") or {}
    var_ok = bool((exp6.get("variant") or {}).get("available"))
    bad = CK.variant_wording_issues(text, var_ok)
    checks.append(VV.chk("VARIANT_WORDING_REQUIRES_EXPECTATIONS_GAP",
                         not bad, VV.ERROR,
                         "variant wording only with a sourced "
                         "expectations gap",
                         "; ".join(bad) or "clean"))

    # event state (§10)
    ev6 = dict((view5["v4"] or {}).get("event") or {})
    bad = CK.event_state_issues(ev6)
    checks.append(VV.chk("EVENT_STATE_RESPECTS_UPCOMING_EVENT", not bad,
                         VV.ERROR,
                         "no post-call state inside the pre-event "
                         "window", "; ".join(bad) or
                         "state %s" % ev6.get("state")))

    # historical range never a return (§2/§3)
    bad = CK.historical_expected_return_issues(
        text, sc6.get("mode"), sc6.get("weighted"))
    checks.append(VV.chk("HISTORICAL_RANGE_NOT_USED_AS_EXPECTED_RETURN",
                         not bad, VV.ERROR,
                         "the historical range is never presented as an "
                         "expected return", "; ".join(bad) or "clean"))

    # invalidation separation + stage ordering (§12)
    if pubs6 and arch["archetype"] in ("FULL", "FULL_THIN", "THIN"):
        bad = CK.invalidation_separation_issues(text)
        checks.append(VV.chk(
            "FUNDAMENTAL_AND_TACTICAL_INVALIDATION_SEPARATE", not bad,
            VV.ERROR, "fundamental and tactical invalidation lines "
            "both render", "; ".join(bad) or "both present"))
    stages6 = ((view5["v4"] or {}).get("monitoring")
               or {}).get("recovery_stages") or []
    if stages6:
        bad = CK.stage_order_issues(stages6)
        checks.append(VV.chk("TECHNICAL_STAGES_ORDERED", not bad,
                             VV.ERROR,
                             "monitoring stages progress monotonically "
                             "by threshold",
                             "; ".join(bad) or
                             "%d stages ordered" % len(stages6)))

    # universal-ticker scan as a validation check (§18)
    try:
        import test_v5_no_ticker_branches as TB
        tb_bad = TB.scan()
    except Exception as e:
        tb_bad = ["scan failed: %s" % e]
    checks.append(VV.chk("NO_TICKER_SPECIFIC_BRANCH", not tb_bad,
                         VV.ERROR,
                         "no pilot symbol or company name in shared "
                         "production logic",
                         "; ".join(tb_bad[:3]) or
                         "%d shared modules clean" % len(
                             TB.SHARED_MODULES)))

    # Sundheim decision object must be complete AND serialized (§5)
    sd6 = view5.get("sundheim") or {}
    bad = CK.sundheim_issues(sd6)
    checks.append(VV.chk("SUNDHEIM_DECISION_COMPLETE", not bad,
                         VV.ERROR,
                         "the twelve-question Sundheim decision object "
                         "is complete and serialized",
                         "; ".join(bad) or
                         "%d questions + stored fields"
                         % len(sd6.get("questions") or [])))

    # appendix binding (§1)
    if apx_pdf:
        apx_text = "\n".join(_page_texts(apx_pdf))
        core_hash = _hl.sha256(core_pdf).hexdigest()
        # the scenario-vocabulary ban covers the appendix surface too
        if sc6.get("available") and sc6.get("mode") == "historical_range":
            banned_apx = CK.scenario_language_issues(apx_text,
                                                     sc6.get("mode"))
            checks.append(VV.chk(
                "NO_SCENARIO_LANGUAGE_IN_HISTORICAL_RANGE_APPENDIX",
                not banned_apx, VV.ERROR,
                "the appendix carries no scenario vocabulary in "
                "historical mode", ", ".join(banned_apx) or "clean"))
        bad = CK.appendix_version_issues(apx_text)
        checks.append(VV.chk("APPENDIX_VERSION_MATCH", not bad,
                             VV.ERROR,
                             "appendix headed 'Equity Research v5 - "
                             "Appendix' with no v4 metadata",
                             "; ".join(bad) or "v5 throughout"))
        bad = CK.appendix_report_id_issues(apx_text, report_id)
        checks.append(VV.chk("APPENDIX_REPORT_ID_MATCH", not bad,
                             VV.ERROR,
                             "appendix carries this run's report ID",
                             "; ".join(bad) or report_id or ""))
        band6 = next(((view5.get("multiples") or {}).get(k) or {}
                      for k in ("pe", "ps")
                      if ((view5.get("multiples") or {}).get(k)
                          or {}).get("available")), None)
        bad = CK.appendix_method_issues(apx_text, bool(band6),
                                        (band6 or {}).get("kind"))
        checks.append(VV.chk("APPENDIX_METHOD_MATCHES_CORE", not bad,
                             VV.ERROR,
                             "appendix methodology matches what the "
                             "core actually rendered",
                             "; ".join(bad) or "methods agree"))
        bad = CK.appendix_hash_issues(apx_text, core_hash)
        checks.append(VV.chk("APPENDIX_HASH_MATCH", not bad, VV.ERROR,
                             "appendix records the core PDF's sha256",
                             "; ".join(bad) or core_hash[:16]))
        if ledger:
            bad = CK.ledger_hash_issues(apx_text, ledger.get("hash"))
            checks.append(VV.chk("SOURCE_LEDGER_HASH_MATCH", not bad,
                                 VV.ERROR,
                                 "appendix records the source-ledger "
                                 "hash",
                                 "; ".join(bad)
                                 or (ledger.get("hash") or "")[:16]))

    # v4's PDF checks minus its fixed 5/6-page rule: v5 page counts are
    # the archetype's to define, so the range comes from the contract.
    v4_checks = [c for c in VV.check_pdfs(core_pdf, apx_pdf, view5["v4"])
                 if c["check_id"] != "CORE_PAGE_COUNT"]
    checks += v4_checks
    import io as _io
    from pypdf import PdfReader
    n_pages = len(PdfReader(_io.BytesIO(core_pdf)).pages)
    lo, hi = arch["contract"].get("pages", (1, 10))
    checks.append(VV.chk("ARCHETYPE_PAGE_COUNT", lo <= n_pages <= hi,
                         VV.ERROR,
                         "%s core is %d-%d pages" % (arch["archetype"],
                                                     lo, hi),
                         "%d" % n_pages))
    fatal = [c for c in checks if c["status"] == "FAIL"]
    # §6 release provenance: the exact clean source state. Generated
    # artifacts (research-state appends, mutation catalogues, output
    # dirs) are excluded from the dirty computation — "clean" means the
    # SOURCE files match the recorded commit.
    commit_sha = tree_sha = None
    dirty = None
    try:
        import subprocess
        _cwd = os.path.dirname(os.path.abspath(__file__))

        def _git(*args):
            return subprocess.run(["git"] + list(args),
                                  capture_output=True, text=True,
                                  timeout=10, cwd=_cwd).stdout.strip()
        commit_sha = _git("rev-parse", "HEAD") or None
        tree_sha = _git("rev-parse", "HEAD^{tree}") or None
        porcelain = _git("status", "--porcelain")
        _GENERATED = ("data/research_state/", "data/mutation_proofs/",
                      "out_", ".snapcache", "docs/data/")
        dirty = any(ln and not any(g in ln for g in _GENERATED)
                    for ln in porcelain.splitlines())
    except Exception:
        pass
    return {"schema": "equity-research-v5-validation/1",
            "generator_version": "v5.7",
            "commit_sha": commit_sha,
            "source_commit_sha": commit_sha,
            "git_tree_sha": tree_sha,
            "dirty_worktree": dirty,
            "generated_at": datetime.now(timezone.utc
                                         ).isoformat(timespec="seconds"),
            "ticker": (view5["v4"] or {}).get("ticker"),
            "archetype": arch["archetype"],
            "router": {"reasons": arch["reasons"],
                       "override": arch.get("override"),
                       "categories_present": arch.get("categories_present"),
                       "categories_absent": arch.get("categories_absent")},
            "checks": checks,
            "blocking_failures": [c["check_id"] for c in fatal],
            "ok": not fatal}


def run(ticker, out_dir="out_v5", override=None):
    os.makedirs(out_dir, exist_ok=True)
    ticker = ticker.upper().strip()
    snap, prov, view5 = build_view(ticker, override)
    report_id = "%s-%s" % (ticker, datetime.now(timezone.utc
                                                ).isoformat(
                                                    timespec="seconds"))
    view5["report_id"] = report_id

    chart_png = chart_meta = None
    if view5["archetype"]["archetype"] in (A.FULL, A.FULL_THIN,
                                           A.NEW_LISTING):
        try:
            import report_v4_run as RR
            chart_png, chart_meta = RR._chart(ticker, snap, prov,
                                              view5["v4"])
        except Exception as e:
            print("  chart: %s" % e)

    # pre-render changeset (analysis-object diff only; the persisted
    # state with artifact hashes is written after validation)
    import report_v5_memory as MEM0
    _prior, _prior_reason = MEM0.load_prior(ticker)
    _pre_state = MEM0.build_state(ticker, view5, {"artifacts": {}},
                                  prior_id=(_prior or {}).get(
                                      "report_id"))
    _cs = MEM0.changeset(_prior, _pre_state)
    if _prior:
        from datetime import datetime as _dtc
        try:
            _age_h = (_dtc.fromisoformat(_pre_state["as_of"])
                      - _dtc.fromisoformat(_prior["as_of"])
                      ).total_seconds() / 3600.0
        except Exception:
            _age_h = 999
        if _age_h < 6:
            _cs["same_session"] = True
            _cs["prior_as_of"] = _prior["as_of"]
    view5["changeset"] = _cs

    core_p = os.path.join(out_dir, "%s_equity_research_v5.pdf" % ticker)
    apx_p = os.path.join(out_dir,
                         "%s_equity_research_v5_appendix.pdf" % ticker)
    val_p = os.path.join(out_dir,
                         "%s_equity_research_v5_validation.json" % ticker)

    # evidence ledger (§8/§3) — built before the render so the appendix
    # and the validation JSON bind to the same evidence universe, and
    # bound to the issuer's CIK (§1)
    import report_v5_ledger as LG
    try:
        _cik = RL.cik_for(ticker)
    except Exception:
        _cik = None
    snap["cik"] = snap.get("cik") or _cik
    ledger = LG.build(snap, view5, report_id, issuer_cik=_cik)
    view5["ledger_hash"] = ledger["hash"]

    data, rendered = R5.build_core(snap, view5, core_p, chart_png,
                                   chart_meta)
    # v5 appendix (§1): generated from the SAME canonical view/state as
    # the core, hash-bound to it. The v4 appendix no longer ships on v5
    # packages.
    import hashlib as _hl

    import report_v5_appendix as APX
    core_hash = _hl.sha256(data).hexdigest()
    apx = APX.build(snap, view5, prov, core_hash, ledger, report_id,
                    apx_p)
    result = validate(view5, data, rendered, apx, ledger=ledger,
                      report_id=report_id, snap=snap)

    # ── §5 rendered-layout checks on the written appendix ────────────
    import report_v4_validate as VV5
    import report_v5_checks as CK5
    apx_pages = _page_texts(apx)
    sd5 = view5.get("sundheim") or {}
    apx_full = "\n".join(apx_pages)
    bad = CK5.sundheim_render_issues(apx_full, sd5)
    result["checks"].append(VV5.chk(
        "APPENDIX_TABLE_CELL_NOT_CLIPPED", not bad, VV5.ERROR,
        "every Sundheim answer renders in full (wrapped, never "
        "clipped)", "; ".join(bad) or
        "%d answers verified" % len(sd5.get("questions") or [])))
    bad = CK5.sundheim_header_issues(apx_pages, sd5)
    result["checks"].append(VV5.chk(
        "TABLE_HEADER_REPEATED_AFTER_BREAK", not bad, VV5.ERROR,
        "a split table repeats its header on the continuation page",
        "; ".join(bad) or "headers repeat"))
    bad = CK5.stranded_tail_issues(apx_pages)
    result["checks"].append(VV5.chk(
        "NO_STRANDED_SECTION_TAIL", not bad, VV5.ERROR,
        "no appendix page opens mid-sentence",
        "; ".join(bad) or "clean"))
    occ = CK5.measure_occupancy(apx_p)
    bad = CK5.page_occupancy_issues(occ)
    result["checks"].append(VV5.chk(
        "APPENDIX_PAGE_OCCUPANCY",
        not [b for b in bad if "final" not in b], VV5.ERROR,
        "interior appendix pages carry real content",
        "; ".join(b for b in bad if "final" not in b) or
        "occupancy %s" % ["%.0f%%" % (r * 100) for r in occ]))
    result["checks"].append(VV5.chk(
        "NO_LOW_DENSITY_FINAL_PAGE",
        not [b for b in bad if "final" in b], VV5.ERROR,
        "the final appendix page is not a sparse tail",
        "; ".join(b for b in bad if "final" in b) or
        "final %.0f%%" % (occ[-1] * 100 if occ else 0)))
    import report_v4_run as RR
    result["artifacts"] = RR._artifact_hashes(core_p, apx_p)
    result["report_id"] = report_id
    result["source_ledger_hash"] = ledger["hash"]
    result["ledger_ids"] = ledger["count"]
    # Independent auditability: the complete ID-to-source ledger ships
    # both embedded in the validation JSON and as its own artifact, so
    # an outside reviewer can resolve every reference without trusting
    # the validator's own PASS.
    result["evidence_ledger"] = [{"id": k, "record": v}
                                 for k, v in sorted(
                                     (ledger.get("ids") or {}).items())]
    ledger_p = os.path.join(out_dir,
                            "%s_source_ledger.json" % ticker)
    with open(ledger_p, "w", encoding="utf-8") as fh:
        json.dump({"schema": ledger["schema"], "report_id": report_id,
                   "ticker": ticker, "hash": ledger["hash"],
                   "issuer_cik": ledger.get("issuer_cik"),
                   "count": ledger["count"], "ids": ledger["ids"]},
                  fh, indent=1, sort_keys=True)
    with open(ledger_p, "rb") as fh:
        result["artifacts"][os.path.basename(ledger_p)] = {
            "sha256": _hl.sha256(fh.read()).hexdigest()}
    # Serialized decision objects (§4/§5): the complete Sundheim object
    # and the full framework matrix travel with the validation JSON.
    result["sundheim"] = view5.get("sundheim")
    result["framework"] = view5.get("framework")

    # ── research memory (v5.5 phase F) ───────────────────────────────
    import report_v5_memory as MEM
    import report_v4_validate as VV
    prior, prior_reason = MEM.load_prior(ticker)
    state = MEM.build_state(ticker, view5, result,
                            prior_id=(prior or {}).get("report_id"),
                            report_id=report_id,
                            ledger_hash=ledger["hash"])
    cs = MEM.changeset(prior, state)
    if prior_reason and not cs["initial_underwriting"]:
        cs["note"] = prior_reason
    result["changeset"] = cs
    result["research_state_id"] = state["report_id"]
    result["checks"].append(VV.chk(
        "PRIOR_REPORT_HASH_VALID",
        cs["initial_underwriting"] or bool(cs.get("prior_core_pdf_hash")),
        VV.ERROR,
        "comparisons only against a hash-verified prior (or initial "
        "underwriting)",
        prior_reason or ("prior %s verified"
                         % cs.get("prior_report_id"))))
    mat = [c for c in cs["changes"]
           if c["category"] in ("rating_change", "assessment_change",
                                "valuation_row_change", "archetype_change")]
    result["checks"].append(VV.chk(
        "CHANGESET_MATERIAL_CHANGE_EXPLAINED",
        all(c.get("reason") and c.get("evidence_refs") is not None
            for c in mat), VV.ERROR,
        "every material change carries a machine-readable reason",
        "%d material change(s)" % len(mat)))
    # ── mutation proofs (§16): every new semantic check must carry a
    # recorded, reproducible mutation that fails it. The harness
    # (test_v5_mutations.py) writes data/mutation_proofs/MUTATIONS.json;
    # each validation JSON embeds the collection so a clean PASS is
    # never presented without its negative proof.
    mut_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "data", "mutation_proofs", "MUTATIONS.json")
    mutations = []
    if os.path.exists(mut_path):
        try:
            with open(mut_path, encoding="utf-8") as fh:
                mutations = json.load(fh).get("mutation_tests") or []
        except Exception:
            mutations = []
    result["mutation_tests"] = mutations
    proven = {m.get("intended_check") for m in mutations
              if m.get("proven")}
    need_proof = [c["check_id"] for c in result["checks"]
                  if c["check_id"] in _MUTATION_REQUIRED
                  and c["check_id"] not in proven]
    result["checks"].append(VV.chk(
        "MUTATION_PROOF_PRESENT", not need_proof, VV.ERROR,
        "every new semantic check carries a recorded mutation proof",
        ("missing: " + ", ".join(need_proof[:6])) if need_proof
        else "%d checks proven by %d mutations" % (len(proven),
                                                   len(mutations))))
    # §4 (v5.7): the validation JSON is itself a surface — in
    # historical mode the serialized result may carry no scenario
    # token outside the exempt language-check ids and the mutation
    # catalogue.
    import report_v5_checks as CKJ
    _mode7 = (view5.get("scenarios") or {}).get("mode")
    bad = CKJ.json_scenario_issues(result, _mode7)
    result["checks"].append(VV.chk(
        "NO_SCENARIO_LANGUAGE_IN_VALIDATION_JSON", not bad, VV.ERROR,
        "the serialized validation record carries no scenario "
        "vocabulary in historical mode",
        "; ".join(bad) or "clean"))
    # §6: the artifact identifies its exact clean source state
    bad = CKJ.provenance_issues(result)
    result["checks"].append(VV.chk(
        "PROVENANCE_VALID", not bad, VV.ERROR,
        "generator version, source commit, tree sha, clean-worktree "
        "flag, timestamp and report ID all recorded",
        "; ".join(bad) or "%s @ %s"
        % (result.get("generator_version"),
           (result.get("source_commit_sha") or "")[:10])))
    result["blocking_failures"] = [c["check_id"]
                                   for c in result["checks"]
                                   if c["status"] == "FAIL"]
    result["ok"] = not result["blocking_failures"]
    MEM.append_state(state)
    _sc_key = ("scenarios"
               if (view5.get("scenarios") or {}).get("mode")
               == "underwritten" else "historical_valuation_range")
    result["v5_inputs"] = {
        "multiples": {k: view5["multiples"].get(k) for k in ("pe", "ps")},
        _sc_key: view5.get("scenarios"),
        "claims": view5.get("claims"),
    }
    with open(val_p, "w") as fh:
        json.dump(result, fh, indent=1, default=str, sort_keys=True)

    print("\n%s  (%s)" % (ticker, view5["archetype"]["archetype"]))
    print("  core        %s" % core_p)
    print("  appendix    %s" % apx_p)
    print("  validation  %s" % val_p)
    for c in result["checks"]:
        if c["status"] in ("FAIL", "WARN"):
            print("     %-6s %-28s %s" % (c["status"], c["check_id"],
                                          c["observed"]))
    print("  result      %s" % ("PASS" if result["ok"] else "PROBLEMS"))
    return 0 if result["ok"] else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--out", default="out_v5")
    ap.add_argument("--archetype", default=None,
                    help="override the router (recorded + WARNed)")
    a = ap.parse_args()
    return run(a.ticker, a.out, a.archetype)


if __name__ == "__main__":
    sys.exit(main())
