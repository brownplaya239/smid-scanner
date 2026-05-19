"""
swing_report.py — Relative Trend Strength "report card".

Grades the liquid universe A+ -> G on a blend of Relative Strength and Trend
Strength, flags ATR-to-SMA50 over-extension, and tags each name's themes.
Emits a dated JSON history (docs/reports/swing_report.json) the dashboard
renders as the Swing Report Card grid.

Methodology (every weight / threshold here is tunable):
  Universe       : ThinkScript spec —
                     close*volume >= $50M  on the most recent trading day
                     AND
                     14-day ATR > 50-day average of the 14-day ATR
                     (today's volatility above its own 50-day baseline)
  Rel. Strength  : percentile rank of 0.4*r63 + 0.3*r126 + 0.2*r21 + 0.1*r252
  Trend Strength : MA-stack (<=50) + rising SMA50 (25) + 52w-high proximity (25)
  Composite      : 0.6*RS + 0.4*Trend  ->  20 grades A+ .. G
  Extension      : (close - SMA50) / ATR(14)  —  >=7 over-extended, 5-7 extended
"""

import os
import sys
import json
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

import pytz
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv(override=True)

import polygon_data
import themes
from uoa_scanner import EXCLUDE_ETFS, _sic_sector

ET = pytz.timezone("America/New_York")
_BASE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(_BASE, "docs", "reports", "swing_report.json")
META_CACHE_PATH = os.path.join(_BASE, "docs", "reports", "swing_meta_cache.json")
META_REFRESH_DAYS = 30           # company name/sector/exchange change slowly

MIN_DOLLAR_VOL = 50_000_000
EXCHANGE_NAME = {
    "XNAS": "NASDAQ", "XNYS": "NYSE",   "XASE": "AMEX",
    "ARCX": "NYSE Arca", "BATS": "BATS", "IEXG": "IEX",
}
GRADES = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+",
          "D", "D-", "E+", "E", "E-", "F+", "F", "F-", "G+", "G"]
# lower composite edge of each grade (A+ .. G). A+ is open-ended at the top,
# G is the weak-tail catch-all. Bullish = A+ .. D+  ->  composite >= 52.
GRADE_MIN = [88, 84, 80, 76, 72, 68, 64, 60, 56, 52,
             48, 44, 40, 36, 32, 28, 24, 20, 16, -1]
EXT_OVEREXTENDED = 7.0     # ATR-to-SMA50 — red font
EXT_EXTENDED     = 5.0     # ATR-to-SMA50 — purple font


# ─── Security meta (name/sector/industry/country/mcap/exchange) ─────────────

def _swing_cap_bucket(mc):
    if not mc:           return "Unknown"
    if mc >= 200e9:      return "Mega"
    if mc >=  10e9:      return "Large"
    if mc >=   2e9:      return "Mid"
    if mc >=   3e8:      return "Small"
    if mc >=   5e7:      return "Micro"
    return "Nano"


