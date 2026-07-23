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

from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Spacer, Table, TableStyle

import report_v3 as R
import report_v3_model as M3
import report_v4_model as M4
import research_snapshot as rs
from report_v3 import (para, safe, _clean, tag, _table, _fit_page,
                       _avail_height, _story_height, _image, BODY_W, ST,
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
    """Valuation on real inputs only. Rendered when at least one honest
    multiple exists; build_core omits the page entirely otherwise, rather
    than padding six pages with a near-empty one."""
    val = view["valuation"]
    st = [para("Valuation", "h2")]

    # Headline multiples: trailing P/E, EV, EV/run-rate revenue, FCF yield.
    tp = val.get("trailing_pe") or {}
    ev = val.get("enterprise_value") or {}
    evr = val.get("ev_to_revenue") or {}
    fy = val.get("fcf_yield") or {}
    rows = []
    if tp.get("available"):
        rows.append(["Trailing P/E", "%.1fx" % tp["value"],
                     "price / TTM EPS"])
    if ev.get("available"):
        rows.append(["Enterprise value", _money(ev["value"]),
                     "market cap + debt &minus; cash"])
    if evr.get("available"):
        rows.append(["EV / revenue (run-rate)", "%.1fx" % evr["value"],
                     "EV / annualised latest quarter"])
    if fy.get("available"):
        rows.append(["FCF yield (run-rate)", "%.1f%%" % fy["value"],
                     "annualised quarterly FCF / market cap"])
    if rows:
        st.append(_table([[r[0], r[1], R.Paragraph(r[2], ST["small"])]
                          for r in rows],
                         [BODY_W * 0.32, BODY_W * 0.22, BODY_W * 0.38],
                         header=["Metric", "Value", "Basis"], zebra=True))
        st.append(para("Run-rate multiples annualise the latest quarter and "
                       "are labelled as such; they are not trailing-twelve-"
                       "month figures.", "small", DERIVED))

    # Peer multiples.
    st.append(para("Peer multiples", "h2"))
    pr = val["peers"]
    if pr.get("rows"):
        prows = [[r["ticker"],
                  "%.1fx" % r["pe"] if r.get("pe") is not None else "n/a"]
                 for r in pr["rows"]]
        subj = "%.1fx" % tp["value"] if tp.get("available") else "n/a"
        prows.insert(0, ["%s (subject)" % (view.get("ticker") or ""), subj])
        st.append(_table(prows, [BODY_W * 0.5, BODY_W * 0.42],
                         header=["Ticker", "Trailing P/E"], zebra=True))
        st.append(para("Peers are Finnhub's sector grouping; multiples are "
                       "vendor trailing P/E. %s." % _clean(pr.get("source")
                                                           or ""), "small"))
    else:
        st.append(_wh_line("Peer multiples", pr, "small"))

    # What this tier cannot value on, said plainly — never manufactured.
    st.append(para("Not valued here", "h2"))
    st.append(_wh_line("Historical multiple band", val["historical_multiples"],
                       "small"))
    st.append(_wh_line("Forward / bull-base-bear scenarios",
                       val["forward_scenarios"], "small"))

    st, _ = _fit_page(st, [], "v4-p4")
    return st


# ── page 5: the technical chart ─────────────────────────────────────────

def _page5(snap, view, chart_png=None, chart_meta=None):
    """The chart a reader trades from, full text width, with the levels it
    plots tabulated beneath it and our technical read in words. The chart
    is built by the runner (it needs the raw bar series, which the view
    does not carry) and passed in; absent, the page says so plainly."""
    st = [para("Price and technicals", "h2")]
    lv = snap.get("levels") or {}

    if chart_png:
        st.append(_image(chart_png, BODY_W, 4.4 * inch))
        cm = chart_meta or {}
        bits = []
        if cm.get("sessions"):
            bits.append("%d completed sessions" % cm["sessions"])
        if cm.get("log_scale"):
            bits.append("log price axis")
        bits.append("relative strength vs SPY rebased to 100 at the window "
                    "start" if cm.get("rs_panel") else
                    "relative-strength panel omitted: benchmark series not "
                    "retained for this run")
        if cm.get("earnings_marked"):
            bits.append("E marks a verified earnings release")
        if cm.get("partial"):
            bits.append("final bar PARTIAL and excluded from every average")
        st.append(para("Candles with SMA 20/50/200, volume against its "
                       "20-session average, and RSI(14). %s."
                       % ("; ".join(bits)), "small", DERIVED))
    else:
        st.append(para("Price chart unavailable: no bar series was retained "
                       "for this run.", "small"))

    rows = []
    px = view.get("price")
    if px is not None:
        rows.append(["Last (completed session)", "$%.2f" % px])
    for key, lab in (("resistance_major", "52-week closing high"),
                     ("resistance", "60-session closing high"),
                     ("support", "60-session closing low"),
                     ("support_major", "52-week closing low")):
        val = rs.fv(lv.get(key))
        if val is not None:
            rows.append([lab, "$%.2f" % val])
    if len(rows) > 1:
        st.append(para("Key levels on the chart", "h2"))
        st.append(_table(rows, [BODY_W * 0.55, BODY_W * 0.37],
                         header=["Level", "Price"], zebra=True))

    tac = (view.get("ratings") or {}).get("tactical") or {}
    if tac.get("available") and tac.get("detail"):
        detail = _clean(tac["detail"]).rstrip(".")
        st.append(para("Technical read", "h2"))
        st.append(para("%s. Tactical stance: <b>%s</b> (%d of the 20/50/200 "
                       "averages reclaimed)."
                       % (detail, _clean(tac["band"]),
                          tac.get("above_mas", 0)), "body", DERIVED))

    st, _ = _fit_page(st, [], "v4-p5")
    return st


# ── page 6: catalysts, variant, thesis triggers, monitoring ─────────────

def _page6(snap, view):
    """What moves the name next, where our view differs, and the specific
    prices that would confirm or break the thesis — the page a reader keeps
    open. Every trigger is a dated, evidence-linked level the v3 decision
    block already computed; nothing here is a fresh opinion."""
    st = [para("Catalysts, variant view and what to monitor", "h2")]

    cat = view.get("catalysts") or {}
    lr = cat.get("last_reported") or {}
    dr = cat.get("current_driver") or {}
    nxt = cat.get("next") or []
    st.append(para("Catalysts", "h3"))
    if lr.get("what"):
        st.append(para("<b>Last confirmed:</b> %s (%s). %s"
                       % (_clean(lr.get("what")), _clean(lr.get("when") or ""),
                          _clean(lr.get("confirmation") or "")), "small",
                       lr.get("grade")))
    if dr.get("text"):
        st.append(para(_clean(dr["text"]), "small", dr.get("grade")))
    if nxt:
        n0 = nxt[0]
        st.append(para("<b>Next:</b> %s%s (%s)"
                       % (_clean(n0.get("what") or ""),
                          " on %s" % _clean(n0["when"]) if n0.get("when")
                          else "", _clean(n0.get("confirmation") or "")),
                       "small", n0.get("grade")))

    st.append(para("Ranked risks", "h3"))
    for r in (view.get("risks") or [])[:3]:
        st.append(para("&bull; %s" % _clean(r.get("text") or ""), "small",
                       r.get("grade")))
    if not view.get("risks"):
        st.append(para("No risk rose above the filing-evidence bar for this "
                       "name.", "small"))

    var = view.get("variant") or {}
    st.append(para("Variant perception", "h3"))
    if var.get("available"):
        st.append(para(_clean(var["text"]), "body", var.get("grade")))
    else:
        st.append(_wh_line("Variant perception", var, "small"))

    mon = view.get("monitoring") or {}
    st.append(para("Thesis confirm / break", "h3"))
    if mon.get("upgrade_trigger"):
        st.append(para("<b>Confirms on:</b> %s" % _clean(mon["upgrade_trigger"]),
                       "small", DERIVED))
    if mon.get("downside_confirmation"):
        st.append(para("<b>Breaks on:</b> %s"
                       % _clean(mon["downside_confirmation"]), "small",
                       DERIVED))

    stages = mon.get("recovery_stages") or []
    if stages:
        st.append(para("Monitoring checklist", "h3"))
        srows = [["met" if s.get("met") else "open",
                  _clean(s.get("stage") or ""),
                  _clean(s.get("condition") or "")] for s in stages]
        st.append(_table(srows, [BODY_W * 0.12, BODY_W * 0.26, BODY_W * 0.54],
                         header=["State", "Stage", "Condition"], zebra=True))
    if mon.get("monitor_next"):
        st.append(para("<b>Next to watch:</b> %s" % _clean(mon["monitor_next"]),
                       "small", DERIVED))
    if mon.get("review_date"):
        st.append(para("Scheduled review: %s." % _clean(mon["review_date"]),
                       "small"))

    st, _ = _fit_page(st, [], "v4-p6")
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


# ── the appendix: evidence and methodology ──────────────────────────────

def _acc_cell(accession, url):
    """An accession number, hyperlinked to the filing when we hold the URL
    — the reader can open the primary source, not just read its id."""
    if url and accession:
        return R.Paragraph('<link href="%s" color="#1f3a5f">%s</link>'
                           % (_clean(url), _clean(accession)), ST["cell"])
    return accession or "—"


def _appendix_story(snap, view, estimates=None, prov=None):
    """The v4 audit trail: the event-state reconciliation and the estimates
    ledger it turns on, the valuation formulas, the source inventory, the
    raw insider and institutional tables with their accession numbers, and
    the evidence that was sampled or rejected. Everything the six pages
    cite but do not print, so any figure can be traced to its filing."""
    est = estimates or {}
    when = snap.get("market_data_time") or snap.get("report_time") or ""
    _n = [0]

    def sec(title):
        _n[0] += 1
        return para("%d. %s" % (_n[0], title), "h2")

    st = [para("Appendix &mdash; evidence and methodology", "h2"),
          para("The audit trail for the six-page Equity Research v4 report on "
               "%s. It is not a summary: it records the event reconciliation, "
               "where every figure came from, what was withheld and why, and "
               "the formula behind each derived value. Market data as of %s."
               % (_clean(view.get("ticker") or ""), _clean(str(when))),
               "small")]

    # Event-state reconciliation
    ev = view.get("event") or {}
    st.append(sec("Event-state reconciliation"))
    st.append(para("Before any rating the report reconciles the ET clock, the "
                   "earnings calendar, the issuer's filings and the call "
                   "status into one event state. This run resolved to <b>%s</b> "
                   "and a directional rating was %s."
                   % (_clean(ev.get("state") or "?"),
                      "permitted" if ev.get("rating_allowed") else "withheld"),
                   "small", OBSERVED))
    for r in (ev.get("reasons") or [])[:6]:
        st.append(para("&bull; " + _clean(str(r)), "small"))

    # 2. Estimates and consensus ledger
    st.append(sec("Estimates and consensus ledger"))
    if est.get("configured"):
        cov = est.get("coverage") or {}
        st.append(_table([[k, v] for k, v in cov.items()],
                         [BODY_W * 0.3, BODY_W * 0.62],
                         header=["Endpoint", "Coverage"], zebra=True,
                         empty="No coverage recorded."))
        rec = est.get("recommendation")
        if rec:
            st.append(para("Consensus recommendation: <b>%s</b> (weighted "
                           "score %.2f on the 1&ndash;5 scale, as of %s), "
                           "from %s /stock/recommendation."
                           % (_clean(rec.get("band") or ""), rec.get("score"),
                              _clean(rec.get("as_of") or ""),
                              _clean(est.get("provider") or "vendor")),
                           "small", OBSERVED))
        pt = est.get("price_target")
        if pt:
            st.append(para("12-month target: mean $%s (as of %s), from "
                           "/stock/price-target."
                           % (pt.get("mean"), _clean(pt.get("as_of") or "")),
                           "small", OBSERVED))
    else:
        st.append(para("No estimate feed was configured for this run (%s), so "
                       "the fundamental rating and the 12-month target were "
                       "withheld rather than invented."
                       % _clean(est.get("reason") or "no key present"),
                       "small"))

    # 3. Valuation method
    val = view.get("valuation") or {}
    ev = val.get("enterprise_value") or {}
    st.append(sec("Valuation method"))
    if ev.get("available"):
        st.append(para("Enterprise value = market cap $%s + total debt $%s "
                       "&minus; cash $%s = $%s. EV / revenue uses the latest "
                       "quarter annualised (run-rate), and FCF yield uses "
                       "annualised quarterly free cash flow over market cap; "
                       "both are labelled run-rate, not trailing-twelve-month."
                       % (_money(ev.get("market_cap")), _money(ev.get("debt")),
                          _money(ev.get("cash")), _money(ev.get("value"))),
                       "small", DERIVED))
    st.append(para("A historical MULTIPLE band and forward bull/base/bear "
                   "prices are deliberately NOT produced: the first needs a "
                   "point-in-time historical EPS series and the second needs "
                   "issuer guidance, neither available on this tier. Dividing "
                   "the 52-week PRICE range by current EPS &mdash; which the "
                   "prior version did &mdash; is circular (the implied prices "
                   "are just the price range) and has been removed.", "small"))
    if rs.fv((snap.get("fundamentals") or {}).get("free_cash_flow")) \
            is not None:
        st.append(para("Free cash flow = operating cash flow &minus; capital "
                       "expenditure (PP&amp;E purchases), both taken from the "
                       "latest filed cash-flow statement.", "small", DERIVED))

    # 4. Source inventory
    cov = (snap.get("evidence") or {}).get("coverage") or {}
    st.append(sec("Source inventory"))
    st.append(_table([[str(k), _clean(str(v))] for k, v in cov.items()],
                     [BODY_W * 0.24, BODY_W * 0.68],
                     header=["Source", "Note"], zebra=True,
                     empty="This snapshot carries no source-coverage "
                           "inventory."))

    # 5. Derived figures and their formulas
    calc = []
    for domain in ("levels", "fundamentals", "valuation"):
        for k, f in (snap.get(domain) or {}).items():
            if isinstance(f, dict) and f.get("calc_version"):
                calc.append([k, _clean(f.get("basis") or "—"),
                             ", ".join(f.get("evidence_refs") or []) or "—"])
    st.append(sec("Derived figures and their formulas"))
    st.append(_table(calc, [BODY_W * 0.22, BODY_W * 0.44, BODY_W * 0.26],
                     header=["Figure", "Basis", "Evidence refs"], zebra=True,
                     empty="No figure in this report was derived; every value "
                           "came directly from a source."))

    # 6. Insider transactions
    ins = view.get("insiders") or {}
    st.append(sec("Insider transactions (Form 4)"))
    if ins.get("reading"):
        st.append(para(_clean(ins["reading"]), "small", ins.get("grade")))
    irows = [[_clean(c.get("label") or ""), str(c.get("n")),
              "view-bearing" if c.get("carries_view") else "mechanical"]
             for c in (ins.get("rows") or []) if c.get("n")]
    st.append(_table(irows, [BODY_W * 0.5, BODY_W * 0.16, BODY_W * 0.26],
                     header=["Category", "Count", "Kind"], zebra=True,
                     empty="No Form 4 filings in the window."))

    # 7. Institutional filings
    own = view.get("ownership") or {}
    st.append(sec("Institutional filings (Schedule 13D / 13G)"))
    if own.get("interpretation"):
        st.append(para(_clean(own["interpretation"]), "small",
                       own.get("grade")))
    st.append(_table([[r.get("form") or "—", r.get("filer") or "not parsed",
                       r.get("accepted") or "—",
                       _acc_cell(r.get("accession"), r.get("url"))]
                      for r in (own.get("rows") or [])],
                     [BODY_W * 0.14, BODY_W * 0.26, BODY_W * 0.24,
                      BODY_W * 0.28],
                     header=["Form", "Filer", "Accepted", "Accession"],
                     zebra=True,
                     empty="No 13D/13G filings on record in the window."))

    # 8. Options
    st.append(sec("Options, open interest and implied volatility"))
    st.append(para("unavailable &mdash; no options feed is wired into this "
                   "report; a chain, its open interest and implied volatility "
                   "are not filed facts and were not sourced.", "small"))

    # 9. Rejected and deferred evidence
    if prov and prov.get("news_rejected"):
        st.append(sec("Coverage rejected, with reason"))
        st.append(_table([[_clean(str(r.get("headline") or ""))[:80],
                           _clean(str(r.get("reason") or ""))]
                          for r in prov["news_rejected"][:20]],
                         [BODY_W * 0.5, BODY_W * 0.42],
                         header=["Headline", "Why it was excluded"],
                         zebra=True))
    if prov and prov.get("deferred"):
        st.append(sec("Filing facts deferred by the point-in-time gate"))
        st.append(para("Filed after this report's timestamp, so excluded from "
                       "every figure above.", "small"))
        st.append(_table([[_clean(str(d.get("metric"))),
                           str(d.get("period_end")), str(d.get("form")),
                           str(d.get("accepted"))]
                          for d in prov["deferred"][:20]],
                         [BODY_W * 0.28, BODY_W * 0.2, BODY_W * 0.16,
                          BODY_W * 0.28],
                         header=["Metric", "Period end", "Form", "Accepted"],
                         zebra=True))

    # 11. Sampled social records
    samples = M3.presentable_samples(
        (snap.get("sentiment") or {}).get("sample_records") or [])
    st.append(sec("Sampled message-board records"))
    st.append(para("Raw, unverified, anonymous. Kept out of the report and "
                   "reproduced here only so any sentiment count can be "
                   "checked.", "small"))
    if samples:
        st.append(_table([[(s.get("author_hash") or "")[:10],
                           M3.to_et(s.get("published_at"))[0]
                           or s.get("published_at") or "—",
                           s.get("sentiment") or "unclassified",
                           _clean(safe(str(s.get("excerpt") or "")))[:150]]
                          for s in samples],
                         [BODY_W * 0.14, BODY_W * 0.2, BODY_W * 0.12,
                          BODY_W * 0.46],
                         header=["Author", "Posted", "Class", "Excerpt"],
                         zebra=True))
    else:
        st.append(para("No records were sampled.", "small"))
    return st


def build_appendix(snap, view, out_path=None, estimates=None, prov=None):
    """Render the evidence and methodology appendix to PDF. Separate
    document from the core report, as the spec requires."""
    from report_v3 import _Doc, _finalize
    rs.assert_exportable(snap, allow_demo=True)
    buf = io.BytesIO()
    doc = _Doc(buf, snap, kind="Equity Research v4 — Appendix", legend=False)
    doc.build(_appendix_story(snap, view, estimates, prov))
    data = _finalize(buf.getvalue(), doc)
    if out_path:
        with open(out_path, "wb") as fh:
            fh.write(data)
    return data


def build_core(snap, view, out_path=None, chart_png=None, chart_meta=None):
    """The 6-page core report, or the one-page flash in DATA HOLD.

    chart_png/chart_meta are the page-5 technical chart, built by the
    runner from the raw bar series (which the view does not carry) and
    passed in. Absent, page 5 says the chart was unavailable rather than
    dropping the page."""
    from report_v3 import _Doc, _finalize
    buf = io.BytesIO()
    doc = _Doc(buf, snap, kind="Equity Research v4")
    if view.get("flash"):
        story = _flash_page(snap, view)
    else:
        story = (_page1(snap, view) + [PageBreak()]
                 + _page2(snap, view) + [PageBreak()]
                 + _page3(snap, view) + [PageBreak()])
        # Page 4 (valuation) is included only when at least one honest
        # multiple exists — the spec's 'omit rather than pad' rule. Its
        # absence makes the core five pages, not a near-empty sixth.
        if (view.get("valuation") or {}).get("available"):
            story += _page4(snap, view) + [PageBreak()]
        story += (_page5(snap, view, chart_png, chart_meta) + [PageBreak()]
                  + _page6(snap, view))
    doc.build(story)
    data = _finalize(buf.getvalue(), doc)
    if out_path:
        with open(out_path, "wb") as fh:
            fh.write(data)
    return data
