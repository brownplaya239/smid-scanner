"""
trade_desk_validation.py — walk-forward validation of the Trade Desk
Alpha Score. Runs BEFORE the score is allowed to rank anything.

Joins the append-only signal ledger (data/uoa_signals.jsonl — features as
they were at flag time) with the frozen outcome cache
(data/uoa_alpha_cache.json — matured forward returns, never recomputed).
No API calls: this validates entirely from the point-in-time record.

Model under test — "Alpha Score v1", shown on the site as the RESEARCH
RANK: an additive expected-excess model on categorical features
observable AT FLAG TIME (side, regime, into-earnings, DTE bucket,
liquidity, cap bucket, opening read, signal type, premium band).
Per-feature deviations from the training-window baseline are shrunk
n/(n+K) and clamped, exactly the house edge-weights contract. The rank
is the percentile of a signal's expected excess within the graded
historical record — NOT a rank among today's candidates and NOT a
probability.

Point-in-time rules (2026-11 accuracy audit):
  - regime = the PRIOR session's breadth label. A day's label is
    computed after that day's close, so joining it to an intraday flag
    was look-ahead; serving also never has today's label until the
    close (live candidates scored as regime:unknown).
  - outcome = close-anchored excess (`exc_c`: stock and SPY legs both
    from the flag-day close). The older `excess` field anchors the stock
    leg at the intraday flag price but SPY at the close — mixed anchors.
    Rows without `exc_c` (pre-July frozen entries) are dropped, counted.
  - next-day OI confirmation is EXCLUDED — not knowable at flag time.

Walk-forward: folds split on session-date boundaries (no date on both
sides), and training rows whose 5-session label window reaches the
validation block are purged (EMBARGO_DAYS). A final holdout (most
recent HOLDOUT_FRAC of graded history) is scored once by the last
fold's model and reported separately — nothing is tuned against it.

Outputs docs/reports/trade_desk_validation.json:
  per-fold + holdout: Spearman IC, decile table, top-bucket stats,
  qualification-cutoff table (score >= 70/80/90: n, hit, avg/median
  direction-signed +5d excess), and the raw-trade_score baseline IC for
  comparison; `regime_conflict` (measured per regime x direction cells
  that the engine's REGIME_CONFLICT filter reads); the production model
  the engine scores with; an `honesty` block with the computed span.

The `qualification` block is informational — it says whether the rank
has any validated cutoff. The flow family's gate does NOT read it: the
gate authority is the setup-level research registry
(trade_desk_research.py). Nothing is hard-coded.

    python trade_desk_validation.py            # fit, validate, emit
    python trade_desk_validation.py --dry-run  # print, don't write
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, date, datetime, timedelta
from statistics import mean, median

from earnings_ideas import _cluster_se
from uoa_alpha import _dte_bucket, _side_of_signal, load_ledger

_BASE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(_BASE, "data", "uoa_alpha_cache.json")
REGIME_PATH = os.path.join(_BASE, "docs", "reports", "regime_history.json")
OUT_PATH = os.path.join(_BASE, "docs", "reports", "trade_desk_validation.json")

MODEL_VERSION = "alpha_score_v1"
SHRINK_K = 200  # same as edge_weights
PER_CLAMP = 2.0  # max |pp| any one feature may contribute
TOTAL_CLAMP = 6.0  # max |pp| total adjustment
N_FOLDS = 3  # expanding-window folds over the pre-holdout span
HOLDOUT_FRAC = 0.15  # most recent slice, scored once, never tuned on
QUAL_MIN_N = 150  # a cutoff needs this many validation signals/fold
QUAL_CUTS = (70, 80, 90)
DECILE_MIN_N = 800
EMBARGO_DAYS = 9  # >= 5 sessions in calendar days: the +5d label horizon
REGIME_MAX_AGE_D = 5  # an older prior label (paused pipeline) reads unknown
# Regime-conflict cells: a (regime, direction) cell blocks candidates only
# when its date-clustered CI sits wholly below directional flow overall.
CONFLICT_MIN_N = 500
CONFLICT_MIN_DATES = 20
# Modeled round trip for a liquid stock position, in pp of price: 2 x a
# 10 bp half-spread (the house cost model's liquid-name bucket,
# uoa_scorecard.cost_model). Gates on stock-level returns must clear it.
STOCK_RT_COST_PP = 0.20


def json_sanitize(o):
    """NaN/Infinity -> None, recursively. Python's json module happily
    EMITS bare NaN, which browsers reject wholesale — one poisoned
    float upstream blanked the whole Trade Desk (2026-09-21). Every
    engine passes its payload through this before json.dump."""
    if isinstance(o, dict):
        return {k: json_sanitize(v) for k, v in o.items()}
    if isinstance(o, list):
        return [json_sanitize(v) for v in o]
    if isinstance(o, float) and (o != o or o in (float("inf"), float("-inf"))):
        return None
    return o


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _regime_map():
    d = _load(REGIME_PATH, None) or {}
    return {r.get("date"): r.get("label") for r in d.get("days", []) if r.get("date")}


def prior_regime(regime_by_date, day):
    """Breadth label of the latest logged session strictly BEFORE `day`
    (YYYY-MM-DD) — the regime that was actually knowable during `day`'s
    session. A day's own label is computed after its close. A prior
    label older than REGIME_MAX_AGE_D calendar days (paused pipeline)
    reads "unknown". Used identically in training and serving."""
    day = (day or "")[:10]
    if not day or not regime_by_date:
        return "unknown"
    prev = max((d for d in regime_by_date if d < day), default=None)
    if prev is None:
        return "unknown"
    try:
        age = (date.fromisoformat(day) - date.fromisoformat(prev)).days
    except ValueError:
        return "unknown"
    if age > REGIME_MAX_AGE_D:
        return "unknown"
    return regime_by_date.get(prev) or "unknown"


def cluster_ci95(pairs):
    """95% CI of a mean over (cluster, value) pairs with a cluster-robust
    (CR1) standard error — the same estimator the earnings gate uses
    (earnings_ideas._cluster_se), so desk and gate CIs share one
    definition. Returns {"n", "clusters", "mean", "ci95"}; ci95 is None
    with fewer than 2 clusters."""
    vals = [v for _, v in pairs]
    if not vals:
        return {"n": 0, "clusters": 0, "mean": None, "ci95": None}
    mu = mean(vals)
    se = _cluster_se(pairs)
    return {
        "n": len(vals),
        "clusters": len({k for k, _ in pairs}),
        "mean": round(mu, 2),
        "ci95": ([round(mu - 1.96 * se, 2), round(mu + 1.96 * se, 2)] if se is not None else None),
    }


def span_weeks(d_from, d_to):
    """Whole weeks between two YYYY-MM-DD dates (for honesty strings —
    computed, never hard-coded)."""
    try:
        return round((date.fromisoformat(d_to) - date.fromisoformat(d_from)).days / 7)
    except TypeError, ValueError:
        return None


def _prem_band(p):
    try:
        p = float(p or 0)
    except TypeError, ValueError:
        return "unknown"
    if p >= 5e6:
        return ">=5M"
    if p >= 1e6:
        return ">=1M"
    if p >= 250e3:
        return ">=250k"
    return "<250k"


def features_at_flag(s, regime_by_date):
    """Categorical features observable at flag time. Mirrored by
    trade_desk.py scoring — keep the two in lockstep. Regime is the
    PRIOR session's label (see prior_regime)."""
    day = (s.get("flagged_at") or "")[:10]
    return {
        "side": _side_of_signal(s),
        "regime": prior_regime(regime_by_date, day),
        "ern": "yes" if "Into ERN" in (s.get("tags") or []) else "no",
        "dte": _dte_bucket(s.get("dte")),
        "liq": s.get("liquidity") or "unknown",
        "cap": s.get("cap_bucket") or "unknown",
        "open": s.get("opening") or "unknown",
        "stype": s.get("signal_type") or "unknown",
        "prem": _prem_band(s.get("premium")),
    }


