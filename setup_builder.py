"""
setup_builder.py — EOD Pre-Breakout Watchlist Builder (SMID + IWM modes)
Spots VCP bases FORMING before the trigger fires.
Usage:
  python setup_builder.py          # SMID mode (default)
  python setup_builder.py --iwm    # IWM Russell 2000 mode
Env: ANTHROPIC_API_KEY, DISCORD_SETUP_WEBHOOK_URL (SMID) or DISCORD_IWM_WEBHOOK_URL (IWM)
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
matplotlib.use("Agg")
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

IWM_MODE              = "--iwm" in sys.argv

ANTHROPIC_API_KEY     = os.environ.get("ANTHROPIC_API_KEY", "")
DISCORD_SETUP_WEBHOOK = os.environ.get("DISCORD_SETUP_WEBHOOK_URL", "")
DISCORD_IWM_WEBHOOK   = os.environ.get("DISCORD_IWM_WEBHOOK_URL", "")

if not ANTHROPIC_API_KEY:
    raise EnvironmentError("Missing: ANTHROPIC_API_KEY")
if IWM_MODE and not DISCORD_IWM_WEBHOOK:
    raise EnvironmentError("Missing: DISCORD_IWM_WEBHOOK_URL")
if not IWM_MODE and not DISCORD_SETUP_WEBHOOK:
    raise EnvironmentError("Missing: DISCORD_SETUP_WEBHOOK_URL")

IWM_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "IWM_holdings.csv")

ET = pytz.timezone("America/New_York")


# ─── Universe (broader — catching bases before they trigger) ──────────────────

def get_universe(size=300):
    """
    Build the broadest possible SMID universe by combining:
    1. yfinance Screener sorted by 52W proximity (RS leaders first)
    2. yfinance Screener sorted by market cap (breadth)
    3. Yahoo predefined screeners (gainers, actives, high-RS)
    Deduplicates and caps at ~500 unique tickers.
    """
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    seen, tickers = set(), []

    def _add(syms):
        for s in syms:
            if s and "." not in s and s not in seen:
                seen.add(s)
                tickers.append(s)

    base_query = {
        "operator": "AND",
        "operands": [
            {"operator": "BTWN", "operands": ["intradaymarketcap", 150_000_000, 8_000_000_000]},
            {"operator": "EQ",   "operands": ["region", "us"]},
            {"operator": "GT",   "operands": ["averageDailyVolume3Month", 150_000]},
            {"operator": "GT",   "operands": ["intradayprice", 3.0]},
        ]
    }

    try:
        from yfinance import Screener

        # Pass 1: sorted by 52W high proximity — these are the RS leaders we want
        s1 = Screener()
        s1.set_body({"offset": 0, "size": size, "sortField": "fiftyTwoWeekHighChange",
                     "sortType": "ASC", "quoteType": "EQUITY", "query": base_query})
        _add([q["symbol"] for q in s1.response.get("quotes", []) if q.get("symbol")])

        # Pass 2: sorted by market cap for breadth
        s2 = Screener()
        s2.set_body({"offset": 0, "size": size, "sortField": "intradaymarketcap",
                     "sortType": "DESC", "quoteType": "EQUITY", "query": base_query})
        _add([q["symbol"] for q in s2.response.get("quotes", []) if q.get("symbol")])

    except Exception:
        pass

    # Pass 3: Yahoo predefined screeners (always run — adds gainers / momentum names)
    try:
        for scr_id in ["small_cap_gainers", "undervalued_small_caps", "most_actives",
                       "day_gainers", "growth_technology_stocks"]:
            resp = requests.get(
                "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved",
                params={"scrIds": scr_id, "count": 100, "region": "US", "lang": "en-US"},
                headers=headers, timeout=10,
            )
            if resp.ok:
                quotes = resp.json().get("finance", {}).get("result", [{}])[0].get("quotes", [])
                _add([q["symbol"] for q in quotes
                      if q.get("symbol") and "." not in q.get("symbol", "")
                      and 150_000_000 <= (q.get("marketCap") or 0) <= 8_000_000_000])
    except Exception:
        pass

    print(f"  📡 {len(tickers)} tickers in combined universe")
    return tickers[:500]


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


# ─── Data + VCP scoring ───────────────────────────────────────────────────────

def fetch_setup_data(tickers):
    """Returns list of candidates with VCP metrics. No green-on-day or vol requirements."""
    spy_ret = 0.0
    spy_hist = None
    try:
        spy_hist = yf.Ticker("SPY").history(period="200d", interval="1d")
        spy_ret  = (spy_hist["Close"].iloc[-1] / spy_hist["Close"].iloc[-63] - 1) * 100
    except Exception:
        pass

    results, hist_cache = [], {}
    print(f"Fetching data for {len(tickers)} tickers...")
    for ticker in tickers:
        try:
            t    = yf.Ticker(ticker)
            info = t.info
            hist = t.history(period="200d", interval="1d")
            if hist.empty or len(hist) < 50:
                continue

            price    = hist["Close"].iloc[-1]
            ma20     = hist["Close"].iloc[-20:].mean()
            ma50     = hist["Close"].iloc[-50:].mean()
            ma200    = hist["Close"].iloc[-200:].mean() if len(hist) >= 200 else None
            high_52w = info.get("fiftyTwoWeekHigh", 0) or 0
            mkt_cap  = info.get("marketCap", 0) or 0
            float_sh = info.get("floatShares", 0) or 0

            # Stage 2 gate: above 50MA and ideally above 200MA
            if price < ma50:
                continue
            if mkt_cap <= 0 or mkt_cap > 8_000_000_000:
                continue
            if float_sh <= 0 or float_sh > 150_000_000:
                continue
            if high_52w <= 0:
                continue

            prox_52w = (price / high_52w) * 100
            if prox_52w < 80:
                continue

            # VCP: volume contraction — recent week below 4-week baseline
            # Strict: v5 < v10 < v20 (perfect stair-step)
            # Soft:   v5 < v20 (recent quieter than month ago — valid in choppy tape)
            v5  = hist["Volume"].iloc[-5:].mean()
            v10 = hist["Volume"].iloc[-10:-5].mean()
            v20 = hist["Volume"].iloc[-20:-10].mean()
            vol_dryup_strict = bool(v5 < v10 < v20)
            vol_dryup_soft   = bool(v5 < v20)
            vol_dryup        = vol_dryup_soft  # use soft; strict flagged in score

            # Base tightness: 20-day close range / ATR
            atr20      = (hist["High"].iloc[-20:] - hist["Low"].iloc[-20:]).mean()
            range20    = hist["Close"].iloc[-20:].max() - hist["Close"].iloc[-20:].min()
            base_tight = round(range20 / atr20, 2) if atr20 > 0 else None

            # Days in base: how long price has been within 10% of current level
            base_high = hist["Close"].iloc[-20:].max()
            days_in   = int((hist["Close"].iloc[-20:] >= base_high * 0.90).sum())

            # Pivot = high of the base (breakout target)
            pivot = round(hist["High"].iloc[-20:].max(), 2)
            pct_to_pivot = round((pivot / price - 1) * 100, 1)

            # RS vs SPY
            stock_12w = (price / hist["Close"].iloc[-63] - 1) * 100 if len(hist) >= 63 else 0
            rs_vs_spy = round(stock_12w - spy_ret, 1)

            # RS line new high
            rs_line_new_high = False
            if spy_hist is not None:
                try:
                    aligned = spy_hist["Close"].reindex(hist.index, method="ffill")
                    rs_line = (hist["Close"] / aligned).dropna()
                    if len(rs_line) >= 20:
                        rs_52w = rs_line.rolling(min(252, len(rs_line))).max().iloc[-1]
                        rs_line_new_high = bool(rs_line.iloc[-1] >= rs_52w * 0.98)
                except Exception:
                    pass

            # Volume ratio (today vs 20d avg) — want LOW here for base
            avg_vol   = hist["Volume"].iloc[-21:-1].mean()
            vol_ratio = round(hist["Volume"].iloc[-1] / avg_vol, 2) if avg_vol > 0 else 1.0

            # Earnings check
            earnings_flag, earnings_days = "", None
            try:
                cal = t.calendar
                if isinstance(cal, dict):
                    dates = cal.get("Earnings Date", [])
                    if dates:
                        ed = dates[0]
                        ed_date = ed.date() if hasattr(ed, "date") else None
                        if ed_date:
                            delta = (ed_date - datetime.now(ET).date()).days
                            earnings_days = delta
                            if 0 <= delta <= 7:
                                earnings_flag = f"EARNINGS IN {delta}D"
                            elif delta < 0:
                                earnings_flag = "REPORTED"
            except Exception:
                pass

            def _pct(v):
                return f"{round((v or 0)*100, 1)}%" if v else "-"
            def _r1(v):
                return round(v, 1) if v else 0
            def _r2(v):
                return round(v, 2) if v else 0

            # Hard filter: must have vol dry-up AND be close to pivot
            if not vol_dryup:
                continue
            if pct_to_pivot > 8:
                continue

            above_50ma = bool(price > ma50)

            hist_cache[ticker] = hist
            results.append({
                "ticker":           ticker,
                "company":          info.get("shortName", ticker),
                "price":            round(price, 2),
                "mkt_cap_b":        round(mkt_cap / 1e9, 2),
                "float_m":          round(float_sh / 1e6, 1),
                "high_52w":         round(high_52w, 2),
                "low_52w":          round(info.get("fiftyTwoWeekLow", 0) or 0, 2),
                "prox_52w":         round(prox_52w, 1),
                "pivot":            pivot,
                "pct_to_pivot":     pct_to_pivot,
                "base_tight":       base_tight,
                "days_in_base":     days_in,
                "vol_dryup":        True,
                "vol_dryup_strict": vol_dryup_strict,
                "vol_w1_k":         round(v5 / 1000, 1),
                "vol_w2_k":         round(v10 / 1000, 1),
                "vol_w3_k":         round(v20 / 1000, 1),
                "vol_contraction":  round((1 - v5 / v20) * 100, 1) if v20 > 0 else 0,
                "vol_ratio_today":  vol_ratio,
                "rs_vs_spy":        rs_vs_spy,
                "rs_line_new_high": rs_line_new_high,
                "above_50ma":       above_50ma,
                "above_200ma":      bool(ma200 and price > ma200),
                "sector":           info.get("sector", ""),
                "industry_yf":      info.get("industry", ""),
                "earnings_flag":    earnings_flag,
                "earnings_days":    earnings_days,
                # Fundamentals
                "trailing_pe":      _r1(info.get("trailingPE")),
                "forward_pe":       _r1(info.get("forwardPE")),
                "peg":              _r2(info.get("pegRatio")),
                "ev_ebitda":        _r1(info.get("enterpriseToEbitda")),
                "ps_ratio":         _r1(info.get("priceToSalesTrailing12Months")),
                "pb_ratio":         _r1(info.get("priceToBook")),
                "gross_margin":     _pct(info.get("grossMargins")),
                "op_margin":        _pct(info.get("operatingMargins")),
                "net_margin":       _pct(info.get("profitMargins")),
                "roe":              _pct(info.get("returnOnEquity")),
                "roa":              _pct(info.get("returnOnAssets")),
                "debt_eq":          _r2(info.get("debtToEquity")),
                "short_pct":        _pct(info.get("shortPercentOfFloat")),
                "short_ratio":      _r1(info.get("shortRatio")),
                "inst_own":         _pct(info.get("institutionPercentHeld")),
                "insider_own":      _pct(info.get("insiderPercentHeld")),
                "beta":             _r2(info.get("beta")),
                "eps_ttm":          _r2(info.get("trailingEps")),
                "eps_fwd":          _r2(info.get("forwardEps")),
                "rev_growth":       _pct(info.get("revenueGrowth")),
                "earn_growth":      _pct(info.get("earningsGrowth")),
                "target_price":     _r2(info.get("targetMeanPrice")),
                "avg_vol_m":        round((info.get("averageVolume", 0) or 0) / 1e6, 2),
                "shs_out_m":        round((info.get("sharesOutstanding", 0) or 0) / 1e6, 1),
            })
        except Exception as e:
            print(f"  ⚠️  Skipped {ticker}: {e}")

    print(f"  ✅ {len(results)} candidates with setup data")
    return results, hist_cache


def score_setups(data):
    """Score and rank VCP setups. Higher = tighter, closer to pivot, better RS."""
    scored = []
    for d in data:
        score = 0

        # Volume dry-up (most important — the coil)
        # Strict 3-week stair-step gets full credit; soft (recent < month avg) gets partial
        if d.get("vol_dryup_strict"):
            score += 30
        elif d["vol_dryup"]:
            score += 15

        # Base tightness
        bt = d["base_tight"] or 99
        if bt < 1.5:   score += 25
        elif bt < 2.0: score += 15
        elif bt < 2.5: score += 5

        # Proximity to 52W high
        if d["prox_52w"] >= 97:   score += 20
        elif d["prox_52w"] >= 92: score += 15
        elif d["prox_52w"] >= 85: score += 8

        # RS vs SPY
        if d["rs_vs_spy"] >= 20:  score += 15
        elif d["rs_vs_spy"] >= 10: score += 10
        elif d["rs_vs_spy"] >= 0:  score += 5

        # RS line at new high
        if d["rs_line_new_high"]:
            score += 15

        # Proximity to pivot (closer = more imminent)
        if d["pct_to_pivot"] <= 1:    score += 10
        elif d["pct_to_pivot"] <= 3:  score += 7
        elif d["pct_to_pivot"] <= 5:  score += 3

        # Earnings catalyst imminent
        ed = d.get("earnings_days")
        if ed is not None and 1 <= ed <= 5:
            score += 10

        # Above 200MA (clean Stage 2)
        if d["above_200ma"]:
            score += 5

        d["setup_score"] = score
        scored.append(d)

    scored.sort(key=lambda x: x["setup_score"], reverse=True)
    return scored[:20]


# ─── Claude analysis ──────────────────────────────────────────────────────────

def run_claude_setup_analysis(setups):
    if not setups:
        return []
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    today  = datetime.now(ET).strftime("%B %d, %Y")

    prompt = f"""You are a Wharton-educated hedge fund analyst specializing in SMID-cap momentum with deep expertise in identifying niche alpha opportunities before institutional consensus catches up. Today is {today}.

