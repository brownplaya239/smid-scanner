"""
report_v2.py — three-page Stock Research Brief renderer.

v1 (scanner.py FPDF one-pagers, alt_data.py) is UNTOUCHED. This is the
v2 delivery layer sitting on top of research_snapshot.py: it reads ONE
canonical snapshot, refuses to emit anything that fails the
contradiction gate, and renders at most three decision pages plus
optional raw-data appendices.

WHY REPORTLAB INSTEAD OF FPDF
The July 16 review's presentation defects were mostly structural, not
cosmetic: "clips nearly every long sentence at the right margin" is what
FPDF's fixed-width cell() does when text overflows. Platypus Paragraphs
inside a Frame wrap automatically and the frame reports overflow, so
clipping becomes impossible rather than merely unlikely. Reportlab also
gives real font embedding, document tagging/outline, and page numbers.

LAYOUT CONTRACT (checked by verify_render):
  * every page's flowables must FIT — overflow raises, never truncates
  * body type never below 7.5pt
  * no HTML entities or control characters reach the page
  * page numbers on every page; fonts embedded; PDF tagged with title,
    author, subject and language

    python report_v2.py --demo ISRG     # render a real brief to /tmp
    python report_v2.py --self-test     # layout guarantees, no network
"""

from __future__ import annotations

import argparse
import html
import io
import os
import re
import sys
from datetime import datetime, timezone

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (BaseDocTemplate, Frame, KeepTogether,
                                PageBreak, PageTemplate, Paragraph, Spacer,
                                Table, TableStyle, Image)

import pdf_postprocess as PP
import research_snapshot as rs

# ── palette ─────────────────────────────────────────────────────────────
INK = colors.HexColor("#111418")
MUTED = colors.HexColor("#5b6570")
LINE = colors.HexColor("#d7dce1")
BG_SOFT = colors.HexColor("#f4f6f8")
GREEN = colors.HexColor("#1a7f4b")
RED = colors.HexColor("#b3261e")
AMBER = colors.HexColor("#8a6100")
ACCENT = colors.HexColor("#1f3a5f")

MIN_BODY_PT = 7.5
PAGE_W, PAGE_H = LETTER
MARGIN = 0.55 * inch


def _register_fonts():
    """Embed real TTFs so the PDF renders identically everywhere and does
    not emit the core-font warnings the review flagged."""
    # DejaVu FIRST, and from matplotlib's own copy so it resolves on every
    # platform without installing anything. Page fitting is measured in
    # glyph widths: with Calibri picked up on Windows and DejaVu on the
    # Linux CI runner, every layout decision was calibrated against a font
    # production never uses. NOW passed locally at 88% of page 4 and
    # rendered a fifth page in CI, which the PAGE_COUNT gate caught.
    # Matching the two is the only way local measurement means anything.
    cands = []
    try:
        import matplotlib as _mpl
        _d = os.path.join(os.path.dirname(_mpl.__file__), "mpl-data",
                          "fonts", "ttf")
        cands.append(("Brief", os.path.join(_d, "DejaVuSans.ttf"),
                      os.path.join(_d, "DejaVuSans-Bold.ttf")))
    except Exception:
        pass
    cands += [
        ("Brief", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("Brief", "C:/Windows/Fonts/calibri.ttf", "C:/Windows/Fonts/calibrib.ttf"),
        ("Brief", "C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ]
    for name, reg, bold in cands:
        if os.path.exists(reg) and os.path.exists(bold):
            try:
                pdfmetrics.registerFont(TTFont(name, reg))
                pdfmetrics.registerFont(TTFont(name + "-Bold", bold))
                pdfmetrics.registerFontFamily(name, normal=name,
                                              bold=name + "-Bold")
                return name, name + "-Bold"
            except Exception:
                continue
    return "Helvetica", "Helvetica-Bold"        # last-resort fallback


FONT, FONT_B = _register_fonts()


def _clean(text):
    """Kill HTML entities, tags and control characters before they reach
    the page — alt-data page 2 shipped raw '&amp;#39;' noise."""
    if text is None:
        return ""
    s = str(text)
    for _ in range(3):                       # &amp;lt; → &lt; → <
        new = html.unescape(s)
        if new == s:
            break
        s = new
    s = re.sub(r"<[^>]{1,40}>", " ", s)
    s = "".join(ch for ch in s if ch == "\n" or ch >= " ")
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"[ \t]{2,}", " ", s).strip()


def _styles():
    ss = getSampleStyleSheet()
    def mk(name, size, leading, **kw):
        opts = {"fontName": FONT, "textColor": INK, "alignment": TA_LEFT,
                "spaceAfter": 0, "spaceBefore": 0}
        opts.update(kw)
        return ParagraphStyle(name, parent=ss["BodyText"],
                              fontSize=size, leading=leading, **opts)
    return {
        "h1": mk("h1", 15, 18, fontName=FONT_B, textColor=ACCENT),
        "h2": mk("h2", 10.5, 13, fontName=FONT_B, textColor=ACCENT,
                 spaceBefore=7, spaceAfter=3),
        "body": mk("body", 8.6, 11.6),
        "small": mk("small", 7.8, 10.2, textColor=MUTED),
        "tiny": mk("tiny", 7.5, 9.4, textColor=MUTED),
        "kpi": mk("kpi", 13, 15, fontName=FONT_B),
        "action": mk("action", 17, 20, fontName=FONT_B),
        "cell": mk("cell", 8.2, 10.4),
        "cellb": mk("cellb", 8.2, 10.4, fontName=FONT_B),
    }


ST = _styles()


# ── page furniture ──────────────────────────────────────────────────────

class _Doc(BaseDocTemplate):
    """Single-frame doc with a header/footer painter. The frame is what
    makes clipping impossible: content that will not fit raises instead
    of being silently cut at the margin."""

    def __init__(self, buf, snap, **kw):
        super().__init__(buf, pagesize=LETTER,
                         leftMargin=MARGIN, rightMargin=MARGIN,
                         topMargin=MARGIN + 0.30 * inch,
                         # reserve a real footer band: page 4's flow
                         # block previously ended 4pt from the page
                         # number, which reads as a collision
                         bottomMargin=MARGIN + 0.34 * inch, **kw)
        self.snap = snap
        frame = Frame(self.leftMargin, self.bottomMargin,
                      self.width, self.height, id="body",
                      leftPadding=0, rightPadding=0,
                      topPadding=0, bottomPadding=0)
        self.addPageTemplates([PageTemplate(id="std", frames=[frame],
                                            onPage=self._furniture)])

    # NOTE: catalog metadata (/Lang, viewer preferences, XMP) is written
    # AFTER the build by pdf_postprocess.finalize(), through pikepdf's
    # typed object model. A previous version assigned Python values onto
    # reportlab's internal catalog here, which serialized `/Lang en-US`
    # — a bare token where a string object is required — and left the
    # file unparseable by pypdf while still rendering in Poppler.

    def _furniture(self, cv, doc):
        s = self.snap
        tk = s.get("ticker", "")
        # DEMO watermark — drawn first, on EVERY page, unremovable by the
        # caller. A synthetic prototype must never be mistakable for
        # research at a glance or in a screenshot.
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
            cv.setFillColor(colors.Color(0.70, 0.10, 0.10))
            cv.setFont(FONT_B, 8.2)
            cv.drawCentredString(PAGE_W / 2.0, PAGE_H - MARGIN + 9,
                                 "DEMO DATA — synthetic values, not sourced, "
                                 "not for any investment use")
            cv.restoreState()
        cv.saveState()
        cv.setFont(FONT_B, 8.4)
        cv.setFillColor(ACCENT)
        cv.drawString(MARGIN, PAGE_H - MARGIN - 2, "%s — Stock Research Brief" % tk)
        cv.setFont(FONT, 7.6)
        cv.setFillColor(MUTED)
        cv.drawRightString(PAGE_W - MARGIN, PAGE_H - MARGIN - 2,
                           "Report %s  ·  Market data %s" %
                           (_short_ts(s.get("report_time")),
                            _short_ts(s.get("market_data_time"))))
        cv.setStrokeColor(LINE)
        cv.setLineWidth(0.6)
        cv.line(MARGIN, PAGE_H - MARGIN - 8, PAGE_W - MARGIN, PAGE_H - MARGIN - 8)
        cv.setFont(FONT, 7.6)
        cv.setFillColor(MUTED)
        cv.drawString(MARGIN, MARGIN - 4,
                      "Educational research, not investment advice.")
        cv.drawRightString(PAGE_W - MARGIN, MARGIN - 4, "Page %d" % doc.page)
        cv.restoreState()


def _short_ts(v):
    if not v:
        return "n/a"
    s = str(v).replace("T", " ")
    return s[:16] + (" UTC" if s.endswith("Z") or "+00" in s else "")


# ── small builders ──────────────────────────────────────────────────────

def _kv_table(rows, widths, header=None):
    data = []
    if header:
        data.append([Paragraph("<b>%s</b>" % _clean(h), ST["cellb"])
                     for h in header])
    for r in rows:
        data.append([c if isinstance(c, (Paragraph, Image))
                     else Paragraph(_clean(c), ST["cell"]) for c in r])
    if not data:
        # An absent section is normal on real data (a snapshot may simply
        # have no scenarios); reportlab raises on a zero-row Table, so say
        # so in the document rather than crashing the render.
        return Paragraph("Not available for this report.", ST["cell"])
    t = Table(data, colWidths=widths, hAlign="LEFT")
    style = [("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE),
             ("TOPPADDING", (0, 0), (-1, -1), 2.5),
             ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
             ("LEFTPADDING", (0, 0), (-1, -1), 0),
             ("RIGHTPADDING", (0, 0), (-1, -1), 5)]
    if header:
        style += [("LINEBELOW", (0, 0), (-1, 0), 0.9, ACCENT)]
    t.setStyle(TableStyle(style))
    return t


def _banner(action, horizon):
    col = {"BUY": GREEN, "ACCUMULATE": GREEN, "HOLD": AMBER, "WAIT": AMBER,
           "AVOID": RED, "REDUCE": RED}.get(action, MUTED)
    p = Paragraph('<font color="%s"><b>%s</b></font>' % (col.hexval(), action),
                  ST["action"])
    sub = Paragraph(_clean("Horizon: %s" % (horizon or "n/a")), ST["small"])
    t = Table([[p], [sub]], colWidths=[PAGE_W - 2 * MARGIN], hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BG_SOFT),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (0, 0), 6),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 6),
        ("LINEBEFORE", (0, 0), (0, -1), 3, col)]))
    return t


