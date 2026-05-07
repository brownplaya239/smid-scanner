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

load_dotenv(override=True)

IWM_MODE = "--iwm" in sys.argv

ANTHROPIC_API_KEY     = os.environ.get("ANTHROPIC_API_KEY", "")
DISCORD_WEBHOOK_URL   = os.environ.get("DISCORD_WEBHOOK_URL", "")
DISCORD_IWM_WEBHOOK   = os.environ.get("DISCORD_IWM_WEBHOOK_URL", "")

if not ANTHROPIC_API_KEY:
    raise EnvironmentError("Missing: ANTHROPIC_API_KEY")
if IWM_MODE and not DISCORD_IWM_WEBHOOK:
    raise EnvironmentError("Missing: DISCORD_IWM_WEBHOOK_URL")
if not IWM_MODE and not DISCORD_WEBHOOK_URL:
    raise EnvironmentError("Missing: DISCORD_WEBHOOK_URL")

IWM_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "IWM_holdings.csv")

ET = pytz.timezone("America/New_York")


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

    print(f"  Bulk downloading {len(tickers)} tickers...")
    try:
        bulk = yf.download(
            tickers, period="200d", interval="1d",
            group_by="ticker", auto_adjust=True, threads=True, progress=False,
        )
    except Exception as e:
        print(f"  Bulk download failed: {e}")
        return [], {}, spy_hist

    tech_pass, hist_cache = [], {}
    for ticker in tickers:
        try:
            hist = bulk[ticker].dropna(how="all") if len(tickers) > 1 else bulk.dropna(how="all")
            if hist.empty or len(hist) < 20:
                continue
            price      = float(hist["Close"].iloc[-1])
            prev       = float(hist["Close"].iloc[-2])
            change_pct = (price - prev) / prev * 100
            avg_vol    = float(hist["Volume"].iloc[-21:-1].mean())
            vol_ratio  = float(hist["Volume"].iloc[-1]) / avg_vol if avg_vol > 0 else 1.0
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
            today_vol   = hist["Volume"].iloc[-1]
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

def run_claude_analysis(candidates, scan_type, sector_leaders=None):
    if not candidates:
        return []
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    today = datetime.now(ET).strftime("%B %d, %Y")

    sector_ctx = ""
    if sector_leaders:
        leaders_str = ", ".join(f"{s} ({r:+.1f}% vs SPY)" for s, r in sector_leaders)
        sector_ctx = f"\nLeading sectors this week: {leaders_str}\nStocks in these sectors score +0.5 grade when setup quality is equal.\n"

    prompt = f"""You are a Wharton-educated hedge fund analyst specializing in SMID-cap momentum with deep expertise in identifying live breakout inflection points. Today is {today}. Scan type: {scan_type}.
{sector_ctx}
These stocks passed a strict pre-filter: mkt cap <$10B, float <150M, above 20MA, volume >1.5x avg, green on day. A breakout is happening NOW — your task is to determine which are institutional-quality setups with follow-through potential vs. noise.

Candidates:
{json.dumps(candidates, indent=2)}

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
   - Known growth fund holders, ETF exposure, float rotation dynamics?
   - Short squeeze mechanics if short interest is elevated?

5. EARNINGS CONTEXT (if applicable)
   - Next earnings exact date (BMO/AMC), EPS/revenue consensus, beat/miss history (last 4 quarters)
   - Is this a serial beater that re-rates higher on each beat?

6. RISK — What kills the trade?
   - Specific bear case: binary event risk, dilution at $X, single customer, competitive threat, technical failure level?

GRADING (Qullamaggie methodology):
- "A - Breakout": 4+ of: vol >2x, prox_52w >85, rs_line_new_high, rs_vs_spy >+10, base_tight <2.0, durable catalyst. Pure technical breakouts with rs_line_new_high + vol surge ARE valid A setups even without same-day news.
- "B - Strong": vol >1.5x, prox_52w >75, rs_vs_spy >0, above 20MA+50MA
- "C - Watch": elevated vol, above 20MA, RS positive or turning

IMPORTANT: Include ALL candidates with genuine momentum. Do NOT exclude for lack of news catalyst. Return at least the top 10 by technical quality.

Return ONLY a raw JSON array. No markdown. No preamble.
Each object must include ALL fields:
  ticker, company, price, changePercent, marketCapB, floatM, rsVsSpy, prox52w, baseTight,
  rsLineNewHigh, earningsFlag,
  theme (2-4 words),
  industry (precise label),
  catalyst (1-2 sentences: what is specifically driving today's move),
  businessDescription (2 sentences: what they do + competitive position),
  factorExposure (1-2 sentences: secular themes + sentiment direction),
  institutionalAngle (1-2 sentences: volume character + known holders + ETF exposure),
  earningsContext (consensus EPS/rev + beat history, or "" if not relevant),
  keyRisk (1 sentence: the specific bear case),
  signal (exact technical condition: "Close above $X.XX on vol >Y% of 20d avg"),
  volumeVsAvg (e.g. "2.4x"),
  rs,
  score (MUST be exactly one of: "A", "B", or "C" — the letter grade only, no numbers),
  reasoning (2 sentences: why this is or isn't a high-conviction follow-through)."""

    # Trim to top 20 before sending — richer output per ticker × 30 overflows 6K tokens
    candidates = candidates[:20]
    print("  ✅ Sending to Claude...")
    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=16000,
        messages=[{"role": "user", "content": prompt}]
    )
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


