#!/usr/bin/env python3
"""render_v4_preview.py — render the v4 core report to PDF + PNGs for
visual inspection during development. NOT the production runner (that is
report_v4_run, built in a later slice and gated on review).

    python render_v4_preview.py NOW [--cache .snapcache_v4] [--variant full]

variants:
  free    — no estimate feed, no peers (the honest local/default state)
  withkey — a free-tier Finnhub key: consensus + surprises, target gated
  full    — a premium key: adds target + forward estimates, plus peers

The estimate/peer payloads for withkey/full are SYNTHETIC — shaped like
the provider's real output — so the layout can be inspected without a key.
The `free` variant uses only the real snapshot, which is what CI renders.
"""

import argparse
import io
import os
import pickle
import sys

import fitz  # PyMuPDF

import report_chart_v3 as C
import report_v4 as R4
import report_v4_model as V4
import research_snapshot as rs

# v4 wants the classic 20/50/200 set, not v3's 9/21/50/200.
SMA_V4 = ((20, "#e08a1e"), (50, "#1a7f4b"), (200, "#b3261e"))

# Synthetic provider records, shaped like estimates_provider output.
EST_WITHKEY = {
    "configured": True, "provider": "finnhub",
    "recommendation": {"strong_buy": 15, "buy": 33, "hold": 5, "sell": 1,
                       "strong_sell": 0, "score": 1.8, "band": "Buy",
                       "as_of": "2026-07-01"},
    "price_target": None,
    "coverage": {"price_target": "premium-gated",
                 "eps_estimate": "premium-gated"},
    "surprises": [{"period": "2026-06-30", "actual": 3.9, "estimate": 3.75,
                   "surprise_pct": 4.0},
                  {"period": "2026-03-31", "actual": 3.5, "estimate": 3.55,
                   "surprise_pct": -1.4}],
}
EST_FULL = dict(EST_WITHKEY,
                price_target={"mean": 1100, "high": 1250, "low": 900,
                              "n_analysts": 30, "as_of": "2026-07-18"},
                eps_estimate_next={"avg": 4.1, "high": 4.35, "low": 3.9,
                                   "period": "2026-09-30"},
                rev_estimate_next={"avg": 3450.0, "period": "2026-09-30"},
                coverage={"price_target": "ok", "eps_estimate": "ok"})
PEERS = {"rows": [{"ticker": "CRM", "pe": 42.3}, {"ticker": "WDAY", "pe": 38.0},
                  {"ticker": "TEAM", "pe": None}],
         "source": "finnhub /stock/peers + /stock/metric"}


def load(ticker, cache):
    """Returns (snap, prov). The pickle is (snap, alt, recs, prov); prov
    carries the raw market series the chart needs under _mk."""
    obj = pickle.load(io.open(os.path.join(cache, "%s.pkl" % ticker), "rb"))
    if isinstance(obj, tuple):
        return obj[0], (obj[3] if len(obj) > 3 else {})
    return obj, {}


def build_chart(ticker, snap, prov, view, want_spy=True):
    """Build the page-5 technical chart the way report_v4_run will: the raw
    bar series from prov, 20/50/200 averages, verified earnings markers, and
    the SPY series for the relative-strength panel when it can be fetched."""
    mk = (prov or {}).get("_mk") or {}
    mk.setdefault("ticker", ticker)
    if not mk.get("completed_closes"):
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
        try:
            import research_live as RL
            spy = (RL.fetch_market("SPY") or {}).get("closes")
        except Exception as e:
            print("  SPY unavailable (%s) — RS panel will be omitted" % e)
    return C.trading_chart(
        mk, levels=ann, sma_set=SMA_V4,
        earnings_dates=(view.get("chart") or {}).get("earnings_dates"),
        spy_closes=spy)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--cache", default=".snapcache_v4")
    ap.add_argument("--variant", default="full",
                    choices=("free", "withkey", "full"))
    ap.add_argument("--out", default="out_v4")
    ap.add_argument("--no-spy", action="store_true",
                    help="skip the SPY fetch (RS panel omitted)")
    a = ap.parse_args()

    snap, prov = load(a.ticker, a.cache)
    est = {"free": None, "withkey": EST_WITHKEY, "full": EST_FULL}[a.variant]
    peers = PEERS if a.variant == "full" else None
    view = V4.build(snap, estimates=est, peers=peers)

    chart_png, chart_meta = build_chart(a.ticker, snap, prov, view,
                                        want_spy=not a.no_spy)

    os.makedirs(os.path.join(a.out, "png"), exist_ok=True)
    pdf = os.path.join(a.out, "%s_%s.pdf" % (a.ticker, a.variant))
    R4.build_core(snap, view, pdf, chart_png=chart_png, chart_meta=chart_meta)

    apx = os.path.join(a.out, "%s_%s_appendix.pdf" % (a.ticker, a.variant))
    R4.build_appendix(snap, view, apx, estimates=est, prov=prov)

    for label, path, tag in (("core", pdf, ""), ("appendix", apx, "apx")):
        doc = fitz.open(path)
        print("%s pages=%d  ->  %s" % (label, doc.page_count, path))
        for i, page in enumerate(doc, 1):
            pm = page.get_pixmap(dpi=120)
            name = "%s_%s%s_%d.png" % (a.ticker, a.variant,
                                       "_" + tag if tag else "", i)
            pm.save(os.path.join(a.out, "png", name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
