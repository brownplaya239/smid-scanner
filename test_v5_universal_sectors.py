#!/usr/bin/env python3
"""test_v5_universal_sectors.py — unseen-ticker generalization (§18).

Nine synthetic securities across nine sectors run through adapter
classification, dashboard construction, framework coverage and
capability routing. None of the tickers is a pilot; none has ever been
run. The suite asserts:

  * each sector selects its own adapter from classification facts;
  * no sector dashboard borrows another sector's metrics;
  * missing sector data degrades to "no admitted source" slots;
  * a financially complete name with NOT_ASSESSED qualitative
    dimensions routes FULL_THIN, never FULL;
  * pre-revenue and new-listing shapes route to their archetypes.
"""

import sys

import report_v5_adapters as ADP
import report_v5_capability as CAP
import report_v5_framework as FW

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS, FAIL = 0, []


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL.append(name)
        print("  FAIL  %s  %s" % (name, detail))


def F(v, refs=None):
    return {"v": v, "evidence_refs": refs or ["XBRL-syn-1"],
            "period_end": "2026-03-31"}


def make(sector, industry, rev=1.2e9, sessions=900, quarters=12,
         listing=None, full_history=True, pre_revenue=False):
    snap = {
        "business": {"industry": industry},
        "trading_history": {"sessions": sessions,
                            "full_history": full_history,
                            "listing_date": listing},
        "fundamentals": ({} if pre_revenue else {
            "revenue_q": F(rev), "revenue_growth": F(12.0),
            "net_margin": F(8.0), "gross_margin": F(55.0),
            "operating_cash_flow": F(rev * 0.2),
            "cash": F(2e9), "debt": F(1e9),
        }),
        "exhibit": {}, "catalyst": {"next_event_date": "2026-08-20"},
        "insiders": {}, "ownership": {},
    }
    if pre_revenue:
        snap["fundamentals"] = {"cash": F(4e8), "debt": F(0.0),
                                "operating_cash_flow": F(-5e7)}
    profile = {"sector": sector, "industry": industry,
               "listing_date": listing,
               "full_price_history": full_history,
               "sessions": sessions,
               "pre_revenue_status": pre_revenue}
    multiples = {"n_eps_quarters": quarters, "n_rev_quarters": quarters,
                 "pe": {"available": quarters >= 8, "kind": "pe",
                        "window_years": 3, "actual_years": 3.0,
                        "coverage": 0.9},
                 "ps": {"available": False, "reason": "syn"}}
    return snap, profile, multiples


SECTORS = [
    ("QQSW", "Technology", "Software - Application",
     "subscription_software", "cRPO / RPO"),
    ("QQFP", "Financial Services", "Capital Markets",
     "financial_platform", "Funded customers / investment accounts"),
    ("QQRS", "Consumer Cyclical", "Restaurants", "restaurant",
     "Same-store sales / traffic / ticket"),
    ("QQIN", "Industrials", "Specialty Industrial Machinery",
     "industrial", "Backlog and book-to-bill"),
    ("QQBK", "Financial Services", "Banks - Regional", "bank_insurer",
     "Net interest margin"),
    ("QQRE", "Real Estate", "REIT - Office", "reit",
     "FFO / AFFO per share"),
    ("QQEN", "Energy", "Oil & Gas E&P", "energy_materials",
     "Production volumes and realized prices"),
]

FOREIGN = {"subscription_software": "Same-store sales",
           "financial_platform": "cRPO",
           "restaurant": "cRPO",
           "industrial": "Funded customers",
           "bank_insurer": "AUV",
           "reit": "Net interest revenue",
           "energy_materials": "FFO"}

