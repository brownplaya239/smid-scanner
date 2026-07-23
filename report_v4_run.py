#!/usr/bin/env python3
"""report_v4_run.py — build the Equity Research v4 package for one ticker.

    python report_v4_run.py NOW [--out out_v4] [--no-spy]

Emits the four artefacts and prints the validation result:

    <TICKER>_equity_research_v4.pdf           the six-page core report
    <TICKER>_equity_research_v4_appendix.pdf  the evidence/methodology appendix
    <TICKER>_equity_research_v4_validation.json  the gate's structured result

This is the production entrypoint, but it is deliberately NOT wired into
the site's lookup workflow — v3 still ships there. The first live v4 output
is meant to be reviewed before any cutover.

Estimates come from Finnhub (estimates_provider), which fails closed with
no FINNHUB_API_KEY: the consensus rating and 12-month target are then
WITHHELD, not invented — the honest local/default state. In CI, with the
secret set, a free-tier key adds the consensus and the surprise history and
a premium key adds the target and forward estimates.

The PDFs are written whether or not validation passes: a package that fails
its own gate is exactly what a reviewer needs to see. The exit code is what
reports pass/fail.
"""

import argparse
import io
import json
import os
import sys

import estimates_provider as EP
import report_chart_v3 as C
import report_v4 as R4
import report_v4_model as V4
import research_snapshot as rs

SMA_V4 = ((20, "#e08a1e"), (50, "#1a7f4b"), (200, "#b3261e"))


def _snapshot(ticker):
    """Fetch the snapshot, honouring TD_SNAP_CACHE so a layout change can be
    re-rendered against the identical snapshot instead of a three-minute
    refetch — the same contract report_v3_run uses."""
    import research_live as RL
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
    return snap, prov


def _chart(ticker, snap, prov, view, want_spy=True):
    """The page-5 technical chart: candles with SMA 20/50/200, volume, RSI,
    verified earnings markers, and RS vs SPY when the benchmark is fetched.
    Built from the raw bar series in prov, which the view does not carry."""
    mk = (prov or {}).get("_mk") or {}
    mk.setdefault("ticker", ticker)
    if not mk.get("completed_closes"):
        print("  no bar series in the snapshot — chart omitted")
        return None, None
    lv = snap.get("levels") or {}
    px = rs.fv(lv.get("price_used")) or view.get("price")
    ann = {}
    for k, lab in (("ma50", "50-day average"), ("ma20", "20-day average")):
        v = rs.fv(lv.get(k))
        if v and px and v > px:
            ann["confirmation"] = {"value": v, "label": lab}
            break
    if rs.fv(lv.get("support")):
        ann["boundary"] = {"value": rs.fv(lv["support"])}
    spy = None
    if want_spy:
        import research_live as RL
        try:
            spy = (RL.fetch_market("SPY") or {}).get("closes")
        except Exception as e:
            print("  SPY series unavailable (%s) — RS panel omitted" % e)
    return C.trading_chart(
        mk, levels=ann, sma_set=SMA_V4,
        earnings_dates=(view.get("chart") or {}).get("earnings_dates"),
        spy_closes=spy)


def run(ticker, out_dir="out_v4", want_spy=True):
    import report_v4_validate as VV
    os.makedirs(out_dir, exist_ok=True)
    ticker = ticker.upper().strip()

    snap, prov = _snapshot(ticker)

    # Estimates + peers. Both fail closed with no key, which withholds the
    # consensus rating, the target and the peer table rather than faking one.
    estimates = EP.fetch_estimates(ticker, report_time=snap.get("report_time"))
    peers = EP.fetch_peers(ticker)
    if not estimates.get("configured"):
        print("  estimates: not configured (%s) — rating/target withheld"
              % (estimates.get("reason") or "no key"))

    view = V4.build(snap, estimates=estimates, peers=peers)
    chart_png, chart_meta = _chart(ticker, snap, prov, view, want_spy)

    stem = "%s_equity_research_v4" % ticker
    core_p = os.path.join(out_dir, stem + ".pdf")
    apx_p = os.path.join(out_dir, stem + "_appendix.pdf")
    val_p = os.path.join(out_dir, stem + "_validation.json")

    core = R4.build_core(snap, view, core_p, chart_png=chart_png,
                         chart_meta=chart_meta)
    apx = R4.build_appendix(snap, view, apx_p, estimates=estimates,
                            prov=prov)
    result = VV.report(view, snap, core, apx, estimates=estimates,
                       run_mutation=True)
    with open(val_p, "w") as fh:
        json.dump(result, fh, indent=1, default=str, sort_keys=True)

    return {"ticker": ticker, "core": core_p, "appendix": apx_p,
            "validation": val_p, "result": result,
            "event_state": (view.get("event") or {}).get("state"),
            "flash": bool(view.get("flash"))}


def _print(res):
    r = res["result"]
    print("\n%s  (%s)" % (res["ticker"], res["event_state"]))
    for k in ("core", "appendix", "validation"):
        print("  %-11s %s" % (k, res[k]))
    if res["flash"]:
        print("  %-11s %s" % ("mode", "DATA HOLD — flash, no rating"))
    print("  %-11s %s" % ("result", "PASS" if r["ok"] else "PROBLEMS"))
    for c in r["checks"]:
        if c["status"] in ("FAIL", "WARN"):
            print("     %-6s %-28s %s"
                  % (c["status"], c["check_id"], c["observed"]))
    if r["blocking_failures"]:
        print("     blocking: %s" % ", ".join(r["blocking_failures"]))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--out", default="out_v4")
    ap.add_argument("--no-spy", action="store_true")
    a = ap.parse_args()
    res = _print(run(a.ticker, a.out, not a.no_spy))
    return 0 if res["result"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
