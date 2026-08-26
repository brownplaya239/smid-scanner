"""
trade_desk_research.py — setup-level alpha research for the Trade Desk.

The signal-level validation (trade_desk_validation.py) treats every print
as an observation; same-ticker same-day prints are massively correlated,
and the product question isn't "rank 119k prints" but "find the top ~1%
of independent SETUPS with persistent, executable forward alpha".
This module does that properly:

  1. SETUP TABLE — collapse the raw ledger into one observation per
     (ticker, session): flow counts, unique strikes/expiries, premium,
     directional imbalance, golden-sweep share, into-earnings, DTE,
     liquidity, opening share, repeat share, market regime, and
     point-in-time-legal PRIOR-session OI persistence (a T-1 print's
     next-day OI check is known by T; same-day OI is NOT used).
     Outcome = direction-signed +5-session close-anchored excess vs SPY
     (`exc_c`, both legs anchored at the flag-day close — no intraday
     drift leak; falls back to `excess` for pre-July frozen rows).

  2. PURGED, EMBARGOED WALK-FORWARD — expanding-window folds split by
     session date; training rows whose 5-session label window reaches
     into the validation block are purged (embargo = label horizon in
     calendar days). Final holdout untouched by any tuning.

  3. EXTREME-TAIL PRECISION — the product metric. Per fold and pooled
     OOS: avg / median signed alpha, hit rate and n at ALL / top 20 /
     10 / 5 / 2 / 1% of the model score. Overall IC is reported but is
     NOT the decision criterion.

  4. INTERACTION DISCOVERY — bounded conjunction search (2-3 clauses
     from a curated point-in-time feature vocabulary), train-half
     stability + min-n gates, evaluated OOS. Multiple-testing honesty
     note attached; survivors are CANDIDATES, not production.

  5. FEATURE ABLATION — drop one feature group at a time, remeasure
     pooled OOS top-10% alpha. What actually creates the edge.

  6. CLUSTER BOOTSTRAP CI — ticker-week cluster bootstrap on the tail
     selection's mean alpha; effective-cluster count reported. A CI
     spanning zero is not high conviction, whatever the mean says.

  7. CHAMPION / CHALLENGER REGISTRY — champion = whatever model is in
     production for the flow family (none qualifies at ship).
     Challengers (setup additive model, tail rules) promote ONLY on
     predefined OOS criteria: pooled top-bucket avg>0 AND median>0 AND
     bootstrap CI low >0 AND n>=100 AND positive in >=2/3 folds.
     No champion beating the bar -> NO CHAMPION -> the desk abstains.

Outputs docs/reports/trade_desk_research.json. trade_desk.py reads the
registry: a promoted challenger is the ONLY path to a qualified flow
idea. Nothing here is hand-tuned to a desired answer; if the record
says no edge, the JSON says no edge.

    python trade_desk_research.py            # full run
    python trade_desk_research.py --dry-run  # print, don't write
"""

from __future__ import annotations

import argparse
import json
import os
import random
from datetime import datetime, timedelta, timezone
from itertools import combinations
from statistics import mean, median

from uoa_alpha import load_ledger, _parse_occ
from trade_desk_validation import spearman, _regime_map, _load

_BASE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(_BASE, "data", "uoa_alpha_cache.json")
OUT_PATH = os.path.join(_BASE, "docs", "reports",
                        "trade_desk_research.json")

RESEARCH_VERSION = "setup_research_v1"
SHRINK_K = 60          # setups are far fewer than prints — lighter K
PER_CLAMP = 2.5
TOTAL_CLAMP = 7.5
N_FOLDS = 3
HOLDOUT_FRAC = 0.15
EMBARGO_DAYS = 9       # >= 5 sessions in calendar days
TAILS = (20, 10, 5, 2, 1)
RULE_MIN_TRAIN = 80    # min train setups for a conjunction
RULE_MIN_OOS = 25
BOOT_ITERS = 2000
PROMO = {"min_n": 100, "min_folds_pos": 2}


# ------------------------------------------------------------ setups

def _prem_band(p):
    if p >= 5e6:   return ">=5M"
    if p >= 1e6:   return ">=1M"
    if p >= 250e3: return ">=250k"
    return "<250k"


def _dte_band(d):
    if d is None:  return "unknown"
    if d <= 7:     return "0-7"
    if d <= 30:    return "8-30"
    if d <= 90:    return "31-90"
    return ">90"


