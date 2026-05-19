"""
momentum_scanner.py — Two market-leader momentum screens (clean data tables).

  1. Qullamaggie Biggest One-Month Gainers
     Universe ranked by 1-month % change; top 2% taken, then filtered to
     ADR% >= 5 and 20-day average dollar volume >= $100M.

  2. Stockbee 20%+ in a Week
     Every name up 20%+ over the last 5 trading days, with the same
     $100M liquidity floor.

No Claude analysis — these are pure quantitative screens. Run EOD when the
daily candle is complete. Publishes clean table PDFs to the site archive.
"""

import os
import sys
import csv
import json
import time
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
import pytz
import requests
import yfinance as yf
from fpdf import FPDF
from dotenv import load_dotenv

load_dotenv()

import polygon_data

ET = pytz.timezone("America/New_York")
IWM_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "IWM_holdings.csv")

# Hardcoded liquid large / mega caps so the universe spans the full market.
# IWM covers small caps; this covers everything above it.
LARGE_CAPS = [
    "AAPL","MSFT","NVDA","AMZN","GOOGL","GOOG","META","AVGO","TSLA","BRK-B",
    "LLY","JPM","V","XOM","UNH","MA","COST","HD","PG","JNJ","WMT","NFLX","CRM",
    "BAC","ORCL","MRK","ABBV","CVX","KO","AMD","PEP","ADBE","ACN","LIN","TMO",
    "MCD","CSCO","ABT","INTC","INTU","TXN","QCOM","WFC","DHR","IBM","GE","NOW",
    "AMAT","CAT","VZ","AXP","PFE","MS","NEE","SPGI","RTX","UNP","LOW","HON",
    "GS","ISRG","BKNG","COP","PGR","T","BLK","ELV","C","MU","LRCX","SYK","BSX",
    "ADI","MDT","VRTX","REGN","TJX","CB","PLD","ADP","MMC","KLAC","CI","SO",
    "DE","BMY","SCHW","MO","DUK","SBUX","FI","ICE","SHW","ZTS","BX","WM","CME",
    "GD","EOG","SNPS","CDNS","TT","ITW","NOC","MCK","APH","CL","PH","MSI","PNC",
    "USB","AON","MAR","CMG","FCX","ECL","EMR","NXPI","ORLY","MMM","PYPL","ROP",
    "APD","COF","HCA","CARR","AJG","NSC","PCAR","SLB","TFC","WELL","TGT","DKNG",
    "PLTR","SMCI","COIN","ARM","CRWD","SNOW","DASH","ABNB","UBER","SHOP","PANW",
    "DDOG","NET","SNDK","WDC","STX","ON","MRVL","ANET","DELL","HPQ","CSX","FTNT",
]


# ─── Universe ─────────────────────────────────────────────────────────────────

def load_iwm_tickers(top_n=500):
    """Top-N IWM constituents by weight from the holdings CSV."""
    out = []
    try:
        with open(IWM_CSV, encoding="utf-8") as f:
            lines = f.readlines()
        hdr = next(i for i, ln in enumerate(lines) if ln.strip().startswith("Ticker"))
        reader = csv.DictReader(iter(lines[hdr:]))
        for row in reader:
            if not row or "Asset Class" not in row:
                continue
            if row.get("Asset Class") and "Equity" in row["Asset Class"]:
                t = (row.get("Ticker") or "").strip()
                if t and "." not in t and t != "-":
                    out.append(t)
            if len(out) >= top_n:
                break
    except Exception as e:
        print(f"  IWM CSV read failed: {e}")
    return out


def get_market_universe():
    seen, uni = set(), []
    for t in LARGE_CAPS + load_iwm_tickers(500):
        tt = t.strip().upper()
        if tt and tt not in seen:
            seen.add(tt)
            uni.append(tt)
    print(f"  Universe: {len(uni)} liquid US names")
    return uni


# ─── Data + metrics ───────────────────────────────────────────────────────────