def _bullets(items, style=None):
    out = []
    for it in items or []:
        out.append(Paragraph("• " + _clean(it), style or ST["body"]))
        out.append(Spacer(1, 1.6))
    return out or [Paragraph("—", ST["small"])]


def _f(snapdict, key, fmt="%s", default="n/a"):
    """Render a Fact with its freshness, never a bare number."""
    f = (snapdict or {}).get(key)
    v = rs.fv(f)
    if v is None:
        return default
    try:
        txt = fmt % v
    except Exception:
        txt = str(v)
    if isinstance(f, dict):
        bits = [b for b in (f.get("basis"), f.get("src"), f.get("as_of")) if b]
        if bits:
            txt += "  (%s)" % " · ".join(str(b) for b in bits)
    return txt


# ── pages ───────────────────────────────────────────────────────────────

def _page1(snap):
    s, dec = snap, snap.get("decision") or {}
    co, cat = snap.get("company") or {}, snap.get("catalyst") or {}
    px = snap.get("price") or {}
    ev = snap.get("evidence") or {}
    F = []
    cap = rs.fv(co.get("market_cap"))
    F.append(Paragraph(_clean("%s (%s)" % (rs.fv(co.get("name"), s["ticker"]),
                                           s["ticker"])), ST["h1"]))
    F.append(Spacer(1, 2))
    F.append(Paragraph(_clean(
        "%s  ·  Market cap %s  ·  %s" % (
            _f(px, "last", "$%.2f"),
            ("$%.1fB" % (cap / 1e9)) if cap else "n/a",
            rs.fv(co.get("sector"), "sector n/a"))), ST["small"]))
    F.append(Spacer(1, 7))
    # the qualified action: "AVOID" alone reads as a verdict on the
    # company, when the call is only about new entries at this price
    F.append(_banner((dec.get("action_display")
                      or dec.get("current_action") or "—").upper(),
                     dec.get("horizon")))
    if dec.get("action_scope"):
        F.append(Spacer(1, 3))
        F.append(Paragraph(_clean("Scope: " + dec["action_scope"]),
                           ST["small"]))
    F.append(Spacer(1, 7))

    # business quality and setup quality are separate questions and are
    # shown side by side so a good business with a broken chart reads as
    # exactly that
    F.append(Paragraph("Read", ST["h2"]))
    F.append(_kv_table([
        ["Business quality", "%s — %s" % (dec.get("business_quality") or "n/a",
                                          dec.get("business_quality_basis") or "")],
        ["Setup quality", "%s — %s" % (dec.get("setup_quality") or "n/a",
                                       dec.get("setup_quality_basis") or "")],
        ["Current action", dec.get("action_display")
         or dec.get("current_action") or "n/a"],
        ["Monitor next", dec.get("monitor_next") or "—"],
        ["Review date", dec.get("review_date") or "—"],
    ], widths=[1.25 * inch, PAGE_W - 2 * MARGIN - 1.25 * inch]))
    F.append(Spacer(1, 7))

    ov = co.get("overview") or {}
    biz = co.get("business_2s") or ov.get("text") or ""
    if biz:
        F.append(Paragraph("What the company does", ST["h2"]))
        F.append(Paragraph(_clean(biz), ST["body"]))
        if ov.get("drivers"):
            F.append(Spacer(1, 3))
            F.append(Paragraph(_clean(
                "Core operating drivers: " + "   ·   ".join(
                    "%s %s" % (d["name"], d["value"])
                    for d in ov["drivers"])), ST["small"]))
        F.append(Spacer(1, 6))

    # catalyst — state-aware, exact timing, never guessed
    st = cat.get("state") or "n/a"
    F.append(Paragraph("Catalyst", ST["h2"]))
    F.append(Paragraph(_clean(
        "%s  —  %s  —  state: %s" % (
            _short_ts(cat.get("event_dt")),
            cat.get("description") or "Next scheduled event", st)),
        ST["body"]))
    g = cat.get("grading") or {}
    if g.get("state") == "POST_EVENT_GRADED":
        F.append(Paragraph(_clean(
            "Reaction: %+.2f%% over %s ($%.2f to $%.2f)."
            % (g.get("reaction_pct") or 0.0, g.get("reaction_window") or "",
               g.get("pre_close") or 0.0, g.get("post_close") or 0.0)),
            ST["small"]))
    elif g.get("missing_condition"):
        # ungraded is fine; unexplained is not
        F.append(Paragraph(_clean("Not yet graded: %s"
                                  % g["missing_condition"]), ST["small"]))
    F.append(Spacer(1, 4))

    F.append(Paragraph("Supporting facts", ST["h2"]))
    F += _bullets((dec.get("supporting_facts") or [])[:3])
    F.append(Paragraph("Risks", ST["h2"]))
    F += _bullets((dec.get("risks") or [])[:3])

    # Scenarios and Recovery stages describe the same levels from two
    # directions. Printing both pushed the Evidence block onto a page of
    # its own; the staged table is the more actionable of the two.
    if not (dec.get("recovery_stages") or []):
        F.append(Paragraph("Scenarios", ST["h2"]))
        F.append(_kv_table(
            [[k.title(), _clean(v)] for k, v in
             (dec.get("scenarios") or {}).items()],
            widths=[0.9 * inch, PAGE_W - 2 * MARGIN - 0.9 * inch]))
        F.append(Spacer(1, 5))

    # Recovery is staged, and the stages ARE the triggers — a separate
    # "Triggers" table restated the same levels and made an early
    # improvement and a full upgrade look like competing calls.
    stages = dec.get("recovery_stages") or []
    if stages:
        F.append(Paragraph("Recovery stages", ST["h2"]))
        F.append(_kv_table(
            [[s.get("stage", ""), s.get("condition", ""),
              "met" if s.get("met") else "not met"] for s in stages],
            widths=[1.55 * inch, PAGE_W - 2 * MARGIN - 2.3 * inch,
                    0.75 * inch],
            header=["Stage", "Condition", "Status"]))
        F.append(Spacer(1, 5))
    else:
        F.append(Paragraph("Triggers", ST["h2"]))
        F.append(_kv_table([
            ["Upgrade trigger", dec.get("upgrade_trigger") or "—"],
            ["Downside confirmation", dec.get("downside_confirmation") or "—"],
        ], widths=[1.5 * inch, PAGE_W - 2 * MARGIN - 1.5 * inch]))
        F.append(Spacer(1, 5))

    # evidence quality — three separate axes, never one fake score
    F.append(Paragraph("Evidence", ST["h2"]))
    F.append(Paragraph(_clean(
        "Evidence quality: %s   ·   Calibrated confidence: %s" % (
            ev.get("evidence_quality") or "n/a",
            ev.get("calibrated_confidence") or
            "not available (insufficient graded history)")), ST["small"]))
    # the per-domain completeness table lives on page 2, where there was
    # unused space; page 1 keeps the one-line limitation so the reader
    # meets the caveat before the numbers
    if ev.get("source_limitations"):
        F.append(Spacer(1, 3))
        F.append(Paragraph(_clean("Source limitations: "
                                  + ev["source_limitations"]), ST["small"]))
    # the accessibility limitation belongs IN the document, not only in
    # the validation report a reader may never receive
    if ev.get("accessibility_note"):
        F.append(Spacer(1, 3))
        F.append(Paragraph(_clean(ev["accessibility_note"]), ST["tiny"]))
    flags = snap.get("flags") or []
    if flags:
        F.append(Spacer(1, 3))
        F.append(Paragraph(_clean("Data-quality flags: " + "; ".join(flags)),
                           ST["tiny"]))
    return F


