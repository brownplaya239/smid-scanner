#!/usr/bin/env python3
"""macro_news_relay.py — relay Finnhub's macro/general news wire into
Supabase news_live.

WHY A RELAY: the site's news tape is fed by Polygon's company-news API,
which carries no commodities/Fed/geopolitics coverage at all (the live
firehose is press releases + retail commentary). Finnhub's general
feed fills that hole, but Finnhub rejects requests from Cloudflare
Workers egress IPs (401 with a valid key), so the worker cannot proxy
it. GitHub Actions IPs work — this script runs on a short cron there
and upserts the headlines into the same news_live table the tape
already merges every 15 seconds. No new frontend path required.

Secrets (CI only, never local): FINNHUB_API_KEY, SUPABASE_URL,
SUPABASE_SERVICE_KEY.
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

MAX_ITEMS = 40
MAX_AGE_H = 26


def fetch_finnhub():
    """Finnhub /news is NOT included in the current key's plan (401
    from every IP — the estimates endpoints are what the plan covers).
    Tried first anyway: if the plan is ever upgraded this path starts
    working with no code change. Returns [] on any failure."""
    key = (os.environ.get("FINNHUB_API_KEY") or "").strip()
    if not key:
        return []
    try:
        req = urllib.request.Request(
            "https://finnhub.io/api/v1/news?category=general",
            headers={"X-Finnhub-Token": key,
                     "Accept": "application/json",
                     "User-Agent": "TickerDesk-MacroRelay/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            out = json.loads(r.read().decode("utf-8"))
            return out if isinstance(out, list) else []
    except Exception as e:
        print("  finnhub unavailable (%s) — falling back to RSS wires"
              % e)
        return []


# Public macro/market RSS wires — the sources that actually carry the
# "Brent falls 5%" / "US-Iran talks" tape. Parsed with stdlib only.
RSS_FEEDS = (
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("MarketWatch",
     "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("MarketWatch",
     "https://feeds.content.dowjones.io/public/rss/mw_marketpulse"),
    ("Google News · Markets",
     "https://news.google.com/rss/search?q=markets%20OR%20oil%20OR%20"
     "fed%20OR%20treasury%20OR%20opec%20when:1d&hl=en-US&gl=US"
     "&ceid=US:en"),
)


def fetch_rss():
    """-> list of Finnhub-shaped article dicts from the RSS wires."""
    import email.utils
    import hashlib
    import xml.etree.ElementTree as ET
    out = []
    for name, url in RSS_FEEDS:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "TickerDesk-MacroRelay/1.0",
                              "Accept": "application/rss+xml, text/xml"})
            with urllib.request.urlopen(req, timeout=20) as r:
                root = ET.fromstring(r.read())
        except Exception as e:
            print("  rss %s failed (non-fatal): %s" % (name, e))
            continue
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not title:
                continue
            ts = None
            pd = item.findtext("pubDate")
            if pd:
                try:
                    ts = int(email.utils.parsedate_to_datetime(pd)
                             .timestamp())
                except Exception:
                    ts = None
            out.append({
                "id": hashlib.sha1((link or title).encode()
                                   ).hexdigest()[:16],
                "headline": title,
                "summary": (item.findtext("description") or "")[:400],
                "source": name,
                "url": link,
                "datetime": ts,
                "related": "",
            })
    # newest first, dedupe by normalized title
    seen, uniq = set(), []
    for a in sorted(out, key=lambda x: -(x.get("datetime") or 0)):
        k = "".join(ch for ch in a["headline"].lower() if ch.isalnum())
        if k in seen:
            continue
        seen.add(k)
        uniq.append(a)
    return uniq


def to_row(a):
    if not a or not a.get("headline"):
        return None
    ts = a.get("datetime")
    published = (datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                 if ts else None)
    if published:
        age = datetime.now(timezone.utc) - datetime.fromtimestamp(
            ts, tz=timezone.utc)
        if age > timedelta(hours=MAX_AGE_H):
            return None
    syms = [s for s in str(a.get("related") or "").split(",") if s][:6]
    return {
        "id": "fh-%s" % (a.get("id") or ts or a.get("url")),
        "headline": str(a["headline"])[:500],
        "summary": str(a.get("summary") or "")[:1000],
        "source": str(a.get("source") or "Finnhub")[:100],
        "url": str(a.get("url") or "")[:1000],
        "symbols": syms,
        "published_at": published,
    }


def upsert(rows):
    base = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    svc = os.environ.get("SUPABASE_SERVICE_KEY") or ""
    if not base or not svc:
        sys.exit("SUPABASE_URL / SUPABASE_SERVICE_KEY not set")
    body = json.dumps(rows).encode("utf-8")
    req = urllib.request.Request(
        base + "/rest/v1/news_live?on_conflict=id",
        data=body, method="POST",
        headers={"apikey": svc, "Authorization": "Bearer " + svc,
                 "Content-Type": "application/json",
                 "Prefer": "resolution=ignore-duplicates,return=minimal"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def main():
    arts = fetch_finnhub() or fetch_rss()
    rows = [r for r in (to_row(a) for a in arts[: MAX_ITEMS * 3])
            if r][:MAX_ITEMS]
    if not rows:
        print("no fresh macro headlines in the window — nothing to relay")
        return 0
    status = upsert(rows)
    print("relayed %d macro headlines (HTTP %s); newest: %r"
          % (len(rows), status, rows[0]["headline"][:80]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