def build_dataset():
    """Graded, direction-signed rows: ledger features x frozen outcomes.
    Sellers and hedge/income prints are excluded — the Trade Desk only
    considers buyer-initiated directional flow, so only that population
    is validated. Outcome = close-anchored +5d excess (`exc_c`); rows
    whose frozen outcome predates that field are dropped, never mixed in.

    Returns (rows, meta) — meta counts what was dropped and why."""
    cache = _load(CACHE_PATH, {})
    regimes = _regime_map()
    rows = []
    no_anchor = 0
    for s in load_ledger():
        if s.get("direction") not in ("bullish", "bearish"):
            continue
        if _side_of_signal(s) == "seller":
            continue
        c = cache.get(s.get("id"))
        if not c:
            continue
        r5 = (c.get("returns") or {}).get("5") or (c.get("returns") or {}).get(5) or {}
        exc = r5.get("exc_c")
        if exc is None:
            if r5.get("excess") is not None:
                no_anchor += 1  # graded, but only on the mixed-anchor field
            continue
        signed = exc if s["direction"] == "bullish" else -exc
        rows.append(
            {
                "date": (s.get("flagged_at") or "")[:10],
                "direction": s["direction"],
                "feats": features_at_flag(s, regimes),
                "raw_score": s.get("trade_score") or 0,
                "y": signed,
            }
        )
    rows.sort(key=lambda r: r["date"])
    return rows, {"dropped_no_close_anchor": no_anchor}


