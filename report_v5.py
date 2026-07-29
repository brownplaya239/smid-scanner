#!/usr/bin/env python3
"""report_v5.py — the v5 renderer (slice 4c/5).

Archetype-shaped documents. FULL is the sourced pitch: dashboard with
scenario table, the argument with counterevidence, the financial grid,
valuation detail, then v4's proven technicals and variant/monitoring
pages. NEW_LISTING is a different document entirely (fact sheet /
timeline / trading-since-listing). THIN is the argument + grid without
the pages its evidence cannot carry. DATA_HOLD stays v4's flash.

build_core() also returns the rendered-section map so validation can
hold the document to the archetype contract from both sides.
"""

import io

from reportlab.platypus import PageBreak, Spacer

import report_v4 as R4
import report_v5_archetype as A
import research_snapshot as rs
from report_v3 import (BODY_W, _Doc, _clean, _finalize, _fit_page, _table,
                       para)
from report_v4 import DERIVED, INFERRED, OBSERVED

def _fmt_checkpoint(cp):
    """Typed checkpoint -> reader text."""
    if isinstance(cp, dict):
        if cp.get("date"):
            return "%s (%s)" % (cp["date"], cp.get("source") or "")
        return cp.get("label") or cp.get("source") or "unscheduled"
    return str(cp or "")


ASM_NOTE = ("[ASM] assumption, stated basis — ours or user-supplied, "
            "never a measurement")


def _money(v):
    if v is None:
        return "n/a"
    a = abs(v)
    if a >= 1e12:
        return "$%.2fT" % (v / 1e12)
    if a >= 1e9:
        return "$%.2fB" % (v / 1e9)
    if a >= 1e6:
        return "$%.0fM" % (v / 1e6)
    return "$%.2f" % v


def _pct(v, signed=False):
    if v is None:
        return "n/a"
    return ("%+.1f%%" if signed else "%.1f%%") % v


# ── data confidence box (P1) ─────────────────────────────────────────

def confidence(view5):
    """Five separate axes — strong filing coverage must never imply a
    complete thesis. Each axis: HIGH/MEDIUM/LOW/NOT_AVAILABLE + reason."""
    m = view5.get("multiples") or {}
    nq = max(m.get("n_eps_quarters") or 0, m.get("n_rev_quarters") or 0)
    band = next((m[k] for k in ("pe", "ps")
                 if (m.get(k) or {}).get("available")), None)
    est = (view5.get("v4") or {}).get("estimates_configured")
    exp_var = ((view5.get("expectations") or {}).get("variant")
               or {}).get("available")
    cl = view5.get("claims") or {}
    fund_claims = [c for c in cl.get("claims") or []
                   if c.get("claim_type") in ("fundamental", "valuation")]

    axes = {
        "source_integrity": (
            "HIGH", "filed SEC facts, licensed bars, dated vendor feeds"),
        "quantitative_coverage": (
            ("HIGH" if nq >= 12 and band else
             "MEDIUM" if nq >= 4 else "LOW"),
            "%d filed quarters; band %s" % (nq,
                "%.0f%% coverage" % (100 * band["coverage"])
                if band else "withheld")),
        "qualitative_coverage": (
            "LOW", "industry, moat, management and unit economics have "
                   "no admitted source"),
        "expectations_coverage": (
            ("MEDIUM" if est else "LOW") if not exp_var else "HIGH",
            "consensus feed %s; KPI-level expectations %s"
            % ("connected" if est else "absent",
               "sourced" if exp_var else "not sourced")),
        "thesis_completeness": (
            ("MEDIUM" if len(fund_claims) >= 2 else "LOW"),
            "%d published fundamental claim(s); no underwritten "
            "forecasts" % len(fund_claims)),
    }
    # legacy single level = the weakest of the five (conservative)
    order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "NOT_AVAILABLE": 0}
    worst = min(axes.values(), key=lambda v: order[v[0]])[0]
    return {"level": worst.title(),
            "axes": {k: {"level": v[0], "reason": v[1]}
                     for k, v in axes.items()},
            "reasons": ["%s: %s" % (k.replace("_", " "), v[0])
                        for k, v in axes.items()]}


# ── FULL pages ───────────────────────────────────────────────────────

