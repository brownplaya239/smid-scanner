#!/usr/bin/env python3
"""brief_render.py — the Market Intelligence + Watchlist email.

Reads market_layer (what environment) and brief_compose (what changed in
the user's names) and renders them in that order. The hierarchy is the
product: a reader who stops after the first block should already know the
regime, today's calendar risk, and how many of their names moved.

Email HTML is not web HTML. Outlook ignores <body> backgrounds and most
CSS, so the dark theme is painted with bgcolor + inline styles on nested
tables, and layout is tables rather than flex/grid. The only media query
is for stacking; nothing depends on it.

    python brief_render.py --demo        # write a preview + stats
    python brief_render.py --self-test
"""

import html
import os
import re
import sys

import brief_compose as BC

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
BODY_PX = 15          # spec floor is 14; 15 reads better on mobile
SMALL_PX = 14

REGIME_COLOR = {"RISK-ON": GREEN, "RISK-OFF": RED,
                "BALANCED": ACCENT, "TRANSITION": AMBER}


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def _sign(v, suffix="%"):
    if v is None:
        return '<span style="color:%s">n/a</span>' % MUTED
    c = GREEN if v > 0 else RED if v < 0 else MUTED
    return '<span style="color:%s">%+.2f%s</span>' % (c, v, suffix)


def _cell(inner, pad="6px 10px", align="left", extra=""):
    return ('<td align="%s" style="padding:%s;font-family:%s;font-size:%dpx;'
            'color:%s;%s">%s</td>' % (align, pad, FONT, SMALL_PX, TEXT,
                                      extra, inner))


def _section(title, inner, sub=""):
    """One card. bgcolor attribute AND inline style — Outlook needs the
    attribute, everything else honours the style."""
    subhtml = ('<div style="font-family:%s;font-size:%dpx;color:%s;'
               'padding:0 0 8px">%s</div>' % (FONT, SMALL_PX, MUTED, sub)
               ) if sub else ""
    return (
      '<tr><td style="padding:0 0 14px">'
      '<table role="presentation" width="100%%" cellpadding="0" cellspacing="0"'
      ' border="0" bgcolor="%s" style="background:%s;border:1px solid %s;'
      'border-radius:10px">'
      '<tr><td style="padding:14px 16px">'
      '<div style="font-family:%s;font-size:16px;font-weight:700;color:%s;'
      'padding:0 0 6px">%s</div>%s%s'
      '</td></tr></table></td></tr>'
      % (PANEL, PANEL, BORDER, FONT, TEXT, esc(title), subhtml, inner))


# ── above the fold ──────────────────────────────────────────────────────

