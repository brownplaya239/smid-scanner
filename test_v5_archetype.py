#!/usr/bin/env python3
"""test_v5_archetype.py — slice-3 proof: the router picks the right
shape for each pilot profile, records its reasons, exposes overrides,
and the contract check bites in BOTH directions."""

import sys

import report_v5_archetype as A

PASS, FAIL = 0, []


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL.append(name)
        print("  FAIL  %s  %s" % (name, detail))


MATURE = {"trading_history": {"listing_date": "2012-06-29",
                              "sessions": 511, "full_history": True},
          "fundamentals": {"revenue_q": {"period_end": "2026-06-30",
                                         "v": 1.0}}}
LISTING = {"trading_history": {"listing_date": "2026-06-12",
                               "sessions": 29, "full_history": False},
           "fundamentals": {}}
NO_EVENT = {"state": "PRE-RELEASE", "flash": None}
HOLDING = {"state": "DATA HOLD", "flash": {"headline": "x"},
           "reasons": ["exhibit unread"]}
BANDS_OK = {"pe": {"available": True}, "ps": {"available": False},
            "n_eps_quarters": 14, "n_rev_quarters": 14}
BANDS_NONE = {"pe": {"available": False, "reason": "r"},
              "ps": {"available": False, "reason": "r"},
              "n_eps_quarters": 14, "n_rev_quarters": 14}

# ── routing ──────────────────────────────────────────────────────────
r = A.decide(MATURE, NO_EVENT, BANDS_OK, has_options=True)
check("mature name routes FULL", r["archetype"] == A.FULL, r)
check("router records reasons", bool(r["reasons"]), r)
check("band ok -> valuation_table becomes REQUIRED",
      "valuation_table" in r["contract"]["required"], r["contract"])

r2 = A.decide(LISTING, NO_EVENT, {"pe": {"available": False},
                                  "ps": {"available": False},
                                  "n_eps_quarters": 0,
                                  "n_rev_quarters": 0})
check("new listing routes NEW_LISTING",
      r2["archetype"] == A.NEW_LISTING, r2["archetype"])
check("NEW_LISTING forbids the scenario table and argument",
      "valuation_table" in r2["contract"]["forbidden"]
      and "argument" in r2["contract"]["forbidden"])

r3 = A.decide(MATURE, HOLDING, BANDS_OK)
check("gate flash routes DATA_HOLD", r3["archetype"] == A.DATA_HOLD)

thin_m = {"pe": {"available": False}, "ps": {"available": True},
          "n_eps_quarters": 2, "n_rev_quarters": 2}
r4 = A.decide(MATURE, NO_EVENT, thin_m)
check("two filed quarters routes THIN", r4["archetype"] == A.THIN,
      r4["archetype"])
check("THIN with a surviving band still requires the scenario table",
      "valuation_table" in r4["contract"]["required"])

r5 = A.decide(MATURE, NO_EVENT, BANDS_NONE, has_options=False)
check("no band -> scenario stays optional, reason recorded",
      "valuation_table" not in r5["contract"]["required"]
      and any("withheld" in x for x in r5["reasons"]))
check("no options -> flow page forbidden with reason",
      "flow_positioning" in r5["contract"]["forbidden"]
      and any("options" in x for x in r5["reasons"]))

# ── override is recorded and attributed, never silent ────────────────
r6 = A.decide(MATURE, NO_EVENT, BANDS_OK, override=A.THIN)
check("override applies and records from/to",
      r6["archetype"] == A.THIN
      and r6["override"] == {"from": A.FULL, "to": A.THIN}, r6["override"])
check("override reason names both archetypes",
      any("OVERRIDDEN to THIN" in x and "FULL" in x for x in r6["reasons"]))

# ── the contract check bites both ways ───────────────────────────────
c = A.CONTRACTS[A.NEW_LISTING]
viol = A.check_rendered_sections(
    {"listing_factsheet": True, "listing_timeline": True,
     "listing_trading": True, "valuation_table": True}, c)
check("forbidden section rendered -> violation",
      viol == ["forbidden section rendered: valuation_table"], viol)
viol2 = A.check_rendered_sections(
    {"listing_factsheet": True, "listing_trading": True}, c)
check("missing required section -> violation",
      viol2 == ["missing required section: listing_timeline"], viol2)
viol3 = A.check_rendered_sections(
    {"listing_factsheet": True, "listing_timeline": True,
     "listing_trading": True}, c)
check("honouring the contract -> no violations", viol3 == [], viol3)

print("\n%d/%d checks passed" % (PASS, PASS + len(FAIL)))
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
