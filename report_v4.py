#!/usr/bin/env python3
"""report_v4.py — Equity Research v4 renderer.

A clean sell-side format: a concise 6-page core (investment summary,
business, earnings, valuation, technicals, catalysts/monitoring) and a
separate evidence-and-methodology appendix. The evidence discipline is
v3's — every figure traces to a source, nothing is invented — but the
provenance lives in the appendix and in restrained inline labels, not in
a tag on every clause, so the page reads like research.

Two structural behaviours the spec makes non-negotiable and this file
enforces before a page is drawn:

  * The event gate decides the document. In DATA HOLD the core report is
    NOT produced; a one-page "Earnings update pending verification" flash
    is, because a rating on unverified results is exactly what must not
    ship.
  * A withheld datum renders as a labelled "unavailable — <reason>" line,
    never a blank and never a guess. On the current estimate tier the
    12-month target and forward consensus are withheld this way.

Rendering primitives (fonts, tables, the page-fit compression that keeps
a page inside its frame under the CI font) are imported from report_v3 —
the same machinery, already fixed and tested.
"""

import io

from reportlab.platypus import PageBreak, Spacer, Table, TableStyle

import report_v3 as R
import report_v4_model as M4
import research_snapshot as rs
from report_v3 import (para, safe, _clean, tag, _table, _fit_page,
                       _avail_height, _story_height, BODY_W, ST,
                       INK, MUTED, ACCENT, GREEN, RED, AMBER, LINE, BG_SOFT)

OBSERVED, DERIVED, INFERRED = M4.OBSERVED, M4.DERIVED, M4.INFERRED

# Colour a rating band by direction, without turning the page into a
# traffic light: only the two decisive bands take a hue.
_BAND_COLOR = {"Buy": GREEN, "Constructive": GREEN, "Outperform": GREEN,
               "Sell": RED, "Weak": RED, "Underperform": RED}


def _fv(x):
    return rs.fv(x)


def _wh_line(label, wh, style="body"):
    """A withheld datum, stated plainly with its reason. This is the
    honest shape of a gap — the reader learns what is missing and why,
    not a blank."""
    return para("<b>%s:</b> unavailable &mdash; %s"
                % (_clean(label), _clean(wh.get("reason") or "no source")),
                style)


def _money(v):
    if v is None:
        return "n/a"
    a = abs(v)
    if a >= 1e9:
        return "$%.2fB" % (v / 1e9)
    if a >= 1e6:
        return "$%.0fM" % (v / 1e6)
    return "$%s" % ("{:,.0f}".format(v))


# ── masthead ────────────────────────────────────────────────────────────

def _masthead(snap, view):
    co = snap.get("company") or {}
    name = rs.fv(co.get("name")) or view.get("ticker")
    sector = rs.fv(co.get("sector")) or "—"
    px = view.get("price")
    ev = view["event"]
    band_f = view["ratings"]["fundamental"]
    band_t = view["ratings"]["tactical"]

    left = [
        para("EQUITY RESEARCH", "lab"),
        para("<b>%s</b>  <font color='%s'>%s</font>"
             % (_clean(view.get("ticker") or ""), MUTED.hexval(),
                _clean(name)), "h2"),
        para("%s &middot; %s" % (_clean(sector),
                                 "$%.2f" % px if px else "price n/a"),
             "small"),
    ]
    fr = (band_f["band"] if band_f.get("available") else "NR")
    tr = (band_t["band"] if band_t.get("available") else "NR")
    right = [
        para("<b>Event state:</b> %s" % _clean(ev["state"]), "small"),
        para("Fundamental (consensus): <b>%s</b>" % _clean(fr), "small"),
        para("Tactical (technical): <b>%s</b>" % _clean(tr), "small"),
    ]
    t = Table([[left, right]], colWidths=[BODY_W * 0.62, BODY_W * 0.38],
              hAlign="LEFT")
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("LEFTPADDING", (0, 0), (-1, -1), 0),
                           ("LINEBELOW", (0, 0), (-1, -1), 0.8, LINE),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    return t


# ── page 1: investment summary ──────────────────────────────────────────