def build_setups():
    """One observation per (ticker, session). Direction from the
    premium-weighted buyer imbalance; outcome from the day's last
    directional print's frozen close-anchored excess."""
    cache = _load(CACHE_PATH, {})
    regimes = _regime_map()
    days = {}
    for s in load_ledger():
        if s.get("direction") not in ("bullish", "bearish"):
            continue
        if s.get("flow_side") in ("put_seller", "call_seller"):
            continue
        day = (s.get("flagged_at") or "")[:10]
        tk = s.get("ticker")
        if not day or not tk:
            continue
        days.setdefault((tk, day), []).append(s)

    # per-ticker chronological index for prior-session persistence
    by_ticker = {}
    for (tk, day) in days:
        by_ticker.setdefault(tk, []).append(day)
    for tk in by_ticker:
        by_ticker[tk].sort()

    setups = []
    for (tk, day), grp in days.items():
        bull = sum(g.get("premium") or 0 for g in grp
                   if g["direction"] == "bullish")
        bear = sum(g.get("premium") or 0 for g in grp
                   if g["direction"] == "bearish")
        tot = bull + bear
        if tot <= 0:
            continue
        share = bull / tot
        if share >= 0.6:
            direction = "bullish"
        elif share <= 0.4:
            direction = "bearish"
        else:
            continue  # mixed conviction — no directional setup
        strikes, expiries = set(), set()
        for g in grp:
            ct, k = _parse_occ(g.get("contract"))
            if k:
                strikes.add(k)
            c = g.get("contract") or ""
            if len(c) > 15:
                expiries.add(c[-15:-9])
        dtes = [g.get("dte") for g in grp if g.get("dte") is not None]
        liqs = [g.get("liquidity") for g in grp if g.get("liquidity")]
        golden = sum(1 for g in grp if g.get("signal_type") ==
                     "golden_sweep")
        opening_prem = sum(g.get("premium") or 0 for g in grp
                           if g.get("opening") == "likely_open")
        repeat = sum(1 for g in grp if "Repeat" in (g.get("tags") or []))
        ern = any("Into ERN" in (g.get("tags") or []) for g in grp)

        # Prior-session persistence (point-in-time legal): in the
        # previous 3 sessions, did this ticker print the SAME direction,
        # and did any of those prints' next-day OI checks confirm?
        prior_days = [d for d in by_ticker[tk] if d < day][-3:]
        prior_same, prior_confirm = 0, False
        for pd in prior_days:
            pgrp = days[(tk, pd)]
            if any(g["direction"] == direction for g in pgrp):
                prior_same += 1
                for g in pgrp:
                    if g["direction"] != direction:
                        continue
                    c = cache.get(g.get("id")) or {}
                    if (c.get("oi") or {}).get("status") == "confirmed":
                        prior_confirm = True

        # Outcome: last directional print of the day with frozen returns.
        y5 = y1 = None
        for g in sorted(grp, key=lambda x: x.get("flagged_at") or "",
                        reverse=True):
            if g["direction"] != direction:
                continue
            c = cache.get(g.get("id"))
            if not c:
                continue
            r = c.get("returns") or {}
            r5 = r.get("5") or r.get(5) or {}
            r1 = r.get("1") or r.get(1) or {}
            e5 = r5.get("exc_c") if r5.get("exc_c") is not None \
                else r5.get("excess")
            e1 = r1.get("exc_c") if r1.get("exc_c") is not None \
                else r1.get("excess")
            if e5 is not None:
                sign = 1 if direction == "bullish" else -1
                y5, y1 = sign * e5, (sign * e1 if e1 is not None else None)
                break

        setups.append({
            "ticker": tk, "date": day, "direction": direction,
            "y5": y5, "y1": y1,
            "feats": {
                "dir":      direction,
                "conv":     "high" if (share >= 0.8 or share <= 0.2)
                            else "med",
                "nprints":  "1" if len(grp) == 1 else
                            ("2-4" if len(grp) <= 4 else ">=5"),
                "strikes":  "1" if len(strikes) <= 1 else
                            ("2-3" if len(strikes) <= 3 else ">=4"),
                "expiries": "1" if len(expiries) <= 1 else ">=2",
                "prem":     _prem_band(tot),
                "golden":   "yes" if golden else "no",
                "ern":      "yes" if ern else "no",
                "dte":      _dte_band(int(median(dtes)) if dtes else None),
                "liq":      ("AB" if any(l in ("A", "B") for l in liqs)
                             else "C-"),
                "open":     ("high" if tot and opening_prem / tot >= 0.5
                             else "low"),
                "repeat":   "yes" if repeat else "no",
                "regime":   regimes.get(day, "unknown"),
                "persist":  ("confirmed" if prior_confirm else
                             ("flow" if prior_same else "none")),
            },
        })
    setups.sort(key=lambda s: s["date"])
    return setups


