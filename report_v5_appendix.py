#!/usr/bin/env python3
"""report_v5_appendix.py — the v5 appendix (v5.6 §1).

A COMPLETE replacement for the v4 appendix on v5 packages: generated
from the same canonical view/state as the core report, in the required
fourteen-section order, opening with the framework coverage the core
summarizes and closing with the artifact hashes that bind the package
together. The methodology sections describe the methods the core
ACTUALLY used — a historical multiple band that the core renders is
affirmed here with its actual window, never denied.

Binding: the appendix records the core PDF's sha256, the source-ledger
hash, the assumptions schema version, the report ID and the generation
timestamp. Validation fails the package when any of them disagree with
the core or the validation JSON.
"""

import io
from datetime import datetime, timezone

import research_snapshot as rs
from report_v3 import BODY_W, _Doc, _clean, _finalize, _table, para
from report_v4 import DERIVED, OBSERVED

APPENDIX_KIND = "Equity Research v5 - Appendix"

SECTION_TITLES = (
    "Framework coverage summary",
    "Tiger diligence matrix",
    "Industry structure and competitive power",
    "Business model and unit economics",
    "Management, incentives, culture, and capital allocation",
    "Accounting and earnings-quality review",
    "Financial and operating history",
    "Expectations and guidance ledger",
    "Historical valuation and scenario assumptions",
    "Claims, counterevidence, and invalidation",
    "Ownership, insiders, sentiment, and catalysts",
    "Research-state change log",
    "Source inventory and rejected evidence",
    "Validation summary and artifact hashes",
)

_QUAL_DIMS_INDUSTRY = (
    "industry_structure", "market_opportunity", "customer_power",
    "supplier_power", "competitor_power", "regulatory_power",
    "barriers_to_entry", "pricing_power",
)
_QUAL_DIMS_MGMT = (
    "management_record", "incentives_and_alignment",
    "culture_and_execution", "capital_allocation",
)


def _fv(x):
    return rs.fv(x) if isinstance(x, dict) else x


# §5 (v5.8) trim chain: sampled-table caps shrink one notch per
# compact level so the story can fit its final page above the 45%
# floor without starving an interior page. The full populations are
# always in the validation JSON — only the printed SAMPLE shortens.
_COMPACT = [0]


def _cap(n):
    # positive levels shorten the printed samples; NEGATIVE levels
    # lengthen them (more of the already-recorded population printed —
    # more disclosure, never invention) so a sparse final page can be
    # filled with real content when nothing can be pulled up
    return max(3, n - 3 * _COMPACT[0])


