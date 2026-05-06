"""
scanner.py — reads credentials from environment variables.
Use this version when deploying to GitHub Actions or any cloud environment.
Set these env vars / GitHub Secrets:
  ANTHROPIC_API_KEY
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
"""

import os
import yfinance as yf
import anthropic
import requests
import json
import re
from datetime import datetime
import pytz

# ─── CREDENTIALS (from environment) ──────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

if not all([ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
    raise EnvironmentError(
        "Missing one or more required env vars: "
        "ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID"
    )
# ─────────────────────────────────────────────────────────────────────────────

UNIVERSE = [
    "RKLB", "LUNR", "ASTS", "FLY", "ACHR", "JOBY", "KTOS",
    "IONQ", "RGTI", "QUBT", "NBIS", "SOUN", "BBAI",
    "SMR", "OKLO", "BWXT", "LEU",
    "RXRX", "BEAM", "VCNX", "ACRS", "ITOS",
    "STRL", "ARIS", "APOG", "GRC",
    "SERV", "MVST", "LIDR",
    "CIFR", "MARA", "RIOT", "CLSK",
]

ET = pytz.timezone("America/New_York")

def get_scan_type():
    now = datetime.now(ET)
    hour = now.hour
    if 9 <= hour < 11:
        return "MARKET OPEN"
    elif 15 <= hour <= 16:
        return "MARKET CLOSE"
    else:
        return f"SCAN ({now.strftime('%H:%M ET')})"

def fetch_yfinance_data(tickers):
    results = []
    print(f"Fetching data for {len(tickers)} tickers...")
    for ticker in tickers:
        try:
            t = yf.Ticker(ticker)
            info = t.info
            hist = t.history(period="3mo", interval="1d")
            if hist.empty or len(hist) < 20:
                continue
            price = hist["Close"].iloc[-1]
            prev_close = hist["Close"].iloc[-2]
            change_pct = ((price - prev_close) / prev_close) * 100
            avg_vol = hist["Volume"].iloc[-21:-1].mean()
            today_vol = hist["Volume"].iloc[-1]
            vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0
            ma20 = hist["Close"].iloc[-20:].mean()
            ma50 = hist["Close"].iloc[-50:].mean() if len(hist) >= 50 else None
            above_20ma = price > ma20
            above_50ma = price > ma50 if ma50 else None
            start_price = hist["Close"].iloc[0]
            rs_3m = ((price - start_price) / start_price) * 100
            mkt_cap = info.get("marketCap", 0) or 0
            float_shares = info.get("floatShares", 0) or 0
            results.append({
                "ticker": ticker,
                "company": info.get("shortName", ticker),
                "price": round(price, 2),
                "change_pct": round(change_pct, 2),
                "mkt_cap_b": round(mkt_cap / 1e9, 2),
                "float_m": round(float_shares / 1e6, 1),
                "vol_ratio": round(vol_ratio, 2),
                "above_20ma": above_20ma,
                "above_50ma": above_50ma,
                "rs_3m": round(rs_3m, 1),
                "sector": info.get("sector", ""),
                "industry": info.get("industry", ""),
                "52w_high": round(info.get("fiftyTwoWeekHigh", 0), 2),
                "52w_low": round(info.get("fiftyTwoWeekLow", 0), 2),
            })
        except Exception as e:
            print(f"  ⚠ Skipped {ticker}: {e}")
    print(f"  → {len(results)} tickers fetched")
    return results

def pre_filter(data):
    filtered = []
    for d in data:
        if d["mkt_cap_b"] <= 0 or d["mkt_cap_b"] >= 10:
            continue
        if d["float_m"] <= 0 or d["float_m"] >= 150:
            continue
        if not d["above_20ma"]:
            continue
        if d["vol_ratio"] < 1.5:
            continue
        if d["change_pct"] < 0:
            continue
        filtered.append(d)
    filtered.sort(key=lambda x: x["vol_ratio"], reverse=True)
    print(f"  → {len(filtered)} passed pre-filter")
    return filtered[:20]

def run_claude_analysis(candidates, scan_type):
    if not candidates:
        return []
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    today = datetime.now(ET).strftime("%B %d, %Y")
    prompt = f"""You are a professional momentum trader running a daily breakout scan. Today is {today}. Scan type: {scan_type}.

Pre-filtered candidates (mkt cap <$10B, float <150M, above 20MA, vol >1.5x avg, green on day):
{json.dumps(candidates, indent=2)}

Score each against all 7 criteria using your knowledge of recent news and catalysts.
Return ONLY a raw JSON array of top 5-7 setups. No markdown. No preamble.
Each object: ticker, company, price, changePercent, marketCapB, floatM, theme, catalyst, signal, volumeVsAvg, rs, score ("A - Top Setup"/"B - Strong Setup"/"C - Watch List"), reasoning (2 sentences max).
Only include tickers with genuine known catalysts."""

    print("  → Sending to Claude...")
    response = client.messages.create(
        model="claude-opus-4-5-20251101",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except:
        match = re.search(r'\[[\s\S]+\]', raw)
        return json.loads(match.group(0)) if match else []

def format_telegram_message(candidates, scan_type):
    now = datetime.now(ET)
    date_str = now.strftime("%b %d, %Y — %I:%M %p ET")
    if not candidates:
        return (
            f"🔍 *BREAKOUT SCANNER — {scan_type}*\n_{date_str}_\n\n"
            "No candidates passed all 7 criteria today."
        )
    score_emoji = {"A - Top Setup": "🟢", "B - Strong Setup": "🔵", "C - Watch List": "🟡"}
    lines = [f"🔍 *BREAKOUT SCANNER — {scan_type}*", f"_{date_str}_\n"]
    for s in candidates:
        chg = s.get("changePercent", 0)
        chg_str = f"+{chg:.2f}%" if chg >= 0 else f"{chg:.2f}%"
        arrow = "▲" if chg >= 0 else "▼"
        emoji = score_emoji.get(s.get("score", ""), "⚪")
        lines.append(
            f"{emoji} *{s['ticker']}* — {s.get('company','')}\n"
            f"  💲 ${s.get('price',0):.2f}  {arrow} {chg_str}\n"
            f"  📊 Cap: ${s.get('marketCapB',0):.1f}B  |  Float: {s.get('floatM',0):.0f}M sh\n"
            f"  🏷 {s.get('theme','')}\n"
            f"  ⚡ {s.get('catalyst','')}\n"
            f"  📈 {s.get('signal','')}  |  Vol: {s.get('volumeVsAvg','')}  |  {s.get('rs','')}\n"
            f"  _{s.get('reasoning','')}_\n"
        )
    lines.append("─────────────────────")
    lines.append("⚠️ _Not financial advice. Do your own due diligence._")
    return "\n".join(lines)

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }, timeout=15)
    if resp.status_code == 200:
        print("  ✅ Telegram sent")
    else:
        print(f"  ❌ Telegram error {resp.status_code}: {resp.text}")

def run_scan():
    scan_type = get_scan_type()
    print(f"\n{'='*50}\nBREAKOUT SCANNER — {scan_type}")
    print(f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S ET')}\n{'='*50}")

    print("\n[1/4] Fetching YFinance data...")
    raw_data = fetch_yfinance_data(UNIVERSE)

    print("\n[2/4] Pre-filtering...")
    candidates = pre_filter(raw_data)

    print("\n[3/4] Claude analysis...")
    results = run_claude_analysis(candidates, scan_type)

    print("\n[4/4] Sending to Telegram...")
    send_telegram(format_telegram_message(results, scan_type))
    print("\nDone.")

if __name__ == "__main__":
    run_scan()
