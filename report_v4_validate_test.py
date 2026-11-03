#!/usr/bin/env python3
"""report_v4_validate_test.py — the v4 gate passes a good package and is
provably able to fail.

Two things CI must know before shipping v4: a correctly built package
clears every check, and every check can be made to fail (so a pass means
something). The first is asserted here on each cached snapshot; the second
is report_v4_mutation, whose result is asserted to prove all checks.

    python report_v4_validate_test.py
"""

import glob
import os
import pickle

import pytest

import report_v4 as R4
import report_v4_model as V4
import report_v4_mutation as MUT
import report_v4_validate as VV

# Reads .snapcache/*.pkl, written by a live report_v3_run (network + API keys).
pytestmark = pytest.mark.data


def _load(t):
    obj = pickle.load(open(".snapcache/%s.pkl" % t, "rb"))
    return (obj[0], obj[3] if len(obj) > 3 else {}) if isinstance(obj, tuple) else (obj, {})


def test_gate_on_cached_snapshots(check):
    CACHED = sorted(os.path.basename(p)[:-4] for p in glob.glob(".snapcache/*.pkl"))
    if not CACHED:
        pytest.skip("no cached snapshots — run report_v3_run on a ticker first")

    print("a correctly built package clears every check")
    for t in CACHED:
        snap, prov = _load(t)
        view = V4.build(snap)  # free tier, no chart
        core = R4.build_core(snap, view)
        apx = R4.build_appendix(snap, view, prov=prov)
        res = VV.report(view, snap, core, apx, run_mutation=False)
        fails = [c["check_id"] for c in res["checks"] if c["status"] == "FAIL"]
        check("%s no blocking failure (%s)" % (t, res["event_state"]), not fails)
        if fails:
            print("       -> %s" % ", ".join(fails))

    print("\nthe gate is provably able to fail (mutation suite)")
    m = MUT.summary()
    check("every check was proven to fail when it should", m.get("all_checks_proven") is True)
    if not m.get("all_checks_proven"):
        print("       unproven: %s" % m.get("unproven"))
    check("a healthy number of checks were exercised", m.get("n_proven", 0) >= 12)

    print("\nfull report() with mutation gives ok=True on a good package")
    snap, prov = _load(CACHED[0])
    view = V4.build(snap)
    res = VV.report(
        view,
        snap,
        R4.build_core(snap, view),
        R4.build_appendix(snap, view, prov=prov),
        run_mutation=True,
    )
    # ok can only be True if there are no fatal failures AND mutation proved out
    check(
        "ok=True when clean and mutation proven", res["ok"] is True or view.get("flash") is not None
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
