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
    # trading_chart returns a bare None when there are too few completed
    # sessions to draw — a name weeks past its listing. Omitting the chart
    # is the correct outcome there; crashing on the unpack is not.
    res = C.trading_chart(
        mk, levels=ann, sma_set=SMA_V4,
        earnings_dates=(view.get("chart") or {}).get("earnings_dates"),
        spy_closes=spy)
    if not res:
        print("  too few completed sessions to draw a chart — omitted")
        return None, None
    return res


def _artifact_hashes(*paths):
    """sha256 and byte length of each rendered PDF, keyed by basename."""
    import hashlib
    out = {}
    for pth in paths:
        try:
            with open(pth, "rb") as fh:
                blob = fh.read()
            out[os.path.basename(pth)] = {
                "sha256": hashlib.sha256(blob).hexdigest(),
                "bytes": len(blob)}
        except OSError as e:
            out[os.path.basename(pth)] = {"sha256": None, "error": str(e)}
    return out


def verify(out_dir):
    """Re-hash the PDFs in out_dir and compare against the hashes recorded
    in the validation JSON beside them. Prints one line per artifact and
    returns a non-zero exit code on any mismatch or missing record, so a
    stale validation cannot be mistaken for a current one."""
    vals = [f for f in sorted(os.listdir(out_dir))
            if f.endswith("_validation.json")]
    if not vals:
        print("no validation JSON in %s" % out_dir)
        return 2
    bad = 0
    for v in vals:
        with open(os.path.join(out_dir, v)) as fh:
            rec = json.load(fh)
        arts = rec.get("artifacts") or {}
        if not arts:
            print("  %-46s NO HASHES RECORDED (pre-hash run)" % v)
            bad += 1
            continue
        for name, want in sorted(arts.items()):
            got = _artifact_hashes(os.path.join(out_dir, name)).get(name, {})
            ok = got.get("sha256") and got["sha256"] == want.get("sha256")
            bad += 0 if ok else 1
            print("  %-46s %s" % (name, "matches validation" if ok
                                  else "MISMATCH — validation is stale for "
                                       "this PDF"))
        print("  %-46s validated %s" % (v, rec.get("generated_at") or "?"))
    return 1 if bad else 0


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
    # Bind the validation to the exact bytes it validated. Without this a
    # reader comparing file timestamps cannot tell a current validation
    # from one left over beside a newer PDF — and a stale PASS is worse
    # than no PASS. Hashed AFTER both PDFs are written, and re-checkable
    # with `python report_v4_run.py --verify <dir>`.
    result["artifacts"] = _artifact_hashes(core_p, apx_p)
    with open(val_p, "w") as fh:
        json.dump(result, fh, indent=1, default=str, sort_keys=True)

    return {"ticker": ticker, "core": core_p, "appendix": apx_p,
            "validation": val_p, "result": result,
            "event_state": (view.get("event") or {}).get("state"),
            "flash": bool(view.get("flash"))}


def run_for_user(ticker, user_id="", out_dir=None):
    """CI entry point for a user-requested v4 report — the site's lookup
    path. Same contract research_live.run_for_user honoured for v3:
    build, refuse to ship anything that fails its own validation, then
    upload core + appendix to the requester's private Storage, falling
    back to the public archive. The validation JSON (with the PDF hashes
    it is bound to) ships beside them so a reader can prove the pair."""
    import datetime as dt
    ticker = ticker.upper().strip()
    out_dir = out_dir or "out_v4_user"
    print("=" * 62)
    print("EQUITY RESEARCH v4: %s (user %s)"
          % (ticker, user_id or "<public archive>"))
    print("=" * 62)
    res = _print(run(ticker, out_dir))
    if not res["result"]["ok"]:
        mut = res["result"].get("mutation_tests") or {}
        why = ", ".join(res["result"]["blocking_failures"]) or (
            "mutation suite unproven: %s"
            % (mut.get("note") or mut.get("error") or "see validation JSON")
            if not mut.get("all_checks_proven") else "see validation JSON")
        raise SystemExit("v4 package failed validation; nothing uploaded: "
                         + why)

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d_%H%M")
    # Only the CORE report claims the My Reports row. The appendix and
    # the validation JSON are sidecars under the same filename stem —
    # linking the appendix too inserted a second identical ticker entry
    # whose newer timestamp made the preview open the appendix instead of
    # the report. The worker derives the appendix link from the stem.
    parts = [(res["core"], "research_%s_%s.pdf" % (ticker, stamp), True),
             (res["appendix"], "research_%s_%s_appendix.pdf"
              % (ticker, stamp), False),
             (res["validation"], "research_%s_%s_validation.json"
              % (ticker, stamp), False)]
    try:
        from report_archive import archive, upload_user_report
        for path, name, link in parts:
            with open(path, "rb") as fh:
                blob = fh.read()
            uploaded = False
            if user_id:
                uploaded = upload_user_report(blob, name, user_id,
                                              ticker, "research", link=link)
            if not uploaded:
                archive(blob, name)
                print("  Archived publicly: %s" % name)
    except Exception as e:
        print("  archive/upload failed: %s" % e)
    return 0


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
    ap.add_argument("ticker", nargs="?")
    ap.add_argument("--out", default="out_v4")
    ap.add_argument("--no-spy", action="store_true")
    ap.add_argument("--user-id", default="",
                    help="deliver to this user's private Storage (CI); "
                         "falls back to the public archive")
    ap.add_argument("--for-user", action="store_true",
                    help="run the site lookup path: build, gate, upload")
    ap.add_argument("--verify", metavar="DIR",
                    help="re-hash the PDFs in DIR against the validation "
                         "JSON beside them and exit")
    a = ap.parse_args()
    if a.verify:
        return verify(a.verify)
    if not a.ticker:
        ap.error("a ticker is required unless --verify is given")
    if a.for_user or a.user_id:
        return run_for_user(a.ticker, a.user_id, a.out)
    res = _print(run(a.ticker, a.out, not a.no_spy))
    return 0 if res["result"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
