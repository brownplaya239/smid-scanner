#!/usr/bin/env python3
"""report_v3.py — Stock Research Brief v3.

Four pages, in the order a reader actually needs them:

    1  Decision dashboard      what to do, at what levels, and why
    2  Company and financials  what the business reported
    3  Market, positioning     tape, insiders, filings, options coverage
    4  Catalysts and coverage  what happened, what is next, what is said

The appendix, the evidence bundle and the validation report are separate
artefacts. They are the audit trail, and burying the decision behind
them was the main thing wrong with v2.

What changed from v2, and why:

  * The trigger ladder is sorted by price. v2 printed "upgrade trigger"
    and "downside confirmation" as two flat rows, which silently assumed
    the 20-day average sits below the 50-day. In a downtrend it does not.
  * With no position on the book the exit is a "risk boundary", not an
    "invalidation" — there is nothing to invalidate yet.
  * Timestamps are Eastern for the reader and UTC in the metadata. v2
    showed the reader a bare Z-suffixed UTC string.
  * Body type has a 9pt floor. v2 shipped 7.5pt.
  * Raw message-board excerpts live in the appendix, never in the brief.
  * Every conclusion carries OBSERVED / DERIVED / INFERRED.
"""

import io
import json
import os

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.lib.units import inch
from reportlab.platypus import (BaseDocTemplate, Frame, Image, KeepTogether,
                                PageBreak, PageTemplate, Paragraph, Spacer,
                                Table, TableStyle)

import report_v3_model as M
import report_v3_validate as V
import research_snapshot as rs
from report_v2 import FONT, FONT_B, _clean

# ── palette ─────────────────────────────────────────────────────────────
INK = colors.HexColor("#111418")
MUTED = colors.HexColor("#5b6570")
FAINT = colors.HexColor("#8a939c")
LINE = colors.HexColor("#d7dce1")
BG_SOFT = colors.HexColor("#f4f6f8")
BG_BAND = colors.HexColor("#1f3a5f")
GREEN = colors.HexColor("#1a7f4b")
RED = colors.HexColor("#b3261e")
AMBER = colors.HexColor("#8a6100")
ACCENT = colors.HexColor("#1f3a5f")

MIN_BODY_PT = 9.0
PAGE_W, PAGE_H = LETTER
MARGIN = 0.55 * inch
BODY_W = PAGE_W - 2 * MARGIN
# Frame insets. _avail_height() and _Doc both read these; when the
# two drifted apart the chart was sized into 8pt that did not
# exist and silently pushed the brief to a fifth page.
TOP_PAD = MARGIN + 0.26 * inch
BOT_PAD = MARGIN + 0.56 * inch

GRADE_COLOR = {M.OBSERVED: GREEN, M.DERIVED: ACCENT, M.INFERRED: AMBER}
GRADE_SHORT = {M.OBSERVED: "OBS", M.DERIVED: "DER", M.INFERRED: "INF"}


def _styles():
    """Nothing below 9pt. The floor is the point of this stylesheet: v2's
    7.5pt footnotes were unreadable on paper and on a phone."""
    ss = getSampleStyleSheet()

    def mk(name, size, leading, **kw):
        opts = {"fontName": FONT, "textColor": INK, "alignment": TA_LEFT,
                "spaceAfter": 0, "spaceBefore": 0}
        opts.update(kw)
        return ParagraphStyle(name, parent=ss["BodyText"], fontSize=size,
                              leading=leading, **opts)
    return {
        "ticker": mk("ticker", 25, 27, fontName=FONT_B,
                     textColor=colors.white),
        "tsub": mk("tsub", 9.6, 12, textColor=colors.HexColor("#c7d2de")),
        "bigpx": mk("bigpx", 17, 20, fontName=FONT_B, textColor=colors.white),
        "bigact": mk("bigact", 14, 17, fontName=FONT_B,
                     textColor=colors.white),
        "action": mk("action", 15, 17, fontName=FONT_B),
        "h2": mk("h2", 11, 13.5, fontName=FONT_B, textColor=ACCENT,
                 spaceBefore=8, spaceAfter=3),
        "h3": mk("h3", 9.6, 12, fontName=FONT_B, textColor=INK,
                 spaceBefore=4, spaceAfter=2),
        "body": mk("body", 9.4, 12.4),
        "small": mk("small", 9.0, 11.4, textColor=MUTED),
        "cell": mk("cell", 9.0, 11.2),
        "cellb": mk("cellb", 9.0, 11.2, fontName=FONT_B),
        "num": mk("num", 10.6, 12.6, fontName=FONT_B),
        "lab": mk("lab", 9.0, 10.6, textColor=MUTED),
    }


ST = _styles()


# ── grade tags ──────────────────────────────────────────────────────────

def tag(g):
    """A three-letter provenance stamp. Small, greyed, always present —
    a reader should never have to guess whether a line is a measurement
    or our opinion."""
    if not g:
        return ""
    return ('  <font size="9" color="%s">[%s]</font>'
            % (GRADE_COLOR.get(g, MUTED).hexval(), GRADE_SHORT.get(g, "?")))


