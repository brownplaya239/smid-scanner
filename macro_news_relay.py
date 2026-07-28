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
    key = (os.environ.get("FINNHUB_API_KEY") or "").strip()
    if not key:
        sys.exit("FINNHUB_API_KEY not set — this relay is CI-only")
    req = urllib.request.Request(
        "https://finnhub.io/api/v1/news?category=general",
        headers={"X-Finnhub-Token": key, "Accept": "application/json",
                 "User-Agent": "TickerDesk-MacroRelay/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


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
    arts = fetch_finnhub()
    if not isinstance(arts, list):
        sys.exit("unexpected Finnhub response shape: %r" % type(arts))
    rows = [r for r in (to_row(a) for a in arts[: MAX_ITEMS * 2])
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