def _story(snap, view5, prov, core_hash, ledger, report_id):
    import report_v5_framework as FW
    v4 = view5.get("v4") or {}
    ticker = v4.get("ticker") or ""
    fw = view5.get("framework") or {}
    dims = fw.get("dimensions") or {}
    fsum = fw.get("summary") or {}
    when = snap.get("market_data_time") or snap.get("report_time") or ""
    gen_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sc = view5.get("scenarios") or {}
    m = view5.get("multiples") or {}
    band = next((m[k] for k in ("pe", "ps")
                 if (m.get(k) or {}).get("available")), None)
    asm_note = sc.get("assumptions_note")
    _n = [0]

    def sec(idx):
        _n[0] += 1
        return para("%d. %s" % (_n[0], SECTION_TITLES[idx - 1]), "h2")

    # §5: a section that opens with prose keeps its heading and the
    # intro paragraph together — a page break must never strand half a
    # sentence at the top of the next page.
    from reportlab.platypus import KeepTogether as _KT

    def _keep_intro(st_list):
        """Fold the last two appended flowables (heading + intro para)
        into one KeepTogether block."""
        intro = st_list.pop()
        head = st_list.pop()
        st_list.append(_KT([head, intro]))

    st = [para(APPENDIX_KIND, "h2"),
          para("Companion to the Equity Research v5 core report on %s "
               "(report ID %s). Generated from the same canonical "
               "research state as the core; market data as of %s; "
               "generated %s UTC. Core PDF sha256 %s. Source-ledger "
               "hash %s (%d registered evidence IDs). Assumptions "
               "schema v5-assumptions/1."
               % (_clean(ticker), _clean(report_id), _clean(str(when)),
                  _clean(gen_at), _clean(core_hash or "n/a"),
                  _clean((ledger or {}).get("hash") or "n/a"),
                  (ledger or {}).get("count") or 0), "small")]

    # 1. Framework coverage summary
    st.append(sec(1))
    counts = fsum.get("counts") or {}
    st.append(para("%d of %d Tiger diligence dimensions carry at least "
                   "a partial underwriting; %d have no admitted source "
                   "and are stated NOT_ASSESSED rather than inferred. "
                   "Dimensions blocking a FULL routing: %s."
                   % (fsum.get("assessed") or 0, fsum.get("total") or 26,
                      counts.get("NOT_ASSESSED", 0),
                      _clean(", ".join(d.replace("_", " ") for d in
                                       fsum.get("missing_for_full")
                                       or [])) or "none"), "body"))
    st.append(_table([[label, str(counts.get(key, 0))]
                      for key, label in
                      (("UNDERWRITTEN", "Underwritten"),
                       ("PARTIAL", "Partially assessed"),
                       ("NOT_ASSESSED", "Not assessed"),
                       ("NOT_APPLICABLE", "Not applicable"))],
                     [BODY_W * .4, BODY_W * .2],
                     header=["Status", "Dimensions"], zebra=True))

    # 2. Tiger diligence matrix (full) + the Sundheim decision record
    st.append(sec(2))
    rows = []
    for k in FW.TIGER_DIMENSIONS:
        d = dims.get(k) or {}
        rows.append([FW.DIM_LABELS.get(k, k),
                     (d.get("status") or "").replace("_", " ").title(),
                     _clean((d.get("conclusion") or "")[:110]),
                     _clean("; ".join(d.get("next_evidence_needed")
                                      or [])[:80])])
    st.append(_table(rows, [BODY_W * .18, BODY_W * .12, BODY_W * .38,
                            BODY_W * .26],
                     header=["Dimension", "Status", "Conclusion",
                             "Next evidence needed"], zebra=True))
    sd = view5.get("sundheim") or {}
    if sd.get("questions"):
        st.append(para("Sundheim decision record", "h3"))
        st.append(para("Underwriting status %s &middot; thesis type %s "
                       "&middot; principal uncertainty: %s. The "
                       "complete object is serialized in the "
                       "validation JSON."
                       % (_clean((sd.get("underwriting_status") or ""
                                  ).replace("_", " ").lower()),
                          _clean((sd.get("thesis_type") or ""
                                  ).replace("_", " ").lower()),
                          _clean(sd.get("principal_uncertainty")
                                 or "")), "small"))
        # Full answers, never truncated (§5): _table wraps cells as
        # Paragraphs, so long answers grow the row instead of clipping,
        # and the header row repeats after any page break.
        st.append(_table([[_clean(q["question"]),
                           _clean(str(q["answer"]))]
                          for q in sd["questions"]],
                         [BODY_W * .34, BODY_W * .56],
                         header=["Question", "Answer (sourced inputs "
                                 "only)"], zebra=True))

    # 3. Industry structure and competitive power
    st.append(sec(3))
    st.append(para("None of the industry-power dimensions carries an "
                   "admitted source in this run; each is recorded "
                   "NOT_ASSESSED with the evidence that would move it. "
                   "No industry conclusion in the core rests on them.",
                   "body"))
    _keep_intro(st)
    st.append(_table([[FW.DIM_LABELS[k],
                       _clean("; ".join((dims.get(k) or {}).get(
                           "next_evidence_needed") or []))]
                      for k in _QUAL_DIMS_INDUSTRY],
                     [BODY_W * .26, BODY_W * .6],
                     header=["Dimension", "What an admitted source "
                             "would look like"], zebra=True))

    # 4. Business model and unit economics
    st.append(sec(4))
    ad = view5.get("adapter") or {}
    st.append(para("Business-model classification: <b>%s</b> (%s). "
                   "Unit economics: %s."
                   % (_clean(ad.get("label") or "not classified"),
                      _clean(ad.get("reason") or "no adapter"),
                      _clean((dims.get("unit_economics") or {}).get(
                          "conclusion") or "not assessed")), "body"))
    _keep_intro(st)
    for note in ad.get("notes") or []:
        st.append(para("&bull; %s" % _clean(note), "small"))
    if ad.get("key"):
        import report_v5_adapters as ADP
        drows = ADP.build_dashboard(ad, snap)
        st.append(_table([[_clean(a), _clean(b), _clean(c)]
                          for a, b, c in drows],
                         [BODY_W * .38, BODY_W * .26, BODY_W * .26],
                         header=["Sector metric", "Value",
                                 "Provenance"], zebra=True))

    # 5. Management, incentives, culture, capital allocation
    st.append(sec(5))
    st.append(para("No admitted source grades management in this run — "
                   "the dimensions below are NOT_ASSESSED and the core "
                   "makes no management claim. Insider transactions "
                   "(section 11) are mechanical records, not a "
                   "management assessment.", "body"))
    _keep_intro(st)
    st.append(_table([[FW.DIM_LABELS[k],
                       _clean("; ".join((dims.get(k) or {}).get(
                           "next_evidence_needed") or []))]
                      for k in _QUAL_DIMS_MGMT],
                     [BODY_W * .26, BODY_W * .6],
                     header=["Dimension", "What an admitted source "
                             "would look like"], zebra=True))

    # 6. Accounting and earnings-quality review
    st.append(sec(6))
    st.append(para(_clean((dims.get("accounting_quality") or {}).get(
        "conclusion") or "not assessed"), "body"))
    _keep_intro(st)
    st.append(para("Earnings quality: %s. Cash conversion: %s."
                   % (_clean((dims.get("earnings_quality") or {}).get(
                       "conclusion") or "not assessed"),
                      _clean((dims.get("cash_conversion") or {}).get(
                          "conclusion") or "not assessed")), "small"))
    for o in ad.get("one_time_items") or []:
        st.append(para("<b>One-time item:</b> %s (evidence: %s)."
                       % (_clean(o["label"]),
                          _clean(", ".join(o.get("evidence_refs")
                                           or []))), "small", OBSERVED))

    # 7. Financial and operating history
    st.append(sec(7))
    g = view5.get("grid") or {}
    st.append(para("The core's financial dashboard renders %d filed "
                   "annual column(s) plus TTM through %s. Basis: %s."
                   % (len(g.get("years") or []),
                      _clean((g.get("ttm") or {}).get("through")
                             or "n/a"),
                      _clean(g.get("basis") or "n/a")), "body"))
    _keep_intro(st)
    for gap in g.get("gaps") or []:
        st.append(para("&bull; %s" % _clean(gap), "small"))

    # 8. Expectations and guidance ledger
    st.append(sec(8))
    exp = view5.get("expectations") or {}
    kpis = exp.get("kpis") or []
    if kpis:
        st.append(_table(
            [[_clean(k["metric"])[:30], _clean(k["period"])[:24],
              ("%s-%s" % (k["company_guidance"]["low"],
                          k["company_guidance"]["high"]))
              if k.get("company_guidance") else "none filed",
              (str(k["consensus"]) if k.get("consensus") is not None
               else _clean(str(k.get("consensus_state") or "absent"))),
              k.get("evidence_grade") or ""]
             for k in kpis[:8]],
            [BODY_W * .26, BODY_W * .2, BODY_W * .18, BODY_W * .18,
             BODY_W * .08],
            header=["KPI", "Period", "Guidance", "Consensus", "Grade"],
            zebra=True))
    else:
        st.append(para("No KPI-level expectations were sourced this "
                       "run; the core claims no variant view.", "body"))
    var = exp.get("variant") or {}
    st.append(para("Variant availability: %s."
                   % _clean(("available on %s" % var.get("metric"))
                            if var.get("available")
                            else (var.get("reason") or "unavailable")),
                   "small"))

    # 9. Historical valuation range (or scenario assumptions when a
    # user assumptions file underwrote real scenarios) — the section
    # title itself is mode-aware so no scenario vocabulary attaches to
    # a historical range (§2).
    _n[0] += 1
    st.append(para("%d. %s" % (_n[0],
                               "Historical valuation and scenario "
                               "assumptions"
                               if sc.get("mode") == "underwritten" else
                               "Historical valuation range and "
                               "assumptions status"), "h2"))
    if band:
        ay = band.get("actual_years")
        st.append(para("The core RENDERS a historical multiple band: "
                       "percentiles of this name's own daily trailing "
                       "%s over the available %s history (%s to %s), "
                       "each session computed only from filings "
                       "available before it, per-share facts rebased "
                       "across splits by filing date, as first "
                       "reported. Coverage %.0f%%."
                       % (_clean((band.get("kind") or "").upper()),
                          ("%.1f-year" % ay) if ay else "available",
                          _clean(band.get("window_start") or ""),
                          _clean(band.get("window_end") or ""),
                          100.0 * (band.get("coverage") or 0)),
                       "body", DERIVED))
        if sc.get("mode") == "underwritten":
            st.append(para("Mode: underwritten scenarios — a user "
                           "assumptions file supplied the forward "
                           "metric and probabilities (schema "
                           "v5-assumptions/1).", "small"))
        else:
            st.append(para("Mode: historical range only. The P25 / "
                           "Median / P75 prices hold the trailing "
                           "metric constant; they are descriptive "
                           "context, carry no probabilities, and are "
                           "not a forecast.", "small"))
    else:
        st.append(para("No multiple band survived its coverage floor "
                       "this run, so the core withholds the valuation "
                       "range: %s."
                       % _clean(sc.get("reason") or "insufficient "
                                "history"), "body"))
    if asm_note:
        st.append(para("Assumptions note: %s." % _clean(asm_note),
                       "small"))

    # 10. Claims, counterevidence, and invalidation
    st.append(sec(10))
    cl = view5.get("claims") or {}
    for c in cl.get("claims") or []:
        st.append(para("<b>%s</b> [%s &middot; %s &middot; %s]"
                       % (_clean(c["claim"]), c["claim_type"],
                          c["direction"], c["status"]), "body"))
        st.append(para("Evidence refs: %s &middot; counter refs: %s "
                       "&middot; breaks if: %s"
                       % (_clean(", ".join(c.get("evidence_refs")
                                           or []))[:160],
                          _clean(", ".join(c.get("counterevidence_refs")
                                           or []) or "none (declared "
                                 "absent)"),
                          _clean(c.get("breaks_if") or "")), "small"))
    for r in (cl.get("rejected") or [])[:_cap(5)]:
        st.append(para("&bull; REJECTED: %s &mdash; %s"
                       % (_clean(r["claim"][:70]),
                          _clean("; ".join(r["failed_gates"]))),
                       "small"))
    if not (cl.get("claims") or cl.get("rejected")):
        st.append(para(_clean(cl.get("note") or "No claim candidates "
                              "were generated."), "body"))

    # 11. Ownership, insiders, sentiment, and catalysts
    st.append(sec(11))
    ins = v4.get("insiders") or {}
    irows = [[_clean(c.get("label") or ""), str(c.get("n")),
              "view-bearing" if c.get("carries_view") else "mechanical"]
             for c in (ins.get("rows") or []) if c.get("n")]
    st.append(_table(irows, [BODY_W * .5, BODY_W * .14, BODY_W * .22],
                     header=["Form 4 category", "Count", "Kind"],
                     zebra=True,
                     empty="No Form 4 filings in the window."))
    own = v4.get("ownership") or {}
    orows = [[r.get("form") or "-", r.get("filer") or "not parsed",
              r.get("accepted") or "-"]
             for r in (own.get("rows") or [])[:_cap(8)]]
    st.append(_table(orows, [BODY_W * .14, BODY_W * .38, BODY_W * .3],
                     header=["Form", "Filer", "Accepted"], zebra=True,
                     empty="No 13D/13G filings on record in the "
                           "window."))
    cat = snap.get("catalyst") or {}
    st.append(para("Next expected event: %s (%s)."
                   % (_clean(str(cat.get("next_event_date")
                                 or cat.get("event_dt")
                                 or "not scheduled")[:10]),
                      "data-vendor calendar" if cat.get("next_event_date")
                      else "n/a"), "small"))

    # 12. Research-state change log
    st.append(sec(12))
    cs = view5.get("changeset") or {}
    if cs.get("same_session"):
        st.append(para("Same-session regeneration (prior %s): no "
                       "re-underwriting interval elapsed; change classes "
                       "are presentation/artifact only."
                       % _clean(cs.get("prior_as_of") or ""), "body"))
    elif cs.get("initial_underwriting") or not cs:
        st.append(para("Initial underwriting — no admitted prior "
                       "research state for this name.", "body"))
    else:
        st.append(para("Prior report %s (%s); %s h elapsed."
                       % (_clean(cs.get("prior_report_id") or ""),
                          _clean(cs.get("prior_as_of") or ""),
                          cs.get("elapsed_underwriting_hours")), "body"))
        for c in (cs.get("changes") or [])[:10]:
            st.append(para("&bull; [%s] %s: %s &rarr; %s (%s)"
                           % (_clean(c.get("change_class") or "other"),
                              _clean(c["category"]),
                              _clean(str(c.get("from"))[:36]),
                              _clean(str(c.get("to"))[:36]),
                              _clean(c.get("reason") or "")), "small"))
        if not cs.get("changes"):
            st.append(para("No material change against the prior "
                           "admitted report (artifact regenerated).",
                           "small"))

    # 13. Source inventory and rejected evidence
    st.append(sec(13))
    cov = (snap.get("evidence") or {}).get("coverage") or {}
    st.append(_table([[str(k), _clean(str(v))] for k, v in cov.items()],
                     [BODY_W * .24, BODY_W * .64],
                     header=["Source", "Note"], zebra=True,
                     empty="No source-coverage inventory recorded."))
    pr = ((v4.get("valuation") or {}).get("peers") or {})
    if pr.get("rows"):
        st.append(para("Preliminary peer candidates (vendor grouping) "
                       "&mdash; EXCLUDED from the core: business-model "
                       "similarity, revenue composition, growth, margin "
                       "structure, capital intensity, geography and "
                       "valuation-method comparability have not been "
                       "assessed for any candidate.", "small"))
        st.append(_table([[r.get("ticker") or "-",
                           ("%.1fx" % r["pe"]) if r.get("pe") else "n/a",
                           "not curated"]
                          for r in pr["rows"][:_cap(8)]],
                         [BODY_W * .2, BODY_W * .2, BODY_W * .3],
                         header=["Candidate", "Trailing P/E",
                                 "Inclusion status"], zebra=True))
    if prov and prov.get("news_rejected"):
        st.append(para("Coverage rejected, with reason:", "small"))
        st.append(_table([[_clean(str(r.get("headline") or ""))[:70],
                           _clean(str(r.get("reason") or ""))[:60]]
                          for r in prov["news_rejected"][:_cap(8)]],
                         [BODY_W * .48, BODY_W * .4],
                         header=["Headline", "Why excluded"],
                         zebra=True))
    if prov and prov.get("deferred"):
        st.append(para("Filing facts deferred by the point-in-time gate "
                       "(filed after this report's timestamp):",
                       "small"))
        st.append(_table([[_clean(str(d.get("metric"))),
                           str(d.get("period_end")), str(d.get("form"))]
                          for d in prov["deferred"][:_cap(8)]],
                         [BODY_W * .34, BODY_W * .22, BODY_W * .16],
                         header=["Metric", "Period end", "Form"],
                         zebra=True))

    # 14. Validation summary and artifact hashes — the whole section is
    # kept together so a page break never strands a fragment of the
    # binding table as a sparse final page (§5). Short checksums here;
    # the FULL hashes are recorded once in the introduction paragraph,
    # which is what the binding checks verify against.
    from reportlab.platypus import KeepTogether
    _n[0] += 1
    sec14 = [
        para("%d. %s" % (_n[0], SECTION_TITLES[13]), "h2"),
        para("Binding record for this package:", "body"),
        _table(
            [["Report ID", _clean(report_id)],
             ["Core PDF sha256 (prefix)",
              _clean((core_hash or "n/a")[:16])],
             ["Source-ledger hash (prefix)",
              _clean(((ledger or {}).get("hash") or "n/a")[:16])],
             ["Registered evidence IDs",
              str((ledger or {}).get("count") or 0)],
             ["Assumptions schema", "v5-assumptions/1"],
             ["Generated (UTC)", _clean(gen_at)],
             ["Archetype", _clean((view5.get("archetype") or {}).get(
                 "archetype") or "")]],
            [BODY_W * .3, BODY_W * .6], zebra=True),
        # §5 (v5.8): the binding block certifies WHAT it binds — the
        # complete section inventory travels with the hashes, so the
        # closing page is a genuine certification record rather than a
        # stranded hash table
        para("Sections certified by this record:", "body"),
        _table([[str(i + 1),
                 # the inventory prints the RENDERED titles — section 9
                 # is mode-aware so no scenario vocabulary attaches to
                 # a historical range (§2/§4)
                 ("Historical valuation range and assumptions status"
                  if i == 8 and sc.get("mode") != "underwritten"
                  else SECTION_TITLES[i])]
                for i in range(14)],
               [BODY_W * .08, BODY_W * .62],
               header=["No.", "Contents"], zebra=True),
        para("Full hashes appear once in the introduction; the "
             "appendix PDF's own sha256 and the complete ID-to-source "
             "ledger are in the validation JSON and the companion "
             "source-ledger file (an appendix cannot contain its own "
             "final hash). Validation fails the package when the "
             "version, report ID, method, or any hash disagrees with "
             "the core or the validation JSON.", "small"),
    ]
    st.append(KeepTogether(sec14))
    return st