def safe(text):
    """Drop characters the embedded font has no glyph for.

    User-generated text — message-board posts, headlines — carries emoji
    and symbols Calibri cannot render. reportlab draws those as .notdef,
    which a PDF reader extracts as a NUL byte and a human sees as a
    hollow box. Asking the font what it can actually draw is exact;
    the codepoint range below is the fallback for core fonts, which
    expose no character map."""
    if not text:
        return ""
    try:
        cmap = pdfmetrics.getFont(FONT).face.charToGlyph
        return "".join(c for c in str(text)
                       if c in "\n " or ord(c) in cmap)
    except Exception:
        return "".join(c for c in str(text)
                       if ord(c) < 0x2500 and not 0xD800 <= ord(c) <= 0xDFFF)


def para(text, style="body", g=None):
    return Paragraph(_clean(safe(text)) + (tag(g) if g else ""), ST[style])


# ── document ────────────────────────────────────────────────────────────

class _Doc(BaseDocTemplate):
    """One frame per page. A frame that overflows raises rather than
    clipping, so 'text ran off the page' cannot ship silently."""

    def __init__(self, buf, snap, kind="Research Brief",
                 legend=True, **kw):
        super().__init__(buf, pagesize=LETTER, leftMargin=MARGIN,
                         rightMargin=MARGIN, topMargin=TOP_PAD,
                         bottomMargin=BOT_PAD, **kw)
        self.snap, self.kind = snap, kind
        self.legend = legend
        self.addPageTemplates([PageTemplate(
            id="std",
            frames=[Frame(self.leftMargin, self.bottomMargin, self.width,
                          self.height, id="body", leftPadding=0,
                          rightPadding=0, topPadding=0, bottomPadding=0)],
            onPage=self._furniture)])

    def _furniture(self, cv, doc):
        s = self.snap
        if rs.is_demo(s):
            cv.saveState()
            cv.setFillColor(colors.Color(0.85, 0.12, 0.12, alpha=0.13))
            cv.setFont(FONT_B, 74)
            cv.translate(PAGE_W / 2.0, PAGE_H / 2.0)
            cv.rotate(35)
            cv.drawCentredString(0, 0, "DEMO DATA")
            cv.setFont(FONT_B, 19)
            cv.drawCentredString(0, -46, "SYNTHETIC — NOT RESEARCH")
            cv.restoreState()
        cv.saveState()
        cv.setFont(FONT_B, 9.0)
        cv.setFillColor(ACCENT)
        cv.drawString(MARGIN, PAGE_H - MARGIN - 2,
                      "%s — %s" % (s.get("ticker", ""), self.kind))
        cv.setFont(FONT, 9.0)
        cv.setFillColor(MUTED)
        et, _ = M.to_et(s.get("report_time"))
        cv.drawRightString(PAGE_W - MARGIN, PAGE_H - MARGIN - 2,
                           "Prepared %s" % (et or "n/a"))
        cv.setStrokeColor(LINE)
        cv.setLineWidth(0.6)
        cv.line(MARGIN, PAGE_H - MARGIN - 9, PAGE_W - MARGIN,
                PAGE_H - MARGIN - 9)
        cv.setFont(FONT, 9.0)
        cv.setFillColor(MUTED)
        # The grade key is a legend, not content. Painting it here rather
        # than pushing it through the flow keeps it off page 2 — it was
        # the one block that kept tipping the brief to five pages.
        if doc.page == 1 and self.legend:
            dec = s.get("decision") or {}
            cv.drawString(MARGIN, MARGIN + 18,
                          "[OBS] cited source   [DER] computed here, formula "
                          "in the appendix   [INF] our reading, not a "
                          "measurement")
            cv.drawString(MARGIN, MARGIN + 7,
                          "Review %s   ·   evidence quality %s, self-assessed "
                          "and not calibrated against outcomes"
                          % (dec.get("review_date") or "not set",
                             (s.get("evidence") or {}).get("evidence_quality")
                             or "not stated"))
        cv.drawString(MARGIN, MARGIN - 6,
                      "Educational research, not investment advice.")
        cv.drawRightString(PAGE_W - MARGIN, MARGIN - 6, "Page %d" % doc.page)
        cv.restoreState()


# ── small builders ──────────────────────────────────────────────────────

def _table(rows, widths, header=None, zebra=False,
           empty="Not available for this report."):
    if not rows:
        return para(empty, "small")
    data = []
    if header:
        data.append([Paragraph("<b>%s</b>" % _clean(safe(h)),
                               ST["cellb"])
                     for h in header])
    for r in rows:
        data.append([c if isinstance(c, (Paragraph, Image))
                     else Paragraph(_clean(safe(c)), ST["cell"])
                     for c in r])
    if not data:
        return para("Not available for this report.", "small")
    t = Table(data, colWidths=widths, hAlign="LEFT")
    st = [("VALIGN", (0, 0), (-1, -1), "TOP"),
          ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE),
          ("TOPPADDING", (0, 0), (-1, -1), 3),
          ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
          ("LEFTPADDING", (0, 0), (-1, -1), 0),
          ("RIGHTPADDING", (0, 0), (-1, -1), 6)]
    if header:
        st.append(("LINEBELOW", (0, 0), (-1, 0), 0.9, ACCENT))
    if zebra:
        for i in range(1 if header else 0, len(data)):
            if i % 2 == (1 if header else 0):
                st.append(("BACKGROUND", (0, i), (-1, i), BG_SOFT))
        st += [("LEFTPADDING", (0, 0), (-1, -1), 4),
               ("RIGHTPADDING", (0, 0), (-1, -1), 4)]
    t.setStyle(TableStyle(st))
    return t


