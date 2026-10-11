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

Fallback + session arbiter: Nasdaq's public earnings calendar
  GET https://api.nasdaq.com/api/calendar/earnings?date=YYYY-MM-DD
  (browser headers; the Cloudflare worker proxies it when Nasdaq refuses
  the CI runner's address). EW's endpoint has 404'd since the week of
  2026-09-28, and Finnhub's calendar — the previous fallback — is 401 on
  the free tier even with a valid key, so Nasdaq fills the whole week
  when EW returns nobody and otherwise just confirms EW's BMO/AMC bucket.

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


# ── Nasdaq public earnings calendar ──────────────────────────────────
# The JSON behind nasdaq.com/market-activity/earnings. Akamai in front
# of it wants browser-shaped headers and refuses some datacenter ranges
# outright, so after a direct miss the fetch goes through our Cloudflare
# worker (Cloudflare egress is accepted — the econ calendar has pulled
# Nasdaq from there since 2026-07).
NASDAQ_URL = "https://api.nasdaq.com/api/calendar/earnings?date="
WORKER = "https://api.tickerdesk.io"
NASDAQ_HDRS = {
    "User-Agent": HDRS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/market-activity/earnings",
}
# Nasdaq's `time` field -> our session. "time-not-supplied" has no
# confirmed hour and cannot be placed in a BMO or AMC column.
NASDAQ_SESSION = {"time-pre-market": "bmo", "time-after-hours": "amc"}
FALLBACK_PER_SESSION = 25     # largest names kept per day + session


def _get_json(url, headers, timeout=30):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _nasdaq_day(date_iso):
    """Nasdaq's raw rows for one date -> (rows, 'nasdaq'|'worker').
    Direct first, then the worker proxy; raises only when both fail."""
    errors = []
    for label, url, hdrs in (
            ("nasdaq", NASDAQ_URL + date_iso, NASDAQ_HDRS),
            ("worker", WORKER + "/?earnings-calendar=" + date_iso,
             {"User-Agent": "tickerdesk-ci/earnings_anticipated"})):
        try:
            data = _get_json(url, hdrs) or {}
        except Exception as e:
            errors.append(f"{label}: {e}")
            continue
        status = data.get("status") or {}
        code = status.get("rCode")
        if code not in (None, 200):
            errors.append(f"{label}: rCode {code} "
                          f"{status.get('bCodeMessage') or ''}".strip())
            continue
        return ((data.get("data") or {}).get("rows") or []), label
    raise RuntimeError("; ".join(errors))


def _parse_mcap(s):
    """Nasdaq's '$880,976,070,000' -> 880976070000.0; 'N/A'/blank -> None."""
    s = str(s or "").strip().replace("$", "").replace(",", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def _shape_nasdaq(r, hour, date_iso):
    """A Nasdaq row in the _shape() layout the dashboard renders. No
    anticipation score exists on this feed (None); the market cap rides
    along so the sort needs no Polygon lookup for these names."""
    ests = str(r.get("noOfEsts") or "").replace(",", "").strip()
    return {
        "ticker":       (r.get("symbol") or "").strip().upper(),
        "company":      (r.get("name") or "").strip() or None,
        "score":        None,
        "analysts":     int(ests) if ests.isdigit() else None,
        "release_time": 1 if hour == "bmo" else 3,
        "confirmed":    True,                    # session stated by the feed
        "next_eps":     date_iso,
        "q":            (r.get("fiscalQuarterEnding") or "").strip() or None,
        "eps_forecast": (r.get("epsForecast") or "").strip() or None,
        "mcap":         _parse_mcap(r.get("marketCap")),
        "source":       "nasdaq",
    }


def _nasdaq_week(monday):
    """Nasdaq's confirmed reporters Mon-Fri as _shape()-style rows:
    {YYYY-MM-DD: {'bmo': [...], 'amc': [...]}}. Plain US tickers only
    (Nasdaq also lists ADR lines and units with dots/slashes the rest of
    the site can't price) and only rows with a stated session. A day
    whose fetch failed both ways is absent, so a caller can tell "nobody
    reports" from "couldn't ask"."""
    out = {}
    for i in range(5):
        iso = (monday + timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            rows, via = _nasdaq_day(iso)
        except Exception as e:
            print(f"  {iso} Nasdaq calendar unavailable: {e}")
            continue
        day = {"bmo": [], "amc": []}
        skipped = 0
        for r in rows:
            sym = (r.get("symbol") or "").strip().upper()
            hour = NASDAQ_SESSION.get((r.get("time") or "").strip().lower())
            if not sym.isalpha() or len(sym) > 5 or not hour:
                skipped += 1
                continue
            day[hour].append(_shape_nasdaq(r, hour, iso))
        out[iso] = day
        print(f"  {iso} Nasdaq via {via}: BMO={len(day['bmo']):3d}  "
              f"AMC={len(day['amc']):3d}  (skipped {skipped} unconfirmed/"
              "non-US rows)")
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
    nq = _nasdaq_week(monday)
    # Confirmed report hours by (SYMBOL, date). They arbitrate EW's
    # session buckets, which are wrong often enough to matter (2026-07-27:
    # EW listed Noble Corp as before-open; NE reported after the close
    # and fell 4% AH while sitting in the site's morning column).
    sessions = {(r["ticker"], ds): hour
                for ds, day in nq.items()
                for hour in ("bmo", "amc") for r in day[hour]}
    if sessions:
        print(f"  Nasdaq session cross-check: {len(sessions)} confirmed hours")
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
        # Re-bucket where Nasdaq's confirmed hour disagrees with EW.
        if sessions:
            dstr_iso = d.strftime("%Y-%m-%d")
            moved = []
            keep_b, keep_a = [], []
            for r in bmo_s:
                hour = sessions.get(((r.get("ticker") or "").upper(), dstr_iso))
                if hour == "amc":
                    r["session_corrected"] = "EW listed BMO; Nasdaq AMC"
                    keep_a.append(r)
                    moved.append(r["ticker"] + "->AMC")
                else:
                    keep_b.append(r)
            for r in amc_s:
                hour = sessions.get(((r.get("ticker") or "").upper(), dstr_iso))
                if hour == "bmo":
                    r["session_corrected"] = "EW listed AMC; Nasdaq BMO"
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

    source = "earningswhispers.com (Most Anticipated calendar)"
    fallback = False
    if not any(day["bmo"] or day["amc"] for day in days):
        if any(day["bmo"] or day["amc"] for day in nq.values()):
            fallback = True
            source = ("nasdaq.com earnings calendar (confirmed pre-market/"
                      "after-hours reporters, largest %d per session; "
                      "Earnings Whispers unavailable)" % FALLBACK_PER_SESSION)
            for day in days:
                got = nq.get(day["date"]) or {}
                day["bmo"] = list(got.get("bmo") or [])
                day["amc"] = list(got.get("amc") or [])
            print("  Earnings Whispers returned nobody — using Nasdaq "
                  "(%d confirmed reporters)" % sum(
                      len(day["bmo"]) + len(day["amc"]) for day in days))

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
    # calendar in is by size. Nasdaq rows carry their own cap; Polygon
    # (behind the weekly cache) fills in EW rows, which have none.
    need = sorted({r["ticker"] for day in days
                   for k in ("bmo", "amc") for r in day[k]
                   if r.get("ticker") and r.get("mcap") is None})
    caps = _market_caps(need) if need else {}
    for day in days:
        for k in ("bmo", "amc"):
            for r in day[k]:
                # `or` keeps a row's feed/report-day mcap when the
                # lookup comes back empty for a name Polygon dropped.
                r["mcap"] = caps.get(r.get("ticker")) or r.get("mcap")
            day[k].sort(key=lambda x: (x.get("mcap") is None,
                                       -(x.get("mcap") or 0)))
            if fallback:
                # Nasdaq lists every reporter (hundreds a week); the
                # calendar is "the names people watch", so keep the
                # largest per session, like EW's curated list.
                day[k] = day[k][:FALLBACK_PER_SESSION]

    total = sum(len(x["bmo"]) + len(x["amc"]) for x in days)
    payload = {
        "week_of":   monday.strftime("%Y-%m-%d"),
        "generated": datetime.now(ET).isoformat(timespec="seconds"),
        "source":    source,
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