def _p1_dashboard(snap, view5):
    v4 = view5["v4"]
    st = [R4._masthead(snap, v4), Spacer(1, 6)]

    # One confidence box, not two: enrich the v4 panel's slot with the
    # v5 coverage facts (filed-quarter depth, band coverage) so the
    # rating panel renders a single, richer line.
    conf = confidence(view5)
    v4["data_confidence"] = conf

    st.append(para("Investment summary", "h2"))
    st.append(R4._rating_panel(v4))

    asx = view5.get("assessment") or {}
    bq = asx.get("business_quality") or {}
    ia = asx.get("investment_attractiveness") or {}
    if bq.get("level"):
        _bq_r = "; ".join(bq.get("reasons") or [])
        st.append(para("<b>Reported financial quality: %s &middot; "
                       "overall business quality: Partially "
                       "underwritten</b>%s Not assessed (no "
                       "admitted source): %s."
                       % (bq["level"].title(),
                          (" &mdash; %s." % _clean(_bq_r)) if _bq_r
                          else ".",
                          _clean(", ".join(bq.get("not_assessed")
                                           or []))), "small"))
    if ia.get("level"):
        st.append(para("<b>Investment attractiveness: %s</b> &mdash; "
                       "%s." % (ia["level"],
                                _clean("; ".join(ia["reasons"]))),
                       "small"))
    if asx.get("tension"):
        st.append(para("<i>%s</i>" % _clean(asx["tension"]), "body"))

    # ── underwriting decision (Sundheim object, §5) ──────────────────
    sd = view5.get("sundheim") or {}
    fw = view5.get("framework") or {}
    fsum = fw.get("summary") or {}
    if sd:
        _sc0 = view5.get("scenarios") or {}
        _mode = ("underwritten scenarios"
                 if _sc0.get("mode") == "underwritten"
                 else "historical range (descriptive)"
                 if _sc0.get("available") else "no valuation basis")
        _var0 = ((view5.get("expectations") or {}).get("variant")
                 or {})
        _cl0 = (view5.get("claims") or {}).get("claims") or []
        _cp0 = next((c.get("next_checkpoint") for c in _cl0
                     if c.get("next_checkpoint")), None)
        st.append(para("<b>Underwriting decision (Sundheim record) "
                       "&mdash; status: %s</b> &middot; thesis "
                       "type: %s &middot; framework coverage: %d of %d "
                       "dimensions assessed &middot; valuation basis: "
                       "%s."
                       % (_clean((sd.get("underwriting_status") or ""
                                  ).replace("_", " ").title()),
                          _clean((sd.get("thesis_type") or ""
                                  ).replace("_", " ").lower()),
                          fsum.get("assessed") or 0,
                          fsum.get("total") or 26, _mode), "small"))
        st.append(para("Expectations gap: %s &middot; principal "
                       "uncertainty: %s &middot; next checkpoint: %s."
                       % ("sourced (%+.1f%% on %s)"
                          % (_var0.get("gap_pct", 0),
                             _clean(_var0.get("metric") or ""))
                          if _var0.get("available")
                          else "none sourced &mdash; no variant view "
                               "held",
                          _clean(sd.get("principal_uncertainty")
                                 or "not stated"),
                          _clean(_fmt_checkpoint(_cp0))
                          or "not scheduled"), "small"))
        _fund_inv = next((c["breaks_if"] for c in _cl0
                          if c.get("claim_type") in ("fundamental",
                                                     "valuation")),
                         None)
        _tact_inv = next((c["breaks_if"] for c in _cl0
                          if c.get("claim_type") == "technical"), None)
        st.append(para("<b>Fundamental invalidation:</b> %s &middot; "
                       "<b>Tactical invalidation:</b> %s"
                       % (_clean(_fund_inv or "not established"),
                          _clean(_tact_inv or "not established")),
                       "small"))
        if fsum.get("missing_for_full"):
            st.append(para("Not underwritten (no admitted source): %s. "
                           "Full matrix in the appendix."
                           % _clean(", ".join(
                               d.replace("_", " ") for d in
                               fsum["missing_for_full"])), "small"))

    sc = view5.get("scenarios") or {}
    if sc.get("available"):
        _under = sc.get("mode") == "underwritten"
        st.append(para("Underwritten scenarios" if _under else
                       "Historical valuation range &mdash; not a "
                       "forecast", "h2"))
        head = [""] + [r.get("label") or r["leg"].title()
                       for r in sc["rows"]]
        mults, prices, vs = ["Multiple"], ["Price"], ["vs last"]
        for r in sc["rows"]:
            mults.append("%.1fx [%s]" % (r["multiple"]["value"],
                                         r["multiple"]["grade"]))
            prices.append("$%.2f" % r["price"])
            vs.append(_pct(r["vs_spot_pct"], signed=True))
        metric = sc["rows"][0]["metric"]
        body = [mults,
                ["Trailing metric",
                 "%.2f [%s]" % (metric["value"], metric["grade"]), "", ""],
                prices, vs]
        st.append(_table(body, [BODY_W * .28, BODY_W * .22, BODY_W * .22,
                                BODY_W * .22], header=head, zebra=True))
        w = sc.get("weighted")
        st.append(para(
            ("Probability-weighted value $%.2f [ASM] &mdash; %s. "
             % (w["price"], _clean(w["basis"]))) if w else
            ("Scenarios are unweighted: probabilities render only when "
             "user-supplied." if _under else
             "Percentile prices only &mdash; the range carries no "
             "probabilities and no return forecast."), "small"))
        band = sc.get("band_ref") or {}
        _ay = band.get("actual_years")
        band_note = para("Percentiles of this name's own daily trailing "
                         "%s over the available %s history "
                         "(%s&ndash;%s), each day computed only from "
                         "filings available before that session, applied "
                         "to a CONSTANT trailing metric. This is where "
                         "the stock has traded, not where it is going. "
                         "Full arithmetic on the valuation page."
                         % ((band.get("kind") or "").upper(),
                            ("%.1f-year" % _ay) if _ay else "",
                            _clean(band.get("window_start") or ""),
                            _clean(band.get("window_end") or "")),
                         "small", DERIVED)
        band_note_short = para("Own-history trailing-%s percentiles "
                               "over the available %s window, constant "
                               "metric &mdash; descriptive, not a "
                               "forecast. Method and arithmetic on the "
                               "valuation page."
                               % ((band.get("kind") or "").upper(),
                                  ("%.1f-year" % _ay) if _ay else ""),
                               "small", DERIVED)
        st.append(band_note)
    else:
        st.append(para("Valuation range", "h2"))
        st.append(para("Withheld: %s." % _clean(sc.get("reason")
                                                or "no basis"), "small"))
    if sc.get("assumptions_note"):
        st.append(para("Assumptions: %s" % _clean(sc["assumptions_note"]),
                       "small"))

    # What-changed is built as a discrete block so the fit logic can
    # move it wholesale to the appendix (section 12 carries it in full)
    # rather than stranding its tail on a spilled page.
    cs = view5.get("changeset") or {}
    changed_block = [para("What changed since the prior report", "h2")]
    if cs.get("same_session"):
        changed_block.append(para(
            "Prior report generated in the same session (%s) &mdash; "
            "no re-underwriting interval has elapsed; change tracking "
            "begins with the next dated run."
            % _clean(cs.get("prior_as_of") or ""), "small"))
        cs = {"suppressed": True}
    if cs.get("suppressed"):
        pass
    elif cs.get("initial_underwriting") or not cs:
        changed_block.append(para(
            "Initial underwriting &mdash; no prior admitted report for "
            "this name.", "small"))
    else:
        _tax = cs.get("taxonomy") or {}
        changed_block.append(para(
            "Prior: %s (core sha %s&hellip;)%s."
            % (_clean(cs.get("prior_as_of") or ""),
               (cs.get("prior_core_pdf_hash") or "")[:12],
               " &middot; %.1f h elapsed &middot; changes by class: %s"
               % (cs.get("elapsed_underwriting_hours"),
                  _clean(", ".join("%s %d" % (k.replace("_", " "), v)
                                   for k, v in sorted(_tax.items()))))
               if cs.get("elapsed_underwriting_hours") is not None
               and _tax else ""), "small"))
        changes = cs.get("changes") or []
        if not changes:
            changed_block.append(para(
                "No material change against the prior admitted "
                "report.", "small"))
        for c in changes[:5]:
            changed_block.append(para(
                "&bull; %s: %s &rarr; %s (%s)"
                % (_clean(c["category"]),
                   _clean(str(c.get("from"))[:38]),
                   _clean(str(c.get("to"))[:38]),
                   _clean(c.get("reason") or "")), "small"))
    st += changed_block

    cl = view5.get("claims") or {}
    one_look = [para("Investment case in one look", "h2")]
    if cl.get("claims"):
        for c in cl["claims"]:
            one_look.append(para("&bull; [%s, %s confidence] %s"
                                 % (c["direction"], c["confidence"],
                                    _clean(c["claim"])), "body"))
    else:
        one_look.append(para(_clean(cl.get("note") or "No claim "
                                    "cleared the evidence bar."),
                             "small"))
    st += one_look
    asm_para = para(ASM_NOTE, "small")
    st.append(asm_para)

    # Page 1 must fit its frame (§13) — an overflow strands the tail on
    # a near-blank page 2. Trims move content, never delete evidence:
    # the one-look list duplicates page 2's full argument, the change
    # log lives complete in the appendix, and the band note keeps a
    # compact form that still points at the full method.
    def _drop_one_look(story):
        # page 2's "Investment case" heading carries the full argument;
        # no pointer line is appended — it would just become the next
        # spilled tail
        return [f for f in story if f not in one_look]

    change_paras = [f for f in st
                    if getattr(f, "text", "").startswith("&bull; ")
                    and f not in one_look]

    def _drop_changes(story):
        return [f for f in story if f not in change_paras]

    def _shorten_band_note(story):
        try:
            return [band_note_short if f is band_note else f
                    for f in story]
        except NameError:
            return story

    def _drop_asm_note(story):
        # the [ASM] grade is restated wherever an assumption renders
        # (valuation page) and defined in the appendix — the standalone
        # legend line is the last thing to keep over a page spill
        return [f for f in story if f is not asm_para]

    def _drop_changed_block(story):
        # appendix section 12 carries the complete change log; the
        # whole block moves rather than stranding its tail
        return [f for f in story if f not in changed_block]

    trims = [("change detail moved to appendix", _drop_changes),
             ("band note compacted", _shorten_band_note),
             ("one-look moved to page 2", _drop_one_look),
             ("ASM legend moved to appendix", _drop_asm_note),
             ("change log moved to appendix", _drop_changed_block)]
    st, _trim = _fit_page(st, trims, "v5-p1")
    # Font-metric drift (tuned vs rendered face) makes a "fits" page
    # spill a two-line tail onto a near-blank page 2. Keep a safety
    # margin: trim further while within 15% of the frame budget.
    from report_v3 import _avail_height, _story_height
    for _name, fn in trims:
        if _story_height(st) <= _avail_height() * 0.85:
            break
        st = fn(st)
    return st


