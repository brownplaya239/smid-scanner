"""
alt_data.py — Robust real-time alt-data intelligence report for a ticker.

  python alt_data.py --ticker NVDA

Signals (every one degrades gracefully if its source fails):
  - News flow        : Google News RSS, deduped + keyword-categorized
  - Social sentiment : StockTwits (full-message tone-scored by Claude)
  - Reddit chatter   : Reddit search via Nimble proxy
  - Web/SERP context : Nimble /extract
  - Price tape       : yfinance — for sentiment-vs-price divergence
  - Analyst consensus: yfinance

Measured, not just descriptive:
  - Buzz Score (0-100)        : mention volume vs this ticker's own baseline
  - Sentiment Index (-100..100): full-message tone read
  - Divergence verdict        : does the chatter CONFIRM or contradict the tape?
  - Baseline deltas           : is attention building or fading vs prior runs?
  - Forward catalysts, manipulation scan, analyst read

Per-ticker run history is kept in docs/reports/altdata_history.json so the
report can say "this is a real buzz spike" vs "normal noise".

Env: ANTHROPIC_API_KEY, NIMBLE_API_KEY.
"""

import os
import sys
import re
import json
import math
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pytz
import requests
import anthropic
from fpdf import FPDF
from dotenv import load_dotenv

load_dotenv(override=True)

from nimble_data import gather_alt_data

ET = pytz.timezone("America/New_York")
ANTHROPIC_API_KEY       = os.environ.get("ANTHROPIC_API_KEY", "")
HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "docs", "reports", "altdata_history.json")


def _safe(text):
    s = str(text)
    s = (s.replace("—", "-").replace("–", "-")
          .replace("‘", "'").replace("’", "'")
          .replace("“", '"').replace("”", '"').replace("…", "..."))
    return re.sub(r"[^\x00-\xFF]", "", s).strip()


# ─── Per-ticker run history (baseline) ────────────────────────────────────────

def load_history():
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_history(hist):
    try:
        os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(hist, f, indent=1)
    except Exception as e:
        print(f"  history save failed: {e}")


def compute_baseline(hist, ticker):
    """Average of this ticker's prior runs — the yardstick for 'is this unusual?'."""
    runs = hist.get(ticker.upper(), [])
    if not runs:
        return None
    recent = runs[-10:]
    n = len(recent)
    return {
        "runs":      n,
        "news":      sum(r.get("news", 0) for r in recent) / n,
        "social":    sum(r.get("social", 0) for r in recent) / n,
        "attention": sum(r.get("attention", 0) for r in recent) / n,
        "last_watchers": recent[-1].get("watchers", 0),
    }


# ─── Quantified scores ────────────────────────────────────────────────────────

def compute_scores(alt, baseline):
    """Buzz Score, raw sentiment, and the deterministic divergence read."""
    s = alt["social"]
    attention = alt["attention"]

    # Buzz Score 0-100 — vs the ticker's own baseline if we have one
    if baseline and baseline["attention"] > 0:
        ratio = attention / baseline["attention"]
        buzz  = 50 + 25 * math.log2(max(ratio, 0.0625))
    else:
        buzz = attention * 1.4          # first run — score on absolute volume
    buzz = max(0, min(100, round(buzz)))

    # Raw sentiment from StockTwits' own tags (Claude refines with full scoring)
    bull, bear = s.get("bull", 0), s.get("bear", 0)
    sent_raw = round((bull - bear) / (bull + bear) * 100) if (bull + bear) else 0

    # Deltas vs baseline
    deltas = {}
    if baseline and baseline["runs"] >= 1:
        def _d(cur, base):
            if base <= 0:
                return None
            return round((cur / base - 1) * 100)
        deltas = {
            "news":      _d(len(alt["news"]), baseline["news"]),
            "social":    _d(s.get("total", 0), baseline["social"]),
            "attention": _d(attention, baseline["attention"]),
            "watchers":  (s.get("watchers", 0) - baseline["last_watchers"]),
            "runs":      baseline["runs"],
        }

    # Deterministic divergence (Claude also assesses; this is the cross-check)
    price = alt.get("price", {})
    chg_1w = price.get("chg_1w", 0) if price.get("price_available") else None
    divergence = "NEUTRAL"
    if chg_1w is not None:
        sd = 1 if sent_raw > 15 else -1 if sent_raw < -15 else 0
        pd = 1 if chg_1w > 2 else -1 if chg_1w < -2 else 0
        if sd and pd:
            if sd == pd:
                divergence = "CONFIRM"
            elif sd > 0 > pd:
                divergence = "DIVERGE-BEARISH"
            else:
                divergence = "DIVERGE-BULLISH"

    return {"buzz": buzz, "sentiment_raw": sent_raw,
            "deltas": deltas, "divergence_raw": divergence}