# ------------------------------------------------------ model (additive)

def fit_add(train, exclude=()):
    ys = [r["y5"] for r in train]
    base = mean(ys)
    stats = {}
    for r in train:
        for f, v in r["feats"].items():
            if f in exclude:
                continue
            key = f + ":" + str(v)
            st = stats.setdefault(key, [0, 0.0])
            st[0] += 1
            st[1] += r["y5"]
    adj = {}
    for key, (n, tot) in stats.items():
        dev = (tot / n) - base
        a = dev * n / (n + SHRINK_K)
        adj[key] = max(-PER_CLAMP, min(PER_CLAMP, a))
    return {"base": base, "adj": adj, "exclude": list(exclude),
            "n_train": len(train)}


def pred_add(model, feats):
    t = sum(model["adj"].get(f + ":" + str(v), 0.0)
            for f, v in feats.items())
    return model["base"] + max(-TOTAL_CLAMP, min(TOTAL_CLAMP, t))


# ------------------------------------------------------------- metrics

def tail_table(train_preds, val_scored):
    """val_scored: (pred, y) rows. Thresholds from TRAIN prediction
    percentiles — selection rule known before validation."""
    tp = sorted(train_preds)
    out = {}
    def stats(rows):
        ys = [y for _, y in rows]
        if not ys:
            return {"n": 0}
        return {"n": len(ys),
                "avg": round(mean(ys), 2), "med": round(median(ys), 2),
                "hit": round(100 * sum(1 for y in ys if y > 0) / len(ys))}
    out["all"] = stats(val_scored)
    for pct in TAILS:
        cut = tp[min(len(tp) - 1, int(len(tp) * (100 - pct) / 100))]
        out["top" + str(pct)] = dict(stats(
            [r for r in val_scored if r[0] >= cut]), cut=round(cut, 3))
    return out


def cluster_bootstrap(rows, iters=BOOT_ITERS, seed=7):
    """rows: (cluster_key, y). 95% CI of the mean under ticker-week
    cluster resampling."""
    clusters = {}
    for k, y in rows:
        clusters.setdefault(k, []).append(y)
    keys = list(clusters)
    if len(keys) < 5:
        return {"status": "insufficient_clusters", "clusters": len(keys)}
    rng = random.Random(seed)
    means = []
    for _ in range(iters):
        ys = []
        for _ in range(len(keys)):
            ys.extend(clusters[rng.choice(keys)])
        means.append(mean(ys))
    means.sort()
    return {"clusters": len(keys),
            "mean": round(mean(y for _, y in rows), 2),
            "ci95": [round(means[int(iters * 0.025)], 2),
                     round(means[int(iters * 0.975)], 2)]}


# ------------------------------------------------ interaction discovery

RULE_VOCAB = [  # (feature, value) clauses with an economic rationale
    ("golden", "yes"), ("ern", "yes"), ("persist", "confirmed"),
    ("persist", "flow"), ("conv", "high"), ("prem", ">=1M"),
    ("prem", ">=5M"), ("regime", "risk_on"), ("regime", "risk_off"),
    ("dte", "8-30"), ("dte", "31-90"), ("liq", "AB"), ("open", "high"),
    ("repeat", "yes"), ("nprints", ">=5"), ("strikes", ">=4"),
    ("dir", "bearish"), ("dir", "bullish"),
]


def _match(r, clauses):
    return all(r["feats"].get(f) == v for f, v in clauses)