def _metrics_from_series(ticker, close, high, low, vol):
    """Compute the momentum metrics from raw daily series — source-agnostic
    (Polygon or yfinance both feed plain lists here)."""
    n = len(close)
    if n < 25:
        return None
    price = float(close[-1])
    if price <= 0:
        return None

    # ADR% — Qullamaggie volatility metric: avg(High/Low) over last 20d - 1
    pairs = [(h, l) for h, l in zip(high[-20:], low[-20:]) if l and l > 0]
    adr_pct = (sum(h / l for h, l in pairs) / len(pairs) - 1) * 100 if pairs else 0.0

    # 20-day average dollar volume
    last20 = list(zip(close[-20:], vol[-20:]))
    dollar_vol = sum(c * v for c, v in last20) / len(last20) if last20 else 0.0

    # Returns
    chg_1mo = (price / float(close[-22]) - 1) * 100 if n >= 22 and close[-22] else 0.0
    chg_1wk = (price / float(close[-6])  - 1) * 100 if n >= 6  and close[-6]  else 0.0

    return {
        "ticker":     ticker,
        "price":      round(price, 2),
        "adr_pct":    round(adr_pct, 2),
        "dollar_vol": dollar_vol,
        "chg_1mo":    round(chg_1mo, 2),
        "chg_1wk":    round(chg_1wk, 2),
    }


def _series_via_polygon(tickers, workers=12):
    """Per-ticker daily bars from Polygon, fetched concurrently.
    {ticker: (close[], high[], low[], vol[])}. Polygon Starter has no
    per-minute rate cap, so a thread pool is safe and ~12x faster."""
    def _one(t):
        bars = polygon_data.daily_bars(t, days=130)
        if bars and len(bars) >= 25:
            return t, (
                [b.get("c", 0) for b in bars],
                [b.get("h", 0) for b in bars],
                [b.get("l", 0) for b in bars],
                [b.get("v", 0) for b in bars],
            )
        return t, None

    out = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for t, series in ex.map(_one, tickers):
            if series:
                out[t] = series
    return out


def _series_via_yfinance(tickers):
    """Chunked yf.download fallback. {ticker: (close[], high[], low[], vol[])}."""
    out = {}
    for ci in range(0, len(tickers), 50):
        chunk = tickers[ci:ci + 50]
        for attempt in range(3):
            try:
                bd = yf.download(chunk, period="130d", interval="1d",
                                 group_by="ticker", auto_adjust=True,
                                 threads=True, progress=False)
                if bd is not None and not bd.empty:
                    for t in chunk:
                        try:
                            h = bd[t].dropna(how="all") if len(chunk) > 1 else bd.dropna(how="all")
                            if not h.empty:
                                out[t] = (
                                    [float(x) for x in h["Close"]],
                                    [float(x) for x in h["High"]],
                                    [float(x) for x in h["Low"]],
                                    [float(x) for x in h["Volume"]],
                                )
                        except Exception:
                            pass
                    break
            except Exception:
                pass
            time.sleep(3 * (attempt + 1))
        time.sleep(1.0)
    return out


def fetch_metrics(tickers):
    """Per-ticker price metrics (price, dollar_vol, adr_pct, chg_1mo, chg_1wk).

    Polygon is the primary feed — an official keyed API, reliable from the
    GitHub Actions datacenter IPs that routinely got rate-limited / empty
    frames from yfinance's chunked bulk download. yfinance is kept as an
    automatic per-ticker fallback for anything Polygon misses."""
    series = {}
    if polygon_data.available():
        print(f"  Fetching {len(tickers)} tickers via Polygon...")
        series = _series_via_polygon(tickers)
        print(f"  Polygon: {len(series)}/{len(tickers)} tickers")
    else:
        print("  POLYGON_API_KEY not set — using yfinance only")

    missing = [t for t in tickers if t not in series]
    if missing:
        print(f"  yfinance fallback for {len(missing)} tickers...")
        fb = _series_via_yfinance(missing)
        series.update(fb)
        print(f"  yfinance recovered {len(fb)}/{len(missing)}")

    print(f"  Got data for {len(series)}/{len(tickers)} tickers")

    rows = []
    for ticker, (close, high, low, vol) in series.items():
        try:
            m = _metrics_from_series(ticker, close, high, low, vol)
            if m:
                rows.append(m)
        except Exception:
            pass

    print(f"  Computed metrics for {len(rows)} names")
    return rows


