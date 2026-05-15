"""
scanner.py – SMID / IWM Russell 2000 Breakout Scanner
Usage:
  python scanner.py          # SMID mode (default)
  python scanner.py --iwm    # IWM Russell 2000 mode
Env vars required: ANTHROPIC_API_KEY, DISCORD_WEBHOOK_URL (SMID) or DISCORD_IWM_WEBHOOK_URL (IWM)
"""

import os
import sys
import io
import csv
import json
import re
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import matplotlib
matplotlib.use("Agg")  # headless — required for GitHub Actions / no display
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
import pytz
import requests
import yfinance as yf
import anthropic
from fpdf import FPDF
from dotenv import load_dotenv

from macro_context import fetch_macro_context
from insider_activity import enrich_candidates_with_insiders, fetch_insider_transactions_detail
from institutional_data import (
    fetch_institutional_data, fetch_13d_13g_filings, compute_smart_money_score, classify_fund
)
from volume_intelligence import (
    compute_ad_rating, find_significant_volume_bars, compute_monthly_flow, detect_silent_build
)

load_dotenv(override=True)

IWM_MODE    = "--iwm" in sys.argv
TICKER_MODE = "--ticker" in sys.argv

ANTHROPIC_API_KEY      = os.environ.get("ANTHROPIC_API_KEY", "")
DISCORD_WEBHOOK_URL    = os.environ.get("DISCORD_WEBHOOK_URL", "")
DISCORD_IWM_WEBHOOK    = os.environ.get("DISCORD_IWM_WEBHOOK_URL", "")
DISCORD_TICKER_WEBHOOK = os.environ.get("DISCORD_TICKER_WEBHOOK_URL", "")

# Mode-aware validation — each mode only needs its own webhook.
if not ANTHROPIC_API_KEY:
    raise EnvironmentError("Missing: ANTHROPIC_API_KEY")
if TICKER_MODE:
    if not DISCORD_TICKER_WEBHOOK:
        raise EnvironmentError("Missing: DISCORD_TICKER_WEBHOOK_URL")
elif IWM_MODE:
    if not DISCORD_IWM_WEBHOOK:
        raise EnvironmentError("Missing: DISCORD_IWM_WEBHOOK_URL")
else:
    if not DISCORD_WEBHOOK_URL:
        raise EnvironmentError("Missing: DISCORD_WEBHOOK_URL")

IWM_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "IWM_holdings.csv")

ET = pytz.timezone("America/New_York")


def _trading_day_fraction():
    """
    Fraction of the regular US session (9:30-16:00 ET) elapsed, 0.15-1.0.
    Returns 1.0 before open, after close, or on weekends — the latest daily
    bar is then complete and needs no projection.
    Intraday it returns the elapsed fraction so a partial-day volume bar can
    be projected to a full-day estimate:  projected = partial / fraction.
    Without this, mid-day scans see ~half a day's volume and the relative-
    volume filter rejects almost everything.
    """
    now = datetime.now(ET)
    if now.weekday() >= 5:          # weekend — last bar is Friday, complete
        return 1.0
    open_t  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    if now <= open_t or now >= close_t:
        return 1.0
    elapsed = (now - open_t).total_seconds()
    total   = (close_t - open_t).total_seconds()
    return max(0.15, elapsed / total)


# ─── Universe ─────────────────────────────────────────────────────────────────

def get_dynamic_universe(size=250):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    try:
        from yfinance import Screener
        screener = Screener()
        screener.set_body({
            "offset": 0, "size": size,
            "sortField": "intradaymarketcap", "sortType": "DESC",
            "quoteType": "EQUITY",
            "query": {
                "operator": "AND",
                "operands": [
                    {"operator": "BTWN", "operands": ["intradaymarketcap", 200_000_000, 10_000_000_000]},
                    {"operator": "EQ", "operands": ["region", "us"]},
                    {"operator": "GT", "operands": ["averageDailyVolume3Month", 300_000]},
                    {"operator": "GT", "operands": ["intradayprice", 3.0]},
                ]
            }
        })
        quotes = screener.response.get("quotes", [])
        tickers = [q["symbol"] for q in quotes if q.get("symbol") and "." not in q["symbol"]]
        if tickers:
            print(f"  📡 {len(tickers)} tickers from yfinance Screener")
            return tickers
    except Exception:
        pass

    try:
        tickers = set()
        for scr_id in ["small_cap_gainers", "undervalued_small_caps", "most_actives", "day_gainers"]:
            resp = requests.get(
                "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved",
                params={"scrIds": scr_id, "count": 100, "region": "US", "lang": "en-US"},
                headers=headers, timeout=10
            )
            if resp.ok:
                quotes = resp.json().get("finance", {}).get("result", [{}])[0].get("quotes", [])
                for q in quotes:
                    sym = q.get("symbol", "")
                    mktcap = q.get("marketCap", 0) or 0
                    if sym and "." not in sym and 200_000_000 <= mktcap <= 10_000_000_000:
                        tickers.add(sym)
        if tickers:
            print(f"  📡 {len(tickers)} tickers from Yahoo predefined screeners")
            return list(tickers)
    except Exception:
        pass

    print("  ⚠️  All screeners failed, using fallback list")
    return [
        "TGTX", "KRYS", "PTGX", "RCUS", "VERA", "EVTL", "HUMA", "AEYE",
        "KNTK", "FLNC", "ARRY", "SHLS", "STEM", "WPRT",
        "BKSY", "SPIR", "LUNR", "RDW", "MNTS",
        "AEHR", "PDYN", "TPIC", "ITRN", "OSIS", "BFAM",
        "ACMR", "PRCT", "FTLF", "ETON", "ANIP",
    ]


# ─── IWM universe ─────────────────────────────────────────────────────────────

def load_iwm_universe(top_n=500):
    """Load top N IWM equity constituents from CSV (sorted by weight desc)."""
    tickers = []
    try:
        with open(IWM_CSV, encoding="utf-8") as f:
            lines = f.readlines()
        header_idx = next(i for i, l in enumerate(lines) if l.strip().startswith("Ticker"))
        reader = csv.DictReader(io.StringIO("".join(lines[header_idx:])))
        for row in reader:
            ticker = row.get("Ticker", "").strip().strip('"')
            if not re.match(r'^[A-Z]{1,5}$', ticker):
                continue
            if "Equity" not in (row.get("Asset Class") or ""):
                continue
            tickers.append(ticker)
            if len(tickers) >= top_n:
                break
    except Exception as e:
        print(f"  ⚠️  IWM CSV load failed: {e}")
    print(f"  📋 {len(tickers)} IWM constituents loaded")
    return tickers


def fetch_iwm_data(tickers):
    """Bulk-download OHLCV for all IWM tickers, pre-filter technically,
    then fetch .info only for survivors. Much faster than individual calls."""
    spy_hist = yf.Ticker("SPY").history(period="200d", interval="1d")
    spy_12w  = (spy_hist["Close"].iloc[-1] / spy_hist["Close"].iloc[-63] - 1) * 100 \
               if len(spy_hist) >= 63 else 0.0

    _day_frac = _trading_day_fraction()
    if _day_frac < 1.0:
        print(f"  Intraday run — projecting volume (session {_day_frac*100:.0f}% elapsed)")

    # Chunked download — a single 500-ticker yf.download gets flagged as abuse
    # from datacenter IPs (GitHub Actions) and returns empty. 50-ticker chunks
    # with retries + polite delays look like normal traffic and succeed.
    import time as _time
    print(f"  Downloading {len(tickers)} tickers in chunks of 50...")
    bulk_data = {}
    chunk_size = 50
    for ci in range(0, len(tickers), chunk_size):
        chunk = tickers[ci:ci + chunk_size]
        got = False
        for attempt in range(3):
            try:
                bd = yf.download(
                    chunk, period="200d", interval="1d",
                    group_by="ticker", auto_adjust=True, threads=True, progress=False,
                )
                if bd is not None and not bd.empty:
                    for t in chunk:
                        try:
                            h = bd[t].dropna(how="all") if len(chunk) > 1 else bd.dropna(how="all")
                            if not h.empty:
                                bulk_data[t] = h
                        except Exception:
                            pass
                    got = True
                    break
            except Exception:
                pass
            _time.sleep(3 * (attempt + 1))
        if not got:
            print(f"    chunk {ci // chunk_size + 1} failed after 3 retries")
        else:
            _time.sleep(1.0)  # polite gap between chunks
    print(f"  Got data for {len(bulk_data)}/{len(tickers)} tickers")

    tech_pass, hist_cache = [], {}
    for ticker, hist in bulk_data.items():
        try:
            if hist.empty or len(hist) < 20:
                continue
            price      = float(hist["Close"].iloc[-1])
            prev       = float(hist["Close"].iloc[-2])
            change_pct = (price - prev) / prev * 100
            avg_vol    = float(hist["Volume"].iloc[-21:-1].mean())
            # Project today's partial volume bar to a full-day estimate
            today_vol  = float(hist["Volume"].iloc[-1]) / _day_frac
            vol_ratio  = today_vol / avg_vol if avg_vol > 0 else 1.0
            ma20       = float(hist["Close"].iloc[-20:].mean())
            ma50       = float(hist["Close"].iloc[-50:].mean()) if len(hist) >= 50 else None

            if price < ma20:      continue
            if vol_ratio < 1.5:   continue
            if change_pct < 0:    continue

            hist_cache[ticker] = hist
            tech_pass.append({
                "ticker":      ticker,
                "price":       round(price, 2),
                "change_pct":  round(change_pct, 2),
                "vol_ratio":   round(vol_ratio, 2),
                "above_20ma":  True,
                "above_50ma":  bool(ma50 and price > ma50),
                "_hist":       hist,
            })
        except Exception:
            pass

    print(f"  {len(tech_pass)} passed technical pre-filter — fetching fundamentals...")

    results = []
    for c in tech_pass:
        ticker = c["ticker"]
        hist   = c.pop("_hist")
        try:
            info     = yf.Ticker(ticker).info
            mkt_cap  = info.get("marketCap", 0) or 0
            float_sh = info.get("floatShares", 0) or 0
            high_52w = info.get("fiftyTwoWeekHigh", 0) or 0
            low_52w  = info.get("fiftyTwoWeekLow", 0) or 0

            if mkt_cap  <= 0 or mkt_cap  > 8_000_000_000: continue
            if float_sh <= 0 or float_sh > 200_000_000:    continue

            stock_12w = (c["price"] / float(hist["Close"].iloc[-63]) - 1) * 100 \
                        if len(hist) >= 63 else 0.0
            rs_vs_spy = round(stock_12w - spy_12w, 1)
            prox_52w  = round((c["price"] / high_52w) * 100, 1) if high_52w > 0 else None

            if len(hist) >= 20:
                last10     = hist["Close"].iloc[-10:]
                atr20      = (hist["High"].iloc[-20:] - hist["Low"].iloc[-20:]).mean()
                base_tight = round((float(last10.max()) - float(last10.min())) / float(atr20), 2) \
                             if float(atr20) > 0 else None
            else:
                base_tight = None

            results.append({**c,
                "company":    info.get("shortName", ticker),
                "mkt_cap_b":  round(mkt_cap / 1e9, 2),
                "float_m":    round(float_sh / 1e6, 1),
                "rs_3m":      round(stock_12w, 1),
                "rs_vs_spy":  rs_vs_spy,
                "prox_52w":   prox_52w,
                "base_tight": base_tight,
                "sector":     info.get("sector", ""),
                "industry":   info.get("industry", ""),
                "52w_high":   round(high_52w, 2),
                "52w_low":    round(low_52w, 2),
            })
        except Exception:
            pass

    print(f"  ✅ {len(results)} IWM candidates with fundamentals")
    return results, hist_cache, spy_hist


# ─── Data fetching ────────────────────────────────────────────────────────────

def get_scan_type():
    now = datetime.now(ET)
    hour, minute = now.hour, now.minute
    if (hour == 8 and minute >= 30) or (hour == 9 and minute < 30):
        return "PRE-MARKET"
    elif 9 <= hour < 11:
        return "MARKET OPEN"
    elif 15 <= hour <= 16:
        return "MARKET CLOSE"
    else:
        return f"SCAN ({now.strftime('%H:%M ET')})"