def _p2_argument(snap, view5):
    cl = view5.get("claims") or {}
    st = [para("Investment case", "h2")]
    if not cl.get("claims"):
        st.append(para(_clean(cl.get("note") or "no claims"), "body"))
        return st
    for i, c in enumerate(cl["claims"], 1):
        st.append(para("%d. %s  <b>[%s &middot; %s &middot; %s "
                       "confidence]</b>"
                       % (i, _clean(c["claim"]), c["direction"],
                          _clean(c.get("status") or ""),
                          c["confidence"]), "h3"))
        if c.get("market_expectation"):
            st.append(para("<i>Market:</i> %s (%s)"
                           % (_clean(c["market_expectation"]),
                              _clean(c.get("market_expectation_source")
                                     or "")), "small"))
        else:
            st.append(para("<i>Business insight</i> &mdash; no sourced "
                           "market expectation; no variant view is "
                           "claimed.", "small"))
        st.append(para("<i>Mechanism:</i> %s"
                       % _clean(c.get("mechanism") or ""), "body"))
        for sline in c["support"]:
            st.append(para("&bull; %s" % _clean(sline), "body"))
        if c["counterevidence"]:
            for x in c["counterevidence"]:
                st.append(para("&bull; <i>Against:</i> %s" % _clean(x),
                               "body"))
        else:
            st.append(para("&bull; <i>Counterevidence:</i> none "
                           "identified in admitted evidence. Coverage "
                           "limitations are detailed in the appendix.",
                           "small"))
        st.append(para("<i>Implication:</i> %s &middot; %s"
                       % (_clean(c.get("financial_implication") or ""),
                          _clean(c.get("valuation_implication") or "")),
                       "small"))
        st.append(para("<i>Breaks:</i> %s &middot; <i>next checkpoint:"
                       "</i> %s &middot; <i>valid until:</i> %s"
                       % (_clean(c["breaks_if"]),
                          _clean(_fmt_checkpoint(
                              c.get("next_checkpoint"))),
                          _clean(c.get("maximum_valid_until") or "")),
                       "small", DERIVED))
    rej = cl.get("rejected") or []
    if rej:
        st.append(para("Candidates that failed the publication gate",
                       "h3"))
        for r in rej[:4]:
            st.append(para("&bull; %s &mdash; %s"
                           % (_clean(r["claim"][:70]),
                              _clean("; ".join(r["failed_gates"]))),
                           "small"))
    st, _ = _fit_page(st, [], "v5-p2")
    return st


