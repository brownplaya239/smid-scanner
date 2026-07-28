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
    """High/Medium/Low + 2-3 reasons, from coverage facts only."""
    reasons, score = [], 0
    m = view5.get("multiples") or {}
    nq = max(m.get("n_eps_quarters") or 0, m.get("n_rev_quarters") or 0)
    if nq >= 12:
        score += 1
        reasons.append("%d filed quarters of history" % nq)
    elif nq:
        reasons.append("only %d filed quarters" % nq)
    band = next((m[k] for k in ("pe", "ps")
                 if (m.get(k) or {}).get("available")), None)
    if band:
        score += 1
        reasons.append("multiple band at %.0f%% session coverage"
                       % (100 * band.get("coverage", 0)))
    else:
        reasons.append("no multiple band survived coverage")
    if (view5.get("v4") or {}).get("estimates_configured"):
        score += 1
        reasons.append("consensus feed connected")
    else:
        reasons.append("no consensus feed this run")
    level = "High" if score >= 3 else "Medium" if score == 2 else "Low"
    return {"level": level, "reasons": reasons[:3]}


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

    sc = view5.get("scenarios") or {}
    if sc.get("available"):
        st.append(para("Scenarios &mdash; own-history multiples, filed "
                       "trailing metric", "h2"))
        head = ["", "Bear", "Base", "Bull"]
        rows = {r["leg"]: r for r in sc["rows"]}
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
        st.append(para("Multiples are the P25/P50/P75 of this name's own "
                       "daily trailing %s over %s&ndash;%s, each day "
                       "computed only from filings available before that "
                       "session. Full arithmetic on the valuation page."
                       % ((band.get("kind") or "").upper(),
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

    cl = view5.get("claims") or {}
    st.append(para("The argument in one look", "h2"))
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
    st = [para("The argument", "h2")]
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
            st.append(para("&bull; <i>Against:</i> none found in filed "
                           "data (searched: %s)"
                           % _clean("; ".join(cl.get("searched") or [])),
                           "small"))
        st.append(para("<i>Implication:</i> %s &middot; %s"
                       % (_clean(c.get("financial_implication") or ""),
                          _clean(c.get("valuation_implication") or "")),
                       "small"))
        st.append(para("<i>Breaks:</i> %s &middot; <i>next checkpoint:"
                       "</i> %s &middot; <i>valid until:</i> %s"
                       % (_clean(c["breaks_if"]),
                          _clean(c.get("next_checkpoint") or ""),
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
        st.append(para("Scenario arithmetic", "h2"))
        for line in sc.get("arithmetic") or []:
            st.append(para(_clean(line), "body", DERIVED))
        rows = {r["leg"]: r for r in sc["rows"]}
        base = rows.get("base")
        if base:
            sens = base["metric"]["value"]
            st.append(para("Sensitivity: &plusmn;1 turn of the base "
                           "multiple = &plusmn;$%.2f on the base price."
                           % sens, "small", DERIVED))
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
        st.append(para("Peer cross-check (vendor grouping, uncurated)",
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
        if arch == A.FULL:
            story += R4._page5(snap, v4, chart_png, chart_meta) \
                + [PageBreak()]
            rendered["technicals"] = True
            story += R4._page6(snap, v4)
            rendered["event_path"] = True
            rendered["variant_risks"] = True
        else:
            story += R4._page6(snap, v4)
            rendered["variant_risks"] = True

    doc.build(story)
    data = _finalize(buf.getvalue(), doc)
    if out_path:
        with open(out_path, "wb") as fh:
            fh.write(data)
    return data, rendered
