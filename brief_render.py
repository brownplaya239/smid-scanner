#!/usr/bin/env python3
"""brief_render.py — the HTML half of the message.

Renders brief_model sections and nothing else. It holds no opinion about
which contracts to show or how many: those decisions live in the model so
that this file and brief_text.py cannot disagree about what the email
contains. Presentation only.

Email HTML is not web HTML. Outlook ignores <body> backgrounds and most
CSS, so the dark theme is painted with bgcolor + inline styles on nested
tables, and layout is tables rather than flex/grid. The only media query
stacks the index table on narrow screens.

    python brief_render.py --self-test
"""

import html
import re
import sys

import brief_compose as BC
import brief_model as BM

# Site dark theme (docs/index.html "navy" default), reused so the email
# and the dashboard read as one product.
BG = "#0a0f24"
PANEL = "#111a38"
PANEL2 = "#162246"
BORDER = "#22305c"
TEXT = "#e8ecf6"
DIM = "#cdd6ee"
MUTED = "#8a93a8"
GREEN = "#1fb363"
RED = "#ff5b78"
AMBER = "#ffc800"
ACCENT = "#7aa9ff"

FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
        "Helvetica,Arial,sans-serif")
BODY_PX = 15
SMALL_PX = 14

REGIME_COLOR = {"RISK-ON": GREEN, "RISK-OFF": RED,
                "BALANCED": ACCENT, "TRANSITION": AMBER}
STATUS_COLOR = {
    BC.TRIGGER_REACHED: RED, BC.REVIEW: RED, BC.WEAKENING: RED,
    BC.MIXED_SETUP: AMBER, BC.BEARISH_FLOW_ALERT: AMBER,
    BC.BULLISH_FLOW_ALERT: AMBER, BC.STRENGTHENING: GREEN,
    BC.MONITOR: MUTED, BC.NOTABLE: MUTED, BC.NO_CHANGE: MUTED,
}
DIR_COLOR = {BM.DIR_BULL: GREEN, BM.DIR_BEAR: RED, BM.DIR_NONE: MUTED}


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def _sign_txt(v, suffix="%", places=2):
    if v is None:
        return "n/a"
    if abs(round(v, places)) < 10 ** -places / 2:
        return "%.*f%s" % (places, 0.0, suffix)
    return "%+.*f%s" % (places, v, suffix)


def _sign(v, suffix="%", places=2):
    """Signed, coloured, and never '-0.00%'. A value that rounds to zero
    has no sign to report."""
    if v is None:
        return '<span style="color:%s">n/a</span>' % MUTED
    if abs(round(v, places)) < 10 ** -places / 2:
        return '<span style="color:%s">%.*f%s</span>' % (MUTED, places, 0.0,
                                                         suffix)
    c = GREEN if v > 0 else RED
    return '<span style="color:%s">%+.*f%s</span>' % (c, places, v, suffix)


def _cell(inner, pad="6px 10px", align="left", extra="", cls=""):
    return ('<td align="%s"%s style="padding:%s;font-family:%s;font-size:%dpx;'
            'color:%s;%s">%s</td>' % (align, (' class="%s"' % cls) if cls
                                      else "", pad, FONT, SMALL_PX, TEXT,
                                      extra, inner))


def _card(title, inner, sub=""):
    """One card. bgcolor attribute AND inline style — Outlook needs the
    attribute, everything else honours the style."""
    subhtml = ('<div style="font-family:%s;font-size:%dpx;color:%s;'
               'padding:0 0 8px">%s</div>' % (FONT, SMALL_PX, MUTED, esc(sub))
               ) if sub else ""
    head = ('<div style="font-family:%s;font-size:16px;font-weight:700;'
            'color:%s;padding:0 0 6px">%s</div>' % (FONT, TEXT, esc(title))
            ) if title else ""
    return (
      '<tr><td style="padding:0 0 14px">'
      '<table role="presentation" width="100%%" cellpadding="0" cellspacing="0"'
      ' border="0" bgcolor="%s" style="background:%s;border:1px solid %s;'
      'border-radius:10px">'
      '<tr><td style="padding:14px 16px">%s%s%s'
      '</td></tr></table></td></tr>'
      % (PANEL, PANEL, BORDER, head, subhtml, inner))


def _line(txt, size=None, color=None, pad="3px 0"):
    return ('<div style="font-family:%s;font-size:%dpx;color:%s;padding:%s">'
            '%s</div>' % (FONT, size or SMALL_PX, color or DIM, pad, txt))