# ─── News categorization (keyword) ────────────────────────────────────────────

_NEWS_CATS = [
    ("Earnings",     ["earnings", "eps", "revenue", "quarter", "beat", "guidance", "results"]),
    ("Analyst",      ["analyst", "upgrade", "downgrade", "price target", "rating",
                      "initiated", "overweight", "underweight", "buy rating", "outperform"]),
    ("M&A",          ["acquisition", "merger", "acquire", "takeover", "buyout", "to buy"]),
    ("Regulatory",   ["lawsuit", "sec ", "fda", "investigation", "settlement", "probe",
                      "court", "subpoena", "regulator"]),
    ("Product/Deal", ["launch", "partnership", "contract", "unveil", "deal", "product",
                      "collaborat", "agreement"]),
    ("Macro/Sector", ["fed", "tariff", "sector", "inflation", "rate cut", "economy"]),
]


def categorize_news(items):
    for n in items:
        low = n["title"].lower()
        n["category"] = "General"
        for cat, kws in _NEWS_CATS:
            if any(k in low for k in kws):
                n["category"] = cat
                break
    return items


# ─── Claude synthesis ─────────────────────────────────────────────────────────

SYNTH_PROMPT = """You are a market intelligence analyst. You are given real-time
alt-data for a ticker plus the actual price tape. Produce a MEASURED, ACTIONABLE
read - reference specific headlines/posts as evidence, never invent data.

Return ONLY a raw JSON object (no markdown) with these fields:
  messagesBull       : integer - of the StockTwits sample, how many messages read
                       bullish in tone (score ALL of them, not just tagged ones).
  messagesBear       : integer - how many read bearish.
  messagesNeutral    : integer - how many neutral/non-directional.
  sentimentIndex     : integer -100..+100 - overall social sentiment from your
                       full-message read (negative = bearish).
  sentimentSummary   : 1-2 sentences - the real-time sentiment read.
  narrative          : 2-3 sentences - the dominant story/catalyst driving attention.
  divergenceVerdict  : one of CONFIRM / DIVERGE-BULLISH / DIVERGE-BEARISH / NEUTRAL
                       - does the chatter agree with the price tape? (Euphoric
                       chatter + falling price = DIVERGE-BEARISH = distribution.
                       Disgust + rising/stable price = DIVERGE-BULLISH = capitulation.)
  divergenceNote     : 1-2 sentences explaining the verdict vs the tape.
  bullCase           : 1-2 sentences - what the bulls in the chatter argue.
  bearCase           : 1-2 sentences - what the bears argue.
  catalysts          : array of 2-6 short strings - SPECIFIC upcoming events the
                       chatter references, each tagged with TYPE and IMPACT, e.g.
                       "Q2 earnings ~Aug 5 (Earnings, High)", "GTC keynote
                       (Industry, Med)", "lockup expiry (Corporate, Low)".
                       Types: Earnings / Corporate / Industry / Macro.
                       Think weeks-to-months out, not the day's headline.
                       [] if none clear.
  manipulationFlag   : CLEAN / CAUTION / HIGH-RISK - scan for pump-and-dump
                       language and coordinated promotion. Also weigh the
                       account-quality data: bot/pump accounts were already
                       filtered out, but a high bot_ratio (>0.25) or a large
                       junk_accounts_filtered count is itself evidence of a
                       coordinated spam surge - escalate the flag accordingly.
  manipulationNote   : 1 sentence on the manipulation read, citing bot_ratio
                       / junk_accounts_filtered when they drove the verdict.
  analystRead        : 1-2 sentences - synthesize the analyst consensus / recent
                       rating actions (use the provided analyst data + news).
  attentionRead      : 1-2 sentences - is retail attention building or fading?
                       Use the baseline deltas and watcher count as evidence.
  actionRead         : 2-3 sentences - the bottom-line measured take on what the
                       alt-data picture means for someone researching this stock.
                       Weigh disconfirming evidence as rigorously as confirming.
                       MUST end with an explicit conviction tag and a falsifiable
                       invalidation: "Conviction: High/Medium/Low; this read flips
                       if [specific event / price / data point]." """


