"""
alt_data.py — Real-time alt-data intelligence report for a single ticker.

  python alt_data.py --ticker NVDA

Pulls news flow, social sentiment, Reddit chatter, and Nimble-extracted web
context (see nimble_data.py), sends it to Claude for synthesis, and produces a
clean PDF: an AI read of the narrative + sentiment, plus the raw data tables.

Env: ANTHROPIC_API_KEY, NIMBLE_API_KEY, DISCORD_ALTDATA_WEBHOOK_URL (optional).
"""

import os
import sys
import re
import json
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pytz
import requests
import anthropic
from fpdf import FPDF
from dotenv import load_dotenv

# override=True so .env wins over any stale/empty OS env var (e.g. a globally
# set empty ANTHROPIC_API_KEY). Harmless in CI — no .env file exists there.
load_dotenv(override=True)

from nimble_data import gather_alt_data

ET = pytz.timezone("America/New_York")
ANTHROPIC_API_KEY      = os.environ.get("ANTHROPIC_API_KEY", "")
DISCORD_ALTDATA_WEBHOOK = os.environ.get("DISCORD_ALTDATA_WEBHOOK_URL", "")


def _safe(text):
    s = str(text)
    s = (s.replace("—", "-").replace("–", "-")
          .replace("‘", "'").replace("’", "'")
          .replace("“", '"').replace("”", '"'))
    return re.sub(r"[^\x00-\xFF]", "", s).strip()


# ─── Claude synthesis ─────────────────────────────────────────────────────────

SYNTH_PROMPT = """You are a market intelligence analyst. You are given real-time
alt-data for a stock ticker: recent news headlines, StockTwits social messages
(with the platform's own Bullish/Bearish tags), Reddit posts, and a block of
web-search context. Synthesize it into a concise, useful read.

Return ONLY a raw JSON object (no markdown) with these fields:
  sentimentSummary  : 1-2 sentences - the overall real-time sentiment read.
  narrative         : 2-3 sentences - the dominant story / catalyst driving
                      attention right now.
  bullCase          : 1-2 sentences - what the bulls in the chatter are saying.
  bearCase          : 1-2 sentences - what the bears in the chatter are saying.
  attentionRead     : 1-2 sentences - is retail attention building or fading,
                      and why (use the watcher count, message volume, news
                      density as evidence).
  riskFlags         : 1 sentence - anything concerning in the chatter (pump
                      language, lawsuit/dilution buzz, etc.) or "None apparent."
Be specific and reference actual headlines/themes. Do not invent data."""


def synthesize(alt):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    s = alt["social"]
    payload = {
        "ticker": alt["ticker"],
        "news_headlines": [f"{n['title']}  [{n['source']}]" for n in alt["news"]],
        "stocktwits": {
            "watchers": s.get("watchers", 0),
            "messages_sampled": s.get("total", 0),
            "bullish_tagged": s.get("bull", 0),
            "bearish_tagged": s.get("bear", 0),
            "sample_messages": [m["body"] for m in s.get("messages", [])[:20]],
        },
        "reddit_posts": [f"r/{r['subreddit']}: {r['title']} ({r['score']}u)"
                         for r in alt["reddit"]],
        "web_context": alt["web"][:4000],
    }
    msg = (f"{SYNTH_PROMPT}\n\nAlt-data for {alt['ticker']}:\n"
           f"{json.dumps(payload, indent=2)}")
    print("  Sending alt-data to Claude for synthesis...")
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": msg}],
    )
    raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", raw)
        return json.loads(m.group(0)) if m else {}


# ─── PDF ──────────────────────────────────────────────────────────────────────