def generate_pdf(results, scan_type, hist_cache, report_label="SMID BREAKOUT SCANNER"):
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

    # Cover footer
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
        badge_rgb    = (34, 153, 84) if grade_letter == "A" else (41, 128, 185)
        badge_label  = "A  BREAKOUT" if grade_letter == "A" else "B  STRONG SETUP"
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

        stats = [
            ("Price",       f"${price:.2f}"),
            ("Day Change",  chg_s),
            ("Mkt Cap",     f"${cap_b:.2f}B"),
            ("Float",       f"{fl_m:.0f}M sh"),
            ("RS vs SPY",   rs_s),
            ("52W Hi Prox", f"{prox:.0f}%"),
            ("Base Tight",  str(bt)),
            ("Vol vs Avg",  str(vol_avg)),
            ("RS Line Hi",  "YES" if s.get("rsLineNewHigh", s.get("rs_line_new_high")) else "no"),
            ("Above 50MA",  "YES" if s.get("above50ma", s.get("above_50ma")) else "-"),
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

        if ticker in hist_cache:
            chart_buf = generate_chart(ticker, hist_cache[ticker])
            if chart_buf:
                avail_h = 291 - chart_y
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

    return bytes(pdf.output())


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

    print("\n[4/7] Enriching candidates (earnings + RS line + sector leaders)...")
    ticker_objs = {t: yf.Ticker(t) for t in [c["ticker"] for c in candidates]}
    candidates = enrich_with_earnings(candidates, ticker_objs)
    candidates = add_rs_line_new_high(candidates, hist_cache, spy_hist)
    sector_leaders = get_sector_leaders(spy_hist)
    if sector_leaders:
        print(f"  Leading sectors: {', '.join(s for s, _ in sector_leaders)}")
    earnings_soon = [c for c in candidates if c.get("earnings_days") is not None and 0 <= c["earnings_days"] <= 7]
    if earnings_soon:
        print(f"  Earnings within 7d: {', '.join(c['ticker'] + ' (' + c['earnings_flag'] + ')' for c in earnings_soon)}")

    print("\n[5/7] Claude analysis (Qullamaggie + earnings + RS line)...")
    results = run_claude_analysis(candidates, scan_type, sector_leaders=sector_leaders)

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
        requests.post(webhook, json={"content": (
            f"**{label} Scanner  |  {scan_type}**  —  "
            f"{datetime.now(ET).strftime('%b %d %Y %I:%M %p ET')}  |  "
            "No setups passed pre-filter today."
        )}, timeout=15)
        print("  No results — empty scan notification sent.")

    print("\nDone.")


if __name__ == "__main__":
    run_scan()
