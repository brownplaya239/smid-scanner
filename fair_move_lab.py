"""
fair_move_lab.py — point-in-time scoring of TickerDesk Fair Move and its
challengers. The research target (reviewer, 2026-08): "what is the best
point-in-time forecast of ABSOLUTE earnings movement?" — not "which
condor should we sell?".

v2 (2026-11 accuracy audit). v1 scored a "ticker median" built from the
idea log's OWN prior events (>= 3 needed; no ticker had more than 4, so
~400 of 401 scored events silently used the global cohort median) — not
the Fair Move the cards displayed. v2 scores the figures the site
actually showed, as of the read, and nothing else:

  fair_mean        ACTIVE display since 2026-11: the ticker's MEAN
                   |reaction| over its last <=8 reports (earnings_edge
                   fair_move), logged with the last pre-event read.
                   A straddle prices a mean |move|, so this is the
                   like-for-like forecast of what the option is worth.
  fair_median      the display until 2026-11: the median of the same
                   reports. Point-in-time from the idea log (since
                   2026-11) or from data/setup_context.jsonl, which has
                   logged earnings_edge's realized_med every session
                   since 2026-08-26 (latest row in [D-10, D]).
  market_anchored  event-only implied x trailing median |realized|/implied
                   over strictly earlier events — "trust the market,
                   correct its average bias"
  cap_shrunk       w = n/(n+4) (n = the ticker's report count): fair_mean
                   shrunk toward the trailing mean |realized| of its
                   market-cap band
  blend            0.5 * cap_shrunk + 0.5 * market_anchored

Target = the graded |reaction| (abs_move, grade v3). Reports of unknown
time are graded over two sessions and are left out here (the forecasts
are one-session numbers).

Scoring per forecaster (pp of the stock price):
  rmse       root mean squared error — the PROPER score for a forecast of
             a mean (squared error is minimized by the mean; absolute
             error is minimized by the median and would reward a forecast
             that sits below the straddle's fair value)
  mae        mean absolute error (context)
  bias       mean (forecast - |realized|)
  edge_sign  % of events where sign(implied - forecast) matched
             sign(implied - |realized|) — does the forecast call
             SELL/BUY VOL correctly? (the product-relevant metric)
  half-split stability (first vs second half of the shared events)

Challengers are compared on the SAME events (all forecasts available).

PROMOTION RULE (predeclared, v2): a challenger replaces the active
display only if it beats the active model's RMSE by >= 5% in BOTH halves
AND does not lose on edge_sign. Until then the lab publishes standings
and the display stays fair_mean.

Outputs docs/reports/fair_move_lab.json. No API calls.

    python fair_move_lab.py            # full run
    python fair_move_lab.py --dry-run  # print, don't write
"""

from __future__ import annotations

import argparse
import json
import math
import os
from datetime import UTC, date, datetime, timedelta
from statistics import mean, median

from earnings_vol_engine import _load_events

_BASE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(_BASE, "docs", "reports", "fair_move_lab.json")
CONTEXT_PATH = os.path.join(_BASE, "data", "setup_context.jsonl")

LAB_VERSION = "fair_move_lab_v2"
ACTIVE = "fair_mean"
PREVIOUS = "fair_median"
CHALLENGERS = ("fair_median", "market_anchored", "cap_shrunk", "blend")
SHRINK_K = 4
MIN_PRIOR_COHORT = 10
MIN_SCORED = 40
RMSE_IMPROVE = 0.05  # challenger must beat the active RMSE by >= 5%
CONTEXT_LOOKBACK_D = 10

DEFINITIONS = {
    "fair_mean": "ticker's MEAN |reaction| over its last <=8 reports, as "
    "displayed (active since 2026-11)",
    "fair_median": "ticker's MEDIAN |reaction| over the same reports, as displayed until 2026-11",
    "market_anchored": "event-only implied x trailing median "
    "|realized|/implied (earlier events only)",
    "cap_shrunk": "fair_mean shrunk toward its market-cap band's trailing "
    "mean |realized| (w = n/(n+4))",
    "blend": "0.5 x cap_shrunk + 0.5 x market_anchored",
}