def _header_band(snap, view):
    """The masthead the original one-pager had and v2 dropped: ticker,
    name, sector and the decision, all legible from arm's length."""
    co = snap.get("company") or {}
    dec = snap.get("decision") or {}
    # These arrive as Fact wrappers, not strings. Reading them raw put
    # the whole dict — quality flags, evidence refs and all — into the
    # masthead and blew the band up to three inches.
    name = rs.fv(co.get("name")) or (co.get("overview") or {}).get("name") or ""
    sector = rs.fv(co.get("sector")) or "sector not classified"
    px = view.get("price")
    chg = rs.fv((snap.get("price") or {}).get("change_pct"))
    left = [Paragraph(_clean(view.get("ticker") or ""), ST["ticker"]),
            Paragraph(_clean("%s  ·  %s" % (name, sector))
                      if name else _clean(sector), ST["tsub"])]
    mid = []
    if px is not None:
        mid.append(Paragraph("%.2f" % px, ST["bigpx"]))
        if chg is not None:
            col = "#7fe0a8" if chg >= 0 else "#ff9a91"
            mid.append(Paragraph('<font color="%s">%+.2f%% on the session'
                                 "</font>" % (col, chg), ST["tsub"]))
        mid.append(Paragraph(_clean("as of %s" % (view.get("quote_time_et")
                                                  or "n/a")), ST["tsub"]))
    act = dec.get("action_display") or dec.get("current_action") or "—"
    col = {"BUY": "#7fe0a8", "ACCUMULATE": "#7fe0a8", "HOLD": "#ffd479",
           "WAIT": "#ffd479", "AVOID": "#ff9a91",
           "REDUCE": "#ff9a91"}.get(str(act).upper(), "#ffffff")
    right = [Paragraph('<font color="%s">%s</font>'
                       % (col, _clean(act)), ST["bigact"]),
             Paragraph(_clean("Horizon: %s" % (dec.get("horizon") or "n/a")),
                       ST["tsub"])]
    scope = dec.get("action_scope")
    if scope:
        right.append(Paragraph(_clean(scope), ST["tsub"]))
    t = Table([[left, mid, right]],
              colWidths=[BODY_W * 0.44, BODY_W * 0.26, BODY_W * 0.30],
              hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BG_BAND),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    return t


def _metrics_grid(snap, view):
    """The compact block of numbers the original report opened with. Each
    cell is label over value; absent metrics are simply not drawn."""
    lv = snap.get("levels") or {}
    co = snap.get("company") or {}
    pr = snap.get("price") or {}
    cells = []

    def add(label, value, g):
        if value is not None:
            cells.append((label, value, g))

    add("Last", "%.2f" % view["price"] if view.get("price") else None,
        M.OBSERVED)
    pc = rs.fv(pr.get("prev_close"))
    add("Prior close", "%.2f" % pc if pc is not None else None, M.OBSERVED)
    ch = rs.fv(pr.get("change_pct"))
    add("1-day", "%+.2f%%" % ch if ch is not None else None, M.DERIVED)
    cap = rs.fv(co.get("market_cap"))
    add("Market cap", ("$%.1fB" % (cap / 1e9)) if cap else None, M.DERIVED)
    rsv = rs.fv(lv.get("rs_vs_spy"))
    add("RS vs SPY (12w)", "%+.1f%%" % rsv if rsv is not None else None,
        M.DERIVED)
    rv = rs.fv(lv.get("rel_volume"))
    add("Volume vs 20d", "%.2fx" % rv if rv is not None else None, M.DERIVED)
    atr = rs.fv(lv.get("atr14"))
    add("ATR(14)", "%.2f" % atr if atr is not None else None, M.DERIVED)
    pe = rs.fv((snap.get("valuation") or {}).get("pe_trailing"))
    add("P/E trailing", "%.1fx" % pe if pe is not None else None, M.DERIVED)

    if not cells:
        return para("No quote metrics were admitted for this report.",
                    "small")
    rows, per = [], 4
    for i in range(0, len(cells), per):
        chunk = cells[i:i + per]
        rows.append([Paragraph(_clean(c[0]), ST["lab"]) for c in chunk]
                    + [""] * (per - len(chunk)))
        rows.append([Paragraph(_clean(c[1]) + tag(c[2]), ST["num"])
                     for c in chunk] + [""] * (per - len(chunk)))
    t = Table(rows, colWidths=[BODY_W / per] * per, hAlign="LEFT")
    st = [("VALIGN", (0, 0), (-1, -1), "TOP"),
          ("LEFTPADDING", (0, 0), (-1, -1), 0),
          ("TOPPADDING", (0, 0), (-1, -1), 1),
          ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]
    for i in range(1, len(rows), 2):
        st.append(("BOTTOMPADDING", (0, i), (-1, i), 6))
    t.setStyle(TableStyle(st))
    return t


# ── page 1 ──────────────────────────────────────────────────────────────

