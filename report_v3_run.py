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
    # A full snapshot costs three minutes of network. TD_SNAP_CACHE lets a
    # layout or wording change be re-rendered against the identical
    # snapshot instead of refetching, which also makes the output
    # byte-comparable between runs.
    cache = os.environ.get("TD_SNAP_CACHE")
    cpath = os.path.join(cache, "%s.pkl" % ticker) if cache else None
    if cpath and os.path.exists(cpath):
        import pickle
        with open(cpath, "rb") as fh:
            snap, alt, recs, prov = pickle.load(fh)
        print("  [cache] snapshot reused from %s" % cpath)
    else:
        snap, alt, recs, prov = RL.build_snapshot(ticker)
        if cpath:
            import pickle
            os.makedirs(cache, exist_ok=True)
            try:
                with open(cpath, "wb") as fh:
                    pickle.dump((snap, alt, recs, prov), fh)
            except Exception as e:
                print("  [cache] not saved: %s" % e)
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

    return build_from_snapshot(snap, recs, prov, out_dir, mk=mk, spy=spy)


def build_from_snapshot(snap, recs, prov, out_dir, mk=None, spy=None):
    """Render the four v3 artefacts from a snapshot that already exists.

    Split out of run() so the live CI path and this CLI build the brief
    through the same code. A second copy of the chart-annotation and
    build_all wiring is a second thing to drift, and the whole point of
    shipping v3 to the site is that what a user downloads is what we
    tested here."""
    import research_snapshot as _rs
    mk = mk if mk is not None else ((prov or {}).get("_mk") or {})
    _lv = snap.get("levels") or {}
    _px = _rs.fv(_lv.get("price_used")) or _rs.fv(
        (snap.get("price") or {}).get("last"))
    _ann = {}
    for _k, _lab in (("ma50", "50-day average"), ("ma20", "20-day average")):
        _v = _rs.fv(_lv.get(_k))
        if _v and _px and _v > _px:
            _ann["confirmation"] = {"value": _v, "label": _lab}
            break
    if _rs.fv(_lv.get("support")):
        _ann["boundary"] = {"value": _rs.fv(_lv["support"])}
    # page 3 gets the chart a reader trades from; the 12-month structural
    # view moves to the appendix
    trading, tmeta = (C.trading_chart(mk, levels=_ann)
                      if mk.get("closes") else (None, None))
    structural = C.full_chart(mk, spy) if mk.get("closes") else None
    if not trading:
        print("  no bar series in the snapshot - charts omitted")

    res = R3.build_all(snap, out_dir=out_dir, chart_png=None,
                       chart_full=trading, chart_structural=structural,
                       recs=recs, prov=prov, chart_meta=tmeta,
                       led=(prov or {}).get("_ledger"))
    return res


def _report(ticker, res):
    print("\n%s" % ticker)
    for k in ("core", "appendix", "evidence", "validation"):
        print("  %-11s %s" % (k, res[k]))
    print("  %-11s %s" % ("result", "PASS" if res["ok"] else "PROBLEMS"))
    for c in res["checks"]:
        if c["status"] in ("FAIL", "WARN"):
            print("     %-6s %-30s %s"
                  % (c["status"], c["check_id"], c["observed"]))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--out", default="out_v3")
    ap.add_argument("--no-spy", action="store_true")
    a = ap.parse_args()
    res = _report(a.ticker.upper(), run(a.ticker.upper(), a.out,
                                        not a.no_spy))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