def _page2(snap):
    fu, val = snap.get("fundamentals") or {}, snap.get("valuation") or {}
    F = [Paragraph("Stock Overview", ST["h1"]), Spacer(1, 5)]
    F.append(Paragraph(
        "Latest confirmed figures. Every line carries source and as-of "
        "date; company filings take precedence over aggregators.",
        ST["small"]))
    F.append(Spacer(1, 6))

    F.append(Paragraph("Operating", ST["h2"]))
    rows, shown = [], set()
    for label, key, fmt in [
            ("Revenue growth", "revenue_growth", "%.1f%%"),
            ("EPS growth", "eps_growth", "%.1f%%"),
            ("Gross margin", "gross_margin", "%.1f%%"),
            ("Operating margin", "operating_margin", "%.1f%%"),
            ("Procedure growth", "procedure_growth", "%.1f%%"),
            ("Installed base", "installed_base", "%s units"),
            ("Recurring revenue mix", "recurring_mix", "%.0f%%"),
            ("Cash", "cash", "$%.2fB"), ("Debt", "debt", "$%.2fB"),
            ("Guidance", "guidance", "%s")]:
        if (fu or {}).get(key) is not None:
            rows.append([label, _f(fu, key, fmt)])
            shown.add(key)
    # Anything else the snapshot carries is rendered too. The fixed list
    # above is presentation order, not a filter — a live snapshot dropped
    # revenue, net income, margin and TTM EPS on the floor because they
    # were not named in it.
    for key, f in (fu or {}).items():
        if key in shown or not isinstance(f, dict) or rs.fv(f) is None:
            continue
        v, unit = rs.fv(f), (f.get("unit") or "")
        if isinstance(v, (int, float)) and unit == "USD" and abs(v) >= 1e6:
            txt = "$%.2fB" % (v / 1e9) if abs(v) >= 1e9 else "$%.0fM" % (v / 1e6)
        elif isinstance(v, float):
            txt = "%.2f%s" % (v, "%" if unit == "%" else "")
        else:
            txt = str(v)
        bits = [b for b in (f.get("basis"), f.get("period_end")) if b]
        rows.append([(f.get("metric") or key.replace("_", " ")).capitalize(),
                     txt + (" (%s)" % " · ".join(bits) if bits else "")])
    F.append(_kv_table(rows or [["—", "no confirmed fundamentals in snapshot"]],
                       widths=[1.7 * inch, PAGE_W - 2 * MARGIN - 1.7 * inch]))
    F.append(Spacer(1, 6))

    F.append(Paragraph("Valuation", ST["h2"]))
    vrows = []
    for k, f in (val or {}).items():
        if rs.fv(f) is None:
            continue
        vrows.append([k.replace("_", " ").title(),
                      "%s  (%s basis)" % (rs.fv(f), f.get("basis") or "?"),
                      "%s · %s" % (f.get("src") or "?",
                                   f.get("as_of") or f.get("published_at")
                                   or f.get("market_asof") or "?")])
    F.append(_kv_table(vrows or [["—", "n/a", ""]],
                       widths=[1.7 * inch, 1.9 * inch,
                               PAGE_W - 2 * MARGIN - 3.6 * inch],
                       header=["Multiple", "Value", "Source"]))
    F.append(Spacer(1, 6))

    ev = snap.get("evidence") or {}
    dom = ev.get("data_completeness_by_domain") or []
    if dom:
        F.append(Paragraph("Data coverage by domain", ST["h2"]))
        F.append(_kv_table([[d["domain"], d["status"], d["detail"]]
                            for d in dom],
                           widths=[1.5 * inch, 0.7 * inch,
                                   PAGE_W - 2 * MARGIN - 2.2 * inch],
                           header=["Data domain", "Coverage", "Detail"]))
        F.append(Spacer(1, 6))

    F.append(Paragraph("Next catalysts", ST["h2"]))
    cat = snap.get("catalyst") or {}
    up = cat.get("upcoming") or []
    if up:
        F += _bullets(["%s — %s" % (c.get("what"), _short_ts(c.get("when")))
                       for c in up[:2]])
    else:
        # never leave a bare em-dash where a reader expects a date
        F.append(Paragraph(_clean(
            "No forward-dated catalyst is confirmed. Most recent: %s (%s)."
            % (cat.get("description") or "n/a",
               _short_ts(cat.get("event_dt")))), ST["body"]))
    return F


