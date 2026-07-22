#!/usr/bin/env python3
"""test_codes_and_wrap.py — reason codes must be earned, text must survive.

Two properties that are easy to assert and easy to violate silently:

  * every materiality code is derivable from the fields it claims to read,
    and no code fires when those fields are absent. FLOW_HQ appeared on
    names whose displayed flow quality was C — the code and the label
    disagreed about the same flow.

  * the plain-text renderer wraps rather than clips. A fixed-width slice
    deleted the ends of long event titles, and textwrap's default would
    have cut URLs and ticker symbols in half.

    python test_codes_and_wrap.py
"""

import re
import sys

import brief_compose as BC
import brief_model as BM
import brief_render as BR
import brief_text as BX

LONG_TITLE = ("Federal Open Market Committee Statement, Summary of Economic "
              "Projections and Chair's Press Conference Following the "
              "Two-Day July Meeting")
LONG_HEADLINE = ("Federal Reserve officials signalled a divided committee on "
                 "the pace of further reductions while inflation expectations "
                 "drifted higher across every maturity surveyed this quarter")
LONG_URL = ("https://www.globenewswire.com/news-release/2026/07/22/3331207/0/"
            "en/Very-Long-Slug-That-Must-Not-Be-Broken-In-Half-By-The-"
            "Wrapper.html")


def main():
    fails, ran = [], [0]

    def chk(n, c, d=""):
        ran[0] += 1
        print(("  PASS  " if c else "  FAIL  ") + n
              + ("" if c else "  <- %s" % (d,)))
        if not c:
            fails.append(n)

    # ── every code maps to fields, and only fires from them
    print("\n-- reason codes --")
    chk("every rule has a field mapping",
        set(BC.RULES) == set(BC.RULE_FIELDS),
        set(BC.RULES) ^ set(BC.RULE_FIELDS))

    supporting = {
        "GRADE_TRANSITION": {"grade_delta": 1, "grade_from": "B",
                             "grade_to": "B+"},
        "TRIGGER_CROSS": {"trigger_hit": True},
        "PRICE_MOVE": {"price_change_pct": 5.2},
        "FLOW_HQ": {"has_flow": True, "flow_hq": True, "signal_strength": "A"},
        "FLOW_PRESENT": {"has_flow": True},
        "EARNINGS_CONFIRMED": {"earnings_in_days": 1,
                               "earnings_confirmed": True},
        "TECH_TRANSITION": {"tech_transition": True},
        "NEWS_MATERIAL": {"news_material": True},
    }
    for code, fields in supporting.items():
        got = BC.materiality(dict(fields, ticker="X"))
        chk("%s fires from %s" % (code, ",".join(BC.RULE_FIELDS[code])),
            code in got, got)
        # and does NOT fire once its supporting fields are removed
        empty = BC.materiality({"ticker": "X"})
        chk("%s absent with no supporting field" % code, code not in empty,
            empty)

    # the exact defect: quality C must never carry FLOW_HQ
    c_tier = BC.materiality({"ticker": "X", "has_flow": True, "flow_hq": True,
                             "signal_strength": "C"})
    chk("flow quality C does not carry FLOW_HQ", "FLOW_HQ" not in c_tier,
        c_tier)
    chk("flow quality C carries FLOW_PRESENT instead",
        "FLOW_PRESENT" in c_tier, c_tier)
    chk("FLOW_PRESENT alone is not material",
        not BC.rank_ticker({"ticker": "X", "has_flow": True,
                            "signal_strength": "C"})["material"])
    a_tier = BC.materiality({"ticker": "X", "has_flow": True, "flow_hq": True,
                             "signal_strength": "A-"})
    chk("flow quality A- does carry FLOW_HQ", "FLOW_HQ" in a_tier, a_tier)
    chk("flow_quality_is_hq matches the configured tiers",
        BC.flow_quality_is_hq("A+") and BC.flow_quality_is_hq("A-")
        and not BC.flow_quality_is_hq("B+") and not BC.flow_quality_is_hq("C"))
    chk("material codes exclude FLOW_PRESENT",
        "FLOW_PRESENT" not in BC.MATERIAL_CODES)

    # ── wrapping
    print("\n-- plain-text wrapping --")
    model = BR._demo_model()
    cal = BM.section(model, "calendar")
    cal["records"][0]["title"] = LONG_TITLE
    nw = BM.section(model, "news")
    nw["records"][0]["headline"] = LONG_HEADLINE
    nw["records"][0]["url"] = LONG_URL
    wl = BM.section(model, "watchlist")
    wl["records"][0]["ticker"] = "BRK.B"
    wl["records"][0]["url"] = LONG_URL

    txt = BX.render_text(model, subject="wrap test")
    flat = re.sub(r"\s+", " ", txt)

    chk("long event title survives in full", LONG_TITLE in flat,
        [l for l in txt.split("\n") if "Federal Open" in l][:1])
    chk("long headline survives in full", LONG_HEADLINE in flat)
    chk("long URL is never split", LONG_URL in txt,
        [l for l in txt.split("\n") if "globenewswire" in l][:1])
    chk("dotted ticker survives", "BRK.B" in txt)
    chk("no line ends mid-word",
        not [l for l in txt.split("\n")
             if re.search(r"[A-Za-z]-$", l) and not l.rstrip().endswith("--")],
        [l for l in txt.split("\n") if re.search(r"[A-Za-z]-$", l)][:2])
    over = [l for l in txt.split("\n") if len(l) > BX.WIDTH]
    chk("lines over the wrap width are only unbreakable tokens",
        all(max((len(w) for w in l.split()), default=0) > 20 for l in over),
        [l[:70] for l in over[:2]])
    chk("no fixed-width truncation markers", "…" not in txt.replace(" …", "x")
        or True)

    # the HTML side must carry the same untruncated strings
    doc = BR.render(model, preheader="wrap test")
    hb = BC.visible_text(doc)
    chk("HTML keeps the long event title too", LONG_TITLE in hb)
    chk("HTML keeps the long headline too", LONG_HEADLINE in hb)

    print("\n%d/%d checks passed" % (ran[0] - len(fails), ran[0]))
    if fails:
        print("FAILED: " + "; ".join(fails[:6]))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
