"""
grade_engine.py — the nightly Engine Room: is the grading engine working,
what moved inside it, and what has it learned?

Answers four questions every post-close run, all from data that already
exists (nothing is modeled, nothing is invented):

  1. WHAT CHANGED    grade migration matrix (letter buckets, DoD), biggest
                     movers with their technical state, move-size rarity
  2. WHY / CONTEXT   factor shifts among tracked names (EMA crossings, RS
                     drift, volume expansion — facts file vs yesterday's)
  3. DID IT WORK     engine health: per-grade realized hit rates (gated,
                     from setup_outcomes), pick scorecard + rolling record
                     (evening_review), transition outcomes (gated, matured
                     here at +5 sessions)
  4. WHAT IT LEARNED strongest current edge-weight adjustments + decaying/
                     strengthening features + regime status

GATES everywhere: any stat below its minimum sample says "accruing" with
the honest count. Rarity needs >=20 logged days; transition outcomes need
n>=30 per bucket; per-grade accuracy inherits setup_outcomes' n>=30.

Writes docs/reports/grade_engine.json (site) and accrues
data/grade_migrations.json (migration + transition history, capped).

    python grade_engine.py            # nightly build
    python grade_engine.py --dry-run  # print, don't write
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone

_BASE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(_BASE, "docs", "reports")
HIST_PATH = os.path.join(_BASE, "data", "grade_migrations.json")
FACTS_PREV = os.path.join(_BASE, "data", "technical_facts_prev.json")
OUT_PATH = os.path.join(REPORTS, "grade_engine.json")

LADDER = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D",
          "D-", "E+", "E", "E-", "F+", "F", "F-", "G+", "G"]
RANK = {g: i for i, g in enumerate(LADDER)}
LETTERS = ["A", "B", "C", "D", "E", "F", "G"]

RARITY_MIN_DAYS = 20      # logged days before rarity percentages publish
TRANS_MIN_N     = 30      # matured transitions per bucket before stats
TRANS_LOG_CAP   = 15      # transitions logged per day for maturation
HIST_CAP_D      = 250
HOLD_D          = 5
MAX_MATURE      = 40


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _grade_map(run):
    out = {}
    for g in LADDER:
        for c in (run.get("grades") or {}).get(g) or []:
            if c.get("t"):
                out[c["t"]] = g
    return out


def _letter(g):
    return g[0] if g else None


# ── 1. migrations ────────────────────────────────────────────────────────

def migrations(runs):
    now, was = _grade_map(runs[-1]), _grade_map(runs[-2])
    matrix = {}          # "B->A" letter-bucket counts
    moves = []
    for t, g in now.items():
        pg = was.get(t)
        if not pg or pg == g:
            continue
        step = RANK[pg] - RANK[g]          # + = upgrade
        moves.append({"t": t, "from": pg, "to": g, "step": step})
        key = f"{_letter(pg)}->{_letter(g)}"
        if _letter(pg) != _letter(g):
            matrix[key] = matrix.get(key, 0) + 1
    ups = sorted([m for m in moves if m["step"] > 0],
                 key=lambda m: -m["step"])
    dns = sorted([m for m in moves if m["step"] < 0],
                 key=lambda m: m["step"])
    hist = {}
    for m in moves:
        b = _step_bucket(m["step"])
        hist[b] = hist.get(b, 0) + 1
    return {"n_up": len(ups), "n_down": len(dns), "matrix": matrix,
            "top_up": ups[:5], "top_down": dns[:5],
            "step_hist": hist, "graded_universe": len(now)}


def _step_bucket(step):
    a = abs(step)
    sign = "up" if step > 0 else "dn"
    return f"{sign}_{'3plus' if a >= 3 else a}"


# ── rarity + transition-outcome history ─────────────────────────────────

def accrue_history(date, mig):
    hist = _load(HIST_PATH) or {"days": [], "transitions": []}
    days = [d for d in hist.get("days") or [] if d.get("date") != date]
    days.append({"date": date, "step_hist": mig["step_hist"],
                 "n_up": mig["n_up"], "n_down": mig["n_down"],
                 "universe": mig["graded_universe"]})
    days.sort(key=lambda d: d["date"])
    hist["days"] = days[-HIST_CAP_D:]
    # log today's biggest transitions for +5d maturation
    trans = [t for t in hist.get("transitions") or []
             if not (t.get("date") == date)]
    todays = (mig["top_up"] + mig["top_down"])[:TRANS_LOG_CAP]
    for m in todays:
        trans.append({"date": date, "t": m["t"], "step": m["step"],
                      "bucket": _step_bucket(m["step"])})
    hist["transitions"] = trans[-(HIST_CAP_D * TRANS_LOG_CAP):]
    return hist


def rarity(hist, mig):
    days = hist.get("days") or []
    if len(days) < RARITY_MIN_DAYS:
        return {"status": "accruing", "days": len(days),
                "activates_at": RARITY_MIN_DAYS}
    total_moves, bucket_tot = 0, {}
    for d in days:
        for b, n in (d.get("step_hist") or {}).items():
            bucket_tot[b] = bucket_tot.get(b, 0) + n
            total_moves += n
    out = {}
    for b, n in bucket_tot.items():
        out[b] = round(100.0 * n / total_moves, 1)
    return {"status": "active", "days": len(days),
            "pct_of_all_moves": out}


def mature_transitions(hist):
    """+HOLD_D-session raw return per logged transition, matured against
    actual closes (same honest frame as setup_outcomes)."""
    pending = {}
    for r in hist.get("transitions") or []:
        if "ret" not in r:
            pending.setdefault(r["t"], []).append(r)
    if not pending:
        return 0
    import yfinance as yf
    tickers = list(pending.keys())[:MAX_MATURE]
    data = yf.download(tickers, period="4mo", interval="1d",
                       auto_adjust=True, group_by="ticker",
                       progress=False, threads=True)
    graded = 0
    for tk in tickers:
        try:
            closes = data[tk].dropna(how="all")["Close"].dropna()
        except Exception:
            continue
        dates = [d.date().isoformat() for d in closes.index]
        vals = [float(v) for v in closes.tolist()]
        for r in pending[tk]:
            if r["date"] not in dates:
                continue
            i0 = dates.index(r["date"])
            if i0 + HOLD_D >= len(vals) or vals[i0] <= 0:
                continue
            r["ret"] = round(100.0 * (vals[i0 + HOLD_D] / vals[i0] - 1), 2)
            graded += 1
    return graded


def transition_stats(hist):
    by_bucket = {}
    for r in hist.get("transitions") or []:
        if "ret" in r:
            by_bucket.setdefault(r["bucket"], []).append(r["ret"])
    out = {}
    for b, rets in by_bucket.items():
        # downgrades "work" when the name keeps falling
        signed = [(-x if b.startswith("dn") else x) for x in rets]
        if len(signed) < TRANS_MIN_N:
            out[b] = {"status": "accruing", "n": len(signed),
                      "activates_at": TRANS_MIN_N}
            continue
        out[b] = {"status": "active", "n": len(signed),
                  "hit": round(100 * sum(1 for v in signed if v > 0)
                               / len(signed)),
                  "avg": round(sum(signed) / len(signed), 2),
                  "hold_d": HOLD_D}
    return out


# ── 2. persistence ───────────────────────────────────────────────────────

def persistence(runs):
    """Average sessions the CURRENT holders of each letter grade have
    already held it, within the available run window."""
    maps = [_grade_map(r) for r in runs]
    streaks = {}
    for t, g in maps[-1].items():
        s = 1
        for m in reversed(maps[:-1]):
            if _letter(m.get(t)) == _letter(g):
                s += 1
            else:
                break
        streaks.setdefault(_letter(g), []).append(s)
    return {L: round(sum(v) / len(v), 1)
            for L, v in streaks.items() if v}, len(runs)


# ── 3. factor shifts (tracked names, facts vs yesterday's facts) ───────

def factor_shifts():
    cur = (_load(os.path.join(REPORTS, "technical_facts.json")) or {})
    prev = _load(FACTS_PREV)
    cf, pf = cur.get("facts") or {}, (prev or {}).get("facts") or {}
    if not cf:
        return None
    if not pf:
        return {"status": "baseline", "note": "first run — baseline stored"}
    common = set(cf) & set(pf)
    if len(common) < 10:
        return {"status": "baseline",
                "note": f"only {len(common)} overlapping names — thin diff"}
    sh = {"names": len(common)}
    for span in (20, 50):
        up = sum(1 for t in common
                 if cf[t].get(f"ema{span}") == "above"
                 and pf[t].get(f"ema{span}") == "below")
        dn = sum(1 for t in common
                 if cf[t].get(f"ema{span}") == "below"
                 and pf[t].get(f"ema{span}") == "above")
        sh[f"ema{span}_cross_up"] = up
        sh[f"ema{span}_cross_down"] = dn
    vol_exp = sum(1 for t in common
                  if (cf[t].get("vol_ratio") or 0) >= 1.5
                  and (pf[t].get("vol_ratio") or 0) < 1.5)
    sh["vol_expansions"] = vol_exp
    rs_d = [((cf[t].get("rs") or {}).get("d5") or 0)
            - ((pf[t].get("rs") or {}).get("d5") or 0) for t in common]
    sh["avg_rs5_shift_pp"] = round(sum(rs_d) / len(rs_d), 1)
    sh["status"] = "active"
    return sh


def snapshot_facts():
    cur = _load(os.path.join(REPORTS, "technical_facts.json"))
    if cur:
        os.makedirs(os.path.dirname(FACTS_PREV), exist_ok=True)
        with open(FACTS_PREV, "w", encoding="utf-8") as f:
            json.dump(cur, f, separators=(",", ":"))


# ── 4. health + learned ─────────────────────────────────────────────────

def engine_health():
    so = _load(os.path.join(REPORTS, "setup_outcomes.json")) or {}
    ev = _load(os.path.join(REPORTS, "evening_review.json")) or {}
    rh = _load(os.path.join(REPORTS, "regime_history.json")) or {}
    grades = so.get("grades") or {}
    active = {g: v for g, v in grades.items()
              if v.get("status") == "active"}
    acc = None
    if active:
        n = sum(v["n"] for v in active.values())
        acc = round(sum(v["win_rate"] * v["n"] for v in active.values()) / n)
    return {
        "accuracy": ({"status": "active", "pct": acc,
                      "grades": {g: {"win": v["win_rate"], "n": v["n"]}
                                 for g, v in active.items()}}
                     if acc is not None else
                     {"status": "accruing",
                      "graded": so.get("total_graded", 0),
                      "activates_at": so.get("min_n", 30),
                      "note": "per-grade +5-session hit rates publish at "
                              "n>=30 per grade"}),
        "scorecard": ev.get("graded"),
        "record": ev.get("record"),
        "calibration": (ev.get("calibration") or {}).get("status"),
        "regime": ((rh.get("days") or [{}])[-1]).get("label"),
        "breadth": ((rh.get("days") or [{}])[-1]).get("breadth"),
    }


def learned():
    ew = _load(os.path.join(REPORTS, "edge_weights.json")) or {}
    feats = sorted((ew.get("features") or {}).items(),
                   key=lambda x: -abs(x[1].get("adj") or 0))
    strongest = [{"f": k, "adj": v["adj"]} for k, v in feats[:5]
                 if v.get("adj")]
    decay = ew.get("decay") or {}
    dying = sorted([(k, v) for k, v in decay.items()
                    if v["delta"] <= -0.05], key=lambda x: x[1]["delta"])
    rising = sorted([(k, v) for k, v in decay.items()
                     if v["delta"] >= 0.05], key=lambda x: -x[1]["delta"])
    fmt = lambda k, v: {"f": k, "prior": round(v["prior_hit"] * 100),
                        "recent": round(v["recent_hit"] * 100)}
    return {"weights_version": ew.get("version"),
            "graded": ew.get("graded"),
            "strongest": strongest,
            "dying": [fmt(k, v) for k, v in dying[:3]],
            "rising": [fmt(k, v) for k, v in rising[:3]],
            "regime_weights": (ew.get("regimes") or {}).get("status"),
            "note": ("Adjustments are the UOA outcome-learner's, applied "
                     "with min-N/shrinkage/clamps. The swing grade formula "
                     "itself is NOT auto-reweighted — changes to it go "
                     "through validation, never silent updates.")}


def build():
    # Full report first — persistence needs the deep run history (the
    # light summary keeps only the freshest runs).
    sw = (_load(os.path.join(REPORTS, "swing_report.json"))
          or _load(os.path.join(REPORTS, "swing_latest_summary.json")))
    runs = (sw or {}).get("runs") or []
    if len(runs) < 2:
        print("  grade engine: need >=2 swing runs — skipping")
        return None, None
    date = runs[-1].get("date")
    mig = migrations(runs)
    hist = accrue_history(date, mig)
    matured = mature_transitions(hist)
    pers, window = persistence(runs)
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_date": date,
        "migrations": mig,
        "rarity": rarity(hist, mig),
        "transition_outcomes": transition_stats(hist),
        "persistence": {"by_letter": pers, "window_sessions": window},
        "factor_shifts": factor_shifts(),
        "health": engine_health(),
        "learned": learned(),
    }
    snapshot_facts()
    print(f"  grade engine: {mig['n_up']}↑/{mig['n_down']}↓ · "
          f"{matured} transitions matured · "
          f"accuracy {payload['health']['accuracy'].get('pct', 'accruing')}"
          f" · regime {payload['health'].get('regime')}")
    return payload, hist


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    payload, hist = build()
    if payload is None:
        return
    if args.dry_run:
        print(json.dumps(payload, indent=1)[:3500])
        return
    os.makedirs(os.path.dirname(HIST_PATH), exist_ok=True)
    with open(HIST_PATH, "w", encoding="utf-8") as f:
        json.dump(hist, f, separators=(",", ":"))
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"  Wrote {os.path.relpath(OUT_PATH, _BASE)} + migration history")


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