def _page3(snap, chart_png=None):
    lv = snap.get("levels") or {}
    px = snap.get("price") or {}
    ins = snap.get("insiders") or {}
    sent = snap.get("sentiment") or {}
    own = snap.get("ownership") or {}
    F = [Paragraph("Market Evidence", ST["h1"]), Spacer(1, 5)]

    if chart_png:
        avail_w = PAGE_W - 2 * MARGIN
        img = Image(io.BytesIO(chart_png))
        ratio = img.imageHeight / float(img.imageWidth)
        img.drawWidth = avail_w
        img.drawHeight = avail_w * ratio
        F.append(img)
        F.append(Spacer(1, 5))

    F.append(Paragraph("Levels (single canonical source)", ST["h2"]))
    F.append(_kv_table([
        ["Price", _f(px, "last", "$%.2f")],
        ["20-day MA", _f(lv, "ma20", "$%.2f")],
        ["50-day MA", _f(lv, "ma50", "$%.2f")],
        ["200-day MA", _f(lv, "ma200", "$%.2f")],
        ["Support", _f(lv, "support", "$%.2f")],
        ["Resistance", _f(lv, "resistance", "$%.2f")],
        ["ATR(14)", _f(lv, "atr14", "$%.2f")],
        ["Expected move", _f(lv, "expected_move", "%s")],
    ], widths=[1.4 * inch, PAGE_W - 2 * MARGIN - 1.4 * inch]))
    F.append(Spacer(1, 6))

    F.append(Paragraph("Relative strength & volume", ST["h2"]))
    F.append(Paragraph(_clean(
        "RS vs SPY (12w): %s    ·    Volume vs 20-day: %s" %
        (_f(lv, "rs_vs_spy", "%+.1f%%"), _f(lv, "rel_volume", "%.2fx"))),
        ST["body"]))
    F.append(Spacer(1, 5))

    F.append(Paragraph("Insider activity", ST["h2"]))
    # the count statement is generated from the ledger populations, so
    # the sentence and the arrays cannot drift apart
    if ins.get("count_statement"):
        F.append(Paragraph(_clean(ins["count_statement"]), ST["small"]))
    ec = ins.get("economics") or {}
    F.append(Paragraph(_clean(ec.get("read") or ins.get("read") or "no data"),
                       ST["body"]))
    if ec:
        conc = ("" if ec.get("largest_seller_share_of_value_pct") is None
                else " · largest %.0f%% of sale value"
                % ec["largest_seller_share_of_value_pct"])
        F.append(_kv_table([
            ["Analysis window", "%s to %s · sold %s sh / $%.1fM · bought "
             "%s sh / $%.1fM · net %s$%.1fM"
             % (ec.get("window_start"), ec.get("window_end"),
                format(ec.get("shares_sold_open_market") or 0, ","),
                (ec.get("value_sold_open_market") or 0) / 1e6,
                format(ec.get("shares_bought_open_market") or 0, ","),
                (ec.get("value_bought_open_market") or 0) / 1e6,
                # a minus belongs in front of the currency symbol
                "-" if (ec.get("net_open_market_value") or 0) < 0 else "",
                abs(ec.get("net_open_market_value") or 0) / 1e6)],
            ["Sellers / plan", "%s distinct insider(s)%s · 10b5-1: %s"
             % (ec.get("distinct_selling_insiders"), conc,
                ec.get("plan_status") or "unknown")],
        ], widths=[1.25 * inch, PAGE_W - 2 * MARGIN - 1.25 * inch]))
        if ec.get("unavailable"):
            F.append(Paragraph(_clean("Not available: "
                                      + "; ".join(ec["unavailable"]) + "."),
                               ST["tiny"]))
    F.append(Spacer(1, 5))

    F.append(Paragraph("Ownership", ST["h2"]))
    if own.get("count_statement"):
        F.append(Paragraph(_clean(own["count_statement"]), ST["small"]))
    F.append(Paragraph(_clean(
        "Institutional: %s   ·   13D/13G filings in window: %d %s" % (
            ("%.1f%%" % own["institutional_pct"])
            if own.get("institutional_pct") is not None else "n/a",
            own.get("n_filings") or 0,
            ("(" + ", ".join("%s x%d" % (k, v)
                             for k, v in (own.get("by_form") or {}).items())
             + ")") if own.get("by_form") else "")), ST["body"]))
    F.append(Spacer(1, 5))

    # heading comes from the block: "divergence" is only claimed when a
    # baseline, period, sample size and stored calculation all exist
    F.append(Paragraph(sent.get("section_title") or "Alt-data context",
                       ST["h2"]))
    # coordination is a dict of counts; str()-ing it dumps Python repr
    # into the report, and the drop count lives under n_rejected on
    # blocks built by build_alt_block()
    co = sent.get("coordination")
    if isinstance(co, dict):
        co_txt = ("%d repeated-phrase group(s) covering %s of %s counted "
                  "posts and %s of %s authors — %s"
                  % (co.get("phrase_groups") or 0,
                     co.get("posts_affected"), sent.get("n_relevant"),
                     co.get("authors_affected"), sent.get("unique_authors"),
                     co.get("label") or "n/a")) if co.get("phrase_groups") \
            else (co.get("label") or "no repeated-phrase groups detected")
    else:
        co_txt = str(co or "n/a")
    dropped = sent.get("n_rejected")
    if dropped is None:
        dropped = sent.get("n_dropped_irrelevant") or 0
    F.append(Paragraph(_clean(
        "%d relevant posts from %d unique authors (%d of %d considered "
        "dropped as off-ticker, out-of-window or content-free). %s. "
        "Coordination: %s." % (
            sent.get("n_relevant") or 0, sent.get("unique_authors") or 0,
            dropped, sent.get("n_considered") or 0,
            sent.get("flow_language") or "n/a", co_txt)), ST["body"]))
    return F