def _subhead(label):
    return ('<div style="font-family:%s;font-size:12px;color:%s;'
            'text-transform:uppercase;letter-spacing:.5px;'
            'padding:8px 0 6px">%s</div>' % (FONT, MUTED, esc(label)))


# ── sections ────────────────────────────────────────────────────────────

def render_market(sec, site):
    reg = sec.get("regime") or {}
    col = REGIME_COLOR.get(reg.get("label"), ACCENT)
    rows = []
    for r in sec["records"]:
        dist = r["vs20"]
        dcol = {BC.ABOVE: GREEN, BC.BELOW: RED}.get(r["ma_state"], MUTED)
        second = ('<span class="mo" style="display:none;font-size:12px;'
                  'color:%s">1W %s · YTD %s</span>'
                  % (MUTED, _sign_txt(r["w1"]), _sign_txt(r["ytd"])))
        rows.append(
            "<tr>" + _cell('<a href="%s" style="color:%s;'
                           'text-decoration:none"><b>%s</b></a>%s'
                           % (esc(r["url"]), ACCENT, esc(r["ticker"]),
                              second)) +
            _cell("%.2f" % (r["last"] or 0), align="right") +
            _cell(_sign(r["d1"]), align="right") +
            _cell(_sign(r["w1"]), align="right", cls="wk") +
            _cell(_sign(r["ytd"]), align="right", cls="wk") +
            _cell('<span style="color:%s">%s</span>'
                  % (dcol, _sign_txt(dist, places=1)), align="right")
            + "</tr>")
    head = ("<tr>" + "".join(
        '<th align="%s"%s style="padding:4px 10px;font-family:%s;'
        'font-size:12px;color:%s;text-transform:uppercase;letter-spacing:.4px;'
        'font-weight:600">%s</th>' % (a, (' class="%s"' % c) if c else "",
                                      FONT, MUTED, h)
        for h, a, c in (("", "left", ""), ("Last", "right", ""),
                        ("1D", "right", ""), ("1W", "right", "wk"),
                        ("YTD", "right", "wk"),
                        ("vs 20d", "right", ""))) + "</tr>")
    table = ('<table role="presentation" width="100%%" cellpadding="0" '
             'cellspacing="0" border="0">%s%s</table>' % (head, "".join(rows)))

    banner = ('<div style="font-family:%s;font-size:22px;font-weight:800;'
              'color:%s;letter-spacing:.3px">%s</div>%s'
              % (FONT, col, esc(reg.get("label") or "MARKET"),
                 _line(esc(reg.get("why") or ""), SMALL_PX, DIM, "4px 0 12px")))
    tail = _line(esc(sec.get("ma_summary") or ""), 12, MUTED, "6px 0 0") \
        if sec.get("ma_summary") else ""

    ev = sec.get("event") or {}
    ev_html = ""
    if ev.get("title"):
        ev_html = _line('NEXT: <b style="color:%s">%s</b> · %s · '
                        '<span style="color:%s">%s</span>'
                        % (TEXT, esc(ev.get("title")),
                           esc(ev.get("time_et") or ""), MUTED,
                           esc(ev.get("status") or "")), SMALL_PX, DIM,
                        "8px 0 0")
    alert = ('<div style="font-family:%s;font-size:%dpx;color:%s;'
             'font-weight:700;padding:10px 12px;margin:12px 0 0;background:%s;'
             'border-left:3px solid %s;border-radius:4px">%s</div>'
             % (FONT, BODY_PX, TEXT, PANEL2, ACCENT,
                esc(sec.get("alert_line") or "")))
    note = _line("Quotes span more than one session; each value is labelled "
                 "with its own as-of.", 12, MUTED, "8px 0 0") \
        if sec.get("mixed_sessions") else ""
    return _card(sec["title"], banner + table + tail + ev_html + alert + note,
                 sub=sec.get("sub"))


