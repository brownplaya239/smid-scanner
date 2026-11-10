"""Accuracy rules for the Hypothesis Lab (hypothesis_lab.py).

Pins the 2026-11 audit fixes: no open-interest features (next-day OI is not
known at scan time), one alert per contract per ET day, direction-adjusted
grading of directional flow only, an embargo before the out-of-sample
slice, and the Benjamini-Hochberg multiple-testing gate.
"""

import hypothesis_lab as HL
import uoa_research as R
from uoa_research_test import _alert, synthetic_ledger


def test_no_open_interest_atoms():
    s = _alert(1, oi={"status": "confirmed", "basis": "next_day"}, tags=["Into ERN", "Block"])
    atoms = HL._atoms(s, ("Into ERN", "Block"), lambda d: "swing")
    assert not [a for a in atoms if a.startswith("oi:")]
    assert "tag:Into ERN" in atoms and "dir:bullish" in atoms


def test_graded_alerts_collapse_relogs_and_grade_direction_only():
    put = _alert(1, side="put_buyer", exc=-4.0, contract="O:P")
    relog = dict(put, id="relog", flagged_at="2026-10-05T19:00:00+00:00")
    call = _alert(2, side="call_buyer", exc=3.0, contract="O:C")
    seller = _alert(3, side="put_seller", exc=5.0, contract="O:S")
    mixed = _alert(4, side="mixed", exc=5.0, contract="O:M")
    g = HL.graded_alerts([put, relog, call, seller, mixed])
    assert len(g) == 2  # one put alert + one call alert; seller / mixed not graded
    assert sorted(x["v"] for x in g) == [3.0, 4.0]  # the put that fell is a +4 win


def test_bh_select_controls_the_family():
    fam = [{"name": "a", "p": 0.001}, {"name": "b", "p": 0.02}, {"name": "c", "p": 0.5}]
    assert HL.bh_select(fam, q=0.10) == {"a", "b"}
    assert HL.bh_select([{"name": "x", "p": 0.2}], q=0.10) == set()


def test_run_lab_embargoes_the_oos_slice_and_marks_bh():
    lab = HL.run_lab(synthetic_ledger())
    assert lab["status"] == "active"
    s = lab["sample"]
    graded = HL.graded_alerts(synthetic_ledger())
    assert lab["graded"] == len(graded)
    assert s["train_n"] + s["oos_n"] + s["embargoed_n"] == len(graded)
    assert s["embargo_sessions"] == R.EMBARGO_SESSIONS
    sessions = sorted({g["date"] for g in graded})
    train_last = max(g["date"] for g in graded[: s["train_n"]])
    gap = sessions.index(s["oos_from"]) - sessions.index(train_last)
    assert gap == R.EMBARGO_SESSIONS + 1
    assert lab["multiple_testing"]["se"] == "clustered by flag date"
    assert all("bh_pass" in sv for sv in lab["survivors"])
    assert not any("oi:" in sv["hypothesis"] for sv in lab["survivors"])


def test_run_lab_finds_a_planted_edge_and_it_passes_bh():
    ledger = synthetic_ledger(per_day=150)
    for s in ledger:
        r5 = s["returns"].get(5)
        # puts bought into earnings: the stock falls 4pp more (a put-buyer win)
        if r5 and s["direction"] == "bearish" and "Into ERN" in s["tags"]:
            r5["excess"] -= 4.0
            r5["exc_c"] -= 4.0
            r5["ret"] -= 4.0
    lab = HL.run_lab(ledger)
    # (the synthetic cap / liquidity / Into-ERN columns are collinear, so
    # equivalent cohorts tie; the tagged one must be among the BH passes)
    found = [sv for sv in lab["survivors"] if "tag:Into ERN" in sv["hypothesis"]]
    assert found and found[0]["oos_lift_pp"] > 2 and found[0]["bh_pass"]
    assert found[0]["oos"]["days"] >= 10
    assert lab["multiple_testing"]["bh_survivors"] >= 1
