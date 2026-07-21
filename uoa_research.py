"""
uoa_research.py — nightly alpha-research engine (roadmap items 2,4,5,6,7,8).

Runs once nightly (momentum.yml). Joins the signal ledger with matured
outcomes and publishes docs/reports/uoa_research.json. Everything is
OUT-OF-SAMPLE where a claim is made: walk-forward by ISO week, train
strictly on prior weeks. All sections gated and fail-open; nothing here
auto-applies to live ranking — survivors are inputs to the (separately
guard-railed) nightly learner and to human review.

Sections (roadmap numbering):
  #2 attribution   — matched-control benchmark: each day's flagged
                     UNIVERSE drift (mean +5d excess of unique flagged
                     underlyings) is the control. flow_alpha = what
                     direction-following earned NET of that drift.
                     Separates "picking hot names" (selection) from
                     "reading the flow's direction" (flow alpha).
  #4 loser_model   — categorical odds model predicting bottom-quintile
                     outcomes, walk-forward weekly. Reports the EV of
                     the book after excluding predicted disasters vs
                     baseline, OOS only. Dependency-free (naive-Bayes
                     odds on side/dte/cap/liq/premium/golden).
  #5 score2        — empirical-Bayes cohort-EV score (shrunken sum of
                     per-feature EV deltas, K=200). Weekly OOS Spearman
                     IC vs the ledger score for comparison.
  #6 sizing        — EV-weighted size hints per score2 quintile:
                     clamp(shrunken EV / MAE vol proxy), fractional-
                     Kelly style divisor 4, published as a table.
  #7 stability     — weekly side EV series, weekly OOS IC series, PSI
                     (population stability index) of feature mix last
                     2 ISO weeks vs prior history.
  #8 interactions  — depth<=3 categorical interaction search, n>=400,
                     three gates: split-half sign agreement, time-
                     ordered holdout (last 30%) same-sign, |EV|>=0.3.
                     Survivors are REVIEW CANDIDATES only.

Sample note: joins whatever per-signal outcomes data/uoa_alpha_cache.json
retains (the maturation window); published n is always the honest join.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations

_BASE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(_BASE, "data", "uoa_signals.jsonl")
CACHE = os.path.join(_BASE, "data", "uoa_alpha_cache.json")
OUT = os.path.join(_BASE, "docs", "reports", "uoa_research.json")

CFG = {
    "shrink_k": 200,          # EB shrinkage strength (per-feature EV)
    "disaster_q": 0.20,       # bottom quintile = "disaster" label
    "drop_frac": 0.20,        # loser model excludes worst 20% by odds
    "min_cohort_n": 400,      # interaction search gate
    "holdout_frac": 0.30,     # time-ordered holdout share
    "kelly_div": 4.0,         # fractional-Kelly divisor
    "size_cap": 2.0,          # max size multiple
}


# ── join ────────────────────────────────────────────────────────────────

def _prem_bucket(p):
    p = p or 0
    return ("p<250K" if p < 250e3 else "p250K-1M" if p < 1e6
            else "p1M-5M" if p < 5e6 else "p>5M")


def _dte_bucket(d):
    if d is None:
        return "dte?"
    return ("dte0-7" if d <= 7 else "dte8-21" if d <= 21
            else "dte22-60" if d <= 60 else "dte>60")


def load_rows():
    try:
        cache = json.load(open(CACHE, encoding="utf-8"))
    except Exception:
        return []
    rows = []
    with open(LEDGER, encoding="utf-8") as f:
        for line in f:
            try:
                s = json.loads(line)
            except Exception:
                continue
            c = cache.get(s.get("id"))
            if not c:
                continue
            r5 = (c.get("returns") or {}).get("5")
            if not r5 or r5.get("excess") is None:
                continue
            con = s.get("contract", "")
            typ = s.get("type") or (
                "put" if len(con) > 9 and con[-9] == "P" else "call")
            seller = s.get("flow_side") in ("put_seller", "call_seller")
            side = "seller" if seller else typ + "_buy"
            bull = (typ == "call") != seller
            raw = r5["excess"]                  # underlying excess (unsigned)
            mae = (c.get("excursion") or {}).get("mae")
            rows.append({
                "date": (s.get("flagged_at") or "")[:10],
                "ticker": s.get("ticker"),
                "raw": raw,
                "exc": raw if bull else -raw,   # direction-signed
                "bull": bull,
                "score": s.get("raw_score") or s.get("trade_score") or 0,
                "mae": abs(mae) if mae is not None else None,
                "f": [                          # categorical features
                    "side:" + side,
                    _dte_bucket(s.get("dte")),
                    "cap:" + (s.get("cap_bucket") or "unknown"),
                    "liq:" + (s.get("liquidity") or "C"),
                    _prem_bucket(s.get("premium")),
                    "golden" if ("Sweep" in (s.get("tags") or [])
                                 and "Block" in (s.get("tags") or []))
                    else "non_golden",
                ],
            })
    rows.sort(key=lambda r: r["date"])
    return rows


def _week(d):
    y, w, _ = datetime.strptime(d, "%Y-%m-%d").isocalendar()
    return "%d-W%02d" % (y, w)


def _mean(v):
    return sum(v) / len(v) if v else None


def _spearman(a, b):
    n = len(a)
    if n < 30:
        return None
    def ranks(x):
        order = sorted(range(n), key=lambda i: x[i])
        rk = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and x[order[j + 1]] == x[order[i]]:
                j += 1
            avg = (i + j) / 2.0
            for k2 in range(i, j + 1):
                rk[order[k2]] = avg
            i = j + 1
        return rk
    ra, rb = ranks(a), ranks(b)
    ma, mb = _mean(ra), _mean(rb)
    num = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    da = sum((v - ma) ** 2 for v in ra) ** 0.5
    db = sum((v - mb) ** 2 for v in rb) ** 0.5
    return round(num / (da * db), 4) if da and db else None


# ── #2 matched-control attribution ─────────────────────────────────────

def attribution(rows):
    """Universe drift control: per day, the mean +5d excess of UNIQUE
    flagged underlyings (direction-free). flow_alpha per trade =
    sign * (raw - drift): what direction-reading added beyond simply
    holding that day's flagged universe long."""
    by_day = defaultdict(dict)
    for r in rows:
        by_day[r["date"]].setdefault(r["ticker"], []).append(r["raw"])
    drift = {d: _mean([_mean(v) for v in tk.values()])
             for d, tk in by_day.items()}
    out = {"universe_drift_ev": round(_mean(list(drift.values())), 3)}
    for side_pred, name in ((lambda r: r["bull"], "long_book"),
                            (lambda r: not r["bull"], "short_book")):
        sel = [r for r in rows if side_pred(r)]
        if len(sel) < 30:
            continue
        dir_ev = _mean([r["exc"] for r in sel])
        alpha = _mean([(r["raw"] - drift[r["date"]]) *
                       (1 if r["bull"] else -1) for r in sel])
        out[name] = {"n": len(sel), "dir_ev": round(dir_ev, 3),
                     "flow_alpha": round(alpha, 3),
                     "selection_component": round(dir_ev - alpha, 3)}
    out["note"] = ("flow_alpha = EV net of the flagged-universe's own "
                   "drift that day. selection_component = what you'd "
                   "have earned from the universe drift alone with the "
                   "book's direction mix.")
    return out