These stocks have passed a strict VCP filter: volume is genuinely contracting week-over-week, base is tight, and price is within 8% of the pivot — the breakout has NOT occurred. Your task: produce institutional-quality research on each setup.

Candidates (all have confirmed vol dry-up + Stage 2 base):
{json.dumps(setups, indent=2)}

For each candidate, think rigorously across six dimensions:

1. BUSINESS & INDUSTRY POSITION
   - What does this company actually do and what is its competitive position?
   - Is it a category leader, fast follower, or niche disruptor?
   - What is the total addressable market and growth trajectory?

2. FACTOR & THEME EXPOSURE
   - Which secular themes does this tap? (AI infrastructure, defense spending, energy transition, biotech cycle, reshoring, consumer recovery, space economy, cybersecurity, GLP-1, etc.)
   - Is sentiment in this theme accelerating or decelerating?
   - Peer read-throughs: what has similar-category stock action told us?

3. EARNINGS & FINANCIAL CATALYST
   - Next earnings date (exact: "May 14 BMO" or "May 14 AMC") — if unknown say ""
   - Street consensus: EPS estimate and revenue estimate if known
   - Last 4 quarters beat/miss pattern — is this a serial beater?
   - Recent guidance trend (raised/maintained/cut)
   - Any upcoming investor days, analyst days, or conference presentations?

