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

    check(
        "plainly stated figures carry no bound",
        all("bound" not in k[n] for n in ("subscription_revenue", "crpo", "rpo")),
    )
    check("a crossed figure is a floor", k["ai_acv"]["bound"] == "at_least")


# Round-3 re-check: "approximately $10.9 billion" and "More than 120 deals"
# were parsed as exact figures. The words right before the number decide.
@pytest.mark.parametrize(
    "prose,key,value,bound,phrase",
    [
        (
            (
                "Current remaining performance obligations were approximately $10.9 billion"
                " as of Q3 2026, representing 21% year-over-year growth."
            ),
            "crpo",
            10.9e9,
            "approx",
            "approximately",
        ),
        (
            (
                "Current remaining performance obligations of about $10.9 billion,"
                " representing 21% year-over-year growth."
            ),
            "crpo",
            10.9e9,
            "approx",
            "about",
        ),
        (
            # the nearest qualifier to the number wins: "roughly", not "crossed"
            (
                "Current remaining performance obligations crossed roughly $13.2 billion in Q2,"
                " representing 21% year-over-year growth."
            ),
            "crpo",
            13.2e9,
            "approx",
            "roughly",
        ),
        (
            "More than 120 deals over $1 million in net new annual contract value.",
            "acv_over_1m_net_new_deals",
            120,
            "over",
            "more than",
        ),
        (
            "The company closed at least 120 transactions over $1 million in net new ACV.",
            "acv_over_1m_net_new_deals",
            120,
            "at_least",
            "at least",
        ),
        (
            "It ended the quarter with nearly 658 customers with more than $5 million in ACV.",
            "acv_over_5m_customers",
            658,
            "approx",
            "nearly",
        ),
        (
            (
                "Subscription revenues of $3,877 million, representing 24.5% year-over-year growth."
                " The company had in excess of 123 transactions over $1 million in net new ACV."
            ),
            "acv_over_1m_net_new_deals",
            123,
            "over",
            "in excess of",
        ),
        (
            "It had up to 40 customers with more than $5 million in ACV.",
            "acv_over_5m_customers",
            40,
            "under",
            "up to",
        ),
    ],
)
def test_qualifier_before_a_kpi_is_kept_as_its_bound(prose, key, value, bound, phrase):
    k = SX.parse_kpis("<p>%s</p>" % prose)[key]
    assert k["value"] == value
    assert (k.get("bound"), k.get("bound_phrase")) == (bound, phrase)


@pytest.mark.parametrize(
    "prose,key",
    [
        # "year-over-year" in the previous clause is not a bound on the next figure
        (
            (
                "Subscription revenues of $3,877 million, representing 24.5% year-over-year growth."
                " Current remaining performance obligations of $13.20 billion, representing 21%"
                " year-over-year growth."
            ),
            "crpo",
        ),
        # a qualifier in the previous sentence does not carry over
        (
            (
                "Revenue was about flat. Remaining performance obligations of $29.0 billion,"
                " representing 21% year-over-year growth."
            ),
            "rpo",
        ),
        # a qualifier far from the number, with other words between, is not a bound
        (
            (
                "Over the course of the quarter the company had 123 transactions over $1 million"
                " in net new ACV."
            ),
            "acv_over_1m_net_new_deals",
        ),
    ],
)
def test_plain_figures_are_not_given_a_bound(prose, key):
    assert "bound" not in SX.parse_kpis("<p>%s</p>" % prose)[key]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