def fetch_yfinance_data(tickers):
    """Returns (results_list, hist_cache_dict). Adds RS vs SPY, 52W proximity, base tightness."""
    # Fetch SPY once for relative strength calculation
    spy_12w_return = 0.0
    try:
        spy_hist = yf.Ticker("SPY").history(period="200d", interval="1d")
        if len(spy_hist) >= 63:
            spy_12w_return = (spy_hist["Close"].iloc[-1] / spy_hist["Close"].iloc[-63] - 1) * 100
    except Exception:
        pass

    results = []
    hist_cache = {}
    print(f"Fetching data for {len(tickers)} tickers...")
    for ticker in tickers:
        try:
            t = yf.Ticker(ticker)
            info = t.info
            hist = t.history(period="200d", interval="1d")
            if hist.empty or len(hist) < 20:
                continue

            price       = hist["Close"].iloc[-1]
            prev_close  = hist["Close"].iloc[-2]
            change_pct  = ((price - prev_close) / prev_close) * 100
            avg_vol     = hist["Volume"].iloc[-21:-1].mean()
            # Project today's partial volume bar to a full-day estimate
            today_vol   = hist["Volume"].iloc[-1] / _trading_day_fraction()
            vol_ratio   = today_vol / avg_vol if avg_vol > 0 else 1.0
            ma20        = hist["Close"].iloc[-20:].mean()
            ma50        = hist["Close"].iloc[-50:].mean() if len(hist) >= 50 else None
            above_20ma  = bool(price > ma20)
            above_50ma  = bool(price > ma50) if ma50 is not None else None
            mkt_cap     = info.get("marketCap", 0) or 0
            float_sh    = info.get("floatShares", 0) or 0
            high_52w    = info.get("fiftyTwoWeekHigh", 0) or 0
            low_52w     = info.get("fiftyTwoWeekLow", 0) or 0

            # RS vs SPY (12-week relative return)
            if len(hist) >= 63:
                stock_12w = (price / hist["Close"].iloc[-63] - 1) * 100
                rs_vs_spy = round(stock_12w - spy_12w_return, 1)
            else:
                stock_12w = (price / hist["Close"].iloc[0] - 1) * 100
                rs_vs_spy = round(stock_12w - spy_12w_return, 1)

            # 52W high proximity (100 = AT 52W high)
            prox_52w = round((price / high_52w) * 100, 1) if high_52w > 0 else None

            # Base tightness: 10-day close range / 20-day ATR (lower = tighter VCP)
            if len(hist) >= 20:
                last10     = hist["Close"].iloc[-10:]
                atr20      = (hist["High"].iloc[-20:] - hist["Low"].iloc[-20:]).mean()
                base_tight = round((last10.max() - last10.min()) / atr20, 2) if atr20 > 0 else None
            else:
                base_tight = None

            hist_cache[ticker] = hist
            results.append({
                "ticker":       ticker,
                "company":      info.get("shortName", ticker),
                "price":        round(price, 2),
                "change_pct":   round(change_pct, 2),
                "mkt_cap_b":    round(mkt_cap / 1e9, 2),
                "float_m":      round(float_sh / 1e6, 1),
                "vol_ratio":    round(vol_ratio, 2),
                "above_20ma":   above_20ma,
                "above_50ma":   above_50ma,
                "rs_3m":        round(stock_12w, 1),
                "rs_vs_spy":    rs_vs_spy,
                "prox_52w":     prox_52w,
                "base_tight":   base_tight,
                "sector":       info.get("sector", ""),
                "industry":     info.get("industry", ""),
                "52w_high":     round(high_52w, 2),
                "52w_low":      round(low_52w, 2),
            })
        except Exception as e:
            print(f"  ⚠️  Skipped {ticker}: {e}")
    print(f"  ✅ {len(results)} tickers fetched")
    return results, hist_cache, spy_hist


def enrich_with_earnings(candidates, ticker_objs):
    """Fetch earnings dates for pre-filter survivors only (fast — small list)."""
    now = datetime.now(ET).date()
    for d in candidates:
        ticker = d["ticker"]
        d["earnings_date"] = None
        d["earnings_days"] = None
        d["earnings_flag"] = ""
        try:
            cal = ticker_objs[ticker].calendar
            if cal is None:
                continue
            dates = cal.get("Earnings Date", []) if isinstance(cal, dict) else []
            if not dates:
                continue
            ed = dates[0]
            ed_date = ed.date() if hasattr(ed, "date") else None
            if ed_date:
                delta = (ed_date - now).days
                d["earnings_date"] = str(ed_date)
                d["earnings_days"] = delta
                if delta < 0:
                    d["earnings_flag"] = "REPORTED"
                elif delta == 0:
                    d["earnings_flag"] = "EARNINGS TODAY"
                elif delta <= 3:
                    d["earnings_flag"] = f"EARNINGS IN {delta}D"
                elif delta <= 7:
                    d["earnings_flag"] = f"EARNINGS IN {delta}D"
        except Exception:
            pass
    return candidates


def get_sector_leaders(spy_hist):
    """Return top 3 sectors outperforming SPY over 4 weeks."""
    SECTOR_ETFS = {
        "Technology": "XLK", "Healthcare": "XLV", "Financials": "XLF",
        "Energy": "XLE", "Industrials": "XLI", "Materials": "XLB",
        "Communications": "XLC", "Consumer Disc": "XLY",
        "Consumer Staples": "XLP", "Utilities": "XLU", "Real Estate": "XLRE",
    }
    try:
        spy_ret = (spy_hist["Close"].iloc[-1] / spy_hist["Close"].iloc[-20] - 1) * 100
        leaders = []
        for sector, etf in SECTOR_ETFS.items():
            try:
                h = yf.Ticker(etf).history(period="60d", interval="1d")
                if len(h) >= 20:
                    ret = (h["Close"].iloc[-1] / h["Close"].iloc[-20] - 1) * 100
                    leaders.append((sector, round(ret - spy_ret, 1)))
            except Exception:
                pass
        leaders.sort(key=lambda x: x[1], reverse=True)
        return leaders[:3]
    except Exception:
        return []


def add_rs_line_new_high(candidates, hist_cache, spy_hist):
    """Flag if the RS line (stock/SPY) is at a 52-week high — early leader signal."""
    for d in candidates:
        ticker = d["ticker"]
        d["rs_line_new_high"] = False
        try:
            hist = hist_cache.get(ticker)
            if hist is None or len(hist) < 20:
                continue
            aligned_spy = spy_hist["Close"].reindex(hist.index, method="ffill")
            rs_line = hist["Close"] / aligned_spy
            rs_line = rs_line.dropna()
            if len(rs_line) < 10:
                continue
            rs_52w_high = rs_line.rolling(min(252, len(rs_line))).max().iloc[-1]
            d["rs_line_new_high"] = bool(rs_line.iloc[-1] >= rs_52w_high * 0.98)
        except Exception:
            pass
    return candidates


def pre_filter(data):
    filtered, rejected = [], []
    for d in data:
        t = d["ticker"]
        if d["mkt_cap_b"] <= 0 or d["mkt_cap_b"] >= 10:
            rejected.append(f"  ✗ {t}: mkt cap ${d['mkt_cap_b']:.1f}B")
            continue
        if d["float_m"] <= 0 or d["float_m"] >= 150:
            rejected.append(f"  ✗ {t}: float {d['float_m']:.0f}M")
            continue
        if not d["above_20ma"]:
            rejected.append(f"  ✗ {t}: below 20MA (${d['price']:.2f})")
            continue
        if d["vol_ratio"] < 1.5:
            rejected.append(f"  ✗ {t}: vol {d['vol_ratio']:.2f}x")
            continue
        if d["change_pct"] < 0:
            rejected.append(f"  ✗ {t}: red {d['change_pct']:.2f}%")
            continue
        filtered.append(d)
    for r in rejected:
        print(r)
    filtered.sort(key=lambda x: x["vol_ratio"], reverse=True)
    print(f"\n  ✅ {len(filtered)}/{len(data)} passed pre-filter")
    return filtered[:30]


# ─── Claude analysis ──────────────────────────────────────────────────────────

# Static prompt — cacheable across calls within 5 min (parallel SMID/IWM runs)
SCANNER_STATIC_PROMPT = """You are a Wharton-educated hedge fund analyst specializing in SMID-cap momentum with deep expertise in identifying live breakout inflection points.

These stocks passed a strict pre-filter: mkt cap <$10B, float <150M, above 20MA, volume >1.5x avg, green on day. A breakout is happening NOW — your task is to determine which are institutional-quality setups with follow-through potential vs. noise.

Key fields:
- earnings_flag: "EARNINGS TODAY" / "EARNINGS IN Xd" / "REPORTED"
- rs_line_new_high: RS line at 52W high — Qullamaggie's #1 early leader signal
- rs_vs_spy: 12-week outperformance vs SPY
- prox_52w: proximity to 52W high (100 = at high, >85 = breakout zone)
- base_tight: 10d close range / ATR (<2 = VCP-tight, <1.5 = textbook coil)

For each candidate, analyze rigorously across these dimensions:

1. TODAY'S CATALYST — What is actually driving this move RIGHT NOW?
   - Earnings beat/guidance raise, FDA/regulatory approval, contract win, partnership, analyst upgrade, sector rotation, sympathy play, short squeeze, technical breakout from base?
   - Is the catalyst durable or a one-day event?

2. BUSINESS & INDUSTRY POSITION — What does this company do and what is its competitive edge?
   - Category leader, fast follower, or niche disruptor in what specific market?
   - Why does this business deserve a premium at this moment?

3. FACTOR & THEME EXPOSURE — Which secular tailwinds does this tap?
   - AI infrastructure, defense spending, energy transition, biotech, reshoring, GLP-1, cybersecurity, space economy, consumer recovery?
   - Is theme momentum accelerating or decelerating?

4. INSTITUTIONAL & SMART MONEY — Is real money chasing this?
   - Volume surge profile: institutional block buying or retail FOMO?
   - **INSIDER ACTIVITY (the strongest single alpha factor):** check `insider_count`, `insider_value`, `insider_senior`, `insider_summary`. These are open-market BUYS from SEC Form 4 in last 60 days (option exercises and 10b5-1 sales excluded). A cluster (multiple insiders, especially C-suite) is top-tier conviction. State this in `institutionalAngle`.
   - Known growth fund holders, ETF exposure, float rotation dynamics?
   - Short squeeze mechanics if short interest is elevated?

GRADE UPGRADE RULE: If `insider_cluster >= 5` (multiple insiders OR senior officer buys with material dollar size) AND macro is risk-on, upgrade grade by one tier (B → A). Note the upgrade explicitly. Insider buying is rare — when present, weight it heavily.

VOLUME INTELLIGENCE (when fields are present, weave into analysis):
- `ad_rating_grade`: O'Neill A/D letter grade A-E. A/B = institutional accumulation; D/E = distribution; C = neutral. ALWAYS cite this in `institutionalAngle` if present.
- `monthly_flow_trend`: Trajectory across last 3 months (e.g. "Mixed -> Distribution -> Strong Accumulation"). The recent direction matters more than the absolute level — a stock going Distribution → Accumulation is a regime change worth flagging.
- `silent_build_detected`: Boolean. If true, this is a CLASSIC institutional setup (vol drying up + price tight = quiet absorption). State explicitly: "silent-build pattern present — institutions absorbing supply without spiking price."
- `accumulation_bars_count` / `distribution_bars_count` / `absorption_bars_count`: How many extreme volume days (>2x avg) in last 90d. Multiple accumulation bars + zero distribution = strong setup. Reverse = avoid.

DATA QUALITY GUARDRAILS (avoid these hallucinations):
1. TICKER PRESERVATION: Use the ticker symbol EXACTLY as provided in the input candidate. Do NOT modify, abbreviate, transliterate, or "correct" any ticker. If input says "IONQ", return "IONQ" — never "IONO" or any variant.
2. INSTITUTIONAL OWNERSHIP: If `inst_own` is "0%", "0.0%", or "—", that means yfinance has no data — DO NOT report it as "0% institutional ownership" or call it a red flag. State that institutional ownership data is not available from this feed and recommend cross-referencing with 13F filings if it matters to the thesis. Most $1B+ companies have 50-90% institutional ownership.
3. INSIDER TIME WINDOW: The `insider_count` / `insider_summary` fields cover only the LAST 90 DAYS. Cross-reference with `insider_12m_buys_count`, `insider_12m_sales_count`, and `insider_most_recent_buy` for full 12-month context. Do NOT claim "zero insider buying" if `insider_12m_buys_count > 0`. If a buy happened 91+ days ago, say "no insider buying in the last 90 days, but [most recent buy] occurred [X] days ago" — give the broader picture.
4. NEGATIVE P/E: For pre-revenue or unprofitable companies, a negative forward P/E is meaningless — do NOT cite it as a valuation indicator. Use Price/Sales (`ps_ratio` if provided) or note "valuation reflects pre-commercial / growth-stage premium; P/E inapplicable."
5. SHORT INTEREST: When discussing short interest, ALSO compute and reference days-to-cover (short_ratio field if available, or roughly: short_pct × float / avg_daily_volume). High SI alone is ambiguous; high SI with low days-to-cover is squeeze fuel.

5. EARNINGS CONTEXT (if applicable)
   - Use the `earnings_date` field (ISO YYYY-MM-DD) as the SOLE source of truth for the next earnings date. Do NOT generate a date from your own knowledge — yfinance has the live calendar; your training data is stale.
   - If `earnings_date` is null or empty, do not invent one — return earningsContext as "".
   - When citing the date, format as "Month DD" (e.g. "May 14"). Append " BMO" or " AMC" ONLY if you have explicit confirmation; if uncertain, omit the time-of-day designator.
   - EPS/revenue consensus, beat/miss history (last 4 quarters), is this a serial beater that re-rates higher on each beat?

6. RISK — What kills the trade?
   - Specific bear case: binary event risk, dilution at $X, single customer, competitive threat, technical failure level?

GRADING (Qullamaggie methodology):
- "A - Breakout": 4+ of: vol >2x, prox_52w >85, rs_line_new_high, rs_vs_spy >+10, base_tight <2.0, durable catalyst. Pure technical breakouts with rs_line_new_high + vol surge ARE valid A setups even without same-day news.
- "B - Strong": vol >1.5x, prox_52w >75, rs_vs_spy >0, above 20MA+50MA
- "C - Watch": elevated vol, above 20MA, RS positive or turning

GRADING EXAMPLES (be consistent with these):
A-grade example: ticker at $42, vol_ratio 3.2x, prox_52w 95, rs_line_new_high=true, rs_vs_spy +28, base_tight 1.6, breakout on a partnership announcement = clear A. Five technical criteria met plus a durable catalyst.
A-grade example (no news): vol_ratio 2.5x, prox_52w 92, rs_line_new_high=true, rs_vs_spy +18, base_tight 1.8 — a clean technical breakout from a textbook coil with leadership confirmed by the RS line at new high. Valid A even without same-day news.
B-grade example: vol_ratio 1.8x, prox_52w 78, rs_vs_spy +5, no rs_line_new_high, base_tight 2.4 — solid momentum but the institutional confirmation signal is missing and the base is wider than ideal. Clear B.
B-grade example: vol_ratio 2.1x, prox_52w 88, rs_vs_spy +12, rs_line_new_high=false, no obvious catalyst — strong technicals but the RS line not at new high downgrades it from A. Solid B-grade follow-through candidate.
C-grade example: vol_ratio 1.6x, but stock is extended 15% from base on a single news headline, sector decelerating, prox_52w 72 = C, watchlist only. Wait for it to set up properly.
C-grade example: vol_ratio 1.7x, prox_52w 80, but rs_vs_spy is barely positive at +1.5 and base is wide (base_tight 3.1) — this is an early stage potential setup that hasn't earned breakout treatment yet. C, monitor.

IMPORTANT: Include ALL candidates with genuine momentum. Do NOT exclude for lack of news catalyst. Return at least the top 10 by technical quality.

OUTPUT EFFICIENCY: For A and B grades, populate ALL analytical fields below in full. For C grades, set businessDescription, factorExposure, institutionalAngle, earningsContext, and keyRisk to "" (empty string) — only ticker, company, price, changePercent, marketCapB, floatM, rsVsSpy, prox52w, baseTight, rsLineNewHigh, earningsFlag, theme, industry, catalyst, signal, volumeVsAvg, rs, score, and a short reasoning are required for C-grade rows.

Return ONLY a raw JSON array. No markdown. No preamble.
Each object must include ALL fields:
  ticker, company, price, changePercent, marketCapB, floatM, rsVsSpy, prox52w, baseTight,
  rsLineNewHigh, earningsFlag,
  theme (2-4 words),
  industry (precise label),
  catalyst (1-2 sentences: what is specifically driving today's move),
  businessDescription (2 sentences for A/B, "" for C),
  factorExposure (1-2 sentences for A/B, "" for C),
  institutionalAngle (1-2 sentences for A/B, "" for C),
  earningsContext (consensus EPS/rev + beat history, or ""),
  keyRisk (1 sentence for A/B, "" for C),
  signal (exact technical condition: "Close above $X.XX on vol >Y% of 20d avg"),
  volumeVsAvg (e.g. "2.4x"),
  rs,
  score (MUST be exactly one of: "A", "B", or "C" — the letter grade only, no numbers),
  reasoning (1-2 sentences: why this is or isn't a high-conviction follow-through)."""