def _page1(snap, view, chart_png=None):
    dec = snap.get("decision") or {}
    st = [_header_band(snap, view), Spacer(1, 7), _metrics_grid(snap, view)]

    st += [para("What changed since the previous report", "h2")]
    ch = view["changed"]
    if ch.get("items"):
        st.append(para("Compared with the brief of %s:"
                       % (ch.get("since") or "the prior run"), "small"))
        for it in ch["items"][:4]:
            st.append(para("• " + it["text"], "body", it["grade"]))
    else:
        st.append(para(ch.get("note") or "", "body", ch.get("grade")))

    st += [para("Where price stands", "h2")]
    if view.get("technical_state"):
        st.append(para(view["technical_state"], "body", M.DERIVED))
    else:
        st.append(para("No moving-average series was admitted, so the trend "
                       "state cannot be stated.", "body", M.OBSERVED))

    # the ladder — ordered by price, always
    st += [para("Trigger ladder", "h2"),
           para("Sorted by price, nearest first — not by convention.",
                "small")]
    rows = []
    for s in view["recovery"][:3]:
        rows.append([s["label"], "%.2f" % s["value"],
                     "%+.1f%%" % s["distance_pct"],
                     "Reclaims %s" % s["label"]])
    for s in view["downside"][:2]:
        rows.append([s["label"], "%.2f" % s["value"],
                     "%+.1f%%" % s["distance_pct"],
                     "Loses %s" % s["label"]])
    if rows:
        st.append(_table(rows, [BODY_W * .30, BODY_W * .14, BODY_W * .14,
                                BODY_W * .42],
                         header=["Level", "Price", "Distance", "If reached"],
                         zebra=True))
    else:
        st.append(para("No level series was admitted, so no ladder can be "
                       "built.", "small"))

    ex = view["exit"]
    if ex.get("value") is not None:
        st += [para(ex["label"], "h2"),
               para("%.2f — %s. That is %.1f x ATR(14) below spot, against "
                    "a %.1f x floor for this holding period (%s)."
                    % (ex["value"], ex["basis"], ex.get("atr_multiple") or 0,
                       ex.get("floor") or 0,
                       ex.get("horizon") or "horizon not stated"),
                    "body", M.DERIVED)]
        if not ex["active_entry"]:
            st.append(para("No entry is on the book: a boundary for the "
                           "read, not a stop for a position.", "small"))

    facts = (dec.get("supporting_facts") or [])[:3]
    risks = (dec.get("risks") or [])[:3]
    if facts or risks:
        lft = [para("Thesis", "h3")] + [
            para("• " + str(f.get("text") if isinstance(f, dict) else f),
                 "body") for f in facts]
        rgt = [para("What would break it", "h3")] + [
            para("• " + str(r.get("text") if isinstance(r, dict) else r),
                 "body") for r in risks]
        t = Table([[lft, rgt]], colWidths=[BODY_W * .49, BODY_W * .51],
                  hAlign="LEFT")
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LEFTPADDING", (0, 0), (0, 0), 0),
                               ("LEFTPADDING", (1, 0), (1, 0), 8)]))
        st += [Spacer(1, 5), t]

    # Content length varies by ticker, so the chart takes whatever space
    # is genuinely left rather than a height picked from one fixture. A
    # fixed height fits ISRG and pushes the next name onto a fifth page.
    if chart_png:
        st = _fit_chart(st, chart_png)

    return st


def _avail_height():
    return PAGE_H - TOP_PAD - BOT_PAD


def _story_height(story):
    used = 0.0
    for f in story:
        try:
            used += f.wrap(BODY_W, PAGE_H)[1]
        except Exception:
            used += 0.0
        stl = getattr(f, "style", None)
        if stl is not None:
            used += getattr(stl, "spaceBefore", 0) or 0
            used += getattr(stl, "spaceAfter", 0) or 0
    return used


def _fit_chart(story, png, floor=0.75 * inch):
    """Measure what page 1 already uses and give the chart the remainder.
    Below `floor` a chart is too squashed to read, so it is dropped with
    a line saying where the full-size one is rather than shipped as an
    unreadable band."""
    # wrap() reports the ink box only; the leading a style asks for
    # around itself is real vertical space and has to be counted, or the
    # chart is sized into room that does not exist.
    left = _avail_height() - _story_height(story) - 8
    if left < floor:
        return story + [Spacer(1, 4),
                        para("Price chart is on page 3 at full width.",
                             "small")]
    return story + [Spacer(1, 5), _image(png, BODY_W, left)]


def _image(png, w, max_h):
    try:
        from reportlab.lib.utils import ImageReader
        iw, ih = ImageReader(io.BytesIO(png)).getSize()
        h = min(max_h, w * ih / float(iw))
        return Image(io.BytesIO(png), width=h * iw / float(ih), height=h)
    except Exception:
        return para("Chart could not be rendered.", "small")


# Snapshots disagree about scale: the live XBRL path writes absolute
# dollars with unit="USD", while older fixtures pre-scale to billions and
# leave unit empty. Dividing both by 1e9 printed "$0.00B" for a company
# holding $8.42bn in cash, so the unit is honoured where it exists and
# the magnitude only decides the suffix.
def _money(f, v):
    unit = (f.get("unit") if isinstance(f, dict) else None) or ""
    if unit.upper() == "USD" or abs(v) >= 1e6:
        if abs(v) >= 1e9:
            return "$%.2fB" % (v / 1e9)
        if abs(v) >= 1e6:
            return "$%.0fM" % (v / 1e6)
        return "$%.0f" % v
    return "$%.2fB" % v          # already scaled by the producer