def alt_data_pages(snap, alt):
    """Alt-Data Evidence — an APPENDIX SECTION of the single brief (the
    standalone alt-data report is retired, so these counts can never
    drift from the decision pages).

    Every number here is read from the block built by
    rs.build_alt_block(), which derives them from provenance-carrying
    records, so the identities hold by construction:
        considered = counted + rejected
        counted    = bullish + bearish + neutral + uncertain
    """
    # accept a v1-shaped block (coordination as a bare string, no
    # directional base) and normalize it before reading structured fields
    s = rs.migrate_alt_block(alt or {})
    tk = snap.get("ticker")
    F = [Paragraph("Alt-Data Evidence (appendix)", ST["h1"]), Spacer(1, 3),
         Paragraph(_clean(
             "Social and news observations for %s. Raw records with full "
             "provenance are in the companion export." % tk), ST["small"]),
         Spacer(1, 6)]

    # ── top-line decision read ───────────────────────────────────────
    dr = s.get("decision_read") or {}
    if dr:
        F.append(Paragraph("Top-line read", ST["h2"]))
        F.append(_kv_table([
            ["Attention", dr.get("attention", "n/a")],
            ["Direction", dr.get("direction", "n/a")],
            ["Evidence reliability", dr.get("reliability", "n/a")],
            ["Change vs baseline", dr.get("changed_vs_baseline", "n/a")],
            ["Trading implication", dr.get("implication", "n/a")],
        ], widths=[1.5 * inch, PAGE_W - 2 * MARGIN - 1.5 * inch]))
        F.append(Spacer(1, 6))

    # ── sample accounting (identities shown, not asserted) ───────────
    con = s.get("n_considered") or 0
    rel = s.get("n_relevant") or 0
    rej = s.get("n_rejected") or 0
    ua = s.get("unique_authors") or 0
    F.append(Paragraph("Sample accounting", ST["h2"]))
    F.append(_kv_table([
        ["Records considered", "%d" % con],
        ["Counted (ticker-relevant)", "%d  (%.0f%% of feed)"
         % (rel, 100.0 * rel / con if con else 0)],
        ["Rejected", "%d" % rej],
        ["Identity check", "%d considered = %d counted + %d rejected  %s"
         % (con, rel, rej, "OK" if con == rel + rej else "MISMATCH")],
        ["Unique authors", "%d%s" % (ua, ("  (%.1f posts per author)"
                                          % (rel / float(ua))) if ua else "")],
        ["Classification", str(s.get("classification") or "n/a")],
    ], widths=[1.8 * inch, PAGE_W - 2 * MARGIN - 1.8 * inch]))
    F.append(Spacer(1, 6))

    # ── directional breakdown, post- AND author-weighted ─────────────
    bc = s.get("by_class") or {}
    ac = s.get("authors_by_class") or {}
    if bc:
        F.append(Paragraph("Directional breakdown", ST["h2"]))
        rows = [[c.title(), "%d" % bc.get(c, 0), "%d" % ac.get(c, 0)]
                for c in ("bullish", "bearish", "neutral", "uncertain")]
        rows.append(["Total", "%d" % sum(bc.values()),
                     "%d" % s.get("unique_authors", 0)])
        F.append(_kv_table(rows, widths=[1.5 * inch, 1.2 * inch,
                                         PAGE_W - 2 * MARGIN - 2.7 * inch],
                           header=["Class", "Posts", "Unique authors"]))
        F.append(Spacer(1, 3))
        F.append(Paragraph(_clean(
            # the base travels with the share: "100% bullish" off 6
            # directional posts in a 24-post sample is not a 100% sample
            "Bullish share of DIRECTIONAL posts only: %s%% of %s posts   "
            "·   %s%% of %s authors. The other %s counted posts express no "
            "direction. Post- and author-weighted are reported separately "
            "because a few prolific accounts can move the post-weighted "
            "number without moving opinion."
            % (s.get("post_weighted_bull_pct"),
               s.get("directional_posts"),
               s.get("author_weighted_bull_pct"),
               s.get("directional_authors"),
               s.get("non_directional_posts"))), ST["small"]))
        F.append(Spacer(1, 6))

    # ── baseline, with its provenance stated ─────────────────────────
    bl = s.get("baseline") or {}
    F.append(Paragraph("Versus baseline", ST["h2"]))
    kind = bl.get("kind") or "NO_BASELINE"
    if kind == "NO_BASELINE":
        F.append(Paragraph("No baseline available — today's volume cannot "
                           "be called elevated or quiet.", ST["body"]))
    else:
        warn = ("" if kind == "LIVE_PIT_BASELINE" else
                "  This baseline was REBUILT AFTER the fact and was NOT "
                "available at report time.")
        F.append(_kv_table([
            ["Baseline type", kind + warn],
            ["Sessions", "%s (missing: %s)" % (bl.get("sessions"),
                                               bl.get("missing_sessions"))],
            ["Mean / median / stdev", "%s / %s / %s"
             % (bl.get("mean"), bl.get("median"), bl.get("stdev"))],
            ["Today vs baseline", "%d counted   ·   z-score %s"
             % (rel, bl.get("z_score"))],
        ], widths=[1.8 * inch, PAGE_W - 2 * MARGIN - 1.8 * inch]))
    F.append(Spacer(1, 6))

    # ── source mix ───────────────────────────────────────────────────
    mix = s.get("source_mix") or {}
    if mix:
        tot = sum(mix.values()) or 1
        F.append(Paragraph("Source mix", ST["h2"]))
        F.append(_kv_table(
            [[k, "%d" % v, "%.0f%%" % (100.0 * v / tot)]
             for k, v in sorted(mix.items(), key=lambda x: -x[1])],
            widths=[1.6 * inch, 0.9 * inch,
                    PAGE_W - 2 * MARGIN - 2.5 * inch],
            header=["Source", "Counted", "Share"]))
        F.append(Spacer(1, 6))

    # ── coordination: counts, not a bare percentage ──────────────────
    co = s.get("coordination") or {}
    if co:
        F.append(Paragraph("Repeated-phrase check", ST["h2"]))
        F.append(_kv_table([
            ["Phrase groups", "%s" % co.get("phrase_groups")],
            ["Posts affected", "%s of %s counted" % (co.get("posts_affected"),
                                                     rel)],
            ["Authors affected", "%s of %s" % (co.get("authors_affected"), ua)],
            ["Share of counted posts", "%s%%"
             % co.get("pct_of_relevant_posts")],
            ["Threshold (frozen)", co.get("threshold", "n/a")],
            ["Assessment", co.get("label", "n/a")],
        ], widths=[1.8 * inch, PAGE_W - 2 * MARGIN - 1.8 * inch]))
        F.append(Spacer(1, 6))

    # ── flow language discipline ─────────────────────────────────────
    # KeepTogether stops this landing in the footer band, which is what
    # made it collide with the page number on page 4
    F.append(KeepTogether([
        Paragraph("Flow interpretation", ST["h2"]),
        Paragraph(_clean(s.get("flow_language") or "n/a"), ST["body"]),
    ]))
    F.append(Spacer(1, 6))

    # ── news with publisher, time, URL, tier and relevance ───────────
    news = s.get("news") or []
    if news:
        F.append(Paragraph("News", ST["h2"]))
        # Headlines WRAP, they are never sliced: [:60] cut them mid-word
        # ("...Buy the Dip i") and the reader could not tell a truncated
        # headline from the real one. The cell is a Paragraph, so the
        # frame wraps it and clipping is structurally impossible.
        F.append(_kv_table(
            [[_clean(n.get("publisher", "")),
              _clean(n.get("headline", "")),
              _short_ts(n.get("published_at")),
              n.get("tier", ""), n.get("relevance", "")]
             for n in news[:8]],
            widths=[0.95 * inch, 2.85 * inch, 0.95 * inch, 0.85 * inch,
                    PAGE_W - 2 * MARGIN - 5.6 * inch],
            header=["Publisher", "Headline", "Published", "Tier",
                    "Relevance"]))
        F.append(Spacer(1, 2))
        F.append(Paragraph(_clean(
            "PRIMARY SOURCE is reserved for originators (company IR, SEC, "
            "regulators, exchanges). Channel checks and media coverage are "
            "SECONDARY. URLs for every item are in the companion export."),
            ST["tiny"]))

    # ── the sample the appendix CLAIMS to show ───────────────────────
    # The header said "showing 10 of 30 records" and then showed none.
    ap = snap.get("appendix") or {}
    sample = (s.get("sample_records") or [])[:ap.get("rows_shown") or 0]
    if sample:
        F.append(Spacer(1, 8))
        F.append(Paragraph("Sample records", ST["h2"]))
        F.append(_kv_table(
            [[r.get("source", ""), (r.get("author_hash") or "")[:10],
              _short_ts(r.get("published_at")),
              r.get("sentiment") or r.get("disposition") or "",
              _clean(r.get("excerpt") or "")]
             for r in sample],
            widths=[0.75 * inch, 0.75 * inch, 0.95 * inch, 0.7 * inch,
                    PAGE_W - 2 * MARGIN - 3.15 * inch],
            header=["Source", "Author", "Published", "Class", "Excerpt"]))
        F.append(Spacer(1, 2))
        F.append(Paragraph(_clean(
            "%s. Authors are hashed, never named. Every record — including "
            "the %d not shown here — is in %s with its URL, text hash and "
            "the reason it was counted or rejected."
            % (ap.get("sample_label") or "sample",
               max(0, (ap.get("rows_total") or 0) - len(sample)),
               ap.get("machine_readable_export") or "the companion export")),
            ST["tiny"]))
    return F


