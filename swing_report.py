"""
swing_report.py — Relative Trend Strength "report card".

Grades the liquid universe A+ -> G on a blend of Relative Strength and Trend
Strength, flags ATR-to-SMA50 over-extension, and tags each name's themes.
Emits a dated JSON history (docs/reports/swing_report.json) the dashboard
renders as the Swing Report Card grid.

Methodology (every weight / threshold here is tunable):
  Universe       : 20-day avg $-volume >= $50M AND 20-day ADR% >= universe median
  Rel. Strength  : percentile rank of 0.4*r63 + 0.3*r126 + 0.2*r21 + 0.1*r252
  Trend Strength : MA-stack (<=50) + rising SMA50 (25) + 52w-high proximity (25)
  Composite      : 0.6*RS + 0.4*Trend  ->  20 grades A+ .. G
  Extension      : (close - SMA50) / ATR(14)  —  >=7 over-extended, 5-7 extended
"""

import os
import sys
import json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import pytz
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv(override=True)

import polygon_data
import themes
from momentum_scanner import get_market_universe

ET = pytz.timezone("America/New_York")
_BASE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(_BASE, "docs", "reports", "swing_report.json")

MIN_DOLLAR_VOL = 50_000_000
GRADES = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+",
          "D", "D-", "E+", "E", "E-", "F+", "F", "F-", "G+", "G"]
# lower composite edge of each grade (A+ .. G). A+ is open-ended at the top,
# G is the weak-tail catch-all. Bullish = A+ .. D+  ->  composite >= 52.
GRADE_MIN = [88, 84, 80, 76, 72, 68, 64, 60, 56, 52,
             48, 44, 40, 36, 32, 28, 24, 20, 16, -1]
EXT_OVEREXTENDED = 7.0     # ATR-to-SMA50 — red font
EXT_EXTENDED     = 5.0     # ATR-to-SMA50 — purple font


# ─── Data ─────────────────────────────────────────────────────────────────────

def _fetch_bars(tickers, workers=24):
    """{ticker: {c,h,l,v}} — ~400 calendar days of daily bars, enough for
    SMA200, a 12-month return and a 14-day ATR."""
    def _one(t):
        bars = polygon_data.daily_bars(t, days=400)
        if not bars or len(bars) < 60:
            return t, None
        return t, {
            "c": [b.get("c", 0) or 0 for b in bars],
            "h": [b.get("h", 0) or 0 for b in bars],
            "l": [b.get("l", 0) or 0 for b in bars],
            "v": [b.get("v", 0) or 0 for b in bars],
        }
    out = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for t, s in ex.map(_one, tickers):
            if s:
                out[t] = s
    return out


def _sma(series, n):
    return sum(series[-n:]) / n if len(series) >= n else None


def _atr(c, h, l, n=14):
    if len(c) < n + 1:
        return None
    trs = []
    for i in range(len(c) - n, len(c)):
        trs.append(max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1])))
    return sum(trs) / n if trs else None


def _ret(c, days):
    if len(c) <= days or not c[-days-1]:
        return None
    return (c[-1] / c[-days-1] - 1) * 100


def metrics(ticker, s):
    """Per-ticker raw metrics, or None if it can't be graded."""
    c, h, l, v = s["c"], s["h"], s["l"], s["v"]
    n = len(c)
    price = c[-1]
    if price <= 0 or n < 60:
        return None
    last20 = list(zip(c[-20:], v[-20:]))
    dollar_vol = sum(x * y for x, y in last20) / len(last20) if last20 else 0
    pairs = [(hh, ll) for hh, ll in zip(h[-20:], l[-20:]) if ll > 0]
    adr_pct = (sum(hh / ll for hh, ll in pairs) / len(pairs) - 1) * 100 if pairs else 0
    return {
        "ticker":     ticker,
        "price":      price,
        "dollar_vol": dollar_vol,
        "adr_pct":    adr_pct,
        "sma10":  _sma(c, 10),  "sma20":  _sma(c, 20),
        "sma50":  _sma(c, 50),  "sma200": _sma(c, 200),
        "sma50_prev": _sma(c[:-20], 50) if n >= 70 else None,
        "atr":   _atr(c, h, l, 14),
        "hi52":  max(c[-252:]) if n >= 252 else max(c),
        "r21":   _ret(c, 21),  "r63":  _ret(c, 63),
        "r126":  _ret(c, 126), "r252": _ret(c, 252),
    }


# ─── Grading ──────────────────────────────────────────────────────────────────

def _blended_return(m):
    parts = [(0.4, m["r63"]), (0.3, m["r126"]), (0.2, m["r21"]), (0.1, m["r252"])]
    w = sum(wt for wt, r in parts if r is not None)
    return sum(wt * r for wt, r in parts if r is not None) / w if w else None