# ── #4 walk-forward loser model ────────────────────────────────────────

def _odds_model(train):
    cut = sorted(r["exc"] for r in train)[int(len(train) * CFG["disaster_q"])]
    dis = [r for r in train if r["exc"] <= cut]
    n_d, n_a = len(dis), len(train)
    fd, fa = defaultdict(int), defaultdict(int)
    for r in train:
        for f in r["f"]:
            fa[f] += 1
    for r in dis:
        for f in r["f"]:
            fd[f] += 1
    def score(r):
        s = 0.0
        for f in r["f"]:
            pd = (fd[f] + 1) / (n_d + 2)
            pa = (fa[f] + 1) / (n_a + 2)
            s += (pd / pa) - 1.0        # >0 = disaster-enriched feature
        return s
    return score


def loser_model(rows):
    weeks = sorted(set(_week(r["date"]) for r in rows))
    if len(weeks) < 3:
        return {"status": "accruing", "weeks": len(weeks),
                "needs": "3+ ISO weeks of matured outcomes"}
    byw = defaultdict(list)
    for r in rows:
        byw[_week(r["date"])].append(r)
    base_all, kept_all, per_week = [], [], []
    for i in range(2, len(weeks)):
        train = [r for w in weeks[:i] for r in byw[w]]
        test = byw[weeks[i]]
        if len(train) < 1000 or len(test) < 100:
            continue
        sc = _odds_model(train)
        ranked = sorted(test, key=lambda r: sc(r), reverse=True)
        drop_n = int(len(ranked) * CFG["drop_frac"])
        kept = ranked[drop_n:]
        per_week.append({
            "week": weeks[i], "n": len(test),
            "ev_base": round(_mean([r["exc"] for r in test]), 3),
            "ev_kept": round(_mean([r["exc"] for r in kept]), 3),
        })
        base_all += [r["exc"] for r in test]
        kept_all += [r["exc"] for r in kept]
    if not per_week:
        return {"status": "accruing", "weeks": len(weeks)}
    return {
        "status": "active",
        "oos_weeks": per_week,
        "ev_base": round(_mean(base_all), 3),
        "ev_after_exclusion": round(_mean(kept_all), 3),
        "kept_frac": round(1 - CFG["drop_frac"], 2),
        "weeks_improved": sum(1 for w in per_week
                              if w["ev_kept"] > w["ev_base"]),
        "weeks_total": len(per_week),
        "note": ("Categorical odds model trained on prior weeks only; "
                 "excludes the predicted-worst " +
                 str(int(CFG["drop_frac"] * 100)) + "% each OOS week. "
                 "Review-only until it clears more regimes."),
    }