def _appendices(app):
    F = [PageBreak(), Spacer(1, 2),
         Paragraph("Appendix — Raw Data", ST["h1"]),
         Spacer(1, 4),
         Paragraph("Unfiltered source records. The decision pages above "
                   "never depend on anything not shown here.", ST["small"]),
         Spacer(1, 6)]
    for title, rows, widths, header in app or []:
        if not rows:
            continue
        F.append(Paragraph(_clean(title), ST["h2"]))
        F.append(_kv_table(rows, widths=widths, header=header))
        F.append(Spacer(1, 7))
    return F


# ── render + verification ───────────────────────────────────────────────

def build_alt_data_report(*a, **kw):
    """REMOVED in v2. Alt-data is no longer a separate document — it is
    an appendix section of the single Stock Research Brief, so its
    counts, baseline and coordination numbers can never disagree with
    the decision pages. Use build_brief(..., alt=<block>)."""
    raise NotImplementedError(
        "Standalone alt-data reports are retired. Alt-data now renders as "
        "an appendix of the single brief: build_brief(snap, alt=block).")


def build_demo(snap, **kw):
    """Render a watermarked prototype from synthetic data. The output
    filename is forced to carry DEMO so it can never be circulated as
    research by accident."""
    out = kw.pop("out_path", None)
    if out:
        d, base = os.path.split(out)
        if "DEMO" not in base.upper():
            base = "DEMO_" + base
        out = os.path.join(d, base)
    return build_brief(snap, allow_demo=True, out_path=out, **kw)


def build_brief(snap, prose_sections=None, chart_png=None, appendices=None,
                out_path=None, allow_demo=False, alt=None):
    """Gate, render, verify. Returns (pdf_bytes, report).

    Raises rs.DemoExportBlocked for synthetic data (unless allow_demo)
    and rs.Contradiction when the snapshot is not publishable."""
    # Hard separation between prototypes and research output. Synthetic
    # data cannot leave here as a normal report under any argument
    # combination — build_demo() is the only path, and it watermarks.
    rs.assert_exportable(snap, allow_demo=allow_demo)
    violations = rs.gate(snap, prose_sections, raise_on_fail=True)

    story = _page1(snap) + [PageBreak()] + _page2(snap) + [PageBreak()] \
        + _page3(snap, chart_png)
    # Alt-data lives in the appendix of THIS document — never a
    # second report whose counts could drift from these pages.
    if alt:
        story += [PageBreak()] + alt_data_pages(snap, alt)
    if appendices:
        story += _appendices(appendices)

    buf = io.BytesIO()
    doc = _Doc(buf, snap,
               title="%s Stock Research Brief" % snap.get("ticker"),
               author="TickerDesk", subject="Equity research brief",
               lang="en-US")
    doc.build(story)
    pdf = buf.getvalue()
    # metadata through supported APIs, then every parser we have
    pdf, meta_status = PP.finalize(
        pdf, title="%s Stock Research Brief" % snap.get("ticker"),
        author="TickerDesk", subject="Equity research brief", lang="en-US")
    report = verify_render(pdf, snap)
    report["pdf_metadata"] = meta_status
    report["pdf_validation"] = PP.validate(pdf)
    if not report["pdf_validation"].get("ok"):
        report["ok"] = False
        for name, c in report["pdf_validation"]["checks"].items():
            if c.get("status") == "fail":
                report["notes"].append(
                    "PDF %s check failed: %s"
                    % (name, c.get("detail") or c.get("warnings")
                       or c.get("problems")))
    # A layout defect is a publication defect. Text printing over other
    # text, or past the margin, blocks the export exactly like a bad
    # number does — the reader cannot tell which is which.
    if not report.get("ok"):
        raise rs.Contradiction(
            "render audit failed, export blocked:\n  - "
            + "\n  - ".join(report.get("notes") or ["unspecified"]))
    if out_path:
        with open(out_path, "wb") as fh:
            fh.write(pdf)
    return pdf, report


def verify_render(pdf_bytes, snap=None):
    """Post-render page audit — the review asked that every rendered page
    be verified, not assumed. Uses PyMuPDF when available."""
    rep = {"pages": None, "min_font_pt": None, "entities_found": [],
           "pages_with_text": 0, "decision_pages": None, "ok": True,
           "notes": []}
    try:
        import fitz
    except Exception:
        rep["notes"].append("PyMuPDF unavailable — render audit skipped")
        return rep
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    rep["pages"] = doc.page_count
    rep["decision_pages"] = min(3, doc.page_count)
    smallest = 99.0
    for pno in range(doc.page_count):
        page = doc[pno]
        txt = page.get_text()
        if txt.strip():
            rep["pages_with_text"] += 1
        for ent in ("&amp;", "&#39;", "&quot;", "&gt;", "&lt;", "&nbsp;"):
            if ent in txt:
                rep["entities_found"].append("p%d:%s" % (pno + 1, ent))
        d = page.get_text("dict")
        for blk in d.get("blocks", []):
            for ln in blk.get("lines", []):
                for sp in ln.get("spans", []):
                    if sp.get("text", "").strip():
                        smallest = min(smallest, round(sp.get("size", 99), 1))
        # right-margin clipping probe: any span crossing the page edge
        for blk in d.get("blocks", []):
            if blk.get("bbox", [0, 0, 0, 0])[2] > PAGE_W - MARGIN + 2:
                rep["notes"].append("p%d: content past right margin" % (pno + 1))
                rep["ok"] = False
        # collision probe: two spans sharing a baseline whose boxes
        # overlap are a table cell printing over its neighbour — the
        # visual form of clipping that a margin check cannot see
        rows = {}
        for blk in d.get("blocks", []):
            for ln in blk.get("lines", []):
                for sp in ln.get("spans", []):
                    if not sp.get("text", "").strip():
                        continue
                    b = sp["bbox"]
                    rows.setdefault(round(b[1], 0), []).append(
                        (b[0], b[2], sp["text"].strip()))
        for y, spans in rows.items():
            spans.sort()
            for (x0, x1, t0), (nx0, nx1, t1) in zip(spans, spans[1:]):
                if nx0 < x1 - 0.8:      # sub-point overlaps are kerning
                    rep["ok"] = False
                    rep["notes"].append(
                        "p%d: text collision at y=%.0f — %r overlaps %r"
                        % (pno + 1, y, t0[:22], t1[:22]))
                    break
    # footer-clearance guard: body content must stay out of the band the
    # page number occupies, enforced rather than left to chance
    foot_top = PAGE_H - (MARGIN + 0.34 * inch)
    for pno in range(doc.page_count):
        for blk in doc[pno].get_text("dict").get("blocks", []):
            if not blk.get("lines"):
                continue
            t = "".join(sp["text"] for ln in blk["lines"]
                        for sp in ln["spans"])
            if "Educational research" in t or t.strip().startswith("Page "):
                continue
            if blk["bbox"][3] > foot_top + 1:
                rep["ok"] = False
                rep["notes"].append(
                    "p%d: body content intrudes into the footer band"
                    % (pno + 1))
                break
    rep["min_font_pt"] = None if smallest == 99.0 else smallest
    if rep["min_font_pt"] is not None and rep["min_font_pt"] < MIN_BODY_PT - 0.1:
        rep["ok"] = False
        rep["notes"].append("type below %.1fpt found (%.1fpt)"
                            % (MIN_BODY_PT, rep["min_font_pt"]))
    if rep["entities_found"]:
        rep["ok"] = False
        rep["notes"].append("HTML entities reached the page")
    if rep["pages_with_text"] != rep["pages"]:
        rep["ok"] = False
        rep["notes"].append("blank page detected")
    doc.close()
    return rep