def render_watchlist(sec, site):
    if not sec["records"]:
        inner = _line("No material changes across your watch list.", BODY_PX)
        return _card(sec["title"], inner, sub=sec.get("sub"))
    out = []
    for x in sec["records"]:
        col = STATUS_COLOR.get(x["status"], MUTED)
        meta = []
        if x.get("flow_quality"):
            meta.append("Flow quality %s" % esc(x["flow_quality"]))
        if x.get("evidence"):
            meta.append("Evidence %s" % esc(x["evidence"]))
        if x.get("edge"):
            meta.append(esc(x["edge"]))
        if x.get("reason_codes"):
            meta.append("Rules: %s" % esc(", ".join(x["reason_codes"])))
        trig = []
        if x.get("next_confirmation"):
            trig.append("Next confirmation: %s" % esc(x["next_confirmation"]))
        if x.get("invalidation"):
            trig.append("Invalidation: %s" % esc(x["invalidation"]))
        basis = (" <span style=\"color:%s\">· %s</span>"
                 % (MUTED, esc(x["status_basis"]))) if x.get("status_basis") \
            else ""
        out.append(
          '<table role="presentation" width="100%%" cellpadding="0" '
          'cellspacing="0" border="0" style="padding:0 0 12px">'
          '<tr><td style="padding:8px 0;border-top:1px solid %s">'
          '<span style="font-family:%s;font-size:11px;font-weight:700;'
          'color:%s;letter-spacing:.6px">%s</span>%s<br>'
          '<a href="%s" style="font-family:%s;font-size:17px;font-weight:700;'
          'color:%s;text-decoration:none">%s</a>'
          '<span style="font-family:%s;font-size:%dpx;color:%s">&nbsp;%s'
          '</span>%s%s%s</td></tr></table>'
          % (BORDER, FONT, col, esc(x["status"]), basis,
             esc(x["url"]), FONT, TEXT, esc(x["ticker"]),
             FONT, SMALL_PX, MUTED, esc(x["price"]["text"]),
             _line(esc("; ".join(x["reasons"])), SMALL_PX, TEXT, "4px 0 0"),
             _line(esc(x.get("technical") or ""), 13, DIM, "3px 0 0")
             if x.get("technical") else "",
             (_line(" &nbsp;·&nbsp; ".join(trig), 13, MUTED, "4px 0 0")
              if trig else "")
             + (_line(" · ".join(meta), 12, MUTED, "5px 0 0")
                if meta else "")))
    if sec.get("overflow_line"):
        out.append('<div style="padding:8px 0 0;border-top:1px solid %s">'
                   '<a href="%s" style="font-family:%s;font-size:13px;'
                   'color:%s;text-decoration:none">%s</a></div>'
                   % (BORDER, esc(sec["overflow_url"]), FONT, ACCENT,
                      esc(sec["overflow_line"])))
    for key in ("notable_line", "quiet_line"):
        if sec.get(key):
            out.append(_line(esc(sec[key]), 13, MUTED, "6px 0 0"))
    return _card(sec["title"], "".join(out), sub=sec.get("sub"))


def _contract_html(c, show_ticker=True):
    bits = []
    if show_ticker:
        bits.append("<b>%s</b>" % esc(c["ticker"]))
    bits.append("%s %s %s" % (esc(c["right"]), esc(c["strike"]),
                              esc(c["expiry"])))
    bits.append('<span style="color:%s">%s · %s</span>'
                % (DIR_COLOR.get(c["direction"], MUTED),
                   esc(c["action"]), esc(c["direction"])))
    if c.get("premium"):
        bits.append(esc(c["premium"]))
    if c.get("spot") is not None:
        bits.append("spot %s" % esc(c["spot"]))
    if c.get("sweep"):
        bits.append("sweep")
    line1 = _line(" · ".join(bits), SMALL_PX, DIM)
    sub = []
    if c.get("flow_at"):
        sub.append("printed %s" % esc(c["flow_at"]))
    oi = esc(c.get("oi_state") or "")
    if c.get("oi_as_of"):
        oi += " as of %s" % esc(c["oi_as_of"])
    if oi:
        sub.append(oi)
    return line1 + (_line(" · ".join(sub), 12, MUTED, "0 0 4px")
                    if sub else "")


def render_flow_group(sec, site):
    parts = []
    for r in sec["records"]:
        vc = {BC.BULLISH: GREEN, BC.BEARISH: RED,
              BC.MIXED_DIR: AMBER}.get(r["direction"], MUTED)
        parts.append(
            '<div style="font-family:%s;font-size:12px;color:%s;'
            'text-transform:uppercase;letter-spacing:.5px;padding:10px 0 4px;'
            'border-top:1px solid %s">'
            '<a href="%s" style="color:%s;text-decoration:none">%s</a> — '
            '<span style="color:%s;font-weight:700">%s</span> '
            '<span style="color:%s">(%s)</span></div>'
            % (FONT, MUTED, BORDER, esc(r["url"]), ACCENT, esc(r["ticker"]),
               vc, esc(r["verdict"]), MUTED, esc(r["score"])))
        parts.append(_line(esc(r["explain"]), 13, DIM, "0 0 4px"))
        for c in r["contracts"]:
            parts.append(_contract_html(c, show_ticker=False))
        if r.get("omitted_line"):
            # the verdict above reads every print; these rows are a subset,
            # and the difference is stated rather than left to be noticed
            parts.append(_line(esc(r["omitted_line"]), 12, MUTED, "2px 0 0"))
    return _card(sec["title"], "".join(parts), sub=sec.get("sub"))