# ── #5 empirical-Bayes score2 + OOS IC comparison ──────────────────────

def _eb_deltas(train):
    ev_all = _mean([r["exc"] for r in train])
    by_f = defaultdict(list)
    for r in train:
        for f in r["f"]:
            by_f[f].append(r["exc"])
    K = CFG["shrink_k"]
    return {f: (len(v) / (len(v) + K)) * (_mean(v) - ev_all)
            for f, v in by_f.items()}, ev_all


def score2_eval(rows):
    weeks = sorted(set(_week(r["date"]) for r in rows))
    byw = defaultdict(list)
    for r in rows:
        byw[_week(r["date"])].append(r)
    ics, ics_old = [], []
    for i in range(2, len(weeks)):
        train = [r for w in weeks[:i] for r in byw[w]]
        test = byw[weeks[i]]
        if len(train) < 1000 or len(test) < 100:
            continue
        deltas, _ = _eb_deltas(train)
        s2 = [sum(deltas.get(f, 0.0) for f in r["f"]) for r in test]
        y = [r["exc"] for r in test]
        ic = _spearman(s2, y)
        ic_old = _spearman([r["score"] for r in test], y)
        if ic is not None:
            ics.append({"week": weeks[i], "ic_score2": ic,
                        "ic_ledger_score": ic_old})
            ics_old.append(ic_old)
    if not ics:
        return {"status": "accruing"}
    return {
        "status": "active",
        "weekly": ics,
        "avg_ic_score2": round(_mean([w["ic_score2"] for w in ics]), 4),
        "avg_ic_ledger_score": round(_mean([w["ic_ledger_score"]
                                            for w in ics
                                            if w["ic_ledger_score"]
                                            is not None]), 4),
        "note": ("score2 = shrunken sum of per-feature EV deltas "
                 "(K=" + str(CFG["shrink_k"]) + "), trained on prior "
                 "weeks only. Not yet applied to live ranking."),
    }


# ── #6 sizing table ────────────────────────────────────────────────────

def sizing(rows):
    """EV-weighted size hints by score2 quintile on the full sample
    (descriptive, not OOS — sizing consumes the OOS-validated score).
    size = clamp(EV / (MAE vol proxy) / kelly_div, 0, cap)."""
    deltas, _ = _eb_deltas(rows)
    scored = sorted(rows, key=lambda r: sum(
        deltas.get(f, 0.0) for f in r["f"]))
    n = len(scored)
    if n < 500:
        return {"status": "accruing"}
    out = []
    for q in range(5):
        seg = scored[int(n * q / 5): int(n * (q + 1) / 5)]
        ev = _mean([r["exc"] for r in seg])
        vol = _mean([r["mae"] for r in seg if r["mae"] is not None]) or 5.0
        size = max(0.0, min(CFG["size_cap"],
                            (ev / vol) * 10 / CFG["kelly_div"]))
        out.append({"quintile": q + 1, "n": len(seg), "ev": round(ev, 3),
                    "mae_vol": round(vol, 2), "size_x": round(size, 2)})
    return {"status": "active", "by_quintile": out,
            "note": ("Fractional-Kelly-style: size ∝ EV / MAE-vol / " +
                     str(CFG["kelly_div"]) + ", capped at " +
                     str(CFG["size_cap"]) + "x. Quintile 1 (worst) "
                     "sizes to 0 — exclusion via sizing, not deletion.")}


# ── #7 stability harness ───────────────────────────────────────────────