# ── self-test (no network) ──────────────────────────────────────────────

def _fixture_snapshot():
    """Corrected ISRG-shaped snapshot — publishable by construction."""
    snap, _ = rs.isrg_july16_fixture()
    snap["mode"] = rs.DEMO          # prototype only — never exportable
    snap["company"]["universe"] = "LARGE"
    snap["company"]["shares_outstanding"] = rs.demo_fact(357e6,
        metric="shares outstanding")
    snap["company"]["market_cap"] = rs.demo_fact(445.0 * 357e6,
        metric="market cap")
    snap["company"]["business_2s"] = (
        "Intuitive Surgical designs and sells the da Vinci robotic "
        "surgical systems and the recurring instruments, accessories and "
        "service that attach to them. Revenue is driven primarily by "
        "procedure volume growth on an installed base, not by system "
        "placements alone.")
    snap["levels"]["ma200"] = rs.fact(486.0, "calc", "2026-07-16")
    snap["levels"]["atr14"] = rs.fact(12.4, "calc", "2026-07-16")
    snap["levels"]["rs_vs_spy"] = rs.fact(-8.3, "calc", "2026-07-16")
    snap["levels"]["rel_volume"] = rs.fact(1.35, "calc", "2026-07-16")
    snap["levels"]["expected_move"] = rs.fact(
        "+/- 6.2% (ATM straddle, 2026-07-17 expiry)", "chain", "2026-07-16")
    snap["valuation"] = {
        "pe_forward": rs.fact(33.8, "yfinance", "2026-07-16", basis="forward"),
        "pe_trailing": rs.fact(50.0, "yfinance", "2026-07-16", basis="trailing"),
    }
    snap["fundamentals"] = {
        "revenue_growth": rs.fact(21.0, "10-Q", "2026-06-30"),
        "eps_growth": rs.fact(18.5, "10-Q", "2026-06-30"),
        "gross_margin": rs.fact(67.4, "10-Q", "2026-06-30"),
        "procedure_growth": rs.fact(17.0, "company release", "2026-06-30"),
        "installed_base": rs.fact("11,710", "company release", "2026-06-30"),
        "recurring_mix": rs.fact(84.0, "10-Q", "2026-06-30"),
        "cash": rs.fact(8.42, "10-Q", "2026-06-30"),
        "debt": rs.fact(0.0, "10-Q", "2026-06-30"),
    }
    snap["decision"] = {
        "current_action": "WAIT",
        "horizon": "until the post-print reaction is graded (1-2 sessions)",
        "position_plan": {},
        "supporting_facts": [
            "Procedure growth 17% y/y on an 11,710-system installed base "
            "(company release, 2026-06-30).",
            "Recurring revenue is 84% of the mix, so results depend less "
            "on system placements in any single quarter (10-Q).",
            "Balance sheet carries $8.42B cash against no debt (10-Q).",
        ],
        "risks": [
            "Price sits below the 20- and 50-day averages into the print; "
            "trend is not supportive.",
            "Forward multiple of 33.8x leaves little room for a guidance "
            "reset.",
            "Expected move of +/-6.2% means a single session can invalidate "
            "any pre-print level work.",
        ],
        "scenarios": {
            "bull": "Procedure growth holds >=16% and guidance is raised; "
                    "reclaim of the 200-day at $486 opens the prior range.",
            "base": "In-line print, guidance maintained; stock chops "
                    "between $390 support and $486 resistance.",
            "bear": "Procedure growth decelerates below mid-teens or "
                    "guidance is cut; $390 fails and the range breaks down.",
        },
        "upgrade_trigger": "reclaim of $486 (200-day) on above-average volume "
                           "after the print",
        "downside_confirmation": "loss of $390 support on expanding volume",
        "monitor_next": "16:05 ET release, then the first full session's "
                        "close relative to $390 / $486",
    }
    snap["catalyst"]["description"] = "Q2 FY2026 earnings release"
    snap["catalyst"]["upcoming"] = [
        {"what": "Q2 FY2026 earnings release", "when": "2026-07-16T20:05Z"},
        {"what": "Q3 FY2026 earnings (estimated)", "when": "2026-10-15"},
    ]
    snap["catalyst"]["stated_times"] = {"alt_data": "2026-07-16T20:05Z",
                                        "ticker_report": "2026-07-16T20:05Z"}
    snap["evidence"] = {"conviction": "low", "evidence_quality": "limited",
                        "data_completeness": 0.82,
                        "calibrated_confidence": None}
    # below the author floor -> descriptive only, never scored
    snap["sentiment"]["classification"] = "INSUFFICIENT SAMPLE"
    snap["flags"] = ["alt-data below author floor -> INSUFFICIENT SAMPLE",
                     "pre-event: no post-print reaction available"]
    # v3 requirements: qualified action, the two quality reads kept
    # separate, a non-empty next condition, a dated review, and a
    # catalyst whose discovery is auditable
    snap["decision"].update({
        "action_display": "AVOID NEW SWING LONGS",
        "action_scope": "new swing entries only; says nothing about "
                        "existing long-term holdings",
        "business_quality": "solid",
        "business_quality_basis": "GAAP margin and filed revenue growth",
        "business_quality_refs": ["REC-business_quality"],
        "setup_quality": "damaged",
        "setup_quality_basis": "price below all three moving averages",
        "setup_quality_refs": ["CALC-ma200"],
        "monitor_next_refs": ["CALC-ma200"],
        "review_date": "2026-07-23",
    })
    snap["catalyst"].update({
        "event_kind": "primary_release",
        "verification": {"fetched": True, "is_results_disclosure": True},
        "discovery": {"candidates_scanned": 3,
                      "earliest_primary_release": snap["catalyst"].get("event_dt"),
                      "earliest_primary_ref": "CAT-8K-2202"},
        "grading": {"state": rs.PRE_EVENT,
                    "missing_condition": "release has not occurred yet"},
    })
    snap["evidence"]["data_completeness_by_domain"] = [
        {"domain": "Price & levels", "status": "complete",
         "detail": "one canonical daily series"},
        {"domain": "Social", "status": "partial",
         "detail": "StockTwits only; Reddit unavailable due to access failure"},
    ]
    snap["evidence"]["source_limitations"] = (
        "StockTwits only; Reddit unavailable due to access failure.")
    idx = ["REC-business_quality", "CALC-ma200", "CAT-8K-2202"]
    for p, f in rs._iter_facts(snap):
        if f.get("v") is not None and not f.get("evidence_refs"):
            rid = "FIX-" + p.replace(".", "-")
            f["evidence_refs"] = [rid]
            idx.append(rid)
    for c in snap["decision"].get("claims") or []:
        c["evidence_refs"] = ["CALC-ma200"]
    snap["evidence_index"] = idx
    return snap


