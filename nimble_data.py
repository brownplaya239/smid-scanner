"""
nimble_data.py — Real-time alt-data for a ticker.

  news     : Google News RSS (clean, structured, free)
  social   : StockTwits public API (messages + built-in bull/bear tags) + Reddit
  web/SERP : Nimble /extract on a search-results page (residential proxy + JS render)
  attention: derived from news + social mention volume

Nimble's /extract endpoint is what the account key has access to (/search is
enterprise-gated). /extract turns any URL into clean markdown via residential
proxies — used here for the SERP/web-context signal and as a resilient fetcher.
"""

import os
import re
import json
import time
import requests
import xml.etree.ElementTree as ET
from urllib.parse import quote
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except Exception:
    pass

NIMBLE_EXTRACT = "https://sdk.nimbleway.com/v1/extract"


def _nimble_key():
    """Read the key fresh each call — avoids import-order vs load_dotenv() bugs."""
    return os.environ.get("NIMBLE_API_KEY", "")

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


# ─── Nimble /extract ──────────────────────────────────────────────────────────

def nimble_extract(url, country="US", retries=2, fmt="markdown"):
    """Fetch a URL via Nimble's residential-proxy extractor.
    fmt='markdown' for web pages; fmt='html' for raw bodies (e.g. API JSON —
    markdown conversion mangles JSON, html returns it verbatim).
    Retries — /extract occasionally returns an empty body on the first attempt."""
    key = _nimble_key()
    if not key:
        print("  NIMBLE_API_KEY not set — skipping Nimble extract")
        return ""
    for attempt in range(retries + 1):
        try:
            r = requests.post(
                NIMBLE_EXTRACT,
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json={"url": url, "formats": [fmt], "country": country},
                timeout=90,
            )
            if r.ok:
                body = r.json().get("data", {}).get(fmt, "") or ""
                if body.strip():
                    return body
            else:
                print(f"  nimble_extract {r.status_code}: {r.text[:120]}")
        except Exception as e:
            print(f"  nimble_extract error: {e}")
        if attempt < retries:
            time.sleep(2 * (attempt + 1))
    return ""


def _unescape_markdown_json(md):
    """Nimble's markdown converter escapes JSON special chars (\\_ \\[ \\]).
    Strip those so an extracted API JSON body parses cleanly."""
    return re.sub(r"\\([_\[\]*`])", r"\1", md or "").strip()


# ─── News flow — Google News RSS ──────────────────────────────────────────────

def fetch_news(ticker, company="", max_items=12):
    q = f"{ticker} {company} stock".strip()
    url = (f"https://news.google.com/rss/search?q={quote(q)}"
           f"&hl=en-US&gl=US&ceid=US:en")
    items = []
    try:
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=25)
        if r.ok:
            root = ET.fromstring(r.content)
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                link  = (it.findtext("link") or "").strip()
                pub   = (it.findtext("pubDate") or "").strip()
                src_el = it.find("{*}source")
                src = src_el.text.strip() if src_el is not None and src_el.text else ""
                if title:
                    items.append({"title": title, "link": link, "date": pub, "source": src})
                if len(items) >= max_items:
                    break
    except Exception as e:
        print(f"  news fetch failed: {e}")
    return items


# ─── Social sentiment — StockTwits + Reddit ───────────────────────────────────

def fetch_stocktwits(ticker, max_msgs=30):
    """StockTwits stream — recent messages with built-in Bullish/Bearish tags.

    StockTwits blocks datacenter IPs / the `requests` TLS fingerprint, so the
    primary path routes the API call through Nimble's residential proxy
    (/extract). Direct request is the fallback."""
    out = {"messages": [], "bull": 0, "bear": 0, "tagged": 0, "total": 0,
           "watchers": 0, "available": False}
    api_url = f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"

    # Primary: Nimble /extract with fmt='html' returns the raw JSON body verbatim.
    raw = nimble_extract(api_url, fmt="html").strip()
    if not raw or not raw.startswith("{"):
        # fallback: direct (works from some IPs)
        try:
            r = requests.get(api_url, headers={"User-Agent": _UA}, timeout=25)
            if r.ok:
                raw = r.text
        except Exception as e:
            print(f"  stocktwits direct fallback failed: {e}")

    if not raw or not raw.startswith("{"):
        return out
    try:
        data = json.loads(raw)
    except Exception as e:
        print(f"  stocktwits JSON parse failed: {e}")
        return out

    out["available"] = True
    out["watchers"]  = data.get("symbol", {}).get("watchlist_count", 0)
    for m in data.get("messages", [])[:max_msgs]:
        ent  = (m.get("entities") or {}).get("sentiment") or {}
        sent = ent.get("basic", "") or ""
        if sent == "Bullish":   out["bull"] += 1; out["tagged"] += 1
        elif sent == "Bearish": out["bear"] += 1; out["tagged"] += 1
        out["messages"].append({
            "body":      (m.get("body", "") or "")[:240],
            "sentiment": sent,
            "created":   m.get("created_at", ""),
            "user":      (m.get("user") or {}).get("username", ""),
        })
    out["total"] = len(out["messages"])
    return out


