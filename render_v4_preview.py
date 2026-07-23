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

import report_v4 as R4
import report_v4_model as V4

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
    obj = pickle.load(io.open(os.path.join(cache, "%s.pkl" % ticker), "rb"))
    return obj[0] if isinstance(obj, tuple) else obj


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--cache", default=".snapcache_v4")
    ap.add_argument("--variant", default="full",
                    choices=("free", "withkey", "full"))
    ap.add_argument("--out", default="out_v4")
    a = ap.parse_args()

    snap = load(a.ticker, a.cache)
    est = {"free": None, "withkey": EST_WITHKEY, "full": EST_FULL}[a.variant]
    peers = PEERS if a.variant == "full" else None
    view = V4.build(snap, estimates=est, peers=peers)

    os.makedirs(os.path.join(a.out, "png"), exist_ok=True)
    pdf = os.path.join(a.out, "%s_%s.pdf" % (a.ticker, a.variant))
    R4.build_core(snap, view, pdf)

    doc = fitz.open(pdf)
    print("pages=%d  ->  %s" % (doc.page_count, pdf))
    fills = []
    for i, page in enumerate(doc, 1):
        pm = page.get_pixmap(dpi=120)
        png = os.path.join(a.out, "png", "%s_%s_%d.png"
                           % (a.ticker, a.variant, i))
        pm.save(png)
        # rough content-fill: fraction of the page above the last text line
        txt = page.get_text("blocks")
        maxy = max((b[3] for b in txt), default=0)
        fills.append("p%d %d%%" % (i, round(100.0 * maxy / page.rect.height)))
    print("  fill:", " ".join(fills))
    return 0


if __name__ == "__main__":
    sys.exit(main())