def stability(rows):
    weeks = sorted(set(_week(r["date"]) for r in rows))
    byw = defaultdict(list)
    for r in rows:
        byw[_week(r["date"])].append(r)
    side_ev = []
    for w in weeks:
        e = {"week": w, "n": len(byw[w])}
        for side in ("side:call_buy", "side:put_buy", "side:seller"):
            v = [r["exc"] for r in byw[w] if side in r["f"]]
            if len(v) >= 30:
                e[side.split(":")[1]] = round(_mean(v), 2)
        side_ev.append(e)
    # PSI: last 2 weeks feature mix vs prior
    recent = [r for r in rows if _week(r["date"]) in weeks[-2:]]
    prior = [r for r in rows if _week(r["date"]) not in weeks[-2:]]
    psi = None
    if len(recent) > 500 and len(prior) > 500:
        import math
        fr, fp = defaultdict(int), defaultdict(int)
        for r in recent:
            for f in r["f"]:
                fr[f] += 1
        for r in prior:
            for f in r["f"]:
                fp[f] += 1
        psi = 0.0
        for f in set(list(fr) + list(fp)):
            a = max(fr[f] / len(recent), 1e-4)
            b = max(fp[f] / len(prior), 1e-4)
            psi += (a - b) * math.log(a / b)
        psi = round(psi, 4)
    return {"weekly_side_ev": side_ev, "psi_last2w_vs_prior": psi,
            "psi_note": "PSI <0.1 stable · 0.1-0.25 shifting · >0.25 major drift"}


# ── #8 guarded interaction search (depth <=3) ─────────────────────────

def interactions(rows):
    n = len(rows)
    if n < 5000:
        return {"status": "accruing", "n": n}
    cut = int(n * (1 - CFG["holdout_frac"]))
    dev, hold = rows[:cut], rows[cut:]
    ev_dev = _mean([r["exc"] for r in dev])
    # candidate features present
    feats = sorted(set(f for r in dev for f in r["f"]))
    survivors, tested = [], 0
    for depth in (2, 3):
        for combo in combinations(feats, depth):
            # skip same-dimension combos (side:x + side:y can't co-occur)
            dims = [c.split(":")[0] if ":" in c else c[:3] for c in combo]
            if len(set(dims)) != depth:
                continue
            sel = [r for r in dev if all(c in r["f"] for c in combo)]
            if len(sel) < CFG["min_cohort_n"]:
                continue
            tested += 1
            ev = _mean([r["exc"] for r in sel])
            lift = ev - ev_dev
            if abs(lift) < 0.5:
                continue
            # gate 1: split-half sign agreement
            h = len(sel) // 2
            a, b = _mean([r["exc"] for r in sel[:h]]), _mean(
                [r["exc"] for r in sel[h:]])
            if a is None or b is None or (a - ev_dev) * (b - ev_dev) <= 0:
                continue
            # gate 2: time-ordered holdout same sign, meaningful size
            hsel = [r["exc"] for r in hold if all(c in r["f"]
                                                  for c in combo)]
            if len(hsel) < 100:
                continue
            hev = _mean(hsel)
            ev_hold = _mean([r["exc"] for r in hold])
            hlift = hev - ev_hold
            if hlift * lift <= 0 or abs(hlift) < 0.3:
                continue
            survivors.append({
                "combo": list(combo), "n_dev": len(sel),
                "ev_dev": round(ev, 3), "lift_dev": round(lift, 3),
                "n_holdout": len(hsel), "lift_holdout": round(hlift, 3),
            })
    survivors.sort(key=lambda s: -abs(s["lift_holdout"]))
    return {"status": "active", "tested": tested,
            "survivors": survivors[:12],
            "note": ("Depth<=3 categorical interactions, n>=" +
                     str(CFG["min_cohort_n"]) + ", gates: split-half "
                     "sign agreement + time-ordered holdout same-sign "
                     ">=0.3pp. With " + str(tested) + " tested, some "
                     "survivors will be luck — review candidates only, "
                     "nothing auto-applies.")}


def main():
    rows = load_rows()
    if len(rows) < 1000:
        print("  research: only %d joined matured rows — skipping" % len(rows))
        return
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "joined_matured_n": len(rows),
        "span": [rows[0]["date"], rows[-1]["date"]],
        "attribution": attribution(rows),
        "loser_model": loser_model(rows),
        "score2": score2_eval(rows),
        "sizing": sizing(rows),
        "stability": stability(rows),
        "interactions": interactions(rows),
        "note": ("Nightly alpha-research engine. All OOS claims are "
                 "walk-forward (train strictly on prior ISO weeks). "
                 "Joined-n covers the per-signal outcome retention "
                 "window; nothing here auto-applies to live ranking. "
                 "Educational, not advice."),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    a = payload["attribution"].get("long_book") or {}
    lm = payload["loser_model"]
    print("  research: n=%d · long book dir_ev=%s flow_alpha=%s · "
          "loser model %s · %d interaction survivors" %
          (len(rows), a.get("dir_ev"), a.get("flow_alpha"),
           lm.get("ev_after_exclusion", "accruing"),
           len((payload["interactions"].get("survivors") or []))))
    print("  Wrote uoa_research.json")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