def _load_meta_cache():
    try:
        with open(META_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_meta_cache(cache):
    try:
        os.makedirs(os.path.dirname(META_CACHE_PATH), exist_ok=True)
        with open(META_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=0, sort_keys=True)
    except Exception as e:
        print(f"  swing_meta_cache save failed: {e}")


def _meta_is_fresh(entry, today):
    try:
        fetched = datetime.strptime(entry["fetched"], "%Y-%m-%d").date()
    except (KeyError, ValueError, TypeError):
        return False
    if (today - fetched).days > META_REFRESH_DAYS:
        return False
    # Schema-upgrade safety: re-fetch legacy entries that pre-date the raw
    # market_cap field so popups can show the actual figure ($X.XB / $X.XT)
    # instead of a coarse bucket label.
    if "market_cap" not in entry:
        return False
    return True


def _fetch_one_meta(ticker):
    """Polygon ticker_details -> { name, sector, industry, country, exchange,
    mcap_bucket, fetched } for the dashboard hover card."""
    try:
        d = polygon_data.ticker_details(ticker) or {}
    except Exception:
        d = {}
    loc = (d.get("locale") or "").lower()
    pex = d.get("primary_exchange") or ""
    return {
        "name":        d.get("name") or ticker,
        "sector":      _sic_sector(d.get("sic_code")),
        "industry":    d.get("sic_description") or "",
        "country":     "USA" if loc == "us" else loc.upper(),
        "exchange":    EXCHANGE_NAME.get(pex, pex),
        "mcap_bucket": _swing_cap_bucket(d.get("market_cap")),
        "market_cap":  d.get("market_cap"),     # raw figure for popups
        "fetched":     datetime.now(ET).strftime("%Y-%m-%d"),
    }


# ─── Universe ────────────────────────────────────────────────────────────────

def _last_trading_day():
    d = datetime.now(ET).date()
    while d.weekday() >= 5:                 # Mon-Fri only
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _build_universe(min_dollar_vol=MIN_DOLLAR_VOL, ref_date=None):
    """Single-day dollar-volume gate, applied to the full US-stock universe
    via Polygon's grouped_daily. Mirrors the ThinkScript volumeCondition
    (close * volume >= 50M on the last trading day). Filters out ETFs,
    obvious non-common symbols (dots, length>5), and bad data."""
    ref_date = ref_date or _last_trading_day()
    grouped = polygon_data.grouped_daily(ref_date)
    out = []
    for tk, bar in grouped.items():
        if "." in tk or len(tk) > 5 or tk in EXCLUDE_ETFS:
            continue
        c = bar.get("c") or 0
        v = bar.get("v") or 0
        if c > 0 and v > 0 and c * v >= min_dollar_vol:
            out.append(tk)
    return sorted(out)


def _atr_now_vs_avg(c, h, l, atr_len=14, base_len=50):
    """ThinkScript volatileCondition: (currentATR > averageATR) where
        currentATR = Average(TrueRange, atr_len)        # most recent value
        averageATR = Average(currentATR, base_len)      # most recent value
    Returns (current_atr, average_atr), or (None, None) if not enough history."""
    n = len(c)
    if n < atr_len + base_len + 1:
        return None, None
    # true-range series (one shorter than the close series)
    trs = []
    for i in range(1, n):
        trs.append(max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1])))
    # ATR(atr_len) series — simple mean of last atr_len TRs ending at each bar
    atr_series = []
    for k in range(atr_len, len(trs) + 1):
        atr_series.append(sum(trs[k - atr_len:k]) / atr_len)
    if len(atr_series) < base_len:
        return None, None
    current_atr = atr_series[-1]
    average_atr = sum(atr_series[-base_len:]) / base_len
    return current_atr, average_atr


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
    chg_day = ((c[-1] / c[-2] - 1) * 100) if n >= 2 and c[-2] else None
    # Relative volume: today's bar volume vs the prior 30-day average.
    # A standard breakout-confirmation metric: 1.0 = average, 2.0 = 2x normal.
    rvol = None
    if n >= 31:
        prior_avg = sum(v[-31:-1]) / 30
        if prior_avg > 0:
            rvol = v[-1] / prior_avg
    return {
        "ticker":     ticker,
        "price":      price,
        "dollar_vol": dollar_vol,
        "adr_pct":    adr_pct,
        "rvol":       rvol,
        "sma10":  _sma(c, 10),  "sma20":  _sma(c, 20),
        "sma50":  _sma(c, 50),  "sma200": _sma(c, 200),
        "sma50_prev": _sma(c[:-20], 50) if n >= 70 else None,
        "atr":   _atr(c, h, l, 14),
        "hi52":  max(c[-252:]) if n >= 252 else max(c),
        "r21":   _ret(c, 21),  "r63":  _ret(c, 63),
        "r126":  _ret(c, 126), "r252": _ret(c, 252),
        "chg_day": chg_day,
        "spark":   [round(x, 2) for x in c[-30:]],   # last 30 daily closes
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

    ref = _last_trading_day()
    print(f"  Universe stage 1 — grouped-daily $-vol >= $50M on {ref}...")
    uni = _build_universe(ref_date=ref)
    print(f"  Stage 1 passed: {len(uni)} names")
    print(f"  Fetching ~400d daily bars for {len(uni)} names...")
    bars = _fetch_bars(uni)
    print(f"  Bars retrieved for {len(bars)}")

    # ATR-expansion gate (ThinkScript: 14-day ATR > 50-day avg of 14-day ATR)
    expanding = {}
    for t, s in bars.items():
        cur, avg = _atr_now_vs_avg(s["c"], s["h"], s["l"])
        if cur is not None and avg is not None and cur > avg:
            expanding[t] = s
    print(f"  Stage 2 passed (ATR expanding): {len(expanding)} names")

    universe = [m for m in (metrics(t, s) for t, s in expanding.items()) if m]
    if not universe:
        print("  No names cleared the universe filters.")
        return
    print(f"  Final universe: {len(universe)} names")

    # Security meta — name, sector, industry, country, mcap bucket, exchange.
    # 30-day cache so subsequent runs are near-instant.
    today_d = datetime.now(ET).date()
    meta_cache = _load_meta_cache()
    stale = [m["ticker"] for m in universe
             if not _meta_is_fresh(meta_cache.get(m["ticker"], {}), today_d)]
    if stale:
        print(f"  Fetching ticker details for {len(stale)} names "
              f"(cache hits: {len(universe) - len(stale)})...")
        with ThreadPoolExecutor(max_workers=24) as ex:
            for t, info in ex.map(lambda t: (t, _fetch_one_meta(t)), stale):
                meta_cache[t] = info
        _save_meta_cache(meta_cache)

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
        meta = meta_cache.get(m["ticker"], {})
        grades[m["grade"]].append({
            "t":     m["ticker"],
            "ext":   m["ext"],
            "n":     meta.get("name", m["ticker"]),
            "sec":   meta.get("sector", ""),
            "ind":   meta.get("industry", ""),
            "ctry":  meta.get("country", ""),
            "xch":   meta.get("exchange", ""),
            "mcb":   meta.get("mcap_bucket", ""),
            "mcap":  meta.get("market_cap"),    # raw value (for popup formatting)
            "p":     round(m["price"], 2),
            "chg":   round(m["chg_day"], 2) if m["chg_day"] is not None else None,
            "rvol":  round(m["rvol"], 2) if m.get("rvol") is not None else None,
            "spark": m["spark"],
            "th":    themes.themes_for(m["ticker"]),
        })

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