def run_claude_analysis(candidates, scan_type, sector_leaders=None, macro=None, force_full_descriptives=False):
    if not candidates:
        return []
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    today = datetime.now(ET).strftime("%B %d, %Y")

    sector_ctx = ""
    if sector_leaders:
        leaders_str = ", ".join(f"{s} ({r:+.1f}% vs SPY)" for s, r in sector_leaders)
        sector_ctx = f"Leading sectors this week: {leaders_str}\nStocks in these sectors score +0.5 grade when setup quality is equal.\n"

    macro_block = ""
    if macro:
        macro_block = f"""
MARKET REGIME (factor into conviction):
- Regime: {macro.get('regime', 'Unknown')} — {macro.get('regime_description', '')}
- SPY 20d: {macro.get('spy_20d_pct', 0):+.1f}%  |  IWM 20d: {macro.get('iwm_20d_pct', 0):+.1f}%  |  IWM/SPY trend: {macro.get('iwm_spy_trend', 0):+.1f}%
- VIX: {macro.get('vix', 0)} ({macro.get('vix_change_20d', 0):+.1f} vs 20d avg)
- Leading sectors: {', '.join(macro.get('leading_sectors', []))}

Risk-Off → most breakouts fail; note macro headwind in reasoning. Risk-On with small-cap leadership → A-grades have full historical edge.
"""

    candidates = candidates[:15]  # trimmed from 20 — top 15 cover all A/B grades

    override_block = ""
    if force_full_descriptives:
        override_block = """
OVERRIDE — SINGLE TICKER MODE: This is an on-demand single-ticker lookup. The OUTPUT EFFICIENCY rule does NOT apply. Populate ALL analytical fields (businessDescription, factorExposure, institutionalAngle, earningsContext, keyRisk) for ALL grades (A, B, AND C). Token cost is immaterial for a single name — give full institutional-quality treatment regardless of grade.
"""

    full_prompt = f"""{SCANNER_STATIC_PROMPT}

Today is {today}. Scan type: {scan_type}.

{sector_ctx}{macro_block}{override_block}
Candidates (include insider activity from SEC Form 4 last 60d):
{json.dumps(candidates, indent=2)}"""

    print("  ✅ Sending to Claude (Sonnet 4.6)...")
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=12000,
        messages=[{"role": "user", "content": full_prompt}],
    )
    usage = getattr(response, "usage", None)
    if usage:
        it = getattr(usage, "input_tokens", 0) or 0
        ot = getattr(usage, "output_tokens", 0) or 0
        print(f"  Tokens — input:{it}  output:{ot}")
    raw = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    print(f"  Raw response length: {len(raw)} chars, starts: {raw[:80]!r}")
    try:
        parsed = json.loads(raw)
        print(f"  Parsed OK: {len(parsed)} items")
        return parsed
    except Exception as e:
        print(f"  JSON parse failed: {e}")
        print(f"  Last 200 chars: {raw[-200:]!r}")
        match = re.search(r'\[[\s\S]*\]', raw)
        if match:
            try:
                parsed = json.loads(match.group(0))
                print(f"  Regex fallback OK: {len(parsed)} items")
                return parsed
            except Exception as e2:
                print(f"  Regex fallback also failed: {e2}")
        return []


# ─── Chart generation ─────────────────────────────────────────────────────────

def generate_chart(ticker, hist):
    """Qullamaggie-style: candlestick + 9/21/50/200 SMA + RSI + relative volume coloring."""
    try:
        data = hist.copy()
        data["SMA9"]   = data["Close"].rolling(9).mean()
        data["SMA21"]  = data["Close"].rolling(21).mean()
        data["SMA50"]  = data["Close"].rolling(50).mean()
        data["SMA200"] = data["Close"].rolling(200).mean()

        delta        = data["Close"].diff()
        gain         = delta.clip(lower=0).rolling(14).mean()
        loss         = (-delta.clip(upper=0)).rolling(14).mean()
        rsi_raw      = 100 - (100 / (1 + gain / loss.where(loss != 0, float("nan"))))
        data["RSI"]  = rsi_raw.ffill().bfill().clip(0, 100)

        avg_vol          = data["Volume"].rolling(20).mean()
        data["RelVol"]   = data["Volume"] / avg_vol.where(avg_vol > 0, float("nan"))

        plot_data = data.tail(90).copy()
        for col in ["SMA9", "SMA21", "SMA50", "SMA200"]:
            plot_data[col] = plot_data[col].ffill().bfill()

        vcolors = []
        for rv in plot_data["RelVol"]:
            if rv >= 3:   vcolors.append("#FF4500")
            elif rv >= 2: vcolors.append("#FFA500")
            elif rv >= 1.5: vcolors.append("#90EE90")
            else:           vcolors.append("#4a4a4a")

        def _ap(series, **kwargs):
            if series.notna().sum() < 2:
                return None
            return mpf.make_addplot(series, **kwargs)

        apds = [ap for ap in [
            _ap(plot_data["SMA9"],   color="#00BFFF", width=0.9),
            _ap(plot_data["SMA21"],  color="#FFA500", width=0.9),
            _ap(plot_data["SMA50"],  color="#32CD32", width=1.3),
            _ap(plot_data["SMA200"], color="#FF4500", width=1.6),
            _ap(plot_data["RSI"],                                    panel=2, color="#9370DB", width=1.0, ylabel="RSI"),
            _ap(pd.Series(70.0, index=plot_data.index, dtype=float), panel=2, color="#FF6666", width=0.5, linestyle="--"),
            _ap(pd.Series(30.0, index=plot_data.index, dtype=float), panel=2, color="#66FF66", width=0.5, linestyle="--"),
        ] if ap is not None]

        style = mpf.make_mpf_style(
            base_mpf_style="nightclouds",
            gridstyle="--", gridcolor="#2a2a2a",
            facecolor="#141414", edgecolor="#2a2a2a",
            figcolor="#141414", y_on_right=True,
        )

        fig, axes = mpf.plot(
            plot_data, type="candle", style=style, addplot=apds,
            volume=True, figsize=(14, 9),
            title=f"\n{ticker} — 90D Daily  |  SMA: 9(blue) 21(orange) 50(green) 200(red)",
            panel_ratios=(4, 1.2, 1.8), returnfig=True,
        )

        if len(axes) > 1:
            bars = axes[1].patches
            for bar, color in zip(bars, vcolors[-len(bars):]):
                bar.set_facecolor(color)
                bar.set_alpha(0.85)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=110, bbox_inches="tight", facecolor="#141414")
        plt.close(fig)
        buf.seek(0)
        return buf
    except Exception as e:
        print(f"  ⚠️  Chart failed for {ticker}: {e}")
        return None


# ─── PDF generation ───────────────────────────────────────────────────────────

def _safe(text):
    s = str(text)
    s = s.replace('—', '-').replace('–', '-').replace('—', '-').replace('–', '-')
    s = s.replace('‘', "'").replace('’', "'").replace('“', '"').replace('”', '"')
    return re.sub(r'[^\x00-\xFF]', '', s).strip()


