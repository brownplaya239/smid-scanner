#!/usr/bin/env python3
"""estimates_provider_test.py — parse Finnhub payloads without a key.

The client cannot be exercised live here (the key is a CI secret and must
never be handled locally), so the parsing is tested against synthetic
payloads shaped exactly like Finnhub's documented responses, plus the two
behaviours that matter most: fails closed with no key, and fails per-field
when the tier gates an endpoint.

    python estimates_provider_test.py
"""

import os
import sys

import estimates_provider as P

_pass = _fail = 0


def chk(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print("  PASS  %s" % name)
    else:
        _fail += 1
        print("  FAIL  %s" % name)


# ── documented Finnhub response shapes ──────────────────────────────────
PT = {"symbol": "NOW", "targetHigh": 1200, "targetLow": 800,
      "targetMean": 1050.5, "targetMedian": 1075, "numberOfAnalysts": 34,
      "lastUpdated": "2026-07-18 00:00:00"}
REC = [{"symbol": "NOW", "period": "2026-07-01", "strongBuy": 18, "buy": 9,
        "hold": 6, "sell": 1, "strongSell": 0},
       {"symbol": "NOW", "period": "2026-06-01", "strongBuy": 17, "buy": 10,
        "hold": 6, "sell": 1, "strongSell": 0}]
EPS = {"symbol": "NOW", "freq": "quarterly",
       "data": [{"period": "2026-09-30", "quarter": 3, "year": 2026,
                 "epsAvg": 4.12, "epsHigh": 4.35, "epsLow": 3.9,
                 "numberAnalysts": 28}]}
REV = {"symbol": "NOW", "freq": "quarterly",
       "data": [{"period": "2026-09-30", "quarter": 3, "year": 2026,
                 "revenueAvg": 3450.0, "revenueHigh": 3510.0,
                 "revenueLow": 3390.0, "numberAnalysts": 26}]}
SUR = [{"period": "2026-06-30", "actual": 3.9, "estimate": 3.75,
        "surprisePercent": 4.0, "symbol": "NOW"},
       {"period": "2026-03-31", "actual": 3.5, "estimate": 3.55,
        "surprisePercent": -1.4, "symbol": "NOW"}]


print("full premium payload")
rec = P.parse_all({
    "price_target": (PT, None), "recommendation": (REC, None),
    "eps": (EPS, None), "rev": (REV, None), "surprises": (SUR, None)})
chk("configured", rec["configured"] is True)
chk("target mean parsed", rec["price_target"]["mean"] == 1050.5)
chk("target carries as-of date", rec["price_target"]["as_of"] == "2026-07-18")
chk("recommendation band = Buy",
    rec["recommendation"]["band"] == "Buy")
chk("recommendation score is weighted mean",
    rec["recommendation"]["score"] < 2.0)
chk("EPS estimate parsed", rec["eps_estimate_next"]["avg"] == 4.12)
chk("revenue estimate parsed", rec["rev_estimate_next"]["avg"] == 3450.0)
chk("surprises parsed (2)", len(rec["surprises"]) == 2)
chk("all coverage ok",
    all(v == "ok" for v in rec["coverage"].values()))

print("\nfree tier: premium endpoints 403, free ones return")
rec = P.parse_all({
    "price_target": (None, "HTTP 403"), "recommendation": (REC, None),
    "eps": (None, "HTTP 403"), "rev": (None, "HTTP 403"),
    "surprises": (SUR, None)})
chk("recommendation still present", rec["recommendation"] is not None)
chk("surprises still present", len(rec["surprises"]) == 2)
chk("price target withheld, not invented", rec["price_target"] is None)
chk("price target marked premium-gated",
    rec["coverage"]["price_target"] == "premium-gated")
chk("eps estimate marked premium-gated",
    rec["coverage"]["eps_estimate"] == "premium-gated")
chk("free recommendation not marked gated",
    rec["coverage"]["recommendation"] == "ok")

print("\nempty / malformed bodies")
rec = P.parse_all({
    "price_target": ({"targetMean": 0}, None),   # zero == no data
    "recommendation": ([], None),
    "eps": ({"data": []}, None), "rev": (None, None),
    "surprises": ([], None)})
chk("zero target mean -> None", rec["price_target"] is None)
chk("empty recommendation -> None", rec["recommendation"] is None)
chk("empty eps data -> None", rec["eps_estimate_next"] is None)
chk("empty surprises -> []", rec["surprises"] == [])

print("\nsurprise rows missing actual/estimate are dropped")
rec = P.parse_all({"surprises": (
    [{"period": "2026-06-30", "actual": None, "estimate": 3.75},
     {"period": "2026-03-31", "actual": 3.5, "estimate": 3.55,
      "surprisePercent": -1.4}], None)})
chk("only the complete surprise row survives", len(rec["surprises"]) == 1)

print("\nfails closed with no key")
saved = os.environ.pop(P.ENV_KEY, None)
try:
    r = P.fetch_estimates("NOW")
    chk("no key -> configured False", r["configured"] is False)
    chk("no key -> rating inputs all None",
        r["price_target"] is None and r["recommendation"] is None)
    chk("no key -> reason recorded", "not set" in (r.get("reason") or ""))
finally:
    if saved is not None:
        os.environ[P.ENV_KEY] = saved

print("\nband thresholds")
chk("all-hold -> Hold", P._rec_consensus(0, 0, 10, 0, 0)[1] == "Hold")
chk("all-strong-sell -> Sell", P._rec_consensus(0, 0, 0, 0, 5)[1] == "Sell")
chk("skew to sell -> Underperform",
    P._rec_consensus(0, 0, 2, 8, 0)[1] == "Underperform")

print("\npeer parsing")
PEERS = ["NOW", "CRM", "WDAY", "TEAM"]
METRICS = {
    "CRM": ({"metric": {"peTTM": 42.3}, "symbol": "CRM"}, None),
    "WDAY": ({"metric": {"peBasicExclExtraTTM": 38.0}, "symbol": "WDAY"},
             None),
    "TEAM": (None, "HTTP 403"),
}
pr = P.parse_peers(PEERS, METRICS, "NOW")
chk("subject dropped from peer rows",
    all(r["ticker"] != "NOW" for r in pr["rows"]))
chk("three peer rows", len(pr["rows"]) == 3)
chk("CRM peTTM parsed", any(r["ticker"] == "CRM" and r["pe"] == 42.3
                            for r in pr["rows"]))
chk("WDAY fallback pe key parsed",
    any(r["ticker"] == "WDAY" and r["pe"] == 38.0 for r in pr["rows"]))
chk("peer with no metric kept with blank pe",
    any(r["ticker"] == "TEAM" and r["pe"] is None for r in pr["rows"]))

saved = os.environ.pop(P.ENV_KEY, None)
try:
    chk("fetch_peers fails closed with no key",
        P.fetch_peers("NOW") is None)
finally:
    if saved is not None:
        os.environ[P.ENV_KEY] = saved

print("\n%d/%d checks passed" % (_pass, _pass + _fail))
sys.exit(1 if _fail else 0)