def _rating_panel(view):
    r = view["ratings"]
    fr, tr, tg = r["fundamental"], r["tactical"], r["target"]
    rows = []

    def band_cell(x, kind):
        if not x.get("available"):
            return Paragraph_muted("Not rated — %s" % (x.get("reason") or ""))
        col = _BAND_COLOR.get(x["band"], INK).hexval()
        sub = x.get("detail") or ""
        return R.Paragraph("<font color='%s'><b>%s</b></font><br/>"
                           "<font size='8' color='%s'>%s</font>"
                           % (col, _clean(x["band"]), MUTED.hexval(),
                              _clean(sub)), ST["body"])

    rows.append(["Fundamental rating", band_cell(fr, "f")])
    rows.append(["Tactical rating", band_cell(tr, "t")])
    if tg.get("available"):
        er = tg.get("expected_return_pct")
        rows.append(["12-month target",
                     R.Paragraph("<b>$%s</b> (range $%s-$%s)%s"
                                 % (tg["mean"], tg.get("low"), tg.get("high"),
                                    "  &middot;  expected return %+.1f%%"
                                    % er if er is not None else ""),
                                 ST["body"])])
    else:
        rows.append(["12-month target",
                     R.Paragraph("unavailable &mdash; %s"
                                 % _clean(tg.get("reason") or ""),
                                 ST["body"])])
    rows.append(["Horizon", R.Paragraph(_clean(view.get("horizon") or ""),
                                        ST["body"])])
    rows.append(["Event state", R.Paragraph(_clean(view["event"]["state"]),
                                            ST["body"])])
    t = _table(rows, [BODY_W * 0.26, BODY_W * 0.74], zebra=True)
    return t


def Paragraph_muted(text):
    return R.Paragraph("<font color='%s'>%s</font>"
                       % (MUTED.hexval(), _clean(text)), ST["body"])


def _page1(snap, view):
    st = [_masthead(snap, view), Spacer(1, 6),
          para("Investment summary", "h2"),
          _rating_panel(view)]

    st.append(para("Thesis", "h2"))
    th = view.get("thesis") or []
    if th:
        for b in th:
            st.append(para("&bull; %s" % _clean(b["text"]), "body"))
    else:
        st.append(para("No thesis point cleared the evidence gate.", "small"))

    st.append(para("Key risks", "h2"))
    rk = view.get("risks") or []
    if rk:
        for b in rk:
            st.append(para("&bull; %s" % _clean(b["text"]), "body"))
    else:
        st.append(para("No filed-fact risk rose to the top three.", "small"))

    st.append(para("Consensus and estimates carry Finnhub's as-of date; "
                   "filed figures are SEC XBRL. Observed facts, derived "
                   "calculations and interpretation are separated in the "
                   "appendix.", "small"))

    def _fewer_thesis(story):
        # thesis and risks are the compressible copy; keep two of each
        keep, seen_h, seen_r = [], 0, 0
        for f in story:
            keep.append(f)
        return keep
    st, _ = _fit_page(st, [], "v4-p1")
    return st


# ── page 2: business ────────────────────────────────────────────────────

def _page2(snap, view):
    st = [para("Business model and competitive position", "h2")]
    bd = view.get("business") or {}
    sents = bd.get("sentences") or []
    biz_ps = []
    if sents:
        p = para(" ".join(_clean(safe(x["text"])) for x in sents), "body")
        biz_ps.append(p)
        st.append(p)
        srcs = []
        for x in sents:
            if x["source"] not in srcs and x["source"] != "coverage statement":
                srcs.append(x["source"])
        st.append(para("Sources: %s." % "; ".join(srcs), "small"))
    elif bd.get("plain"):
        st.append(para(bd["plain"], "body", OBSERVED))
    else:
        st.append(para("No business description was admitted from a cited "
                       "source.", "body", OBSERVED))

    # Product mix / market opportunity: honest about what is and is not
    # sourced. Segment mix and market share have no structured feed.
    st.append(para("Product mix and market opportunity", "h2"))
    st.append(para("Segment revenue mix, addressable-market sizing and "
                   "market share are not ingested from a structured "
                   "source and are not estimated here. The filed figures "
                   "above describe scale and profitability; the "
                   "competitive read is qualitative and sourced to the "
                   "issuer's own description.", "small"))

    # Customer economics — from what the filings carry (margins, cash gen).
    fin = view["financials"]["reported"]
    st.append(para("Customer economics and cash generation", "h2"))
    bits = []
    if fin.get("gross_margin") is not None:
        bits.append("gross margin %.1f%%" % fin["gross_margin"])
    if fin.get("net_margin") is not None:
        bits.append("net margin %.1f%%" % fin["net_margin"])
    if fin.get("operating_cash_flow") is not None:
        bits.append("operating cash flow %s"
                    % _money(fin["operating_cash_flow"]))
    if fin.get("free_cash_flow") is not None:
        bits.append("free cash flow %s" % _money(fin["free_cash_flow"]))
    if bits:
        st.append(para("Unit economics are read through the P&L and cash "
                       "flow the issuer files: %s. Per-customer metrics "
                       "(ACV, net retention) are not disclosed in a "
                       "structured form and are not shown."
                       % ", ".join(bits), "body", DERIVED))
    else:
        st.append(para("No margin or cash-flow figure cleared the filing "
                       "gate for this period.", "small"))

    st, _ = _fit_page(st, [], "v4-p2")
    return st