def synthesize(alt, scores):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    s, price = alt["social"], alt.get("price", {})
    payload = {
        "ticker": alt["ticker"],
        "news_headlines": [f"[{n.get('category','General')}] {n['title']} ({n['source']})"
                           for n in alt["news"]],
        "stocktwits": {
            "watchers": s.get("watchers", 0),
            "messages_sampled": s.get("total", 0),
            "self_tagged_bull": s.get("bull", 0),
            "self_tagged_bear": s.get("bear", 0),
            # account-quality filter results — bot/pump accounts already
            # removed from the sample below; ratios kept for the manip read
            "junk_accounts_filtered": s.get("junk_filtered", 0),
            "bot_ratio": s.get("bot_ratio", 0.0),
            "low_credibility_kept": s.get("low_cred", 0),
            "messages": [m["body"] for m in s.get("messages", [])[:30]],
        },
        "reddit_posts": [f"r/{r['subreddit']}: {r['title']} ({r['score']}u/{r['comments']}c)"
                         for r in alt["reddit"]],
        "web_context": alt["web"][:3500],
        "price_tape": {
            "price":          price.get("price", 0),
            "change_1week":   price.get("chg_1w", 0),
            "change_1month":  price.get("chg_1m", 0),
            "volume_vs_avg":  price.get("vol_ratio", 0),
            "analyst_rating": price.get("analyst_rating", ""),
            "analyst_target": price.get("target_price", 0),
            "num_analysts":   price.get("num_analysts", 0),
        },
        "buzz_score":      scores["buzz"],
        "baseline_deltas": scores["deltas"] or "first run - no baseline yet",
        "computed_divergence": scores["divergence_raw"],
    }
    msg = f"{SYNTH_PROMPT}\n\nAlt-data for {alt['ticker']}:\n{json.dumps(payload, indent=1)}"
    print("  Sending alt-data to Claude for synthesis...")
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[{"role": "user", "content": msg}],
    )
    raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except Exception:
        # truncation salvage
        cut = raw.rfind("}")
        if cut > 0:
            try:
                return json.loads(raw[:cut + 1])
            except Exception:
                pass
        m = re.search(r"\{[\s\S]*\}", raw)
        return json.loads(m.group(0)) if m else {}


# ─── PDF ──────────────────────────────────────────────────────────────────────

