#!/usr/bin/env python3
"""estimates_provider.py — the v4 consensus / price-target feed.

v4's headline rating and 12-month target may not be invented, so they
need a real estimates source. This is that source, behind one contract:

    fetch_estimates(ticker, report_time) -> dict

The provider is Finnhub. Its analyst data carries genuine as-of dates
(price-target lastUpdated, estimate period), which is why it clears the
same dating bar every other figure in the report must — an undated
vendor aggregate would not.

TWO PROPERTIES THIS MODULE GUARANTEES.

Fails closed. With no FINNHUB_API_KEY it returns configured=False and
every field None. The report then WITHHOLDS the rating ("no admitted
estimate source") rather than printing an invented one. The key is a
CI/worker secret; it is never read from anywhere but the environment.

Fails per-field. Finnhub gates price-target and EPS/revenue estimates
behind its premium tier while recommendation trends and earnings
surprises are free. A gated endpoint (403) or an empty body leaves that
one field None and records why in `coverage`; it never fabricates the
number and never takes down the whole fetch. So a free-tier key still
yields the consensus recommendation and the surprise history, and a
premium key adds the target and the estimates.

Every returned figure carries its own as-of date and the endpoint it came
from, so the renderer can label it and the validator can check it.
"""

import datetime as dt
import json
import os
import urllib.error
import urllib.request

PROVIDER = "finnhub"
ENV_KEY = "FINNHUB_API_KEY"
BASE = "https://finnhub.io/api/v1"
TIMEOUT = 15

# Finnhub free tier serves these; the rest 403 without premium. Recorded
# so a gated field reads as "premium-gated", not "absent" — a real
# distinction for a reader deciding whether the gap is fixable.
_FREE = {"recommendation", "surprises", "quote"}


