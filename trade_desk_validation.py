"""
trade_desk_validation.py — walk-forward validation of the Trade Desk
Alpha Score. Runs BEFORE the score is allowed to rank anything.

Joins the append-only signal ledger (data/uoa_signals.jsonl — features as
they were at flag time) with the frozen outcome cache
(data/uoa_alpha_cache.json — matured forward returns, never recomputed).
No API calls: this validates entirely from the point-in-time record.

Model under test — "Alpha Score v1": an additive expected-excess model on
categorical features observable AT FLAG TIME (side, regime, into-earnings,
DTE bucket, liquidity, cap bucket, opening read, signal type, premium
band). Per-feature deviations from the training-window baseline are
shrunk n/(n+K) and clamped, exactly the house edge-weights contract.
Next-day OI confirmation is deliberately EXCLUDED — it is not knowable
at flag time (look-ahead).

Walk-forward: folds split by flag date; each fold fits on its train
window only and scores the later validation window. A final holdout
(most recent HOLDOUT_FRAC of graded history) is scored once by the last
fold's model and reported separately — nothing is tuned against it.

Outputs docs/reports/trade_desk_validation.json:
  per-fold + holdout: Spearman IC, decile table, top-bucket stats,
  qualification-cutoff table (score >= 70/80/90: n, hit, avg/median
  direction-signed +5d excess), and the raw-trade_score baseline IC for
  comparison. An `honesty` block states the record's limits (single
  ~11-week window, overlapping same-ticker signals, regime labels only
  from 2026-07-06).

The emitted `qualification` block is what trade_desk.py reads: the
lowest cutoff whose validation slices showed positive mean AND median
signed excess in every fold that had n >= QUAL_MIN_N. If no cutoff
clears, it emits {"status": "no_validated_edge"} and the Trade Desk
abstains from flow-ranked ideas entirely. Nothing is hard-coded.

    python trade_desk_validation.py            # fit, validate, emit
    python trade_desk_validation.py --dry-run  # print, don't write
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from statistics import mean, median

from uoa_alpha import load_ledger, _dte_bucket, _side_of_signal

_BASE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(_BASE, "data", "uoa_alpha_cache.json")
REGIME_PATH = os.path.join(_BASE, "docs", "reports", "regime_history.json")
OUT_PATH = os.path.join(_BASE, "docs", "reports", "trade_desk_validation.json")

MODEL_VERSION = "alpha_score_v1"
SHRINK_K = 200          # same as edge_weights
PER_CLAMP = 2.0         # max |pp| any one feature may contribute
TOTAL_CLAMP = 6.0       # max |pp| total adjustment
N_FOLDS = 3             # expanding-window folds over the pre-holdout span
HOLDOUT_FRAC = 0.15     # most recent slice, scored once, never tuned on
QUAL_MIN_N = 150        # a cutoff needs this many validation signals/fold
QUAL_CUTS = (70, 80, 90)
DECILE_MIN_N = 800


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _regime_map():
    d = _load(REGIME_PATH, None) or {}
    return {r.get("date"): r.get("label")
            for r in d.get("days", []) if r.get("date")}


def _prem_band(p):
    try:
        p = float(p or 0)
    except (TypeError, ValueError):
        return "unknown"
    if p >= 5e6:   return ">=5M"
    if p >= 1e6:   return ">=1M"
    if p >= 250e3: return ">=250k"
    return "<250k"


def features_at_flag(s, regime_by_date):
    """Categorical features observable at flag time. Mirrored by
    trade_desk.py scoring — keep the two in lockstep."""
    day = (s.get("flagged_at") or "")[:10]
    return {
        "side":   _side_of_signal(s),
        "regime": regime_by_date.get(day, "unknown"),
        "ern":    "yes" if "Into ERN" in (s.get("tags") or []) else "no",
        "dte":    _dte_bucket(s.get("dte")),
        "liq":    s.get("liquidity") or "unknown",
        "cap":    s.get("cap_bucket") or "unknown",
        "open":   s.get("opening") or "unknown",
        "stype":  s.get("signal_type") or "unknown",
        "prem":   _prem_band(s.get("premium")),
    }


def build_dataset():
    """Graded, direction-signed rows: ledger features x frozen outcomes.
    Sellers and hedge/income prints are excluded — the Trade Desk only
    considers buyer-initiated directional flow, so only that population
    is validated."""
    cache = _load(CACHE_PATH, {})
    regimes = _regime_map()
    rows = []
    for s in load_ledger():
        if s.get("direction") not in ("bullish", "bearish"):
            continue
        if _side_of_signal(s) == "seller":
            continue
        c = cache.get(s.get("id"))
        if not c:
            continue
        r5 = (c.get("returns") or {}).get("5") or \
             (c.get("returns") or {}).get(5)
        exc = (r5 or {}).get("excess")
        if exc is None:
            continue
        signed = exc if s["direction"] == "bullish" else -exc
        rows.append({
            "date": (s.get("flagged_at") or "")[:10],
            "feats": features_at_flag(s, regimes),
            "raw_score": s.get("trade_score") or 0,
            "y": signed,
        })
    rows.sort(key=lambda r: r["date"])
    return rows


# ---------------------------------------------------------------- model

def fit(train):
    """Per-feature-value shrunk mean-deviation model on signed +5d excess."""
    base = mean(r["y"] for r in train)
    stats = {}
    for r in train:
        for f, v in r["feats"].items():
            key = f + ":" + str(v)
            st = stats.setdefault(key, [0, 0.0])
            st[0] += 1
            st[1] += r["y"]
    adj = {}
    for key, (n, tot) in stats.items():
        dev = (tot / n) - base
        a = dev * n / (n + SHRINK_K)
        adj[key] = max(-PER_CLAMP, min(PER_CLAMP, a))
    return {"base": base, "adj": adj, "n_train": len(train)}


def predict(model, feats):
    total = 0.0
    for f, v in feats.items():
        total += model["adj"].get(f + ":" + str(v), 0.0)
    total = max(-TOTAL_CLAMP, min(TOTAL_CLAMP, total))
    return model["base"] + total


def score_scale(model, train):
    """Map expected excess -> 0-100 via the train distribution (percentile
    rank), so 'Alpha Score 90' literally means 'expected edge above 90% of
    the training population'."""
    preds = sorted(predict(model, r["feats"]) for r in train)
    def to_score(p):
        import bisect
        i = bisect.bisect_right(preds, p)
        return round(100.0 * i / len(preds), 1)
    return to_score