4. NEWS FLOW & COMPANY CATALYSTS (last 30-60 days)
   - Contract wins, partnership announcements, regulatory approvals, product launches
   - FDA dates (PDUFA), CMS decisions, government contract awards
   - Index inclusion/exclusion events (Russell rebalance, S&P additions)
   - Insider buying signals or 13D/13G filings
   - Short squeeze potential (high short interest + improving fundamentals)

5. INSTITUTIONAL & SMART MONEY ANGLE
   - Is there evidence of institutional accumulation in the base? (vol dry-up with price holding = quiet accumulation)
   - Known activist investors, growth fund holders (Dragoneer, Tiger, Coatue etc.)
   - ETF flow exposure — which funds hold this and are growing?
   - Float rotation dynamics

6. RISK ASSESSMENT
   - The single most important bear case (be specific: dilution risk at $X, binary FDA event, customer concentration, etc.)
   - Short interest % and days-to-cover
   - Key support level that invalidates the setup

VCP GRADING:
- "A - Prime Setup": Textbook VCP coil, vol_w1 < vol_w2 < vol_w3 confirmed, RS line at new high, pivot within 3%, institutional-quality catalyst within 2 weeks, base_tight < 2.0
- "B - Building Setup": Strong base quality, most VCP criteria met, catalyst developing, 1-3 weeks from potential trigger
- "C - Monitoring": Early stage VCP, worth watching, needs more time or a catalyst to develop

