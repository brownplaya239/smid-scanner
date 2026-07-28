#!/usr/bin/env python3
"""test_v5_claims.py — the argument builder's honesty guards."""

import sys

import report_v5_claims as C

PASS, FAIL = 0, []


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL.append(name)
        print("  FAIL  %s  %s" % (name, detail))


def snap(growth=None, margin=None, rev=None, fcf=None, px=None,
         ma200=None):
    fu = {}
    import datetime as _dt
    recent = (_dt.date.today() - _dt.timedelta(days=30)).isoformat()
    if growth is not None:
        fu["revenue_growth"] = {"v": growth, "evidence_refs": ["R1"]}
    if margin is not None:
        fu["net_margin"] = {"v": margin}
    if rev is not None:
        fu["revenue_q"] = {"v": rev, "evidence_refs": ["R2"],
                           "period_end": recent}
    if fcf is not None:
        fu["free_cash_flow"] = {"v": fcf, "evidence_refs": ["F1"]}
    lv = {}
    if px is not None:
        lv["price_used"] = {"v": px}
    if ma200 is not None:
        lv["ma200"] = {"v": ma200}
    return {"fundamentals": fu, "levels": lv, "exhibit": {}}


V4 = {"ratings": {"tactical": {"available": False}}}

# SAME-PROPOSITION RULE: technical price action is NOT counterevidence
# to revenue durability — the 200-day must not appear against a
# fundamental growth claim.
r = C.build(snap(growth=25.0, rev=1e9, px=90.0, ma200=100.0), V4)
g = [c for c in r["claims"] if "growth" in c["claim"]][0]
check("technical structure NEVER counters a fundamental claim",
      not any("200-day" in x for x in g["counterevidence"]), g)
check("claims carry an invalidation", bool(g["breaks_if"]))
check("same-domain counterevidence downgrades to medium",
      C.build(snap(growth=25.0, margin=-5.0, rev=1e9), V4
              )["claims"][0]["confidence"] == C.MEDIUM)

# decline claim: a one-time gain is NOT counterevidence to a decline
r2 = C.build(snap(growth=-3.0, margin=78.0, rev=1e9), V4)
d = [c for c in r2["claims"] if "DECLINED" in c["claim"]][0]
check("one-time gain never counters a revenue decline",
      not any("one-time" in x or "78" in x
              for x in d["counterevidence"]), d)
check("outlier margin never cited as 'profitability holds'",
      not any("profitability holds" in x for x in d["counterevidence"]))

# broker-style OCF suppressed
r3 = C.build(snap(growth=5.0, rev=1e9, fcf=1.9e9), V4)
check("conversion above outlier bar produces NO claim",
      not any("self-funds" in c["claim"] for c in r3["claims"]))
check("...and the exclusion is recorded in searched",
      any("balance-sheet flows" in x for x in r3["searched"]))

# valuation gap wording is bounded
import datetime as _dt2
SC = {"available": True, "spot": 50.0,
      "band_ref": {"window_years": 3, "kind": "pe",
                   "window_end": (_dt2.date.today()
                                  - _dt2.timedelta(days=3)).isoformat()},
      "rows": [{"leg": "base", "price": 100.0}]}
r4 = C.build(snap(rev=1e9), V4, SC)
v = [c for c in r4["claims"] if "median" in c["claim"]][0]
check("cheap side renders as a bounded discount (50%, not 100%+)",
      "50% discount" in v["claim"], v["claim"])

# empty record says what was searched
r5 = C.build(snap(), V4)
check("zero candidates -> note naming what was searched",
      not r5["claims"] and "searched" in (r5["note"] or ""), r5["note"])

# ── contract v2: statuses + publication gate ─────────────────────────
r6 = C.build(snap(growth=25.0, rev=1e9), V4)
g6 = [c for c in r6["claims"] if c["claim_id"] == "growth-above-bar"][0]
check("no counterevidence -> SUPPORTED", g6["status"] == C.SUPPORTED)
check("claims carry lifecycle fields",
      g6["reunderwrite_when"] and g6["next_checkpoint"]
      and g6["maximum_valid_until"])
check("technical-free claim carries mechanism + implication",
      g6["mechanism"] and (g6["financial_implication"]
                           or g6["valuation_implication"]))

r7 = C.build(snap(growth=25.0, rev=1e9, px=90.0, ma200=100.0), V4)
g7 = [c for c in r7["claims"] if c["claim_id"] == "growth-above-bar"][0]
check("tape weakness no longer conflicts a fundamental claim",
      g7["status"] == C.SUPPORTED, g7["status"])

# stale evidence fails the gate with the reason recorded
import report_v5_claims as _C
stale_snap = snap(growth=25.0, rev=1e9)
stale_snap["fundamentals"]["revenue_q"]["period_end"] = "2025-01-01"
r8 = C.build(stale_snap, V4)
rej = [x for x in r8["rejected"] if x["claim_id"] == "growth-above-bar"]
check("stale critical evidence -> rejected as STALE with the gate named",
      rej and rej[0]["status"] == C.STALE
      and any("stale" in f for f in rej[0]["failed_gates"]),
      str(rej))

# a sourced consensus expectation renders; never fabricated
EST = {"recommendation": {"band": "Buy", "as_of": "2026-07-01",
                          "strong_buy": 5, "buy": 5, "hold": 2,
                          "sell": 0, "strong_sell": 0}}
r9 = C.build(snap(growth=25.0, rev=1e9), V4, None, EST)
g9 = [c for c in r9["claims"] if c["claim_id"] == "growth-above-bar"][0]
check("consensus expectation carries its source",
      g9["market_expectation"] and "finnhub" in
      (g9["market_expectation_source"] or ""), g9["market_expectation_source"])
r10 = C.build(snap(growth=25.0, rev=1e9), V4)
g10 = [c for c in r10["claims"] if c["claim_id"] == "growth-above-bar"][0]
check("no consensus -> no expectation text (business insight path)",
      g10["market_expectation"] is None)

print("\n%d/%d checks passed" % (PASS, PASS + len(FAIL)))
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