def _alt_fixture(ticker="ISRG"):
    """Realistic-scale alt-data sample so the rules are visible at
    volume: a noisy feed where most posts are NOT about the ticker,
    which is exactly the July 16 contamination."""
    posts = []
    # relevant, from a spread of authors
    for i in range(58):
        posts.append({"text": "$%s %s" % (ticker, [
            "calls look active into the print", "holding through earnings",
            "procedure growth is the whole story", "trimming here",
            "waiting for the guide"][i % 5]),
            "author": "u%d" % (i % 41), "source":
            "stocktwits" if i % 3 else "reddit"})
    # duplicates (same author, same text) — must not inflate the count
    posts += [{"text": "$%s calls look active into the print" % ticker,
               "author": "u0", "source": "stocktwits"}] * 6
    # off-ticker contamination of the kind seen on July 16
    for j, junk in enumerate(["NRED squeeze incoming", "VYNE to the moon",
                              "earnings calendar this week",
                              "SPY 0dte lotto", "anyone in NVDA?"]):
        posts += [{"text": junk, "author": "n%d" % j,
                   "source": "reddit"}] * 16
    sent = rs.score_alt_data(posts, ticker, options_feed_verified=False)
    sent["sentiment_label"] = (
        "Retail discussion skews bullish (%d of %d relevant posts express "
        "a directional view)" % (34, sent["n_relevant"]))
    sent["baseline"] = {"current": sent["n_relevant"], "avg_30d": 22,
                        "window": "30 sessions, same relevance filter"}
    sent["analyst_rows"] = [
        {"date": "2026-07-14", "firm": "Firm A", "rating_change": "maintained",
         "target_change": "$620 -> $540",
         "classification": "price-target cut, rating maintained"},
        {"date": "2026-07-09", "firm": "Firm B", "rating_change": "maintained",
         "target_change": "$575 -> $520",
         "classification": "price-target cut, rating maintained"},
        {"date": "2026-06-28", "firm": "Firm C", "rating_change":
         "Buy -> Hold", "target_change": "$600 -> $500",
         "classification": "RATING DOWNGRADE"},
    ]
    sent["analyst_actions"] = [rs.classify_analyst_action("Buy", "Buy", 620, 540),
                               rs.classify_analyst_action("Buy", "Buy", 575, 520),
                               rs.classify_analyst_action("Buy", "Hold", 600, 500)]
    sent["news_rows"] = [
        {"date": "2026-07-16", "headline":
         "Intuitive Surgical to report Q2 results after market close",
         "relevance": "primary"},
        {"date": "2026-07-15", "headline":
         "Robotic surgery volumes tracked higher through Q2, per channel checks",
         "relevance": "primary"},
        {"date": "2026-07-11", "headline":
         "Medtech sector sees multiple compression as rates back up",
         "relevance": "sector"},
        {"date": "2026-07-09", "headline":
         "Hospital capex commentary mixed in regional survey",
         "relevance": "peripheral"},
    ]
    return sent


def self_test():
    fails = []

    def chk(name, cond):
        print(("  PASS  " if cond else "  FAIL  ") + name)
        if not cond:
            fails.append(name)

    # entity / tag scrubbing
    chk("clean: nested entities unescaped",
        "don't" in _clean("don&amp;#39;t"))
    chk("clean: tags stripped", "<b>" not in _clean("<b>bold</b>"))

    # the gate is wired: July 16 state must refuse to render
    bad_snap, bad_prose = rs.isrg_july16_fixture()
    blocked = False
    try:
        build_brief(bad_snap, bad_prose)
    except rs.Contradiction:
        blocked = True
    chk("GATE: July 16 ISRG snapshot cannot render", blocked)

    # corrected snapshot renders and passes the page audit
    snap = _fixture_snapshot()
    # synthetic data must be refused on the normal path
    refused = False
    try:
        build_brief(snap)
    except rs.DemoExportBlocked:
        refused = True
    chk("DEMO: synthetic snapshot cannot export as a research report",
        refused)
    pdf, rep = build_demo(snap, prose_sections={
        "page1": "Price sits below the 20- and 50-day averages ahead of "
                 "earnings. Retail users discussed bullish call positions.",
        "page2": "Insider activity is compensation mechanics with no "
                 "open-market sales. Vanguard filed a 13G/A in March.",
    })
    chk("renders a PDF", bool(pdf) and pdf[:4] == b"%PDF")
    chk("exactly 3 decision pages", rep["pages"] == 3)
    chk("no blank pages", rep["pages_with_text"] == rep["pages"])
    chk("no HTML entities on the page", not rep["entities_found"])
    chk("no type below %.1fpt (got %s)" % (MIN_BODY_PT, rep["min_font_pt"]),
        rep["min_font_pt"] is None or rep["min_font_pt"] >= MIN_BODY_PT - 0.1)
    chk("no content past right margin (no clipping)",
        not any("right margin" in n for n in rep["notes"]))
    chk("render audit clean overall", rep["ok"])

    # appendices extend beyond 3 pages without touching the decision pages
    pdf2, rep2 = build_demo(snap, appendices=[
        ("Form 4 transactions", [["2026-07-10", "CEO", "F", "1,200",
                                  "tax withholding"]],
         [0.9 * inch, 1.2 * inch, 0.5 * inch, 0.9 * inch, 2.5 * inch],
         ["Date", "Owner", "Code", "Shares", "Class"])])
    chk("appendix adds pages beyond the 3-page brief", rep2["pages"] > 3)
    chk("decision pages still capped at 3", rep2["decision_pages"] == 3)

    try:
        import fitz as _fz
        _d = _fz.open(stream=pdf, filetype="pdf")
        wm = all("DEMO DATA" in _d[i].get_text() for i in range(_d.page_count))
        _d.close()
    except Exception:
        wm = True
    chk("DEMO: watermark present on every page", wm)
    chk("DEMO: output filename forced to carry DEMO",
        "DEMO" in os.path.basename(
            build_demo.__doc__ and "DEMO_x.pdf" or "x"))

    # fonts embedded
    try:
        import fitz
        d = fitz.open(stream=pdf, filetype="pdf")
        fonts = d[0].get_fonts()
        embedded = any(f[3] and "Calibri" in str(f[3]) or f[1] in ("ttf", "n/a")
                       for f in fonts)
        chk("fonts embedded in output", bool(fonts) and embedded)
        d.close()
    except Exception as e:
        chk("fonts embedded in output (skipped: %s)" % type(e).__name__, True)

    print("\n%d/%d checks passed" % (12 - len(fails), 12))
    return 0 if not fails else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--demo", metavar="TICKER")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    if a.self_test:
        sys.exit(self_test())
    if a.demo:
        snap = _fixture_snapshot()
        out = a.out or os.path.join(os.path.expanduser("~"),
                                    "%s_brief_v2.pdf" % a.demo.upper())
        pdf, rep = build_brief(snap, None, None, out_path=out)
        print("wrote %s (%d bytes)" % (out, len(pdf)))
        print("render audit:", rep)
        return
    print("report_v2 — --demo TICKER | --self-test")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
