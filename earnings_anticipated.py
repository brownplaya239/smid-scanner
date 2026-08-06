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

try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except Exception:
    pass

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


def _finnhub_sessions(monday):
    """Finnhub's earnings calendar for the week -> {SYMBOL: 'bmo'|'amc'},
    used to arbitrate the report session. EW's quickcaldata buckets are
    wrong often enough to matter (2026-07-27: it listed Noble Corp as
    before-open; NE reported after the close and fell 4% AH while sitting
    in the site's morning column). Finnhub sources the hour from company
    confirmations. Keyless runs return {} and EW stands unchallenged."""
    key = os.environ.get("FINNHUB_API_KEY", "")
    if not key:
        return {}
    friday = monday + timedelta(days=4)
    url = ("https://finnhub.io/api/v1/calendar/earnings?from=%s&to=%s"
           "&token=%s" % (monday, friday, key))
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  Finnhub session cross-check unavailable: {e}")
        return {}
    out = {}
    for e in data.get("earningsCalendar") or []:
        sym, hour = e.get("symbol"), (e.get("hour") or "").lower()
        if sym and hour in ("bmo", "amc"):
            out[(sym.upper(), e.get("date"))] = hour
    return out


MCAP_CACHE = os.path.join(_BASE, "data", "earnings_mcap_cache.json")
MCAP_TTL_DAYS = 7


def _market_caps(tickers):
    """Polygon market cap per ticker, behind a committed 7-day cache so a
    scrape only fetches names it has not seen this week. Caps drive the
    calendar sort; a name Polygon does not know sorts last with mcap
    None rather than being dropped."""
    try:
        with open(MCAP_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:
        cache = {}
    now = datetime.now(ET).timestamp()
    out, fetched = {}, 0
    try:
        import polygon_data as PG
        ok = PG.available()
    except Exception:
        ok = False
    for t in tickers:
        ent = cache.get(t)
        if ent and now - ent.get("ts", 0) < MCAP_TTL_DAYS * 86400:
            out[t] = ent.get("mcap")
            continue
        if not ok:
            out[t] = (ent or {}).get("mcap")
            continue
        try:
            det = PG.ticker_details(t) or {}
            mc = det.get("market_cap")
        except Exception:
            mc = (ent or {}).get("mcap")
        out[t] = mc
        cache[t] = {"mcap": mc, "ts": now}
        fetched += 1
    try:
        os.makedirs(os.path.dirname(MCAP_CACHE), exist_ok=True)
        with open(MCAP_CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except Exception:
        pass
    if fetched:
        print(f"  Market caps: {fetched} fetched, "
              f"{len(tickers) - fetched} cached")
    return out


def run():
    today = datetime.now(ET).date()
    monday = _week_monday(today)
    print(f"Scraping Earnings Whisper week of {monday}...")
    fh = _finnhub_sessions(monday)
    if fh:
        print(f"  Finnhub session cross-check: {len(fh)} confirmed hours")
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
        bmo_s = [_shape(r) for r in bmo]
        amc_s = [_shape(r) for r in amc]
        # Re-bucket where Finnhub's confirmed hour disagrees with EW.
        if fh:
            dstr_iso = d.strftime("%Y-%m-%d")
            moved = []
            keep_b, keep_a = [], []
            for r in bmo_s:
                hour = fh.get(((r.get("ticker") or "").upper(), dstr_iso))
                if hour == "amc":
                    r["session_corrected"] = "EW listed BMO; Finnhub AMC"
                    keep_a.append(r)
                    moved.append(r["ticker"] + "->AMC")
                else:
                    keep_b.append(r)
            for r in amc_s:
                hour = fh.get(((r.get("ticker") or "").upper(), dstr_iso))
                if hour == "bmo":
                    r["session_corrected"] = "EW listed AMC; Finnhub BMO"
                    keep_b.append(r)
                    moved.append(r["ticker"] + "->BMO")
                else:
                    keep_a.append(r)
            bmo_s, amc_s = keep_b, keep_a
            if moved:
                print(f"  {d} session corrections: {', '.join(moved)}")
        days.append({
            "date": d.strftime("%Y-%m-%d"),
            "dow":  d.strftime("%A"),
            "bmo":  bmo_s,
            "amc":  amc_s,
        })
        print(f"  {d.strftime('%a %Y-%m-%d')}  BMO={len(bmo_s):3d}  AMC={len(amc_s):3d}")

    # Past days keep their report-day rows. EW's calendar API deletes a
    # company the moment it has reported, so a mid-week re-scrape
    # retroactively guts finished days — Wednesday's run returned Monday
    # as 3 stragglers because PLTR, MAR, TSN and everyone else who
    # printed had vanished from the source. The calendar should read as
    # a record of the week: for days already behind us, the previously
    # captured rows win and the fresh scrape can only add names, never
    # remove them. A new week starts clean — week_of no longer matches.
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            prior = json.load(f)
    except Exception:
        prior = None
    if prior and prior.get("week_of") == monday.strftime("%Y-%m-%d"):
        prior_days = {d.get("date"): d for d in (prior.get("days") or [])}
        today_iso = today.strftime("%Y-%m-%d")
        for day in days:
            pd = prior_days.get(day["date"])
            if day["date"] >= today_iso or not pd:
                continue
            kept = {r.get("ticker")
                    for k in ("bmo", "amc") for r in (pd.get(k) or [])}
            for k in ("bmo", "amc"):
                day[k] = list(pd.get(k) or []) + \
                    [r for r in day[k] if r.get("ticker") not in kept]
            print(f"  {day['date']} is past — kept {len(kept)} "
                  "report-day rows over the re-scrape")

    # Sort every session largest company first. Anticipation score stays
    # on the row (tooltip + marquee); the ordering a reader scans a
    # calendar in is by size.
    all_tk = sorted({r["ticker"] for day in days
                     for k in ("bmo", "amc") for r in day[k]
                     if r.get("ticker")})
    caps = _market_caps(all_tk)
    for day in days:
        for k in ("bmo", "amc"):
            for r in day[k]:
                # `or` keeps a preserved row's report-day mcap when the
                # re-fetch comes back empty for a name Polygon dropped.
                r["mcap"] = caps.get(r.get("ticker")) or r.get("mcap")
            day[k].sort(key=lambda x: (x.get("mcap") is None,
                                       -(x.get("mcap") or 0)))

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