def generate_altdata_pdf(ticker, alt, synth):
    now = datetime.now(ET)
    s   = alt["social"]
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_page()

    # Header
    pdf.set_fill_color(12, 20, 48)
    pdf.rect(0, 0, 210, 32, "F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 24)
    pdf.set_xy(10, 4)
    pdf.cell(120, 13, _safe(ticker))
    pdf.set_font("Helvetica", "", 8)
    pdf.set_xy(10, 18)
    pdf.cell(120, 5, "ALT-DATA INTELLIGENCE  -  Real-Time News, Social & Web")
    pdf.set_xy(10, 23)
    pdf.cell(120, 5, _safe(now.strftime("%B %d, %Y  -  %I:%M %p ET")))

    # Sentiment badge
    bull, bear = s.get("bull", 0), s.get("bear", 0)
    if bull + bear > 0:
        bull_pct = bull / (bull + bear) * 100
    else:
        bull_pct = 50
    if   bull_pct >= 65: badge_rgb, badge = (39, 174, 96),  "BULLISH"
    elif bull_pct >= 55: badge_rgb, badge = (90, 180, 110), "LEAN BULL"
    elif bull_pct >= 45: badge_rgb, badge = (200, 200, 200),"MIXED"
    elif bull_pct >= 35: badge_rgb, badge = (220, 130, 70), "LEAN BEAR"
    else:                badge_rgb, badge = (192, 57, 43),  "BEARISH"
    pdf.set_fill_color(*badge_rgb)
    pdf.rect(140, 5, 62, 22, "F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_xy(140, 8)
    pdf.cell(62, 8, _safe(f"SOCIAL: {badge}"), align="C")
    pdf.set_font("Helvetica", "", 6.5)
    pdf.set_xy(140, 17)
    pdf.cell(62, 5, _safe(f"StockTwits {bull} bull / {bear} bear tagged"), align="C")
    pdf.set_fill_color(255, 200, 0)
    pdf.rect(0, 32, 210, 1.2, "F")

    # Stat strip
    y = 37
    stats = [
        ("StockTwits Watchers", f"{s.get('watchers', 0):,}"),
        ("Messages Sampled",    str(s.get("total", 0))),
        ("News Items (7-30d)",  str(len(alt["news"]))),
        ("Attention Score",     str(alt["attention"])),
    ]
    bw = 46
    for i, (lbl, val) in enumerate(stats):
        x = 10 + i * 47.5
        pdf.set_fill_color(22, 34, 70)
        pdf.rect(x, y, bw, 15, "F")
        pdf.set_text_color(180, 210, 255)
        pdf.set_font("Helvetica", "", 6.3)
        pdf.set_xy(x + 1, y + 1.5)
        pdf.cell(bw - 2, 4, lbl, align="C")
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_xy(x + 1, y + 6)
        pdf.cell(bw - 2, 7, val, align="C")

    # AI synthesis
    y = 58
    def _section(label, text, hdr_rgb=(12, 20, 48)):
        nonlocal y
        txt = _safe(text or "").strip()
        if not txt:
            return
        pdf.set_fill_color(*hdr_rgb)
        pdf.rect(10, y, 190, 5.5, "F")
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 7.5)
        pdf.set_xy(12, y + 1)
        pdf.cell(0, 3.5, label.upper())
        y += 7
        pdf.set_text_color(35, 42, 66)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_xy(12, y)
        pdf.multi_cell(186, 4.4, txt)
        y = pdf.get_y() + 3

    _section("Real-Time Sentiment Read", synth.get("sentimentSummary", ""))
    _section("Dominant Narrative / Catalyst", synth.get("narrative", ""))
    _section("Bull Case (from the chatter)", synth.get("bullCase", ""), (22, 90, 55))
    _section("Bear Case (from the chatter)", synth.get("bearCase", ""), (120, 35, 30))
    _section("Retail Attention", synth.get("attentionRead", ""))
    _section("Risk Flags", synth.get("riskFlags", ""), (150, 90, 10))

    # News table
    y += 2
    pdf.set_fill_color(12, 20, 48)
    pdf.rect(10, y, 190, 6, "F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_xy(12, y + 1.2)
    pdf.cell(0, 4, f"RECENT NEWS FLOW  ({len(alt['news'])} headlines)")
    y += 8
    pdf.set_text_color(30, 36, 60)
    for i, n in enumerate(alt["news"][:14]):
        bg = (244, 247, 252) if i % 2 == 0 else (255, 255, 255)
        pdf.set_fill_color(*bg)
        pdf.set_xy(10, y)
        pdf.set_font("Helvetica", "", 7.4)
        pdf.cell(150, 5, _safe(n["title"])[:92], fill=True)
        pdf.set_font("Helvetica", "B", 6.6)
        pdf.set_text_color(90, 100, 130)
        pdf.cell(40, 5, _safe(n["source"])[:24], fill=True, align="R")
        pdf.set_text_color(30, 36, 60)
        y += 5

    # Social messages sample
    msgs = s.get("messages", [])
    if msgs:
        y += 4
        if y > 250:
            pdf.add_page(); y = 16
        pdf.set_fill_color(12, 20, 48)
        pdf.rect(10, y, 190, 6, "F")
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_xy(12, y + 1.2)
        pdf.cell(0, 4, "STOCKTWITS SOCIAL STREAM  (recent messages)")
        y += 8
        for m in msgs[:14]:
            sent = m.get("sentiment", "")
            chip = (39, 174, 96) if sent == "Bullish" else \
                   (192, 57, 43) if sent == "Bearish" else (140, 140, 150)
            pdf.set_xy(10, y)
            pdf.set_fill_color(*chip)
            pdf.set_text_color(255, 255, 255)
            pdf.set_font("Helvetica", "B", 5.8)
            pdf.cell(15, 5, (sent[:4].upper() or "-"), fill=True, align="C")
            pdf.set_fill_color(248, 249, 252)
            pdf.set_text_color(40, 46, 66)
            pdf.set_font("Helvetica", "", 7)
            pdf.cell(175, 5, _safe(m["body"])[:118], fill=True)
            y += 5
            if y > 285:
                pdf.add_page(); y = 16

    # Footer
    pdf.set_auto_page_break(auto=False)
    pdf.set_xy(10, 291)
    pdf.set_font("Helvetica", "I", 5.0)
    pdf.set_text_color(150, 150, 150)
    pdf.cell(0, 4, "Alt-data: Google News, StockTwits, Reddit, Nimble web extraction. "
             "Not financial advice. Real-time chatter is noisy - corroborate before acting.",
             align="C")

    return bytes(pdf.output())


# ─── Main ─────────────────────────────────────────────────────────────────────

def run_altdata_lookup(ticker):
    ticker  = ticker.upper().strip()
    webhook = DISCORD_ALTDATA_WEBHOOK
    print(f"\n{'='*54}\nALT-DATA INTELLIGENCE: {ticker}\n{'='*54}")

    if not ANTHROPIC_API_KEY:
        print("  Missing ANTHROPIC_API_KEY"); return

    print("[1/4] Gathering real-time alt-data...")
    alt = gather_alt_data(ticker)

    if not alt["news"] and not alt["social"].get("total") and not alt["reddit"]:
        print(f"  No alt-data found for {ticker} — likely an invalid ticker.")
        if webhook:
            requests.post(webhook, json={"content":
                f"**{ticker}** — no alt-data found. Check the symbol."}, timeout=15)
        return

    print("[2/4] Claude synthesis...")
    try:
        synth = synthesize(alt)
    except Exception as e:
        print(f"  Synthesis failed: {e}")
        synth = {}

    print("[3/4] Generating PDF...")
    pdf_bytes = generate_altdata_pdf(ticker, alt, synth)

    print("[4/4] Publishing...")
    now      = datetime.now(ET)
    filename = f"altdata_{ticker}_{now.strftime('%Y-%m-%d_%H%M')}.pdf"
    if webhook:
        try:
            s = alt["social"]
            content = (f"**{ticker} - Alt-Data Intelligence**\n"
                       f"{now.strftime('%b %d %Y %I:%M %p ET')}  |  "
                       f"{len(alt['news'])} news  |  "
                       f"StockTwits {s.get('bull',0)} bull / {s.get('bear',0)} bear")
            resp = requests.post(
                webhook,
                data={"payload_json": json.dumps({"content": content})},
                files={"files[0]": (filename, pdf_bytes, "application/pdf")},
                timeout=60,
            )
            print(f"  Discord: {'sent' if resp.status_code in (200,204) else resp.status_code}")
        except Exception as e:
            print(f"  Discord send failed: {e}")
    else:
        print("  DISCORD_ALTDATA_WEBHOOK_URL not set — site archive only")

    try:
        from report_archive import archive
        archive(pdf_bytes, filename)
    except Exception as e:
        print(f"  Archive failed: {e}")

    print("\nDone.")


if __name__ == "__main__":
    if "--ticker" in sys.argv:
        idx = sys.argv.index("--ticker")
        if idx + 1 < len(sys.argv):
            run_altdata_lookup(sys.argv[idx + 1])
        else:
            print("Usage: python alt_data.py --ticker SYMBOL")
    else:
        print("Usage: python alt_data.py --ticker SYMBOL")