def _cap_band(e):
    return (e.get("feat") or {}).get("cap") or "?"


def load_context_medians(path=CONTEXT_PATH):
    """{ticker: [(date, realized_med), ...] sorted} from setup_context.jsonl
    (earnings_edge's realized_med as it stood each session)."""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                t, d, m = r.get("ticker"), r.get("date"), r.get("realized_med")
                if t and d and isinstance(m, (int, float)) and m > 0:
                    out.setdefault(t, []).append((str(d)[:10], float(m)))
    except OSError:
        return {}
    for v in out.values():
        v.sort()
    return out


def context_median(ctx, ticker, event_date):
    """Latest realized_med logged for `ticker` in [D-10, D] — a read that
    still predates the report (a row dated D was logged from the previous
    evening's earnings_edge)."""
    rows = ctx.get(ticker) or []
    try:
        lo = (date.fromisoformat(event_date) - timedelta(days=CONTEXT_LOOKBACK_D)).isoformat()
    except ValueError:
        return None
    best = None
    for d, m in rows:
        if lo <= d <= event_date:
            best = m
    return best


def build_rows(events=None, ctx=None):
    """One row per graded one-session vol event, with the point-in-time
    displayed figures attached. Returns (rows, n_two_session_excluded)."""
    ev = _load_events() if events is None else events
    ctx = load_context_medians() if ctx is None else ctx
    rows, excluded = [], 0
    for evs in ev.values():
        for e in evs:
            if (e.get("frame_sessions") or 1) != 1:
                excluded += 1
                continue
            med, med_src = e.get("realized_med"), "logged"
            if e.get("basis") != "exact" or med is None:
                med, med_src = context_median(ctx, e["ticker"], e["date"]), "setup_context"
            rows.append(
                {
                    "ticker": e["ticker"],
                    "date": e["date"],
                    "implied": e["implied"],
                    "realized": e["realized"],
                    "fair_mean": e.get("fair_move") if e.get("basis") == "exact" else None,
                    "fair_median": med,
                    "median_src": med_src if med is not None else None,
                    "n_reports": e.get("n_reports"),
                    "cap": _cap_band(e),
                }
            )
    rows.sort(key=lambda r: r["date"])
    return rows, excluded


def forecasts_for(rows):
    """Walk forward; return [(row, {forecaster: value or None})]. Trailing
    quantities use events dated strictly before the row (same-night
    events never inform each other)."""
    out = []
    for i, r in enumerate(rows):
        prior = [p for p in rows[:i] if p["date"] < r["date"]]
        f = {"fair_mean": r["fair_mean"], "fair_median": r["fair_median"]}
        ratios = [p["realized"] / p["implied"] for p in prior if p["implied"]]
        f["market_anchored"] = (
            r["implied"] * median(ratios) if len(ratios) >= MIN_PRIOR_COHORT else None
        )
        cap = [p["realized"] for p in prior if p["cap"] == r["cap"]]
        cap_mean = mean(cap) if len(cap) >= MIN_PRIOR_COHORT else None
        if r["fair_mean"] is not None and cap_mean is not None:
            n_t = r.get("n_reports") or 0
            w = n_t / (n_t + SHRINK_K)
            f["cap_shrunk"] = w * r["fair_mean"] + (1 - w) * cap_mean
        else:
            f["cap_shrunk"] = None
        f["blend"] = (
            0.5 * f["cap_shrunk"] + 0.5 * f["market_anchored"]
            if f["cap_shrunk"] is not None and f["market_anchored"] is not None
            else None
        )
        out.append((r, f))
    return out


def score_one(pairs, name):
    """Scores for one forecaster over (row, forecasts) pairs where it exists."""
    sel = [(r, f[name]) for r, f in pairs if f.get(name) is not None]
    if not sel:
        return {"n": 0}
    errs = [v - r["realized"] for r, v in sel]
    hits = sum(1 for r, v in sel if (r["implied"] - v) * (r["implied"] - r["realized"]) > 0)
    return {
        "n": len(sel),
        "rmse": round(math.sqrt(mean(e * e for e in errs)), 3),
        "mae": round(mean(abs(e) for e in errs), 3),
        "bias": round(mean(errs), 3),
        "edge_sign_pct": round(100 * hits / len(sel)),
    }