def generate_pdf(results, scan_type, hist_cache, report_label="SMID BREAKOUT SCANNER",
                 insider_transactions=None, institutional_data=None, filings_13=None,
                 smart_money=None, volume_intelligence=None):
    now     = datetime.now(ET)
    ts      = now.strftime("%B %d, %Y  |  %I:%M %p ET")
    buckets = {"A": [], "B": [], "C": []}
    for r in results:
        g = str(r.get("score", ""))[:1]
        if g in buckets:
            buckets[g].append(r["ticker"])

    NAVY   = (12, 20, 48)
    NAVY2  = (22, 34, 70)
    GOLD   = (255, 200, 0)
    WHITE  = (255, 255, 255)
    INK    = (15, 20, 50)
    MUTED  = (100, 110, 135)

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=12)

    # ════════════════════════════════════════════════════════════════════════════
    # COVER PAGE
    # ════════════════════════════════════════════════════════════════════════════
    pdf.add_page()

    # Header
    pdf.set_fill_color(*NAVY)
    pdf.rect(0, 0, 210, 44, "F")
    pdf.set_text_color(*WHITE)
    pdf.set_font("Helvetica", "B", 21)
    pdf.set_xy(0, 7)
    pdf.cell(210, 11, _safe(report_label), align="C")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_xy(0, 20)
    pdf.cell(210, 6, _safe(scan_type), align="C")
    pdf.set_xy(0, 28)
    pdf.cell(210, 6, _safe(ts), align="C")
    pdf.set_xy(0, 36)
    pdf.cell(210, 5,
        f"{len(results)} setups identified  |  {len(buckets['A'])} A-Grade  |  "
        f"{len(buckets['B'])} B-Grade  |  {len(buckets['C'])} C-Grade",
        align="C")
    pdf.set_fill_color(*GOLD)
    pdf.rect(0, 43, 210, 1.5, "F")

    # Grade buckets
    pdf.set_text_color(*INK)
    pdf.set_xy(10, 51)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(0, 5, "Grade Summary")
    pdf.ln(5)

    col_w = 62
    grade_meta = [
        ("A - Breakout Setup", buckets["A"], (34, 153, 84),  (220, 245, 230)),
        ("B - Strong Setup",   buckets["B"], (41, 128, 185), (220, 235, 250)),
        ("C - Watch List",     buckets["C"], (194, 120, 3),  (250, 240, 215)),
    ]
    pdf.set_font("Helvetica", "B", 8)
    for label, tickers, hdr_rgb, _ in grade_meta:
        pdf.set_fill_color(*hdr_rgb)
        pdf.set_text_color(*WHITE)
        pdf.cell(col_w, 6, f"  {label} ({len(tickers)})", border=1, fill=True)
    pdf.ln()
    max_r = max(len(gm[1]) for gm in grade_meta) or 1
    for i in range(max_r):
        for _, tickers, _, bg in grade_meta:
            val = tickers[i] if i < len(tickers) else ""
            pdf.set_fill_color(*bg)
            pdf.set_text_color(*INK)
            pdf.set_font("Helvetica", "B" if val else "", 8)
            pdf.cell(col_w, 5, val, border=1, fill=True, align="C")
        pdf.ln()

    # Summary table
    # Cols: Gr(7)+Ticker(13)+Company(28)+Theme(26)+Price(13)+Chg%(12)+Cap(14)+Float(13)+RS/SPY(14)+52W Hi(13)+Catalyst(37) = 190
    pdf.ln(5)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*INK)
    pdf.cell(0, 5, "All Setups")
    pdf.ln(5)

    CAT_W = 37
    cols = [
        ("Gr", 7), ("Ticker", 13), ("Company", 28), ("Theme", 26),
        ("Price", 13), ("Chg %", 12), ("Cap", 14), ("Float", 13),
        ("RS/SPY", 14), ("52W Hi", 13), ("Catalyst", CAT_W),
    ]
    pdf.set_fill_color(*NAVY)
    pdf.set_text_color(*WHITE)
    pdf.set_font("Helvetica", "B", 7)
    for name, w in cols:
        pdf.cell(w, 6, name, border=1, fill=True, align="C")
    pdf.ln()

    pdf.set_text_color(*INK)
    for i, r in enumerate(results):
        grade = str(r.get("score", ""))[:1]
        chg   = r.get("changePercent", 0) or 0
        rs    = r.get("rsVsSpy", r.get("rs_vs_spy", 0)) or 0
        prox  = r.get("prox52w", r.get("prox_52w", 0)) or 0
        chg_s = f"+{chg:.1f}%" if chg >= 0 else f"{chg:.1f}%"
        rs_s  = f"+{rs:.1f}" if rs >= 0 else f"{rs:.1f}"
        theme = _safe(r.get("theme", r.get("sector", "")))
        cat   = _safe(r.get("catalyst", ""))[:22]

        bg = (220,245,230) if grade=="A" else (220,235,250) if grade=="B" else (250,240,215) if grade=="C" \
             else ((248,248,252) if i%2==0 else (255,255,255))
        pdf.set_fill_color(*bg)
        pdf.set_font("Helvetica", "B" if grade=="A" else "", 7)
        row = [
            (grade,                                  7),
            (r.get("ticker", ""),                   13),
            (_safe(r.get("company", ""))[:16],       28),
            (theme[:17],                             26),
            (f"${r.get('price', 0):.2f}",           13),
            (chg_s,                                  12),
            (f"${r.get('marketCapB', 0):.1f}B",     14),
            (f"{r.get('floatM', 0):.0f}M",          13),
            (rs_s,                                   14),
            (f"{prox:.0f}%",                         13),
            (cat,                                    CAT_W),
        ]
        for val, w in row:
            pdf.cell(w, 5, val, border=1, fill=True, align="C")
        pdf.ln()

    # Cover footer — disable auto page break so the footer cell doesn't trigger a phantom page 2
    pdf.set_auto_page_break(auto=False)
    pdf.set_xy(10, 287)
    pdf.set_font("Helvetica", "I", 5.5)
    pdf.set_text_color(150, 150, 150)
    pdf.cell(0, 4,
        "Not financial advice. For informational purposes only. Do your own due diligence.",
        align="C")

    # ════════════════════════════════════════════════════════════════════════════
    # PER-TICKER ONE-PAGER  (A-grade; same layout as setup_builder)
    # Header 0-32 | Gold line | Subheader 33-40 | Left stats 41-~ | Right analysis 41-~ | Chart below
    # ════════════════════════════════════════════════════════════════════════════
    # Single-ticker (ad-hoc) mode renders ALL grades; standard scans render A/B only
    if len(results) == 1:
        a_grades = results
    else:
        a_grades = [r for r in results if str(r.get("score", ""))[:1] in ("A", "B")]
    for s in a_grades:
        ticker  = s["ticker"]
        company = _safe(s.get("company", ticker))
        price   = s.get("price", 0) or 0
        chg     = s.get("changePercent", 0) or 0
        cap_b   = s.get("marketCapB", 0) or 0
        fl_m    = s.get("floatM", 0) or 0
        rs      = s.get("rsVsSpy", s.get("rs_vs_spy", 0)) or 0
        prox    = s.get("prox52w", s.get("prox_52w", 0)) or 0
        bt      = s.get("baseTight", s.get("base_tight", "-"))
        vol_avg = s.get("volumeVsAvg", "-")
        chg_s   = f"+{chg:.2f}%" if chg >= 0 else f"{chg:.2f}%"
        rs_s    = f"+{rs:.1f}%" if rs >= 0 else f"{rs:.1f}%"

        pdf.add_page()
        pdf.set_auto_page_break(auto=False)

        # ── Header (0-32) ────────────────────────────────────────────────────
        pdf.set_fill_color(*NAVY)
        pdf.rect(0, 0, 210, 32, "F")

        pdf.set_text_color(*WHITE)
        pdf.set_font("Helvetica", "B", 26)
        pdf.set_xy(10, 3)
        pdf.cell(60, 14, ticker)

        pdf.set_font("Helvetica", "", 8)
        pdf.set_xy(10, 19)
        pdf.cell(95, 5, company[:42])

        # Price + change (center)
        pdf.set_font("Helvetica", "B", 16)
        pdf.set_xy(78, 4)
        pdf.cell(54, 10, f"${price:.2f}", align="C")
        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_xy(78, 15)
        pdf.cell(54, 5, chg_s, align="C")
        pdf.set_xy(78, 22)
        pdf.cell(54, 5, f"RS vs SPY: {rs_s}", align="C")

        # Grade badge (right)
        grade_letter = str(s.get("score", ""))[:1]
        if grade_letter == "A":
            badge_rgb, badge_label = (34, 153, 84),  "A  BREAKOUT"
        elif grade_letter == "B":
            badge_rgb, badge_label = (41, 128, 185), "B  STRONG SETUP"
        elif grade_letter == "C":
            badge_rgb, badge_label = (200, 130, 20), "C  WATCH LIST"
        else:
            badge_rgb, badge_label = (110, 110, 120), grade_letter or "—"
        pdf.set_fill_color(*badge_rgb)
        pdf.rect(140, 4, 62, 24, "F")
        pdf.set_text_color(*WHITE)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_xy(140, 7)
        pdf.cell(62, 8, badge_label, align="C")
        pdf.set_font("Helvetica", "", 6.5)
        pdf.set_xy(140, 17)
        pdf.cell(62, 5, f"${cap_b:.1f}B Cap  |  {fl_m:.0f}M Float", align="C")

        # Gold accent
        pdf.set_fill_color(*GOLD)
        pdf.rect(0, 32, 210, 1.2, "F")

        # ── Subheader strip (33-40) ──────────────────────────────────────────
        pdf.set_fill_color(*NAVY2)
        pdf.rect(0, 33.2, 210, 7, "F")
        pdf.set_text_color(180, 210, 255)
        pdf.set_font("Helvetica", "", 7)
        pdf.set_xy(10, 34.8)
        above20  = "Above 20MA: YES" if s.get("above20ma", True) else "Above 20MA: no"
        earn     = _safe(s.get("earningsFlag", s.get("earnings_flag", "")))
        earn_str = f"  |  {earn}" if earn else ""
        industry = _safe(s.get("industry", s.get("sector", "")))
        theme    = _safe(s.get("theme", ""))
        subhdr   = f"{industry}  |  Theme: {theme}  |  52W Hi: {prox:.0f}%  |  Base Tight: {bt}  |  Vol: {vol_avg}  |  RS/SPY: {rs_s}  |  {above20}{earn_str}"
        pdf.cell(0, 4, subhdr)

        # ── Left column — quick stats (x=10, y=41, w=92) ────────────────────
        MX, MY, MW = 10, 41, 92
        pdf.set_fill_color(*NAVY)
        pdf.set_text_color(*WHITE)
        pdf.set_font("Helvetica", "B", 7)
        pdf.set_xy(MX, MY)
        pdf.cell(MW, 5, "  SETUP METRICS", fill=True)

        # Fundamentals: prefer P/S over P/E for unprofitable companies (negative fwd P/E is meaningless)
        fwd_pe = s.get("forward_pe", 0) or 0
        ps     = s.get("ps_ratio", 0) or 0
        if fwd_pe and fwd_pe > 0:
            valuation_label, valuation_val = "Fwd P/E", f"{fwd_pe:.1f}x"
        elif ps and ps > 0:
            valuation_label, valuation_val = "P/Sales", f"{ps:.1f}x"
        else:
            valuation_label, valuation_val = "Fwd P/E", "—"

        # Days to cover — yfinance shortRatio IS days-to-cover
        short_ratio = s.get("short_ratio", 0) or 0
        short_pct   = s.get("short_pct", "")
        d2c_str     = f"{short_ratio:.1f}d" if short_ratio else "—"

        # Inst. own — display dash for missing data instead of "0%"
        inst_own_raw = s.get("inst_own", "")
        if inst_own_raw in (None, "", "0%", "0.0%", 0, "0", "0.0"):
            inst_own_str = "— (data lag)"
        else:
            inst_own_str = str(inst_own_raw)

        stats = [
            ("Price",         f"${price:.2f}"),
            ("Day Change",    chg_s),
            ("Mkt Cap",       f"${cap_b:.2f}B"),
            ("Float",         f"{fl_m:.0f}M sh"),
            ("RS vs SPY",     rs_s),
            ("52W Hi Prox",   f"{prox:.0f}%"),
            ("Base Tight",    str(bt)),
            ("Vol vs Avg",    str(vol_avg)),
            ("RS Line Hi",    "YES" if s.get("rsLineNewHigh", s.get("rs_line_new_high")) else "no"),
            ("Above 50MA",    "YES" if s.get("above50ma", s.get("above_50ma")) else "-"),
            (valuation_label, valuation_val),
            ("Short %",       short_pct or "—"),
            ("Days to Cover", d2c_str),
            ("Inst. Own",     inst_own_str),
        ]
        LW, VW = 26, 20
        row_y = MY + 5
        for idx, (lbl, val) in enumerate(stats):
            bg = (245, 247, 252) if idx % 2 == 0 else (255, 255, 255)
            pdf.set_fill_color(*bg)
            pdf.set_xy(MX, row_y)
            pdf.set_text_color(*MUTED)
            pdf.set_font("Helvetica", "", 5.8)
            pdf.cell(LW, 4, lbl, fill=True)
            pdf.set_text_color(*INK)
            pdf.set_font("Helvetica", "B", 6.2)
            pdf.cell(VW + 26, 4, _safe(val), fill=True)
            row_y += 4
        metrics_end_y = row_y

        # ── Right column — analysis (x=108, y=41, w=92) ─────────────────────
        CX, CY, CW = 108, 41, 92
        pdf.set_fill_color(*NAVY)
        pdf.set_text_color(*WHITE)
        pdf.set_font("Helvetica", "B", 7)
        pdf.set_xy(CX, CY)
        pdf.cell(CW, 5, "  ANALYSIS", fill=True)

        cat_y = CY + 5

        def _section(label, text, hdr_rgb=(30, 50, 90)):
            nonlocal cat_y
            txt = _safe(str(text or "")).strip()
            if not txt or txt == "-":
                return
            pdf.set_fill_color(*hdr_rgb)
            pdf.set_text_color(200, 220, 255)
            pdf.set_font("Helvetica", "B", 5.5)
            pdf.set_xy(CX, cat_y)
            pdf.cell(CW, 3.5, f"  {label.upper()}", fill=True)
            cat_y += 3.5
            pdf.set_text_color(*INK)
            pdf.set_font("Helvetica", "", 6.3)
            pdf.set_xy(CX, cat_y)
            pdf.multi_cell(CW, 3.8, txt, border=0)
            cat_y = pdf.get_y() + 1.5

        _section("Catalyst / Today's Driver", s.get("catalyst", ""),              (20, 80, 55))
        _section("Business & Position",      s.get("businessDescription", ""),  (15, 55, 95))
        _section("Factor & Theme",           s.get("factorExposure", ""),       (20, 60, 80))
        _section("Breakout Signal",          s.get("signal", ""),               (15, 80, 55))
        _section("Institutional",            s.get("institutionalAngle", ""),   (60, 55, 10))
        _section("Earnings",                 s.get("earningsFlag", s.get("earnings_flag", "")), (55, 25, 90))
        _section("Earnings Context",         s.get("earningsContext", ""),      (50, 20, 80))
        _section("Key Risk",                 s.get("keyRisk", ""),              (110, 25, 25))
        _section("Analysis",                 s.get("reasoning", ""),            (30, 50, 90))

        # Entry / Stop box
        cat_y += 1
        pdf.set_fill_color(220, 245, 230)
        pdf.rect(CX, cat_y, CW, 13, "F")
        pdf.set_draw_color(34, 153, 84)
        pdf.rect(CX, cat_y, CW, 13, "D")
        pdf.set_text_color(*INK)
        pdf.set_font("Helvetica", "B", 6.0)
        signal = _safe(str(s.get("signal", s.get("breakoutLevel", "-"))))[:55]
        pdf.set_xy(CX + 1, cat_y + 1.5)
        pdf.cell(CW - 2, 4, f"Signal: {signal}", border=0)
        pdf.set_xy(CX + 1, cat_y + 6)
        vol_txt = _safe(str(s.get("volumeVsAvg", "-")))
        pdf.cell(CW - 2, 4, f"Vol vs Avg: {vol_txt}", border=0)

        # ── Chart ────────────────────────────────────────────────────────────
        chart_y = max(metrics_end_y, cat_y + 14) + 3
        # If columns ran tall, push chart to page 2 instead of squeezing it
        avail_h = 291 - chart_y
        if avail_h < 80:
            pdf.add_page()
            pdf.set_fill_color(12, 20, 48)
            pdf.rect(0, 0, 210, 14, "F")
            pdf.set_text_color(255, 255, 255)
            pdf.set_font("Helvetica", "B", 11)
            pdf.set_xy(10, 4)
            pdf.cell(0, 6, _safe(f"{ticker} - Technical Chart"))
            pdf.set_fill_color(255, 200, 0)
            pdf.rect(0, 14, 210, 1.2, "F")
            chart_y = 18
            avail_h = 291 - chart_y

        if ticker in hist_cache:
            chart_buf = generate_chart(ticker, hist_cache[ticker])
            if chart_buf:
                chart_h = min(avail_h, round(190 / 1.556, 1))
                if avail_h >= 50:
                    pdf.image(chart_buf, x=10, y=chart_y, w=190, h=chart_h)

        # Per-page footer
        pdf.set_xy(10, 291)
        pdf.set_font("Helvetica", "I", 5.0)
        pdf.set_text_color(150, 150, 150)
        pdf.cell(0, 4,
            f"SMID Breakout Scanner  |  {now.strftime('%b %d %Y')}  |  "
            "Not financial advice. For informational purposes only. Do your own due diligence.",
            align="C")

    # ── Insider Transactions Table (ad-hoc mode only) ───────────────────────
    if insider_transactions:
        pdf.add_page()
        # Header
        pdf.set_fill_color(12, 20, 48)
        pdf.rect(0, 0, 210, 22, "F")
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 14)
        pdf.set_xy(10, 5)
        pdf.cell(0, 7, "INSIDER TRANSACTIONS - LAST 12 MONTHS")
        pdf.set_font("Helvetica", "", 8)
        pdf.set_xy(10, 13)
        pdf.cell(0, 5, "Source: SEC EDGAR Form 4  |  P=open buy, S=sale, M=opt exercise, A=grant, F=tax withhold")
        pdf.set_fill_color(255, 200, 0)
        pdf.rect(0, 22, 210, 1.2, "F")

        # Aggregate summary at top
        buys = [t for t in insider_transactions if t["code"] == "P"]
        sales = [t for t in insider_transactions if t["code"] == "S"]
        buy_val = sum(t["value"] for t in buys)
        sale_val = sum(t["value"] for t in sales)

        pdf.set_text_color(20, 20, 40)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_xy(10, 28)
        pdf.cell(0, 5, "12-Month Summary")
        pdf.set_font("Helvetica", "", 8)
        pdf.set_xy(10, 34)
        summary_line = (f"Open-market buys: {len(buys)} ({_fmt_money(buy_val)})  |  "
                        f"Sales: {len(sales)} ({_fmt_money(sale_val)})  |  "
                        f"Net: {_fmt_money(buy_val - sale_val)}  |  "
                        f"Total transactions: {len(insider_transactions)}")
        pdf.cell(0, 5, _safe(summary_line))

        # Column header
        cols = [
            ("Date",     22),
            ("Insider",  55),
            ("Title",    35),
            ("Action",   18),
            ("Shares",   22),
            ("Price",    18),
            ("Value",    22),
        ]
        y = 44
        pdf.set_xy(10, y)
        pdf.set_fill_color(12, 20, 48)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 7.5)
        for name, w in cols:
            pdf.cell(w, 6, name, border=1, fill=True, align="C")
        pdf.ln()
        y += 6

        # Rows — strong color coding: BUY = green tint + green action chip,
        #                           SALE = red tint + red action chip,
        #                           neutral = grey
        max_rows = 40
        for idx, t in enumerate(insider_transactions[:max_rows]):
            code = t["code"]
            if code == "P":           # Open-market buy
                row_bg     = (200, 240, 215)   # bright green tint
                action_bg  = (39, 174, 96)     # solid green chip
                action_fg  = (255, 255, 255)
                text_color = (15, 80, 35)
                bold       = True
            elif code == "S":         # Sale
                row_bg     = (255, 215, 215)   # bright red tint
                action_bg  = (192, 57, 43)     # solid red chip
                action_fg  = (255, 255, 255)
                text_color = (110, 25, 25)
                bold       = True
            elif code == "M":         # Option exercise
                row_bg     = (245, 235, 215)
                action_bg  = (200, 130, 20)
                action_fg  = (255, 255, 255)
                text_color = (90, 60, 15)
                bold       = False
            elif code == "A":         # Grant
                row_bg     = (230, 235, 250)
                action_bg  = (52, 100, 180)
                action_fg  = (255, 255, 255)
                text_color = (35, 50, 100)
                bold       = False
            else:                     # F (tax withhold), G (gift), D (disposition), etc.
                row_bg     = (245, 247, 252) if idx % 2 == 0 else (255, 255, 255)
                action_bg  = (130, 130, 140)
                action_fg  = (255, 255, 255)
                text_color = (60, 60, 60)
                bold       = False

            # First six columns — Date, Insider, Title, ...skip Action..., Shares, Price, Value
            cols_left  = [
                (t["date"][:10],         22, "C"),
                (_safe(t["owner"][:30]), 55, "L"),
                (_safe(t["title"][:22]), 35, "L"),
            ]
            cols_right = [
                (f"{int(t['shares']):,}", 22, "R"),
                (f"${t['price']:.2f}",    18, "R"),
                (_fmt_money(t["value"]),  22, "R"),
            ]

            pdf.set_fill_color(*row_bg)
            pdf.set_text_color(*text_color)
            pdf.set_font("Helvetica", "B" if bold else "", 7)
            for val, w, align in cols_left:
                pdf.cell(w, 5, val, border=1, fill=True, align=align)

            # Action column — solid colored chip with white text
            pdf.set_fill_color(*action_bg)
            pdf.set_text_color(*action_fg)
            pdf.set_font("Helvetica", "B", 7)
            pdf.cell(18, 5, t["code_label"], border=1, fill=True, align="C")

            # Resume row tint for shares/price/value
            pdf.set_fill_color(*row_bg)
            pdf.set_text_color(*text_color)
            pdf.set_font("Helvetica", "B" if bold else "", 7)
            for val, w, align in cols_right:
                pdf.cell(w, 5, val, border=1, fill=True, align=align)
            pdf.ln()

        if len(insider_transactions) > max_rows:
            pdf.set_font("Helvetica", "I", 6)
            pdf.set_text_color(120, 120, 120)
            pdf.ln(2)
            pdf.cell(0, 4, f"... showing {max_rows} of {len(insider_transactions)} total transactions", align="C")

        # Footer
        pdf.set_xy(10, 291)
        pdf.set_font("Helvetica", "I", 5.0)
        pdf.set_text_color(150, 150, 150)
        pdf.cell(0, 4, "Source: SEC EDGAR  |  Not financial advice  |  All filings subject to standard SEC reporting delays.",
                 align="C")

    # ── Institutional Ownership Page (ad-hoc mode only) ─────────────────────
    if institutional_data and (institutional_data.get("top_holders") or institutional_data.get("major_holders")):
        pdf.add_page()

        # Header
        pdf.set_fill_color(12, 20, 48)
        pdf.rect(0, 0, 210, 22, "F")
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 14)
        pdf.set_xy(10, 5)
        pdf.cell(0, 7, "INSTITUTIONAL OWNERSHIP & FILINGS")
        pdf.set_font("Helvetica", "", 8)
        pdf.set_xy(10, 13)
        pdf.cell(0, 5, "Source: yfinance (13F snapshot) + SEC EDGAR (13D/13G filings)")
        pdf.set_fill_color(255, 200, 0)
        pdf.rect(0, 22, 210, 1.2, "F")

        # Major holders breakdown — 4 stat boxes
        mh = institutional_data.get("major_holders", {})
        insider_pct = mh.get("insidersPercentHeld", 0) or 0
        inst_pct    = mh.get("institutionsPercentHeld", 0) or 0
        float_pct   = mh.get("institutionsFloatPercentHeld", 0) or 0
        inst_count  = int(mh.get("institutionsCount", 0) or 0)

        # 4 stat boxes — width 46, gap 1.5, total = 4*46 + 3*1.5 = 188.5mm fits in 190mm usable
        box_w, box_h = 46, 18
        box_gap = 1.5
        box_y = 28
        boxes = [
            ("Insider %",       f"{insider_pct*100:.2f}%" if insider_pct else "—",       (45, 65, 110)),
            ("Institutional %", f"{inst_pct*100:.1f}%"    if inst_pct    else "—",       (39, 110, 80)),
            ("Inst. Float %",   f"{float_pct*100:.1f}%"   if float_pct   else "—",       (52, 100, 180)),
            ("# Institutions",  f"{inst_count:,}"          if inst_count  else "—",       (110, 75, 130)),
        ]
        for i, (lbl, val, color) in enumerate(boxes):
            x = 10 + i * (box_w + box_gap)
            pdf.set_fill_color(*color)
            pdf.rect(x, box_y, box_w, box_h, "F")
            pdf.set_text_color(220, 230, 245)
            pdf.set_font("Helvetica", "", 7)
            pdf.set_xy(x + 1, box_y + 1.5)
            pdf.cell(box_w - 2, 4, lbl, align="C")
            pdf.set_text_color(255, 255, 255)
            pdf.set_font("Helvetica", "B", 13)
            pdf.set_xy(x + 1, box_y + 7)
            pdf.cell(box_w - 2, 8, val, align="C")

        # Smart money signal chip
        if smart_money:
            sm_y = 50
            color = smart_money.get("color", (130, 130, 140))
            pdf.set_fill_color(*color)
            pdf.rect(10, sm_y, 190, 12, "F")
            pdf.set_text_color(255, 255, 255)
            pdf.set_font("Helvetica", "B", 10)
            pdf.set_xy(10, sm_y + 1.5)
            pdf.cell(190, 5, _safe(f"SMART MONEY SIGNAL: {smart_money.get('label', '')}"), align="C")
            reasons = smart_money.get("reasons", [])
            if reasons:
                pdf.set_font("Helvetica", "", 7)
                pdf.set_xy(10, sm_y + 7)
                pdf.cell(190, 4, _safe(" | ".join(reasons)[:160]), align="C")

        # Top Holders Table
        y = 66
        pdf.set_text_color(20, 20, 40)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_xy(10, y)
        pdf.cell(0, 5, "Top 10 Institutional Holders")
        y += 7

        cols = [
            ("Fund",          82),
            ("% Out",         18),
            ("Shares",        28),
            ("Value",         24),
            ("Reported",      20),
            ("Type",          18),
        ]
        pdf.set_xy(10, y)
        pdf.set_fill_color(12, 20, 48)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 7.5)
        for name, w in cols:
            pdf.cell(w, 6, name, border=1, fill=True, align="C")
        pdf.ln()
        y += 6

        holders = institutional_data.get("top_holders", [])
        for idx, h in enumerate(holders[:10]):
            cat, color = classify_fund(h["fund"])
            row_bg = (220, 245, 230) if cat == "Smart $" else (245, 247, 252) if idx % 2 == 0 else (255, 255, 255)

            pdf.set_fill_color(*row_bg)
            pdf.set_text_color(20, 20, 40)
            pdf.set_font("Helvetica", "B" if cat == "Smart $" else "", 7)

            shares_str = f"{h['shares']/1e6:.1f}M" if h['shares'] >= 1e6 else f"{h['shares']:,}"
            value_str  = _fmt_money(h['value'])
            pct_str    = f"{h['pct_out']*100:.2f}%" if h['pct_out'] < 1 else f"{h['pct_out']:.2f}%"

            pdf.cell(82, 5, _safe(h["fund"][:50]),       border=1, fill=True, align="L")
            pdf.cell(18, 5, pct_str,                     border=1, fill=True, align="R")
            pdf.cell(28, 5, shares_str,                  border=1, fill=True, align="R")
            pdf.cell(24, 5, value_str,                   border=1, fill=True, align="R")
            pdf.cell(20, 5, h["date_reported"][:10],     border=1, fill=True, align="C")
            # Type chip
            pdf.set_fill_color(*color)
            pdf.set_text_color(255, 255, 255)
            pdf.set_font("Helvetica", "B", 7)
            pdf.cell(18, 5, cat,                         border=1, fill=True, align="C")
            pdf.ln()
            y += 5

        if not holders:
            pdf.set_font("Helvetica", "I", 8)
            pdf.set_text_color(120, 120, 120)
            pdf.cell(190, 5, "No institutional holder data available from yfinance for this ticker.", align="C")
            pdf.ln(5)
            y += 5

        # 13D/13G filings table
        y += 5
        pdf.set_text_color(20, 20, 40)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_xy(10, y)
        pdf.cell(0, 5, "Recent 13D / 13G Filings (last 12 months)")
        pdf.set_font("Helvetica", "", 7)
        pdf.set_xy(10, y + 5)
        pdf.cell(0, 4, "13D = activist stake, board pressure, takeover  |  13G = passive 5%+ holder, lower-conviction")
        y += 12

        if filings_13:
            f_cols = [("Date", 22), ("Form", 20), ("Filer", 100), ("Stake", 22), ("Type", 26)]
            pdf.set_xy(10, y)
            pdf.set_fill_color(12, 20, 48)
            pdf.set_text_color(255, 255, 255)
            pdf.set_font("Helvetica", "B", 7.5)
            for name, w in f_cols:
                pdf.cell(w, 6, name, border=1, fill=True, align="C")
            pdf.ln()
            y += 6

            for idx, fi in enumerate(filings_13[:15]):
                if fi.get("is_active"):
                    row_bg = (255, 215, 215)        # 13D = activist (red tint = high signal)
                    type_label, type_color = "ACTIVIST", (192, 57, 43)
                else:
                    row_bg = (245, 247, 252) if idx % 2 == 0 else (255, 255, 255)
                    type_label, type_color = "Passive 5%+", (130, 130, 140)

                pdf.set_fill_color(*row_bg)
                pdf.set_text_color(20, 20, 40)
                pdf.set_font("Helvetica", "B" if fi.get("is_active") else "", 7)
                pdf.cell(22, 5, fi["date"],                  border=1, fill=True, align="C")
                pdf.cell(20, 5, fi["form"],                  border=1, fill=True, align="C")
                pdf.cell(100, 5, _safe(fi["filer"][:60]),    border=1, fill=True, align="L")
                pdf.cell(22, 5, fi["stake"],                 border=1, fill=True, align="R")
                pdf.set_fill_color(*type_color)
                pdf.set_text_color(255, 255, 255)
                pdf.set_font("Helvetica", "B", 7)
                pdf.cell(26, 5, type_label,                  border=1, fill=True, align="C")
                pdf.ln()
        else:
            pdf.set_font("Helvetica", "I", 8)
            pdf.set_text_color(120, 120, 120)
            pdf.set_xy(10, y)
            pdf.cell(190, 5, "No 13D or 13G filings against this company in the last 12 months.", align="C")

        # ── Interpretation Panel: "What does this ownership profile tell us?" ──
        pp_y = 220  # anchored near bottom of page to fill whitespace
        pdf.set_fill_color(245, 247, 252)
        pdf.rect(10, pp_y, 190, 60, "F")
        pdf.set_draw_color(12, 20, 48)
        pdf.rect(10, pp_y, 190, 60, "D")

        pdf.set_text_color(12, 20, 48)
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_xy(12, pp_y + 1.5)
        pdf.cell(0, 5, "Ownership Interpretation")

        # Build interpretive bullets based on the data
        interpret_lines = []

        # Smart money interpretation
        sm_count = (institutional_data.get("smart_money_count", 0) if institutional_data else 0)
        passive_count = sum(1 for h in (institutional_data.get("top_holders", []) if institutional_data else []) if classify_fund(h["fund"])[0] == "Passive")
        active_count  = sum(1 for h in (institutional_data.get("top_holders", []) if institutional_data else []) if classify_fund(h["fund"])[0] == "Active")

        if sm_count >= 3:
            interpret_lines.append(("Smart Money:", f"{sm_count} known active stock-pickers in top holders — high-conviction signal."))
        elif sm_count >= 1:
            interpret_lines.append(("Smart Money:", f"{sm_count} known active fund(s) detected — partial conviction signal. Monitor for more entries."))
        elif passive_count >= 5 and active_count <= 2:
            interpret_lines.append(("Smart Money:", "Top holders are dominated by passive/index funds. No high-conviction stock-pickers detected — ownership is mostly index-driven, not research-driven."))
        else:
            interpret_lines.append(("Smart Money:", f"Top holders skew {passive_count} passive / {active_count} active. No high-conviction smart-money names identified."))

        # Concentration interpretation
        if institutional_data:
            holders = institutional_data.get("top_holders", [])
            top10_pct = sum(h.get("pct_out", 0) for h in holders[:10])
            if top10_pct >= 0.40:
                interpret_lines.append(("Concentration:", f"Top 10 hold {top10_pct*100:.0f}% — heavy concentration. Outsized impact from any single fund's exit."))
            elif top10_pct >= 0.25:
                interpret_lines.append(("Concentration:", f"Top 10 hold {top10_pct*100:.0f}% — moderate concentration, normal for SMID."))
            elif top10_pct > 0:
                interpret_lines.append(("Concentration:", f"Top 10 hold {top10_pct*100:.0f}% — diffuse ownership, lower single-fund risk."))

        # 13D/G interpretation
        if filings_13:
            active_filings = sum(1 for f in filings_13 if f.get("is_active"))
            passive_filings = sum(1 for f in filings_13 if not f.get("is_active"))
            if active_filings >= 1:
                interpret_lines.append(("13D Activist:", f"{active_filings} 13D filing(s) in last 12 months — active stake building, possible board pressure or M&A interest."))
            elif passive_filings >= 1:
                interpret_lines.append(("13G 5%+:", f"{passive_filings} 5%+ passive filings — large funds quietly building positions without activist intent."))
        else:
            interpret_lines.append(("13D/13G:", "No 5%+ stake filings in last 12 months. Either nobody's gone over the threshold or the company is too large for 5% to be common (>$10B caps rarely see 13D/G activity)."))

        # Insider context
        if insider_transactions:
            buys = [t for t in insider_transactions if t["code"] == "P"]
            sales = [t for t in insider_transactions if t["code"] == "S"]
            if len(buys) >= 3:
                interpret_lines.append(("Insider Tape:", f"{len(buys)} open-market buys vs {len(sales)} sales in last 12 months — net bullish insider behavior."))
            elif len(buys) >= 1:
                interpret_lines.append(("Insider Tape:", f"{len(buys)} buy / {len(sales)} sales over 12 months — mixed picture, watch for cluster patterns."))
            else:
                interpret_lines.append(("Insider Tape:", f"{len(sales)} insider sales in 12 months, no open-market purchases — neutral to bearish (but typical for unprofitable growth companies)."))

        # Render
        pdf.set_text_color(20, 20, 40)
        line_y = pp_y + 8
        for label, text in interpret_lines[:5]:
            pdf.set_font("Helvetica", "B", 7.5)
            pdf.set_xy(12, line_y)
            pdf.cell(28, 4.5, label)
            pdf.set_font("Helvetica", "", 7.5)
            pdf.set_xy(40, line_y)
            pdf.multi_cell(168, 4.5, _safe(text))
            line_y = pdf.get_y() + 1.5

        # Footer
        pdf.set_xy(10, 291)
        pdf.set_font("Helvetica", "I", 5.0)
        pdf.set_text_color(150, 150, 150)
        pdf.cell(0, 4,
            "Smart-money classifications are heuristic. 13F filings are reported with a 45-day lag. "
            "Cross-reference with WhaleWisdom or Fintel for production decisions.",
            align="C")

    # ── Price/Volume Intelligence Page (ad-hoc mode only) ─────────────────────
    if volume_intelligence:
        vi = volume_intelligence
        ad      = vi.get("ad_rating", {})
        bars    = vi.get("sig_bars", [])
        monthly = vi.get("monthly_flow", [])
        silent  = vi.get("silent_build", {})

        pdf.add_page()

        # Header
        pdf.set_fill_color(12, 20, 48)
        pdf.rect(0, 0, 210, 22, "F")
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 14)
        pdf.set_xy(10, 5)
        pdf.cell(0, 7, "INSTITUTIONAL FOOTPRINT - PRICE / VOLUME INTELLIGENCE")
        pdf.set_font("Helvetica", "", 8)
        pdf.set_xy(10, 13)
        pdf.cell(0, 5, "What institutions are doing right now (the price/volume tape never lies)")
        pdf.set_fill_color(255, 200, 0)
        pdf.rect(0, 22, 210, 1.2, "F")

        # ── BIG A/D RATING (left half) + SILENT BUILD STATUS (right half) ─────
        ad_y = 28
        # Left: massive letter grade
        ad_grade = ad.get("grade", "—")
        ad_color_map = {
            "A": (39, 174, 96),    # bright green
            "B": (90, 180, 110),   # lime green
            "C": (200, 200, 200),  # neutral grey
            "D": (220, 130, 70),   # orange
            "E": (192, 57, 43),    # red
        }
        ad_color = ad_color_map.get(ad_grade, (130, 130, 140))

        pdf.set_fill_color(*ad_color)
        pdf.rect(10, ad_y, 90, 50, "F")
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 56)
        pdf.set_xy(10, ad_y + 2)
        pdf.cell(90, 38, ad_grade, align="C")
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_xy(10, ad_y + 36)
        pdf.cell(90, 5, _safe(ad.get("label", "")), align="C")
        pdf.set_font("Helvetica", "", 8)
        pdf.set_xy(10, ad_y + 42)
        pdf.cell(90, 4, _safe(f"Score: {ad.get('score', 0)}/100"), align="C")
        pdf.set_xy(10, ad_y + 46)
        pdf.cell(90, 4,
                 _safe(f"{ad.get('up_days', 0)} up days | {ad.get('down_days', 0)} down days | {ad.get('neutral_days', 0)} flat (last 65)"),
                 align="C")

        # Right: Silent Build status panel
        sb_x = 105
        sb_detected = silent.get("detected", False)
        sb_color = (39, 174, 96) if sb_detected else (130, 130, 140)
        pdf.set_fill_color(*sb_color)
        pdf.rect(sb_x, ad_y, 95, 50, "F")
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_xy(sb_x, ad_y + 2)
        pdf.cell(95, 6, "SILENT BUILD PATTERN", align="C")
        pdf.set_font("Helvetica", "B", 22)
        pdf.set_xy(sb_x, ad_y + 11)
        pdf.cell(95, 14, "DETECTED" if sb_detected else "NOT PRESENT", align="C")
        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_xy(sb_x + 2, ad_y + 28)
        pdf.multi_cell(91, 4, _safe(silent.get("notes", ""))[:400], align="C")

        # ── MONTHLY FLOW TABLE ──────────────────────────────────────────────
        my = ad_y + 56
        pdf.set_text_color(20, 20, 40)
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_xy(10, my)
        pdf.cell(0, 5, "Monthly Accumulation/Distribution Flow")
        pdf.set_font("Helvetica", "", 8)
        pdf.set_xy(10, my + 5)
        pdf.cell(0, 4,
                 "Trend over last 3 months: are institutions accumulating, distributing, or rotating?")
        my += 12

        flow_cols = [("Period", 35), ("Up-Vol %", 22), ("A/D Trend", 60), ("Price Chg", 22), ("Vol vs Avg", 28), ("Signal", 23)]
        pdf.set_xy(10, my)
        pdf.set_fill_color(12, 20, 48)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 8)
        for name, w in flow_cols:
            pdf.cell(w, 6, name, border=1, fill=True, align="C")
        pdf.ln()
        my += 6

        for i, m in enumerate(monthly):
            ratio = m.get("ratio", 50)
            trend = m.get("trend", "")
            if "Strong Accumulation" in trend:
                row_bg = (200, 240, 215)
                signal, sig_rgb = "BULLISH", (39, 174, 96)
            elif "Accumulation" in trend:
                row_bg = (225, 245, 230)
                signal, sig_rgb = "Bullish", (90, 180, 110)
            elif "Heavy Distribution" in trend:
                row_bg = (255, 215, 215)
                signal, sig_rgb = "BEARISH", (192, 57, 43)
            elif "Distribution" in trend:
                row_bg = (250, 230, 230)
                signal, sig_rgb = "Bearish", (220, 130, 70)
            else:
                row_bg = (245, 247, 252) if i % 2 == 0 else (255, 255, 255)
                signal, sig_rgb = "Neutral", (130, 130, 140)

            pdf.set_fill_color(*row_bg)
            pdf.set_text_color(20, 20, 40)
            pdf.set_font("Helvetica", "B", 8)
            pdf.cell(35, 6, m.get("label", ""),                                 border=1, fill=True, align="L")
            pdf.set_font("Helvetica", "", 8)
            pdf.cell(22, 6, f"{ratio:.1f}%",                                    border=1, fill=True, align="R")
            pdf.cell(60, 6, _safe(trend),                                       border=1, fill=True, align="L")
            chg = m.get("price_chg", 0)
            chg_str = f"+{chg:.1f}%" if chg >= 0 else f"{chg:.1f}%"
            pdf.cell(22, 6, chg_str,                                            border=1, fill=True, align="R")
            pdf.cell(28, 6, f"{m.get('avg_vol_ratio', 1):.2f}x",                border=1, fill=True, align="R")
            # Signal chip
            pdf.set_fill_color(*sig_rgb)
            pdf.set_text_color(255, 255, 255)
            pdf.set_font("Helvetica", "B", 7.5)
            pdf.cell(23, 6, signal,                                             border=1, fill=True, align="C")
            pdf.ln()
            my += 6

        # ── SIGNIFICANT VOLUME BARS (top 12 most extreme) ──────────────────
        my += 8
        pdf.set_text_color(20, 20, 40)
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_xy(10, my)
        pdf.cell(0, 5, "Significant Volume Days (Last 90 Days, >2x Average Volume)")
        pdf.set_font("Helvetica", "", 8)
        pdf.set_xy(10, my + 5)
        pdf.cell(0, 4,
                 "Each row = a day institutions left a clear footprint. Absorption = stealth accumulation (silent buy).")
        my += 12

        bar_cols = [("Date", 22), ("Close", 18), ("Day Chg %", 22), ("Volume", 32), ("Rel Vol", 18), ("Classification", 60), ("Signal", 18)]
        pdf.set_xy(10, my)
        pdf.set_fill_color(12, 20, 48)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 8)
        for name, w in bar_cols:
            pdf.cell(w, 6, name, border=1, fill=True, align="C")
        pdf.ln()
        my += 6

        for i, b in enumerate(bars[:12]):
            cls = b.get("classification", "")
            if cls == "ACCUMULATION":
                row_bg = (200, 240, 215)
                chip_rgb = (39, 174, 96)
            elif cls == "DISTRIBUTION":
                row_bg = (255, 215, 215)
                chip_rgb = (192, 57, 43)
            elif cls == "ABSORPTION":
                row_bg = (255, 240, 195)
                chip_rgb = (200, 130, 20)
            else:
                row_bg = (245, 247, 252) if i % 2 == 0 else (255, 255, 255)
                chip_rgb = (130, 130, 140)

            pdf.set_fill_color(*row_bg)
            pdf.set_text_color(20, 20, 40)
            pdf.set_font("Helvetica", "", 7.5)
            chg = b.get("change_pct", 0)
            chg_str = f"+{chg:.1f}%" if chg >= 0 else f"{chg:.1f}%"
            pdf.cell(22, 5, b["date"],                                          border=1, fill=True, align="C")
            pdf.cell(18, 5, f"${b['close']:.2f}",                               border=1, fill=True, align="R")
            pdf.cell(22, 5, chg_str,                                            border=1, fill=True, align="R")
            pdf.cell(32, 5, f"{b['volume']:,}",                                 border=1, fill=True, align="R")
            pdf.cell(18, 5, f"{b['rel_vol']:.1f}x",                             border=1, fill=True, align="R")
            pdf.cell(60, 5, cls,                                                border=1, fill=True, align="C")
            pdf.set_fill_color(*chip_rgb)
            pdf.set_text_color(255, 255, 255)
            pdf.set_font("Helvetica", "B", 7.5)
            pdf.cell(18, 5, b.get("signal", ""),                                border=1, fill=True, align="C")
            pdf.ln()

        if not bars:
            pdf.set_font("Helvetica", "I", 9)
            pdf.set_text_color(120, 120, 120)
            pdf.set_xy(10, my)
            pdf.cell(190, 5, "No days with >2x average volume in last 90 days. Quiet tape.", align="C")

        # ── Synthesis Panel: aggregate signal interpretation + recommended action ──
        sp_y = 220
        pdf.set_fill_color(245, 247, 252)
        pdf.rect(10, sp_y, 190, 60, "F")
        pdf.set_draw_color(12, 20, 48)
        pdf.rect(10, sp_y, 190, 60, "D")

        pdf.set_text_color(12, 20, 48)
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_xy(12, sp_y + 1.5)
        pdf.cell(0, 5, "Volume / Tape Synthesis")

        synth_lines = []
        ad_grade = ad.get("grade", "—")

        if ad_grade in ("A", "B"):
            synth_lines.append(("A/D Verdict:",
                f"{ad_grade}-rated tape ({ad.get('label', '')}). Volume action favors buyers — institutions are net accumulating over the last 13 weeks."))
        elif ad_grade == "C":
            synth_lines.append(("A/D Verdict:",
                "C-rated tape (neutral). Buying and selling pressure roughly balanced — wait for a regime change before sizing up."))
        elif ad_grade in ("D", "E"):
            synth_lines.append(("A/D Verdict:",
                f"{ad_grade}-rated tape ({ad.get('label', '')}). Volume action favors sellers — institutions distributing. Avoid long entries until tape flips."))

        if monthly and len(monthly) >= 2:
            recent_trend = monthly[-1].get("trend", "")
            prior_trend  = monthly[0].get("trend", "")
            if "Accumulation" in recent_trend and "Distribution" in prior_trend:
                synth_lines.append(("Trajectory:",
                    f"Regime change: {prior_trend} -> {recent_trend}. Money has rotated FROM exit TO entry — highest-conviction window for breakout entries."))
            elif "Accumulation" in recent_trend:
                synth_lines.append(("Trajectory:",
                    f"Recent month is {recent_trend.lower()} — building on prior momentum. Trend is on your side."))
            elif "Distribution" in recent_trend:
                synth_lines.append(("Trajectory:",
                    f"Recent month is {recent_trend.lower()} — sellers in control. Wait for monthly flow to flip green."))

        accum_count = sum(1 for b in bars if b.get("classification") == "ACCUMULATION")
        dist_count  = sum(1 for b in bars if b.get("classification") == "DISTRIBUTION")
        absorb_count = sum(1 for b in bars if b.get("classification") == "ABSORPTION")
        if accum_count + dist_count + absorb_count > 0:
            parts = []
            if accum_count: parts.append(f"{accum_count} accumulation")
            if absorb_count: parts.append(f"{absorb_count} absorption")
            if dist_count: parts.append(f"{dist_count} distribution")
            verdict = ("net bullish — accumulation outpacing distribution" if accum_count + absorb_count > dist_count else
                       "net bearish — distribution outpacing accumulation" if dist_count > accum_count + absorb_count else
                       "balanced")
            synth_lines.append(("Vol Footprint:",
                f"Last 90d: {', '.join(parts)} day(s) at >2x average volume. {verdict.capitalize()}."))

        if silent.get("detected"):
            synth_lines.append(("Silent Build:",
                f"DETECTED — {silent.get('vol_dryup_pct', 0):.0f}% vol dry-up while price held in a {silent.get('price_band_pct', 0):.1f}% band. Classic institutional absorption."))

        # Action score
        score = 0
        if ad_grade == "A": score += 2
        elif ad_grade == "B": score += 1
        elif ad_grade in ("D", "E"): score -= 2
        if monthly and "Accumulation" in monthly[-1].get("trend", ""): score += 1
        if monthly and "Distribution" in monthly[-1].get("trend", ""): score -= 1
        if accum_count + absorb_count > dist_count + 1: score += 1
        if dist_count > accum_count + absorb_count + 1: score -= 1
        if silent.get("detected"): score += 2

        if score >= 3:
            action, action_color = "ACCUMULATE / SIZE UP ON BREAKOUT", (39, 174, 96)
        elif score >= 1:
            action, action_color = "WATCH / SMALL STARTER OK", (90, 180, 110)
        elif score >= -1:
            action, action_color = "WAIT FOR CONFIRMATION", (200, 130, 20)
        else:
            action, action_color = "AVOID / TAPE NOT CONSTRUCTIVE", (192, 57, 43)

        pdf.set_text_color(20, 20, 40)
        line_y = sp_y + 8
        for label, text in synth_lines[:4]:
            pdf.set_font("Helvetica", "B", 7.5)
            pdf.set_xy(12, line_y)
            pdf.cell(28, 4.5, label)
            pdf.set_font("Helvetica", "", 7.5)
            pdf.set_xy(40, line_y)
            pdf.multi_cell(168, 4.5, _safe(text))
            line_y = pdf.get_y() + 1.5

        # Action chip at bottom of panel
        action_y = sp_y + 50
        pdf.set_fill_color(*action_color)
        pdf.rect(12, action_y, 186, 8, "F")
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_xy(12, action_y + 1.5)
        pdf.cell(186, 5, _safe(f"TAPE-BASED ACTION: {action}"), align="C")

        # Footer
        pdf.set_xy(10, 291)
        pdf.set_font("Helvetica", "I", 5.0)
        pdf.set_text_color(150, 150, 150)
        pdf.cell(0, 4,
            "A/D Rating: O'Neill IBD methodology, time-weighted. Volume profile = price/volume action only (no 13F lag). "
            "Action chip is heuristic - combine with fundamental thesis.",
            align="C")

    return bytes(pdf.output())