FT_COLOR = {"STRONG FOLLOW-THROUGH": GREEN,
            "PARTIAL FOLLOW-THROUGH": AMBER,
            "NO NET FOLLOW-THROUGH": RED,
            "STRUCTURE UNCLEAR": MUTED, "OI PENDING": MUTED,
            "OI DATA DELAYED": AMBER, "NOT EVALUABLE": MUTED}


def render_followthrough(sec, site):
    """Yesterday's cleared open interest, against yesterday's prints.

    Thirteen fields will not fit a 620px card as columns, and forcing them
    to would push the body sideways on a phone. The numeric spine is a
    table; the provenance rides underneath each row, where it is readable
    rather than truncated.
    """
    if not sec["records"]:
        return _card(sec["title"],
                     _line(esc(sec.get("empty_line") or ""), SMALL_PX, DIM)
                     + (_line('<a href="%s" style="color:%s;'
                              'text-decoration:none">%s</a>'
                              % (esc(sec["desk_url"]), ACCENT,
                                 esc(sec.get("desk_line") or "")),
                              13, MUTED, "6px 0 0")
                        if sec.get("desk_line") else ""),
                     sub=sec.get("sub"))
    rows = []
    for r in sec["records"]:
        ratio = r.get("follow_through_ratio")
        col = FT_COLOR.get(r.get("oi_state"), MUTED)
        rows.append(
            "<tr>" +
            _cell('<span style="color:%s">%s</span>' % (MUTED, r.get("rank")),
                  extra="white-space:nowrap") +
            _cell('<a href="%s" style="color:%s;text-decoration:none">'
                  "<b>%s</b></a> %s %s %s"
                  % (esc(r["url"]), ACCENT, esc(r["ticker"]),
                     esc((r.get("right") or "").upper()), esc(r.get("strike")),
                     esc(r.get("expiry")))) +
            _cell(_fmt_int(r.get("delta_oi"), signed=True), align="right") +
            _cell(("%d%%" % round(ratio * 100)) if ratio is not None else "—",
                  align="right") +
            _cell('<span style="color:%s">%s</span>'
                  % (col, esc(r.get("oi_state") or "")), align="right",
                  extra="white-space:nowrap") + "</tr>")
        # everything the columns could not hold, kept whole
        detail = ("%s · %s &nbsp;·&nbsp; observed %s contracts · %s "
                  "&nbsp;·&nbsp; OI %s → %s &nbsp;·&nbsp; %s (%s confidence)"
                  % (esc(r.get("action") or "—"), esc(r.get("direction") or ""),
                     _fmt_int(r.get("observed_contracts")),
                     esc(_prem(r.get("premium"))),
                     _fmt_int(r.get("oi_before")), _fmt_int(r.get("oi_after")),
                     esc(r.get("structure") or ""),
                     esc(r.get("structure_confidence") or "")))
        stamps = "Flow %s &nbsp;·&nbsp; %s EOD OI &nbsp;·&nbsp; verified %s" % (
            esc(r.get("flow_at") or "—"), esc(r.get("oi_data_date") or "—"),
            esc(r.get("oi_verified_at") or "—"))
        rows.append('<tr><td colspan="5" style="padding:0 10px 8px">%s%s'
                    "</td></tr>"
                    % (_line(detail, 12, DIM, "0"),
                       _line(stamps, 12, MUTED, "1px 0 0")))
    head = ("<tr>" + "".join(
        '<th align="%s" style="padding:4px 10px;font-family:%s;font-size:12px;'
        'color:%s;text-transform:uppercase;letter-spacing:.4px;'
        'font-weight:600">%s</th>' % (a, FONT, MUTED, h)
        for h, a in (("#", "left"), ("Contract", "left"), ("ΔOI", "right"),
                     ("Follow", "right"), ("Status", "right"))) + "</tr>")
    tail = _line('<a href="%s" style="color:%s;text-decoration:none">%s →</a>'
                 % (esc(sec["desk_url"]), ACCENT,
                    esc(sec.get("desk_line") or "See the full table")),
                 SMALL_PX, ACCENT, "10px 0 0")
    return _card(sec["title"],
                 '<table role="presentation" width="100%%" cellpadding="0" '
                 'cellspacing="0" border="0">%s%s</table>%s'
                 % (head, "".join(rows), tail), sub=sec.get("sub"))


