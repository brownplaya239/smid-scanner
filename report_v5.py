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
        st.append(para("<b>Reported financial quality: %s &middot; "
                       "overall business quality: Partially "
                       "underwritten</b> &mdash; %s. Not assessed (no "
                       "admitted source): %s."
                       % (bq["level"].title(),
                          _clean("; ".join(bq["reasons"])),
                          _clean(", ".join(bq.get("not_assessed")
                                           or []))), "small"))
    if ia.get("level"):
        st.append(para("<b>Investment attractiveness: %s%s</b> &mdash; "
                       "%s." % (ia["level"],
                                " (provisional)" if ia.get("qualifier")
                                == "PROVISIONAL" else "",
                                _clean("; ".join(ia["reasons"]))),
                       "small"))
    if asx.get("tension"):
        st.append(para("<i>%s</i>" % _clean(asx["tension"]), "body"))

    sc = view5.get("scenarios") or {}
    if sc.get("available"):
        _under = sc.get("mode") == "underwritten"
        st.append(para("Underwritten scenarios" if _under else
                       "Historical valuation range &mdash; not a "
                       "forecast", "h2"))
        rows = {r["leg"]: r for r in sc["rows"]}
        head = [""] + [rows[l].get("label") or l.title()
                       for l in ("bear", "base", "bull")]
        mults, prices, vs = ["Multiple"], ["Price"], ["vs last"]
        for leg in ("bear", "base", "bull"):
            r = rows[leg]
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
            "Scenarios are unweighted: probabilities render only when "
            "user-supplied.", "small"))
        band = sc.get("band_ref") or {}
        _ay = band.get("actual_years")
        st.append(para("Percentiles of this name's own daily trailing "
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
                       "small", DERIVED))
    else:
        st.append(para("Scenarios", "h2"))
        st.append(para("Withheld: %s." % _clean(sc.get("reason")
                                                or "no basis"), "small"))
    if sc.get("assumptions_note"):
        st.append(para("Assumptions: %s" % _clean(sc["assumptions_note"]),
                       "small"))

    cs = view5.get("changeset") or {}
    st.append(para("What changed since the prior report", "h2"))
    if cs.get("same_session"):
        st.append(para("Prior report generated in the same session "
                       "(%s) &mdash; no re-underwriting interval has "
                       "elapsed; change tracking begins with the next "
                       "dated run." % _clean(cs.get("prior_as_of")
                                             or ""), "small"))
        cs = {"suppressed": True}
    if cs.get("suppressed"):
        pass
    elif cs.get("initial_underwriting") or not cs:
        st.append(para("Initial underwriting &mdash; no prior admitted "
                       "report for this name.", "small"))
    else:
        st.append(para("Prior: %s (core sha %s&hellip;)."
                       % (_clean(cs.get("prior_as_of") or ""),
                          (cs.get("prior_core_pdf_hash") or "")[:12]),
                       "small"))
        changes = cs.get("changes") or []
        if not changes:
            st.append(para("No material change against the prior "
                           "admitted report.", "small"))
        for c in changes[:5]:
            st.append(para("&bull; %s: %s &rarr; %s (%s)"
                           % (_clean(c["category"]),
                              _clean(str(c.get("from"))[:38]),
                              _clean(str(c.get("to"))[:38]),
                              _clean(c.get("reason") or "")), "small"))

    cl = view5.get("claims") or {}
    _pubs = cl.get("claims") or []
    _fund_inv = next((c["breaks_if"] for c in _pubs
                      if c.get("claim_type") in ("fundamental",
                                                 "valuation")), None)
    _tact_inv = next((c["breaks_if"] for c in _pubs
                      if c.get("claim_type") == "technical"), None)
    st.append(para("<b>Fundamental invalidation:</b> %s &middot; "
                   "<b>Tactical invalidation:</b> %s"
                   % (_clean(_fund_inv or "not established"),
                      _clean(_tact_inv or "not established")), "small"))

    st.append(para("Investment case in one look", "h2"))
    if cl.get("claims"):
        for c in cl["claims"]:
            st.append(para("&bull; [%s, %s confidence] %s"
                           % (c["direction"], c["confidence"],
                              _clean(c["claim"])), "body"))
    else:
        st.append(para(_clean(cl.get("note") or "No claim cleared the "
                              "evidence bar."), "small"))
    st.append(para(ASM_NOTE, "small"))
    st, _ = _fit_page(st, [], "v5-p1")
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