# ------------------------------------------------------------- metrics

def spearman(xs, ys):
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        rk = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0
            for k in range(i, j + 1):
                rk[order[k]] = avg
            i = j + 1
        return rk
    rx, ry = ranks(xs), ranks(ys)
    mx, my = mean(rx), mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    return round(num / (dx * dy), 4) if dx and dy else 0.0


def decile_table(scored):
    """scored: list of (score, y). Sorted-by-score decile stats."""
    if len(scored) < DECILE_MIN_N:
        return {"status": "insufficient", "n": len(scored)}
    rows = sorted(scored)
    out = []
    n = len(rows)
    for d in range(10):
        seg = rows[d * n // 10:(d + 1) * n // 10]
        ys = [y for _, y in seg]
        out.append({"d": d + 1, "n": len(seg),
                    "hit": round(100 * sum(1 for y in ys if y > 0) / len(ys)),
                    "avg": round(mean(ys), 2), "med": round(median(ys), 2)})
    return {"status": "active", "n": n, "deciles": out,
            "top_minus_bottom": round(out[-1]["avg"] - out[0]["avg"], 2),
            "monotonic_steps": sum(
                1 for i in range(9)
                if out[i + 1]["avg"] >= out[i]["avg"]) }


def cutoff_stats(scored, cut):
    ys = [y for s, y in scored if s >= cut]
    if not ys:
        return {"n": 0}
    return {"n": len(ys),
            "hit": round(100 * sum(1 for y in ys if y > 0) / len(ys)),
            "avg": round(mean(ys), 2),
            "med": round(median(ys), 2)}


# ---------------------------------------------------------------- main

def run(dry=False):
    rows = build_dataset()
    if len(rows) < 2000:
        result = {"generated": datetime.utcnow().isoformat() + "Z",
                  "status": "insufficient_history", "n": len(rows)}
        if not dry:
            with open(OUT_PATH, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=1)
        print(json.dumps(result, indent=1))
        return result

    n = len(rows)
    n_hold = int(n * HOLDOUT_FRAC)
    dev, hold = rows[:n - n_hold], rows[n - n_hold:]

    folds = []
    fold_edge = len(dev) // (N_FOLDS + 1)
    last_model, last_scale = None, None
    for k in range(1, N_FOLDS + 1):
        train = dev[:fold_edge * k]
        val = dev[fold_edge * k:fold_edge * (k + 1)] if k < N_FOLDS \
            else dev[fold_edge * k:]
        if not train or not val:
            continue
        model = fit(train)
        scale = score_scale(model, train)
        scored = [(scale(predict(model, r["feats"])), r["y"]) for r in val]
        raw = [(r["raw_score"], r["y"]) for r in val]
        folds.append({
            "fold": k,
            "train": {"n": len(train), "from": train[0]["date"],
                      "to": train[-1]["date"]},
            "val": {"n": len(val), "from": val[0]["date"],
                    "to": val[-1]["date"]},
            "ic": spearman([s for s, _ in scored], [y for _, y in scored]),
            "ic_raw_trade_score": spearman([s for s, _ in raw],
                                           [y for _, y in raw]),
            "deciles": decile_table(scored),
            "cutoffs": {str(c): cutoff_stats(scored, c) for c in QUAL_CUTS},
        })
        last_model, last_scale = model, scale

    # Holdout — scored once by the final fold's model, never tuned on.
    hold_scored = [(last_scale(predict(last_model, r["feats"])), r["y"])
                   for r in hold]
    holdout = {
        "n": len(hold), "from": hold[0]["date"], "to": hold[-1]["date"],
        "ic": spearman([s for s, _ in hold_scored],
                       [y for _, y in hold_scored]),
        "deciles": decile_table(hold_scored),
        "cutoffs": {str(c): cutoff_stats(hold_scored, c) for c in QUAL_CUTS},
    }

    # Qualification: lowest cutoff positive (avg AND med) in EVERY fold
    # with n >= QUAL_MIN_N, and non-negative avg on holdout.
    qual = {"status": "no_validated_edge"}
    for c in QUAL_CUTS:
        ok = True
        for f in folds:
            cs = f["cutoffs"][str(c)]
            if cs.get("n", 0) < QUAL_MIN_N or cs.get("avg", -1) <= 0 \
                    or cs.get("med", -1) <= 0:
                ok = False
                break
        hs = holdout["cutoffs"][str(c)]
        if ok and hs.get("n", 0) >= QUAL_MIN_N and hs.get("avg", -1) > 0:
            qual = {"status": "validated", "min_score": c,
                    "holdout": hs}
            break

    # Final production model refits on ALL graded rows (dev + holdout) —
    # standard practice once validation methodology has passed/failed;
    # the qualification verdict above is what gates its use.
    prod = fit(rows)
    prod_scale_rows = sorted(predict(prod, r["feats"]) for r in rows)

    result = {
        "generated": datetime.utcnow().isoformat() + "Z",
        "model_version": MODEL_VERSION,
        "n_graded": n,
        "span": {"from": rows[0]["date"], "to": rows[-1]["date"]},
        "params": {"shrink_k": SHRINK_K, "per_clamp": PER_CLAMP,
                   "total_clamp": TOTAL_CLAMP, "folds": N_FOLDS,
                   "holdout_frac": HOLDOUT_FRAC,
                   "qual_min_n": QUAL_MIN_N},
        "folds": folds,
        "holdout": holdout,
        "qualification": qual,
        "production_model": {"base": round(prod["base"], 3),
                             "n_train": prod["n_train"],
                             "adj": {k: round(v, 3)
                                     for k, v in prod["adj"].items()},
                             "scale_anchors": [
                                 round(prod_scale_rows[
                                     min(len(prod_scale_rows) - 1,
                                         int(len(prod_scale_rows) * q / 100))
                                 ], 3) for q in range(0, 101, 5)]},
        "honesty": (
            "Single ~11-week live window (one macro stretch), overlapping "
            "same-ticker signals inflate effective n, regime labels only "
            "exist from 2026-07-06 (earlier rows are regime:unknown). "
            "+5d underlying excess vs SPY, direction-signed; option-level "
            "P/L is NOT modeled here. Scores map to train-percentile of "
            "expected excess — 90 means 'top decile of expected edge', "
            "not '90% win probability'."),
    }
    if not dry:
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1)
    # Console summary
    print("graded:", n, "span:", result["span"])
    for f in folds:
        print(f"fold {f['fold']}: IC {f['ic']} (raw {f['ic_raw_trade_score']})",
              "cut80:", f["cutoffs"]["80"])
    print("holdout: IC", holdout["ic"], "cut80:", holdout["cutoffs"]["80"])
    print("qualification:", qual)
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    run(dry=ap.parse_args().dry_run)
