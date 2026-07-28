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
    if growth is not None:
        fu["revenue_growth"] = {"v": growth, "evidence_refs": ["R1"]}
    if margin is not None:
        fu["net_margin"] = {"v": margin}
    if rev is not None:
        fu["revenue_q"] = {"v": rev}
    if fcf is not None:
        fu["free_cash_flow"] = {"v": fcf, "evidence_refs": ["F1"]}
    lv = {}
    if px is not None:
        lv["price_used"] = {"v": px}
    if ma200 is not None:
        lv["ma200"] = {"v": ma200}
    return {"fundamentals": fu, "levels": lv, "exhibit": {}}


V4 = {"ratings": {"tactical": {"available": False}}}

# growth claim + market-not-paying counterevidence
r = C.build(snap(growth=25.0, rev=1e9, px=90.0, ma200=100.0), V4)
g = [c for c in r["claims"] if "growth" in c["claim"]][0]
check("growth claim carries structure counterevidence",
      any("200-day" in x for x in g["counterevidence"]), g)
check("claims carry an invalidation", bool(g["invalidation"]))
check("claim with counterevidence downgrades to medium",
      g["confidence"] == C.MEDIUM)

# decline claim: outlier margin reframed, never cited as strength
r2 = C.build(snap(growth=-3.0, margin=78.0, rev=1e9), V4)
d = [c for c in r2["claims"] if "DECLINED" in c["claim"]][0]
check("outlier margin reframed as likely one-time",
      any("one-time" in x for x in d["counterevidence"]), d)
check("outlier margin never cited as 'profitability holds'",
      not any("profitability holds" in x for x in d["counterevidence"]))

# broker-style OCF suppressed
r3 = C.build(snap(growth=5.0, rev=1e9, fcf=1.9e9), V4)
check("conversion above outlier bar produces NO claim",
      not any("self-funds" in c["claim"] for c in r3["claims"]))
check("...and the exclusion is recorded in searched",
      any("balance-sheet flows" in x for x in r3["searched"]))

# valuation gap wording is bounded
SC = {"available": True, "spot": 50.0, "band_ref": {"window_years": 3},
      "rows": [{"leg": "base", "price": 100.0}]}
r4 = C.build(snap(rev=1e9), V4, SC)
v = [c for c in r4["claims"] if "median" in c["claim"]][0]
check("cheap side renders as a bounded discount (50%, not 100%+)",
      "50% discount" in v["claim"], v["claim"])

# empty record says what was searched
r5 = C.build(snap(), V4)
check("zero claims -> note naming what was searched",
      not r5["claims"] and "Searched" in (r5["note"] or ""), r5["note"])

print("\n%d/%d checks passed" % (PASS, PASS + len(FAIL)))
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