FUND_LABEL = {
    "revenue_q": ("Revenue, latest quarter", "money"),
    "revenue_growth": ("Revenue growth y/y", "pct"),
    "gross_profit": ("Gross profit", "money"),
    "gross_margin": ("Gross margin (GAAP)", "pct"),
    "net_income_q": ("Net income", "money"),
    "net_margin": ("Net margin (GAAP)", "pct"),
    "operating_cash_flow": ("Operating cash flow", "money"),
    "eps_ttm": ("Diluted EPS (TTM)", "usd"),
    "eps_growth": ("EPS growth y/y", "pct"),
    "cash": ("Cash and equivalents", "money"),
    "debt": ("Long-term debt", "money"),
    "recurring_mix": ("Recurring revenue mix", "pct"),
    "procedure_growth": ("Procedure growth y/y", "pct"),
    "installed_base": ("Installed base", "count"),
}


def _cov_row(c):
    """The coverage inventory is written by several call sites and comes
    back as a 3-tuple, a dict, or a bare sentence. Render whichever
    arrived rather than assuming one and crashing the appendix on live
    data, which is exactly what happened the first time this ran."""
    if isinstance(c, (list, tuple)):
        vals = [str(x) for x in c] + ["", ""]
        return vals[:3]
    if isinstance(c, dict):
        return [str(c.get("source") or c.get("name") or "-"),
                str(c.get("state") or c.get("status") or "-"),
                str(c.get("reason") or c.get("effect") or "-")]
    return [str(c), "-", "-"]


def _src_of(f):
    """A snapshot field may be a Fact wrapper or a bare string."""
    return f.get("src") if isinstance(f, dict) else None


def _fund_rows(fu):
    """Render every fundamental the snapshot actually carries, in a fixed
    order, then anything else it has. A hardcoded list silently dropped
    four populated fields when the snapshot shape differed."""
    order = [k for k in FUND_LABEL if k in (fu or {})]
    order += [k for k in (fu or {}) if k not in FUND_LABEL]
    rows = []
    for key in order:
        f = fu.get(key)
        v = rs.fv(f)
        if v is None or isinstance(v, (str, bool)):
            continue
        label, kind = FUND_LABEL.get(
            key, (key.replace("_", " ").capitalize(), "raw"))
        if kind == "money":
            shown = _money(f, v)
        elif kind == "pct":
            shown = "%+.1f%%" % v if "growth" in key else "%.1f%%" % v
        elif kind == "usd":
            shown = "$%.2f" % v
        elif kind == "count":
            shown = "{:,}".format(int(v))
        else:
            shown = ("%,.0f" % v).replace(",", ",") if abs(v) > 999                 else "%.2f" % v
        rows.append([label,
                     Paragraph(_clean(shown) + tag(M.grade(f)), ST["cellb"]),
                     (f.get("period_end") or "—") if isinstance(f, dict)
                     else "—",
                     (f.get("src") or "—") if isinstance(f, dict) else "—"])
    return rows


# ── page 2 ──────────────────────────────────────────────────────────────

def _page2(snap, view):
    co = snap.get("company") or {}
    ov = co.get("overview") or {}
    fu = snap.get("fundamentals") or {}
    val = snap.get("valuation") or {}
    st = [para("Company and financial overview", "h2")]

    txt = ov.get("text") or rs.fv(co.get("business_2s"))
    if txt:
        st += [para(str(txt)[:700], "body", M.OBSERVED),
               para("Source: %s"
                    % (ov.get("source") or _src_of(co.get("business_2s"))
                       or "issuer profile"), "small")]
    else:
        st.append(para("No business description was admitted from a cited "
                       "source. We do not paraphrase one.", "body",
                       M.OBSERVED))
    bits = []
    if ov.get("industry"):
        bits.append("Industry: %s" % ov["industry"])
    if ov.get("employees"):
        bits.append("Employees: %s" % ov["employees"])
    if bits:
        st.append(para("  ·  ".join(bits), "small"))

    # Company-level facts belong on the company page. v2 scattered these
    # across the header and left page 2 half empty underneath.
    crows = []
    for key, label, kind in (("shares_outstanding", "Shares outstanding",
                              "count"),
                             ("market_cap", "Market capitalisation", "money"),
                             ("universe", "Size classification", "text"),
                             ("profitable", "Profitable, latest quarter",
                              "bool"),
                             ("sector", "Sector", "text")):
        f = co.get(key)
        v = rs.fv(f)
        if v is None:
            continue
        if kind == "money":
            shown = _money(f, float(v))
        elif kind == "count":
            shown = "{:,}".format(int(v))
        elif kind == "bool":
            shown = "Yes" if v else "No"
        else:
            shown = str(v)
        crows.append([label, Paragraph(_clean(shown) + tag(M.grade(f)),
                                       ST["cellb"]),
                      _src_of(f) or "—"])
    if crows:
        st += [para("Company facts", "h2"),
               _table(crows, [BODY_W * .32, BODY_W * .22, BODY_W * .46],
                      header=["Fact", "Value", "Source"], zebra=True)]

    st += [para("Reported results", "h2"),
           para("Every figure below is a GAAP tag from an SEC filing "
                "accepted before this report's timestamp. Nothing is "
                "estimated or annualised.", "small")]
    rows = _fund_rows(fu)
    if rows:
        st.append(_table(rows, [BODY_W * .30, BODY_W * .18, BODY_W * .16,
                                BODY_W * .36],
                         header=["Metric", "Reported", "Period end",
                                 "Source"], zebra=True))
    else:
        st.append(para("No filing figure cleared the point-in-time gate for "
                       "this report.", "body", M.OBSERVED))

    if val:
        st += [para("Valuation", "h2")]
        vr = []
        for key, label in (("pe_trailing", "P/E, trailing"),
                           ("pe_forward", "P/E, forward")):
            f = val.get(key)
            v = rs.fv(f)
            if v is None:
                continue
            vr.append([label, "%.1fx" % v,
                       (f.get("basis") or "—") if isinstance(f, dict)
                       else "—",
                       (f.get("note") or f.get("src") or "—")
                       if isinstance(f, dict) else "—"])
        st.append(_table(vr, [BODY_W * .20, BODY_W * .12, BODY_W * .26,
                              BODY_W * .42],
                         header=["Multiple", "Value", "Basis",
                                 "Operands and caveat"], zebra=True))

    # What we do not have, stated as a coverage gap rather than implied
    gaps = []
    if not fu.get("gross_margin"):
        gaps.append("gross margin (no GrossProfit tag filed)")
    gaps.append("non-GAAP margins — companies publish these in press-release "
                "exhibits, which are not XBRL-tagged, so there is no figure "
                "to admit")
    gaps.append("revenue by end market or geography — segment detail is not "
                "exposed by the company-concept API this report reads")
    gaps.append("management guidance and analyst estimates — neither is a "
                "filed fact")
    st += [para("Not covered, and why", "h2"),
           para("These are gaps in what we can source. None of them is a "
                "negative finding about the business.", "small")]
    for g in gaps:
        st.append(para("• " + g, "body", M.OBSERVED))
    return st


