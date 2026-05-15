"""
momentum_scanner.py — Two market-leader momentum screens (clean data tables).

  1. Qullamaggie Biggest One-Month Gainers
     Universe ranked by 1-month % change; top 2% taken, then filtered to
     ADR% >= 5 and 20-day average dollar volume >= $100M.

  2. Stockbee 20%+ in a Week
     Every name up 20%+ over the last 5 trading days, with the same
     $100M liquidity floor.

No Claude analysis — these are pure quantitative screens. Run EOD when the
daily candle is complete. Publishes clean table PDFs to the site archive
(and Discord if DISCORD_MOMENTUM_WEBHOOK_URL is set).
"""

import os
import sys
import csv
import json
import time
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
import pytz
import requests
import yfinance as yf
from fpdf import FPDF
from dotenv import load_dotenv

load_dotenv()

ET = pytz.timezone("America/New_York")
DISCORD_MOMENTUM_WEBHOOK = os.environ.get("DISCORD_MOMENTUM_WEBHOOK_URL", "")
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

def fetch_metrics(tickers):
    """
    Chunked bulk download, then compute per ticker:
      price, dollar_vol (20d avg), adr_pct (20d), chg_1mo, chg_1wk
    """
    print(f"  Downloading {len(tickers)} tickers in chunks of 50...")
    bulk = {}
    for ci in range(0, len(tickers), 50):
        chunk = tickers[ci:ci + 50]
        for attempt in range(3):
            try:
                bd = yf.download(chunk, period="120d", interval="1d",
                                 group_by="ticker", auto_adjust=True,
                                 threads=True, progress=False)
                if bd is not None and not bd.empty:
                    for t in chunk:
                        try:
                            h = bd[t].dropna(how="all") if len(chunk) > 1 else bd.dropna(how="all")
                            if not h.empty:
                                bulk[t] = h
                        except Exception:
                            pass
                    break
            except Exception:
                pass
            time.sleep(3 * (attempt + 1))
        time.sleep(1.0)
    print(f"  Got data for {len(bulk)}/{len(tickers)} tickers")

    rows = []
    for ticker, h in bulk.items():
        try:
            if h.empty or len(h) < 25:
                continue
            close = h["Close"]
            price = float(close.iloc[-1])
            if price <= 0:
                continue

            # ADR% — Qullamaggie volatility metric: avg(High/Low) over 20d - 1
            hi, lo = h["High"].tail(20), h["Low"].tail(20)
            adr_pct = float((hi / lo.where(lo > 0)).mean() - 1) * 100

            # 20-day average dollar volume
            dollar_vol = float((h["Close"] * h["Volume"]).tail(20).mean())

            # Returns
            chg_1mo = (price / float(close.iloc[-22]) - 1) * 100 if len(close) >= 22 else 0.0
            chg_1wk = (price / float(close.iloc[-6])  - 1) * 100 if len(close) >= 6  else 0.0

            rows.append({
                "ticker":     ticker,
                "price":      round(price, 2),
                "adr_pct":    round(adr_pct, 2),
                "dollar_vol": dollar_vol,
                "chg_1mo":    round(chg_1mo, 2),
                "chg_1wk":    round(chg_1wk, 2),
            })
        except Exception:
            pass

    print(f"  Computed metrics for {len(rows)} names")
    return rows


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

    # Methodology blurb box
    by = 41
    blurb_h = 7 + 4.4 * len(blurb_lines)
    pdf.set_fill_color(244, 246, 251)
    pdf.rect(10, by, 190, blurb_h, "F")
    pdf.set_draw_color(12, 20, 48)
    pdf.rect(10, by, 190, blurb_h, "D")
    pdf.set_text_color(12, 20, 48)
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.set_xy(13, by + 2)
    pdf.cell(0, 5, "METHODOLOGY")
    pdf.set_text_color(35, 42, 66)
    pdf.set_font("Helvetica", "", 7.6)
    ly = by + 7.5
    for line in blurb_lines:
        pdf.set_xy(13, ly)
        pdf.multi_cell(184, 4.0, line)
        ly = pdf.get_y() + 0.4

    # Table
    ty = by + blurb_h + 6
    cols = [
        ("#",          10, "C"),
        ("Symbol",     22, "L"),
        (change_label, 26, "R"),
        ("$ Vol",      26, "R"),
        ("ADR%",       22, "R"),
        ("Price",      28, "R"),
    ]
    # center the 134mm-wide table
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
            vals = [
                (str(i + 1),                10, "C", (90, 96, 116), ""),
                (r["ticker"],               22, "L", (15, 20, 50),  "B"),
                (chg_str,                   26, "R", (22, 130, 60) if chg >= 0 else (190, 50, 45), "B"),
                (f"${_dvol(r['dollar_vol'])}", 26, "R", (15, 20, 50), ""),
                (f"{r['adr_pct']:.2f}",     22, "R", (15, 20, 50),  ""),
                (f"${r['price']:.2f}",      28, "R", (15, 20, 50),  ""),
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

def publish(pdf_bytes, filename, discord_text):
    if DISCORD_MOMENTUM_WEBHOOK:
        try:
            resp = requests.post(
                DISCORD_MOMENTUM_WEBHOOK,
                data={"payload_json": json.dumps({"content": discord_text})},
                files={"files[0]": (filename, pdf_bytes, "application/pdf")},
                timeout=60,
            )
            print(f"  Discord: {'sent' if resp.status_code in (200,204) else resp.status_code}")
        except Exception as e:
            print(f"  Discord send failed: {e}")
    else:
        print("  DISCORD_MOMENTUM_WEBHOOK_URL not set — site archive only")
    try:
        from report_archive import archive
        archive(pdf_bytes, filename)
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
    ]
    qm_pdf = generate_table_pdf(
        "QM -- BIGGEST ONE-MONTH GAINERS",
        "Top 2% of the market by 1-month gain  |  ADR% >= 5  |  $100M+ dollar volume",
        qm_blurb, qm, "1-Mo %",
    )
    publish(qm_pdf, f"qm_monthly_{stamp}.pdf",
            f"**QM -- Biggest One-Month Gainers**  |  {now.strftime('%b %d %Y')}  |  "
            f"{len(qm)} names")
    print(f"  QM Monthly: {len(qm)} names")

    # ── Screen 2: Stockbee Weekly 20% ──
    sb = screen_stockbee_weekly(rows)
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
    ]
    sb_pdf = generate_table_pdf(
        "STOCKBEE -- 20%+ IN A WEEK",
        "Up 20%+ over the last 5 trading days  |  $100M+ dollar volume",
        sb_blurb, sb, "1-Wk %",
    )
    publish(sb_pdf, f"stockbee_weekly_{stamp}.pdf",
            f"**Stockbee -- 20%+ in a Week**  |  {now.strftime('%b %d %Y')}  |  "
            f"{len(sb)} names")
    print(f"  Stockbee Weekly: {len(sb)} names")

    print("\nDone.")


if __name__ == "__main__":
    run()
