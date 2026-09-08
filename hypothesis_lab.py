"""
hypothesis_lab.py — the autonomous quant lab, with statistical discipline.

Generates pairwise feature hypotheses over the matured signal ledger
("Into ERN × positioning-DTE beats the tape"), then makes each one survive
three independent gates before it may be shown to a human:

  1. SAMPLE    joint n >= MIN_JOINT (and >= MIN_HALF in each time half)
  2. STABILITY split-half by time — the lift must appear in BOTH halves
               (same sign, and each half >= STABLE_FRAC of the full lift)
  3. HOLDOUT   the most recent OOS_FRAC of signals is never used to select;
               the lift must persist out-of-sample

Multiple-testing honesty: with hundreds of hypotheses tested, some
survivors WILL be luck. The payload carries n_tested and a plain-language
warning, and survivors are CANDIDATES for human review — nothing here
feeds the scanner automatically. Graduation to production = a deliberate
code change with its own guardrails, never a side effect.

Runs weekly (Fridays post-close, or --force). Metric convention matches
the edge-weight learner: direction-signed +5d excess vs SPY; bullish wins
when excess > 0, bearish when excess < 0; income/hedge excluded.

    python hypothesis_lab.py --force    # full run now
    python hypothesis_lab.py            # no-op unless Friday / stale file
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
from datetime import datetime, timezone

_BASE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(_BASE, "docs", "reports", "hypothesis_lab.json")

MIN_ATOM_N   = 800      # marginal sample before an atom joins the pool
MIN_JOINT    = 400      # joint sample for a pair hypothesis
MIN_HALF     = 150      # per time-half
MIN_OOS      = 80       # in the holdout slice
OOS_FRAC     = 0.20     # most recent fraction held out from selection
STABLE_FRAC  = 0.40     # each half must carry >= this share of full lift
LIFT_HIT_PP  = 2.0      # full-sample hit-rate lift gate (percentage points)
LIFT_EXC     = 0.75     # full-sample avg-excess lift gate (pp vs baseline)
MAX_PAIRS    = 600      # hard cap on hypotheses per run
STALE_DAYS   = 6        # weekly cadence enforcement


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _due(force: bool) -> bool:
    if force:
        return True
    if datetime.now(timezone.utc).weekday() == 4:      # Friday
        return True
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            gen = json.load(f).get("generated") or ""
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(gen)).days
        return age > STALE_DAYS
    except Exception:
        return True                                    # no file yet


def _signed_excess(s):
    """Direction-signed +5d excess, or None. Mirrors uoa_alpha._ew_win."""
    d = s.get("direction")
    if d not in ("bullish", "bearish"):
        return None
    r5 = (s.get("returns") or {}).get(5) or (s.get("returns") or {}).get("5")
    exc = (r5 or {}).get("excess")
    if exc is None:
        return None
    return exc if d == "bullish" else -exc


def _atoms(s, attrib_tags, dte_bucket):
    """Feature atoms for one signal — all computable at scan time, so a
    surviving hypothesis is actionable, not hindsight."""
    a = [f"type:{s.get('signal_type')}",
         f"dte:{dte_bucket(s.get('dte'))}",
         f"cap:{s.get('cap_bucket') or 'unknown'}",
         f"liq:{s.get('liquidity') or 'C'}",
         f"dir:{s.get('direction')}"]
    sc = s.get("trade_score") or 0
    a.append("score:80+" if sc >= 80 else
             "score:65-79" if sc >= 65 else "score:<65")
    a += [f"tag:{t}" for t in (s.get("tags") or []) if t in attrib_tags]
    oi = (s.get("oi") or {}).get("status")
    if oi and oi != "pending":
        a.append(f"oi:{oi}")
    return a


def _stats(vals):
    n = len(vals)
    if not n:
        return {"n": 0, "hit": None, "avg": None}
    mu = sum(vals) / n
    sd = (sum((v - mu) ** 2 for v in vals) / n) ** 0.5 if n > 1 else 0.0
    return {"n": n,
            "hit": round(100.0 * sum(1 for v in vals if v > 0) / n, 1),
            "avg": round(mu, 3), "sd": round(sd, 3)}


def run_lab():
    # Reuse the outcome tracker's own loading + scoring machinery so the
    # lab tests EXACTLY the data the learner sees (no parallel pipeline
    # to drift out of sync).
    import uoa_alpha
    _, scored = uoa_alpha.compute_edge()

    graded = []
    for s in scored:
        v = _signed_excess(s)
        if v is None:
            continue
        graded.append((s.get("flagged_at") or "", v,
                       set(_atoms(s, uoa_alpha.ATTRIB_TAGS,
                                  uoa_alpha._dte_bucket))))
    graded.sort(key=lambda x: x[0])                    # time order
    if len(graded) < MIN_JOINT * 2:
        return {"generated": datetime.now(timezone.utc)
                .isoformat(timespec="seconds"),
                "status": "accruing", "graded": len(graded)}

    cut = int(len(graded) * (1 - OOS_FRAC))
    train, oos = graded[:cut], graded[cut:]
    half = cut // 2
    h1, h2 = train[:half], train[half:]

    base = _stats([v for _, v, _ in train])
    base_oos = _stats([v for _, v, _ in oos])

    # atom pool: marginal n >= MIN_ATOM_N on the train slice
    counts = {}
    for _, _, atoms in train:
        for a in atoms:
            counts[a] = counts.get(a, 0) + 1
    pool = sorted([a for a, c in counts.items() if c >= MIN_ATOM_N])

    def slice_stats(rows, a, b):
        return _stats([v for _, v, at in rows if a in at and b in at])

    tested, survivors, oos_family = 0, [], []
    for a, b in itertools.combinations(pool, 2):
        # same-dimension pairs (dte:x & dte:y) are impossible — skip free
        if a.split(":")[0] == b.split(":")[0]:
            continue
        if tested >= MAX_PAIRS:
            break
        full = slice_stats(train, a, b)
        if full["n"] < MIN_JOINT:
            continue
        tested += 1
        lift_hit = full["hit"] - base["hit"]
        lift_exc = full["avg"] - base["avg"]
        if lift_hit < LIFT_HIT_PP or lift_exc < LIFT_EXC:
            continue
        s1, s2 = slice_stats(h1, a, b), slice_stats(h2, a, b)
        if s1["n"] < MIN_HALF or s2["n"] < MIN_HALF:
            continue
        l1, l2 = s1["avg"] - base["avg"], s2["avg"] - base["avg"]
        if l1 < STABLE_FRAC * lift_exc or l2 < STABLE_FRAC * lift_exc:
            continue                                   # unstable in time
        so = slice_stats(oos, a, b)
        if so["n"] < MIN_OOS or so["avg"] is None:
            continue
        oos_family.append({"name": f"{a} AND {b}",
                           "lift": so["avg"] - base_oos["avg"],
                           "n": so["n"], "sd": so.get("sd") or 0.0})
        if (so["avg"] - base_oos["avg"]) <= 0:
            continue                                   # died out of sample
        survivors.append({
            "hypothesis": f"{a} AND {b}",
            "train": full,
            "lift_hit_pp": round(lift_hit, 1),
            "lift_exc_pp": round(lift_exc, 2),
            "halves": [s1, s2],
            "oos": so,
            "oos_lift_pp": round(so["avg"] - base_oos["avg"], 2),
        })
    survivors.sort(key=lambda x: -x["oos_lift_pp"])

    # Multiple-testing honesty (2026-09 scorecard redesign): one-sided
    # normal p per OOS-evaluated hypothesis, deflated by the matched
    # scorecard's cluster effective-n ratio (signals cluster by week x
    # sector, so iid z-scores overstate evidence), then
    # Benjamini-Hochberg at q=0.10 across the WHOLE evaluated family.
    import math as _math
    deflate = 1.0
    try:
        sc = _load_json(os.path.join(_BASE, "docs", "reports", "uoa_scorecard.json"))
        bo = ((sc.get("pockets") or {}).get("overall")
              or {}).get("bootstrap") or {}
        if bo.get("n_eff") and sc.get("matched", {}).get("graded_5d"):
            deflate = min(1.0, bo["n_eff"]
                          / sc["matched"]["graded_5d"])
    except Exception:
        pass

    def _p(hyp):
        if hyp["sd"] <= 0 or hyp["n"] < 2:
            return 1.0
        z = hyp["lift"] / (hyp["sd"] / _math.sqrt(hyp["n"]))
        z *= _math.sqrt(deflate)
        return 0.5 * _math.erfc(z / _math.sqrt(2.0))

    fam = sorted(({"name": h["name"], "p": _p(h),
                   "lift": round(h["lift"], 3)} for h in oos_family),
                 key=lambda x: x["p"])
    m = len(fam)
    bh_pass = []
    for i, h in enumerate(fam, start=1):
        if h["p"] <= 0.10 * i / m:
            bh_pass = fam[:i]
    bh_names = {h["name"] for h in bh_pass}
    for sv in survivors:
        sv["bh_pass"] = sv["hypothesis"] in bh_names

    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "active",
        "graded": len(graded),
        "baseline": {"train": base, "oos": base_oos},
        "n_tested": tested,
        "survivors": survivors[:12],
        "graveyard": tested - len(survivors),
        "multiple_testing": {
            "oos_family_size": m,
            "cluster_deflation": round(deflate, 3),
            "bh_q": 0.10,
            "bh_survivors": len([s for s in survivors
                                 if s.get("bh_pass")]),
            "note": "one-sided normal p on OOS lift, z deflated by "
                    "sqrt(effective-n ratio) from the matched "
                    "scorecard, BH at q=0.10 across every "
                    "OOS-evaluated hypothesis. Survivors failing BH "
                    "are marked bh_pass:false and should be treated "
                    "as likely luck."},
        "params": {"min_joint": MIN_JOINT, "oos_frac": OOS_FRAC,
                   "stable_frac": STABLE_FRAC, "lift_hit_pp": LIFT_HIT_PP,
                   "lift_exc_pp": LIFT_EXC},
        "honesty": ("Gates are heuristic, not formal p-values — with "
                    f"{tested} hypotheses tested, expect a few survivors "
                    "to be luck. Survivors are CANDIDATES for review; "
                    "nothing is applied to the scanner automatically."),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if not _due(args.force):
        print("  hypothesis lab: not due (weekly cadence) — skipping")
        return
    payload = run_lab()
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    if payload.get("status") == "active":
        print(f"  hypothesis lab: {payload['n_tested']} tested, "
              f"{len(payload['survivors'])} survived, "
              f"{payload['graveyard']} discarded")
        for sv in payload["survivors"][:5]:
            print(f"    {sv['hypothesis']}: train {sv['train']['hit']}% "
                  f"(+{sv['lift_exc_pp']}pp) · OOS +{sv['oos_lift_pp']}pp "
                  f"(n={sv['train']['n']}/{sv['oos']['n']})")
    print(f"  Wrote {os.path.relpath(OUT_PATH, _BASE)}")


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