# ── page 3 ──────────────────────────────────────────────────────────────

def _page3(snap, view, chart_png=None):
    st = []
    # Page 1 already lists every level with its distance. Repeating the
    # table here bought nothing and squeezed the chart — the one thing
    # this page exists for — down to an unreadable thumbnail. Only the
    # levels page 1 does not reach get a line.
    extra = [r for r in (view.get("ladder") or [])
             if r["key"] in ("resistance", "resistance_major")]
    if extra:
        st += [para("Overhead levels not on page 1: "
                    + "; ".join("%s at %.2f (%+.1f%%)"
                                % (r["label"], r["value"],
                                   r.get("distance_pct") or 0.0)
                                for r in extra), "small")]

    ins = view["insiders"]
    st += [para("Insider transactions", "h2")]
    if ins.get("rows"):
        st.append(para("Filed Form 4 activity over the last %s days, split by "
                       "what the transaction actually was. Compensation "
                       "mechanics and open-market decisions are counted "
                       "separately because they carry different information."
                       % (ins.get("window_days") or "180"), "small"))
        st.append(_table([[r["label"], str(r["n"]),
                           "carries a view" if r["carries_view"]
                           else "mechanical"] for r in ins["rows"]],
                         [BODY_W * .50, BODY_W * .14, BODY_W * .36],
                         header=["Transaction type", "Count", "Reading"],
                         zebra=True))
        if ins.get("plan_status"):
            st.append(para("10b5-1 status: %s" % ins["plan_status"],
                           "small"))
    else:
        st.append(para("No Form 4 transaction was parsed in the window.",
                       "body", M.OBSERVED))

    own = view["ownership"]
    st += [para("Schedule 13D / 13G filings", "h2")]
    if own.get("rows"):
        st.append(_table([[r["form"] or "—", r["filer"] or "not parsed",
                           r["accepted"] or "—", r["accession"] or "—"]
                          for r in own["rows"][:6]],
                         [BODY_W * .14, BODY_W * .30, BODY_W * .26,
                          BODY_W * .30],
                         header=["Form", "Filer", "Accepted", "Accession"],
                         zebra=True))
    if own.get("interpretation"):
        st.append(para(own["interpretation"], "body", M.OBSERVED))
    ip = own.get("institutional_pct")
    st.append(para("Institutional ownership percentage: %s"
                   % ("%.1f%%" % ip if ip is not None else
                      "not aggregated by this report; 13F holdings are not "
                      "collected."), "small"))

    op = view["options"]
    st += [para("Options", "h2"),
           para(op.get("note") or "Expected move %s" % op.get("expected_move"),
                "body", op.get("grade"))]

    head = [para("Market, positioning and flow", "h2")]
    if not chart_png:
        return head + [para("Price chart unavailable: no bar series was "
                            "passed to the renderer for this run.", "body",
                            M.OBSERVED)] + st
    # The chart is the centrepiece of this page, but the sections below it
    # are the evidence. Sizing the chart from the space the text actually
    # leaves keeps both on one page for any ticker.
    room = _avail_height() - _story_height(head + st) - 22
    cap = para("Close with 20, 50 and 200-day averages, session volume, and "
               "relative strength against SPY where the benchmark series was "
               "retained.", "small")
    room -= _story_height([cap])
    if room < 2.0 * inch:
        return head + st + [Spacer(1, 5),
                            para("Chart omitted: this name's evidence "
                                 "sections fill the page, and a chart "
                                 "smaller than two inches is not readable.",
                                 "small")]
    return head + [_image(chart_png, BODY_W, min(3.5 * inch, room)), cap] + st


# ── page 4 ──────────────────────────────────────────────────────────────

