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

import pytest

import sec_exhibit as SX

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
    "with more than $5 million in ACV.</p>"
)


def test_kpis_from_release_prose(check):
    k = SX.parse_kpis(PROSE)

    check("subscription revenue value", k["subscription_revenue"]["value"] == 3877e6)
    check("subscription revenue growth", k["subscription_revenue"]["growth_yoy_pct"] == 24.5)
    check("cRPO value (billions)", k["crpo"]["value"] == 13.2e9)
    check("cRPO not confused with RPO", k["crpo"]["value"] != k["rpo"]["value"])
    check("RPO value (billions)", k["rpo"]["value"] == 29.0e9)
    check("AI ACV floor", k["ai_acv"]["value"] == 1e9 and k["ai_acv"]["basis"] == "crossed (floor)")
    check("$1M net-new ACV deal count", k["acv_over_1m_net_new_deals"]["value"] == 123)
    check(
        "$5M ACV customer count survives the stray space",
        k["acv_over_5m_customers"]["value"] == 658,
    )
    check(
        "every KPI carries its raw provenance string",
        all(isinstance(v, dict) and v.get("raw") for v in k.values()),
    )

    print("\nno KPIs in a document that states none")
    check("empty prose -> no KPIs", SX.parse_kpis("<p>The company did well.</p>") == {})


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