def score(pairs, names):
    half = len(pairs) // 2
    return {
        part: {n: score_one(sel, n) for n in names}
        for part, sel in (("overall", pairs), ("half1", pairs[:half]), ("half2", pairs[half:]))
    }


def run(dry=False, events=None, ctx=None):
    rows, excluded = build_rows(events, ctx)
    pairs = forecasts_for(rows)
    names = (ACTIVE, *CHALLENGERS)
    result = {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "lab_version": LAB_VERSION,
        "display_definition": ACTIVE,
        "definitions": DEFINITIONS,
        "events_total": len(rows) + excluded,
        "events_two_session_excluded": excluded,
    }
    # Active display, scored point-in-time on every event that logged it.
    act = score_one(pairs, ACTIVE)
    result["active"] = dict(
        act,
        name=ACTIVE,
        status="scored" if act["n"] >= MIN_SCORED else "accruing",
        activates_at=MIN_SCORED,
    )
    # The display until 2026-11, scored point-in-time — the number behind
    # the old "under-forecasts by ~Xpp" claims, measured honestly.
    prev = score_one(pairs, PREVIOUS)
    result["previous_display"] = dict(
        prev,
        name=PREVIOUS,
        status="scored" if prev["n"] >= MIN_SCORED else "accruing",
        sources={
            s: sum(1 for r in rows if r["median_src"] == s) for s in ("logged", "setup_context")
        },
    )
    shared = [(r, f) for r, f in pairs if all(f.get(n) is not None for n in names)]
    result["events_scored"] = len(shared)
    if len(shared) < MIN_SCORED:
        result["status"] = "insufficient_history"
    else:
        result["status"] = "scored"
        sc = score(shared, names)
        result["standings"] = sc
        a_all, a1, a2 = (sc[p][ACTIVE] for p in ("overall", "half1", "half2"))
        promo = {}
        for name in CHALLENGERS:
            c_all, c1, c2 = (sc[p][name] for p in ("overall", "half1", "half2"))
            promo[name] = {
                "beats_rmse_both_halves": bool(
                    c1["rmse"] <= a1["rmse"] * (1 - RMSE_IMPROVE)
                    and c2["rmse"] <= a2["rmse"] * (1 - RMSE_IMPROVE)
                ),
                "edge_sign_not_worse": bool(c_all["edge_sign_pct"] >= a_all["edge_sign_pct"]),
            }
            promo[name]["promoted"] = (
                promo[name]["beats_rmse_both_halves"] and promo[name]["edge_sign_not_worse"]
            )
        result["promotion"] = promo
        promoted = [k for k, v in promo.items() if v["promoted"]]
        result["display_definition"] = promoted[0] if promoted else ACTIVE
    result["honesty"] = (
        "Point-in-time: every forecast is the figure the site showed (or "
        "would have shown) before the report, never a value computed with "
        "hindsight. Scored on the SELECTED vol-event universe (events exist "
        "because the earnings loop flagged them) — forecast quality within "
        "the product's own cohort, not across all earnings. Challengers are "
        "compared on the same events; definitions are frozen and the "
        "promotion rule was declared before scoring."
    )
    if not dry:
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1)
    for blk in ("active", "previous_display"):
        b = result[blk]
        if b.get("n"):
            print(
                f"{b['name']:16s} n={b['n']} rmse={b['rmse']:.3f} mae={b['mae']:.3f} "
                f"bias={b['bias']:+.3f} edge_sign={b['edge_sign_pct']}% ({b['status']})"
            )
        else:
            print(f"{b['name']:16s} n=0 ({b['status']})")
    print("standings:", result["status"], "| display:", result["display_definition"])
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    run(dry=ap.parse_args().dry_run)