def _page4(snap, view):
    cat = view["catalysts"]
    st = [para("Catalysts, news and alternative data", "h2")]

    rows = []
    last = cat.get("last_reported")
    if last:
        rows.append(["Last reported", "%s — %s" % (last["when"],
                                                   last.get("what") or ""),
                     last["confirmation"], GRADE_SHORT[last["grade"]]])
    drv = cat.get("current_driver") or {}
    if drv.get("text"):
        rows.append(["Current driver", drv["text"], "—",
                     GRADE_SHORT.get(drv.get("grade"), "INF")])
    for n in cat.get("next") or []:
        rows.append(["Next", "%s — %s" % (n["when"], n.get("what") or ""),
                     n["confirmation"], GRADE_SHORT[n["grade"]]])
    if not any(r[0] == "Next" for r in rows):
        rows.append(["Next", "No scheduled event is confirmed or estimated "
                             "for this name.", "—", "OBS"])
    st.append(_table(rows, [BODY_W * .16, BODY_W * .46, BODY_W * .28,
                            BODY_W * .10],
                     header=["Stage", "What", "Date confirmation", "Grade"],
                     zebra=True))

    news = ((snap.get("sentiment") or {}).get("news") or [])[:5]
    st += [para("Coverage", "h2")]
    if news:
        nr = []
        for n in news:
            et, _ = M.to_et(n.get("published_at"))
            nr.append([_clean(str(n.get("headline") or "")[:110]),
                       "%s · %s" % (n.get("publisher") or "unattributed",
                                    et or "undated")])
        st.append(_table(nr, [BODY_W * .66, BODY_W * .34],
                         header=["Headline", "Publisher and time"],
                         zebra=True))
        st.append(para("Each item was fetched and checked for whether it "
                       "actually discusses the company; items that failed "
                       "that check are listed in the appendix with the "
                       "reason.", "small"))
    else:
        st.append(para("No article cleared the relevance check in this "
                       "window.", "body", M.OBSERVED))

    soc = view["social"]
    st += [para("Retail message-board activity", "h2")]
    considered, counted = soc.get("n_considered"), soc.get("n_counted")
    if counted:
        st.append(para("%s posts considered, %s counted, %s rejected, from "
                       "%s distinct authors. Classification: %s. Reliability: "
                       "%s."
                       % (considered, counted, soc.get("n_rejected"),
                          soc.get("unique_authors"),
                          soc.get("classification") or "not classified",
                          soc.get("reliability") or "not assessed"),
                       "body", M.DERIVED))
        by = soc.get("by_class") or {}
        if by:
            st.append(_table(
                [[k.title(), str(v)] for k, v in by.items() if v],
                [BODY_W * .30, BODY_W * .16],
                header=["Direction", "Posts"], zebra=True))
        co = soc.get("coordination") or {}
        if co.get("phrase_groups"):
            # Only state the operands this block actually carries. v2
            # printed "5 across None posts (None of 58)" beside a
            # sentence asserting 100% of the sample — a number the data
            # never contained.
            parts = ["%s repeated-phrase group%s"
                     % (co["phrase_groups"],
                        "" if co["phrase_groups"] == 1 else "s")]
            if co.get("posts_affected") is not None:
                parts.append("across %s posts" % co["posts_affected"])
            if co.get("pct") is not None:
                parts.append("%.0f%% of those counted" % float(co["pct"]))
            tail = ("  Threshold: %s." % co["threshold"]
                    if co.get("threshold") is not None else
                    "  No coordination threshold is recorded for this "
                    "sample, so the grouping is reported without a pass "
                    "or fail.")
            st.append(para(", ".join(parts) + "." + tail, "body", M.DERIVED))
        else:
            st.append(para("No repeated-phrase group met the coordination "
                           "threshold, so no coordination assessment is "
                           "made.", "body", M.OBSERVED))
    else:
        st.append(para("Too few qualifying posts to summarise. This says "
                       "nothing about the stock.", "body", M.OBSERVED))
    st.append(para("Individual posts are anonymous, unverified and carry no "
                   "accountability. They are summarised here as counts only; "
                   "the sampled records themselves are in the appendix.",
                   "small"))
    return st


# ── build ───────────────────────────────────────────────────────────────

def build_core(snap, view=None, chart_png=None, chart_full=None,
               out_path=None, allow_demo=False):
    """The four-page brief. Nothing else goes in this file."""
    rs.assert_exportable(snap, allow_demo=allow_demo)
    view = view or M.build(snap)
    buf = io.BytesIO()
    doc = _Doc(buf, snap, kind="Stock Research Brief v3")
    story = (_page1(snap, view, chart_png) + [PageBreak()]
             + _page2(snap, view) + [PageBreak()]
             + _page3(snap, view, chart_full) + [PageBreak()]
             + _page4(snap, view))
    doc.build(story)
    data = buf.getvalue()
    if out_path:
        with open(out_path, "wb") as fh:
            fh.write(data)
    return data