# ── page 3: earnings ────────────────────────────────────────────────────

def _pct(v, signed=False):
    if v is None:
        return "n/a"
    return ("%+.1f%%" if signed else "%.1f%%") % v


def _page3(snap, view):
    fin = view["financials"]
    rep = fin["reported"]
    st = [para("Latest quarter and estimate context", "h2")]

    rows = [
        ["Revenue", _money(rep.get("revenue_q")),
         "y/y " + _pct(rep.get("revenue_growth"), signed=True)],
        ["Gross margin", _pct(rep.get("gross_margin")), ""],
        ["Net margin", _pct(rep.get("net_margin")), ""],
        ["Net income", _money(rep.get("net_income_q")), ""],
        ["Operating cash flow", _money(rep.get("operating_cash_flow")), ""],
        ["Free cash flow", _money(rep.get("free_cash_flow")), ""],
        ["Diluted EPS (TTM)",
         "$%.2f" % rep["eps_ttm"] if rep.get("eps_ttm") is not None
         else "n/a", ""],
    ]
    st.append(_table(rows, [BODY_W * 0.34, BODY_W * 0.28, BODY_W * 0.30],
                     header=["Reported (latest quarter, SEC XBRL)", "Value",
                             "Change"], zebra=True))

    # vs consensus: the free tier gives past surprises, not forward
    # estimates. Show what it gives; withhold what it does not.
    st.append(para("Earnings vs consensus (history)", "h2"))
    sur = fin.get("surprises") or []
    if sur:
        srows = [[s.get("period") or "—",
                  "$%.2f" % s["actual"] if s.get("actual") is not None
                  else "n/a",
                  "$%.2f" % s["estimate"] if s.get("estimate") is not None
                  else "n/a",
                  _pct(s.get("surprise_pct"), signed=True)] for s in sur[:4]]
        st.append(_table(srows, [BODY_W * .26, BODY_W * .22, BODY_W * .22,
                                 BODY_W * .22],
                         header=["Period", "Actual EPS", "Consensus EPS",
                                 "Surprise"], zebra=True))
    else:
        st.append(para("No earnings-surprise history was returned by the "
                       "estimate feed (or the feed is not configured).",
                       "small"))

    fc = fin["forward_consensus"]
    if not fc.get("available"):
        st.append(_wh_line("Forward consensus and estimate revisions", fc,
                           "small"))

    # guidance from the exhibit, when it parsed
    cat = view.get("catalysts") or {}
    st.append(para("Guidance", "h2"))
    ex = (snap.get("exhibit") or {})
    gui = ex.get("guidance") if ex.get("disposition") == "ADMITTED" else None
    if gui:
        grows = []
        for k, g in gui.items():
            if isinstance(g, dict) and g.get("raw"):
                grows.append([_clean(g.get("label") or k),
                              _clean(str(g["raw"]))])
        if grows:
            st.append(_table(grows, [BODY_W * 0.5, BODY_W * 0.42],
                             header=["Metric", "Guided"], zebra=True))
    else:
        st.append(para("No guidance parsed from a filed earnings exhibit "
                       "for this period.", "small"))

    st.append(_wh_line("Subscription revenue, cRPO/RPO, ACV, customers and "
                       "net revenue retention", fin["saas_kpis"], "small"))

    st, _ = _fit_page(st, [], "v4-p3")
    return st


# ── page 4: valuation ───────────────────────────────────────────────────

