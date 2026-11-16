"""Published-stat logic in docs/index.html, run in Node.

The Desk cards, the earnings vol badge, the callout headline and the
Performance tab's "Tradeable" filter all quote measured numbers. These tests
extract the real functions and feed them payloads shaped like today's
uoa_edge / uoa_scorecard / uoa_research / scan_outcomes / setup_outcomes /
earnings_ideas / earnings_vol_exec, so the wording follows the data: no
"edge" when the 95% range includes zero, no "EV" on an implied-vs-actual gap.
Skipped when Node is not installed."""

import json
import os

from flow_site_js_test import HTML, _run, pytestmark  # noqa: F401  (skip marker)

STATS = {
    "edge": {
        "direction_split": {"put_buy": {"status": "active", "n": 6951, "ev": 0.33, "hit": 54}}
    },
    "scorecard": {"pockets": {"put_buys": {"bootstrap": {"ci95": [-0.143, 0.842]}}}},
    "research": {
        "attribution": {
            "books": {
                "put_buy": {
                    "status": "active",
                    "flow_alpha": -0.004,
                    "flow_alpha_ci95": [-0.41, 0.402],
                }
            }
        }
    },
    "scans": {
        "overall": {
            "status": "active",
            "n": 1484,
            "win_rate": 43,
            "ev": -1.81,
            "profit_factor": 0.66,
            "hold_d": 5,
        },
        "by_scan": {"stockbee": {"status": "active", "n": 716, "profit_factor": 0.57}},
    },
    "setups": {"grades": {"A+": {"status": "active", "n": 266, "win_rate": 44, "avg": -0.9}}},
}

DESK = ["_sgn", "_nfmt", "deskPutFlowRead", "deskMomentumRead"]


def _desk(expr):
    return _run(
        DESK, "const ST = " + json.dumps(STATS) + ";\nconsole.log(JSON.stringify(" + expr + "));"
    )


def test_put_flow_is_not_called_an_edge_when_range_includes_zero():
    r = _desk("deskPutFlowRead(ST)")
    assert r["edge"] is False
    assert r["badge"] == "54% hit · not a proven edge"
    t = r["text"]
    assert "54%" in t and "+0.33%" in t and "6,951" in t
    assert "−0.14% to +0.84%" in t and "includes zero" in t
    assert "adds about nothing (0.00%)" in t and "own drift" in t
    for stale in ("57%", "0.88", "20.5", "positive edge"):
        assert stale not in t


def test_put_flow_edge_only_when_range_clears_zero():
    r = _desk(
        "deskPutFlowRead(Object.assign({}, ST, {scorecard: {pockets: {put_buys: "
        "{bootstrap: {ci95: [0.1, 0.6]}}}}, research: {}}))"
    )
    assert r["edge"] is True and r["badge"].endswith("measured edge")
    assert "stays above zero" in r["text"] and "drift" not in r["text"]


def test_put_flow_missing_source_returns_nothing():
    assert _desk("deskPutFlowRead({})") is None
    r = _desk("deskPutFlowRead({edge: ST.edge})")
    assert "no confidence range" in r["text"] and r["edge"] is False


def test_momentum_read_quotes_scan_and_setup_outcomes():
    m = _desk("deskMomentumRead(ST)")
    assert m["screens"] == (
        "momentum-long screens run 43% hit / profit factor 0.66 at a 5-day hold (n 1,484)"
    )
    assert m["aplus"] == "A+ setups win 44% (−0.9% avg)"
    assert m["hot"] == "hot weekly screen profit factor 0.57, n=716"
    assert m["loses"] is True
    assert _desk("deskMomentumRead({})") == {"screens": "", "aplus": "", "hot": "", "loses": False}


VOL = {
    "status": "active",
    "n": 226,
    "win_rate": 73,
    "ev": 2.8,
    "n_reconstructed": 226,
}
EXEC = {"expression_registry": {"execution_challenger": {"name": None, "status": "NONE"}}}


def _vol(args):
    return _run(["volIdeaEvLabel"], "console.log(JSON.stringify(volIdeaEvLabel(" + args + ")));")


def test_vol_badge_is_an_implied_vs_actual_gap_not_ev():
    r = _vol('"vol_rich", ' + json.dumps(VOL) + ", " + json.dumps(EXEC))
    assert r["text"] == "implied move beat actual by 2.8 pts avg (backfilled, costs untested)"
    assert "EV" not in r["text"] and "not a trade return" in r["tip"]


def test_vol_badge_variants():
    cheap = dict(VOL, ev=1.4, n_reconstructed=10)
    r = _vol('"vol_cheap", ' + json.dumps(cheap) + ", null")
    assert (
        r["text"]
        == "actual move beat implied by 1.4 pts avg (10 of 226 backfilled, costs untested)"
    )
    tested = {"expression_registry": {"execution_challenger": {"status": "VALIDATED"}}}
    exact = dict(VOL, n_reconstructed=0)
    r = _vol('"vol_rich", ' + json.dumps(exact) + ", " + json.dumps(tested))
    assert r["text"] == "implied move beat actual by 2.8 pts avg"
    assert _vol('"post_report_drift", ' + json.dumps(VOL) + ", null") is None
    assert _vol('"vol_rich", {"status": "accruing", "n": 12}, null') is None


def test_tradeable_needs_n30_and_range_above_zero():
    def c(n, ci, tradeable=True):
        return {
            "alpha_confidence": {"tradeable": tradeable, "n": n},
            "rich": {"n": n, "ci95": ci},
        }

    cases = [
        c(120, [0.05, 0.9]),
        c(29, [0.05, 0.9]),
        c(120, [-0.2, 0.9]),
        c(120, None),
        c(120, [0.05, 0.9], tradeable=False),
    ]
    out = _run(
        ["perfTradeable"],
        "console.log(JSON.stringify(" + json.dumps(cases) + ".map(perfTradeable)));",
    )
    assert out == [True, False, False, False, False]


def test_callout_peak_hits_are_shares_of_tracked():
    out = _run(
        ["coShare"], "console.log(JSON.stringify([coShare(62, 205), coShare(0, 0), coShare(5)]));"
    )
    assert out == ["30%", "—", "—"]


def test_old_hard_coded_stats_are_gone():
    with open(HTML, encoding="utf-8") as f:
        src = f.read()
    for stale in (
        "57% hit",
        "+0.88%",
        "n≈20.5k",
        "≈20,500",
        "win just 20%",
        "profit factor 0.64, n=292",
        "6 scans/day",
        ":05/:35 grid",
        "5:33 PM ET post-close",
        "+1d hit rate · dir-adj",
    ):
        assert stale not in src, stale
    # the callout headline leads with the final result, not peak counts
    i = src.index('"Median final result')
    assert i < src.index('"Touched +50% at some point')
    assert os.path.exists(HTML)
