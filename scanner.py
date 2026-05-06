"""
Breakout Stock Scanner — Daily Market Open & Close
Screens small/mid-cap stocks against 7 momentum criteria and sends results to Telegram.

Requirements:
    pip install yfinance anthropic python-telegram-bot requests

Setup:
    1. Get Anthropic API key: https://console.anthropic.com
    2. Create a Telegram bot: message @BotFather on Telegram → /newbot
    3. Get your chat ID: message @userinfobot on Telegram
    4. Fill in the CONFIG section below
    5. Schedule with cron or Task Scheduler (see README)
"""

import yfinance as yf
import anthropic
import requests
import json
import sys
from datetime import datetime
import pytz

# ─── CONFIG ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = "sk-ant-YOUR_KEY_HERE"
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"       # from @BotFather
TELEGRAM_CHAT_ID   = "YOUR_CHAT_ID_HERE"         # from @userinfobot
# ─────────────────────────────────────────────────────────────────────────────

# Candidate universe — broad small/mid-cap watchlist
# Extend this list as you see fit
UNIVERSE = [
    # Space / Defense
    "RKLB", "LUNR", "ASTS", "FLY", "ACHR", "JOBY", "KTOS",
    # Quantum / AI infra
    "IONQ", "RGTI", "QUBT", "NBIS", "SOUN", "BBAI",
    # Nuclear / Energy
    "SMR", "OKLO", "BWXT", "LEU",
    # Biotech catalyst
    "RXRX", "BEAM", "VCNX", "ACRS", "ITOS",
    # Reshoring / Industrial
    "STRL", "ARIS", "APOG", "GRC",
    # Autonomy / Robotics
    "SERV", "MVST", "LIDR",
    # Misc momentum
    "CIFR", "MARA", "RIOT", "CLSK",
]

ET = pytz.timezone("America/New_York")

def get_scan_type():
    """Determine if this is a market-open or market-close scan."""
    now = datetime.now(ET)
    hour = now.hour
    if 9 <= hour < 11:
        return "MARKET OPEN"
    elif 15 <= hour <= 16:
        return "MARKET CLOSE"
    else:
        return f"SCAN ({now.strftime('%H:%M ET')})"