def _p_diligence(snap, view5):
    """Named Tiger-26 diligence section: one bullet per assessed
    dimension with its conclusion; unassessed dimensions stay a single
    honest line (no admitted source -> never inferred)."""
    import report_v5_framework as FW
    fw = view5.get("framework") or {}
    dims = fw.get("dimensions") or {}
    if not dims:
        return []
    fsum = fw.get("summary") or {}
    st = [para("Diligence matrix (Tiger 26 dimensions)", "h2"),
          para("%d of %d dimensions carry at least a partial "
               "underwriting. Full per-dimension table (evidence refs, "
               "next evidence needed) and the Sundheim decision record: "
               "appendix sections 1&ndash;2."
               % (fsum.get("assessed") or 0,
                  fsum.get("total") or len(FW.TIGER_DIMENSIONS)),
               "small")]
    _status_h = {FW.UNDERWRITTEN: "underwritten",
                 FW.PARTIAL: "partially assessed"}
    n_assessed = 0
    na, inapplicable = [], []
    for k in FW.TIGER_DIMENSIONS:
        d = dims.get(k) or {}
        label = FW.DIM_LABELS.get(k, k)
        if d.get("status") in (FW.UNDERWRITTEN, FW.PARTIAL):
            n_assessed += 1
            st.append(para("&bull; <b>%s</b> &mdash; %s <i>(%s, %s "
                           "confidence)</i>"
                           % (_clean(label),
                              _clean(d.get("conclusion") or ""),
                              _status_h[d["status"]],
                              _clean(d.get("confidence") or "low")),
                           "small", INFERRED))
        elif d.get("status") == FW.NOT_APPLICABLE:
            inapplicable.append(label)
        else:
            na.append(label)
    if not n_assessed:
        st.append(para("No dimension carries an admitted source; "
                       "nothing is inferred in its place.", "small"))
    if na:
        st.append(para("Not assessed (%d &mdash; no admitted source; "
                       "not inferred): %s." % (len(na),
                                               _clean(", ".join(na))),
                       "small"))
    if inapplicable:
        st.append(para("Not applicable under the sector policy: %s."
                       % _clean(", ".join(inapplicable)), "small"))
    st, _ = _fit_page(st, [], "v5-diligence")
    return st