def _boundary(rows, i):
    """Move a split index forward until rows[i] starts a new session
    date, so no date lands on both sides of a split."""
    while 0 < i < len(rows) and rows[i]["date"] == rows[i - 1]["date"]:
        i += 1
    return i


def purge(train, val_start):
    """Drop training rows whose +5-session label window can reach the
    validation block (embargo in calendar days)."""
    try:
        cutoff = (date.fromisoformat(val_start) - timedelta(days=EMBARGO_DAYS)).isoformat()
    except ValueError:
        return list(train)
    return [r for r in train if r["date"] <= cutoff]


def regime_conflict(rows):
    """Measured regime x direction cells for the engine's REGIME_CONFLICT
    filter. A cell is a CONFLICT when its date-clustered 95% CI lies
    wholly below the average of all directional flow — i.e. that
    direction does measurably worse in that regime than flow in general
    (the family gate, not this filter, handles flow being weak overall).
    Uses the same prior-session regime label the features use."""
    if not rows:
        return {"status": "insufficient", "cells": {}, "conflicts": []}
    base = mean(r["y"] for r in rows)
    groups = {}
    for r in rows:
        reg = r["feats"].get("regime") or "unknown"
        if reg == "unknown":
            continue
        groups.setdefault(reg + "|" + r["direction"], []).append((r["date"], r["y"]))
    cells, conflicts = {}, []
    for key in sorted(groups):
        pairs = groups[key]
        ci = cluster_ci95(pairs)
        cells[key] = {
            "n": ci["n"],
            "dates": ci["clusters"],
            "avg": ci["mean"],
            "ci95": ci["ci95"],
        }
        if (
            ci["n"] >= CONFLICT_MIN_N
            and ci["clusters"] >= CONFLICT_MIN_DATES
            and ci["ci95"]
            and ci["ci95"][1] < base
        ):
            conflicts.append(key)
    return {
        "rule": (
            "block a direction in a regime when its +5d close-anchored "
            "excess, 95% CI clustered by flag date, sits wholly below the "
            f"all-flow average (min n {CONFLICT_MIN_N}, min "
            f"{CONFLICT_MIN_DATES} dates); regime = prior session's label"
        ),
        "baseline": {"n": len(rows), "avg": round(base, 2)},
        "cells": cells,
        "conflicts": conflicts,
    }


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
        seg = rows[d * n // 10 : (d + 1) * n // 10]
        ys = [y for _, y in seg]
        out.append(
            {
                "d": d + 1,
                "n": len(seg),
                "hit": round(100 * sum(1 for y in ys if y > 0) / len(ys)),
                "avg": round(mean(ys), 2),
                "med": round(median(ys), 2),
            }
        )
    return {
        "status": "active",
        "n": n,
        "deciles": out,
        "top_minus_bottom": round(out[-1]["avg"] - out[0]["avg"], 2),
        "monotonic_steps": sum(1 for i in range(9) if out[i + 1]["avg"] >= out[i]["avg"]),
    }


def cutoff_stats(scored, cut):
    ys = [y for s, y in scored if s >= cut]
    if not ys:
        return {"n": 0}
    return {
        "n": len(ys),
        "hit": round(100 * sum(1 for y in ys if y > 0) / len(ys)),
        "avg": round(mean(ys), 2),
        "med": round(median(ys), 2),
    }


# ---------------------------------------------------------------- main


def walk_forward_splits(rows):
    """(dev, hold, folds) with every split on a session-date boundary.
    folds = [(k, train, n_purged, val)]: expanding train windows, each
    purged of rows whose label window reaches its validation block."""
    n = len(rows)
    h0 = _boundary(rows, n - int(n * HOLDOUT_FRAC))
    dev, hold = rows[:h0], rows[h0:]
    fold_edge = len(dev) // (N_FOLDS + 1)
    edges = [_boundary(dev, fold_edge * k) for k in range(1, N_FOLDS + 1)] + [len(dev)]
    folds = []
    for k in range(1, N_FOLDS + 1):
        raw_train = dev[: edges[k - 1]]
        val = dev[edges[k - 1] : edges[k]]
        if not raw_train or not val:
            continue
        train = purge(raw_train, val[0]["date"])
        if not train:
            continue
        folds.append((k, train, len(raw_train) - len(train), val))
    return dev, hold, folds


def run(dry=False):
    rows, meta = build_dataset()
    if len(rows) < 2000:
        result = {
            "generated": datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z",
            "status": "insufficient_history",
            "n": len(rows),
            "dropped_no_close_anchor": meta["dropped_no_close_anchor"],
        }
        if not dry:
            with open(OUT_PATH, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=1)
        print(json.dumps(result, indent=1))
        return result

    n = len(rows)
    dev, hold, splits = walk_forward_splits(rows)

    folds = []
    last_model, last_scale = None, None
    for k, train, n_purged, val in splits:
        model = fit(train)
        scale = score_scale(model, train)
        scored = [(scale(predict(model, r["feats"])), r["y"]) for r in val]
        raw = [(r["raw_score"], r["y"]) for r in val]
        folds.append(
            {
                "fold": k,
                "train": {
                    "n": len(train),
                    "purged": n_purged,
                    "from": train[0]["date"],
                    "to": train[-1]["date"],
                },
                "val": {"n": len(val), "from": val[0]["date"], "to": val[-1]["date"]},
                "ic": spearman([s for s, _ in scored], [y for _, y in scored]),
                "ic_raw_trade_score": spearman([s for s, _ in raw], [y for _, y in raw]),
                "deciles": decile_table(scored),
                "cutoffs": {str(c): cutoff_stats(scored, c) for c in QUAL_CUTS},
            }
        )
        last_model, last_scale = model, scale

    # Holdout — scored once by the final fold's model, never tuned on.
    # pyrefly: ignore[not-callable]  set by the fold loop; >= 2000 rows guarantees at least one fold
    hold_scored = [(last_scale(predict(last_model, r["feats"])), r["y"]) for r in hold]
    holdout = {
        "n": len(hold),
        "from": hold[0]["date"],
        "to": hold[-1]["date"],
        "ic": spearman([s for s, _ in hold_scored], [y for _, y in hold_scored]),
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
            if cs.get("n", 0) < QUAL_MIN_N or cs.get("avg", -1) <= 0 or cs.get("med", -1) <= 0:
                ok = False
                break
        hs = holdout["cutoffs"][str(c)]
        if ok and hs.get("n", 0) >= QUAL_MIN_N and hs.get("avg", -1) > 0:
            qual = {"status": "validated", "min_score": c, "holdout": hs}
            break

    # Final production model refits on ALL graded rows (dev + holdout) —
    # standard practice once validation methodology has passed/failed.
    # It powers the displayed Research Rank only; no gate reads it.
    prod = fit(rows)
    prod_scale_rows = sorted(predict(prod, r["feats"]) for r in rows)
    span = {"from": rows[0]["date"], "to": rows[-1]["date"]}
    weeks = span_weeks(span["from"], span["to"])

    result = {
        "generated": datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z",
        "model_version": MODEL_VERSION,
        "n_graded": n,
        "dropped_no_close_anchor": meta["dropped_no_close_anchor"],
        "span": span,
        "span_weeks": weeks,
        "outcome": "exc_c: +5-session close-anchored excess vs SPY, direction-signed",
        "regime_basis": "prior session's breadth label (knowable at flag time)",
        "params": {
            "shrink_k": SHRINK_K,
            "per_clamp": PER_CLAMP,
            "total_clamp": TOTAL_CLAMP,
            "folds": N_FOLDS,
            "holdout_frac": HOLDOUT_FRAC,
            "qual_min_n": QUAL_MIN_N,
            "embargo_days": EMBARGO_DAYS,
        },
        "folds": folds,
        "holdout": holdout,
        "qualification": qual,
        "regime_conflict": regime_conflict(rows),
        "production_model": {
            "base": round(prod["base"], 3),
            "n_train": prod["n_train"],
            "adj": {k: round(v, 3) for k, v in prod["adj"].items()},
            "scale_anchors": [
                round(
                    prod_scale_rows[
                        min(len(prod_scale_rows) - 1, int(len(prod_scale_rows) * q / 100))
                    ],
                    3,
                )
                for q in range(0, 101, 5)
            ],
        },
        "honesty": (
            f"Single ~{weeks}-week live window ({span['from']} to "
            f"{span['to']}), overlapping same-ticker signals inflate "
            "effective n, regime labels only exist from 2026-07-06 (earlier "
            "rows are regime:unknown; each row uses the PRIOR session's "
            "label). +5d close-anchored underlying excess vs SPY, "
            "direction-signed; option-level P/L is NOT modeled here. Scores "
            "map to the percentile of expected excess within the graded "
            "record — 90 means 'top decile of expected edge', not '90% win "
            "probability' — and the walk-forward ICs above are the rank's "
            "measured skill."
        ),
    }
    if not dry:
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1)
    # Console summary
    print("graded:", n, "span:", result["span"])
    for f in folds:
        print(
            f"fold {f['fold']}: IC {f['ic']} (raw {f['ic_raw_trade_score']})",
            "cut80:",
            f["cutoffs"]["80"],
        )
    print("holdout: IC", holdout["ic"], "cut80:", holdout["cutoffs"]["80"])
    print("qualification:", qual)
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    run(dry=ap.parse_args().dry_run)
