"""
earnings_vol_engine.py — Earnings Volatility Alpha Engine, part 1:
signal-level anatomy of the vol_rich / vol_cheap edge.

The 2026-08 review's correction, encoded: what the earnings loop
validated is a FORECAST relationship (implied earnings move tends to
exceed realized), NOT an executable trade. Win rate in premium selling
can conceal catastrophic negative skew, and five same-night short-vol
"wins" can be one giant bet. Before any trade expression is trusted,
this module measures — from the graded idea log, no API calls:

  1. MOVE RATIO distribution per type: |realized| / implied
     (reconstructed as 1 - mv/implied) — median, p75/90/95/99, max,
     and the share of events inside 0.5x / 0.75x / 1.0x implied. This
     is what tells a condor/fly engine where wings belong.
  2. EVENT-NIGHT CLUSTERING: events per night, joint-tail nights
     (>=2 blowthroughs same night), and an EVENT-DATE cluster
     bootstrap CI on EV — the honest version of "n=102".
  3. TAIL / CATASTROPHE metrics on mv: worst, p5, expected shortfall
     at 95/99, blowthrough frequencies (ratio > 1.0 / 1.5 / 2.0).
  4. DECOMPOSITION: EV by implied-size band, cap band, session,
     regime, sector, same-night crowding — min-n gated, to find where
     vol_rich is especially rich (and where it isn't).
  5. FAIR MOVE calibration: per-type median move ratio, published so
     the UI can show TickerDesk Fair Move / Volatility Edge /
     Richness against the ticker's own realized history.
  6. TRADE READINESS verdict: signal_qualified true/false per type
     (date-cluster CI must exclude zero), trade_qualified ALWAYS false
     here — only the option-P&L reconstruction backtester
     (earnings_vol_backtest.py, CI) can flip that.

Outputs docs/reports/earnings_vol.json.

    python earnings_vol_engine.py            # full run
    python earnings_vol_engine.py --dry-run  # print, don't write
"""

from __future__ import annotations

import argparse
import json
import os
import random
from datetime import datetime, timezone
from statistics import mean, median

from trade_desk_validation import _regime_map, _load

_BASE = os.path.dirname(os.path.abspath(__file__))
R = lambda *p: os.path.join(_BASE, *p)
LOG_PATH = R("data", "earnings_ideas_log.json")
OUT_PATH = R("docs", "reports", "earnings_vol.json")

ENGINE_VERSION = "earnings_vol_v1"
MIN_N = 30
COHORT_MIN = 20
BOOT_ITERS = 4000


def _pct(sorted_vals, q):
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, int(round(q / 100 * (len(sorted_vals) - 1))))
    return round(sorted_vals[i], 2)


def _load_events():
    d = _load(LOG_PATH, {}) or {}
    meta = _load(R("docs", "reports", "uoa_meta_cache.json"), {}) or {}
    regimes = _regime_map()
    out = {"vol_rich": [], "vol_cheap": []}
    for i in d.get("ideas") or []:
        t = i.get("type")
        if t not in out or not i.get("result"):
            continue
        imp, mv = i.get("implied"), i.get("mv")
        if not imp or mv is None or imp <= 0:
            continue
        # vol_rich:  mv = implied - |reaction|  -> ratio = 1 - mv/implied
        # vol_cheap: mv = |reaction| - implied  -> ratio = 1 + mv/implied
        ratio = (1 - mv / imp) if t == "vol_rich" else (1 + mv / imp)
        if ratio < 0:
            continue  # malformed grade
        m = meta.get(i.get("t")) or {}
        out[t].append({
            "ticker": i.get("t"), "date": i.get("date"),
            "implied": imp, "mv": mv, "ratio": round(ratio, 3),
            "feat": i.get("feat") or {},
            "sector": m.get("sector") or "unknown",
            "regime": regimes.get(i.get("date"), "unknown"),
        })
    return out


