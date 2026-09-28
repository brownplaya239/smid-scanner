#!/usr/bin/env python3
"""social_cards.py — branded image cards for TickerDesk's social posts.

Renders from the site's own published JSON, so a card can never show a
number the site doesn't. matplotlib is used for layout because it ships
its own fonts (DejaVu), so cards render identically on a CI runner and
locally.

    python scripts/social_cards.py callouts [--date YYYY-MM-DD]
        -> docs/cards/callouts_<date>.png  (1200x675, X/Twitter)

Each card returns (path, alt_text, caption_facts) for the poster.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.axes import Axes  # noqa: E402

# Card text is plain text: "$7.52 → $17.50" must not be parsed as
# mathtext (matplotlib treats paired $...$ as math and drops the $).
_orig_text = Axes.text


def _plain_text(self, *a, **kw):
    kw.setdefault("parse_math", False)
    return _orig_text(self, *a, **kw)


Axes.text = _plain_text

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARDS = os.path.join(_BASE, "docs", "cards")

BG = "#0a0f24"
PANEL = "#111936"
LINE = "#2a3a6e"
TEXT = "#e8ecf4"
DIM = "#8b93b7"
GOLD = "#ffc800"
GREEN = "#1fc36e"
RED = "#ff5b5b"


def _load(name):
    with open(os.path.join(_BASE, "docs", "reports", name),
              encoding="utf-8") as f:
        return json.load(f)


def _pct(v):
    return "—" if v is None else f"{'+' if v > 0 else ''}{round(v)}%"


def _day_label(d):
    dt = datetime.strptime(d, "%Y-%m-%d")
    return f"{dt:%a} {dt:%b} {dt.day}"


def callouts_card(date=None):
    doc = _load("callouts.json")
    days = doc.get("days") or []
    if not days:
        raise SystemExit("callouts.json has no days")
    day = next((d for d in days if d["date"] == date), None) \
        if date else days[0]
    if day is None:
        raise SystemExit(f"no callouts for {date}")
    top = day["top"][:4]

    fig = plt.figure(figsize=(12, 6.75), dpi=100, facecolor=BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1200)
    ax.set_ylim(675, 0)
    ax.axis("off")

    # header
    ax.text(60, 70, "TICKERDESK", color=GOLD, fontsize=15,
            fontweight="bold", va="center")
    ax.text(1140, 70, _day_label(day["date"]), color=DIM, fontsize=15,
            ha="right", va="center")
    ax.text(60, 135, "Options callouts — how they did", color=TEXT,
            fontsize=30, fontweight="bold", va="center")
    ax.text(60, 185,
            f"Best {len(top)} of {day['n']} callouts  ·  "
            f"{day['hit_100']} hit +100%  ·  {day['hit_50']} hit +50% at peak",
            color=DIM, fontsize=16, va="center")

    # rows
    y0, rh = 245, 88
    for i, t in enumerate(top):
        y = y0 + i * rh
        ax.add_patch(plt.Rectangle((50, y), 1100, rh - 14,
                                   facecolor=PANEL, edgecolor=LINE,
                                   linewidth=1))
        cy = y + (rh - 14) / 2
        ax.text(80, cy, t["label"], color=TEXT, fontsize=20,
                fontweight="bold", va="center")
        ax.text(640, cy, f"${t['entry']:.2f} → ${t['peak']:.2f}",
                color=DIM, fontsize=17, va="center")
        ax.text(1120, cy, _pct(t["peak_pct"]), color=GREEN, fontsize=26,
                fontweight="bold", ha="right", va="center")

    # footer
    ax.text(60, 632,
            "Entry = first trade after TickerDesk flagged it. Peak = best "
            "price afterwards, not a typical exit.",
            color=DIM, fontsize=11.5, va="center")
    ax.text(1140, 632, "tickerdesk.io", color=GOLD, fontsize=15,
            fontweight="bold", ha="right", va="center")

    os.makedirs(CARDS, exist_ok=True)
    path = os.path.join(CARDS, f"callouts_{day['date']}.png")
    fig.savefig(path, facecolor=BG)
    plt.close(fig)

    alt = (f"TickerDesk options callout results for {_day_label(day['date'])}: "
           + "; ".join(f"{t['label']} from ${t['entry']:.2f} to a peak of "
                       f"${t['peak']:.2f} ({_pct(t['peak_pct'])})"
                       for t in top)
           + f". {day['hit_100']} of {day['n']} callouts hit +100% at peak.")
    return path, alt[:1000], {"day": day, "top": top}


# ------------------------------------------------------------ generic

def _fmt_money(v):
    if v is None:
        return "—"
    if v >= 1e9:
        return f"${v / 1e9:.1f}B"
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:,.0f}"


def table_card(fname, kicker, title, subtitle, cols, rows, footer,
               colors=None):
    """cols: [(header, x, align)]; rows: list of cell lists;
    colors: optional fn(row_idx, col_idx, value) -> color or None."""
    n = len(rows)
    fig = plt.figure(figsize=(12, 6.75), dpi=100, facecolor=BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1200)
    ax.set_ylim(675, 0)
    ax.axis("off")
    ax.text(60, 62, "TICKERDESK", color=GOLD, fontsize=15,
            fontweight="bold", va="center")
    ax.text(1140, 62, kicker, color=DIM, fontsize=15, ha="right",
            va="center")
    ax.text(60, 118, title, color=TEXT, fontsize=28, fontweight="bold",
            va="center")
    ax.text(60, 163, subtitle, color=DIM, fontsize=15, va="center")
    top, bottom = 200, 600
    rh = min(58, (bottom - top - 34) / max(n, 1))
    fs = 16 if rh >= 44 else 13.5
    for (h, x, al) in cols:
        ax.text(x, top + 12, h.upper(), color=DIM, fontsize=11,
                fontweight="bold", ha=al, va="center")
    ax.plot([50, 1150], [top + 28, top + 28], color=LINE, lw=1)
    for i, cells in enumerate(rows):
        y = top + 34 + i * rh + rh / 2
        if i % 2 == 0:
            ax.add_patch(plt.Rectangle((50, y - rh / 2), 1100, rh,
                                       facecolor=PANEL, edgecolor="none"))
        for j, ((h, x, al), v) in enumerate(zip(cols, cells)):
            col = colors(i, j, v) if colors else None
            ax.text(x, y, str(v), color=col or TEXT, fontsize=fs,
                    fontweight="bold" if j == 0 else "normal", ha=al,
                    va="center")
    ax.text(60, 635, footer, color=DIM, fontsize=11, va="center")
    ax.text(1140, 635, "tickerdesk.io", color=GOLD, fontsize=15,
            fontweight="bold", ha="right", va="center")
    os.makedirs(CARDS, exist_ok=True)
    path = os.path.join(CARDS, fname)
    fig.savefig(path, facecolor=BG)
    plt.close(fig)
    return path


def _signed(cols_signed):
    def f(i, j, v):
        if j in cols_signed and isinstance(v, str) and v not in ("—", ""):
            return RED if v.startswith("-") else GREEN
        return None
    return f


def _returns(tickers):
    """{ticker: (price, r1m, rytd, r1y)} from daily closes."""
    import yfinance as yf
    out = {}
    tickers = sorted(set(tickers))
    if not tickers:
        return out
    px = yf.download(tickers, period="13mo", interval="1d",
                     progress=False, auto_adjust=True)["Close"]
    if not hasattr(px, "columns"):
        px = px.to_frame(tickers[0])
    year0 = datetime.now().strftime("%Y") + "-01-01"
    for t in tickers:
        if t not in px:
            continue
        c = px[t].dropna()
        if len(c) < 30:
            continue
        last = float(c.iloc[-1])

        def ret(base):
            return round(100 * (last / float(base) - 1), 1)
        ytd = c[c.index >= year0]
        out[t] = (round(last, 2),
                  ret(c.iloc[-22]) if len(c) > 22 else None,
                  ret(ytd.iloc[0]) if len(ytd) else None,
                  ret(c.iloc[-253]) if len(c) > 253 else None)
    return out


def _p1(v):
    return "—" if v is None else f"{'+' if v > 0 else ''}{v:.0f}%"


def _contract(r):
    k = r["strike"]
    k = int(k) if float(k).is_integer() else k
    exp = datetime.strptime(r["expiry"], "%Y-%m-%d")
    return (f"{r['ticker']} {exp:%b} {exp.day} ${k}"
            f"{'C' if r['type'] == 'call' else 'P'}")


# ------------------------------------------------------ midday flow

def flow_card():
    doc = _load("uoa_top25_daily.json")
    rows = sorted((r for r in doc.get("rows") or []
                   if r.get("rank") and r["rank"] <= 25),
                  key=lambda r: -(r.get("premium") or 0))
    calls = [r for r in rows if r["type"] == "call"][:3]
    puts = [r for r in rows if r["type"] == "put"][:3]
    def itm(r):
        p = r.get("pct_otm")
        return " (ITM)" if p is not None and p < 0 else ""
    body = [[_contract(r) + itm(r),
             "CALLS" if r["type"] == "call" else "PUTS",
             _fmt_money(r.get("premium")),
             str(r.get("unique_prints") or "—"),
             f"{r['vol_oi']:.0f}x" if r.get("vol_oi") else "—"]
            for r in calls + puts]
    day = doc.get("et_date")
    cols = [("Contract", 80, "left"), ("Side", 560, "left"),
            ("Premium", 830, "right"), ("Prints", 960, "right"),
            ("Vol/OI", 1120, "right")]

    def col(i, j, v):
        if j == 1:
            return GREEN if v == "CALLS" else RED
        return None
    path = table_card(f"flow_{day}.png", _day_label(day),
                      "Biggest options bets today",
                      "Largest call and put premium in TickerDesk's Top 25",
                      cols, body,
                      "Options flow delayed 15 min. Premium = total traded "
                      "today across all prints.", col)
    alt = "Biggest options bets today: " + "; ".join(
        f"{b[0]} {b[1].lower()}, {b[2]} premium" for b in body)
    return path, alt, {"day": day, "calls": calls, "puts": puts,
                       "body": body}


# --------------------------------------------- weekly earnings calendar

def earnings_week_card(limit=12):
    ea = _load("earnings_anticipated.json")
    edge = {r["t"]: r for r in
            (_load("earnings_edge.json").get("names") or [])}
    names = []
    for d in ea.get("days") or []:
        for sess, lst in (("BMO", d.get("bmo") or []),
                          ("AMC", d.get("amc") or [])):
            for r in lst:
                names.append((r.get("mcap") or 0, d["date"], sess, r))
    names.sort(key=lambda x: -x[0])
    names = names[:limit]
    rets = _returns([n[3]["ticker"] for n in names])
    body = []
    for mcap, date, sess, r in sorted(names, key=lambda x: (x[1], x[2])):
        t = r["ticker"]
        e = edge.get(t) or {}
        pr = rets.get(t, (None, None, None, None))
        dt = datetime.strptime(date, "%Y-%m-%d")
        body.append([t, f"{dt:%a} {sess}",
                     f"${pr[0]:,.2f}" if pr[0] else "—",
                     f"±{e['implied']:.1f}%" if e.get("implied") else "—",
                     f"±{e['realized_med']:.1f}%"
                     if e.get("realized_med") else "—",
                     _p1(pr[1]), _p1(pr[2]), _p1(pr[3])])
    cols = [("Ticker", 80, "left"), ("Reports", 190, "left"),
            ("Price", 450, "right"), ("Implied", 580, "right"),
            ("Typical", 710, "right"), ("1M", 850, "right"),
            ("YTD", 990, "right"), ("1Y", 1120, "right")]
    path = table_card(f"earnings_week_{ea.get('week_of')}.png",
                      "Week of " + _day_label(ea["week_of"]),
                      "Earnings this week",
                      "Implied = options-priced move · Typical = median "
                      "move on past reports", cols, body,
                      "Largest companies reporting, by date. — = no liquid "
                      "options read yet.", _signed({5, 6, 7}))
    alt = "Earnings this week: " + "; ".join(
        f"{b[0]} {b[1]}, implied {b[3]}, typical {b[4]}" for b in body)
    return path, alt, {"week_of": ea["week_of"], "body": body}


# ------------------------------------------------ earnings previews

def earnings_preview_card(date=None):
    edge = _load("earnings_edge.json").get("names") or []
    ok = [r for r in edge if r.get("implied") and r.get("realized_med")]
    day = date or datetime.now().strftime("%Y-%m-%d")
    upcoming = sorted({r["date"] for r in ok if r["date"] >= day})
    if not upcoming:
        raise SystemExit("no earnings with implied + typical moves")
    names, used = [], []
    for d in upcoming:
        names += [r for r in ok if r["date"] == d]
        used.append(d)
        if len(names) >= 4:
            break
    day = used[0]
    names = sorted(names, key=lambda r: (r["date"],
                                         -(r.get("mcap") or 0)))[:6]

    def when(r):
        dt = datetime.strptime(r["date"], "%Y-%m-%d")
        return f"{dt:%a} {r.get('session') or ''}".strip()
    body = [[r["t"], when(r), f"±{r['implied']:.1f}%",
             f"±{r['realized_med']:.1f}%",
             f"{r['implied'] / r['realized_med']:.1f}x",
             str(r.get("n_reports") or "—")] for r in names]
    cols = [("Ticker", 80, "left"), ("When", 260, "left"),
            ("Options price", 560, "right"),
            ("Usually moves", 800, "right"), ("Ratio", 960, "right"),
            ("Reports", 1120, "right")]

    def col(i, j, v):
        if j == 4:
            try:
                return GOLD if float(v.rstrip("x")) >= 1.5 else None
            except ValueError:
                return None
        return None
    dt = datetime.strptime(day, "%Y-%m-%d")
    end = datetime.strptime(used[-1], "%Y-%m-%d")
    kick = f"{dt:%a} {dt:%b} {dt.day}" + (
        f" – {end:%a} {end:%b} {end.day}" if used[-1] != day else "")
    path = table_card(f"earnings_preview_{day}.png", kick,
                      "Earnings: priced vs usual move",
                      "Options-implied move vs the stock's median move on "
                      "past reports", cols, body,
                      "Ratio above 1 = options price a bigger move than the "
                      "stock usually makes. Not a prediction.", col)
    alt = "Earnings priced vs usual move: " + "; ".join(
        f"{b[0]} options price {b[2]}, usually moves {b[3]}" for b in body)
    return path, alt, {"date": day, "names": names, "body": body}


# ------------------------------------------------- momentum lists

def momentum_card(which):
    fname = {"stockbee": "momentum_stockbee.json",
             "qm": "momentum_qm.json"}[which]
    runs = _load(fname).get("runs") or []
    run = max(runs, key=lambda r: r.get("date", ""))
    rows = sorted(run.get("rows") or [],
                  key=lambda r: -(r.get("chg") or 0))[:10]
    title = {"stockbee": "Biggest 5-day gainers",
             "qm": "Strongest 1-month gainers"}[which]
    sub = {"stockbee": "Stockbee 20%-in-a-week screen · change over the "
                       "last 5 trading days",
           "qm": "Top 2% by 1-month gain · ADR 5%+ · $100M+ daily "
                 "volume"}[which]
    body = [[r["ticker"], f"${r['price']:,.2f}", f"+{r['chg']:.0f}%",
             f"{r.get('adr_pct', 0):.1f}%", _fmt_money(r.get("dollar_vol")),
             r.get("ern") or "—"] for r in rows]
    cols = [("Ticker", 80, "left"), ("Price", 400, "right"),
            ("Gain", 560, "right"), ("ADR", 700, "right"),
            ("$ Volume", 900, "right"), ("Earnings", 1120, "right")]
    path = table_card(f"momentum_{which}_{run['date']}.png",
                      _day_label(run["date"]), title, sub, cols, body,
                      f"{run.get('count', len(rows))} names on the full "
                      "list. Screens of past strength, not picks.",
                      lambda i, j, v: GREEN if j == 2 else None)
    alt = f"{title}: " + ", ".join(f"{b[0]} {b[2]}" for b in body)
    return path, alt, {"which": which, "date": run["date"], "body": body,
                       "count": run.get("count", len(rows))}


# ------------------------------------------------- SPY/QQQ levels

def levels_card():
    import yfinance as yf
    em = _load("iv_em_context.json")["by_sym"]
    dp = _load("dealer_positioning.json")["symbols"]
    syms = ["SPY", "QQQ"]
    px = yf.download(syms, period="10d", interval="1d", progress=False,
                     auto_adjust=False)
    body = []
    for s in syms:
        h = float(px["High"][s].dropna().iloc[-1])
        lo = float(px["Low"][s].dropna().iloc[-1])
        c = float(px["Close"][s].dropna().iloc[-1])
        e = (em.get(s) or {}).get("em_pct")
        emd = c * e / 100 if e else None
        d = dp.get(s) or {}
        body.append([s, f"${c:,.2f}", f"±${emd:.2f}" if emd else "—",
                     f"{lo:,.2f} – {h:,.2f}",
                     f"{d.get('put_wall', '—')} / {d.get('call_wall', '—')}",
                     (d.get("regime") or "—").capitalize()])
    cols = [("", 80, "left"), ("Last", 300, "right"),
            ("Exp. move", 480, "right"),
            ("Prior day L – H", 760, "right"),
            ("Put / call wall", 980, "right"), ("Gamma", 1120, "right")]
    day = datetime.now().strftime("%Y-%m-%d")
    path = table_card(f"levels_{day}.png", _day_label(day),
                      "SPY & QQQ levels to watch",
                      "Options-implied move, prior-day range and dealer "
                      "gamma walls", cols, body,
                      "Expected move from same-day options. Walls = strikes "
                      "with the largest dealer gamma.",
                      lambda i, j, v: (GREEN if v == "Positive" else RED)
                      if j == 5 else None)
    alt = "SPY and QQQ levels: " + "; ".join(
        f"{b[0]} last {b[1]}, expected move {b[2]}, prior day {b[3]}, "
        f"walls {b[4]}, gamma {b[5]}" for b in body)
    return path, alt, {"day": day, "body": body}


KINDS = {
    "callouts": lambda a: callouts_card(a.date),
    "flow": lambda a: flow_card(),
    "earnings_week": lambda a: earnings_week_card(),
    "earnings_preview": lambda a: earnings_preview_card(a.date),
    "stockbee": lambda a: momentum_card("stockbee"),
    "qm": lambda a: momentum_card("qm"),
    "levels": lambda a: levels_card(),
}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=list(KINDS))
    ap.add_argument("--date")
    a = ap.parse_args()
    p, alt, _ = KINDS[a.kind](a)
    print(p)
    print("alt:", alt)
