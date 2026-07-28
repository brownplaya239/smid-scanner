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
    asm, note = S5.load_assumptions(ticker)
    scenarios = S5.build(ticker, multiples, spot, asm, note)
    print("  [v5] claims + grid...")
    grid = G5.build(ticker)
    import report_v5_expectations as E5
    expectations = E5.build(snap, grid, multiples, scenarios, estimates,
                            asm)
    claims = C5.build(snap, v4, scenarios, estimates)
    import report_v5_assessment as AS
    import report_v5 as _R5
    bq = AS.business_quality(snap, grid)
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
    # EvidenceCapability, never ticker identity. The record carries the
    # categories present/absent and the full reason chain.
    import report_v5_capability as CAP
    profile = CAP.company_profile(snap, multiples)
    capability = CAP.evidence_capability(snap, multiples, estimates,
                                         has_options)
    arch = CAP.route(profile, capability, v4.get("event") or {},
                     multiples, has_options=has_options,
                     override=override,
                     override_author="cli" if override else None,
                     override_reason="--archetype flag" if override
                     else None)
    print("  [v5] archetype: %s (%s)" % (arch["archetype"],
                                         arch["routing_reason"][:70]))
    view5 = {"v4": v4, "archetype": arch, "multiples": multiples,
             "scenarios": scenarios, "claims": claims, "grid": grid,
             "has_options": has_options, "estimates": estimates,
             "profile": profile, "expectations": expectations,
             "assessment": assessment}
    return snap, prov, view5


def _pdf_text(data):
    from pypdf import PdfReader
    return "\n".join(p.extract_text() or ""
                     for p in PdfReader(io.BytesIO(data)).pages)


def validate(view5, core_pdf, rendered, apx_pdf=None):
    """Pilot checks; slice 7 grows this into the full suite."""
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
    if sc.get("available") and rendered.get("scenario_table"):
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
        checks.append(VV.chk("SCENARIO_ARITHMETIC", ok_all, VV.ERROR,
                             "every rendered scenario price recomputes "
                             "from its multiple x metric",
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
    return {"schema": "equity-research-v5-validation/pilot",
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

    chart_png = chart_meta = None
    if view5["archetype"]["archetype"] in (A.FULL, A.NEW_LISTING):
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
    view5["changeset"] = MEM0.changeset(_prior, _pre_state)

    core_p = os.path.join(out_dir, "%s_equity_research_v5.pdf" % ticker)
    apx_p = os.path.join(out_dir,
                         "%s_equity_research_v5_appendix.pdf" % ticker)
    val_p = os.path.join(out_dir,
                         "%s_equity_research_v5_validation.json" % ticker)
    data, rendered = R5.build_core(snap, view5, core_p, chart_png,
                                   chart_meta)
    # v4's evidence appendix is the audit trail for every OBS/DER figure
    # v5 shares with it; the v5-only artifacts (bands, scenarios, claims)
    # ship inside the validation JSON until slice 7 extends the appendix.
    import report_v4 as R4
    apx = R4.build_appendix(snap, view5["v4"], apx_p,
                            estimates=view5.get("estimates"), prov=prov)
    result = validate(view5, data, rendered, apx)
    import report_v4_run as RR
    result["artifacts"] = RR._artifact_hashes(core_p, apx_p)

    # ── research memory (v5.5 phase F) ───────────────────────────────
    import report_v5_memory as MEM
    import report_v4_validate as VV
    prior, prior_reason = MEM.load_prior(ticker)
    state = MEM.build_state(ticker, view5, result,
                            prior_id=(prior or {}).get("report_id"))
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
                                "scenario_change", "archetype_change")]
    result["checks"].append(VV.chk(
        "CHANGESET_MATERIAL_CHANGE_EXPLAINED",
        all(c.get("reason") and c.get("evidence_refs") is not None
            for c in mat), VV.ERROR,
        "every material change carries a machine-readable reason",
        "%d material change(s)" % len(mat)))
    result["blocking_failures"] = [c["check_id"]
                                   for c in result["checks"]
                                   if c["status"] == "FAIL"]
    result["ok"] = not result["blocking_failures"]
    MEM.append_state(state)
    result["v5_inputs"] = {
        "multiples": {k: view5["multiples"].get(k) for k in ("pe", "ps")},
        "scenarios": view5.get("scenarios"),
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