def _page4(snap, view):
    val = view["valuation"]
    st = [para("Valuation", "h2")]

    pe = val.get("pe_trailing")
    band = val["historical_band"]
    if band.get("available"):
        pos = ("currently %.0f%% of the way up the band"
               % band["position_pct"]
               if band.get("position_pct") is not None else "")
        basis = _clean(band["basis"])
        basis = basis[:1].upper() + basis[1:] if basis else basis
        st.append(para("Trailing P/E of <b>%.1fx</b> against a 52-week band "
                       "of <b>%.1fx&ndash;%.1fx</b>%s. %s."
                       % (band["pe_now"], band["pe_low"], band["pe_high"],
                          " &mdash; %s" % pos if pos else "", basis),
                       "body", DERIVED))
    elif pe is not None:
        st.append(para("Trailing P/E %.1fx. A 52-week P/E band could not be "
                       "built &mdash; %s." % (pe, _clean(band.get("reason")
                                              or "band unavailable")),
                       "body", DERIVED))
    else:
        st.append(para("No trailing multiple could be computed for this "
                       "name.", "small"))

    # scenarios: re-rating on unchanged EPS, explicitly not a target
    sc = val["scenarios"]
    st.append(para("Scenario range (re-rating on unchanged EPS)", "h2"))
    if sc.get("available"):
        srows = [
            ["Bear", "%.1fx" % sc["bear"]["pe"],
             "$%.2f" % sc["bear"]["price"]],
            ["Base", "%.1fx" % sc["base"]["pe"],
             "$%.2f" % sc["base"]["price"]],
            ["Bull", "%.1fx" % sc["bull"]["pe"],
             "$%.2f" % sc["bull"]["price"]],
        ]
        st.append(_table(srows, [BODY_W * 0.3, BODY_W * 0.3, BODY_W * 0.32],
                         header=["Scenario", "P/E", "Implied price"],
                         zebra=True))
        _sb = _clean(sc["basis"])
        st.append(para((_sb[:1].upper() + _sb[1:] if _sb else _sb) + ".",
                       "small"))
    else:
        st.append(_wh_line("Scenario range", sc, "small"))

    # peers
    st.append(para("Peer multiples", "h2"))
    pr = val["peers"]
    if pr.get("rows"):
        prows = [[r["ticker"],
                  "%.1fx" % r["pe"] if r.get("pe") is not None else "n/a"]
                 for r in pr["rows"]]
        subj = "%.1fx" % pe if pe is not None else "n/a"
        prows.insert(0, ["%s (subject)" % (view.get("ticker") or ""), subj])
        st.append(_table(prows, [BODY_W * 0.5, BODY_W * 0.42],
                         header=["Ticker", "Trailing P/E"], zebra=True))
        st.append(para("Peers are Finnhub's sector grouping; multiples are "
                       "vendor trailing P/E. %s." % _clean(pr.get("source")
                                                           or ""), "small"))
    else:
        st.append(_wh_line("Peer multiples", pr, "small"))

    st.append(_wh_line("12-month price target and price-target bridge",
                       val["target_bridge"], "small"))

    st, _ = _fit_page(st, [], "v4-p4")
    return st


# ── the DATA HOLD flash ─────────────────────────────────────────────────

def _flash_page(snap, view):
    fl = view["flash"]
    return [
        _masthead(snap, view), Spacer(1, 10),
        para(_clean(fl["headline"]), "action"),
        Spacer(1, 6),
        para(_clean(fl["body"]), "body", OBSERVED),
        Spacer(1, 8),
        para("No rating, target or directional view is issued while the "
             "event gate is in DATA HOLD. This notice is republished when "
             "the primary release and its guidance can be read from a "
             "filed source.", "small"),
    ]


# NOTE: pages 3-6 and the appendix are built in the next slices. build_core
# below renders the flash (DATA HOLD) or pages 1-2; it grows as the pages
# land, each verified before the next.

def build_core(snap, view, out_path=None):
    from report_v3 import _Doc, _finalize
    buf = io.BytesIO()
    doc = _Doc(buf, snap, kind="Equity Research v4")
    if view.get("flash"):
        story = _flash_page(snap, view)
    else:
        story = (_page1(snap, view) + [PageBreak()]
                 + _page2(snap, view) + [PageBreak()]
                 + _page3(snap, view) + [PageBreak()]
                 + _page4(snap, view))
    doc.build(story)
    data = _finalize(buf.getvalue(), doc)
    if out_path:
        with open(out_path, "wb") as fh:
            fh.write(data)
    return data