def generate_altdata_pdf(ticker, alt, scores, synth):
    now   = datetime.now(ET)
    s     = alt["social"]
    price = alt.get("price", {})
    cov   = alt["coverage"]
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_page()

    # ── Header ──
    pdf.set_fill_color(12, 20, 48)
    pdf.rect(0, 0, 210, 30, "F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 23)
    pdf.set_xy(10, 4)
    pdf.cell(120, 12, _safe(ticker))
    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_xy(10, 16)
    pdf.cell(120, 5, "ALT-DATA INTELLIGENCE  -  Real-Time News, Social, Web & Tape")
    pdf.set_xy(10, 21)
    pdf.cell(120, 5, _safe(now.strftime("%B %d, %Y  -  %I:%M %p ET")))

    # Sentiment badge — from Claude's full-message index, raw tags as fallback
    sidx = synth.get("sentimentIndex")
    if sidx is None:
        sidx = scores["sentiment_raw"]
    if   sidx >=  40: brgb, blab = (39, 174, 96),  "BULLISH"
    elif sidx >=  15: brgb, blab = (90, 180, 110), "LEAN BULL"
    elif sidx >  -15: brgb, blab = (150, 150, 160),"MIXED"
    elif sidx >  -40: brgb, blab = (220, 130, 70), "LEAN BEAR"
    else:             brgb, blab = (192, 57, 43),  "BEARISH"
    pdf.set_fill_color(*brgb)
    pdf.rect(138, 5, 64, 20, "F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_xy(138, 7.5)
    pdf.cell(64, 8, _safe(f"SOCIAL: {blab}"), align="C")
    pdf.set_font("Helvetica", "", 6.3)
    pdf.set_xy(138, 16.5)
    pdf.cell(64, 5, _safe(f"Sentiment Index {sidx:+d}  /  Buzz {scores['buzz']}"), align="C")
    pdf.set_fill_color(255, 200, 0)
    pdf.rect(0, 30, 210, 1.2, "F")

    # ── Score strip — 4 measured metrics with deltas ──
    y = 35
    d = scores["deltas"]
    def _delta(key, suffix="%"):
        if not d or d.get(key) is None:
            return "baseline building"
        v = d[key]
        return (f"+{v}{suffix} vs avg" if v >= 0 else f"{v}{suffix} vs avg")
    div_raw = scores["divergence_raw"]
    cards = [
        ("BUZZ SCORE",      str(scores["buzz"]),         _delta("attention"),
         (39, 90, 150)),
        ("SENTIMENT INDEX", f"{sidx:+d}",                f"{s.get('total',0)} msgs scored",
         brgb),
        ("TAPE vs CHATTER", synth.get("divergenceVerdict", div_raw) or div_raw,
         (f"1wk {price.get('chg_1w',0):+.1f}%" if cov["price"] else "no price data"),
         (150, 70, 30) if "DIVERGE" in str(synth.get("divergenceVerdict", div_raw))
         else (39, 110, 80)),
        ("ATTENTION",       str(alt["attention"]),       _delta("news"),
         (90, 70, 130)),
    ]
    for i, (lbl, val, sub, rgb) in enumerate(cards):
        x = 10 + i * 47.5
        pdf.set_fill_color(*rgb)
        pdf.rect(x, y, 46, 19, "F")
        pdf.set_text_color(225, 232, 245)
        pdf.set_font("Helvetica", "", 5.8)
        pdf.set_xy(x + 1, y + 1.3)
        pdf.cell(44, 3.5, lbl, align="C")
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_xy(x + 1, y + 5)
        pdf.cell(44, 7, _safe(str(val))[:18], align="C")
        pdf.set_font("Helvetica", "", 5.6)
        pdf.set_xy(x + 1, y + 13)
        pdf.cell(44, 3.5, _safe(sub)[:30], align="C")

    # Coverage line (robustness — what fed this report)
    y = 57
    have = [k for k in ["news", "social", "reddit", "web", "price"] if cov.get(k)]
    miss = [k for k in ["news", "social", "reddit", "web", "price"] if not cov.get(k)]
    pdf.set_text_color(110, 116, 135)
    pdf.set_font("Helvetica", "", 6.3)
    pdf.set_xy(10, y)
    cov_txt = f"Data coverage: {', '.join(have) or 'none'}"
    if miss:
        cov_txt += f"   |   unavailable: {', '.join(miss)}"
    if d:
        cov_txt += f"   |   baseline: {d['runs']} prior run(s)"
    else:
        cov_txt += "   |   baseline: first run for this ticker"
    pdf.cell(0, 4, _safe(cov_txt))

    # ── AI synthesis sections ──
    y = 64
    def _section(label, text, hdr_rgb=(12, 20, 48)):
        nonlocal y
        txt = _safe(text or "").strip()
        if not txt:
            return
        if y > 250:
            pdf.add_page(); y = 16
        pdf.set_fill_color(*hdr_rgb)
        pdf.rect(10, y, 190, 5.3, "F")
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 7.3)
        pdf.set_xy(12, y + 1)
        pdf.cell(0, 3.5, _safe(label.upper()))
        y += 6.6
        pdf.set_text_color(35, 42, 66)
        pdf.set_font("Helvetica", "", 7.8)
        pdf.set_xy(12, y)
        pdf.multi_cell(186, 4.3, txt)
        y = pdf.get_y() + 2.6

    if synth:
        _section("Real-Time Sentiment Read", synth.get("sentimentSummary", ""))
        _section("Dominant Narrative / Catalyst", synth.get("narrative", ""))
        dv = synth.get("divergenceVerdict", "")
        dcol = (120, 35, 30) if "DIVERGE" in str(dv) else (22, 90, 55)
        _section(f"Chatter vs Tape  ({dv})", synth.get("divergenceNote", ""), dcol)
        _section("Bull Case (from the chatter)", synth.get("bullCase", ""), (22, 90, 55))
        _section("Bear Case (from the chatter)", synth.get("bearCase", ""), (120, 35, 30))
        cats = synth.get("catalysts", [])
        if cats:
            _section("Forward Catalysts", "  -  ".join(str(c) for c in cats), (60, 55, 10))
        _section("Analyst Consensus", synth.get("analystRead", ""))
        _section("Retail Attention Trend", synth.get("attentionRead", ""))
        mf = synth.get("manipulationFlag", "")
        mcol = (150, 30, 30) if mf == "HIGH-RISK" else \
               (160, 110, 20) if mf == "CAUTION" else (60, 90, 70)
        _section(f"Manipulation Scan  ({mf})", synth.get("manipulationNote", ""), mcol)
        _section("Bottom Line — Action Read", synth.get("actionRead", ""), (10, 40, 90))
    else:
        _section("AI Synthesis", "AI synthesis was unavailable for this run - "
                 "the raw data below is still current. Re-run shortly for the "
                 "synthesized read.", (130, 90, 10))

    # ── Price tape panel ──
    if cov["price"]:
        if y > 250:
            pdf.add_page(); y = 16
        pdf.set_fill_color(12, 20, 48)
        pdf.rect(10, y, 190, 5.3, "F")
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 7.3)
        pdf.set_xy(12, y + 1)
        pdf.cell(0, 3.5, "PRICE TAPE & ANALYST CONSENSUS")
        y += 7
        rating = price.get("analyst_rating", "") or "n/a"
        tgt    = price.get("target_price", 0)
        tgt_up = ((tgt / price["price"] - 1) * 100) if (tgt and price.get("price")) else None
        cells = [
            ("Price",        f"${price.get('price',0):.2f}"),
            ("1-Week",       f"{price.get('chg_1w',0):+.1f}%"),
            ("1-Month",      f"{price.get('chg_1m',0):+.1f}%"),
            ("Vol vs Avg",   f"{price.get('vol_ratio',0):.2f}x"),
            ("Analyst Rec",  rating.replace('_', ' ').title()),
            ("Mean Target",  (f"${tgt:.2f} ({tgt_up:+.0f}%)" if tgt_up is not None
                              else (f"${tgt:.2f}" if tgt else "n/a"))),
        ]
        pdf.set_font("Helvetica", "", 7)
        for i, (lbl, val) in enumerate(cells):
            x = 10 + (i % 6) * 31.7
            pdf.set_fill_color(244, 247, 252)
            pdf.set_xy(x, y)
            pdf.set_text_color(100, 108, 130)
            pdf.cell(31.5, 4.3, " " + lbl, fill=True)
            pdf.set_xy(x, y + 4.3)
            pdf.set_text_color(15, 22, 50)
            pdf.set_font("Helvetica", "B", 7.6)
            pdf.cell(31.5, 4.6, " " + _safe(val), fill=True)
            pdf.set_font("Helvetica", "", 7)
        y += 11

    # ── News table ──
    if alt["news"]:
        if y > 245:
            pdf.add_page(); y = 16
        pdf.set_fill_color(12, 20, 48)
        pdf.rect(10, y, 190, 5.3, "F")
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 7.3)
        pdf.set_xy(12, y + 1)
        pdf.cell(0, 3.5, f"NEWS FLOW  ({len(alt['news'])} stories, deduped & categorized)")
        y += 7
        cat_rgb = {
            "Earnings": (52, 100, 180), "Analyst": (140, 70, 160),
            "M&A": (192, 57, 43), "Regulatory": (190, 110, 20),
            "Product/Deal": (39, 130, 90), "Macro/Sector": (90, 100, 120),
            "General": (120, 124, 140),
        }
        for i, n in enumerate(alt["news"][:14]):
            if y > 286:
                pdf.add_page(); y = 16
            cat = n.get("category", "General")
            pdf.set_xy(10, y)
            pdf.set_fill_color(*cat_rgb.get(cat, (120, 124, 140)))
            pdf.set_text_color(255, 255, 255)
            pdf.set_font("Helvetica", "B", 5.5)
            pdf.cell(22, 5, cat[:11], fill=True, align="C")
            pdf.set_fill_color(244, 247, 252) if i % 2 == 0 else pdf.set_fill_color(255, 255, 255)
            pdf.set_text_color(30, 36, 60)
            pdf.set_font("Helvetica", "", 7.2)
            pdf.cell(132, 5, " " + _safe(n["title"])[:84], fill=True)
            pdf.set_font("Helvetica", "B", 6.3)
            pdf.set_text_color(95, 104, 132)
            pdf.cell(36, 5, _safe(n["source"])[:22] + " ", fill=True, align="R")
            y += 5

    # ── StockTwits stream ──
    msgs = s.get("messages", [])
    if msgs:
        if y > 250:
            pdf.add_page(); y = 16
        else:
            y += 4
        pdf.set_fill_color(12, 20, 48)
        pdf.rect(10, y, 190, 5.3, "F")
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 7.3)
        pdf.set_xy(12, y + 1)
        mb, mr, mn = (synth.get("messagesBull"), synth.get("messagesBear"),
                      synth.get("messagesNeutral"))
        tag = (f"  -  Claude scored: {mb} bull / {mr} bear / {mn} neutral"
               if mb is not None else "")
        pdf.cell(0, 3.5, _safe(f"STOCKTWITS SOCIAL STREAM ({len(msgs)} recent){tag}"))
        y += 7
        for m in msgs[:16]:
            if y > 288:
                pdf.add_page(); y = 16
            sent = m.get("sentiment", "")
            chip = (39, 174, 96) if sent == "Bullish" else \
                   (192, 57, 43) if sent == "Bearish" else (140, 140, 150)
            pdf.set_xy(10, y)
            pdf.set_fill_color(*chip)
            pdf.set_text_color(255, 255, 255)
            pdf.set_font("Helvetica", "B", 5.6)
            pdf.cell(14, 5, (sent[:4].upper() or "-"), fill=True, align="C")
            pdf.set_fill_color(248, 249, 252)
            pdf.set_text_color(40, 46, 66)
            pdf.set_font("Helvetica", "", 6.9)
            pdf.cell(176, 5, " " + _safe(m["body"])[:120], fill=True)
            y += 5

    # ── Reddit ──
    if alt["reddit"]:
        if y > 255:
            pdf.add_page(); y = 16
        else:
            y += 4
        pdf.set_fill_color(12, 20, 48)
        pdf.rect(10, y, 190, 5.3, "F")
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 7.3)
        pdf.set_xy(12, y + 1)
        pdf.cell(0, 3.5, f"REDDIT CHATTER ({len(alt['reddit'])} posts)")
        y += 7
        for r in alt["reddit"][:8]:
            if y > 288:
                pdf.add_page(); y = 16
            pdf.set_xy(10, y)
            pdf.set_fill_color(255, 200, 0)
            pdf.set_text_color(15, 22, 50)
            pdf.set_font("Helvetica", "B", 5.8)
            pdf.cell(30, 5, _safe("r/" + r["subreddit"])[:18], fill=True, align="C")
            pdf.set_fill_color(248, 249, 252)
            pdf.set_text_color(40, 46, 66)
            pdf.set_font("Helvetica", "", 6.9)
            pdf.cell(146, 5, " " + _safe(r["title"])[:96], fill=True)
            pdf.set_font("Helvetica", "B", 6.2)
            pdf.set_text_color(95, 104, 132)
            pdf.cell(14, 5, f"{r['score']}u ", fill=True, align="R")
            y += 5

    # Footer
    pdf.set_auto_page_break(auto=False)
    pdf.set_xy(10, 291)
    pdf.set_font("Helvetica", "I", 5.0)
    pdf.set_text_color(150, 150, 150)
    pdf.cell(0, 4, "Alt-data: Google News, StockTwits, Reddit, Nimble web extraction, "
             "yfinance tape. Not financial advice. Real-time chatter is noisy - "
             "corroborate before acting.", align="C")

    return bytes(pdf.output())