def _p3_grid(snap, view5):
    g = view5.get("grid") or {}
    st = [para("Financial dashboard &mdash; as filed", "h2")]
    years = g.get("years") or []
    if not years:
        # a missing annual grid must not short-circuit the SECTOR
        # dashboard below — a thin filing history is exactly when the
        # sector-appropriate slots (and their absences) matter most
        st.append(para("No comparably filed annual history.", "small"))
    else:
        head = ["$M unless noted"] + [y[:4] for y in years] + ["TTM"]
        body = []
        for key, label, kind in g["rows"]:
            row = [label]
            for y in years:
                v = (g["columns"][y] or {}).get(key)
                row.append(_grid_cell(v, kind))
            row.append(_grid_cell((g.get("ttm") or {}).get(key), kind))
            body.append(row)
        w = BODY_W * 0.30
        cw = [w] + [(BODY_W - w) / (len(years) + 1)] * (len(years) + 1)
        st.append(_table(body, cw, header=head, zebra=True))
        _thru = (g.get("ttm") or {}).get("through")
        st.append(para("%s %s." % (
            ("TTM through %s." % _clean(_thru)) if _thru
            else "TTM column suppressed &mdash; no metric has four "
                 "contiguous current quarters (see notes below).",
            _clean(g.get("basis") or "")), "small", OBSERVED))
    for gap in g.get("gaps") or []:
        st.append(para(_clean(gap), "small"))

    # ── sector KPI dashboard (adapter, §7) ───────────────────────────
    # Core shows the admitted values plus the first few sector slots;
    # the complete slot inventory lives in appendix section 4 — the
    # page must not trade its guidance table for empty rows.
    ad = view5.get("adapter") or {}
    adapter_extras = []
    if ad.get("key") and ad.get("key") not in ("new_listing",):
        st.append(para("%s dashboard" % _clean(ad.get("label")
                                               or "Sector"), "h2"))
        import report_v5_adapters as ADP
        drows = ADP.build_dashboard(ad, snap, view5.get("grid"))
        have = [r for r in drows if r[1] != "no admitted source"]
        absent = [r for r in drows if r[1] == "no admitted source"]
        shown = have + absent[:3]
        if shown:
            st.append(_table([[_clean(a), _clean(b), _clean(c)]
                              for a, b, c in shown],
                             [BODY_W * .38, BODY_W * .26, BODY_W * .28],
                             header=["Metric", "Value", "Provenance"],
                             zebra=True))
        if len(absent) > 3:
            st.append(para("+%d further sector metrics have no admitted "
                           "source; the full inventory is in the "
                           "appendix." % (len(absent) - 3), "small"))
        for o in ad.get("one_time_items") or []:
            st.append(para("<b>One-time item, named:</b> the latest "
                           "quarter includes a %s. Margins including it "
                           "are not graded as quality."
                           % _clean(o["label"]), "small", OBSERVED))
        for note in (ad.get("notes") or [])[:1]:
            st.append(para("&bull; %s" % _clean(note), "small"))
        prov_para = para("Adapter selected from the admitted vendor "
                         "classification (%s); slots without an "
                         "admitted source say so rather than borrowing "
                         "another sector's metrics."
                         % _clean(ad.get("reason") or ""), "small")
        adapter_extras.append(prov_para)
        st.append(prov_para)

    # guidance block reuses the v4 page-3 rendering via the view
    v4 = view5["v4"]
    ex = snap.get("exhibit") or {}
    hl = (ex.get("guidance_highlights")
          if ex.get("disposition") == "ADMITTED" else None)
    st.append(para("Guidance (issuer, filed release)", "h2"))
    if hl:
        rows = []
        for k, gd in hl.items():
            if k == "fx_commentary" or not isinstance(gd, dict) \
                    or gd.get("low") is None:
                continue
            rng = ("%.1f%% &ndash; %.1f%%" % (gd["low"], gd["high"])
                   if gd.get("unit") == "%" else
                   "%s &ndash; %s" % (_money(gd["low"]), _money(gd["high"])))
            from report_v5_checks import human_metric_label
            rows.append([_clean(gd.get("label")
                                or human_metric_label(k)), rng])
        if rows:
            st.append(_table(rows, [BODY_W * .45, BODY_W * .45],
                             header=["Metric", "Guided"], zebra=True))
        else:
            st.append(para("No ranges parsed from the admitted exhibit.",
                           "small"))
    else:
        st.append(para("No guidance admitted from a filed exhibit.",
                       "small"))

    # The guidance table must never be traded for adapter boilerplate:
    # the provenance line trims first (it lives in the appendix).
    def _drop_adapter_extras(story):
        return [f for f in story if f not in adapter_extras]

    trims = [("adapter provenance moved to appendix",
              _drop_adapter_extras)]
    st, _ = _fit_page(st, trims, "v5-p3")
    from report_v3 import _avail_height, _story_height
    if _story_height(st) > _avail_height() * 0.90:
        st = _drop_adapter_extras(st)
    return st


def _grid_cell(v, kind):
    if v is None:
        return "&mdash;"
    if kind == "money":
        return "{:,.0f}".format(v / 1e6)
    if kind == "derived-pct":
        return "%.1f%%" % v
    if kind == "pershare":
        return "$%.2f" % v
    return str(v)


