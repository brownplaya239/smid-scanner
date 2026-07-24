#!/usr/bin/env python3
"""sec_exhibit_test.py — the release-prose KPI extractor, without a network.

parse_kpis is the v4.1 fix that brings a SaaS name back from DATA HOLD: it
reads subscription revenue, cRPO/RPO, AI ACV and the deal/customer counts
straight from the issuer's earnings-release prose. These fixtures mirror
the shape of a real 8-K exhibit — including the stray intra-word space the
HTML->text conversion introduces ("cu stomers") — so the counts must be
matched by anchoring on the dollar phrase, not the noun.

    python sec_exhibit_test.py
"""

import sys

import sec_exhibit as SX

_pass = _fail = 0


def chk(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print("  PASS  %s" % name)
    else:
        _fail += 1
        print("  FAIL  %s" % name)


PROSE = (
    "<p>Subscription revenues of $3,877 million in Q2 2026, representing "
    "24.5% year-over-year growth, 23% in constant currency. "
    "Current remaining performance obligations of $13.20 billion as of "
    "Q2 2026, representing 21% year-over-year growth. "
    "Remaining performance obligations of $29.0 billion as of Q2 2026, "
    "representing 21% year-over-year growth. "
    "ServiceNow AI crossed $1 billion in annual contract value in Q2 2026. "
    "The company had 123 transactions over $1 million in net new annual "
    "contract value in Q2 2026, and ended the quarter with 658 cu stomers "
    "with more than $5 million in ACV.</p>")

k = SX.parse_kpis(PROSE)

chk("subscription revenue value", k["subscription_revenue"]["value"] == 3877e6)
chk("subscription revenue growth", k["subscription_revenue"]["growth_yoy_pct"]
    == 24.5)
chk("cRPO value (billions)", k["crpo"]["value"] == 13.2e9)
chk("cRPO not confused with RPO", k["crpo"]["value"] != k["rpo"]["value"])
chk("RPO value (billions)", k["rpo"]["value"] == 29.0e9)
chk("AI ACV floor", k["ai_acv"]["value"] == 1e9
    and k["ai_acv"]["basis"] == "crossed (floor)")
chk("$1M net-new ACV deal count", k["acv_over_1m_net_new_deals"]["value"] == 123)
chk("$5M ACV customer count survives the stray space",
    k["acv_over_5m_customers"]["value"] == 658)
chk("every KPI carries its raw provenance string",
    all(isinstance(v, dict) and v.get("raw") for v in k.values()))

print("\nno KPIs in a document that states none")
chk("empty prose -> no KPIs", SX.parse_kpis("<p>The company did well.</p>")
    == {})

print("\n%d/%d checks passed" % (_pass, _pass + _fail))
sys.exit(1 if _fail else 0)