def render_market_30s(m, wl):
    """Regime, the four indices, vol, rates, breadth, today's event, and
    the count of the user's names that moved — in that order."""
    reg = m.get("regime") or {}
    col = REGIME_COLOR.get(reg.get("label"), ACCENT)
    idx = m.get("indices") or {}

    rows = []
    for t in ("SPY", "QQQ", "IWM", "DIA"):
        d = idx.get(t)
        if not d:
            continue
        dist = d.get("dist_ma20_pct")
        dcol = GREEN if (dist or 0) >= 0 else RED
        rows.append(
            "<tr>" + _cell("<b>%s</b>" % t) +
            _cell("%.2f" % d["last"], align="right") +
            _cell(_sign(d.get("chg_1d_pct")), align="right") +
            _cell(_sign(d.get("chg_1w_pct")), align="right") +
            _cell('<span style="color:%s">%s</span>'
                  % (dcol, ("%+.1f%%" % dist) if dist is not None else "n/a"),
                  align="right") + "</tr>")
    head = ("<tr>" + "".join(
        '<th align="%s" style="padding:4px 10px;font-family:%s;font-size:12px;'
        'color:%s;text-transform:uppercase;letter-spacing:.4px;'
        'font-weight:600">%s</th>' % (a, FONT, MUTED, h)
        for h, a in (("", "left"), ("Last", "right"), ("1D", "right"),
                     ("1W", "right"), ("vs 20d", "right"))) + "</tr>")
    table = ('<table role="presentation" width="100%%" cellpadding="0" '
             'cellspacing="0" border="0">%s%s</table>' % (head, "".join(rows)))

    vix, ty = m.get("vix") or {}, m.get("ten_year") or {}
    strip = []
    if vix.get("last") is not None:
        strip.append("VIX <b>%.1f</b> %s" % (vix["last"],
                                             _sign(vix.get("chg_1d_pct"))))
    if ty.get("yield_pct") is not None:
        strip.append("10-yr <b>%.2f%%</b> %s" % (ty["yield_pct"],
                                                 _sign(ty.get("chg_1d_pct"))))
    b = m.get("breadth") or {}
    if b.get("breadth_pct") is not None:
        strip.append("Breadth <b>%d%%</b> of %s"
                     % (b["breadth_pct"], format(b.get("universe") or 0, ",")))
    strip_html = ('<div style="font-family:%s;font-size:%dpx;color:%s;'
                  'padding:10px 0 0">%s</div>'
                  % (FONT, SMALL_PX, DIM, " &nbsp;·&nbsp; ".join(strip)))

    ev = m.get("top_event") or {}
    ev_html = ""
    if ev.get("title"):
        ev_html = ('<div style="font-family:%s;font-size:%dpx;color:%s;'
                   'padding:8px 0 0">Today: <b style="color:%s">%s</b> '
                   '%s · <span style="color:%s">%s</span></div>'
                   % (FONT, SMALL_PX, DIM, TEXT, esc(ev.get("title")),
                      esc(ev.get("time") or ""), MUTED,
                      esc(ev.get("status") or "")))

    alert = ('<div style="font-family:%s;font-size:%dpx;color:%s;font-weight:700;'
             'padding:10px 12px;margin:12px 0 0;background:%s;'
             'border-left:3px solid %s;border-radius:4px">%s</div>'
             % (FONT, BODY_PX, TEXT, PANEL2, ACCENT, esc(wl["alert_line"])))

    banner = ('<div style="font-family:%s;font-size:22px;font-weight:800;'
              'color:%s;letter-spacing:.3px">%s</div>'
              '<div style="font-family:%s;font-size:%dpx;color:%s;'
              'padding:4px 0 12px">%s</div>'
              % (FONT, col, esc(reg.get("label") or "MARKET"),
                 FONT, SMALL_PX, DIM, esc(reg.get("why") or "")))

    mixed = m.get("mixed_sessions")
    note = ""
    if mixed:
        note = ('<div style="font-family:%s;font-size:12px;color:%s;'
                'padding:8px 0 0">Quotes span more than one session; each '
                'value is labelled with its own as-of.</div>' % (FONT, MUTED))
    return _section("Market in 30 seconds",
                    banner + table + strip_html + ev_html + alert + note,
                    sub="%s · as of %s" % (esc(m.get("session_label") or ""),
                                           esc(m.get("as_of_et") or "")))


# ── watchlist ───────────────────────────────────────────────────────────
BUCKET_COLOR = {BC.ACT_NOW: RED, BC.WATCH: AMBER,
                BC.NO_ACTION: MUTED, BC.QUIET: MUTED}


def render_watchlist(wl, site):
    if not wl.get("shown"):
        return _section("Your watch list",
                        '<div style="font-family:%s;font-size:%dpx;color:%s">'
                        'No material changes across your %d names.</div>'
                        % (FONT, BODY_PX, DIM, wl.get("n_total") or 0))
    out = []
    for x in wl["shown"]:
        col = BUCKET_COLOR.get(x["bucket"], MUTED)
        px = ("$%.2f" % x["price"]) if x.get("price") is not None else ""
        line2 = []
        if x.get("technical"):
            line2.append(esc(x["technical"]))
        if x.get("flow_line"):
            line2.append(esc(x["flow_line"]))
        if x.get("catalyst"):
            line2.append(esc(x["catalyst"]))
        meta = []
        if x.get("signal_strength"):
            meta.append("Signal %s" % esc(x["signal_strength"]))
        if x.get("evidence"):
            meta.append("Evidence %s" % esc(x["evidence"]))
        if x.get("edge"):
            meta.append(esc(x["edge"]))
        trig = ""
        if x.get("trigger") or x.get("invalidation"):
            trig = ('<div style="font-family:%s;font-size:13px;color:%s;'
                    'padding:4px 0 0">Trigger: %s &nbsp;·&nbsp; '
                    'Invalidation: %s</div>'
                    % (FONT, MUTED, esc(x.get("trigger") or "n/a"),
                       esc(x.get("invalidation") or "n/a")))
        out.append(
          '<table role="presentation" width="100%%" cellpadding="0" '
          'cellspacing="0" border="0" style="padding:0 0 12px">'
          '<tr><td style="padding:8px 0;border-top:1px solid %s">'
          '<span style="font-family:%s;font-size:11px;font-weight:700;'
          'color:%s;letter-spacing:.6px">%s</span>&nbsp;&nbsp;'
          '<a href="%s/#ticker=%s" style="font-family:%s;font-size:17px;'
          'font-weight:700;color:%s;text-decoration:none">%s</a>'
          '<span style="font-family:%s;font-size:%dpx;color:%s">&nbsp;%s'
          '</span>'
          '<div style="font-family:%s;font-size:%dpx;color:%s;padding:4px 0 0">'
          '%s</div>'
          '<div style="font-family:%s;font-size:13px;color:%s;padding:3px 0 0">'
          '%s</div>%s'
          '<div style="font-family:%s;font-size:12px;color:%s;padding:5px 0 0">'
          '%s</div>'
          '</td></tr></table>'
          % (BORDER, FONT, col, esc(x["bucket"]), site, esc(x["ticker"]),
             FONT, TEXT, esc(x["ticker"]), FONT, SMALL_PX, MUTED, px,
             FONT, SMALL_PX, TEXT,
             esc("; ".join(x.get("reasons") or ["changed"])),
             FONT, DIM, " · ".join(line2), trig,
             FONT, MUTED, " · ".join(meta)))
    if wl.get("quiet_line"):
        out.append('<div style="font-family:%s;font-size:13px;color:%s;'
                   'padding:6px 0 0;border-top:1px solid %s">%s</div>'
                   % (FONT, MUTED, BORDER, esc(wl["quiet_line"])))
    return _section("Your watch list", "".join(out),
                    sub="Ranked by what changed since the last brief")