def _trend_strength(m):
    """0-100 price-structure score: MA stacking + SMA50 slope + 52w proximity."""
    price = m["price"]
    stack = [
        m["sma10"]  and price > m["sma10"],
        m["sma10"]  and m["sma20"]  and m["sma10"]  > m["sma20"],
        m["sma20"]  and m["sma50"]  and m["sma20"]  > m["sma50"],
        m["sma50"]  and m["sma200"] and m["sma50"]  > m["sma200"],
    ]
    score = 12.5 * sum(1 for x in stack if x)                       # <= 50
    if m["sma50"] and m["sma50_prev"] and m["sma50"] > m["sma50_prev"]:
        score += 25                                                 # rising SMA50
    if m["hi52"]:
        prox = price / m["hi52"]                                    # 1.0 = at highs
        score += max(0.0, min(1.0, (prox - 0.70) / 0.30)) * 25
    return round(score, 1)


def _grade(composite):
    """Composite 0-100 -> one of 20 grades via the GRADE_MIN threshold table."""
    for g, lo in zip(GRADES, GRADE_MIN):
        if composite >= lo:
            return g
    return "G"


def _extension(m):
    """How many ATRs the price sits above its SMA50."""
    if m["atr"] and m["atr"] > 0 and m["sma50"]:
        return round((m["price"] - m["sma50"]) / m["atr"], 1)
    return None


# ─── Run ──────────────────────────────────────────────────────────────────────

def run():
    print(f"\n{'='*54}")
    print(f"SWING REPORT CARD  --  {datetime.now(ET):%Y-%m-%d %H:%M ET}")
    print(f"{'='*54}")

    uni = get_market_universe()
    print(f"  Fetching ~400d daily bars for {len(uni)} names...")
    bars = _fetch_bars(uni)
    print(f"  Bars retrieved for {len(bars)}")

    rows = [m for m in (metrics(t, s) for t, s in bars.items()) if m]

    # universe gate — $50M+ dollar volume, then ADR% at/above the median
    liquid = [m for m in rows if m["dollar_vol"] >= MIN_DOLLAR_VOL]
    if not liquid:
        print("  No names cleared the $50M liquidity gate.")
        return
    adrs = sorted(m["adr_pct"] for m in liquid)
    adr_median = adrs[len(adrs) // 2]
    universe = [m for m in liquid if m["adr_pct"] >= adr_median]
    print(f"  Universe: {len(universe)} names "
          f"($50M+ $-vol, ADR% >= {adr_median:.2f} median)")

    # Relative Strength = percentile rank of the blended return
    for m in universe:
        m["_blend"] = _blended_return(m)
    ranked = sorted(universe, key=lambda m: (m["_blend"] is None,
                                             m["_blend"] if m["_blend"] is not None
                                             else 0.0))
    nU = len(ranked)
    for i, m in enumerate(ranked):
        m["rs"] = round(100 * i / (nU - 1), 1) if nU > 1 else 50.0

    # Trend Strength -> composite -> grade -> extension
    for m in universe:
        m["trend"]     = _trend_strength(m)
        m["composite"] = round(0.6 * m["rs"] + 0.4 * m["trend"], 1)
        m["grade"]     = _grade(m["composite"])
        m["ext"]       = _extension(m)

    # grade -> ordered cells (strongest composite first within each grade)
    universe.sort(key=lambda m: m["composite"], reverse=True)
    grades = {g: [] for g in GRADES}
    for m in universe:
        grades[m["grade"]].append({"t": m["ticker"], "ext": m["ext"]})

    bull = sum(len(grades[g]) for g in GRADES[:10])     # A+ .. D+
    total = len(universe)
    bull_pct = round(100 * bull / total, 1) if total else 0.0

    # theme rollup — avg composite per theme (>= 3 members)
    acc = {}
    for m in universe:
        for th in themes.themes_for(m["ticker"]):
            acc.setdefault(th, []).append(m["composite"])
    theme_rollup = sorted(
        ({"theme": th, "n": len(v), "avg": round(sum(v) / len(v), 1)}
         for th, v in acc.items() if len(v) >= 3),
        key=lambda x: x["avg"], reverse=True)

    run_obj = {
        "date":        datetime.now(ET).strftime("%Y-%m-%d"),
        "generated":   datetime.now(ET).isoformat(timespec="seconds"),
        "total":       total,
        "bullish_pct": bull_pct,
        "bearish_pct": round(100 - bull_pct, 1),
        "counts":      {g: len(grades[g]) for g in GRADES},
        "grades":      grades,
        "themes":      theme_rollup,
    }
    _emit(run_obj)
    print(f"  {total} graded  |  bullish {bull_pct}%  |  "
          f"A+ {len(grades['A+'])}  ...  G {len(grades['G'])}")
    print("\nDone.")


def _emit(run_obj):
    """Append today's run to the dated history (last 40 runs kept)."""
    data = {}
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        pass
    runs = [r for r in data.get("runs", []) if r.get("date") != run_obj["date"]]
    runs.append(run_obj)
    runs = runs[-40:]
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"updated": run_obj["generated"], "runs": runs}, f,
                  separators=(",", ":"))
    print(f"  Wrote swing_report.json ({len(runs)} run(s) in history)")


if __name__ == "__main__":
    run()