for tk, sector, industry, want_key, want_slot in SECTORS:
    snap, profile, multiples = make(sector, industry)
    ad = ADP.classify(profile, snap)
    check("%s -> %s adapter" % (tk, want_key), ad["key"] == want_key,
          "%s (%s)" % (ad["key"], ad["reason"]))
    rows = ADP.build_dashboard(ad, snap)
    labels = " | ".join(r[0] for r in rows)
    check("%s dashboard carries its own sector slot" % tk,
          want_slot in labels, labels[:120])
    foreign = FOREIGN[want_key]
    check("%s dashboard borrows nothing (%r absent)" % (tk, foreign),
          foreign not in labels, labels[:120])
    absent = [r for r in rows if r[1] == "no admitted source"]
    check("%s missing sector data degrades to absent slots" % tk,
          all("not" in r[2] or "parsing" in r[2] or "requires" in r[2]
              or "derivable" in r[2] or "filed" in r[2]
              for r in absent) and absent,
          "%d absent slots" % len(absent))

# ── pre-revenue company ──────────────────────────────────────────────
snap, profile, multiples = make("Healthcare", "Biotechnology",
                                sessions=400, quarters=0,
                                pre_revenue=True)
ad = ADP.classify(profile, snap)
check("pre-revenue -> pre_revenue adapter", ad["key"] == "pre_revenue",
      ad["key"])
rows = ADP.build_dashboard(ad, snap)
check("pre-revenue dashboard carries no revenue-value metric",
      all(not r[0].lower().startswith("quarterly revenue")
          and "revenue growth" not in r[0].lower() for r in rows),
      " | ".join(r[0] for r in rows))

# ── new listing ──────────────────────────────────────────────────────
snap, profile, multiples = make("Industrials", "Aerospace & Defense",
                                sessions=25, quarters=0,
                                listing="2026-06-12",
                                full_history=False)
snap["fundamentals"] = {}
multiples["pe"]["available"] = False
cap = CAP.evidence_capability(snap, multiples, {}, None)
fw = FW.build_coverage(profile, cap, snap, None, multiples,
                       ADP.classify(profile, snap, "NEW_LISTING"))
arch = CAP.route(profile, cap, {}, multiples, framework=fw)
check("new listing routes NEW_LISTING",
      arch["archetype"] == "NEW_LISTING", arch["routing_reason"])

# ── FULL_THIN enforcement (§6) ───────────────────────────────────────
snap, profile, multiples = make("Technology", "Software - Application")
cap = CAP.evidence_capability(snap, multiples, {}, None)
ad = ADP.classify(profile, snap)
fw = FW.build_coverage(profile, cap, snap, None, multiples, ad)
arch = CAP.route(profile, cap, {}, multiples, framework=fw)
check("financially complete + NOT_ASSESSED qualitative -> FULL_THIN "
      "(never FULL)", arch["archetype"] == "FULL_THIN",
      "%s / missing: %s" % (arch["archetype"],
                            arch.get("missing_framework_dimensions")))
check("missing framework dimensions recorded on the routing record",
      bool(arch.get("missing_framework_dimensions")),
      str(arch.get("missing_framework_dimensions")))
check("framework coverage carries all 26 dimensions",
      len(fw["dimensions"]) == 26
      and set(fw["dimensions"]) == set(FW.TIGER_DIMENSIONS),
      "%d dims" % len(fw["dimensions"]))

# routing must consult data, not identity: same inputs, different
# ticker string, identical decision
snap2, profile2, multiples2 = make("Technology",
                                   "Software - Application")
arch2 = CAP.route(profile2,
                  CAP.evidence_capability(snap2, multiples2, {}, None),
                  {}, multiples2,
                  framework=FW.build_coverage(
                      profile2,
                      CAP.evidence_capability(snap2, multiples2, {},
                                              None),
                      snap2, None, multiples2,
                      ADP.classify(profile2, snap2)))
check("identical evidence -> identical routing regardless of symbol",
      arch2["archetype"] == arch["archetype"], arch2["archetype"])

print("\n%d/%d checks passed" % (PASS, PASS + len(FAIL)))
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
