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

def _account_quality(user):
    """Heuristic credibility for a StockTwits account — pump-and-dump rings run
    swarms of brand-new, zero-follower, zero-history accounts to fake a buzz.
    Returns one of 'ok' | 'low' | 'junk'.

      followers  : audience size
      ideas      : lifetime post count (real users accumulate history)
      join_date  : account age — fresh accounts are the classic bot tell
      official   : verified company/brand account
    """
    followers = user.get("followers", 0) or 0
    ideas     = user.get("ideas", 0) or 0
    official  = bool(user.get("official"))
    join      = (user.get("join_date", "") or "")[:10]
    age_days  = None
    if join:
        try:
            age_days = (datetime.utcnow().date()
                        - datetime.strptime(join, "%Y-%m-%d").date()).days
        except Exception:
            pass
    # established / real account — keep at full weight
    if official or followers >= 75 or ideas >= 300:
        return "ok"
    # hard junk: no audience AND negligible history AND (young or unknown age)
    young = age_days is None or age_days < 45
    if followers < 8 and ideas < 25 and young:
        return "junk"
    # thin but not obviously fake — keep, but flag as low-credibility
    if followers < 25 and ideas < 100:
        return "low"
    return "ok"


def fetch_stocktwits(ticker, max_msgs=30):
    """StockTwits stream — recent messages with built-in Bullish/Bearish tags.

    StockTwits blocks datacenter IPs / the `requests` TLS fingerprint, so the
    primary path routes the API call through Nimble's residential proxy
    (/extract). Direct request is the fallback.

    Account-quality filter: messages from likely bot / pump accounts (brand-new,
    zero-follower, zero-history) are dropped from the sample and the bull/bear
    tally so they don't poison the sentiment read. The share of dropped junk is
    returned as `bot_ratio` — a high ratio is itself a manipulation signal."""
    out = {"messages": [], "bull": 0, "bear": 0, "tagged": 0, "total": 0,
           "watchers": 0, "available": False,
           "junk_filtered": 0, "bot_ratio": 0.0, "low_cred": 0}
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
    seen_total = 0
    for m in data.get("messages", []):
        if len(out["messages"]) >= max_msgs:
            break
        seen_total += 1
        user = m.get("user") or {}
        quality = _account_quality(user)
        if quality == "junk":
            out["junk_filtered"] += 1
            continue                      # drop bot/pump account entirely
        if quality == "low":
            out["low_cred"] += 1
        ent  = (m.get("entities") or {}).get("sentiment") or {}
        sent = ent.get("basic", "") or ""
        if sent == "Bullish":   out["bull"] += 1; out["tagged"] += 1
        elif sent == "Bearish": out["bear"] += 1; out["tagged"] += 1
        out["messages"].append({
            "body":      (m.get("body", "") or "")[:240],
            "sentiment": sent,
            "created":   m.get("created_at", ""),
            "user":      user.get("username", ""),
            "followers": user.get("followers", 0) or 0,
            "quality":   quality,
        })
    out["total"] = len(out["messages"])
    scanned = seen_total or 1
    out["bot_ratio"] = round(out["junk_filtered"] / scanned, 2)
    return out


def fetch_reddit(ticker, max_items=10):
    """Recent Reddit mentions. Routes through Nimble /extract (Reddit blocks
    datacenter IPs — direct fetch fails from CI); direct request is fallback."""
    items = []
    url = (f"https://www.reddit.com/search.json?q=%24{ticker}"
           f"&sort=new&limit={max_items * 3}&t=month")

    raw = nimble_extract(url, fmt="html").strip()
    if not raw or not raw.startswith("{"):
        try:
            r = requests.get(url, headers={"User-Agent": "smid-scanner/1.0 alt-data"},
                             timeout=25)
            if r.ok:
                raw = r.text
        except Exception as e:
            print(f"  reddit direct fallback failed: {e}")

    if not raw or not raw.startswith("{"):
        return items
    try:
        data = json.loads(raw)
    except Exception:
        return items

    for c in data.get("data", {}).get("children", []):
        d = c.get("data", {})
        title = (d.get("title", "") or "").strip()
        low   = title.lower()
        # skip obvious self-promo / spam
        if not title or low.startswith(("start for free", "free ", "join ")) \
           or "promo" in low or ".io" in low[:25]:
            continue
        items.append({
            "title":     title[:200],
            "subreddit": d.get("subreddit", ""),
            "score":     d.get("score", 0),
            "comments":  d.get("num_comments", 0),
            "created":   d.get("created_utc", 0),
            "url":       "https://reddit.com" + d.get("permalink", ""),
        })
        if len(items) >= max_items:
            break
    return items