def _fmt_money(v):
    """Format dollar amount with K/M/B suffix, signed."""
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1_000_000_000: return f"{sign}${v/1e9:.2f}B"
    if v >= 1_000_000:     return f"{sign}${v/1e6:.2f}M"
    if v >= 1_000:         return f"{sign}${v/1e3:.0f}K"
    return f"{sign}${v:.0f}"


# ─── Discord ──────────────────────────────────────────────────────────────────

def send_discord_pdf(pdf_bytes, results, scan_type, webhook_url, label="SMID"):
    now      = datetime.now(ET)
    prefix   = "iwm" if "IWM" in label else "smid"
    filename = f"{prefix}_scanner_{now.strftime('%Y-%m-%d_%H%M')}.pdf"
    buckets  = {"A": 0, "B": 0, "C": 0}
    for r in results:
        g = str(r.get("score", ""))[:1]
        if g in buckets:
            buckets[g] += 1

    content = (
        f"**{label} Breakout Scanner  |  {scan_type}**\n"
        f"{now.strftime('%B %d, %Y  |  %I:%M %p ET')}  "
        f"|  {len(results)} setups  "
        f"|  {buckets['A']}A  {buckets['B']}B  {buckets['C']}C"
    )

    resp = requests.post(
        webhook_url,
        data={"payload_json": json.dumps({"content": content})},
        files={"files[0]": (filename, pdf_bytes, "application/pdf")},
        timeout=60,
    )
    if resp.status_code in (200, 204):
        print(f"  ✅ PDF sent to Discord: {filename}")
    else:
        print(f"  ❌ Discord error {resp.status_code}: {resp.text}")

    # Publish to the GitHub Pages report archive
    try:
        from report_archive import archive
        archive(pdf_bytes, filename)
    except Exception as e:
        print(f"  ⚠️  Archive step skipped: {e}")