def _pull_up(story, n_tail_sections):
    """§5 (v5.8) repagination: wrap the last N numbered sections in one
    KeepTogether so the binding block travels with real content instead
    of stranding alone on a sparse final page. ReportLab splits an
    oversized KeepTogether, so the worst case is the original layout."""
    import re as _re

    from reportlab.platypus import KeepTogether, Paragraph
    idxs = [i for i, f in enumerate(story)
            if isinstance(f, Paragraph)
            and _re.match(r"^\d{1,2}\.\s", getattr(f, "text", "")
                          or "")]
    # the final section is already a KeepTogether (not a Paragraph), so
    # grouping the last (n_tail_sections - 1) headings pulls it up
    take = n_tail_sections - 1
    if take < 1 or len(idxs) < take:
        return story
    start = idxs[-take]
    return story[:start] + [KeepTogether(story[start:])]


_FINAL_MIN = 0.45
_BODY_MIN = 0.30


def build(snap, view5, prov, core_hash, ledger, report_id,
          out_path=None):
    rs.assert_exportable(snap, allow_demo=True)

    def _render(story):
        buf = io.BytesIO()
        doc = _Doc(buf, snap, kind=APPENDIX_KIND, legend=False)
        doc.build(story)
        return _finalize(buf.getvalue(), doc)

    _COMPACT[0] = 0
    data = _render(_story(snap, view5, prov, core_hash, ledger,
                          report_id))
    # §5 (v5.8): trim + repagination chain BEFORE the final write.
    # Both floors are enforced together: pulling the tail sections onto
    # one page must not starve an interior page, so candidates combine
    # a compact level (shorter printed samples; full populations stay
    # in the validation JSON) with a pull-up level, and the best
    # candidate by (both floors met, final floor, interior floor,
    # final occupancy) wins.
    try:
        import report_v5_checks as _CK

        def _score(occ):
            if not occ:
                return (True, True, True, 1.0)
            interior_ok = all(r >= _BODY_MIN for r in occ[:-1]) \
                if len(occ) > 1 else True
            final_ok = occ[-1] >= _FINAL_MIN or len(occ) == 1
            return (final_ok and interior_ok, final_ok, interior_ok,
                    occ[-1])

        occ = _CK.measure_occupancy(data)
        _needs_work = len(occ) > 1 and (
            occ[-1] < _FINAL_MIN
            or any(r < _BODY_MIN for r in occ[:-1]))
        if _needs_work:
            best, best_occ = data, occ
            for compact, pull in ((0, 2), (0, 3), (1, 0), (1, 2),
                                  (2, 0), (2, 2), (2, 3),
                                  (-1, 0), (-1, 2), (-2, 0), (-2, 2),
                                  (-3, 0)):
                _COMPACT[0] = compact
                story = _story(snap, view5, prov, core_hash, ledger,
                               report_id)
                if pull:
                    story = _pull_up(story, pull)
                cand = _render(story)
                c_occ = _CK.measure_occupancy(cand)
                print("    [apx §5] compact=%d pull=%d -> %s"
                      % (compact, pull,
                         ["%.0f%%" % (r * 100) for r in c_occ]),
                      file=__import__("sys").stderr)
                if _score(c_occ) > _score(best_occ):
                    best, best_occ = cand, c_occ
                if _score(best_occ)[0]:
                    break
            data = best
    except Exception:
        import traceback
        traceback.print_exc()      # measurement failure keeps pass one
    finally:
        _COMPACT[0] = 0
    if out_path:
        with open(out_path, "wb") as fh:
            fh.write(data)
    return data