def _fmt_int(n, signed=False):
    if n is None:
        return "—"
    # "%+,d" is not a valid printf spec; format() carries both flags
    return format(int(n), "+," if signed else ",")


def _prem(p):
    if p is None:
        return "—"
    p = float(p)
    return ("$%.1fM" % (p / 1e6)) if p >= 1e6 else "$%dK" % round(p / 1e3)


def render_earnings(sec, site):
    rows = []
    for r in sec["records"]:
        star = ("<span style=\"color:%s\">★</span> " % ACCENT) \
            if r.get("on_watchlist") else ""
        imp = ("±%.1f%%" % r["implied_move_pct"]) \
            if r.get("implied_move_pct") is not None else "n/a"
        iv = ("%.0f%%" % r["iv_pct"]) if r.get("iv_pct") is not None else "n/a"
        real = ("±%.1f%%" % r["realized_med_pct"]) \
            if r.get("realized_med_pct") is not None else "n/a"
        vcol = {"RICH": RED, "CHEAP": GREEN}.get(r.get("verdict"), MUTED)
        rows.append(
            "<tr>" +
            _cell('%s<a href="%s" style="color:%s;text-decoration:none">'
                  "<b>%s</b></a>" % (star, esc(r["url"]), ACCENT,
                                     esc(r["ticker"]))) +
            _cell(esc(r["session"]), align="right",
                  extra="white-space:nowrap") +
            _cell(esc(imp), align="right") +
            _cell(esc(iv), align="right") +
            _cell(esc(real), align="right") +
            _cell('<span style="color:%s">%s</span>'
                  % (vcol, esc(r.get("verdict") or "—")), align="right",
                  extra="white-space:nowrap") + "</tr>")
    head = ("<tr>" + "".join(
        '<th align="%s" style="padding:4px 10px;font-family:%s;font-size:12px;'
        'color:%s;text-transform:uppercase;letter-spacing:.4px;'
        'font-weight:600">%s</th>' % (a, FONT, MUTED, h)
        for h, a in (("", "left"), ("When", "right"), ("Implied", "right"),
                     ("IV", "right"), ("Typical", "right"),
                     ("Read", "right"))) + "</tr>")
    note = _line(esc(sec.get("note") or ""), 12, MUTED, "8px 0 0") \
        if sec.get("note") else ""
    return _card(sec["title"],
                 '<table role="presentation" width="100%%" cellpadding="0" '
                 'cellspacing="0" border="0">%s%s</table>%s'
                 % (head, "".join(rows), note), sub=sec.get("sub"))


def render_flow_flat(sec, site):
    return _card(sec["title"],
                 "".join(_contract_html(c) for c in sec["records"]),
                 sub=sec.get("sub"))


def render_link(sec, site):
    r = sec["records"][0]
    return ('<tr><td style="padding:0 0 14px"><a href="%s" '
            'style="font-family:%s;font-size:%dpx;color:%s;'
            'text-decoration:none">%s →</a></td></tr>'
            % (esc(r["url"]), FONT, SMALL_PX, ACCENT, esc(r["text"])))


def render_event(sec, site):
    rows = []
    for e in sec["records"]:
        sc = {"COMPLETED": MUTED, "IN PROGRESS": AMBER,
              "UNSCHEDULED": MUTED}.get(e["status"], ACCENT)
        prov = []
        if e.get("source_time") and e.get("source_tz"):
            prov.append("%s %s" % (e["source_time"], e["source_tz"]))
        if e.get("venue"):
            prov.append(e["venue"])
        title = esc(e["title"])
        if e.get("url"):
            title = '<a href="%s" style="color:%s;text-decoration:none">%s</a>' \
                % (esc(e["url"]), TEXT, title)
        rows.append(
            "<tr>" + _cell(esc(e["time_et"]), extra="white-space:nowrap") +
            _cell("<b>%s</b>%s" % (title,
                                   ('<div style="font-size:12px;color:%s">'
                                    'from %s</div>' % (MUTED,
                                                       esc(" · ".join(prov))))
                                   if prov else "")) +
            _cell('<span style="color:%s">%s</span>' % (sc, esc(e["status"])),
                  align="right", extra="white-space:nowrap") + "</tr>")
    return _card(sec["title"],
                 '<table role="presentation" width="100%%" cellpadding="0" '
                 'cellspacing="0" border="0">%s</table>' % "".join(rows),
                 sub=sec.get("sub"))