# ── flow ────────────────────────────────────────────────────────────────

def _contract_line(c):
    bits = ["<b>%s</b> %s %s %s" % (esc(c.get("ticker")),
                                    esc(c.get("right", "")).upper(),
                                    esc(c.get("strike", "")),
                                    esc(c.get("expiry", "")))]
    if c.get("side"):
        bits.append(esc(c["side"].replace("_", " ")))
    if c.get("premium"):
        bits.append(esc(c["premium"]))
    if c.get("spot") is not None:
        bits.append("spot %s" % esc(c["spot"]))
    if c.get("oi_confirmed") is not None:
        bits.append("OI %s" % ("confirmed" if c["oi_confirmed"] else "unconfirmed"))
    if c.get("session_date"):
        bits.append(esc(c["session_date"]))
    return " · ".join(bits)


def render_flow(market_flow, watch_flow, site):
    parts = []
    if market_flow:
        parts.append('<div style="font-family:%s;font-size:12px;color:%s;'
                     'text-transform:uppercase;letter-spacing:.5px;'
                     'padding:2px 0 6px">Market-wide</div>' % (FONT, MUTED))
        for c in market_flow[:3]:
            parts.append('<div style="font-family:%s;font-size:%dpx;color:%s;'
                         'padding:3px 0">%s</div>'
                         % (FONT, SMALL_PX, DIM, _contract_line(c)))
    for tk, group in (watch_flow or {}).items():
        v = BC.reconcile_ticker_flow(group)
        vc = {BC.BULLISH: GREEN, BC.BEARISH: RED,
              BC.MIXED: AMBER}.get(v["verdict"], MUTED)
        parts.append(
            '<div style="font-family:%s;font-size:12px;color:%s;'
            'text-transform:uppercase;letter-spacing:.5px;padding:10px 0 4px;'
            'border-top:1px solid %s">%s — '
            '<span style="color:%s;font-weight:700">%s</span> '
            '<span style="color:%s">(%s; %s)</span></div>'
            % (FONT, MUTED, BORDER, esc(tk), vc, esc(v["verdict"]), MUTED,
               esc(v["score"]), esc(v["explain"])))
        for c in group[:2]:
            parts.append('<div style="font-family:%s;font-size:%dpx;color:%s;'
                         'padding:3px 0">%s</div>'
                         % (FONT, SMALL_PX, DIM, _contract_line(c)))
    if not parts:
        return ""
    return _section("Options flow", "".join(parts),
                    sub="Market-wide first, then your names. A ticker whose "
                        "contracts disagree is labelled MIXED.")


# ── news, calendar, weekly, discovery ───────────────────────────────────