# ─── Earnings dates ───────────────────────────────────────────────────────────

def fetch_earnings_dates(tickers):
    """Next earnings date per ticker via yfinance calendar.

    Names up 20-100% are very often reacting to (or running into) earnings —
    knowing whether the catalyst is AHEAD or already BEHIND is the key risk
    fact for a momentum mover. Fetched only for the handful of screened names."""
    today = datetime.now(ET).date()
    out = {}
    for t in tickers:
        try:
            cal = yf.Ticker(t).calendar
            dates = cal.get("Earnings Date", []) if isinstance(cal, dict) else []
            if not dates:
                continue
            ed = dates[0]
            # yfinance returns plain datetime.date (no .date() method)
            if isinstance(ed, datetime):
                ed = ed.date()
            elif not isinstance(ed, date):
                continue
            out[t] = {
                "date":  ed,
                "delta": (ed - today).days,
                "est":   len(dates) > 1 and dates[-1] != dates[0],
            }
        except Exception:
            pass
        time.sleep(0.25)
    return out


def _ern_str(info):
    """Compact earnings cell: 'May 20', 'May 20*' (estimated), 'Reported', '--'."""
    if not info:
        return "--"
    delta = info["delta"]
    if delta < -10:                       # last report is stale, next not set
        return "--"
    if delta < 0:
        return "Reported"
    return info["date"].strftime("%b %d") + ("*" if info["est"] else "")


# ─── Screens ──────────────────────────────────────────────────────────────────

MIN_DOLLAR_VOL = 100_000_000   # $100M liquidity floor


def screen_qm_monthly(rows):
    """Top 2% of the universe by 1-month gain, then ADR>=5 + $100M vol filter."""
    if not rows:
        return []
    ranked = sorted(rows, key=lambda r: r["chg_1mo"], reverse=True)
    cutoff = max(15, int(len(ranked) * 0.02))
    top2pct = ranked[:cutoff]
    screened = [r for r in top2pct
                if r["adr_pct"] >= 5.0 and r["dollar_vol"] >= MIN_DOLLAR_VOL]
    screened.sort(key=lambda r: r["dollar_vol"], reverse=True)
    return screened


def screen_stockbee_weekly(rows):
    """Every name up 20%+ over the last week, with the $100M liquidity floor."""
    screened = [r for r in rows
                if r["chg_1wk"] >= 20.0 and r["dollar_vol"] >= MIN_DOLLAR_VOL]
    screened.sort(key=lambda r: r["dollar_vol"], reverse=True)
    return screened


# ─── PDF ──────────────────────────────────────────────────────────────────────

def _dvol(v):
    if v >= 1e9:  return f"{v/1e9:.1f}B"
    if v >= 1e6:  return f"{v/1e6:.1f}M"
    return f"{v/1e3:.0f}K"


