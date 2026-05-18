"""
polygon_data.py — Polygon.io (Massive) market-data client.

Primary price/options feed for the hub, replacing the unofficial yfinance
scraper. Polygon is an official keyed REST API — reliable from datacenter
IPs (GitHub Actions), no rate-limit roulette, no empty-frame failures.

Every function FAILS SOFT: on any error it returns None / [] / {} so callers
can fall back to yfinance. Plan: Stocks Starter + Options Starter
(15-min delayed — fine for a thrice-daily / EOD research hub).

Key is read lazily via _key() so import order vs load_dotenv() never bites.
"""

import os
import time
import requests
from datetime import datetime, timedelta, timezone

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except Exception:
    pass

_BASE = "https://api.polygon.io"


def _key():
    """Read POLYGON_API_KEY fresh each call — avoids import-order bugs."""
    return os.environ.get("POLYGON_API_KEY", "")


def available():
    """True if a Polygon key is configured."""
    return bool(_key())


def _get(path, params=None, retries=2, timeout=30):
    """GET a Polygon endpoint with retry/backoff. Returns parsed JSON, or None.

    path may be a relative path ('/v2/...') or a full next_url cursor."""
    key = _key()
    if not key:
        return None
    params = dict(params or {})
    params["apiKey"] = key
    url = path if path.startswith("http") else _BASE + path
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.ok:
                return r.json()
            if r.status_code == 429:                 # rate limited
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code in (401, 403):          # not entitled — don't retry
                print(f"  polygon {r.status_code}: not entitled — {path}")
                return None
            print(f"  polygon {r.status_code}: {r.text[:120]}")
        except Exception as e:
            print(f"  polygon error: {e}")
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    return None


def _paginate(path, params=None, max_pages=25):
    """Follow Polygon next_url cursors, concatenating .results."""
    out, pages = [], 0
    data = _get(path, params)
    while data and pages < max_pages:
        out.extend(data.get("results", []) or [])
        nxt = data.get("next_url")
        if not nxt:
            break
        data = _get(nxt)            # next_url carries its own params; _get adds apiKey
        pages += 1
    return out


# ─── Stocks ───────────────────────────────────────────────────────────────────

def grouped_daily(date_str):
    """Whole-US-market OHLCV for one trading day — a SINGLE API call.

    Returns {ticker: {o,h,l,c,v,vw,n,t}} or {} on failure.
    date_str: 'YYYY-MM-DD'. Non-trading days return {}."""
    data = _get(f"/v2/aggs/grouped/locale/us/market/stocks/{date_str}",
                {"adjusted": "true"})
    if not data or not data.get("results"):
        return {}
    return {row["T"]: row for row in data["results"] if row.get("T")}


def daily_bars(ticker, days=120):
    """Daily OHLCV bars for one ticker over roughly the last `days` calendar
    days. Returns a list of bar dicts (oldest first), each {o,h,l,c,v,vw,t},
    or [] on failure."""
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=int(days * 1.6) + 10)   # pad for weekends/holidays
    data = _get(f"/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
                {"adjusted": "true", "sort": "asc", "limit": 50000})
    if not data or not data.get("results"):
        return []
    return data["results"]


def snapshot(ticker):
    """Current-session snapshot for one ticker (latest price + today's bar +
    prev close). Returns the snapshot dict or None."""
    data = _get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}")
    if not data:
        return None
    return data.get("ticker")


def ticker_details(ticker):
    """Reference data: name, market cap, share class / weighted shares,
    primary exchange, etc. Returns the results dict or None."""
    data = _get(f"/v3/reference/tickers/{ticker}")
    return (data or {}).get("results")


# ─── Options ──────────────────────────────────────────────────────────────────

def option_chain(underlying, contract_type=None, spot=None,
                  strike_window=None, limit=250, max_pages=25):
    """Full option-chain snapshot for an underlying — every contract with
    open_interest, day volume, implied_volatility, greeks, last_quote,
    last_trade, and details (strike_price, expiration_date, contract_type).

    contract_type : 'call' | 'put' | None (both)
    spot+strike_window : if both given, keep only contracts whose strike is
                         within +/- strike_window (fraction) of spot — trims
                         deep ITM/OTM noise and payload size.
    Returns a list of contract snapshot dicts, or []."""
    params = {"limit": limit}
    if contract_type:
        params["contract_type"] = contract_type
    rows = _paginate(f"/v3/snapshot/options/{underlying}", params,
                     max_pages=max_pages)
    if spot and strike_window:
        lo, hi = spot * (1 - strike_window), spot * (1 + strike_window)
        rows = [c for c in rows
                if lo <= (c.get("details", {}).get("strike_price") or 0) <= hi]
    return rows


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"Polygon key configured: {available()}")
    gd = grouped_daily("2026-05-15")
    print(f"grouped_daily 2026-05-15: {len(gd)} tickers")
    bars = daily_bars("AAPL", days=60)
    print(f"daily_bars AAPL: {len(bars)} bars")
    snap = snapshot("AAPL")
    if snap:
        print(f"snapshot AAPL: last=${snap.get('day', {}).get('c')}  "
              f"prevClose=${snap.get('prevDay', {}).get('c')}")
    det = ticker_details("AAPL")
    if det:
        print(f"details AAPL: {det.get('name')}  mktcap={det.get('market_cap')}")
    chain = option_chain("AAPL", contract_type="call")
    print(f"option_chain AAPL calls: {len(chain)} contracts")