def build_appendix(snap, view=None, recs=None, prov=None, out_path=None,
                   allow_demo=False):
    """Everything the brief cites but does not print: the raw social
    sample, rejected news with reasons, deferred filing facts, the source
    inventory and the formula list."""
    rs.assert_exportable(snap, allow_demo=allow_demo)
    view = view or M.build(snap)
    buf = io.BytesIO()
    doc = _Doc(buf, snap, kind="Research Brief v3 — Appendix",
               legend=False)
    st = [para("Appendix", "h2"),
          para("This document is the audit trail for the four-page brief "
               "prepared %s. It is not a summary and is not meant to be read "
               "front to back." % (view.get("report_time_et") or ""),
               "small")]

    st += [para("A. Source inventory", "h2")]
    cov = (snap.get("evidence") or {}).get("coverage") or []
    # Coverage is written as {source: note}. Iterating it as a list gave
    # the keys and silently threw away every note, which is the only part
    # of the section a reader actually needs.
    rows = ([[str(k), "covered" if v else "not covered", str(v or "-")]
             for k, v in cov.items()] if isinstance(cov, dict)
            else [_cov_row(c) for c in cov])
    if True:
        st.append(_table(rows,
                         [BODY_W * .28, BODY_W * .16, BODY_W * .56],
                         header=["Source", "State", "Note"], zebra=True,
                         empty="This snapshot carries no source-coverage "
                               "inventory."))

    st += [para("B. Derived figures and their formulas", "h2")]
    calc = []
    for domain in ("levels", "fundamentals", "valuation"):
        for k, f in (snap.get(domain) or {}).items():
            if isinstance(f, dict) and f.get("calc_version"):
                calc.append([k, f.get("calc_version") or "—",
                             f.get("basis") or "—",
                             ", ".join(f.get("evidence_refs") or []) or "—"])
    st.append(_table(calc, [BODY_W * .22, BODY_W * .18, BODY_W * .30,
                            BODY_W * .30],
                     header=["Figure", "Version", "Basis", "Evidence refs"],
                     zebra=True,
                     empty="No figure in this brief was derived; every value "
                           "shown came directly from a source."))

    st += [para("C. Sampled message-board records", "h2"),
           para("Raw, unverified, anonymous. Kept out of the brief on "
                "purpose and reproduced here only so the counts on page 4 "
                "can be checked.", "small")]
    samples = ((snap.get("sentiment") or {}).get("sample_records") or [])
    if samples:
        st.append(_table([[(s.get("author_hash") or "")[:10],
                           M.to_et(s.get("published_at"))[0]
                           or s.get("published_at") or "—",
                           s.get("sentiment") or "unclassified",
                           _clean(safe(str(s.get("excerpt") or "")))[:150]]
                          for s in samples],
                         [BODY_W * .14, BODY_W * .20, BODY_W * .12,
                          BODY_W * .54],
                         header=["Author", "Posted", "Class", "Excerpt"],
                         zebra=True))
    else:
        st.append(para("No records were sampled.", "small"))

    if prov and prov.get("news_rejected"):
        st += [para("D. Coverage rejected, with reason", "h2")]
        st.append(_table([[_clean(str(r.get("headline") or ""))[:80],
                           str(r.get("reason") or "")]
                          for r in prov["news_rejected"][:20]],
                         [BODY_W * .50, BODY_W * .50],
                         header=["Headline", "Why it was excluded"],
                         zebra=True))
    if prov and prov.get("deferred"):
        st += [para("E. Filing facts deferred by the point-in-time gate",
                    "h2"),
               para("Filed after this report's timestamp, so excluded from "
                    "every figure above.", "small")]
        st.append(_table([[str(d.get("metric")), str(d.get("period_end")),
                           str(d.get("form")), str(d.get("accepted"))]
                          for d in prov["deferred"][:20]],
                         [BODY_W * .28, BODY_W * .20, BODY_W * .16,
                          BODY_W * .36],
                         header=["Metric", "Period end", "Form", "Accepted"],
                         zebra=True))
    doc.build(st)
    data = buf.getvalue()
    if out_path:
        with open(out_path, "wb") as fh:
            fh.write(data)
    return data


def build_all(snap, out_dir=".", chart_png=None, chart_full=None, recs=None,
              prov=None, allow_demo=False, stem=None):
    """Produce the four artefacts and validate them. Returns a manifest."""
    view = M.build(snap)
    tk = snap.get("ticker") or "TICKER"
    stem = stem or "%s_research_brief_v3" % tk
    core_p = os.path.join(out_dir, stem + ".pdf")
    apx_p = os.path.join(out_dir, stem + "_appendix.pdf")
    ev_p = os.path.join(out_dir, stem + "_evidence.json")
    val_p = os.path.join(out_dir, stem + "_validation.json")

    core = build_core(snap, view, chart_png, chart_full, core_p, allow_demo)
    apx = build_appendix(snap, view, recs, prov, apx_p, allow_demo)

    evidence = {"schema": "stock_research_brief_evidence/v3",
                "ticker": tk,
                "report_time_utc": view["report_time_utc"],
                "market_data_time_utc": view["quote_time_utc"],
                "display_timezone": M.ET,
                "grades": M.GRADE_NOTE,
                "view": _jsonable(view),
                "snapshot_schema": snap.get("schema"),
                "evidence_index": snap.get("evidence_index"),
                "count_statements": snap.get("count_statements")}
    with open(ev_p, "w", encoding="utf-8") as fh:
        json.dump(evidence, fh, indent=1, default=str)

    val = V.report(view, snap, core, apx)
    with open(val_p, "w", encoding="utf-8") as fh:
        json.dump(val, fh, indent=1, default=str)
    if val["ok"]:
        M.save_state(snap)
    return {"core": core_p, "appendix": apx_p, "evidence": ev_p,
            "validation": val_p, "ok": val["ok"], "problems": val["problems"],
            "core_bytes": len(core), "appendix_bytes": len(apx)}


def _jsonable(o):
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (str, int, float, bool)) or o is None:
        return o
    return str(o)