# ─── Main ─────────────────────────────────────────────────────────────────────

def run_altdata_lookup(ticker):
    ticker  = ticker.upper().strip()
    print(f"\n{'='*54}\nALT-DATA INTELLIGENCE: {ticker}\n{'='*54}")

    if not ANTHROPIC_API_KEY:
        print("  WARNING: ANTHROPIC_API_KEY not set — report will skip AI synthesis")

    print("[1/5] Gathering real-time alt-data...")
    alt = gather_alt_data(ticker)

    # Invalid-ticker guard — no data anywhere = bad symbol
    if not any(alt["coverage"].values()):
        print(f"  No data found for {ticker} — likely an invalid ticker. Aborting.")
        return

    print("[2/5] Computing baseline + scores...")
    hist     = load_history()
    baseline = compute_baseline(hist, ticker)
    alt["news"] = categorize_news(alt["news"])
    scores   = compute_scores(alt, baseline)
    print(f"  Buzz {scores['buzz']}  |  Sentiment(raw) {scores['sentiment_raw']:+d}  |  "
          f"Divergence {scores['divergence_raw']}  |  baseline runs: "
          f"{baseline['runs'] if baseline else 0}")

    print("[3/5] Claude synthesis...")
    synth = {}
    if ANTHROPIC_API_KEY:
        try:
            synth = synthesize(alt, scores)
        except Exception as e:
            print(f"  Synthesis failed (report will still generate): {e}")

    print("[4/5] Generating PDF + updating history...")
    try:
        pdf_bytes = generate_altdata_pdf(ticker, alt, scores, synth)
    except Exception as e:
        print(f"  PDF generation failed: {e}")
        return

    # Append this run to history
    try:
        hist.setdefault(ticker, []).append({
            "ts":        datetime.now(timezone.utc).isoformat(timespec="minutes"),
            "news":      len(alt["news"]),
            "social":    alt["social"].get("total", 0),
            "attention": alt["attention"],
            "watchers":  alt["social"].get("watchers", 0),
            "buzz":      scores["buzz"],
            "sentiment": synth.get("sentimentIndex", scores["sentiment_raw"]),
        })
        hist[ticker] = hist[ticker][-20:]   # keep last 20 runs per ticker
        save_history(hist)
    except Exception as e:
        print(f"  history update failed: {e}")

    print("[5/5] Archiving to site...")
    now      = datetime.now(ET)
    filename = f"altdata_{ticker}_{now.strftime('%Y-%m-%d_%H%M')}.pdf"
    try:
        from report_archive import archive
        archive(pdf_bytes, filename)
        print(f"  Archived: {filename}")
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