def generate_table_pdf(title, subtitle, blurb_lines, rows, change_label):
    now = datetime.now(ET)
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_page()

    # Header
    pdf.set_fill_color(12, 20, 48)
    pdf.rect(0, 0, 210, 34, "F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 17)
    pdf.set_xy(0, 7)
    pdf.cell(210, 9, title, align="C")
    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_xy(0, 17)
    pdf.cell(210, 5, subtitle, align="C")
    pdf.set_xy(0, 23)
    pdf.cell(210, 5, now.strftime("%B %d, %Y  -  %I:%M %p ET"), align="C")
    pdf.set_fill_color(255, 200, 0)
    pdf.rect(0, 34, 210, 1.5, "F")

    # Methodology section — navy header strip + wrapped text.
    # Text is rendered first and the cursor tracked, so the table is placed
    # below the TRUE end of the (multi-line-wrapping) blurb — no overlap.
    by = 41
    pdf.set_fill_color(12, 20, 48)
    pdf.rect(10, by, 190, 6, "F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_xy(13, by + 1.2)
    pdf.cell(0, 4, "METHODOLOGY")

    pdf.set_text_color(38, 45, 70)
    pdf.set_font("Helvetica", "", 7.7)
    ly = by + 9
    for line in blurb_lines:
        pdf.set_xy(13, ly)
        pdf.multi_cell(184, 4.3, line)
        ly = pdf.get_y() + 2.0
    blurb_bottom = ly

    # Gold divider between methodology and the table
    pdf.set_fill_color(255, 200, 0)
    pdf.rect(10, blurb_bottom + 1, 190, 0.7, "F")

    # Table — clear gap below the divider
    ty = blurb_bottom + 7
    cols = [
        ("#",          10, "C"),
        ("Symbol",     22, "L"),
        (change_label, 24, "R"),
        ("$ Vol",      24, "R"),
        ("ADR%",       20, "R"),
        ("Price",      24, "R"),
        ("Next Ern",   28, "C"),
    ]
    # center the table
    tx = 10 + (190 - sum(c[1] for c in cols)) / 2
    pdf.set_xy(tx, ty)
    pdf.set_fill_color(12, 20, 48)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 8)
    for name, w, _ in cols:
        pdf.cell(w, 7, name, border=1, fill=True, align="C")
    pdf.ln()

    if not rows:
        pdf.set_x(tx)
        pdf.set_text_color(120, 120, 120)
        pdf.set_font("Helvetica", "I", 9)
        pdf.cell(sum(c[1] for c in cols), 9,
                 "No names met the screen criteria today.", border=1, align="C")
    else:
        for i, r in enumerate(rows):
            pdf.set_x(tx)
            bg = (244, 247, 252) if i % 2 == 0 else (255, 255, 255)
            pdf.set_fill_color(*bg)
            chg = r.get("_chg", 0)
            chg_str = f"+{chg:.1f}%" if chg >= 0 else f"{chg:.1f}%"
            ern = r.get("_ern", "--")
            ern_rgb = (190, 50, 45) if ern not in ("--", "Reported") else (90, 96, 116)
            vals = [
                (str(i + 1),                10, "C", (90, 96, 116), ""),
                (r["ticker"],               22, "L", (15, 20, 50),  "B"),
                (chg_str,                   24, "R", (22, 130, 60) if chg >= 0 else (190, 50, 45), "B"),
                (f"${_dvol(r['dollar_vol'])}", 24, "R", (15, 20, 50), ""),
                (f"{r['adr_pct']:.2f}",     20, "R", (15, 20, 50),  ""),
                (f"${r['price']:.2f}",      24, "R", (15, 20, 50),  ""),
                (ern,                       28, "C", ern_rgb,       "B" if ern not in ("--", "Reported") else ""),
            ]
            for txt, w, align, rgb, style in vals:
                pdf.set_text_color(*rgb)
                pdf.set_font("Helvetica", style, 8)
                pdf.cell(w, 6, txt, border=1, fill=True, align=align)
            pdf.ln()

    # Footer
    pdf.set_auto_page_break(auto=False)
    pdf.set_xy(10, 287)
    pdf.set_font("Helvetica", "I", 5.5)
    pdf.set_text_color(150, 150, 150)
    pdf.cell(0, 4, "Not financial advice. Quantitative screen, EOD data. "
             "For informational purposes only. Do your own due diligence.", align="C")

    return bytes(pdf.output())


# ─── Output ───────────────────────────────────────────────────────────────────

def publish(pdf_bytes, filename):
    """Archive a screen PDF to the GitHub Pages report site."""
    try:
        from report_archive import archive
        archive(pdf_bytes, filename)
        print(f"  Archived to site: {filename}")
    except Exception as e:
        print(f"  Archive failed: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def run():
    now = datetime.now(ET)
    stamp = now.strftime("%Y-%m-%d_%H%M")
    print(f"\n{'='*54}\nMOMENTUM SCANS  --  {now.strftime('%Y-%m-%d %H:%M ET')}\n{'='*54}")

    print("\n[1/3] Building universe...")
    universe = get_market_universe()

    print("\n[2/3] Fetching data + computing metrics...")
    rows = fetch_metrics(universe)

    print("\n[3/3] Running screens + publishing...")

    # ── Screen 1: QM Monthly Gainers ──
    qm = screen_qm_monthly(rows)
    # ── Screen 2: Stockbee Weekly 20% ──
    sb = screen_stockbee_weekly(rows)

    # Earnings dates for the union of screened names (small list — fast)
    screened_tickers = sorted({r["ticker"] for r in qm} | {r["ticker"] for r in sb})
    print(f"  Fetching earnings dates for {len(screened_tickers)} screened names...")
    ern_map = fetch_earnings_dates(screened_tickers)
    for r in qm + sb:
        r["_ern"] = _ern_str(ern_map.get(r["ticker"]))

    for r in qm:
        r["_chg"] = r["chg_1mo"]
    qm_blurb = [
        "Qullamaggie's Biggest One-Month Gainers. Of the full liquid universe, the top 2% "
        "by 1-month price change are taken, then filtered to the names that are both highly "
        "volatile and highly liquid.",
        "Membership criteria:  (1) ranks in the top 2% of the market by 1-month % change;  "
        "(2) ADR% >= 5 (20-day Average Daily Range -- a volatility floor);  "
        "(3) 20-day average dollar volume >= $100M (a liquidity floor).",
        "ADR% = average of (daily High / daily Low) over the last 20 sessions, minus 1. "
        "It measures how much room a stock moves intraday -- Qullamaggie targets high-ADR "
        "names because they travel far enough to be worth trading. Sorted by dollar volume.",
        "Next Ern = next scheduled earnings date (* = estimated/unconfirmed window; "
        "'Reported' = reported within the last 10 days). A move running INTO earnings "
        "carries binary event risk; a move just AFTER a report is reacting to known news.",
    ]
    qm_pdf = generate_table_pdf(
        "QM -- BIGGEST ONE-MONTH GAINERS",
        "Top 2% of the market by 1-month gain  |  ADR% >= 5  |  $100M+ dollar volume",
        qm_blurb, qm, "1-Mo %",
    )
    publish(qm_pdf, f"qm_monthly_{stamp}.pdf")
    print(f"  QM Monthly: {len(qm)} names")

    # ── Screen 2: Stockbee Weekly 20% ──
    for r in sb:
        r["_chg"] = r["chg_1wk"]
    sb_blurb = [
        "Stockbee's '20% Plus in a Week' momentum screen -- names that have surged 20% or "
        "more over the last five trading sessions. These are the market's most explosive "
        "short-term movers, often reacting to earnings, news, or sector rotation.",
        "Membership criteria:  (1) up 20%+ over the last 5 trading days;  "
        "(2) 20-day average dollar volume >= $100M (a liquidity floor so only tradeable "
        "names appear).",
        "A large list signals a strong, broad momentum environment; a short list signals a "
        "narrow or risk-off tape. Sorted by dollar volume -- the most liquid movers first.",
        "Next Ern = next scheduled earnings date (* = estimated/unconfirmed window; "
        "'Reported' = reported within the last 10 days). A 20%+ week running INTO earnings "
        "carries binary event risk; a 20%+ week just AFTER a report is reacting to news.",
    ]
    sb_pdf = generate_table_pdf(
        "STOCKBEE -- 20%+ IN A WEEK",
        "Up 20%+ over the last 5 trading days  |  $100M+ dollar volume",
        sb_blurb, sb, "1-Wk %",
    )
    publish(sb_pdf, f"stockbee_weekly_{stamp}.pdf")
    print(f"  Stockbee Weekly: {len(sb)} names")

    print("\nDone.")


if __name__ == "__main__":
    run()