def date_cluster_bootstrap(events, iters=BOOT_ITERS, seed=11):
    """95% CI of mean mv under EVENT-DATE resampling — same-night
    events are one draw, which is the honest unit for earnings vol."""
    nights = {}
    for e in events:
        nights.setdefault(e["date"], []).append(e["mv"])
    keys = list(nights)
    if len(keys) < 8:
        return {"status": "insufficient_nights", "nights": len(keys)}
    rng = random.Random(seed)
    means = []
    for _ in range(iters):
        ys = []
        for _ in range(len(keys)):
            ys.extend(nights[rng.choice(keys)])
        means.append(mean(ys))
    means.sort()
    return {"nights": len(keys),
            "mean": round(mean(e["mv"] for e in events), 2),
            "ci95": [round(means[int(iters * 0.025)], 2),
                     round(means[int(iters * 0.975)], 2)]}


def ratio_distribution(events):
    rs = sorted(e["ratio"] for e in events)
    n = len(rs)
    return {
        "n": n,
        "median": _pct(rs, 50), "p75": _pct(rs, 75), "p90": _pct(rs, 90),
        "p95": _pct(rs, 95), "p99": _pct(rs, 99), "max": _pct(rs, 100),
        "inside_050x": round(100 * sum(1 for r in rs if r <= 0.50) / n),
        "inside_075x": round(100 * sum(1 for r in rs if r <= 0.75) / n),
        "inside_100x": round(100 * sum(1 for r in rs if r <= 1.00) / n),
        "blowthrough_10x": round(100 * sum(1 for r in rs if r > 1.0) / n),
        "blowthrough_15x": round(100 * sum(1 for r in rs if r > 1.5) / n),
        "blowthrough_20x": round(100 * sum(1 for r in rs if r > 2.0) / n),
    }


def tail_metrics(events):
    mvs = sorted(e["mv"] for e in events)   # ascending: worst first
    n = len(mvs)
    k5 = max(1, int(n * 0.05))
    k1 = max(1, int(n * 0.01))
    worst = min(events, key=lambda e: e["mv"])
    return {
        "worst_mv": round(mvs[0], 2),
        "worst_event": {"ticker": worst["ticker"], "date": worst["date"],
                        "implied": worst["implied"],
                        "ratio": worst["ratio"]},
        "p5_mv": _pct(mvs, 5),
        "es95": round(mean(mvs[:k5]), 2),
        "es99": round(mean(mvs[:k1]), 2),
        "note": "mv is in vol-points (implied minus |realized| for "
                "vol_rich) — a SIGNAL-level tail proxy. Real strategy "
                "tails (gap through wings, assignment) need the option "
                "P&L reconstruction and are NOT captured here.",
    }


def clustering(events):
    nights = {}
    for e in events:
        nights.setdefault(e["date"], []).append(e)
    per_night = sorted(len(v) for v in nights.values())
    joint_tail = sum(1 for v in nights.values()
                     if sum(1 for e in v if e["ratio"] > 1.0) >= 2)
    crowded = sum(1 for v in nights.values() if len(v) >= 3)
    return {
        "nights": len(nights),
        "events_per_night": {"median": _pct(per_night, 50),
                             "p90": _pct(per_night, 90),
                             "max": per_night[-1] if per_night else 0},
        "crowded_nights_3plus": crowded,
        "joint_blowthrough_nights": joint_tail,
        "note": "same-night short-vol positions are correlated (one "
                "macro tape, sector vol repricing). Position sizing "
                "must treat a night, not an event, as the unit.",
    }