def rule_search(train, val):
    """Conjunctions of 2-3 clauses. Gates: train n, positive avg+med on
    train, positive avg on BOTH train halves (stability), then OOS
    evaluation. Everything that passes train gates is reported with its
    OOS result — winners AND losers, so multiple testing is visible."""
    half = len(train) // 2
    h1, h2 = train[:half], train[half:]
    base_avg = mean(r["y5"] for r in train)
    tested, results = 0, []
    for size in (2, 3):
        for clauses in combinations(RULE_VOCAB, size):
            feats_used = [c[0] for c in clauses]
            if len(set(feats_used)) != len(feats_used):
                continue
            sel = [r for r in train if _match(r, clauses)]
            tested += 1
            if len(sel) < RULE_MIN_TRAIN:
                continue
            ys = [r["y5"] for r in sel]
            if mean(ys) <= max(0.0, base_avg) or median(ys) <= 0:
                continue
            s1 = [r["y5"] for r in h1 if _match(r, clauses)]
            s2 = [r["y5"] for r in h2 if _match(r, clauses)]
            if len(s1) < 15 or len(s2) < 15 \
                    or mean(s1) <= 0 or mean(s2) <= 0:
                continue
            osel = [r["y5"] for r in val if _match(r, clauses)]
            results.append({
                "rule": " AND ".join(f + "=" + v for f, v in clauses),
                "train": {"n": len(ys), "avg": round(mean(ys), 2),
                          "med": round(median(ys), 2)},
                "oos": ({"n": len(osel), "avg": round(mean(osel), 2),
                         "med": round(median(osel), 2),
                         "hit": round(100 * sum(1 for y in osel if y > 0)
                                      / len(osel))}
                        if len(osel) >= RULE_MIN_OOS
                        else {"n": len(osel), "status": "thin"}),
            })
    results.sort(key=lambda r: -(r["train"]["avg"]))
    return {"tested": tested, "passed_train_gates": len(results),
            "rules": results[:20],
            "honesty": (f"{tested} conjunctions enumerated; with this "
                        "many tests expect some train survivors to be "
                        "luck. OOS column is the arbiter, and even it "
                        "is one window.")}


# ---------------------------------------------------------------- main

def _week(datestr):
    d = datetime.strptime(datestr, "%Y-%m-%d").date()
    return d.isocalendar()[0] * 100 + d.isocalendar()[1]


def _purge(train, val_start):
    """Drop training rows whose 5-session outcome window can reach the
    validation block (embargo in calendar days)."""
    cutoff = (datetime.strptime(val_start, "%Y-%m-%d")
              - timedelta(days=EMBARGO_DAYS)).date().isoformat()
    return [r for r in train if r["date"] <= cutoff]


