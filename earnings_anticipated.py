"""
earnings_anticipated.py — Scrape earningswhispers.com Most Anticipated weekly
calendar into a JSON the dashboard renders as a clean clickable grid.

Replaces the manual-tweet-ID workflow with fully-automated data pulled
directly from EW's own backend (the same JSON their /calendar page
consumes via jQuery). Five weekdays of "this week," both sessions
(BMO = before market open, AMC = after market close), ranked by their
"total" anticipation score (NVDA ≈ 761 on report day, ADI ≈ 48).

The dashboard ticker cells use data-t for drilldown so anything on the
chart becomes a single click into our universal panel — grade, flow,
news, SEC filings, the works.

Endpoint: GET /api/quickcaldata/{YYYYMMDD}/{rt}
  rt=1 BMO  rt=3 AMC
  Returns: [{ticker, company, total, analysts, releaseTime,
              confirmDate, ...}]
  Requires Referer + X-Requested-With headers (else returns 401).

Output: docs/reports/earnings_anticipated.json
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta

import pytz

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ET = pytz.timezone("America/New_York")
_BASE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(_BASE, "docs", "reports",
                        "earnings_anticipated.json")

# EW's API requires browser-like headers; without Referer+XHR header it
# returns 401 even for public data.
HDRS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0 Safari/537.36"),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": "https://www.earningswhispers.com/calendar",
    "X-Requested-With": "XMLHttpRequest",
}


def _fetch(path):
    url = "https://www.earningswhispers.com" + path
    req = urllib.request.Request(url, headers=HDRS)
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode("utf-8")
    if not body.strip():
        return []
    return json.loads(body)


def _shape(r):
    """Trim EW's per-ticker entry to just what the dashboard renders."""
    return {
        "ticker":       r.get("ticker"),
        "company":      r.get("company"),
        "score":        r.get("total"),
        "analysts":     r.get("analysts"),
        "release_time": r.get("releaseTime"),     # 1=BMO, 3=AMC
        "confirmed":    bool(r.get("confirmDate")),
        "next_eps":     r.get("nextEPSDate"),
        "q":            r.get("qDate"),
    }


def _week_monday(d):
    """Monday of the trading week a reader cares about. On Saturday or
    Sunday that is NEXT week — EW purges the finished week from its API
    over the weekend, so scraping the week containing a Saturday returns
    five days of zeros and (before the empty-write guard below) clobbered
    the file the dashboard renders. The 2026-07-26 Saturday run did
    exactly that."""
    if d.weekday() >= 5:                          # Sat/Sun -> next Monday
        return d + timedelta(days=7 - d.weekday())
    return d - timedelta(days=d.weekday())


def run():
    today = datetime.now(ET).date()
    monday = _week_monday(today)
    print(f"Scraping Earnings Whisper week of {monday}...")
    days = []
    for i in range(5):                           # Mon-Fri only
        d = monday + timedelta(days=i)
        ds = d.strftime("%Y%m%d")
        try:
            bmo = _fetch(f"/api/quickcaldata/{ds}/1") or []
        except Exception as e:
            print(f"  {d} BMO failed: {e}")
            bmo = []
        try:
            amc = _fetch(f"/api/quickcaldata/{ds}/3") or []
        except Exception as e:
            print(f"  {d} AMC failed: {e}")
            amc = []
        bmo.sort(key=lambda x: x.get("total", 0) or 0, reverse=True)
        amc.sort(key=lambda x: x.get("total", 0) or 0, reverse=True)
        bmo_s = [_shape(r) for r in bmo]
        amc_s = [_shape(r) for r in amc]
        days.append({
            "date": d.strftime("%Y-%m-%d"),
            "dow":  d.strftime("%A"),
            "bmo":  bmo_s,
            "amc":  amc_s,
        })
        print(f"  {d.strftime('%a %Y-%m-%d')}  BMO={len(bmo_s):3d}  AMC={len(amc_s):3d}")

    total = sum(len(x["bmo"]) + len(x["amc"]) for x in days)
    payload = {
        "week_of":   monday.strftime("%Y-%m-%d"),
        "generated": datetime.now(ET).isoformat(timespec="seconds"),
        "source":    "earningswhispers.com (Most Anticipated calendar)",
        "total":     total,
        "days":      days,
    }
    # Last-good guard: a scrape that found NOBODY reporting all week is a
    # broken scrape or a purged source, not a market fact — never replace
    # a populated file with it. The stale file at least says when it was
    # generated; five days of dashes says nothing.
    if total == 0:
        try:
            with open(OUT_PATH, encoding="utf-8") as f:
                prev = json.load(f)
        except Exception:
            prev = None
        if prev and prev.get("total"):
            print("  Scrape returned 0 reporters — keeping the previous "
                  "file (week of %s, %s reporters) instead of writing an "
                  "empty one" % (prev.get("week_of"), prev.get("total")))
            return

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    print(f"  Wrote earnings_anticipated.json ({total} reporters across "
          f"5 trading days)")


if __name__ == "__main__":
    run()