# ─── Main scan ────────────────────────────────────────────────────────────────

def run_scan():
    scan_type    = get_scan_type()
    label        = "IWM Russell 2000" if IWM_MODE else "SMID"
    report_label = "IWM RUSSELL 2000 SCANNER" if IWM_MODE else "SMID BREAKOUT SCANNER"
    webhook      = DISCORD_IWM_WEBHOOK if IWM_MODE else DISCORD_WEBHOOK_URL

    print(f"\n{'='*50}\n{label.upper()} BREAKOUT SCANNER -- {scan_type}")
    print(f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S ET')}\n{'='*50}")

    if IWM_MODE:
        print("\n[1/7] Loading IWM universe from CSV...")
        universe = load_iwm_universe(top_n=500)

        print("\n[2/7] Bulk downloading OHLCV + fundamentals for survivors...")
        raw_data, hist_cache, spy_hist = fetch_iwm_data(universe)
        candidates = raw_data  # already pre-filtered inside fetch_iwm_data
        print(f"\n[3/7] Pre-filter done inline — {len(candidates)} candidates")
    else:
        print("\n[1/7] Building universe...")
        universe = get_dynamic_universe()

        print("\n[2/7] Fetching YFinance data (+ SPY RS baseline)...")
        raw_data, hist_cache, spy_hist = fetch_yfinance_data(universe)

        print("\n[3/7] Pre-filtering...")
        candidates = pre_filter(raw_data)

    print("\n[4/7] Enriching candidates (earnings + RS line + sector leaders + insider + macro)...")
    ticker_objs = {t: yf.Ticker(t) for t in [c["ticker"] for c in candidates]}
    candidates = enrich_with_earnings(candidates, ticker_objs)
    candidates = add_rs_line_new_high(candidates, hist_cache, spy_hist)
    sector_leaders = get_sector_leaders(spy_hist)
    if sector_leaders:
        print(f"  Leading sectors: {', '.join(s for s, _ in sector_leaders)}")
    earnings_soon = [c for c in candidates if c.get("earnings_days") is not None and 0 <= c["earnings_days"] <= 7]
    if earnings_soon:
        print(f"  Earnings within 7d: {', '.join(c['ticker'] + ' (' + c['earnings_flag'] + ')' for c in earnings_soon)}")

    macro = fetch_macro_context()
    print(f"  Macro regime: {macro['regime']}  |  VIX {macro['vix']}  |  IWM/SPY trend {macro['iwm_spy_trend']:+.1f}%")

    enrich_candidates_with_insiders(candidates, days_back=60)
    insider_hits = [c for c in candidates if c.get("insider_count", 0) > 0]
    if insider_hits:
        print(f"  Insider buys on {len(insider_hits)}/{len(candidates)}: " +
              ", ".join(f"{c['ticker']}({c['insider_summary']})" for c in insider_hits[:5]))

    print("\n[5/7] Claude analysis (Qullamaggie + earnings + RS line + insider + macro)...")
    results = run_claude_analysis(candidates, scan_type, sector_leaders=sector_leaders, macro=macro)

    grade_order = {"A": 0, "B": 1, "C": 2}
    results.sort(key=lambda r: grade_order.get(str(r.get("score", ""))[:1], 9))
    a_count = sum(1 for r in results if str(r.get("score", "")).startswith("A"))
    print(f"  -> {len(results)} setups  |  {a_count} A-grades")

    print("\n[6/7] Generating PDF...")
    # step [7/7] happens inside send_discord_pdf
    print("\n[7/7] Sending to Discord...")
    if results:
        pdf_bytes = generate_pdf(results, scan_type, hist_cache, report_label=report_label)
        send_discord_pdf(pdf_bytes, results, scan_type, webhook, label=label)
    else:
        regime = macro.get("regime", "Unknown") if isinstance(macro, dict) else "Unknown"
        requests.post(webhook, json={"content": (
            f"**{label} Scanner  |  {scan_type}**  —  "
            f"{datetime.now(ET).strftime('%b %d %Y %I:%M %p ET')}\n"
            f"No breakout setups passed the filter (green + vol surge + above 20MA).\n"
            f"Market regime: **{regime}**. On red / low-volume days the breakout "
            "scanner correctly returns nothing — this is the system working as designed, "
            "not a failure. Check the EOD Setup Builder for coiling pre-breakout bases."
        )}, timeout=15)
        print("  No results — empty scan notification sent (with market context).")

    print("\nDone.")


def generate_error_pdf(ticker, reason):
    """A clean one-page 'ticker not found' report for invalid/N/A symbols."""
    now = datetime.now(ET)
    pdf = FPDF()
    pdf.set_auto_page_break(auto=False)
    pdf.add_page()

    pdf.set_fill_color(12, 20, 48)
    pdf.rect(0, 0, 210, 50, "F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 30)
    pdf.set_xy(10, 13)
    pdf.cell(190, 14, _safe(ticker), align="C")
    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_xy(10, 30)
    pdf.cell(190, 6, _safe(now.strftime("%B %d, %Y  -  %I:%M %p ET")), align="C")
    pdf.set_fill_color(192, 57, 43)
    pdf.rect(0, 50, 210, 3, "F")

    pdf.set_text_color(192, 57, 43)
    pdf.set_font("Helvetica", "B", 22)
    pdf.set_xy(10, 84)
    pdf.cell(190, 12, "TICKER NOT FOUND", align="C")

    pdf.set_text_color(40, 45, 70)
    pdf.set_font("Helvetica", "", 11)
    pdf.set_xy(25, 104)
    pdf.multi_cell(160, 6, _safe(reason), align="C")

    pdf.set_text_color(110, 116, 135)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_xy(25, 134)
    pdf.multi_cell(160, 5, _safe(
        "Check the symbol and try again. The ad-hoc lookup supports US-listed "
        "equities with at least 60 trading days of price history. Ticker symbols "
        "are 1-8 characters - e.g. NVDA, BRK-B, ALAB."), align="C")

    pdf.set_xy(10, 287)
    pdf.set_font("Helvetica", "I", 5.5)
    pdf.set_text_color(150, 150, 150)
    pdf.cell(0, 4, "SMID Scanner ad-hoc lookup. Not financial advice.", align="C")
    return bytes(pdf.output())


def _abort_invalid_ticker(ticker, hist, webhook):
    """Kill the lookup immediately for an N/A ticker — clear error to Discord + site."""
    if hist is None or getattr(hist, "empty", True):
        reason = ("No price data found. The symbol may be invalid, delisted, "
                  "or not a US-listed equity.")
    else:
        reason = (f"Only {len(hist)} trading days of price history available - "
                  "too new or illiquid for a full analysis (60+ days required).")
    print(f"  N/A ticker '{ticker}': {reason}")

    now      = datetime.now(ET)
    filename = f"ticker_{ticker}_{now.strftime('%Y-%m-%d_%H%M')}.pdf"
    err_pdf  = generate_error_pdf(ticker, reason)
    try:
        requests.post(
            webhook,
            data={"payload_json": json.dumps(
                {"content": f"**{ticker} - Ticker Not Found**\n{reason}"})},
            files={"files[0]": (filename, err_pdf, "application/pdf")},
            timeout=60,
        )
    except Exception as e:
        print(f"  Discord post failed: {e}")
    try:
        from report_archive import archive
        archive(err_pdf, filename)
    except Exception as e:
        print(f"  Archive failed: {e}")
    print("\nDone (invalid ticker - lookup aborted before pipeline).")


def run_single_ticker_lookup(ticker):
    """
    On-demand one-pager for a single ticker. Skips universe/pre-filter entirely;
    builds candidate dict directly from yfinance + macro + insider, runs Claude,
    generates a one-page PDF, and posts to DISCORD_TICKER_WEBHOOK_URL.
    """
    ticker = ticker.upper().strip()
    webhook = os.environ.get("DISCORD_TICKER_WEBHOOK_URL", "")
    if not webhook:
        print("  ❌ DISCORD_TICKER_WEBHOOK_URL not set in env")
        return

    print(f"\n{'='*50}\nTICKER LOOKUP: {ticker}\n{'='*50}")

    print("[1/5] Validating ticker + fetching data...")
    # Validate BEFORE the pipeline. An invalid / delisted / data-less symbol is
    # killed here with a clear error report — no macro, insider, institutional,
    # volume, or Claude work is attempted on a ticker that has no data.
    try:
        t    = yf.Ticker(ticker)
        hist = t.history(period="200d", interval="1d")
    except Exception as e:
        print(f"  Data fetch error: {e}")
        hist = None

    if hist is None or hist.empty or len(hist) < 50:
        _abort_invalid_ticker(ticker, hist, webhook)
        return

    try:
        info = t.info
        spy_hist = yf.Ticker("SPY").history(period="200d", interval="1d")

        price    = float(hist["Close"].iloc[-1])
        prev     = float(hist["Close"].iloc[-2])
        change_pct = round((price / prev - 1) * 100, 2) if prev else 0
        ma20     = hist["Close"].iloc[-20:].mean()
        ma50     = hist["Close"].iloc[-50:].mean()
        ma200    = hist["Close"].iloc[-200:].mean() if len(hist) >= 200 else None
        high_52w = info.get("fiftyTwoWeekHigh", 0) or float(hist["High"].max())
        prox_52w = round((price / high_52w) * 100, 1) if high_52w else 0

        v5  = hist["Volume"].iloc[-5:].mean()
        v20 = hist["Volume"].iloc[-21:-1].mean()
        vol_ratio = round(float(hist["Volume"].iloc[-1]) / v20, 2) if v20 > 0 else 0

        atr20   = (hist["High"].iloc[-20:] - hist["Low"].iloc[-20:]).mean()
        range20 = hist["Close"].iloc[-20:].max() - hist["Close"].iloc[-20:].min()
        base_tight = round(range20 / atr20, 2) if atr20 > 0 else 0
        pivot = round(float(hist["High"].iloc[-20:].max()), 2)

        stock_12w = (price / hist["Close"].iloc[-63] - 1) * 100 if len(hist) >= 63 else 0
        spy_12w   = (spy_hist["Close"].iloc[-1] / spy_hist["Close"].iloc[-63] - 1) * 100 if len(spy_hist) >= 63 else 0
        rs_vs_spy = round(stock_12w - spy_12w, 1)

        rs_line_new_high = False
        try:
            aligned = spy_hist["Close"].reindex(hist.index, method="ffill")
            rs_line = (hist["Close"] / aligned).dropna()
            if len(rs_line) >= 20:
                rs_52w = rs_line.rolling(min(252, len(rs_line))).max().iloc[-1]
                rs_line_new_high = bool(rs_line.iloc[-1] >= rs_52w * 0.98)
        except Exception:
            pass

        cand = {
            "ticker":           ticker,
            "company":          info.get("shortName", ticker),
            "price":            round(price, 2),
            "change_pct":       change_pct,
            "vol_ratio":        vol_ratio,
            "mkt_cap_b":        round((info.get("marketCap", 0) or 0) / 1e9, 2),
            "float_m":          round((info.get("floatShares", 0) or 0) / 1e6, 1),
            "high_52w":         round(high_52w, 2),
            "prox_52w":         prox_52w,
            "pivot":            pivot,
            "base_tight":       base_tight,
            "rs_vs_spy":        rs_vs_spy,
            "rs_line_new_high": rs_line_new_high,
            "above20ma":        bool(price > ma20),
            "above50ma":        bool(price > ma50),
            "above200ma":       bool(ma200 and price > ma200),
            "sector":           info.get("sector", ""),
            "industry":         info.get("industry", ""),
            "trailing_pe":      round(info.get("trailingPE", 0) or 0, 1),
            "forward_pe":       round(info.get("forwardPE", 0) or 0, 1),
            "ps_ratio":         round(info.get("priceToSalesTrailing12Months", 0) or 0, 1),
            "rev_growth":       f"{round((info.get('revenueGrowth', 0) or 0) * 100, 1)}%",
            "short_pct":        f"{round((info.get('shortPercentOfFloat', 0) or 0) * 100, 1)}%",
            "short_ratio":      round(info.get("shortRatio", 0) or 0, 1),  # days to cover
            "inst_own":         f"{round((info.get('institutionPercentHeld', 0) or 0) * 100, 1)}%",
            "target_price":     round(info.get("targetMeanPrice", 0) or 0, 2),
            "avg_vol_m":        round((info.get("averageVolume", 0) or 0) / 1e6, 2),
        }

        # Earnings enrichment
        candidates = enrich_with_earnings([cand], {ticker: t})

    except Exception as e:
        print(f"  ❌ Data fetch failed: {e}")
        requests.post(webhook, json={"content": f"**{ticker}** — data fetch failed: {e}"}, timeout=15)
        return

    print("[2/5] Macro context + insider activity (90d signal + 12mo transaction log)...")
    macro = fetch_macro_context()
    enrich_candidates_with_insiders(candidates, days_back=90)
    if candidates[0].get("insider_count", 0) > 0:
        print(f"  90d signal: {candidates[0].get('insider_summary', '')}")
    print(f"  Fetching 12-month transaction log from SEC EDGAR...")
    insider_log = fetch_insider_transactions_detail(ticker, days_back=365, max_filings=60)
    print(f"  Found {len(insider_log)} insider transactions in last 12 months")

    # Surface 12-month context to Claude even if 90-day window is empty (avoids "zero insider buying" hallucination)
    buys_12m = [t for t in insider_log if t["code"] == "P"]
    sales_12m = [t for t in insider_log if t["code"] == "S"]
    most_recent_buy = buys_12m[0] if buys_12m else None
    candidates[0]["insider_12m_buys_count"] = len(buys_12m)
    candidates[0]["insider_12m_buys_value"] = int(sum(t["value"] for t in buys_12m))
    candidates[0]["insider_12m_sales_count"] = len(sales_12m)
    candidates[0]["insider_12m_sales_value"] = int(sum(t["value"] for t in sales_12m))
    if most_recent_buy:
        candidates[0]["insider_most_recent_buy"] = (
            f"{most_recent_buy['date']}: {most_recent_buy['owner']} ({most_recent_buy['title']}) "
            f"bought {int(most_recent_buy['shares']):,} sh @ ${most_recent_buy['price']:.2f} = "
            f"${most_recent_buy['value']/1000:.0f}K"
        )
    else:
        candidates[0]["insider_most_recent_buy"] = ""

    # Institutional ownership intelligence (replaces broken yfinance institutionPercentHeld)
    print(f"  Fetching institutional holders + 13D/13G filings...")
    inst_data = fetch_institutional_data(ticker)
    filings_13 = fetch_13d_13g_filings(ticker, days_back=365)
    smart_money = compute_smart_money_score(
        {"count": candidates[0].get("insider_count", 0), "cluster_score": candidates[0].get("insider_cluster", 0),
         "summary": candidates[0].get("insider_summary", "")},
        inst_data,
        filings_13,
    )

    # Override the (often broken) yfinance inst_own with the real major_holders figure
    real_inst_pct = inst_data.get("major_holders", {}).get("institutionsPercentHeld")
    if real_inst_pct and real_inst_pct > 0:
        candidates[0]["inst_own"] = f"{real_inst_pct * 100:.1f}%"
        candidates[0]["institutionsCount"] = int(inst_data["major_holders"].get("institutionsCount", 0) or 0)

    print(f"  Institutional: {real_inst_pct*100 if real_inst_pct else 0:.0f}% held by {candidates[0].get('institutionsCount', 0)} institutions")
    print(f"  Smart money signal: {smart_money['label']}  (score={smart_money['score']})")
    if filings_13:
        print(f"  13D/13G filings (last 12mo): {len(filings_13)}")

    print("[3/5] Claude analysis...")
    results = run_claude_analysis(
        candidates,
        scan_type="ON-DEMAND LOOKUP",
        sector_leaders=None,
        macro=macro,
        force_full_descriptives=True,  # always populate all fields for ad-hoc lookups
    )

    if not results:
        requests.post(webhook, json={"content": f"**{ticker}** — Claude returned no analysis."}, timeout=15)
        return

    # Merge yfinance fields back so PDF has full data
    src = candidates[0]
    for r in results:
        for key, val in src.items():
            if key not in r or r.get(key) in (None, 0, 0.0, "", "-"):
                r[key] = val
        # Force-preserve the original ticker — Claude occasionally hallucinates the symbol
        # (e.g., IONQ → IONO). Always trust the input ticker over Claude's output.
        r["ticker"] = ticker
        if not r.get("company") or r.get("company") == ticker:
            r["company"] = src.get("company", ticker)

    print("  Computing price/volume intelligence (A/D rating, monthly flow, vol profile)...")
    ad_rating = compute_ad_rating(hist)
    sig_bars  = find_significant_volume_bars(hist, lookback=90, threshold=2.0)
    monthly_flow = compute_monthly_flow(hist)
    silent_build = detect_silent_build(hist)
    print(f"  A/D Rating: {ad_rating['grade']} ({ad_rating['label']}, score {ad_rating['score']})")
    if sig_bars:
        print(f"  Significant volume bars: {len(sig_bars)}  "
              f"(latest: {sig_bars[0]['date']} {sig_bars[0]['rel_vol']}x {sig_bars[0]['classification']})")

    # Pass A/D rating context to Claude for the analysis text
    candidates[0]["ad_rating_grade"] = ad_rating["grade"]
    candidates[0]["ad_rating_label"] = ad_rating["label"]
    candidates[0]["monthly_flow_trend"] = " -> ".join(m["trend"] for m in monthly_flow) if monthly_flow else ""
    candidates[0]["silent_build_detected"] = silent_build.get("detected", False)
    candidates[0]["sig_volume_bars_count"] = len(sig_bars)
    candidates[0]["accumulation_bars_count"] = sum(1 for b in sig_bars if b["classification"] == "ACCUMULATION")
    candidates[0]["distribution_bars_count"] = sum(1 for b in sig_bars if b["classification"] == "DISTRIBUTION")
    candidates[0]["absorption_bars_count"] = sum(1 for b in sig_bars if b["classification"] == "ABSORPTION")

    print("[4/5] Generating one-pager PDF + insider + institutional + vol intelligence pages...")
    pdf_bytes = generate_pdf(
        results,
        scan_type="ON-DEMAND LOOKUP",
        hist_cache={ticker: hist},
        insider_transactions=insider_log,
        institutional_data=inst_data,
        filings_13=filings_13,
        smart_money=smart_money,
        volume_intelligence={
            "ad_rating":     ad_rating,
            "sig_bars":      sig_bars,
            "monthly_flow":  monthly_flow,
            "silent_build":  silent_build,
        },
    )

    print("[5/5] Sending to Discord...")
    now      = datetime.now(ET)
    filename = f"ticker_{ticker}_{now.strftime('%Y-%m-%d_%H%M')}.pdf"
    grade    = str(results[0].get("score", ""))[:1] or "?"
    content  = f"**{ticker} — On-Demand One-Pager**\n{now.strftime('%B %d, %Y · %I:%M %p ET')}  |  Grade: **{grade}**  |  Macro: {macro.get('regime', 'Unknown')}"
    resp = requests.post(
        webhook,
        data={"payload_json": json.dumps({"content": content})},
        files={"files[0]": (filename, pdf_bytes, "application/pdf")},
        timeout=60,
    )
    if resp.status_code in (200, 204):
        print(f"  ✅ Sent: {filename}")
    else:
        print(f"  ❌ Discord error {resp.status_code}: {resp.text[:200]}")

    # Publish to the GitHub Pages report archive
    try:
        from report_archive import archive
        archive(pdf_bytes, filename)
    except Exception as e:
        print(f"  ⚠️  Archive step skipped: {e}")
    print("\nDone.")


if __name__ == "__main__":
    if "--ticker" in sys.argv:
        idx = sys.argv.index("--ticker")
        if idx + 1 >= len(sys.argv):
            print("Usage: python scanner.py --ticker SYMBOL")
            sys.exit(1)
        run_single_ticker_lookup(sys.argv[idx + 1])
    else:
        run_scan()
