#!/usr/bin/env python3
"""report_v4_model_test.py — the v4 view model, on real snapshots.

Drives build() against the cached snapshots (the free-tier state: no
estimate feed) and against injected consensus / target payloads, checking
the honesty rules hold: the fundamental rating is the consensus and is
withheld with no feed; the target is withheld when the feed gates it; a
directional rating never survives a DATA HOLD event.

    python report_v4_model_test.py
"""

import glob
import io
import os
import pickle
import sys

import report_v4_model as V4
import report_v4_event as EV

_pass = _fail = 0


def chk(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print("  PASS  %s" % name)
    else:
        _fail += 1
        print("  FAIL  %s" % name)


def _load(t):
    obj = pickle.load(io.open(".snapcache/%s.pkl" % t, "rb"))
    return obj[0] if isinstance(obj, tuple) else obj


CACHED = sorted(os.path.basename(p)[:-4]
                for p in glob.glob(".snapcache/*.pkl"))
if not CACHED:
    print("no cached snapshots — run report_v3_run on a ticker first")
    sys.exit(0)

# Finnhub payloads shaped like the provider's output
EST_FREE = {"configured": True, "provider": "finnhub",
            "recommendation": {"strong_buy": 15, "buy": 33, "hold": 5,
                               "sell": 1, "strong_sell": 0, "score": 1.8,
                               "band": "Buy", "as_of": "2026-07-01"},
            "price_target": None,
            "coverage": {"price_target": "premium-gated",
                         "eps_estimate": "premium-gated"},
            "surprises": [{"period": "2026-06-30", "actual": 3.9,
                           "estimate": 3.75, "surprise_pct": 4.0}]}
EST_PREMIUM = dict(EST_FREE,
                   price_target={"mean": 1100, "high": 1250, "low": 900,
                                 "n_analysts": 30, "as_of": "2026-07-18"},
                   eps_estimate_next={"avg": 4.1, "period": "2026-09-30"},
                   coverage={"price_target": "ok", "eps_estimate": "ok"})


print("free tier (no estimate feed) — the local/default state")
for t in CACHED:
    snap = _load(t)
    v = V4.build(snap)                          # estimates=None
    fr = v["ratings"]["fundamental"]
    tr = v["ratings"]["tactical"]
    tg = v["ratings"]["target"]
    chk("%s fundamental rating withheld (no feed)" % t,
        fr["available"] is False)
    chk("%s tactical rating present (our data)" % t, tr["available"] is True)
    chk("%s target withheld" % t, tg["available"] is False)
    chk("%s event state resolved" % t, v["event"]["state"] in EV.STATES)
    chk("%s <=3 risks, each with text" % t,
        len(v["risks"]) <= 3 and all(r.get("text") for r in v["risks"]))
    chk("%s SaaS KPIs marked unavailable" % t,
        v["financials"]["saas_kpis"]["available"] is False)
    chk("%s thesis is a list of {text} (not the raw dict)" % t,
        isinstance(v["thesis"], list)
        and all(isinstance(b, dict) and b.get("text") for b in v["thesis"]))

t0 = CACHED[0]
snap = _load(t0)

print("\nfree Finnhub key: consensus present, target still gated")
v = V4.build(snap, estimates=EST_FREE)
fr = v["ratings"]["fundamental"]
chk("fundamental rating present", fr["available"] is True)
chk("rating band = Buy", fr["band"] == "Buy")
chk("rating dated", fr["as_of"] == "2026-07-01")
chk("rating grade OBSERVED (consensus, not our opinion)",
    fr["grade"] == V4.OBSERVED)
chk("target still withheld on free tier",
    v["ratings"]["target"]["available"] is False)
chk("target reason names premium",
    "premium" in v["ratings"]["target"]["reason"])
chk("surprise history flows through",
    len(v["financials"]["surprises"]) == 1)
chk("forward consensus withheld (gated)",
    v["financials"]["forward_consensus"]["available"] is False)

print("\npremium key: target and expected return appear")
v = V4.build(snap, estimates=EST_PREMIUM)
tg = v["ratings"]["target"]
chk("target present", tg["available"] is True)
chk("target mean 1100", tg["mean"] == 1100)
chk("expected return derived", tg["expected_return_pct"] is not None)
chk("expected return sign matches (target vs price)",
    (tg["expected_return_pct"] > 0) == (1100 > (v["price"] or 0)))

print("\nDATA HOLD event refuses a rating even with a good feed")
hold = _load(t0)
hold["catalyst"] = dict(hold.get("catalyst") or {},
                        event_kind="primary_release",
                        event_dt="2099-01-01T21:00:00+00:00",
                        verification={"is_results_disclosure": False,
                                      "reason": "unparseable"})
# report_time after that event so it reads as released-but-unverified
v = V4.build(hold, estimates=EST_PREMIUM,
             report_time="2099-01-02T00:00:00+00:00")
chk("event is DATA HOLD", v["event"]["state"] == EV.DATA_HOLD)
chk("fundamental rating refused despite feed",
    v["ratings"]["fundamental"]["available"] is False)
chk("target refused in DATA HOLD",
    v["ratings"]["target"]["available"] is False)
chk("flash is carried", v["flash"] is not None)

print("\npeers")
v = V4.build(snap, estimates=EST_FREE, peers=None)
chk("no peer set -> withheld", v["valuation"]["peers"]["available"] is False)
v = V4.build(snap, estimates=EST_FREE,
             peers={"rows": [{"ticker": "CRM", "pe": 42.0}]})
chk("peer rows -> shown", v["valuation"]["peers"].get("rows") is not None)

print("\n%d/%d checks passed" % (_pass, _pass + _fail))
sys.exit(1 if _fail else 0)
