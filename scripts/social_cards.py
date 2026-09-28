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
    # Unusual = volume at least 2x open interest (new positioning, not
    # existing contracts changing hands). Deep in-the-money contracts
    # (>10% ITM) are dropped: big ITM premium is usually hedging or
    # stock replacement, not a view — size alone isn't conviction.
    rows = [r for r in rows if (r.get("vol_oi") or 0) >= 2
            and (r.get("pct_otm") is None or r["pct_otm"] >= -10)]
    calls = [r for r in rows if r["type"] == "call"][:3]
    puts = [r for r in rows if r["type"] == "put"][:3]
    body = [[_contract(r), "Calls" if r["type"] == "call" else "Puts",
             _fmt_money(r.get("premium")),
             str(r.get("unique_prints") or "—"),
             f"{r['vol_oi']:.0f}x" if r.get("vol_oi") else "—"]
            for r in calls + puts]
    day = doc.get("et_date")
    cols = [("Contract", 80, "left"), ("Type", 560, "left"),
            ("Premium", 830, "right"), ("Prints", 960, "right"),
            ("Volume vs OI", 1120, "right")]

    def col(i, j, v):
        if j == 1:
            return GREEN if v == "Calls" else RED
        return None
    path = table_card(f"flow_{day}.png", _day_label(day),
                      "Most unusual options activity today",
                      "Largest premium where volume is 2x+ open interest — "
                      "new positions, not old ones",
                      cols, body,
                      "Delayed 15 min. Premium = total traded today. Buy vs "
                      "sell side not classified; deep-ITM trades excluded.",
                      col)
    alt = "Most unusual options activity today: " + "; ".join(
        f"{b[0]} {b[1].lower()}, {b[2]} premium, volume {b[4]} open "
        "interest" for b in body)
    return path, alt, {"day": day, "calls": calls, "puts": puts,
                       "body": body, "generated": doc.get("generated")}


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
    # Default: reports from tomorrow on (the preview posts the evening
    # before, so today's reports are already out or moments away).
    from datetime import timedelta
    day = date or (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
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
    # Lead with the story: biggest priced-vs-usual gaps first.
    names = sorted(names, key=lambda r: -(r["implied"]
                                          / r["realized_med"]))[:6]

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
    nrep = sorted({r.get("n_reports") for r in names if r.get("n_reports")})
    nrep_txt = (f"{nrep[0]}" if len(nrep) == 1 else
                f"{nrep[0]}–{nrep[-1]}") if nrep else "past"
    path = table_card(f"earnings_preview_{day}.png", kick,
                      "Earnings: priced vs usual move",
                      "Options-implied move vs the stock's median move over "
                      f"its last {nrep_txt} reports", cols, body,
                      "Ratio above 1 = options price a bigger move than the "
                      "stock usually makes. Not a prediction.", col)
    alt = "Earnings priced vs usual move: " + "; ".join(
        f"{b[0]} options price {b[2]}, usually moves {b[3]}" for b in body)
    return path, alt, {"date": day, "names": names, "body": body}


# ------------------------------------------------- momentum lists

def momentum_card(which):
    fname = {"stockbee": "momentum_stockbee.json",
             "qm": "momentum_qm.json"}[which]
    runs = sorted(_load(fname).get("runs") or [],
                  key=lambda r: r.get("date", ""))
    run = runs[-1]
    # NEW = not on the list in any of the previous 5 sessions' runs, so a
    # follower can see what's fresh vs what's been running for a week.
    seen = {r["ticker"] for prev in runs[-6:-1]
            for r in prev.get("rows") or []}
    rows = sorted(run.get("rows") or [],
                  key=lambda r: -(r.get("chg") or 0))[:10]
    title = {"stockbee": "Biggest 5-day gainers",
             "qm": "Strongest 1-month gainers"}[which]
    sub = {"stockbee": "Up 20%+ in 5 trading days (Stockbee-style "
                       "burst screen)",
           "qm": "Top 2% by 1-month gain · ADR 5%+ · $100M+ daily "
                 "volume (Qullamaggie-style trend screen)"}[which]
    body = [[r["ticker"] + ("  NEW" if r["ticker"] not in seen else ""),
             f"${r['price']:,.2f}", f"+{r['chg']:.0f}%",
             f"{r.get('adr_pct', 0):.1f}%", _fmt_money(r.get("dollar_vol")),
             r.get("ern") or "—"] for r in rows]
    new = [r["ticker"] for r in run.get("rows") or []
           if r["ticker"] not in seen]
    cols = [("Ticker", 80, "left"), ("Price", 400, "right"),
            ("Gain", 560, "right"), ("ADR", 700, "right"),
            ("$ Volume", 900, "right"), ("Earnings", 1120, "right")]

    def col(i, j, v):
        if j == 2:
            return GREEN
        if j == 0 and v.endswith("NEW"):
            return GOLD
        return None
    path = table_card(f"momentum_{which}_{run['date']}.png",
                      _day_label(run["date"]), title, sub, cols, body,
                      f"{run.get('count', len(rows))} names on the full "
                      f"list, {len(new)} new this week (NEW = not listed in "
                      "the prior 5 sessions). Past strength, not picks.",
                      col)
    alt = f"{title}: " + ", ".join(f"{b[0]} {b[2]}" for b in body)
    return path, alt, {"which": which, "date": run["date"], "body": body,
                       "count": run.get("count", len(rows)), "new": new,
                       "generated": run.get("generated")}


# ------------------------------------------------- SPY/QQQ levels

def _chain0(sym):
    """Worker chain0 — the same expected move the site's Index Levels
    panel shows (nearest live expiry ATM straddle)."""
    import urllib.request
    req = urllib.request.Request("https://api.tickerdesk.io/?chain0=" + sym,
                                 headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    return None if d.get("error") else d


def _social_log(kind):
    path = os.path.join(_BASE, "data", "social_log.jsonl")
    out = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("kind") == kind and r.get("tweet_id"):
                    out.append(r)
    return out


def levels_card():
    import yfinance as yf
    dp = _load("dealer_positioning.json")["symbols"]
    syms = ["SPY", "QQQ"]
    px = yf.download(syms, period="10d", interval="1d", progress=False,
                     auto_adjust=False)
    last_day = px.index[-1].strftime("%Y-%m-%d")
    base = datetime.strptime(last_day, "%Y-%m-%d")
    body, ems, check = [], {}, []
    # Last posted roadmap -> how the session it covered actually went.
    prev = next((r for r in reversed(_social_log("levels"))
                 if r.get("date") == last_day and r.get("em")), None)
    for s in syms:
        c = float(px["Close"][s].dropna().iloc[-1])
        pc = float(px["Close"][s].dropna().iloc[-2])
        ch = _chain0(s) or {}
        em = ch.get("expected_move") or {}
        emd = em.get("usd")
        ems[s] = em.get("pct")
        d = dp.get(s) or {}
        rng = f"{c - emd:,.0f} – {c + emd:,.0f}" if emd else "—"
        body.append([s, f"${c:,.2f}",
                     f"±${emd:.2f} ({em['pct']:.2f}%)" if emd else "—",
                     rng,
                     f"{d.get('put_wall', '—')} / {d.get('call_wall', '—')}",
                     f"{d['gamma_flip']:.0f}" if d.get("gamma_flip") else "—",
                     (d.get("regime") or "—").capitalize()])
        if prev and (prev["em"].get(s)):
            mv = 100 * (c / pc - 1)
            inside = abs(mv) <= prev["em"][s]
            check.append(f"{s} priced ±{prev['em'][s]:.2f}%, closed "
                         f"{mv:+.2f}% ({'inside' if inside else 'outside'})")
    cols = [("", 80, "left"), ("Close", 260, "right"),
            ("Expected move", 500, "right"), ("Implied range", 680, "right"),
            ("Put / call wall", 850, "right"), ("Flip", 935, "right"),
            ("Dealer gamma", 1120, "right")]
    today = datetime.now().strftime("%Y-%m-%d")
    foot = (f"{base:%a}'s check: " + "; ".join(check)) if check else (
        "Expected move = nearest-expiry ATM straddle. Dealer gamma "
        "estimated from open interest.")
    path = table_card(f"levels_{today}.png", _day_label(today),
                      "SPY & QQQ trading roadmap",
                      f"Based on {base:%A}'s close · options-implied move "
                      "and estimated dealer gamma", cols, body, foot,
                      lambda i, j, v: (GREEN if v == "Positive" else RED)
                      if j == 6 else None)
    alt = "SPY and QQQ trading roadmap: " + "; ".join(
        f"{b[0]} closed {b[1]}, expected move {b[2]}, implied range {b[3]}, "
        f"walls {b[4]}, gamma flip {b[5]}, estimated dealer gamma {b[6]}"
        for b in body)
    return path, alt, {"day": today, "base": last_day, "body": body,
                       "em": ems, "check": check}


# ------------------------------------------------ earnings receipts

def _site_previews(since_days=10):
    """Names the site previewed, each from the last earnings_edge.json
    committed BEFORE its report date (what a reader saw beforehand)."""
    import subprocess
    r = subprocess.run(["git", "log", f"--since={since_days}.days",
                        "--format=%H %cI", "--",
                        "docs/reports/earnings_edge.json"],
                       cwd=_BASE, capture_output=True, text=True)
    out, seen = [], set()
    for line in r.stdout.splitlines():          # newest first
        sha, iso = line.split(" ", 1)
        day = iso[:10]
        b = subprocess.run(["git", "show",
                            f"{sha}:docs/reports/earnings_edge.json"],
                           cwd=_BASE, capture_output=True)
        try:
            names = json.loads(b.stdout.decode("utf-8")).get("names") or []
        except ValueError:
            continue
        for n in names:
            key = (n["t"], n["date"])
            if key in seen or not (n.get("implied") and n.get("realized_med")):
                continue
            if day < n["date"]:                 # committed before report
                seen.add(key)
                out.append(n)
    return out


def _posted_previews():
    out, seen = [], set()
    for r in _social_log("earnings_preview"):
        for n in r.get("names") or []:
            key = (n["t"], n["date"])
            if key not in seen:
                seen.add(key)
                out.append(n)
    return out


def receipt_card(source="posted"):
    """How previewed earnings actually moved vs what options priced.
    source='posted' = only names we previewed on X (the default: a
    receipt follows up our own post); 'site' = anything the site's
    earnings table showed before the report."""
    import yfinance as yf
    done = {tuple(k) for r in _social_log("earnings_receipt")
            for k in r.get("keys") or []}
    pv = _posted_previews() if source == "posted" else _site_previews()
    pv = [n for n in pv if (n["t"], n["date"]) not in done]
    # A name the calendar now shows reporting LATER was rescheduled —
    # its move on the old date wasn't an earnings reaction. Skip it.
    later = {r["t"]: r["date"] for r in
             _load("earnings_edge.json").get("names") or []}
    moved = [n["t"] for n in pv if later.get(n["t"], "") > n["date"]]
    if moved:
        print("receipt: skipping rescheduled", ", ".join(sorted(moved)))
    pv = [n for n in pv if n["t"] not in moved]
    if not pv:
        raise SystemExit("no previewed earnings awaiting a receipt")
    tick = sorted({n["t"] for n in pv})
    px = yf.download(tick, period="1mo", interval="1d", progress=False,
                     auto_adjust=False)
    close = px["Close"]
    rows = []
    for n in pv:
        try:
            ser = (close[n["t"]] if len(tick) > 1 else close.iloc[:, 0])
            ser = ser.dropna()
        except Exception:
            continue
        dates = [d.strftime("%Y-%m-%d") for d in ser.index]
        if n["date"] not in dates:
            continue
        i = dates.index(n["date"])
        # BMO: prior close -> report-day close. AMC: report-day close ->
        # next close (that next session must exist).
        if (n.get("session") or "").upper() == "AMC":
            if i + 1 >= len(dates):
                continue
            a, z = float(ser.iloc[i]), float(ser.iloc[i + 1])
        else:
            if i == 0:
                continue
            a, z = float(ser.iloc[i - 1]), float(ser.iloc[i])
        rows.append((n, 100 * (z / a - 1)))
    if not rows:
        raise SystemExit("previewed names haven't reported/closed yet")
    rows.sort(key=lambda x: -abs(x[1]) / x[0]["implied"])
    rows = rows[:6]
    body = []
    for n, mv in rows:
        dt = datetime.strptime(n["date"], "%Y-%m-%d")
        verdict = ("Bigger than priced" if abs(mv) > n["implied"]
                   else "Inside priced move")
        body.append([n["t"], f"{dt:%a} {n.get('session') or ''}".strip(),
                     f"±{n['implied']:.1f}%", f"±{n['realized_med']:.1f}%",
                     f"{mv:+.1f}%", verdict])
    beat = sum(1 for n, mv in rows if abs(mv) > n["implied"])
    cols = [("Ticker", 80, "left"), ("Reported", 250, "left"),
            ("Priced", 480, "right"), ("Usual", 620, "right"),
            ("Actual", 780, "right"), ("", 840, "left")]

    def col(i, j, v):
        if j == 4:
            return GREEN if v.startswith("+") else RED
        if j == 5:
            return GOLD if v.startswith("Bigger") else DIM
        return None
    today = datetime.now().strftime("%Y-%m-%d")
    path = table_card(f"earnings_receipt_{today}.png", _day_label(today),
                      "Earnings receipts: priced vs actual",
                      "What options priced before the report vs the stock's "
                      "close-to-close reaction", cols, body,
                      f"{beat} of {len(rows)} moved more than options priced. "
                      "BMO: prior close to report-day close; AMC: report-day "
                      "close to next close.", col)
    alt = "Earnings receipts: " + "; ".join(
        f"{b[0]} priced {b[2]}, moved {b[4]}" for b in body)
    return path, alt, {"date": today, "body": body, "beat": beat,
                       "keys": [[n["t"], n["date"]] for n, _ in rows]}


# ------------------------------------------------ weekly recap

def weekly_recap_card(date=None):
    """Friday: the week's callouts in aggregate — winners AND medians."""
    doc = _load("callouts.json")
    days = doc.get("days") or []
    if not days:
        raise SystemExit("callouts.json has no days")
    end = datetime.strptime(date or days[0]["date"], "%Y-%m-%d")
    mon = end.toordinal() - end.weekday()
    wk = sorted((d for d in days if mon <= datetime.strptime(
        d["date"], "%Y-%m-%d").toordinal() <= end.toordinal()),
        key=lambda d: d["date"])
    if not wk:
        raise SystemExit("no callout days this week")
    body = [[_day_label(d["date"]), str(d["n"]), str(d["hit_50"]),
             str(d["hit_100"]), _pct(d["median_peak_pct"]),
             _pct(d["median_last_pct"]),
             f"{d['top'][0]['label']} {_pct(d['top'][0]['peak_pct'])}"
             if d.get("top") else "—"] for d in wk]
    n = sum(d["n"] for d in wk)
    h100 = sum(d["hit_100"] for d in wk)
    h50 = sum(d["hit_50"] for d in wk)
    cols = [("Day", 80, "left"), ("Callouts", 330, "right"),
            ("+50%", 420, "right"), ("+100%", 510, "right"),
            ("Med. peak", 640, "right"), ("Med. now", 760, "right"),
            ("Best", 800, "left")]
    wk_start = datetime.fromordinal(mon).strftime("%Y-%m-%d")
    path = table_card(f"weekly_recap_{wk_start}.png",
                      "Week of " + _day_label(wk_start),
                      "Callouts this week: the full scorecard",
                      "Every Top 25 callout, tracked at the option level "
                      "from first flag", cols, body,
                      f"{n} callouts · {h50} hit +50% · {h100} hit +100% at "
                      "peak. Peak = best exit available, not typical; "
                      "now = latest close.",
                      lambda i, j, v: (GREEN if v.startswith("+") else RED)
                      if j in (4, 5) and v != "—" else None)
    alt = (f"Callouts week of {wk_start}: {n} tracked, {h50} hit +50%, "
           f"{h100} hit +100% at peak")
    return path, alt, {"date": end.strftime("%Y-%m-%d"), "week": wk_start,
                       "n": n, "hit_50": h50, "hit_100": h100, "days": wk}


KINDS = {
    "callouts": lambda a: callouts_card(a.date),
    "flow": lambda a: flow_card(),
    "earnings_week": lambda a: earnings_week_card(),
    "earnings_preview": lambda a: earnings_preview_card(a.date),
    "stockbee": lambda a: momentum_card("stockbee"),
    "qm": lambda a: momentum_card("qm"),
    "levels": lambda a: levels_card(),
    "earnings_receipt": lambda a: receipt_card(
        getattr(a, "source", None) or "posted"),
    "weekly_recap": lambda a: weekly_recap_card(a.date),
}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=list(KINDS))
    ap.add_argument("--date")
    ap.add_argument("--source", choices=["posted", "site"])
    a = ap.parse_args()
    p, alt, _ = KINDS[a.kind](a)
    print(p)
    print("alt:", alt)