def render_news(sec, site):
    if not sec["records"]:
        return _card(sec["title"], _line(esc(sec.get("empty_line") or ""),
                                         SMALL_PX, DIM), sub=sec.get("sub"))
    parts, scope = [], None
    for it in sec["records"]:
        if it["scope"] != scope:
            scope = it["scope"]
            parts.append(_subhead("Market" if scope == "market"
                                  else "Your names"))
        tickers = (" · " + ", ".join(it["tickers"])) if it.get("tickers") \
            else ""
        parts.append(
            '<div style="padding:4px 0 8px">'
            '<a href="%s" style="font-family:%s;font-size:%dpx;color:%s;'
            'text-decoration:none;font-weight:600">%s</a>%s%s</div>'
            % (esc(it["url"] or site), FONT, SMALL_PX, ACCENT,
               esc(it["headline"]),
               _line("%s · %s · %s%s" % (esc(it["source"]),
                                         esc(it["published_et"]),
                                         esc(it["tier"]), esc(tickers)),
                     13, MUTED, "2px 0 0"),
               _line(esc(it["why"]), 13, DIM, "0")))
    return _card(sec["title"], "".join(parts), sub=sec.get("sub"))


def render_prose(sec, site):
    return _card(sec["title"],
                 "".join(_line(esc(r["text"]), SMALL_PX, DIM)
                         for r in sec["records"]), sub=sec.get("sub"))


def render_discovery(sec, site):
    parts = []
    for x in sec["records"]:
        meta = [x[k] for k in ("contract", "side_label", "premium",
                               "oi_state") if x.get(k)]
        parts.append(
            '<div style="padding:4px 0 8px">'
            '<span style="font-family:%s;font-size:12px;color:%s">#%d</span> '
            '<a href="%s" style="font-family:%s;font-size:%dpx;color:%s;'
            'text-decoration:none;font-weight:700">%s</a>%s%s</div>'
            % (FONT, MUTED, x["rank"], esc(x["url"]), FONT, SMALL_PX, ACCENT,
               esc(x["ticker"]),
               _line(esc(" · ".join(meta)), 13, MUTED, "2px 0 0"),
               _line(esc(x["why"]), 13, DIM, "0")))
    return _card(sec["title"], "".join(parts), sub=sec.get("sub"))


RENDERERS = {
    "index": render_market, "watch": render_watchlist,
    "flow_group": render_flow_group, "flow_flat": render_flow_flat,
    "link": render_link, "event": render_event, "news": render_news,
    "prose": render_prose, "discovery": render_discovery,
    "earnings": render_earnings, "followthrough": render_followthrough,
}


# ── document ────────────────────────────────────────────────────────────

def render(model, *, preheader=None):
    """Everything comes from the model, including the preheader:
    passing it in separately let the envelope and the rendered
    document disagree about what the reader saw."""
    site = model["meta"]["site"]
    unsub = model["meta"]["unsub"]
    if preheader is None:
        preheader = model["meta"].get("preheader") or ""
    body = "".join(RENDERERS[s["kind"]](s, site) for s in model["sections"]
                   if s["kind"] in RENDERERS
                   and (s.get("records") or s.get("empty_line")))

    cta = ('<tr><td align="center" style="padding:4px 0 18px">'
           '<a href="%s/#desk" style="display:inline-block;padding:12px 26px;'
           'background:%s;color:#04122e;font-family:%s;font-size:%dpx;'
           'font-weight:700;text-decoration:none;border-radius:8px">'
           'Open your desk</a></td></tr>' % (site, ACCENT, FONT, BODY_PX))
    foot = ('<tr><td style="padding:6px 0 0;font-family:%s;font-size:12px;'
            'color:%s;line-height:1.6">TickerDesk · educational research, '
            'not investment advice. Delayed market data.<br>'
            '<a href="%s" style="color:%s">Unsubscribe</a> · '
            '<a href="%s/#settings" style="color:%s">Email settings</a> · '
            'Reply to this email to reach a human.</td></tr>'
            # no fallback: an unsubscribe link that is not the subscriber's
            # signed endpoint is worse than a missing one, because it looks
            # like it worked. The send gate blocks when this is empty.
            % (FONT, MUTED, esc(unsub), MUTED, site, MUTED))
    pre = ('<div style="display:none;max-height:0;overflow:hidden;'
           'mso-hide:all;font-size:1px;line-height:1px;color:%s;'
           'opacity:0">%s</div>' % (BG, esc(preheader)))

    return (
      '<!doctype html><html lang="en"><head>'
      '<meta charset="utf-8">'
      '<meta name="viewport" content="width=device-width,initial-scale=1">'
      '<meta name="color-scheme" content="dark light">'
      '<meta name="supported-color-schemes" content="dark light">'
      '<title>TickerDesk Brief</title>'
      '<style>'
      'table{border-collapse:collapse}'
      'img{max-width:100%%}'
      '@media only screen and (max-width:600px){'
      '.wrap{width:100%%!important;max-width:100%%!important}'
      '.pad{padding:12px!important}'
      'table.stack td{display:block!important;width:100%%!important}'
      # six numeric columns do not fit a 320px viewport; 1W and YTD move
      # under the ticker rather than forcing the body to scroll sideways
      '.wk{display:none!important}'
      '.mo{display:block!important}}'
      '</style></head>'
      '<body style="margin:0;padding:0;background:%s;" bgcolor="%s">%s'
      '<table role="presentation" width="100%%" cellpadding="0" cellspacing="0"'
      ' border="0" bgcolor="%s" style="background:%s"><tr>'
      '<td align="center" class="pad" style="padding:20px 12px">'
      '<table role="presentation" class="wrap" width="620" cellpadding="0"'
      ' cellspacing="0" border="0" style="width:620px;max-width:620px">'
      '<tr><td style="padding:0 0 14px;font-family:%s;font-size:18px;'
      'font-weight:800;color:%s">TickerDesk</td></tr>'
      '%s%s%s</table></td></tr></table></body></html>'
      % (BG, BG, pre, BG, BG, FONT, TEXT, body, cta, foot))