def fetch_yfinance_data(tickers: list[str]) -> list[dict]:
    """Pull key metrics for each ticker from Yahoo Finance."""
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

            # Volume ratio (today vs 20-day avg)
            avg_vol = hist["Volume"].iloc[-21:-1].mean()
            today_vol = hist["Volume"].iloc[-1]
            vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0

            # Trend: above 20MA and 50MA?
            ma20 = hist["Close"].iloc[-20:].mean()
            ma50 = hist["Close"].iloc[-50:].mean() if len(hist) >= 50 else None

            above_20ma = price > ma20
            above_50ma = price > ma50 if ma50 else None

            # 3-month RS proxy (% change over 63 days)
            start_price = hist["Close"].iloc[0]
            rs_3m = ((price - start_price) / start_price) * 100

            # Market cap and float from yfinance info
            mkt_cap = info.get("marketCap", 0) or 0
            float_shares = info.get("floatShares", 0) or 0
            mkt_cap_b = mkt_cap / 1e9
            float_m = float_shares / 1e6

            results.append({
                "ticker": ticker,
                "company": info.get("shortName", ticker),
                "price": round(price, 2),
                "change_pct": round(change_pct, 2),
                "mkt_cap_b": round(mkt_cap_b, 2),
                "float_m": round(float_m, 1),
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

    print(f"  → {len(results)} tickers fetched successfully")
    return results

def pre_filter(data: list[dict]) -> list[dict]:
    """Hard filters before sending to Claude — reduces API cost."""
    filtered = []
    for d in data:
        # Market cap < $10B
        if d["mkt_cap_b"] <= 0 or d["mkt_cap_b"] >= 10:
            continue
        # Float < 150M shares
        if d["float_m"] <= 0 or d["float_m"] >= 150:
            continue
        # Must be above 20MA
        if not d["above_20ma"]:
            continue
        # Volume at least 1.5x average (some action)
        if d["vol_ratio"] < 1.5:
            continue
        # Positive on the day
        if d["change_pct"] < 0:
            continue
        filtered.append(d)

    filtered.sort(key=lambda x: x["vol_ratio"], reverse=True)
    print(f"  → {len(filtered)} passed pre-filter")
    return filtered[:20]  # Send top 20 to Claude max

def run_claude_analysis(candidates: list[dict], scan_type: str) -> list[dict]:
    """Send pre-filtered candidates to Claude for full 7-criteria scoring."""
    if not candidates:
        return []

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    today = datetime.now(ET).strftime("%B %d, %Y")

    prompt = f"""You are a professional momentum trader running a daily breakout scan. Today is {today}. Scan type: {scan_type}.

Here are pre-filtered small/mid-cap stocks that passed initial screens (market cap <$10B, float <150M, above 20MA, volume >1.5x avg, positive on the day):

{json.dumps(candidates, indent=2)}

Evaluate each against ALL 7 criteria:
1. Market cap < $10B ✓ (pre-filtered)
2. Float < 150M shares ✓ (pre-filtered)  
3. Breaking out of a big base at a STEEP angle (not choppy, not extended)
4. Hot theme / narrative (AI, space, defense, biotech, nuclear, reshoring, etc.)
5. Recent catalyst (earnings, FDA, contract, partnership, news in last 2-4 weeks)
6. Strong price/volume action: linear uptrend, high RS, above key MAs ✓ (pre-filtered)
7. Exceptional setup quality overall

Using your knowledge of current market conditions and recent news for each ticker, score each one.

Return ONLY a raw JSON array of the TOP 5-7 best setups. No markdown. No preamble. Each object:
- ticker (string)
- company (string)
- price (number)
- changePercent (number)  
- marketCapB (number)
- floatM (number)
- theme (string, 4-6 words)
- catalyst (string, specific recent catalyst 6-10 words)
- signal (string, price action description 4-6 words)
- volumeVsAvg (string, e.g. "2.4x average")
- rs (string, e.g. "94 RS")
- score (exactly: "A - Top Setup" or "B - Strong Setup" or "C - Watch List")
- reasoning (string, 2 sentences max explaining why it fits all 7 criteria TODAY)

Only include tickers with genuine catalysts you know about. Omit any where you have no current catalyst information."""

    print("  → Sending to Claude for analysis...")
    response = client.messages.create(
        model="claude-opus-4-5-20251101",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()

    try:
        results = json.loads(raw)
    except json.JSONDecodeError:
        import re
        match = re.search(r'\[[\s\S]+\]', raw)
        results = json.loads(match.group(0)) if match else []

    print(f"  → Claude returned {len(results)} final candidates")
    return results

def format_telegram_message(candidates: list[dict], scan_type: str) -> str:
    """Format results as a clean Telegram message."""
    now = datetime.now(ET)
    date_str = now.strftime("%b %d, %Y — %I:%M %p ET")

    if not candidates:
        return (
            f"🔍 *BREAKOUT SCANNER — {scan_type}*\n"
            f"_{date_str}_\n\n"
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
            f"  💲 ${s.get('price', 0):.2f}  {arrow} {chg_str}\n"
            f"  📊 Cap: ${s.get('marketCapB',0):.1f}B  |  Float: {s.get('floatM',0):.0f}M sh\n"
            f"  🏷 {s.get('theme','')}\n"
            f"  ⚡ {s.get('catalyst','')}\n"
            f"  📈 {s.get('signal','')}  |  Vol: {s.get('volumeVsAvg','')}  |  {s.get('rs','')}\n"
            f"  _{s.get('reasoning','')}_\n"
        )

    lines.append("─────────────────────")
    lines.append("⚠️ _Not financial advice. Do your own due diligence._")
    return "\n".join(lines)

def send_telegram(message: str):
    """Send message via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=15)
    if resp.status_code == 200:
        print("  ✅ Telegram message sent")
    else:
        print(f"  ❌ Telegram error: {resp.status_code} — {resp.text}")

def run_scan():
    scan_type = get_scan_type()
    print(f"\n{'='*50}")
    print(f"BREAKOUT SCANNER — {scan_type}")
    print(f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S ET')}")
    print('='*50)

    print("\n[1/4] Fetching YFinance data...")
    raw_data = fetch_yfinance_data(UNIVERSE)

    print("\n[2/4] Pre-filtering candidates...")
    candidates = pre_filter(raw_data)

    if not candidates:
        print("No candidates passed pre-filter. Sending empty alert.")
        msg = format_telegram_message([], scan_type)
        send_telegram(msg)
        return

    print("\n[3/4] Running Claude analysis...")
    results = run_claude_analysis(candidates, scan_type)

    print("\n[4/4] Sending to Telegram...")
    msg = format_telegram_message(results, scan_type)
    send_telegram(msg)

    print("\nDone.")

if __name__ == "__main__":
    run_scan()