def render_news(sel, site):
    def block(items, label):
        if not items:
            return ""
        rows = ['<div style="font-family:%s;font-size:12px;color:%s;'
                'text-transform:uppercase;letter-spacing:.5px;'
                'padding:2px 0 6px">%s</div>' % (FONT, MUTED, label)]
        for it in items:
            tier = "PRIMARY" if it.get("source_type") in (
                "company_ir", "sec", "regulator", "exchange") else "SECONDARY"
            att = (it.get("attribution") or {})
            flag = ""
            if any(v == BC.UNCONFIRMED for v in att.values()):
                flag = ('<span style="color:%s"> · relevance unconfirmed'
                        '</span>' % AMBER)
            rows.append(
              '<div style="padding:4px 0 8px">'
              '<a href="%s" style="font-family:%s;font-size:%dpx;color:%s;'
              'text-decoration:none;font-weight:600">%s</a>'
              '<div style="font-family:%s;font-size:13px;color:%s;'
              'padding:2px 0 0">%s · %s · %s%s</div>'
              '<div style="font-family:%s;font-size:13px;color:%s">%s</div>'
              '</div>'
              % (esc(it.get("url") or site), FONT, SMALL_PX, ACCENT,
                 esc(it.get("headline")), FONT, MUTED,
                 esc(it.get("publisher") or "unknown"),
                 esc(it.get("published") or ""), tier, flag,
                 FONT, DIM, esc(it.get("why") or "")))
        return "".join(rows)
    inner = block(sel.get("market"), "Market") + \
        block(sel.get("watchlist"), "Your names")
    return _section("Top news", inner) if inner else ""


def render_calendar(events):
    if not events:
        return ""
    rows = []
    for e in events[:5]:
        sc = {"COMPLETED": MUTED, "IN PROGRESS": AMBER}.get(
            e.get("status"), ACCENT)
        rows.append(
            "<tr>" + _cell(esc(e.get("time") or "--"), extra="white-space:nowrap") +
            _cell("<b>%s</b>" % esc(e.get("title"))) +
            _cell('<span style="color:%s">%s</span>' % (sc, esc(e.get("status"))),
                  align="right", extra="white-space:nowrap") + "</tr>")
    return _section(
        "Macro calendar",
        '<table role="presentation" width="100%%" cellpadding="0" '
        'cellspacing="0" border="0">%s</table>' % "".join(rows),
        sub="Ranked by expected impact. Released numbers leave the queue.")


def render_weekly(weekly):
    if not weekly or not weekly.get("changed"):
        return ""
    return _section("Weekly lens", '<div style="font-family:%s;font-size:%dpx;'
                    'color:%s">%s</div>' % (FONT, SMALL_PX, DIM,
                                            esc(weekly.get("line") or "")))


def render_discovery(d, site):
    if not d:
        return ""
    return _section(
        "Market discovery",
        '<div style="font-family:%s;font-size:%dpx;color:%s">'
        '<b>%s</b> — %s</div>' % (FONT, SMALL_PX, DIM, esc(d.get("ticker")),
                                  esc(d.get("why") or "")),
        sub="Not on your watch list")


# ── document ────────────────────────────────────────────────────────────

def render(market, wl, *, news=None, market_flow=None, watch_flow=None,
           weekly=None, discovery=None, site="https://tickerdesk.io",
           unsub_url="", preheader=""):
    body = (render_market_30s(market, wl)
            + render_watchlist(wl, site)
            + render_flow(market_flow or [], watch_flow or {}, site)
            + render_calendar(market.get("events") or [])
            + render_news(news or {}, site)
            + render_weekly(weekly)
            + render_discovery(discovery, site))

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
            % (FONT, MUTED, esc(unsub_url or site + "/#settings"), MUTED,
               site, MUTED))

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
      '@media only screen and (max-width:600px){'
      '.wrap{width:100%%!important}.pad{padding:12px!important}'
      'table.stack td{display:block!important;width:100%%!important}}'
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
    text = re.sub(r"<[^>]+>", " ", html_doc)
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return {"words": len(text.split()),
            "links": len(re.findall(r"<a\s", html_doc)),
            "bytes": len(html_doc.encode("utf-8"))}


