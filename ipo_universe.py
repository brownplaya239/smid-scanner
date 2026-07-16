"""Recent-IPO universe + average-volume table.

Feeds the desk's "Recent IPOs running" scan: names that listed within the
last N years, with the 30-session average volume needed to compute a live
relative-volume read against the worker's intraday snapshot.

Two facts, two sources:
  • listing date — Polygon reference. Tried cheaply first via the IPOs
    endpoint (one paginated sweep); falls back to per-ticker details for
    any active common stock we haven't dated yet. A listing date NEVER
    changes, so data/ipo_dates_cache.json is a permanent cache: the
    expensive sweep happens once, later runs only date new listings.
  • average volume — 30 grouped_daily calls (one per session, whole market
    each), averaged per ticker. Only computed for the recent-IPO set, so
    the published file stays small.

Output: docs/reports/ipo_universe.json
  {generated, ipo_window_years, sessions_avg, count,
   tickers: {TSLA: {ipo: "2021-03-04", avgvol: 1234567}}}

Runs in CI only — POLYGON_API_KEY is never handled locally.
"""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import polygon_data as pg

_BASE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(_BASE, "data", "ipo_dates_cache.json")
SITE_PATH = os.path.join(_BASE, "docs", "reports", "ipo_universe.json")

IPO_YEARS = 5           # "recent IPO" window
AVG_SESSIONS = 30       # sessions in the average-volume window
WORKERS = 8
# Only date names that could plausibly be in scope. Common stock only —
# ETFs, warrants, units and preferreds aren't IPOs in the sense meant here.
TYPES = ("CS",)


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))


def active_common_stocks():
    """[{ticker, name}] for every active US common stock (paginated)."""
    rows = pg._paginate("/v3/reference/tickers",
                        {"market": "stocks", "active": "true",
                         "type": "CS", "limit": 1000}, max_pages=30)
    out = {}
    for r in rows:
        t = (r.get("ticker") or "").upper()
        if t and "." not in t and "-" not in t:
            out[t] = r.get("name") or ""
    return out


def ipos_endpoint_dates(since):
    """{ticker: listing_date} from Polygon's IPOs endpoint — one cheap
    sweep instead of thousands of detail calls. Experimental endpoint, so
    every failure mode falls through to the per-ticker path."""
    out = {}
    try:
        rows = pg._paginate("/vX/reference/ipos",
                            {"listing_date.gte": since, "limit": 1000,
                             "order": "asc", "sort": "listing_date"},
                            max_pages=30)
        for r in rows or []:
            t = (r.get("ticker") or "").upper()
            d = r.get("listing_date")
            if t and d:
                out[t] = d[:10]
    except Exception as e:
        print(f"  IPOs endpoint unavailable ({e}) — using ticker details")
    return out


def detail_date(ticker):
    try:
        d = pg.ticker_details(ticker) or {}
        return ticker, (d.get("list_date") or "")[:10] or None
    except Exception:
        return ticker, None


def build_dates(universe, cache):
    """cache: {ticker: "YYYY-MM-DD" | ""}. "" = dated, no list_date on
    file (Polygon has none) — cached so we never re-ask."""
    since = (datetime.now(timezone.utc).date() -
             timedelta(days=int(IPO_YEARS * 365.25) + 30)).isoformat()
    fresh = ipos_endpoint_dates(since)
    if fresh:
        print(f"  IPOs endpoint: {len(fresh)} listings since {since}")
        cache.update(fresh)
    todo = [t for t in universe if t not in cache]
    if todo:
        print(f"  dating {len(todo)} tickers via ticker details "
              f"({len(cache)} already cached)")
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for t, d in ex.map(detail_date, todo):
                cache[t] = d or ""
    return cache


def avg_volumes(tickers):
    """{ticker: avg 30-session volume} — whole-market grouped bars, so the
    cost is AVG_SESSIONS calls regardless of how many tickers we want."""
    want = set(tickers)
    sums, counts = {}, {}
    day = datetime.now(timezone.utc).date()
    got = 0
    while got < AVG_SESSIONS and (datetime.now(timezone.utc).date() - day).days < 70:
        day -= timedelta(days=1)
        if day.weekday() >= 5:
            continue
        bars = pg.grouped_daily(day.isoformat())
        if not bars:                       # holiday / not yet published
            continue
        got += 1
        for t, row in bars.items():
            if t in want and row.get("v"):
                sums[t] = sums.get(t, 0) + row["v"]
                counts[t] = counts.get(t, 0) + 1
    print(f"  averaged volume over {got} sessions")
    return {t: round(sums[t] / counts[t]) for t in sums
            if counts.get(t, 0) >= max(5, got // 2)}


def main():
    if not pg.available():
        print("POLYGON_API_KEY missing — skipping (CI-only job)")
        return 0
    universe = active_common_stocks()
    print(f"  {len(universe)} active common stocks")
    if not universe:
        print("  reference sweep empty — aborting without touching outputs")
        return 0
    cache = _load(CACHE_PATH, {})
    cache = build_dates(universe, cache)
    _save(CACHE_PATH, cache)

    cutoff = (datetime.now(timezone.utc).date() -
              timedelta(days=int(IPO_YEARS * 365.25))).isoformat()
    recent = {t: d for t, d in cache.items()
              if d and d >= cutoff and t in universe}
    print(f"  {len(recent)} listed on/after {cutoff}")
    avg = avg_volumes(recent.keys())

    tickers = {}
    for t, d in recent.items():
        if t in avg:
            tickers[t] = {"ipo": d, "avgvol": avg[t]}
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ipo_window_years": IPO_YEARS,
        "sessions_avg": AVG_SESSIONS,
        "cutoff": cutoff,
        "count": len(tickers),
        "tickers": tickers,
        "note": ("Common stocks that listed within the IPO window, with "
                 "their average daily volume over the last "
                 f"{AVG_SESSIONS} sessions. Listing dates are permanent — "
                 "cached once, only new listings are dated on later runs."),
    }
    _save(SITE_PATH, payload)
    print(f"  published {len(tickers)} recent-IPO names with avg volume")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