# ── QA helpers ──────────────────────────────────────────────────────────

def stats(html_doc):
    text = BC.visible_text(html_doc)
    return {"bytes": len(html_doc.encode("utf-8")), "words": len(text.split()),
            "links": len(re.findall(r'href="', html_doc))}


def _demo_model():
    import brief_model as M
    market = {"indices": {
        "SPY": {"last": 748.28, "chg_1d_pct": 0.83, "chg_1w_pct": -0.47,
                "chg_ytd_pct": 9.53, "dist_ma20_pct": 0.4},
        "QQQ": {"last": 708.97, "chg_1d_pct": 1.85, "chg_1w_pct": -1.49,
                "chg_ytd_pct": 15.63, "dist_ma20_pct": -0.8},
        "IWM": {"last": 296.54, "chg_1d_pct": 1.45, "chg_1w_pct": 0.69,
                "chg_ytd_pct": 19.2, "dist_ma20_pct": -0.02},
        "DIA": {"last": 521.51, "chg_1d_pct": 0.69, "chg_1w_pct": -0.61,
                "chg_ytd_pct": 7.83, "dist_ma20_pct": -0.3}},
        "regime": {"label": "RISK-OFF", "why": "33% of the 700-name universe "
                   "is above its 20-day average"},
        "session_label": "Pre-Market Brief",
        "as_of_et": "2026-07-22 07:20 ET",
        "events": [{"title": "Crude Oil Inventories",
                    "time_et": "10:30 a.m. ET", "status": "UPCOMING",
                    "source_time": "2:30pm", "source_tz": "UTC"}]}
    wl = BC.rank_watchlist([
        {"ticker": "GEV", "grade_delta": 1, "grade_from": "B",
         "grade_to": "A-", "has_flow": True, "flow_direction": BC.BEARISH,
         "flow_short_dated": True, "earnings_in_days": 1,
         "earnings_confirmed": True, "price": 1078.81,
         "price_record": BC.price_record(1078.81, BC.BASIS_CLOSE, "Jul 21")},
        {"ticker": "PM", "grade_delta": -1, "grade_from": "B+",
         "grade_to": "B", "has_flow": True, "flow_direction": BC.BEARISH,
         "price": 188.04,
         "price_record": BC.price_record(188.04, BC.BASIS_CLOSE, "Jul 21")},
        {"ticker": "Q1"}, {"ticker": "Q2"}])

    def con(tk, right, strike, side, prem, sweep=False):
        return {"ticker": tk, "right": right, "strike": strike,
                "expiry": "2026-08-21", "side": side, "premium": prem,
                "premium_raw": 2e6, "spot": 100.0,
                "printed_at": "2026-07-21T19:44:00Z", "is_sweep": sweep,
                "oi_state": BC.CONF_PENDING}
    return M.build(
        market, wl,
        market_flow=[con("IBM", "call", 250, "call_buyer", "$4.9M"),
                     con("META", "put", 625, "put_buyer", "$3.0M", True)],
        watch_flow={"GEV": [con("GEV", "put", 930, "put_buyer", "$0.2M")],
                    "PM": [con("PM", "put", 175, "put_buyer", "$0.4M")],
                    "TSLA": [con("TSLA", "call", 380, "mixed", "$42.8M")]},
        news={"market": [{"headline": "Fed official flags inflation risk",
                          "source": "Reuters",
                          "published_et": "2026-07-22 06:10 ET",
                          "tier": "SECONDARY", "why": "Rates path in doubt.",
                          "url": "https://reuters.com/x"}],
              "watchlist": [], "empty": False,
              "empty_line": "No high-relevance headlines since the previous "
                            "brief."},
        discovery=[{"ticker": "CLF", "contract": "PUT 8 2027-03-19",
                    "side_label": "PUT BUY · bearish", "premium": "$2.0M",
                    "oi_state": BC.CONF_PENDING,
                    "why": "largest print outside your list"}],
        weekly={"line": "Breadth fell for a third week.",
                "sub": "Five-session view"},
        site="https://tickerdesk.io",
        unsub="https://api.tickerdesk.io/unsubscribe?u=abc&t=sig")