# ─── Price context — for sentiment-vs-tape divergence ─────────────────────────

def fetch_price_context(ticker):
    """yfinance price action + analyst consensus — used to flag whether the
    chatter CONFIRMS or DIVERGES from the actual tape."""
    out = {"price": 0.0, "chg_1w": 0.0, "chg_1m": 0.0, "vol_ratio": 0.0,
           "analyst_rating": "", "target_price": 0.0, "num_analysts": 0,
           "price_available": False}
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        h = t.history(period="2mo", interval="1d")
        if not h.empty and len(h) >= 6:
            close = h["Close"]
            price = float(close.iloc[-1])
            out["price"]  = round(price, 2)
            out["chg_1w"] = round((price / float(close.iloc[-6]) - 1) * 100, 1)
            if len(close) >= 22:
                out["chg_1m"] = round((price / float(close.iloc[-22]) - 1) * 100, 1)
            avgv = float(h["Volume"].iloc[-21:-1].mean())
            if avgv > 0:
                out["vol_ratio"] = round(float(h["Volume"].iloc[-1]) / avgv, 2)
            out["price_available"] = True
        try:
            info = t.info
            out["analyst_rating"] = info.get("recommendationKey", "") or ""
            out["target_price"]   = round(info.get("targetMeanPrice", 0) or 0, 2)
            out["num_analysts"]   = info.get("numberOfAnalystOpinions", 0) or 0
        except Exception:
            pass
    except Exception as e:
        print(f"  price context failed: {e}")
    return out


def _dedup_news(items):
    """Drop near-duplicate headlines (same story across outlets)."""
    seen, out = set(), []
    for n in items:
        words = re.sub(r"[^a-z0-9 ]", "", n["title"].lower()).split()
        key = " ".join(words[:6])
        if key and key not in seen:
            seen.add(key)
            out.append(n)
    return out


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
    """Run every signal for a ticker. Each source is independent — a failure
    in one degrades gracefully, the rest still populate the report."""
    ticker = ticker.upper().strip()
    print(f"  Alt-data for {ticker}...")

    def _safe_call(fn, default):
        try:
            return fn()
        except Exception as e:
            print(f"  {fn} failed: {e}")
            return default

    news   = _dedup_news(_safe_call(lambda: fetch_news(ticker, company), []))
    social = _safe_call(lambda: fetch_stocktwits(ticker),
                        {"messages": [], "bull": 0, "bear": 0, "total": 0,
                         "watchers": 0, "available": False})
    reddit = _safe_call(lambda: fetch_reddit(ticker), [])
    web    = _safe_call(lambda: fetch_web_context(ticker, company), "")
    price  = _safe_call(lambda: fetch_price_context(ticker),
                        {"price_available": False})

    attention = len(news) + social.get("total", 0) + len(reddit)
    coverage  = {
        "news":   bool(news),
        "social": social.get("available", False) and social.get("total", 0) > 0,
        "reddit": bool(reddit),
        "web":    bool(web),
        "price":  price.get("price_available", False),
    }
    print(f"    news={len(news)}  stocktwits={social.get('total',0)} "
          f"(bull {social.get('bull',0)}/bear {social.get('bear',0)}; "
          f"junk-filtered {social.get('junk_filtered',0)}, "
          f"bot-ratio {social.get('bot_ratio',0)})  "
          f"reddit={len(reddit)}  web_ctx={'yes' if web else 'no'}  "
          f"price={'yes' if coverage['price'] else 'no'}")

    return {
        "ticker":    ticker,
        "news":      news,
        "social":    social,
        "reddit":    reddit,
        "web":       web,
        "price":     price,
        "attention": attention,
        "coverage":  coverage,
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