def run(dry=False):
    setups_all = build_setups()
    graded = [s for s in setups_all if s["y5"] is not None]
    result = {
        "generated": datetime.now(timezone.utc)
        .isoformat(timespec="seconds"),
        "research_version": RESEARCH_VERSION,
        "setups_total": len(setups_all),
        "setups_graded": len(graded),
        "collapse": {"raw_directional_signals": None,  # filled below
                     "note": "one observation per ticker-session; mixed "
                             "(0.4<bull share<0.6) days excluded"},
    }
    if len(graded) < 800:
        result["status"] = "insufficient_history"
        if not dry:
            json.dump(result, open(OUT_PATH, "w", encoding="utf-8"),
                      indent=1)
        print(json.dumps(result, indent=1)[:600])
        return result

    span = {"from": graded[0]["date"], "to": graded[-1]["date"]}
    result["span"] = span
    n = len(graded)
    n_hold = int(n * HOLDOUT_FRAC)
    dev, hold = graded[:n - n_hold], graded[n - n_hold:]

    folds_out, pooled, pooled_rules_val = [], [], []
    edge = len(dev) // (N_FOLDS + 1)
    last_model = None
    fold_tail_avg = []
    for k in range(1, N_FOLDS + 1):
        raw_train = dev[:edge * k]
        val = dev[edge * k:edge * (k + 1)] if k < N_FOLDS \
            else dev[edge * k:]
        if not raw_train or not val:
            continue
        train = _purge(raw_train, val[0]["date"])
        model = fit_add(train)
        tpred = [pred_add(model, r["feats"]) for r in train]
        scored = [(pred_add(model, r["feats"]), r["y5"]) for r in val]
        tails = tail_table(tpred, scored)
        folds_out.append({
            "fold": k,
            "train": {"n": len(train), "purged": len(raw_train) - len(train),
                      "to": train[-1]["date"]},
            "val": {"n": len(val), "from": val[0]["date"],
                    "to": val[-1]["date"]},
            "ic": spearman([p for p, _ in scored],
                           [y for _, y in scored]),
            "tails": tails,
        })
        fold_tail_avg.append((tails.get("top5") or {}).get("avg"))
        for (p, y), r in zip(scored, val):
            pooled.append((p, y, r["ticker"], r["date"]))
        pooled_rules_val.extend(val)
        last_model = model

    # Pooled OOS tail table (thresholds refit per fold already; pool the
    # selected rows per fold's own cut for top5)
    pooled_scored = [(p, y) for p, y, _, _ in pooled]
    pooled_ic = spearman([p for p, _ in pooled_scored],
                         [y for _, y in pooled_scored])

    # tail via per-fold cuts: a row is "top5" if it cleared its fold's cut
    def pooled_tail(pct):
        rows = []
        for f in folds_out:
            cut = (f["tails"].get("top" + str(pct)) or {}).get("cut")
            if cut is None:
                continue
        # reconstruct: easier — recompute from fold cut inline below
        return rows
    # simpler + exact: collect during fold loop next time; here compute
    # pooled tails by re-walking folds
    pooled_tails = {}
    for pct in TAILS:
        sel = []
        idx = 0
        for f in folds_out:
            nval = f["val"]["n"]
            cut = (f["tails"].get("top" + str(pct)) or {}).get("cut")
            seg = pooled[idx:idx + nval]
            idx += nval
            if cut is None:
                continue
            sel.extend([(p, y, tk, dt) for (p, y, tk, dt) in seg
                        if p >= cut])
        ys = [y for _, y, _, _ in sel]
        pooled_tails["top" + str(pct)] = (
            {"n": len(ys), "avg": round(mean(ys), 2),
             "med": round(median(ys), 2),
             "hit": round(100 * sum(1 for y in ys if y > 0) / len(ys)),
             "bootstrap": cluster_bootstrap(
                 [(tk + ":" + str(_week(dt)), y)
                  for _, y, tk, dt in sel])}
            if ys else {"n": 0})
    pooled_tails["all"] = {
        "n": len(pooled_scored),
        "avg": round(mean(y for _, y in pooled_scored), 2),
        "med": round(median([y for _, y in pooled_scored]), 2),
        "hit": round(100 * sum(1 for _, y in pooled_scored if y > 0)
                     / len(pooled_scored))}

    # Holdout — last fold's model, cuts from its train percentiles
    hpred = [(pred_add(last_model, r["feats"]), r["y5"], r["ticker"],
              r["date"]) for r in hold]
    tp = sorted(pred_add(last_model, r["feats"])
                for r in _purge(dev, hold[0]["date"]))
    hold_tails = {}
    for pct in TAILS:
        cut = tp[min(len(tp) - 1, int(len(tp) * (100 - pct) / 100))]
        sel = [(p, y, tk, dt) for p, y, tk, dt in hpred if p >= cut]
        ys = [y for _, y, _, _ in sel]
        hold_tails["top" + str(pct)] = (
            {"n": len(ys), "avg": round(mean(ys), 2),
             "med": round(median(ys), 2),
             "hit": round(100 * sum(1 for y in ys if y > 0) / len(ys)),
             "bootstrap": cluster_bootstrap(
                 [(tk + ":" + str(_week(dt)), y) for _, y, tk, dt in sel])}
            if ys else {"n": 0})
    hys = [y for _, y, _, _ in hpred]
    hold_tails["all"] = {
        "n": len(hys), "avg": round(mean(hys), 2),
        "med": round(median(hys), 2),
        "hit": round(100 * sum(1 for y in hys if y > 0) / len(hys)),
        "ic": spearman([p for p, _, _, _ in hpred], hys)}

    # Ablation — pooled OOS top-10% avg with each feature group removed
    GROUPS = ["persist", "regime", "conv", "prem", "golden", "ern",
              "dte", "liq", "open", "repeat", "nprints", "strikes",
              "expiries", "dir"]
    ablation = {}
    for g in [None] + GROUPS:
        sel_ys = []
        idx = 0
        for k in range(1, len(folds_out) + 1):
            raw_train = dev[:edge * k]
            val = dev[edge * k:edge * (k + 1)] if k < N_FOLDS \
                else dev[edge * k:]
            if not raw_train or not val:
                continue
            train = _purge(raw_train, val[0]["date"])
            m = fit_add(train, exclude=(g,) if g else ())
            tp2 = sorted(pred_add(m, r["feats"]) for r in train)
            cut = tp2[min(len(tp2) - 1, int(len(tp2) * 0.90))]
            sel_ys.extend(r["y5"] for r in val
                          if pred_add(m, r["feats"]) >= cut)
        ablation["full" if g is None else "minus_" + g] = (
            {"n": len(sel_ys), "avg": round(mean(sel_ys), 2)}
            if sel_ys else {"n": 0})

    # Interaction discovery: train = purged dev-minus-last-val-block,
    # OOS = last validation block + holdout (chronological, untuned)
    rs_train = _purge(dev[:edge * N_FOLDS], dev[edge * N_FOLDS]["date"])
    rs_oos = dev[edge * N_FOLDS:] + hold
    rules = rule_search(rs_train, rs_oos)

    # ---------------- champion / challenger registry ----------------
    def promoted(tails_pooled, tails_hold):
        for pct in (5, 2, 1):
            p = tails_pooled.get("top" + str(pct)) or {}
            h = tails_hold.get("top" + str(pct)) or {}
            b = p.get("bootstrap") or {}
            pos_folds = sum(1 for a in fold_tail_avg
                            if a is not None and a > 0)
            if (p.get("n", 0) >= PROMO["min_n"]
                    and p.get("avg", -1) > 0 and p.get("med", -1) > 0
                    and (b.get("ci95") or [-1])[0] > 0
                    and pos_folds >= PROMO["min_folds_pos"]
                    and h.get("avg", -1) > 0):
                return {"promoted": True, "tail_pct": pct,
                        "evidence": {"pooled": p, "holdout": h}}
        return {"promoted": False}

    challenger_verdict = promoted(pooled_tails, hold_tails)
    registry = {
        "champion": {"name": None,
                     "note": "no model currently holds production edge "
                             "for the flow family -> desk abstains"},
        "challengers": {
            "setup_additive_v2": {
                "model": RESEARCH_VERSION,
                "pooled_ic": pooled_ic,
                "verdict": challenger_verdict,
            },
            "tail_rules_v1": {
                "candidates": rules["passed_train_gates"],
                "note": "rule survivors are research candidates only; "
                        "promotion requires a dedicated forward window",
            },
        },
        "promotion_criteria": {
            "pooled_tail_avg": "> 0", "pooled_tail_med": "> 0",
            "cluster_bootstrap_ci_low": "> 0",
            "min_n": PROMO["min_n"],
            "folds_positive": f">= {PROMO['min_folds_pos']}/{N_FOLDS}",
            "holdout_avg": "> 0",
        },
    }
    if challenger_verdict["promoted"]:
        registry["champion"] = {
            "name": "setup_additive_v2",
            "tail_pct": challenger_verdict["tail_pct"],
            "since": result["generated"],
            "model": {"base": round(last_model["base"], 3),
                      "adj": {k: round(v, 3)
                              for k, v in last_model["adj"].items()}},
        }

    result.update({
        "params": {"shrink_k": SHRINK_K, "folds": N_FOLDS,
                   "embargo_days": EMBARGO_DAYS,
                   "holdout_frac": HOLDOUT_FRAC,
                   "bootstrap_iters": BOOT_ITERS},
        "folds": folds_out,
        "pooled_oos": {"ic": pooled_ic, "tails": pooled_tails},
        "holdout": {"n": len(hold), "from": hold[0]["date"],
                    "to": hold[-1]["date"], "tails": hold_tails},
        "ablation": ablation,
        "interactions": rules,
        "registry": registry,
        "honesty": (
            "Setup-level, close-anchored, purged+embargoed — but still "
            "one ~9-week live stretch. Ticker-week cluster bootstrap "
            "corrects overlap optimism only partly (market-wide regime "
            "is one big cluster). Costs are NOT modeled; underlying "
            "excess, not option P/L. A promoted challenger here starts "
            "as PRODUCTION-CANDIDATE; the paper-forward ledger is the "
            "final referee."),
    })
    if not dry:
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1)
    print("setups:", len(setups_all), "graded:", n, "span:", span)
    for f in folds_out:
        print(f"fold {f['fold']}: IC {f['ic']} top5 "
              f"{f['tails'].get('top5')}")
    print("pooled top5:", pooled_tails.get("top5"))
    print("pooled top1:", pooled_tails.get("top1"))
    print("holdout top5:", hold_tails.get("top5"))
    print("challenger:", challenger_verdict)
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    run(dry=ap.parse_args().dry_run)
