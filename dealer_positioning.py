"""
dealer_positioning.py — full-chain dealer gamma + vanna surfaces.

The Index Levels page already shows nearest-expiry GEX walls from the
worker's ?chain0= endpoint (2 expiries, +/-$27 strikes). This nightly
job builds the FULL-CHAIN picture for SPY / QQQ / IWM: every liquid
strike across every expiry out to ~45 days, aggregated into the two
dealer-positioning surfaces traders actually watch:

  GEX (gamma exposure) — where dealer hedging pins or accelerates price.
    Gamma flip = the spot level where cumulative dealer gamma crosses
    zero (below it, negative gamma amplifies moves; above it, positive
    gamma dampens them). Call/put walls = the biggest positive/negative
    gamma strikes.

  VEX (vanna exposure) — how dealer delta shifts as volatility moves,
    the driver of vol-triggered "vanna rallies"/sell-offs. Vanna flip =
    where cumulative vanna crosses zero.

Greeks come from Black-Scholes on each contract's Polygon IV (so gamma
and vanna share one model), weighted by OCC-settled open interest.
Dealer convention (standard simplification, LABELED as such): customers
are net long calls / net short puts, so dealers are short gamma in calls
and long gamma in puts — calls contribute +GEX, puts -GEX. All data is
15-minute delayed; OI is prior-session settled. Educational, not advice.

POLYGON_API_KEY is read from the environment — CI only (the key is never
handled locally). Publishes docs/reports/dealer_positioning.json.

  python dealer_positioning.py             # fetch + aggregate + publish
  python dealer_positioning.py --self-test # validate the BS math offline
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

_BASE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(_BASE, "docs", "reports", "dealer_positioning.json")
SYMS = ("SPY", "QQQ", "IWM")
MAX_DTE = 45            # expiries out to ~6 weeks
STRIKE_PCT = 0.20       # +/-20% of spot
CONTRACT_MULT = 100
RISK_FREE = 0.04        # flat r; levels are insensitive to small changes
HDRS = {"User-Agent": "Mozilla/5.0 (TickerDesk dealer-positioning)"}


# ── Black-Scholes greeks (per share) ────────────────────────────────────

def _norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1(spot, strike, t, sigma, r=RISK_FREE):
    if spot <= 0 or strike <= 0 or t <= 0 or sigma <= 0:
        return None
    return (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / \
        (sigma * math.sqrt(t))


def bs_gamma(spot, strike, t, sigma, r=RISK_FREE):
    """d2V/dS2 — identical for calls and puts."""
    d1 = _d1(spot, strike, t, sigma, r)
    if d1 is None:
        return 0.0
    return _norm_pdf(d1) / (spot * sigma * math.sqrt(t))


def bs_vanna(spot, strike, t, sigma, r=RISK_FREE):
    """d2V/dSdsigma — identical for calls and puts.
    vanna = -phi(d1) * d2 / sigma, with d2 = d1 - sigma*sqrt(t)."""
    d1 = _d1(spot, strike, t, sigma, r)
    if d1 is None:
        return 0.0
    d2 = d1 - sigma * math.sqrt(t)
    return -_norm_pdf(d1) * d2 / sigma


# ── Polygon full-chain fetch (paginated) ────────────────────────────────

def _get(url):
    req = urllib.request.Request(url, headers=HDRS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _spot(sym, key):
    """Underlying last/prev price from Polygon snapshot."""
    url = ("https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/"
           "tickers/" + sym + "?apiKey=" + key)
    try:
        j = _get(url)
        t = j.get("ticker") or {}
        for path in (("lastTrade", "p"), ("day", "c"), ("prevDay", "c")):
            v = t.get(path[0], {}).get(path[1])
            if v:
                return float(v)
    except Exception:
        pass
    return None


def fetch_chain(sym, spot, key):
    """Every contract within STRIKE_PCT of spot, expiring <= MAX_DTE.
    Follows next_url pagination. Returns list of contract dicts."""
    today = datetime.now(timezone.utc).date()
    lte = (today + timedelta(days=MAX_DTE)).isoformat()
    lo = math.floor(spot * (1 - STRIKE_PCT))
    hi = math.ceil(spot * (1 + STRIKE_PCT))
    url = ("https://api.polygon.io/v3/snapshot/options/" + sym +
           "?limit=250&expiration_date.gte=" + today.isoformat() +
           "&expiration_date.lte=" + lte +
           "&strike_price.gte=" + str(lo) +
           "&strike_price.lte=" + str(hi) +
           "&apiKey=" + key)
    out = []
    pages = 0
    while url and pages < 20:                 # hard cap: ~5000 contracts
        j = _get(url)
        out.extend(j.get("results") or [])
        nxt = j.get("next_url")
        url = (nxt + "&apiKey=" + key) if nxt else None
        pages += 1
        if url:
            time.sleep(0.2)                   # be gentle on the API
    return out


# ── aggregation ─────────────────────────────────────────────────────────

def aggregate(contracts, spot):
    """Per-strike GEX + VEX across the full chain (BS greeks from each
    contract's IV × OI × contract multiplier). Dealer convention:
    calls +, puts -."""
    today = datetime.now(timezone.utc).date()
    by_strike = {}
    total_oi = 0
    for c in contracts:
        d = c.get("details") or {}
        k = d.get("strike_price")
        typ = d.get("contract_type")
        exp = d.get("expiration_date")
        oi = c.get("open_interest") or 0
        iv = c.get("implied_volatility")
        if not k or typ not in ("call", "put") or not oi or not iv or not exp:
            continue
        try:
            t = max((datetime.strptime(exp, "%Y-%m-%d").date() - today).days,
                    0) / 365.0
        except Exception:
            continue
        if t <= 0:
            t = 0.5 / 365.0                   # 0DTE -> tiny positive T
        sign = 1.0 if typ == "call" else -1.0
        gamma = bs_gamma(spot, k, t, iv)
        vanna = bs_vanna(spot, k, t, iv)
        # $ gamma per 1% underlying move; $ vanna per 1 vol-point move
        gex = gamma * oi * CONTRACT_MULT * spot * spot * 0.0001 * sign
        vex = vanna * oi * CONTRACT_MULT * spot * 0.01 * sign
        s = by_strike.setdefault(k, {"gex": 0.0, "vex": 0.0})
        s["gex"] += gex
        s["vex"] += vex
        total_oi += oi
    return by_strike, total_oi


def _flip(strikes, key, spot):
    """Spot-interpolated level where cumulative `key` crosses zero, scanning
    from the low strike up. None if it never crosses."""
    ks = sorted(strikes)
    cum = 0.0
    prev_k, prev_cum = None, 0.0
    for k in ks:
        cum += strikes[k][key]
        if prev_k is not None and (prev_cum < 0) != (cum < 0) and \
                (cum - prev_cum) != 0:
            frac = -prev_cum / (cum - prev_cum)
            return round(prev_k + frac * (k - prev_k), 2)
        prev_k, prev_cum = k, cum
    return None


def summarize(sym, by_strike, spot, total_oi):
    if not by_strike:
        return None
    ks = sorted(by_strike)
    total_gex = sum(v["gex"] for v in by_strike.values())
    total_vex = sum(v["vex"] for v in by_strike.values())
    call_wall = max(ks, key=lambda k: by_strike[k]["gex"])
    put_wall = min(ks, key=lambda k: by_strike[k]["gex"])
    largest_gamma = max(ks, key=lambda k: abs(by_strike[k]["gex"]))
    # compact per-strike profile (thin the list for the frontend chart)
    profile = [{"k": k,
                "gex": round(by_strike[k]["gex"] / 1e6, 2),   # $mm / 1%
                "vex": round(by_strike[k]["vex"] / 1e6, 2)}
               for k in ks]
    return {
        "spot": round(spot, 2),
        "gamma_flip": _flip(by_strike, "gex", spot),
        "vanna_flip": _flip(by_strike, "vex", spot),
        "call_wall": call_wall,
        "put_wall": put_wall,
        "largest_gamma_strike": largest_gamma,
        "total_gex_mm": round(total_gex / 1e6, 1),      # $mm per 1% move
        "total_vex_mm": round(total_vex / 1e6, 1),      # $mm per 1 vol pt
        "regime": ("positive" if total_gex >= 0 else "negative"),
        "n_strikes": len(ks),
        "total_oi": total_oi,
        "profile": profile,
    }


def build(key):
    syms = {}
    for sym in SYMS:
        spot = _spot(sym, key)
        if not spot:
            print("  %s: no spot — skipping" % sym)
            continue
        try:
            chain = fetch_chain(sym, spot, key)
        except Exception as e:
            print("  %s: chain fetch failed (%s) — skipping" %
                  (sym, type(e).__name__))
            continue
        by_strike, total_oi = aggregate(chain, spot)
        s = summarize(sym, by_strike, spot, total_oi)
        if s:
            syms[sym] = s
            print("  %s: %d contracts, %d strikes, GEX %.1f$mm (%s gamma), "
                  "flip %s" % (sym, len(chain), s["n_strikes"],
                               s["total_gex_mm"], s["regime"],
                               s["gamma_flip"]))
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbols": syms,
        "delayed": True,
        "note": ("Full-chain dealer gamma (GEX) + vanna (VEX) for SPY/QQQ/"
                 "IWM, all strikes within " + str(int(STRIKE_PCT * 100)) +
                 "% of spot out to " + str(MAX_DTE) + " days. Black-Scholes "
                 "greeks from each contract's implied vol x OCC-settled OI. "
                 "Dealer convention (calls +, puts -) is the standard "
                 "simplification, not a position read. Data 15-min delayed; "
                 "OI prior-session. Educational, not advice."),
    }


def _self_test():
    """Validate the BS math offline (no network / no API key). ATM gamma
    should peak near spot; vanna should flip sign across ATM."""
    spot, t, sig = 100.0, 30 / 365.0, 0.20
    g_atm = bs_gamma(spot, 100, t, sig)
    g_otm = bs_gamma(spot, 130, t, sig)
    assert g_atm > g_otm > 0, "ATM gamma should exceed far-OTM gamma"
    v_lo = bs_vanna(spot, 90, t, sig)
    v_hi = bs_vanna(spot, 110, t, sig)
    assert (v_lo > 0) != (v_hi > 0), "vanna should flip sign across ATM"
    # synthetic chain: call OI concentrated ABOVE spot, put OI BELOW —
    # so calls (+GEX) dominate the high strikes and puts (-GEX) the low
    # ones, giving a call wall above spot and a put wall below it. (Equal
    # call/put OI at a strike cancels, since calls and puts share gamma —
    # dealer walls only exist where the OI is lopsided.)
    exp = (datetime.now(timezone.utc).date() +
           timedelta(days=30)).isoformat()
    contracts = []
    for k in range(80, 121, 5):
        call_oi = 2000 if k >= spot else 200
        put_oi = 2000 if k <= spot else 200
        contracts.append({"details": {"strike_price": k,
                          "contract_type": "call", "expiration_date": exp},
                          "open_interest": call_oi,
                          "implied_volatility": 0.2})
        contracts.append({"details": {"strike_price": k,
                          "contract_type": "put", "expiration_date": exp},
                          "open_interest": put_oi,
                          "implied_volatility": 0.2})
    by_strike, oi = aggregate(contracts, spot)
    s = summarize("TEST", by_strike, spot, oi)
    assert s["n_strikes"] == 9, "one strike row per distinct strike"
    assert s["call_wall"] > spot > s["put_wall"], \
        "call wall above spot, put wall below"
    print("  self-test OK: gamma peaks ATM, vanna flips ATM, aggregate "
          "walls bracket spot (call_wall=%s put_wall=%s flip=%s)"
          % (s["call_wall"], s["put_wall"], s["gamma_flip"]))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    key = os.environ.get("POLYGON_API_KEY")
    if not key:
        print("  POLYGON_API_KEY not set — dealer_positioning is CI-only. "
              "Run --self-test to validate the math locally.")
        return 0
    payload = build(key)
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    print("  Wrote %s (%d symbols)" %
          (os.path.relpath(OUT_PATH, _BASE), len(payload["symbols"])))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