def self_test():
    fails, ran = [], [0]

    def chk(n, c, d=""):
        ran[0] += 1
        print(("  PASS  " if c else "  FAIL  ") + n
              + ("" if c else "  <- %s" % (d,)))
        if not c:
            fails.append(n)

    model = _demo_model()
    pre = "SPY +0.83% QQQ +1.85% · GEV grade improved."
    doc = render(model, preheader=pre)
    st = stats(doc)
    body = BC.visible_text(doc)

    chk("renders a full HTML document", doc.startswith("<!doctype html>"))
    chk("exactly one document", BC.check_document(doc) == [],
        BC.check_document(doc))
    chk("viewport meta present", 'name="viewport"' in doc)
    chk("dark colour-scheme declared", 'content="dark light"' in doc)
    chk("dark background painted with bgcolor for Outlook",
        'bgcolor="%s"' % BG in doc)
    chk("body text >= 14px", "font-size:%dpx" % BODY_PX in doc)
    chk("mobile stacking media query present", "max-width:600px" in doc)
    chk("preheader hidden", "mso-hide:all" in doc)
    chk("market precedes watch list",
        body.index("Market in 30 seconds") < body.index("Your watch list"))
    chk("watch list precedes flow",
        body.index("Your watch list") < body.index("Driving today"))
    chk("flow precedes news",
        body.index("Driving today") < body.index("Top news"))
    chk("regime label shown", "RISK-OFF" in body)
    chk("20-day state summary present", "their 20-day averages" in body, body[:0])
    chk("driving-flow section names the ranked tickers",
        "Driving today" in body and "GEV" in body and "PM" in body)
    chk("unresolved flow filed as other", "Other notable watch-list flow"
        in body)
    chk("contract states side and direction",
        "PUT BUY · bearish" in body, body[:0])
    chk("two-sided print is labelled unresolved",
        "TWO-SIDED · unresolved" in body, body[:0])
    chk("OI state shown per contract", "OI PENDING" in body)
    chk("flow quality replaces the ambiguous Signal label",
        "Signal " not in body, body[:0])
    chk("price carries its basis", "Jul 21 close" in body, body[:0])
    chk("news section present with a real headline",
        "Fed official flags inflation risk" in body)
    chk("news carries source tier", "SECONDARY" in body)
    chk("weekly lens renders", "Breadth fell" in body)
    chk("discovery ranked and detailed",
        "#1" in body and "CLF" in body and "$2.0M" in body)
    chk("one primary CTA", doc.count("Open your desk") == 1)
    chk("unsubscribe is the signed endpoint",
        BC.check_unsubscribe(doc, model["meta"]["unsub"]) == [],
        BC.check_unsubscribe(doc, model["meta"]["unsub"]))
    chk("no negative zero", BC.check_no_negative_zero(body) == [],
        BC.check_no_negative_zero(body))
    chk("no unescaped template holes",
        BC.check_no_placeholders(doc) == [], BC.check_no_placeholders(doc))
    chk("links within budget", 8 <= st["links"] <= 40, st["links"])
    chk("size under the Gmail clip limit", st["bytes"] < 102 * 1024,
        st["bytes"])

    empty = render(_demo_model(), preheader=pre)
    chk("render is deterministic for one model", empty == doc)

    print("\n%d/%d checks passed" % (ran[0] - len(fails), ran[0]))
    return 1 if fails else 0


def main():
    if "--demo" in sys.argv:
        doc = render(_demo_model(), preheader="demo")
        p = "docs/email-previews/render_demo.html"
        with open(p, "w", encoding="utf-8") as f:
            f.write(doc)
        print("wrote %s  %s" % (p, stats(doc)))
        return 0
    return self_test()


if __name__ == "__main__":
    raise SystemExit(main())