def fetch_reddit(ticker, max_items=10):
    """Recent Reddit mentions. Tries the public JSON; falls back to Nimble /extract."""
    items = []
    url = (f"https://www.reddit.com/search.json?q=%24{ticker}"
           f"&sort=new&limit={max_items}&t=week")
    try:
        r = requests.get(url, headers={"User-Agent": "smid-scanner/1.0 alt-data"}, timeout=25)
        if r.ok:
            for c in r.json().get("data", {}).get("children", []):
                d = c.get("data", {})
                items.append({
                    "title":     (d.get("title", "") or "")[:200],
                    "subreddit": d.get("subreddit", ""),
                    "score":     d.get("score", 0),
                    "comments":  d.get("num_comments", 0),
                    "url":       "https://reddit.com" + d.get("permalink", ""),
                })
    except Exception as e:
        print(f"  reddit fetch failed: {e}")
    return items


# ─── Web / SERP context — Nimble /extract ─────────────────────────────────────

def fetch_web_context(ticker, company="", max_chars=6000):
    """Web/SERP surface via Nimble /extract on Bing News for the ticker.
    Markdown is noisy (nav chrome) but Claude pulls the real headlines from it.
    This is the Nimble-key-powered signal (residential proxy + JS render)."""
    q = f"{ticker} {company} stock".strip()
    url = f"https://www.bing.com/news/search?q={quote(q)}"
    md = nimble_extract(url)
    return md[:max_chars]


# ─── Aggregate ────────────────────────────────────────────────────────────────

def gather_alt_data(ticker, company=""):
    """Run all four signals for a ticker. Returns a dict for the report + Claude."""
    ticker = ticker.upper().strip()
    print(f"  Alt-data for {ticker}...")
    news   = fetch_news(ticker, company)
    social = fetch_stocktwits(ticker)
    reddit = fetch_reddit(ticker)
    web    = fetch_web_context(ticker, company)

    # Attention proxy — mention volume across sources
    attention = len(news) + social.get("total", 0) + len(reddit)

    print(f"    news={len(news)}  stocktwits={social.get('total',0)} "
          f"(bull {social.get('bull',0)}/bear {social.get('bear',0)})  "
          f"reddit={len(reddit)}  web_ctx={'yes' if web else 'no'}")

    return {
        "ticker":    ticker,
        "news":      news,
        "social":    social,
        "reddit":    reddit,
        "web":       web,
        "attention": attention,
    }


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from dotenv import load_dotenv
    load_dotenv()
    tk = sys.argv[1] if len(sys.argv) > 1 else "NVDA"
    d = gather_alt_data(tk, "Nvidia")
    _a = lambda s: re.sub(r"[^\x00-\x7F]", "", str(s))
    print(f"\n=== {tk} ===")
    print(f"News ({len(d['news'])}):")
    for n in d["news"][:5]:
        print(f"  - {_a(n['title'])[:80]}  [{_a(n['source'])}]")
    s = d["social"]
    print(f"StockTwits: {s['total']} msgs, {s['bull']} bull / {s['bear']} bear, "
          f"{s.get('watchers',0):,} watchers")
    for m in s["messages"][:3]:
        print(f"  [{m['sentiment'] or '-'}] {_a(m['body'])[:80]}")
    print(f"Reddit ({len(d['reddit'])}):")
    for rd in d["reddit"][:3]:
        print(f"  - r/{_a(rd['subreddit'])} ({rd['score']}u/{rd['comments']}c) {_a(rd['title'])[:60]}")
    print(f"Web context: {len(d['web'])} chars extracted via Nimble")
