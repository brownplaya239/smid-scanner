"""
hypothesis_lab.py — the autonomous quant lab, with statistical discipline.

Generates pairwise feature hypotheses over the matured signal ledger
("Into ERN × swing-DTE beats the tape"), then makes each one survive three
independent gates before it may be shown to a human:

  1. SAMPLE    joint n >= MIN_JOINT (and >= MIN_HALF in each time half)
  2. STABILITY split-half by time — the lift must appear in BOTH halves
               (same sign, and each half >= STABLE_FRAC of the full lift)
  3. HOLDOUT   the most recent OOS_FRAC of sessions is never used to select;
               the lift must persist out-of-sample. The 5 sessions before
               the holdout are dropped (a +5d outcome takes 5 sessions to
               resolve, so those rows would overlap the holdout's moves).

Multiple-testing honesty: with hundreds of hypotheses tested, some
survivors WILL be luck. Every OOS-evaluated hypothesis gets a one-sided p on
its OOS lift with a date-clustered standard error (same-day alerts share a
market move), then Benjamini-Hochberg at q=0.10 across the whole family.
Survivors are CANDIDATES for human review — nothing here feeds the scanner
automatically.

Sample (2026-11 accuracy audit): one alert per contract per US/Eastern day
(scan re-logs collapsed, uoa_alpha.unique_alerts); graded on the +5-session
stock excess vs SPY in the flow's direction (uoa_alpha._signed_returns:
puts bought are flipped; premium selling and mixed / unknown prints have no
direction and are excluded; price-basis errors are dropped). Every feature
is known at scan time. Open-interest status is NOT a feature: next-day OI
is only known the day after the flag.

Runs weekly (Fridays post-close, or --force).

    python hypothesis_lab.py --force    # full run now
    python hypothesis_lab.py            # no-op unless Friday / stale file
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
from datetime import UTC, datetime

_BASE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(_BASE, "docs", "reports", "hypothesis_lab.json")

MIN_ATOM_N = 800  # marginal sample before an atom joins the pool
MIN_JOINT = 400  # joint sample for a pair hypothesis
MIN_HALF = 150  # per time-half
MIN_OOS = 80  # in the holdout slice
MIN_OOS_DAYS = 10  # flag dates needed for a clustered standard error
OOS_FRAC = 0.20  # most recent fraction held out from selection
STABLE_FRAC = 0.40  # each half must carry >= this share of full lift
LIFT_HIT_PP = 2.0  # full-sample hit-rate lift gate (percentage points)
LIFT_EXC = 0.75  # full-sample avg-excess lift gate (pp vs baseline)
MAX_PAIRS = 600  # hard cap on hypotheses per run
STALE_DAYS = 6  # weekly cadence enforcement
BH_Q = 0.10


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _due(force: bool) -> bool:
    if force:
        return True
    if datetime.now(UTC).weekday() == 4:  # Friday
        return True
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            gen = json.load(f).get("generated") or ""
        age = (datetime.now(UTC) - datetime.fromisoformat(gen)).days
        return age > STALE_DAYS
    except Exception:
        return True  # no file yet


def _signed_excess(s):
    """Direction-adjusted +5d excess vs SPY, or None when the alert has no
    stock direction (premium selling, mixed / unknown prints), a bad price
    basis, or no matured +5d outcome."""
    import uoa_alpha

    r = uoa_alpha._signed_returns(s)
    return ((r or {}).get(5) or {}).get("excess")


def _atoms(s, attrib_tags, dte_bucket):
    """Feature atoms for one alert — every one is known at scan time, so a
    surviving hypothesis is actionable, not hindsight. (OI status is left
    out: next-day OI only exists the day after the flag.)"""
    a = [
        f"type:{s.get('signal_type')}",
        f"dte:{dte_bucket(s.get('dte'))}",
        f"cap:{s.get('cap_bucket') or 'unknown'}",
        f"liq:{s.get('liquidity') or 'C'}",
        f"dir:{s.get('direction')}",
    ]
    sc = s.get("trade_score") or 0
    a.append("score:80+" if sc >= 80 else "score:65-79" if sc >= 65 else "score:<65")
    a += [f"tag:{t}" for t in (s.get("tags") or []) if t in attrib_tags]
    return a


def _stats(vals):
    n = len(vals)
    if not n:
        return {"n": 0, "hit": None, "avg": None}
    mu = sum(vals) / n
    sd = (sum((v - mu) ** 2 for v in vals) / n) ** 0.5 if n > 1 else 0.0
    return {
        "n": n,
        "hit": round(100.0 * sum(1 for v in vals if v > 0) / n, 1),
        "avg": round(mu, 3),
        "sd": round(sd, 3),
    }


def graded_alerts(scored):
    """One graded row per unique alert: {date, at, v, atoms}, time-ordered."""
    import uoa_alpha

    graded = []
    for s in uoa_alpha.unique_alerts(scored):
        v = _signed_excess(s)
        if v is None:
            continue
        graded.append(
            {
                "date": uoa_alpha._et_date(s.get("flagged_at")),
                "at": s.get("flagged_at") or "",
                "v": v,
                "atoms": set(_atoms(s, uoa_alpha.ATTRIB_TAGS, uoa_alpha._dte_bucket)),
            }
        )
    graded.sort(key=lambda g: (g["date"], g["at"]))
    return graded


def bh_select(family, q=BH_Q):
    """Benjamini-Hochberg: names of the hypotheses that pass at level q."""
    fam = sorted(family, key=lambda x: x["p"])
    m = len(fam)
    passed = []
    for i, h in enumerate(fam, start=1):
        if h["p"] <= q * i / m:
            passed = fam[:i]
    return {h["name"] for h in passed}


def _one_sided_p(lift, se):
    if not se or se <= 0:
        return 1.0
    return 0.5 * math.erfc((lift / se) / math.sqrt(2.0))


def run_lab(scored=None):
    # Reuse the outcome tracker's own loading + scoring machinery so the
    # lab tests EXACTLY the data the track record sees.
    import uoa_alpha
    from uoa_research import EMBARGO_SESSIONS, embargo_split

    if scored is None:
        _, scored = uoa_alpha.compute_edge(persist=False)
    graded = graded_alerts(scored)
    if len(graded) < MIN_JOINT * 2:
        return {
            "generated": datetime.now(UTC).isoformat(timespec="seconds"),
            "status": "accruing",
            "graded": len(graded),
        }

    train, oos, embargoed = embargo_split(graded, OOS_FRAC, date_of=lambda g: g["date"])
    half = len(train) // 2
    h1, h2 = train[:half], train[half:]

    base = _stats([g["v"] for g in train])
    base_oos = _stats([g["v"] for g in oos])

    # atom pool: marginal n >= MIN_ATOM_N on the train slice
    counts = {}
    for g in train:
        for a in g["atoms"]:
            counts[a] = counts.get(a, 0) + 1
    pool = sorted([a for a, c in counts.items() if c >= MIN_ATOM_N])

    def cohort(rows, a, b):
        return [g for g in rows if a in g["atoms"] and b in g["atoms"]]

    def slice_stats(rows, a, b):
        return _stats([g["v"] for g in cohort(rows, a, b)])

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
            continue  # unstable in time
        oc = cohort(oos, a, b)
        so = _stats([g["v"] for g in oc])
        if so["n"] < MIN_OOS or so["avg"] is None:
            continue
        lift_oos = so["avg"] - base_oos["avg"]
        days = len({g["date"] for g in oc})
        se = (
            uoa_alpha._cluster_se([(g["date"], g["v"]) for g in oc])
            if days >= MIN_OOS_DAYS
            else None
        )
        oos_family.append({"name": f"{a} AND {b}", "p": _one_sided_p(lift_oos, se)})
        if lift_oos <= 0:
            continue  # died out of sample
        so["days"] = days
        survivors.append(
            {
                "hypothesis": f"{a} AND {b}",
                "train": full,
                "lift_hit_pp": round(lift_hit, 1),
                "lift_exc_pp": round(lift_exc, 2),
                "halves": [s1, s2],
                "oos": so,
                "oos_lift_pp": round(lift_oos, 2),
                "oos_se": round(se, 3) if se else None,
            }
        )
    survivors.sort(key=lambda x: -x["oos_lift_pp"])

    bh_names = bh_select(oos_family) if oos_family else set()
    for sv in survivors:
        sv["bh_pass"] = sv["hypothesis"] in bh_names

    return {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": "active",
        "graded": len(graded),
        "sample": {
            "unit": "one alert per contract per ET day, directional flow only",
            "metric": "+5-session stock excess vs SPY in the flow's direction (pp)",
            "span": [graded[0]["date"], graded[-1]["date"]],
            "train_n": len(train),
            "oos_n": len(oos),
            "oos_from": oos[0]["date"] if oos else None,
            "embargoed_n": embargoed,
            "embargo_sessions": EMBARGO_SESSIONS,
        },
        "baseline": {"train": base, "oos": base_oos},
        "n_tested": tested,
        "survivors": survivors[:12],
        "graveyard": tested - len(survivors),
        "multiple_testing": {
            "oos_family_size": len(oos_family),
            "se": "clustered by flag date",
            "bh_q": BH_Q,
            "bh_survivors": sum(1 for s in survivors if s.get("bh_pass")),
            "note": "one-sided normal p on each hypothesis' OOS lift with a "
            "date-clustered standard error (fewer than %d OOS dates = no "
            "evidence), Benjamini-Hochberg at q=%.2f across every "
            "OOS-evaluated hypothesis. Survivors failing BH are marked "
            "bh_pass:false and should be treated as likely luck." % (MIN_OOS_DAYS, BH_Q),
        },
        "params": {
            "min_joint": MIN_JOINT,
            "oos_frac": OOS_FRAC,
            "stable_frac": STABLE_FRAC,
            "lift_hit_pp": LIFT_HIT_PP,
            "lift_exc_pp": LIFT_EXC,
        },
        "honesty": (
            f"With {tested} hypotheses tested, expect a few survivors to be "
            "luck. Survivors are CANDIDATES for review; nothing is applied to "
            "the scanner automatically."
        ),
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
        print(
            f"  hypothesis lab: {payload['graded']} graded alerts, "
            f"{payload['n_tested']} tested, "
            f"{len(payload['survivors'])} survived, "
            f"{payload['graveyard']} discarded, "
            f"{payload['multiple_testing']['bh_survivors']} pass BH"
        )
        for sv in payload["survivors"][:5]:
            print(
                f"    {sv['hypothesis']}: train {sv['train']['hit']}% "
                f"(+{sv['lift_exc_pp']}pp) · OOS +{sv['oos_lift_pp']}pp "
                f"(n={sv['train']['n']}/{sv['oos']['n']}, BH {'pass' if sv['bh_pass'] else 'fail'})"
            )
    print(f"  Wrote {os.path.relpath(OUT_PATH, _BASE)}")


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