def _p3_grid(snap, view5):
    g = view5.get("grid") or {}
    st = [para("Financial dashboard &mdash; as filed", "h2")]
    years = g.get("years") or []
    if not years:
        st.append(para("No comparably filed annual history.", "small"))
        return st
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
    st.append(para("TTM through %s. %s." % (
        _clean((g.get("ttm") or {}).get("through") or "n/a"),
        _clean(g.get("basis") or "")), "small", OBSERVED))
    for gap in g.get("gaps") or []:
        st.append(para(_clean(gap), "small"))

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
            rows.append([_clean(gd.get("label") or k), rng])
        if rows:
            st.append(_table(rows, [BODY_W * .45, BODY_W * .45],
                             header=["Metric", "Guided"], zebra=True))
        else:
            st.append(para("No ranges parsed from the admitted exhibit.",
                           "small"))
    else:
        st.append(para("No guidance admitted from a filed exhibit.",
                       "small"))
    st, _ = _fit_page(st, [], "v5-p3")
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
        rows = {r["leg"]: r for r in sc["rows"]}
        base = rows.get("base")
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
                               % (r["leg"].title(),
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

    # peer cross-check from the v4 view, if present
    val4 = (view5["v4"].get("valuation") or {})
    pr = val4.get("peers") or {}
    if pr.get("rows"):
        st.append(para("Preliminary peer reference (vendor grouping)",
                       "h2"))
        prows = [[r["ticker"], "%.1fx" % r["pe"] if r.get("pe") else "n/a"]
                 for r in pr["rows"][:6]]
        st.append(_table(prows, [BODY_W * .4, BODY_W * .3],
                         header=["Peer", "Trailing P/E"], zebra=True))
    st, _ = _fit_page(st, [], "v5-p4")
    return st


# ── NEW_LISTING pages ────────────────────────────────────────────────

def _nl_factsheet(snap, view5):
    v4 = view5["v4"]
    th = snap.get("trading_history") or {}
    lv = snap.get("levels") or {}
    st = [R4._masthead(snap, v4), Spacer(1, 6),
          para("New listing &mdash; fact sheet", "h2")]
    px = rs.fv(lv.get("price_used")) or rs.fv(lv.get("last_close"))
    rows = [["Listed", _clean(th.get("listing_date") or "n/a")],
            ["Completed sessions", str(th.get("sessions") or 0)],
            ["Last price", "$%.2f" % px if px else "n/a"],
            ["Range since listing",
             "$%.2f &ndash; $%.2f" % (rs.fv(lv.get("support")) or 0,
                                      rs.fv(lv.get("resistance")) or 0)
             if rs.fv(lv.get("support")) else "n/a"]]
    st.append(_table(rows, [BODY_W * .34, BODY_W * .5], zebra=True))
    st.append(para("What does not exist yet (stated once): no filed 10-K "
                   "or 10-Q, so no revenue, margin or cash-flow history; "
                   "no 50/200-day averages or 52-week range at this "
                   "history length; no own-history multiple band, so no "
                   "scenario table &mdash; a valuation anchored to six "
                   "weeks of trading would be invented, not computed.",
                   "body"))
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
        import datetime as dt
        d0 = dt.date.fromisoformat(listed)
        rows.append(["Listing", listed, "exchange record"])
        rows.append(["Customary quiet period ends",
                     "~%s" % (d0 + dt.timedelta(days=25)).isoformat(),
                     "25-day convention &mdash; prospectus not parsed "
                     "[INF]"])
        rows.append(["Customary lock-up expiry",
                     "~%s" % (d0 + dt.timedelta(days=180)).isoformat(),
                     "180-day convention &mdash; the actual terms are in "
                     "the prospectus, which this report does not parse "
                     "[INF]"])
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

def build_core(snap, view5, out_path=None, chart_png=None,
               chart_meta=None):
    """-> (pdf_bytes, rendered_sections) — the section map feeds the
    archetype-contract validation."""
    v4 = view5["v4"]
    arch = view5["archetype"]["archetype"]
    buf = io.BytesIO()
    doc = _Doc(buf, snap, kind="Equity Research v5")
    rendered = {}

    if arch == A.DATA_HOLD or v4.get("flash"):
        story = R4._flash_page(snap, v4)
        rendered["flash"] = True
    elif arch == A.NEW_LISTING:
        story = (_nl_factsheet(snap, view5) + [PageBreak()]
                 + _nl_timeline(snap, view5) + [PageBreak()]
                 + _nl_trading(snap, view5, chart_png, chart_meta))
        rendered.update({"listing_factsheet": True,
                         "listing_timeline": True,
                         "listing_trading": True})
    else:
        story = _p1_dashboard(snap, view5) + [PageBreak()]
        rendered["dashboard"] = True
        rendered["scenario_table"] = bool(
            (view5.get("scenarios") or {}).get("available"))
        story += _p2_argument(snap, view5) + [PageBreak()]
        rendered["argument"] = True
        story += _p3_grid(snap, view5) + [PageBreak()]
        rendered["financial_grid"] = True
        if rendered["scenario_table"] or arch == A.FULL:
            story += _p4_valuation(snap, view5) + [PageBreak()]
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
        _debate_story = ([para("Key debate (no sourced expectations "
                               "&mdash; not a variant view)", "h2"),
                          para(_clean(debate), "body", INFERRED)]
                         if debate else [])
        if arch == A.FULL:
            story += R4._page5(snap, v4, chart_png, chart_meta) \
                + [PageBreak()]
            rendered["technicals"] = True
            story += _debate_story
            story += R4._page6(snap, v4_p6)
            rendered["event_path"] = True
            rendered["variant_risks"] = True
        else:
            story += _debate_story
            story += R4._page6(snap, v4_p6)
            rendered["variant_risks"] = True

    doc.build(story)
    data = _finalize(buf.getvalue(), doc)
    if out_path:
        with open(out_path, "wb") as fh:
            fh.write(data)
    return data, rendered