def _p4_valuation(snap, view5):
    m = view5.get("multiples") or {}
    sc = view5.get("scenarios") or {}
    st = [para("Valuation &mdash; the arithmetic, written out", "h2")]
    for kind in ("pe", "ps"):
        b = m.get(kind) or {}
        lab = "Trailing P/E" if kind == "pe" else "Price / TTM rev per sh"
        if b.get("available"):
            st.append(para("%s band: P25 %.1fx &middot; P50 %.1fx &middot; "
                           "P75 %.1fx &middot; now %.1fx (range %.1f&ndash;"
                           "%.1f) &mdash; %d of %d sessions computable "
                           "over %s to %s"
                           % (lab, b["p25"], b["p50"], b["p75"],
                              b["current"], b["min"], b["max"],
                              b["sessions_computable"],
                              b["sessions_in_window"],
                              _clean(b["window_start"]),
                              _clean(b["window_end"])), "body", DERIVED))
        else:
            st.append(para("%s band withheld: %s"
                           % (lab, _clean(b.get("reason") or "n/a")),
                           "small"))
    st.append(para("Point-in-time rule: each session's multiple uses only "
                   "filings available before that session, as first "
                   "reported; per-share facts are rebased across splits "
                   "by filing date.", "small"))
    if sc.get("available"):
        st.append(para("Range arithmetic (constant trailing metric)", "h2"))
        for line in sc.get("arithmetic") or []:
            st.append(para(_clean(line), "body", DERIVED))
        import report_v5_scenarios as S5
        base = S5.anchor(sc)
        if base:
            sens = base["metric"]["value"]
            st.append(para("Sensitivity: &plusmn;1 turn of the "
                           "median multiple = &plusmn;$%.2f on the "
                           "median-implied price." % sens, "small",
                           DERIVED))
        asym = sc.get("asymmetry") or {}
        if asym and sc.get("mode") == "underwritten":
            st.append(para("Range span: P25 %s%% &middot; P75 %s%% vs spot "
                           "&middot; upside/downside %s"
                           % (asym.get("downside_to_bear_pct"),
                              asym.get("upside_to_bull_pct"),
                              ("%.1fx" % asym["up_down_ratio"])
                              if asym.get("up_down_ratio") else "n/a"),
                           "body", DERIVED))
        w = sc.get("weighted")
        if w:
            st.append(para("Probability-weighted value $%.2f "
                           "(%+.1f%% expected%s) [ASM] &mdash; %s. %s."
                           % (w["price"], w.get("expected_return_pct", 0),
                              (", %.1f%%/yr over %gy"
                               % (w["annualized_return_pct"],
                                  w["horizon_years"]))
                              if w.get("annualized_return_pct")
                              is not None else "",
                              _clean(w["basis"]),
                              _clean(w.get("caveat") or "")), "small"))
        for r in sc["rows"]:
            if r["multiple"]["grade"] == "ASM":
                st.append(para("%s multiple is an assumption: %s"
                               % (_clean(r.get("label")
                                         or r["leg"].title()),
                                  _clean(r["multiple"]["basis"])),
                               "small"))
    # ── expectations matrix (canonical object, phase C) ──────────────
    exp = view5.get("expectations") or {}
    if exp.get("matrix"):
        st.append(para("Expectations &mdash; who expects what", "h2"))
        rows = [[_clean(m["topic"])[:34], _clean(m["market"])[:38],
                 _clean(m["tickerdesk"])[:30], m["evidence"],
                 _clean(m["implication"])[:26]]
                for m in exp["matrix"]]
        st.append(_table(rows, [BODY_W * .22, BODY_W * .26, BODY_W * .2,
                                BODY_W * .07, BODY_W * .17],
                         header=["Topic", "Market / guidance",
                                 "TickerDesk", "Ev.", "Implication"],
                         zebra=True))
        if exp.get("justify_price"):
            st.append(para("<b>Priced in:</b> %s."
                           % _clean(exp["justify_price"]), "small",
                           DERIVED))
        var = exp.get("variant") or {}
        if var.get("available"):
            st.append(para("<b>Variant perception:</b> TickerDesk %.4g "
                           "vs market %.4g on %s &mdash; a %+.1f%% gap "
                           "(%s)." % (var["tickerdesk"], var["market"],
                                      _clean(var["metric"]),
                                      var["gap_pct"],
                                      _clean(var["source"])), "body"))
        else:
            st.append(para("No variant perception is claimed: %s."
                           % _clean(var.get("reason") or ""), "small"))

    # Peer comparison (§14): the vendor grouping is NOT curated for
    # business-model similarity, revenue mix, growth, margins, capital
    # intensity, geography or method comparability — so it does not
    # publish in the core. The candidate list lives in the appendix
    # with its exclusion reason.
    val4 = (view5["v4"].get("valuation") or {})
    pr = val4.get("peers") or {}
    if pr.get("rows"):
        st.append(para("Peer comparison omitted: the vendor peer "
                       "candidates are not curated for comparability; "
                       "the preliminary list and exclusion criteria are "
                       "in the appendix.", "small"))
    st, _ = _fit_page(st, [], "v5-p4")
    return st


# ── NEW_LISTING pages ────────────────────────────────────────────────

def _listing_facts(ticker):
    """Admitted listing-record facts (IPO price, shares, proceeds,
    lock-up terms) from data/listing_facts/<TICKER>.json — a user-
    admitted input file like the assumptions contract. Absent file ->
    absent facts, never a convention-based guess."""
    import json
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "listing_facts",
                        "%s.json" % (ticker or "").upper())
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        if doc.get("schema") != "v5-listing-facts/1":
            return None
        return doc
    except Exception:
        return None