def decomposition(events):
    def band_imp(e):
        i = e["implied"]
        if i >= 12: return ">=12%"
        if i >= 8:  return "8-12%"
        if i >= 5:  return "5-8%"
        return "<5%"
    nights = {}
    for e in events:
        nights.setdefault(e["date"], []).append(e)
    def crowd(e):
        return ">=3" if len(nights[e["date"]]) >= 3 else "1-2"
    dims = {
        "implied_band": band_imp,
        "cap": lambda e: (e["feat"] or {}).get("cap", "?"),
        "session": lambda e: (e["feat"] or {}).get("session", "?"),
        "regime": lambda e: e["regime"],
        "sector": lambda e: e["sector"],
        "same_night": crowd,
    }
    out = {}
    for dim, fn in dims.items():
        groups = {}
        for e in events:
            groups.setdefault(fn(e), []).append(e["mv"])
        rows = {}
        for k, v in groups.items():
            if len(v) < COHORT_MIN:
                rows[k] = {"n": len(v), "status": "thin"}
            else:
                rows[k] = {"n": len(v), "ev": round(mean(v), 2),
                           "med": round(median(v), 2),
                           "win": round(100 * sum(1 for y in v if y > 0)
                                        / len(v))}
        out[dim] = rows
    return out


def run(dry=False):
    ev = _load_events()
    result = {"generated": datetime.now(timezone.utc)
              .isoformat(timespec="seconds"),
              "engine_version": ENGINE_VERSION,
              "types": {}}
    for t, events in ev.items():
        if len(events) < MIN_N:
            result["types"][t] = {"n": len(events), "status": "accruing"}
            continue
        mvs = [e["mv"] for e in events]
        boot = date_cluster_bootstrap(events)
        block = {
            "n": len(events),
            "span": {"from": min(e["date"] for e in events),
                     "to": max(e["date"] for e in events)},
            "ev_vol_pts": round(mean(mvs), 2),
            "med_vol_pts": round(median(mvs), 2),
            "win": round(100 * sum(1 for y in mvs if y > 0) / len(mvs)),
            "date_cluster_bootstrap": boot,
            "move_ratio": ratio_distribution(events),
            "tails": tail_metrics(events),
            "clustering": clustering(events),
            "decomposition": decomposition(events),
        }
        ci = (boot.get("ci95") or [None])
        block["signal_qualified"] = bool(
            ci[0] is not None and ci[0] > 0 and len(events) >= MIN_N)
        block["trade_qualified"] = False
        block["trade_qualification_blockers"] = [
            "no historical option-chain P&L reconstruction yet "
            "(earnings_vol_backtest.py, CI)",
            "no spread/slippage/fees modeling",
            "no strategy-level tail analysis (gap through wings)",
            "no clustered-night portfolio sizing rule",
        ]
        result["types"][t] = block

    # Predeclared challengers (registered 2026-08-26, from the observed
    # implied-size monotonicity). Anti-curve-fit device: each carries
    # its declaration date; CHAMPION-track confirmation may use only
    # events AFTER that date (the in-sample nights suggested the
    # hypothesis and cannot also confirm it). Thresholds are frozen
    # here — do NOT tune them against the same nights.
    CHALLENGER_DECL = "2026-08-26"
    challengers = {}
    vr = ev.get("vol_rich") or []
    if len(vr) >= MIN_N:
        for name, flt, defn in (
            ("vol_rich_base", lambda e: True, "all vol_rich events"),
            ("vol_rich_large_move", lambda e: e["implied"] >= 12,
             "implied move >= 12%"),
        ):
            pre = [e for e in vr if e["date"] <= CHALLENGER_DECL
                   and flt(e)]
            post = [e for e in vr if e["date"] > CHALLENGER_DECL
                    and flt(e)]
            challengers[name] = {
                "definition": defn, "declared": CHALLENGER_DECL,
                "at_declaration": {
                    "n": len(pre),
                    "ev": round(mean(e["mv"] for e in pre), 2)
                    if pre else None,
                    "bootstrap": date_cluster_bootstrap(pre)
                    if pre else None},
                "forward": ({"n": len(post),
                             "ev": round(mean(e["mv"] for e in post), 2),
                             "bootstrap": date_cluster_bootstrap(post)}
                            if post else {"n": 0, "status": "accruing"}),
            }
        challengers["vol_rich_cheap_wings"] = {
            "definition": "iron_fly_1.5 wing debit / straddle credit "
                          "at issuance BELOW the trailing cohort "
                          "median (self-normalizing — no tuned "
                          "constant)",
            "declared": CHALLENGER_DECL,
            "status": "forward_only",
            "why": "wing-economics decomposition showed the wings "
                   "consume most of the gross straddle edge; the "
                   "coherent conditional is 'sell only when tail "
                   "insurance is unusually cheap'. Declared from the "
                   "same nights that suggested it, so only FORWARD "
                   "events can confirm it.",
            "forward": {"n": 0, "status": "accruing"},
        }
        challengers["vol_rich_extreme_ratio"] = {
            "definition": "implied / TickerDesk Fair Move >= 2.0",
            "declared": CHALLENGER_DECL,
            "status": "forward_only",
            "why": "the idea log never captured fair move at selection; "
                   "setup_context.jsonl logs implied + realized_med from "
                   "2026-08-26, so this challenger evaluates on forward "
                   "events only",
            "forward": {"n": 0, "status": "accruing"},
        }
    result["challengers"] = challengers

    # Per-ticker historical behavior — powers the earnings card's
    # "Historical behavior" block. A ticker needs >= 3 graded vol
    # events of its own; otherwise the UI falls back to cohort stats
    # (labeled as such). Ratio = |realized| / implied.
    tick_hist = {}
    all_ev = (ev.get("vol_rich") or []) + (ev.get("vol_cheap") or [])
    by_tick = {}
    for e in all_ev:
        by_tick.setdefault(e["ticker"], []).append(e["ratio"])
    for tk, rs in by_tick.items():
        if len(rs) < 3:
            continue
        rs_s = sorted(rs)
        tick_hist[tk] = {
            "n": len(rs), "median_ratio": round(rs_s[len(rs_s) // 2], 2),
            "inside_100x": round(100 * sum(1 for r in rs if r <= 1.0)
                                 / len(rs)),
            "inside_075x": round(100 * sum(1 for r in rs if r <= 0.75)
                                 / len(rs))}
    result["ticker_history"] = tick_hist
    cohort_rich = (result["types"].get("vol_rich") or {})
    result["cohort_history"] = ({
        "n": cohort_rich["n"],
        "median_ratio": cohort_rich["move_ratio"]["median"],
        "inside_100x": cohort_rich["move_ratio"]["inside_100x"],
        "inside_075x": cohort_rich["move_ratio"]["inside_075x"]}
        if cohort_rich.get("move_ratio") else None)

    # Wing economics — Defined-risk P&L = short-vol edge - wing cost.
    # From the immutable v2 reconstruction rows (frictionless
    # next_open marks, fees only): per event, straddle gross vs
    # iron_fly_1.5 gross; the difference is what the wings consumed.
    # This is the research surface for "sell vol_rich only when tail
    # insurance is unusually inexpensive" — an economically coherent
    # conditional, not a parameter search.
    pnl_cache = _load(R("data", "earnings_vol_pnl.json"), {}) or {}
    wings = []
    for r in pnl_cache.values():
        if r.get("bt_version") != "vol_backtest_v2" or r.get("skip"):
            continue
        if r["event"]["type"] != "vol_rich":
            continue
        st = (r.get("strategies") or {}).get("short_straddle")
        fl = (r.get("strategies") or {}).get("iron_fly_1.5")
        if not st or not fl:
            continue

        def _fless(s):     # frictionless next_open, fees only
            cash = 0.0
            for l in s["legs"]:
                ein = l["marks"].get("entry_close")
                eout = l["marks"].get("exit_open")
                if ein is None or eout is None:
                    return None
                cash += (-l["side"]) * (ein - eout) - 2 * 0.65 / 100.0
            return cash
        sp, fp = _fless(st), _fless(fl)
        if sp is None or fp is None:
            continue
        w_debit = sum(l["marks"]["entry_close"] for l in fl["legs"]
                      if l["side"] > 0
                      and l["marks"].get("entry_close") is not None)
        s_credit = sum(l["marks"]["entry_close"] for l in st["legs"]
                       if l["marks"].get("entry_close") is not None)
        wings.append({"date": r["event"]["date"],
                      "implied": r["event"]["implied"],
                      "straddle": sp, "fly": fp, "drag": fp - sp,
                      "wing_ratio": (w_debit / s_credit
                                     if s_credit else None)})
    if len(wings) >= MIN_N:
        drags = sorted(w["drag"] for w in wings)
        ratios = sorted(w["wing_ratio"] for w in wings
                        if w["wing_ratio"] is not None)
        cheap = [w for w in wings if w["wing_ratio"] is not None
                 and w["wing_ratio"] <= ratios[len(ratios) // 2]]
        rich_w = [w for w in wings if w["wing_ratio"] is not None
                  and w["wing_ratio"] > ratios[len(ratios) // 2]]
        result["wing_economics"] = {
            "n": len(wings),
            "avg_straddle_$": round(100 * mean(w["straddle"]
                                               for w in wings)),
            "avg_fly_$": round(100 * mean(w["fly"] for w in wings)),
            "avg_wing_drag_$": round(100 * mean(w["drag"]
                                                for w in wings)),
            "wing_ratio": {"median": round(ratios[len(ratios) // 2], 3),
                           "p25": round(ratios[len(ratios) // 4], 3),
                           "p75": round(ratios[3 * len(ratios) // 4], 3)}
                          if ratios else None,
            "fly_when_wings_below_median": {
                "n": len(cheap),
                "avg_$": round(100 * mean(w["fly"] for w in cheap))
                if cheap else None},
            "fly_when_wings_above_median": {
                "n": len(rich_w),
                "avg_$": round(100 * mean(w["fly"] for w in rich_w))
                if rich_w else None},
            "note": "frictionless next_open marks, fees only. "
                    "DESCRIPTIVE SPLIT POINTS AGAINST THE HYPOTHESIS: "
                    "fly does better when wings are EXPENSIVE — wing "
                    "richness proxies implied size, where the gross "
                    "edge is larger. The refined conditional would be "
                    "wings-cheap-RELATIVE-to-implied; not launched "
                    "(n small, tuning risk). The forward challenger "
                    "stands as declared and forward data decides."}

    # Fair-move calibration for the UI: per-type median realized/implied
    cal = {}
    for t, events in ev.items():
        if len(events) >= MIN_N:
            cal[t] = {"median_ratio": ratio_distribution(events)["median"],
                      "n": len(events)}
    result["fair_move_calibration"] = {
        "per_type": cal,
        "definition": "TickerDesk Fair Move = the ticker's own median "
                      "|earnings move| across its reported history "
                      "(earnings_edge realized_med). Volatility Edge = "
                      "implied - fair. Richness = implied/fair - 1. "
                      "The per-type median ratios above are the "
                      "selected-universe calibration context, not a "
                      "multiplier applied to any displayed number.",
    }
    result["honesty"] = (
        "Signal-level anatomy only: mv/ratio are underlying-move "
        "quantities from the graded idea log; no option was priced, no "
        "spread was crossed, no fill was assumed. 'signal_qualified' "
        "means the forecast relationship (implied vs realized) holds "
        "with a date-cluster bootstrap CI excluding zero. It is NOT a "
        "validated trade. trade_qualified stays false until the option "
        "P&L reconstruction clears costs and tails.")
    if not dry:
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1)
    for t, b in result["types"].items():
        if b.get("status") == "accruing":
            print(t, "accruing n=", b["n"])
        else:
            print(f"{t}: n={b['n']} EV={b['ev_vol_pts']} win={b['win']}% "
                  f"boot={b['date_cluster_bootstrap']} "
                  f"ratio_med={b['move_ratio']['median']} "
                  f"p95={b['move_ratio']['p95']} "
                  f"blow>1x={b['move_ratio']['blowthrough_10x']}% "
                  f"sigQ={b['signal_qualified']}")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    run(dry=ap.parse_args().dry_run)