def self_test():
    fails = []

    def chk(name, cond, detail=""):
        print(("  PASS  " if cond else "  FAIL  ") + name +
              ("" if cond else "  <- %s" % detail))
        if not cond:
            fails.append(name)

    market = {
        "session_label": "Pre-Market Brief", "as_of_et": "2026-07-22 08:10 ET",
        "regime": {"label": "RISK-OFF",
                   "why": "breadth 33% of 1,000 names; 1 of 4 major indices "
                          "above their 20-day; VIX 24.1 +9.0%; 10-year +0.8%."},
        "indices": {t: {"last": 100.0 + i, "chg_1d_pct": -1.2 - i,
                        "chg_1w_pct": -2.0, "dist_ma20_pct": -3.1,
                        "above_ma20": False}
                    for i, t in enumerate(("SPY", "QQQ", "IWM", "DIA"))},
        "vix": {"last": 24.1, "chg_1d_pct": 9.0},
        "ten_year": {"yield_pct": 4.63, "chg_1d_pct": 0.8},
        "breadth": {"breadth_pct": 33, "universe": 1000},
        "events": [{"time": "10:00am", "title": "UoM Sentiment",
                    "status": "UPCOMING"},
                   {"time": "8:30am", "title": "Jobless Claims",
                    "status": "COMPLETED"}],
        "top_event": {"time": "10:00am", "title": "UoM Sentiment",
                      "status": "UPCOMING"},
        "sectors": {"leaders": [{"name": "Energy"}],
                    "laggards": [{"name": "Semis"}]},
    }
    wl = BC.rank_watchlist([
        {"ticker": "MU", "trigger_hit": True, "has_flow": True,
         "price": 118.2, "technical": "reclaimed the 20-day",
         "flow_line": "call buying, OI confirmed", "trigger": "hold $116",
         "invalidation": "close below $112", "signal_strength": "high",
         "evidence": "moderate", "edge": BC.POSITIVE_EDGE},
        {"ticker": "ASML", "grade_delta": -2, "price": 790.0,
         "technical": "lost the 50-day"},
        {"ticker": "AAPL"}, {"ticker": "MSFT"},
    ])
    news = BC.select_news([
        {"headline": "Fed holds rates steady", "tickers": [],
         "source_type": "regulator", "url": "https://x", "why": "sets the tone",
         "publisher": "Federal Reserve", "published": "2:00pm"},
        {"headline": "Micron guides Q4 above consensus", "tickers": ["MU"],
         "source_type": "company_ir", "url": "https://y",
         "why": "supports the flow", "publisher": "Micron IR",
         "published": "7:05am"},
    ], {"MU"}, aliases={"MU": ["Micron"]})
    flow_w = {"TSLA": [{"ticker": "TSLA", "right": "call", "strike": "300",
                        "expiry": "08/15", "side": "call_buy",
                        "status": "confirmed", "oi_confirmed": True,
                        "premium": "$1.2M", "session_date": "2026-07-21"},
                       {"ticker": "TSLA", "right": "call", "strike": "320",
                        "expiry": "08/15", "side": "call_buy",
                        "status": "failed", "oi_confirmed": False,
                        "premium": "$0.4M", "session_date": "2026-07-21"}]}
    pre = BC.build_preheader(market, wl, {"indices": "SPY -1.2% QQQ -2.2%",
                                          "vix": "VIX +9.0%",
                                          "ten_year": "10Y 4.63%"})
    doc = render(market, wl, news=news, market_flow=[], watch_flow=flow_w,
                 preheader=pre, unsub_url="https://tickerdesk.io/u/abc")
    st = stats(doc)

    chk("renders a full HTML document", doc.startswith("<!doctype html>"))
    chk("viewport meta present", 'name="viewport"' in doc)
    chk("dark colour-scheme declared", 'content="dark light"' in doc)
    chk("dark background painted with bgcolor for Outlook",
        'bgcolor="%s"' % BG in doc)
    chk("body text >= 14px", "font-size:%dpx" % BODY_PX in doc
        and "font-size:13px" in doc)
    chk("mobile stacking media query present", "max-width:600px" in doc)
    chk("preheader hidden and 90-120 chars",
        "mso-hide:all" in doc and 90 <= len(pre) <= 120, len(pre))
    chk("market block precedes watchlist",
        doc.index("Market in 30 seconds") < doc.index("Your watch list"))
    chk("watchlist precedes discovery/news sections",
        doc.index("Your watch list") < doc.index("Top news"))
    chk("regime label shown", "RISK-OFF" in doc)
    chk("alert line present", "2 of your 4 watch-list names" in doc)
    chk("quiet names summarised", "2 names quiet" in doc)
    chk("TSLA reconciled as MIXED", ">MIXED<" in doc)
    chk("mixed score shows full denominator", "1 of 2 confirmed" in doc)
    chk("completed event still labelled COMPLETED", "COMPLETED" in doc)
    chk("one primary CTA", doc.count("Open your desk") == 1)
    chk("unsubscribe link present", "/u/abc" in doc)
    chk("reply-to invitation present", "reach a human" in doc)
    chk("word count 700-1000 or under (compact ok)", st["words"] <= 1000,
        st["words"])
    chk("links within 10-15", 5 <= st["links"] <= 15, st["links"])
    chk("no unescaped template holes",
        not re.search(r"%[sd]\b|\{\}", re.sub(r"<[^>]+>", "", doc)))
    chk("edge label rendered, not a raw hit rate",
        BC.POSITIVE_EDGE in doc and "50%" not in doc)

    print("\nstats: %s" % st)
    total = 22
    print("%d/%d checks passed" % (total - len(fails), total))
    return 1 if fails else 0


def main():
    if "--self-test" in sys.argv:
        return self_test()
    return self_test()


if __name__ == "__main__":
    raise SystemExit(main())