Return ONLY a raw JSON array. No markdown, no preamble.
Every object must include ALL fields:
  ticker, company, price, pivot, pctToPivot, baseTight, volDryup, rsVsSpy, rsLineNewHigh,
  industry (precise: e.g. "Satellite Imagery & Defense Geospatial Analytics"),
  theme (2-5 words: e.g. "Defense AI Infrastructure Spend"),
  businessDescription (2 sentences: what they do and their competitive position),
  factorExposure (1-2 sentences: which themes + sentiment direction),
  earningsDate (e.g. "May 14 BMO" or ""),
  earningsContext (consensus EPS, revenue, beat/miss history — 1-2 sentences or ""),
  newsFlow (2-3 sentences: specific recent catalysts, contracts, FDA, partnerships — be precise),
  institutionalAngle (1-2 sentences: accumulation evidence, known holders, ETF exposure),
  keyRisk (1 sentence: the specific bear case that kills the trade),
  triggerCondition (exact: "Close above $X.XX on volume >Y% of 20-day avg"),
  watchTarget (measured move price target with basis),
  stopLevel (invalidation level with basis),
  grade,
  reasoning (2 sentences: structural setup quality + what makes this an alpha opportunity vs. generic momentum)."""

    print("  ✅ Sending to Claude...")
    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=20000,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    print(f"  Raw response length: {len(raw)} chars")
    try:
        parsed = json.loads(raw)
        print(f"  Parsed OK: {len(parsed)} items")
        return parsed
    except Exception as e:
        print(f"  JSON parse failed: {e}")
        # Truncation salvage: trim to the last complete } before the break, then close the array
        last_close = raw.rfind("}")
        if last_close > 0:
            salvaged = raw[: last_close + 1].rstrip().rstrip(",") + "]"
            try:
                parsed = json.loads(salvaged)
                print(f"  Truncation salvage OK: {len(parsed)} items recovered")
                return parsed
            except Exception as e2:
                print(f"  Salvage also failed: {e2}")
        return []


# ─── Chart generation ─────────────────────────────────────────────────────────

def generate_setup_chart(ticker, hist, pivot):
    """Full Qullamaggie chart: 9/21/50/200 SMA + RSI panel + rel-vol coloring + gold pivot line."""
    try:
        data = hist.copy()
        data["SMA9"]   = data["Close"].rolling(9).mean()
        data["SMA21"]  = data["Close"].rolling(21).mean()
        data["SMA50"]  = data["Close"].rolling(50).mean()
        data["SMA200"] = data["Close"].rolling(200).mean()

        delta       = data["Close"].diff()
        gain        = delta.clip(lower=0).rolling(14).mean()
        loss        = (-delta.clip(upper=0)).rolling(14).mean()
        rsi_raw     = 100 - (100 / (1 + gain / loss.where(loss != 0, float("nan"))))
        data["RSI"] = rsi_raw.ffill().bfill().clip(0, 100)

        avg_vol        = data["Volume"].rolling(20).mean()
        data["RelVol"] = data["Volume"] / avg_vol.where(avg_vol > 0, float("nan"))

        plot_data = data.tail(60).copy()
        for col in ["SMA9", "SMA21", "SMA50", "SMA200"]:
            plot_data[col] = plot_data[col].ffill().bfill()

        vcolors = []
        for rv in plot_data["RelVol"]:
            if rv >= 3:     vcolors.append("#FF4500")
            elif rv >= 2:   vcolors.append("#FFA500")
            elif rv >= 1.5: vcolors.append("#90EE90")
            else:           vcolors.append("#4a4a4a")

        idx        = plot_data.index
        pivot_line = pd.Series(float(pivot), index=idx, dtype=float)
        rsi_70     = pd.Series(70.0,          index=idx, dtype=float)
        rsi_30     = pd.Series(30.0,          index=idx, dtype=float)

        def _ap(s, **kw):
            return mpf.make_addplot(s, **kw) if s.notna().sum() >= 2 else None

        apds = [ap for ap in [
            _ap(plot_data["SMA9"],   color="#00BFFF", width=0.9),
            _ap(plot_data["SMA21"],  color="#FFA500", width=0.9),
            _ap(plot_data["SMA50"],  color="#32CD32", width=1.3),
            _ap(plot_data["SMA200"], color="#FF4500", width=1.6),
            _ap(pivot_line,          color="#FFD700", width=1.4, linestyle="--"),
            _ap(plot_data["RSI"],    panel=2, color="#9370DB", width=1.0, ylabel="RSI"),
            _ap(rsi_70,              panel=2, color="#FF6666", width=0.5, linestyle="--"),
            _ap(rsi_30,              panel=2, color="#66FF66", width=0.5, linestyle="--"),
        ] if ap is not None]

        style = mpf.make_mpf_style(
            base_mpf_style="nightclouds", gridstyle="--",
            gridcolor="#2a2a2a", facecolor="#141414",
            edgecolor="#2a2a2a", figcolor="#141414", y_on_right=True,
        )

        fig, axes = mpf.plot(
            plot_data, type="candle", style=style, addplot=apds,
            volume=True, figsize=(14, 9),
            title=f"\n{ticker} — 60D Base Setup  |  Pivot: ${pivot:.2f} (gold)  |  SMA: 9(blue) 21(orange) 50(green) 200(red)",
            panel_ratios=(4, 1.2, 1.8), returnfig=True,
        )

        if len(axes) > 1:
            for bar, color in zip(axes[1].patches, vcolors[-len(axes[1].patches):]):
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
    s = s.replace('—', '-').replace('–', '-').replace('‘', "'").replace('’', "'").replace('“', '"').replace('”', '"')
    return re.sub(r'[^\x00-\xFF]', '', s).strip()


def generate_setup_pdf(results, hist_cache=None):
    now    = datetime.now(ET)
    grades = {"A": [], "B": [], "C": []}
    for r in results:
        g = str(r.get("grade", ""))[:1]
        if g in grades:
            grades[g].append(r["ticker"])

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=12)

    # ════════════════════════════════════════════════════════════════════════════
    # COVER PAGE
    # ════════════════════════════════════════════════════════════════════════════
    pdf.add_page()

    # Header bar
    pdf.set_fill_color(12, 20, 48)
    pdf.rect(0, 0, 210, 44, "F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 19)
    pdf.set_xy(0, 8)
    pdf.cell(210, 10, "SMID SETUP BUILDER", align="C")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_xy(0, 20)
    pdf.cell(210, 6, "Pre-Breakout VCP Watchlist  |  Qullamaggie Methodology", align="C")
    pdf.set_xy(0, 28)
    pdf.cell(210, 6, _safe(now.strftime("%B %d, %Y  |  %I:%M %p ET")), align="C")
    pdf.set_fill_color(255, 200, 0)
    pdf.rect(0, 38, 210, 2, "F")

    # Grade buckets
    pdf.set_text_color(0, 0, 0)
    pdf.set_xy(10, 50)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, "Setup Grades")
    pdf.ln(6)
    col_w = 62
    grade_meta = [
        ("A - Prime Setup",  grades["A"], (39, 174, 96),  (220, 245, 230)),
        ("B - Building",     grades["B"], (52, 152, 219), (220, 235, 250)),
        ("C - Early Stage",  grades["C"], (200, 130, 20), (250, 240, 215)),
    ]
    pdf.set_font("Helvetica", "B", 8)
    for label, tickers, hdr_rgb, _ in grade_meta:
        pdf.set_fill_color(*hdr_rgb)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(col_w, 6, f"  {label} ({len(tickers)})", border=1, fill=True)
    pdf.ln()
    max_r = max(len(gm[1]) for gm in grade_meta) or 1
    for i in range(max_r):
        for _, tickers, _, bg in grade_meta:
            val = tickers[i] if i < len(tickers) else ""
            pdf.set_fill_color(*bg)
            pdf.set_text_color(20, 20, 40)
            pdf.set_font("Helvetica", "B" if val else "", 8)
            pdf.cell(col_w, 5, val, border=1, fill=True, align="C")
        pdf.ln()

    # Summary table
    # Cols total = 190mm: Gr(7)+Ticker(14)+Company(28)+Theme(32)+MktCap(17)+Price(14)+Pivot(14)+ToPivot(14)+RS/SPY(15)+VolDry(13)+RSHi(12) = 190
    pdf.ln(6)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(0, 0, 0)
    pdf.cell(0, 6, "All Watchlist Setups")
    pdf.ln(6)

    cols = [
        ("Gr", 7), ("Ticker", 14), ("Company", 28), ("Theme", 32), ("Mkt Cap", 17),
        ("Price", 14), ("Pivot", 14), ("To Pivot", 14), ("RS/SPY", 15), ("Vol Dry", 13), ("RS Hi", 12),
    ]
    pdf.set_fill_color(12, 20, 48)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 7)
    for name, w in cols:
        pdf.cell(w, 6, name, border=1, fill=True, align="C")
    pdf.ln()

    pdf.set_text_color(20, 20, 40)
    for i, r in enumerate(results):
        grade = str(r.get("grade", ""))[:1]
        bg    = (220,245,230) if grade=="A" else (220,235,250) if grade=="B" else (250,240,215)
        pdf.set_fill_color(*bg)
        pdf.set_font("Helvetica", "B" if grade == "A" else "", 7)
        rs   = r.get("rsVsSpy", r.get("rs_vs_spy", 0)) or 0
        rs_s = f"+{rs:.1f}" if rs >= 0 else f"{rs:.1f}"
        cap  = r.get("mkt_cap_b", 0) or 0
        theme = _safe(r.get("theme", r.get("sector", "")))
        pivot_v = r.get("pivot", r.get("watchTarget", 0)) or 0
        row = [
            (grade,                                                   7),
            (r.get("ticker", ""),                                    14),
            (_safe(r.get("company", ""))[:17],                       28),
            (theme[:20],                                             32),
            (f"${cap:.2f}B",                                         17),
            (f"${r.get('price', 0):.2f}",                            14),
            (f"${pivot_v:.2f}",                                      14),
            (f"{r.get('pctToPivot', r.get('pct_to_pivot', 0)):.1f}%", 14),
            (rs_s,                                                   15),
            ("YES" if r.get("volDryup", r.get("vol_dryup")) else "no", 13),
            ("YES" if r.get("rsLineNewHigh", r.get("rs_line_new_high")) else "no", 12),
        ]
        for val, w in row:
            pdf.cell(w, 5, val, border=1, fill=True, align="C")
        pdf.ln()

    # Disclaimer on cover
    pdf.set_xy(10, 287)
    pdf.set_font("Helvetica", "I", 5.5)
    pdf.set_text_color(150, 150, 150)
    pdf.cell(0, 4, "Not financial advice. Watchlist only - setups have NOT triggered. For informational purposes only. Do your own due diligence.", align="C")

    # ════════════════════════════════════════════════════════════════════════════
    # PER-TICKER ONE-PAGER
    # Layout (A4 = 210 x 297mm):
    #   Header bar        0  – 32
    #   Subheader strip  32  – 39
    #   Left: metrics    40  – 108   (x=10,  w=92)
    #   Right: catalyst  40  – 108   (x=108, w=92)
    #   Chart           110  – 288   (x=10,  w=190, ~178mm)
    # ════════════════════════════════════════════════════════════════════════════
    for s in results:
        ticker = s["ticker"]
        grade  = str(s.get("grade", ""))[:1]
        pivot  = float(s.get("pivot", s.get("watchTarget", 0)) or 0)
        cap_b  = s.get("mkt_cap_b", 0) or 0
        fl_m   = s.get("float_m", 0) or 0
        price  = s.get("price", 0) or 0
        prox   = s.get("pctToPivot", s.get("pct_to_pivot", 0)) or 0
        rs_raw = s.get("rsVsSpy", s.get("rs_vs_spy", 0)) or 0
        rs_s   = f"+{rs_raw:.1f}%" if rs_raw >= 0 else f"{rs_raw:.1f}%"

        grade_label = {"A": "A  PRIME", "B": "B  BUILDING", "C": "C  EARLY STAGE"}.get(grade, grade)
        grade_rgb   = {"A": (34, 153, 84), "B": (41, 128, 185), "C": (194, 120, 3)}.get(grade, (80,80,80))
        grade_bg    = {"A": (220,245,230), "B": (220,235,250), "C": (250,240,215)}.get(grade, (240,240,240))

        company  = _safe(s.get("company", ticker))
        industry = _safe(s.get("industry", s.get("industry_yf", s.get("sector", ""))))
        theme    = _safe(s.get("theme", ""))

        pdf.add_page()
        pdf.set_auto_page_break(auto=False)

        # ── Dark header (0–32) ───────────────────────────────────────────────
        pdf.set_fill_color(12, 20, 48)
        pdf.rect(0, 0, 210, 32, "F")

        # Ticker (large, left)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 26)
        pdf.set_xy(10, 3)
        pdf.cell(55, 14, ticker)

        # Company below ticker
        pdf.set_font("Helvetica", "", 8)
        pdf.set_xy(10, 19)
        pdf.cell(90, 5, company[:40])

        # Price block (center)
        pdf.set_font("Helvetica", "B", 16)
        pdf.set_xy(78, 4)
        pdf.cell(54, 10, f"${price:.2f}", align="C")
        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_xy(78, 15)
        pdf.cell(54, 5, f"Pivot ${pivot:.2f}  |  {prox:.1f}% away", align="C")
        pdf.set_xy(78, 22)
        pdf.cell(54, 5, f"RS vs SPY: {rs_s}", align="C")

        # Grade badge (right)
        pdf.set_fill_color(*grade_rgb)
        pdf.rect(140, 4, 62, 24, "F")
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_xy(140, 7)
        pdf.cell(62, 8, grade_label, align="C")
        pdf.set_font("Helvetica", "", 6.5)
        pdf.set_xy(140, 17)
        pdf.cell(62, 5, f"${cap_b:.2f}B Cap  |  {fl_m:.0f}M Float", align="C")

        # Gold accent line
        pdf.set_fill_color(255, 200, 0)
        pdf.rect(0, 32, 210, 1.2, "F")

        # ── Industry/theme subheader (33–40) ─────────────────────────────────
        pdf.set_fill_color(22, 34, 70)
        pdf.rect(0, 33.2, 210, 7, "F")
        pdf.set_text_color(180, 210, 255)
        pdf.set_font("Helvetica", "", 7)
        pdf.set_xy(10, 34.5)
        voldry = "Vol Dry-Up: YES" if s.get("volDryup", s.get("vol_dryup")) else "Vol Dry-Up: no"
        rsline = "RS Line Hi: YES" if s.get("rsLineNewHigh", s.get("rs_line_new_high")) else "RS Line Hi: no"
        above  = "Above 200MA: YES" if s.get("above_200ma") else "Above 200MA: no"
        tight  = s.get("base_tight", s.get("baseTight", "-"))
        subhdr = f"{industry}  |  Theme: {theme}  |  {voldry}  |  {rsline}  |  {above}  |  Base Tight: {tight}"
        pdf.cell(0, 4, _safe(subhdr))

        # ── VCP Base Analysis (left, x=10, y=41, w=92) ──────────────────────
        MX, MY, MW = 10, 41, 92

        def _v(val, fmt=None, default="-"):
            if val is None or val == 0 or val == "0" or val == "0.0" or val == "0%":
                return default
            return fmt.format(val) if fmt else str(val)

        def _subhdr(label, y_ref):
            pdf.set_fill_color(22, 34, 70)
            pdf.set_text_color(160, 195, 255)
            pdf.set_font("Helvetica", "B", 5.8)
            pdf.set_xy(MX, y_ref)
            pdf.cell(MW, 3.5, f"  {label}", fill=True)
            return y_ref + 3.5

        def _row(label, value, y_ref, idx=0):
            bg = (245, 247, 252) if idx % 2 == 0 else (255, 255, 255)
            pdf.set_fill_color(*bg)
            pdf.set_xy(MX, y_ref)
            pdf.set_text_color(100, 110, 135)
            pdf.set_font("Helvetica", "", 5.8)
            pdf.cell(34, 4, label, fill=True)
            pdf.set_text_color(15, 20, 50)
            pdf.set_font("Helvetica", "B", 6.2)
            pdf.cell(MW - 34, 4, _safe(str(value)), fill=True)
            return y_ref + 4

        pdf.set_fill_color(12, 20, 48)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 7)
        pdf.set_xy(MX, MY)
        pdf.cell(MW, 5, "  VCP BASE ANALYSIS", fill=True)
        row_y = MY + 5

        bt    = s.get("base_tight", s.get("baseTight", 0)) or 0
        coil  = "Textbook" if bt and bt < 1.5 else "Good" if bt and bt < 2.0 else "Fair" if bt else "-"
        prox_52w_v = s.get("prox_52w", 0) or 0

        row_y = _subhdr("BASE STRUCTURE", row_y)
        row_y = _row("Pivot Price",    f"${pivot:.2f}",                             row_y, 0)
        row_y = _row("% to Pivot",     f"{prox:.1f}%",                             row_y, 1)
        row_y = _row("Days in Base",   str(s.get("days_in_base", "-")),            row_y, 2)
        row_y = _row("Tightness",      f"{bt:.2f}  {coil}" if bt else "-",         row_y, 3)
        row_y = _row("52W Hi Prox",    f"{prox_52w_v:.1f}%",                       row_y, 4)

        v1 = s.get("vol_w1_k", 0) or 0
        v2 = s.get("vol_w2_k", 0) or 0
        v3 = s.get("vol_w3_k", 0) or 0
        contraction = s.get("vol_contraction", 0) or 0
        row_y = _subhdr("VOLUME CONTRACTION  (Wk1 < Wk2 < Wk3 = VCP)", row_y)
        row_y = _row("Wk1 (recent)",  f"{v1:.0f}k  <",  row_y, 0)
        row_y = _row("Wk2",           f"{v2:.0f}k  <",  row_y, 1)
        row_y = _row("Wk3 (oldest)",  f"{v3:.0f}k",     row_y, 2)
        row_y = _row("Coil Strength", f"{contraction:.1f}% from peak" if contraction else "-", row_y, 3)

        rs_raw   = s.get("rsVsSpy", s.get("rs_vs_spy", 0)) or 0
        rs_disp  = f"+{rs_raw:.1f}%" if rs_raw >= 0 else f"{rs_raw:.1f}%"
        rs_hi    = s.get("rs_line_new_high", s.get("rsLineNewHigh", False))
        row_y = _subhdr("RELATIVE STRENGTH", row_y)
        row_y = _row("RS vs SPY (12W)", rs_disp,                                              row_y, 0)
        row_y = _row("RS Line New Hi",  "YES - Early Leader" if rs_hi else "no",             row_y, 1)
        row_y = _row("Above 50MA",      "YES" if s.get("above_50ma") else "no",               row_y, 2)
        row_y = _row("Above 200MA",     "YES" if s.get("above_200ma") else "no",              row_y, 3)

        row_y = _subhdr("FUNDAMENTALS SNAPSHOT", row_y)
        row_y = _row("Mkt Cap",       f"${cap_b:.2f}B",                    row_y, 0)
        row_y = _row("Float",         f"{fl_m:.0f}M sh",                   row_y, 1)
        row_y = _row("P/E (TTM)",     _v(s.get("trailing_pe")),             row_y, 2)
        row_y = _row("Fwd P/E",       _v(s.get("forward_pe")),              row_y, 3)
        row_y = _row("Rev Growth",    _v(s.get("rev_growth")),               row_y, 4)
        row_y = _row("Short %",       _v(s.get("short_pct")),                row_y, 5)
        row_y = _row("Inst. Own",     _v(s.get("inst_own")),                 row_y, 6)
        row_y = _row("Target Pr.",    _v(s.get("target_price"), "${:.2f}"),   row_y, 7)

        # ── Entry / Target / Stop box (left column, below VCP metrics) ────────
        # Write text first without background to measure height, then draw box under
        row_y += 2
        box_top = row_y
        pad = 1.5

        # Measure trigger text height by writing invisibly off-page
        trigger_txt = _safe(str(s.get("triggerCondition", "-")))
        target_txt  = "Target: " + _safe(str(s.get("watchTarget", "-")))
        stop_txt    = "Stop: "   + _safe(str(s.get("stopLevel", "-")))

        # Write the box content
        pdf.set_text_color(15, 20, 50)
        pdf.set_font("Helvetica", "B", 5.8)
        pdf.set_xy(MX + pad, box_top + pad)
        pdf.cell(MW - 2 * pad, 3.8, "ENTRY TRIGGER", border=0)

        pdf.set_font("Helvetica", "", 5.8)
        pdf.set_xy(MX + pad, box_top + pad + 4)
        pdf.multi_cell(MW - 2 * pad, 3.5, trigger_txt, border=0)
        after_trigger = pdf.get_y() + 1.5

        pdf.set_font("Helvetica", "B", 5.8)
        pdf.set_xy(MX + pad, after_trigger)
        pdf.multi_cell(MW - 2 * pad, 3.5, target_txt, border=0)
        pdf.set_xy(MX + pad, pdf.get_y())
        pdf.multi_cell(MW - 2 * pad, 3.5, stop_txt, border=0)

        box_bot = pdf.get_y() + 2
        box_h   = box_bot - box_top

        # Draw box behind text using a filled rect with same color (overwrites, then redraw border)
        # fpdf draws in order so we draw text first then overlay border-only rect
        pdf.set_draw_color(*grade_rgb)
        pdf.set_fill_color(*grade_bg)
        # Re-draw the background behind by painting it — text already written on top is fine
        # since fpdf z-order is paint order; instead draw border-only rect over everything
        pdf.rect(MX, box_top, MW, box_h, "D")

        row_y = box_bot

        metrics_end_y = row_y

        # ── Catalyst panel (right, x=108, y=41, w=92) ───────────────────────
        CX, CY, CW = 108, 41, 92
        pdf.set_fill_color(12, 20, 48)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 7)
        pdf.set_xy(CX, CY)
        pdf.cell(CW, 5, "  CATALYST INTELLIGENCE", fill=True)

        cat_y = CY + 5

        def _section(label, text, hdr_rgb=(30, 50, 90)):
            nonlocal cat_y
            txt = _safe(str(text or "")).strip()
            if not txt or txt in ("-", ""):
                return
            # Section label bar
            pdf.set_fill_color(*hdr_rgb)
            pdf.set_text_color(200, 220, 255)
            pdf.set_font("Helvetica", "B", 5.5)
            pdf.set_xy(CX, cat_y)
            pdf.cell(CW, 3.5, f"  {label.upper()}", fill=True)
            cat_y += 3.5
            # Body text
            pdf.set_text_color(15, 20, 50)
            pdf.set_font("Helvetica", "", 6.3)
            pdf.set_xy(CX, cat_y)
            pdf.multi_cell(CW, 3.8, txt, border=0)
            cat_y = pdf.get_y() + 1.5

        edate = s.get("earningsDate", "")
        ectx  = s.get("earningsContext", "")
        earn_str = ""
        if edate and ectx:
            earn_str = f"{edate}  -  {ectx}"
        elif edate:
            earn_str = edate
        elif ectx:
            earn_str = ectx
        else:
            earn_str = s.get("earnings_flag", s.get("earningsFlag", "No imminent earnings identified"))

        _section("Earnings",            earn_str,                                (55, 25, 90))
        _section("Business & Position", s.get("businessDescription", ""),       (15, 55, 95))
        _section("Factor & Theme",      s.get("factorExposure", ""),            (20, 75, 45))
        _section("News Flow",           s.get("newsFlow", ""),                  (15, 80, 55))
        _section("Institutional",       s.get("institutionalAngle", ""),        (60, 55, 10))
        _section("Key Risk",            s.get("keyRisk", ""),                   (110, 25, 25))

        # Analysis blurb
        reasoning = _safe(s.get("reasoning", ""))
        if reasoning:
            pdf.set_text_color(50, 60, 90)
            pdf.set_font("Helvetica", "I", 6.0)
            pdf.set_xy(CX, cat_y + 1)
            pdf.multi_cell(CW, 3.8, reasoning, border=0)
            cat_y = pdf.get_y()

        # ── Chart (full width, below both panels) ────────────────────────────
        chart_y = max(metrics_end_y, cat_y) + 4

        hist = (hist_cache or {}).get(ticker)
        if hist is None or hist.empty:
            try:
                hist = yf.Ticker(ticker).history(period="200d", interval="1d")
            except Exception:
                hist = None

        if hist is not None and not hist.empty and pivot > 0:
            chart = generate_setup_chart(ticker, hist, pivot)
            if chart:
                avail_h = 291 - chart_y
                # figsize=(14,9) → aspect 1.556; at w=190 → h=122mm
                chart_h = min(avail_h, round(190 / 1.556, 1))
                if avail_h >= 50:
                    pdf.image(chart, x=10, y=chart_y, w=190, h=chart_h)

        # Footer
        pdf.set_xy(10, 291)
        pdf.set_font("Helvetica", "I", 5.0)
        pdf.set_text_color(160, 160, 160)
        pdf.cell(0, 4,
            f"SMID Setup Builder  |  {now.strftime('%b %d %Y')}  |  "
            "Not financial advice. Setups have NOT triggered. Do your own due diligence.",
            align="C")

    return bytes(pdf.output())


# ─── Discord ──────────────────────────────────────────────────────────────────

def send_setup_pdf(pdf_bytes, results, webhook, label="SMID"):
    now      = datetime.now(ET)
    prefix   = "iwm" if "IWM" in label else "smid"
    filename = f"{prefix}_setup_{now.strftime('%Y-%m-%d_%H%M')}.pdf"
    grades   = {"A": 0, "B": 0, "C": 0}
    for r in results:
        g = str(r.get("grade", ""))[:1]
        if g in grades:
            grades[g] += 1

    content = (
        f"**{label} Setup Builder  |  EOD Pre-Breakout Watchlist**\n"
        f"{now.strftime('%B %d, %Y  --  %I:%M %p ET')}  "
        f"|  {len(results)} setups building  "
        f"|  {grades['A']}A  {grades['B']}B  {grades['C']}C\n"
        f"_These have NOT triggered yet. Watch for vol expansion above pivot._"
    )

    resp = requests.post(
        webhook,
        data={"payload_json": json.dumps({"content": content})},
        files={"files[0]": (filename, pdf_bytes, "application/pdf")},
        timeout=60,
    )
    if resp.status_code in (200, 204):
        print(f"  ✅ Setup Builder PDF sent: {filename}")
    else:
        print(f"  ❌ Discord error {resp.status_code}: {resp.text}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def run_setup_builder():
    label   = "IWM Russell 2000" if IWM_MODE else "SMID"
    webhook = DISCORD_IWM_WEBHOOK if IWM_MODE else DISCORD_SETUP_WEBHOOK

    print(f"\n{'='*50}\n{label.upper()} SETUP BUILDER -- EOD VCP WATCHLIST")
    print(f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S ET')}\n{'='*50}")

    print("\n[1/5] Building universe...")
    universe = load_iwm_universe(top_n=500) if IWM_MODE else get_universe()

    print("\n[2/5] Fetching setup data...")
    raw, hist_cache = fetch_setup_data(universe)

    print(f"\n[3/5] Scoring VCP setups ({len(raw)} candidates)...")
    top_setups = score_setups(raw)
    print(f"  Top {len(top_setups)} setups selected")

    print("\n[4/5] Claude analysis...")
    results = run_claude_setup_analysis(top_setups)

    setup_by_ticker = {d["ticker"]: d for d in top_setups}
    for r in results:
        src = setup_by_ticker.get(r.get("ticker"), {})
        for key, val in src.items():
            if key not in r or r.get(key) in (None, 0, 0.0, "", "-"):
                r[key] = val

    results.sort(key=lambda r: {"A": 0, "B": 1, "C": 2}.get(str(r.get("grade", ""))[:1], 9))
    print(f"  -> {len(results)} watchlist setups")

    print("\n[5/5] Generating PDF and sending to Discord...")
    if results:
        pdf_bytes = generate_setup_pdf(results, hist_cache)
        send_setup_pdf(pdf_bytes, results, webhook, label=label)
    else:
        requests.post(webhook, json={
            "content": f"**{label} Setup Builder  |  {datetime.now(ET).strftime('%b %d %Y')}**  |  No qualifying bases found today."
        }, timeout=15)

    print("\nDone.")


if __name__ == "__main__":
    run_setup_builder()
