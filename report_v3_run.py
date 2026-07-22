#!/usr/bin/env python3
"""report_v3_run.py — build the v3 brief for one ticker from live data.

    python report_v3_run.py ISRG [--out out_v3] [--no-spy]

Emits four artefacts and prints the validation result. The PDF is written
whether or not validation passes: a brief that fails its own checks is
still the thing you need to look at to understand why, and hiding it
would make the failure harder to diagnose, not less real. What the exit
code reports is whether it passed.

Nothing here fetches anything the research layer does not already fetch,
apart from the SPY series the relative-strength panel needs — and when
that is unavailable the panel is dropped and labelled rather than drawn
from a substitute.
"""

import argparse
import json
import os
import sys

import report_chart_v3 as C
import report_v3 as R3
import report_v3_model as M


def run(ticker, out_dir="out_v3", want_spy=True):
    import research_live as RL

    os.makedirs(out_dir, exist_ok=True)
    snap, alt, recs, prov = RL.build_snapshot(ticker)
    if alt and not snap.get("sentiment"):
        snap["sentiment"] = alt

    mk = (prov or {}).get("_mk") or {}
    mk.setdefault("ticker", ticker)
    spy = None
    if want_spy:
        try:
            spy = (RL.fetch_market("SPY") or {}).get("closes")
        except Exception as e:
            print("  SPY series unavailable (%s) — the relative-strength "
                  "panel will be omitted and labelled" % e)

    mini = C.mini_chart(mk) if mk.get("closes") else None
    full = C.full_chart(mk, spy) if mk.get("closes") else None
    if not mini:
        print("  no bar series in the snapshot — charts omitted")

    res = R3.build_all(snap, out_dir=out_dir, chart_png=mini,
                       chart_full=full, recs=recs, prov=prov)
    print("\n%s" % ticker)
    for k in ("core", "appendix", "evidence", "validation"):
        print("  %-11s %s" % (k, res[k]))
    print("  %-11s %s" % ("result", "PASS" if res["ok"] else "PROBLEMS"))
    for p in res["problems"]:
        print("     [%s] %s: %s" % (p["stage"], p["code"], p["detail"]))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--out", default="out_v3")
    ap.add_argument("--no-spy", action="store_true")
    a = ap.parse_args()
    res = run(a.ticker.upper(), a.out, not a.no_spy)
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
