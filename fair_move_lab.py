"""
fair_move_lab.py — point-in-time forecast challengers for TickerDesk
Fair Move. The research target (reviewer, 2026-08): "what is the best
point-in-time forecast of ABSOLUTE earnings movement?" — not "which
condor should we sell?".

Every graded vol event in the idea log carries (implied, |realized|).
For each event, each challenger produces a forecast of |realized| using
ONLY events dated strictly before it (chronological walk-forward — no
event ever informs its own forecast). Challengers, all deliberately
simple and interpretable:

  v1_ticker_median   the ticker's own prior |realized| median
                     (>=3 prior events, else global fallback) —
                     the current display definition
  cap_shrunk         w = n/(n+4): ticker median shrunk toward the
                     market-cap cohort median
  sector_shrunk      same, toward the sector cohort
  market_anchored    implied x trailing global median move-ratio —
                     "trust the market, correct its average bias"
  blend              0.5 * cap_shrunk + 0.5 * market_anchored

Scoring per challenger:
  mae        mean |forecast - |realized||  (pp of underlying)
  bias       mean (forecast - |realized|)
  edge_sign  % of events where sign(implied - forecast) matched
             sign(implied - |realized|) — does the forecast call
             SELL/BUY VOL correctly? (the product-relevant metric)
  half-split stability (first vs second half of the record)

PROMOTION RULE (predeclared): a challenger replaces v1 in the display
only if it beats v1's MAE by >= 5% in BOTH halves AND does not lose on
edge_sign. Until then the lab publishes standings and the display
stays v1. Nothing is tuned per-run; the definitions above are frozen.

Outputs docs/reports/fair_move_lab.json. No API calls.

    python fair_move_lab.py            # full run
    python fair_move_lab.py --dry-run  # print, don't write
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from statistics import mean, median

from earnings_vol_engine import _load_events
from trade_desk_validation import _load

_BASE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(_BASE, "docs", "reports", "fair_move_lab.json")

LAB_VERSION = "fair_move_lab_v1"
SHRINK_K = 4
MIN_PRIOR_TICKER = 3
MIN_PRIOR_COHORT = 10
MAE_IMPROVE = 0.05      # challenger must beat v1 MAE by >= 5%


def _cap_band(e):
    return (e.get("feat") or {}).get("cap") or "?"


def build_rows():
    ev = _load_events()
    rows = []
    for t, events in ev.items():
        for e in events:
            rows.append({"ticker": e["ticker"], "date": e["date"],
                         "implied": e["implied"],
                         "realized": round(e["ratio"] * e["implied"], 3),
                         "cap": _cap_band(e),
                         "sector": e.get("sector") or "unknown"})
    rows.sort(key=lambda r: r["date"])
    return rows


def forecasts_for(rows):
    """Walk forward; return list of (row, {challenger: forecast})."""
    out = []
    for i, r in enumerate(rows):
        prior = rows[:i]
        # strictly-before by DATE (same-night events don't inform
        # each other)
        prior = [p for p in prior if p["date"] < r["date"]]
        if len(prior) < MIN_PRIOR_COHORT:
            continue
        g_real = [p["realized"] for p in prior]
        g_med = median(g_real)
        tick = [p["realized"] for p in prior
                if p["ticker"] == r["ticker"]]
        cap = [p["realized"] for p in prior if p["cap"] == r["cap"]]
        sec = [p["realized"] for p in prior
               if p["sector"] == r["sector"]]
        ratios = [p["realized"] / p["implied"] for p in prior
                  if p["implied"]]
        g_ratio = median(ratios)

        t_med = median(tick) if len(tick) >= MIN_PRIOR_TICKER else None
        v1 = t_med if t_med is not None else g_med
        n_t = len(tick)
        w = n_t / (n_t + SHRINK_K)
        cap_med = median(cap) if len(cap) >= MIN_PRIOR_COHORT else g_med
        sec_med = median(sec) if len(sec) >= MIN_PRIOR_COHORT else g_med
        cap_shrunk = w * (t_med if t_med is not None else cap_med) \
            + (1 - w) * cap_med
        sec_shrunk = w * (t_med if t_med is not None else sec_med) \
            + (1 - w) * sec_med
        mkt = r["implied"] * g_ratio
        out.append((r, {
            "v1_ticker_median": v1,
            "cap_shrunk": cap_shrunk,
            "sector_shrunk": sec_shrunk,
            "market_anchored": mkt,
            "blend": 0.5 * cap_shrunk + 0.5 * mkt,
        }))
    return out


def score(pairs, names):
    def stats(sel):
        out = {}
        for name in names:
            errs = [f[name] - r["realized"] for r, f in sel]
            sign_hits = sum(
                1 for r, f in sel
                if (r["implied"] - f[name]) *
                   (r["implied"] - r["realized"]) > 0)
            out[name] = {
                "n": len(sel),
                "mae": round(mean(abs(e) for e in errs), 3),
                "bias": round(mean(errs), 3),
                "edge_sign_pct": round(100 * sign_hits / len(sel)),
            }
        return out
    half = len(pairs) // 2
    return {"overall": stats(pairs),
            "half1": stats(pairs[:half]),
            "half2": stats(pairs[half:])}


def run(dry=False):
    rows = build_rows()
    pairs = forecasts_for(rows)
    names = ["v1_ticker_median", "cap_shrunk", "sector_shrunk",
             "market_anchored", "blend"]
    result = {"generated": datetime.now(timezone.utc)
              .isoformat(timespec="seconds"),
              "lab_version": LAB_VERSION,
              "events_total": len(rows), "events_scored": len(pairs)}
    if len(pairs) < 40:
        result["status"] = "insufficient_history"
    else:
        sc = score(pairs, names)
        result["standings"] = sc
        # predeclared promotion evaluation
        v1 = sc["overall"]["v1_ticker_median"]
        promo = {}
        for name in names:
            if name == "v1_ticker_median":
                continue
            c_all, c1, c2 = (sc["overall"][name], sc["half1"][name],
                             sc["half2"][name])
            v1_1, v1_2 = (sc["half1"]["v1_ticker_median"],
                          sc["half2"]["v1_ticker_median"])
            promo[name] = {
                "beats_mae_both_halves": bool(
                    c1["mae"] <= v1_1["mae"] * (1 - MAE_IMPROVE)
                    and c2["mae"] <= v1_2["mae"] * (1 - MAE_IMPROVE)),
                "edge_sign_not_worse": bool(
                    c_all["edge_sign_pct"] >= v1["edge_sign_pct"]),
            }
            promo[name]["promoted"] = (
                promo[name]["beats_mae_both_halves"]
                and promo[name]["edge_sign_not_worse"])
        result["promotion"] = promo
        promoted = [k for k, v in promo.items() if v["promoted"]]
        result["display_definition"] = (
            promoted[0] if promoted else "v1_ticker_median")
    result["honesty"] = (
        "Walk-forward on the SELECTED vol-event universe (events exist "
        "because the earnings loop flagged them) — this measures "
        "forecast quality within the product's own cohort, not across "
        "all earnings. Same-night events share a forecast information "
        "set but not each other's outcomes. Challenger definitions are "
        "frozen; the promotion rule was declared before scoring.")
    if not dry:
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1)
    if result.get("standings"):
        for name in names:
            o = result["standings"]["overall"][name]
            print(f"{name:18s} mae={o['mae']:.3f} bias={o['bias']:+.3f} "
                  f"edge_sign={o['edge_sign_pct']}%")
        print("display:", result["display_definition"])
    else:
        print("status:", result.get("status"))
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    run(dry=ap.parse_args().dry_run)