def _nl_factsheet(snap, view5):
    v4 = view5["v4"]
    th = snap.get("trading_history") or {}
    lv = snap.get("levels") or {}
    st = [R4._masthead(snap, v4), Spacer(1, 6),
          para("New listing &mdash; fact sheet", "h2"),
          para("<b>Underwriting status: NOT UNDERWRITTEN.</b> No "
               "fundamental claim is published: sufficient prospectus "
               "and periodic operating evidence has not been admitted.",
               "body")]
    est = view5.get("estimates") or {}
    rec = est.get("recommendation") or {}
    if rec.get("band"):
        st.append(para("<b>Street consensus (vendor, dated %s):</b> %s. "
                       "<b>TickerDesk holds no view</b> &mdash; the "
                       "consensus is reported, not endorsed."
                       % (_clean(rec.get("as_of") or ""),
                          _clean(rec.get("band") or "")), "small"))
    px = rs.fv(lv.get("price_used")) or rs.fv(lv.get("last_close"))
    rows = [["Listed", _clean(th.get("listing_date") or "n/a")],
            ["Completed sessions", str(th.get("sessions") or 0)],
            ["Last price", "$%.2f" % px if px else "n/a"],
            ["Range since listing",
             "$%.2f &ndash; $%.2f" % (rs.fv(lv.get("support")) or 0,
                                      rs.fv(lv.get("resistance")) or 0)
             if rs.fv(lv.get("support")) else "n/a"]]
    lf = _listing_facts(v4.get("ticker"))
    if lf:
        for label, key in (("IPO price", "ipo_price"),
                           ("Shares offered", "shares_offered"),
                           ("Gross proceeds", "gross_proceeds"),
                           ("Float", "float_shares"),
                           ("Lock-up terms", "lockup_terms")):
            v = lf.get(key)
            if v is not None:
                rows.append([label, _clean(str(v))])
        st.append(para("Offering facts from the admitted listing record "
                       "(%s)." % _clean(lf.get("source") or
                                        "user-admitted"), "small",
                       OBSERVED))
    else:
        rows.append(["IPO price / proceeds / lock-up",
                     "not admitted &mdash; prospectus not parsed"])
    st.append(_table(rows, [BODY_W * .34, BODY_W * .5], zebra=True))
    st.append(para("What does not exist yet (stated once): no filed 10-K "
                   "or 10-Q, so no revenue, margin or cash-flow history; "
                   "no 50/200-day averages or 52-week range at this "
                   "history length; no own-history multiple band, so no "
                   "valuation range &mdash; a valuation anchored to six "
                   "weeks of trading would be invented, not computed.",
                   "body"))
    q = ((view5.get("framework") or {}).get("summary")
         or {}).get("unanswered") or []
    if q:
        st.append(para("Highest-value unanswered diligence questions",
                       "h3"))
        for line in q[:5]:
            st.append(para("&bull; %s" % _clean(line), "small"))
    st.append(para("Sources: exchange listing data (Polygon reference), "
                   "SEC EDGAR filings index, licensed daily bars.",
                   "small", OBSERVED))
    st, _ = _fit_page(st, [], "v5-nl1")
    return st


def _nl_timeline(snap, view5):
    th = snap.get("trading_history") or {}
    cat = snap.get("catalyst") or {}
    st = [para("Timeline and newness risks", "h2")]
    listed = th.get("listing_date")
    rows = []
    if listed:
        rows.append(["Listing", listed, "exchange record"])
        lf = _listing_facts((view5.get("v4") or {}).get("ticker"))
        if lf and lf.get("lockup_expiry"):
            rows.append(["Lock-up expiry",
                         _clean(str(lf["lockup_expiry"])),
                         "admitted listing record (%s)"
                         % _clean(lf.get("source") or "user-admitted")])
        else:
            # No convention-based estimates (§7): the actual quiet-period
            # and lock-up terms live in the prospectus, which is not
            # parsed — so no date is stated at all.
            rows.append(["Quiet period / lock-up expiry", "not stated",
                         "prospectus terms not parsed &mdash; customary "
                         "conventions are not substituted for the "
                         "actual terms"])
    nxt = cat.get("next_event_date") or cat.get("event_dt")
    if nxt:
        rows.append(["First expected report", _clean(str(nxt)[:10]),
                     "data-vendor estimate, not issuer-confirmed"])
    st.append(_table(rows, [BODY_W * .3, BODY_W * .2, BODY_W * .42],
                     header=["Event", "Date", "Basis"], zebra=True))
    st.append(para("Newness risks", "h2"))
    for r in ("Float expansion at lock-up expiry can add supply "
              "regardless of results.",
              "No filed operating history: every fundamental claim "
              "traces to the prospectus, not to periodic reports this "
              "pipeline verifies.",
              "Index inclusion, coverage initiations and the first "
              "earnings report are one-off events without a base rate "
              "for this security."):
        st.append(para("&bull; %s" % r, "body"))
    st, _ = _fit_page(st, [], "v5-nl2")
    return st


def _nl_trading(snap, view5, chart_png=None, chart_meta=None):
    st = [para("Trading since listing", "h2")]
    if chart_png:
        from report_v3 import _image
        st.append(_image(chart_png, BODY_W, 4.2 * 72))
    else:
        th = snap.get("trading_history") or {}
        st.append(para("Chart omitted: %d completed sessions, fewer than "
                       "the 30 the chart requires."
                       % (th.get("sessions") or 0), "small"))
    lv = snap.get("levels") or {}
    rows = []
    for k, lab in (("resistance", "Highest close since listing"),
                   ("support", "Lowest close since listing"),
                   ("ma20", "20-day average")):
        v = rs.fv(lv.get(k))
        if v is not None:
            rows.append([lab, "$%.2f" % v])
    if rows:
        st.append(_table(rows, [BODY_W * .4, BODY_W * .3],
                         header=["Level", "Price"], zebra=True))
    st, _ = _fit_page(st, [], "v5-nl3")
    return st