def _get(path, params, http):
    """One GET. Returns (json, error_str). error_str is set on any
    non-200 or transport failure; the caller decides what a miss means for
    its field."""
    key = os.environ.get(ENV_KEY, "")
    q = "&".join("%s=%s" % (k, v) for k, v in params.items())
    url = "%s%s?%s&token=%s" % (BASE, path, q, key)
    try:
        req = urllib.request.Request(url, headers={"Accept":
                                                   "application/json"})
        with (http or urllib.request.urlopen)(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, "HTTP %s" % e.code
    except Exception as e:                            # pragma: no cover
        return None, "fetch failed: %s" % e


def _rec_consensus(strong_buy, buy, hold, sell, strong_sell):
    """A weighted mean of the recommendation counts on the standard
    1(strong buy)..5(strong sell) scale, plus the plain-English band. This
    is arithmetic over vendor counts, not a view of our own."""
    n = strong_buy + buy + hold + sell + strong_sell
    if not n:
        return None, None
    score = (1 * strong_buy + 2 * buy + 3 * hold + 4 * sell
             + 5 * strong_sell) / float(n)
    band = ("Buy" if score <= 2.0 else "Outperform" if score <= 2.5
            else "Hold" if score <= 3.5 else "Underperform"
            if score <= 4.0 else "Sell")
    return round(score, 2), band


def _parse_price_target(j):
    if not isinstance(j, dict) or j.get("targetMean") in (None, 0):
        return None
    return {"mean": j.get("targetMean"), "high": j.get("targetHigh"),
            "low": j.get("targetLow"), "median": j.get("targetMedian"),
            "n_analysts": j.get("numberOfAnalysts"),
            "as_of": (j.get("lastUpdated") or "").split(" ")[0] or None,
            "endpoint": "/stock/price-target"}


def _parse_recommendation(j):
    if not isinstance(j, list) or not j:
        return None
    r = j[0]                                          # most recent period
    sb, b = r.get("strongBuy", 0), r.get("buy", 0)
    h = r.get("hold", 0)
    s, ss = r.get("sell", 0), r.get("strongSell", 0)
    score, band = _rec_consensus(sb, b, h, s, ss)
    if score is None:
        return None
    return {"strong_buy": sb, "buy": b, "hold": h, "sell": s,
            "strong_sell": ss, "score": score, "band": band,
            "as_of": r.get("period"), "endpoint": "/stock/recommendation"}


def _parse_estimate(j, avg_key, hi_key, lo_key):
    if not isinstance(j, dict):
        return None
    rows = j.get("data") or []
    if not rows:
        return None
    r = rows[0]
    if r.get(avg_key) is None:
        return None
    return {"avg": r.get(avg_key), "high": r.get(hi_key),
            "low": r.get(lo_key), "n_analysts": r.get("numberAnalysts"),
            "period": r.get("period")}


def _parse_surprises(j, limit=4):
    if not isinstance(j, list):
        return []
    out = []
    for r in j[:limit]:
        if r.get("actual") is None or r.get("estimate") is None:
            continue
        out.append({"period": r.get("period"), "actual": r.get("actual"),
                    "estimate": r.get("estimate"),
                    "surprise_pct": r.get("surprisePercent")})
    return out


def parse_all(payloads):
    """Turn a dict of raw Finnhub responses into the provider record.

    Split from the fetch so the parsing is testable without a key or a
    network. `payloads` maps field -> (json, error).
    """
    pt_j, pt_e = payloads.get("price_target", (None, "not fetched"))
    rec_j, rec_e = payloads.get("recommendation", (None, "not fetched"))
    eps_j, eps_e = payloads.get("eps", (None, "not fetched"))
    rev_j, rev_e = payloads.get("rev", (None, "not fetched"))
    sur_j, sur_e = payloads.get("surprises", (None, "not fetched"))

    price_target = _parse_price_target(pt_j)
    recommendation = _parse_recommendation(rec_j)
    eps_est = _parse_estimate(eps_j, "epsAvg", "epsHigh", "epsLow")
    rev_est = _parse_estimate(rev_j, "revenueAvg", "revenueHigh",
                              "revenueLow")
    surprises = _parse_surprises(sur_j)

    def cover(field, val, err):
        if val:
            return "ok"
        return ("premium-gated" if (err and "403" in str(err)
                                    and field not in _FREE) else
                ("absent" if err in (None, "not fetched") or "HTTP" in
                 str(err) else "error"))

    coverage = {
        "price_target": cover("price_target", price_target, pt_e),
        "recommendation": cover("recommendation", recommendation, rec_e),
        "eps_estimate": cover("eps", eps_est, eps_e),
        "rev_estimate": cover("rev", rev_est, rev_e),
        "surprises": cover("surprises", surprises or None, sur_e),
    }
    return {
        "configured": True,
        "provider": PROVIDER,
        "price_target": price_target,
        "recommendation": recommendation,
        "eps_estimate_next": eps_est,
        "rev_estimate_next": rev_est,
        "surprises": surprises,
        "coverage": coverage,
    }


def not_configured(reason="FINNHUB_API_KEY not set"):
    """The fail-closed record. Every field None, configured False, so the
    report withholds the rating and says why."""
    return {"configured": False, "provider": PROVIDER, "reason": reason,
            "price_target": None, "recommendation": None,
            "eps_estimate_next": None, "rev_estimate_next": None,
            "surprises": [], "coverage": {}}


def fetch_estimates(ticker, report_time=None, http=None):
    """Fetch the consensus record for one ticker.

    Returns not_configured() when the key is absent — the honest default,
    and the one that runs locally where no secret exists. In CI, with the
    secret set, each endpoint is tried independently and a gated one only
    nulls its own field.
    """
    if not os.environ.get(ENV_KEY):
        return not_configured()
    sym = str(ticker).upper().strip()
    payloads = {
        "price_target": _get("/stock/price-target", {"symbol": sym}, http),
        "recommendation": _get("/stock/recommendation", {"symbol": sym},
                               http),
        "eps": _get("/stock/eps-estimate",
                    {"symbol": sym, "freq": "quarterly"}, http),
        "rev": _get("/stock/revenue-estimate",
                    {"symbol": sym, "freq": "quarterly"}, http),
        "surprises": _get("/stock/earnings", {"symbol": sym}, http),
    }
    rec = parse_all(payloads)
    rec["as_of"] = (report_time or
                    dt.datetime.now(dt.timezone.utc).isoformat())
    return rec