# ── assembly ─────────────────────────────────────────────────────────

# v5.8 review fix (P1): a core page below this floor is a layout
# failure — THIN reports collapse to fewer, denser pages instead of
# reserving near-empty ones.
CORE_PAGE_MIN = 0.30


def _fit_core_segments(snap, seg_builders):
    """Assemble segment stories with page breaks, then greedily REMOVE
    breaks while any rendered page falls below CORE_PAGE_MIN — a THIN
    argument page flows into the grid page instead of standing at 9%
    occupancy. Segment builders are re-invoked per attempt (reportlab
    consumes flowables); candidates are scored by (sparse pages, page
    count)."""
    import report_v5_checks as CK

    def render(story):
        buf = io.BytesIO()
        doc = _Doc(buf, snap, kind="Equity Research v5")
        doc.build(story)
        return _finalize(buf.getvalue(), doc)

    def assemble(drop):
        story = []
        segs = [b() for b in seg_builders]
        for i, s in enumerate(segs):
            story += s
            if i < len(segs) - 1 and i not in drop:
                story.append(PageBreak())
        return story

    def sparse(occ):
        if len(occ) <= 1:
            return []
        return [i for i, r in enumerate(occ) if r < CORE_PAGE_MIN]

    drop = set()
    data = render(assemble(drop))
    occ = CK.measure_occupancy(data)
    for _ in range(len(seg_builders)):
        if not sparse(occ):
            break
        best = None
        for k in range(len(seg_builders) - 1):
            if k in drop:
                continue
            cdata = render(assemble(drop | {k}))
            cocc = CK.measure_occupancy(cdata)
            score = (len(sparse(cocc)), len(cocc))
            if best is None or score < best[0]:
                best = (score, k, cdata, cocc)
        if best is None or best[0] >= (len(sparse(occ)), len(occ)):
            break                      # no break-removal helps further
        drop.add(best[1])
        data, occ = best[2], best[3]
    return data


def build_core(snap, view5, out_path=None, chart_png=None,
               chart_meta=None):
    """-> (pdf_bytes, rendered_sections) — the section map feeds the
    archetype-contract validation."""
    v4 = view5["v4"]
    arch = view5["archetype"]["archetype"]
    buf = io.BytesIO()
    doc = _Doc(buf, snap, kind="Equity Research v5")
    rendered = {}
    seg_builders = None

    if arch == A.DATA_HOLD or v4.get("flash"):
        story = R4._flash_page(snap, v4)
        rendered["flash"] = True
    elif arch == A.NEW_LISTING:
        seg_builders = [
            lambda: _nl_factsheet(snap, view5),
            lambda: _nl_timeline(snap, view5),
            lambda: _nl_trading(snap, view5, chart_png, chart_meta)]
        rendered.update({"listing_factsheet": True,
                         "listing_timeline": True,
                         "listing_trading": True})
    else:
        rendered["dashboard"] = True
        rendered["valuation_table"] = bool(
            (view5.get("scenarios") or {}).get("available"))
        rendered["argument"] = True
        rendered["financial_grid"] = True
        rendered["diligence_matrix"] = True
        seg_builders = [lambda: _p1_dashboard(snap, view5),
                        lambda: _p2_argument(snap, view5),
                        lambda: _p_diligence(snap, view5),
                        lambda: _p3_grid(snap, view5)]
        if rendered["valuation_table"] or arch in (A.FULL, A.FULL_THIN):
            seg_builders.append(lambda: _p4_valuation(snap, view5))
            rendered["valuation_detail"] = True
        # Page-6 variant must obey the SAME gate as pages 2 and 4:
        # the canonical expectations decision. Unsourced -> the v4
        # variant text renders as "Key debate", never "Variant
        # perception".
        exp_var = ((view5.get("expectations") or {}).get("variant")
                   or {})
        v4_p6 = v4
        debate = None
        if not exp_var.get("available"):
            old_var = v4.get("variant") or {}
            if old_var.get("available"):
                debate = old_var.get("text") or old_var.get("detail")
            v4_p6 = dict(v4, variant={
                "available": False,
                "reason": "no sourced market expectation — the debate "
                          "renders as a business insight, not a "
                          "variant"})

        def _last_segment():
            _debate_story = ([para("Key debate (no sourced "
                                   "expectations &mdash; not a "
                                   "variant view)", "h2"),
                              para(_clean(debate), "body", INFERRED)]
                             if debate else [])
            return _debate_story + R4._page6(snap, v4_p6)

        if arch in (A.FULL, A.FULL_THIN):
            seg_builders.append(
                lambda: R4._page5(snap, v4, chart_png, chart_meta))
            rendered["technicals"] = True
            rendered["event_path"] = True
        seg_builders.append(_last_segment)
        rendered["variant_risks"] = True

    if seg_builders is not None:
        data = _fit_core_segments(snap, seg_builders)
    else:
        doc.build(story)
        data = _finalize(buf.getvalue(), doc)
    if out_path:
        with open(out_path, "wb") as fh:
            fh.write(data)
    return data, rendered
