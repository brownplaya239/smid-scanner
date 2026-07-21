"""
uoa_daily_top.py — Top 25 Daily Unusual Contracts (cumulative cross-batch).

Runs after every UOA scan batch (uoa.yml, 6-9x/day). Folds the batch
snapshot (docs/reports/uoa_latest.json) into a per-day accumulator keyed
by exact contract identity, rescores the whole day, and publishes the
diversified Top 25 for the Options Flow pane.

FEED SEMANTICS (determined from uoa_scanner.py / Polygon snapshot API):
  * volume, premium, sweeps, blocks, repeat_count are DAY-CUMULATIVE per
    contract in every snapshot — each batch reports the running total for
    the session, not an increment. Cross-batch merge is therefore MAX per
    field, NEVER sum. Summing would double-count massively.
  * open_interest / prev_oi / oi_delta are STATIC intraday (OCC settles
    overnight); oi_delta is yesterday->today and cannot move between
    batches. It is a daily fact, not a per-batch signal.
  * bid/ask/mid are null in the delayed feed; execution tendency comes
    from the scanner's ask_pct/bid_pct approximation (side_method:
    "approx") and spread quality from the liquidity grade.

Day boundary: keyed to the ET date of the BATCH's generated timestamp
(not wall-clock at run time) so a delayed CI run just after midnight
cannot wipe the prior session. New ET date -> fresh accumulator.

Idempotent: a batch's generated timestamp is recorded in batches_seen;
re-running on the same snapshot is a no-op merge.

  data/uoa_daily_top.json          per-day accumulator (repo-committed)
  docs/reports/uoa_top25_daily.json  published ranking for the site

    python uoa_daily_top.py            # merge current batch + publish
    python uoa_daily_top.py --dry-run  # print ranking, write nothing
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import sys
from datetime import datetime

import pytz

ET = pytz.timezone("America/New_York")
_BASE = os.path.dirname(os.path.abspath(__file__))
LATEST_PATH = os.path.join(_BASE, "docs", "reports", "uoa_latest.json")
ACC_PATH = os.path.join(_BASE, "data", "uoa_daily_top.json")
OUT_PATH = os.path.join(_BASE, "docs", "reports", "uoa_top25_daily.json")

# ── single tunable config object ────────────────────────────────────────
CFG = {
    # eligibility (noise gate before any scoring)
    "min_premium": 250_000,      # $ cumulative day premium
    "min_volume": 200,           # contracts
    "min_vol_oi": 1.0,           # day volume vs prior OI
    "max_pct_otm_unconfirmed": 25.0,  # far-OTM lottos need confirmation
    "liq_reject": {"D", "F"},    # unusably illiquid grades
    # ranking weights (see daily_score)
    "w_base": 0.55,              # learned trade_score share
    "w_premium": 12, "w_voloi": 10, "w_oi": 6,
    "w_repeat": 7, "w_batches": 8, "w_side": 6,
    "golden_bonus": 4, "sweep_bonus": 2,
    "otm_knee": 15.0, "otm_slope": 0.4, "otm_cap": 8,
    "seller_penalty": 6, "mixed_penalty": 3,
    # diversification
    "per_ticker_cap": 2,
    "top_n": 25,
}

# Point-in-time fields refreshed from the newest observation of a contract;
# cumulative fields (below) merge via max().
LATEST_FIELDS = [
    "spot", "pct_otm", "is_otm", "iv", "last_price", "liquidity",
    "ask_pct", "bid_pct", "side_method", "flow_side", "direction",
    "opening", "tier", "dte", "earnings_days", "last_print_ts",
    "sector", "cap_bucket", "edge_adj", "oi_delta", "oi_delta_pct",
    "open_interest", "prev_oi", "vol_oi",
]
CUM_FIELDS = ["premium", "volume", "sweeps", "blocks", "sweep_premium",
              "repeat_count", "biggest_print", "trade_score"]


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _et_date(iso_ts):
    """ET session date of an ISO timestamp string."""
    dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    return dt.astimezone(ET).strftime("%Y-%m-%d")


def contract_key(r):
    """Stable exact-contract identity: never merges across expiry, type,
    or strike. Strike normalized via %g (382.50 == 382.5)."""
    return "%s|%s|%s|%g" % (r["ticker"], r["expiry"], r["type"],
                            float(r["strike"]))


# ── 1. merge the current batch into the day accumulator ─────────────────

def merge_batch(acc, snap):
    gen = snap.get("generated")
    rows = snap.get("rows") or []
    if not gen or not rows:
        return acc, False
    day = _et_date(gen)
    if acc.get("et_date") != day:
        acc = {"et_date": day, "batches_seen": [], "contracts": {}}
    if gen in acc["batches_seen"]:
        return acc, False          # idempotent re-run on same snapshot
    acc["batches_seen"].append(gen)
    con = acc["contracts"]
    for r in rows:
        if not (r.get("ticker") and r.get("expiry") and r.get("type")
                and r.get("strike") is not None):
            continue               # invalid identity -> reject outright
        k = contract_key(r)
        c = con.get(k)
        if c is None:
            c = {"ticker": r["ticker"], "type": r["type"],
                 "strike": r["strike"], "expiry": r["expiry"],
                 "first_seen": gen, "batch_count": 0}
            con[k] = c
        c["last_seen"] = gen
        c["batch_count"] += 1
        for f in CUM_FIELDS:       # day-cumulative -> max, never sum
            v = r.get(f)
            if v is not None:
                c[f] = max(c.get(f) or 0, v)
        for f in LATEST_FIELDS:    # point-in-time -> newest observation
            if r.get(f) is not None:
                c[f] = r[f]
    return acc, True


# ── 2. eligibility (noise gate) ─────────────────────────────────────────

def eligible(c):
    if (c.get("premium") or 0) < CFG["min_premium"]:
        return False
    if (c.get("volume") or 0) < CFG["min_volume"]:
        return False
    if (c.get("vol_oi") or 0) < CFG["min_vol_oi"]:
        return False
    if (c.get("liquidity") or "").upper() in CFG["liq_reject"]:
        return False
    po = c.get("pct_otm")
    confirmed = ((c.get("sweeps") or 0) > 0 or (c.get("blocks") or 0) > 0
                 or (c.get("repeat_count") or 0) >= 3
                 or (c.get("oi_delta") or 0) > 0
                 or c["batch_count"] >= 2)
    if po is not None and po > CFG["max_pct_otm_unconfirmed"] and not confirmed:
        return False
    return True


# ── 3. explainable dailyUnusualScore 0-100 ──────────────────────────────

def _pctile_fn(vals):
    s = sorted(vals)
    n = max(1, len(s) - 1)
    return lambda v: bisect.bisect_left(s, v) / n


def score_all(contracts, n_batches):
    """Score every eligible contract against today's eligible universe.
    Base = learned trade_score (already blends premium/vol_oi/opening/
    repeat + the nightly learner's edge_adj); the rest layers on daily
    recurrence + execution-consistency evidence. Percentile features are
    universe-normalized but weight-capped so one batch's churn cannot
    swing rankings drastically."""
    elig = [c for c in contracts.values() if eligible(c)]
    if not elig:
        return []
    p_prem = _pctile_fn([c["premium"] for c in elig])
    p_voloi = _pctile_fn([c.get("vol_oi") or 0 for c in elig])
    for c in elig:
        s = CFG["w_base"] * (c.get("trade_score") or 0)
        s += CFG["w_premium"] * p_prem(c["premium"])
        s += CFG["w_voloi"] * p_voloi(c.get("vol_oi") or 0)
        s += CFG["w_oi"] * min(1.0, (c.get("oi_delta") or 0) / 2000.0)
        s += CFG["w_repeat"] * min(1.0, (c.get("repeat_count") or 0) / 10.0)
        # Recurrence across DISTINCT batches — the pane's defining signal.
        # Normalized by batches available so 9am names aren't penalized
        # early in the day; still requires >=2 batches to earn anything.
        if n_batches >= 2 and c["batch_count"] >= 2:
            s += CFG["w_batches"] * min(1.0, (c["batch_count"] - 1)
                                        / max(1, n_batches - 1))
        ap = c.get("ask_pct")
        if ap is not None:
            s += CFG["w_side"] * (abs(ap - 50) / 50.0)
        if c.get("sweeps") and c.get("blocks"):
            s += CFG["golden_bonus"]
        elif c.get("sweeps"):
            s += CFG["sweep_bonus"]
        # penalties
        po = c.get("pct_otm") or 0
        if po > CFG["otm_knee"]:
            s -= min(CFG["otm_cap"], (po - CFG["otm_knee"]) * CFG["otm_slope"])
        if c.get("flow_side") in ("put_seller", "call_seller"):
            s -= CFG["seller_penalty"]     # income flow: demoted, labeled
        if ap is not None and 40 <= ap <= 60 and not (
                c.get("sweeps") and c.get("blocks")):
            s -= CFG["mixed_penalty"]      # mixed / contradictory tape
        c["daily_score"] = max(0, min(100, round(s)))
    return elig


# ── 4. bias + reason labels ─────────────────────────────────────────────

def bias_of(c):
    """Direction starts from the contract type (call=bull, put=bear),
    then the execution tape must CONFIRM it: only >=55% of prints at/near
    the ask (aggressive buying) earns the plain label. Anything less —
    balanced tape OR bid-heavy prints that look like selling the scanner
    didn't classify — gets the '?' qualifier. Scanner-classified premium
    selling (flow_side) inverts to the income read ('*')."""
    fs = c.get("flow_side")
    if fs == "put_seller":
        return "bull_income"       # premium selling: directionally bullish
    if fs == "call_seller":
        return "bear_income"
    ap = c.get("ask_pct")
    base = "bull" if c["type"] == "call" else "bear"
    if ap is None:
        return base
    return base if ap >= 55 else base + "_mixed"


def reason_of(c, n_batches):
    ed = c.get("earnings_days")
    if ed is not None and 0 <= ed <= 7:
        return "EARNINGS POSITIONING"
    if n_batches >= 2 and c["batch_count"] >= max(2, n_batches // 2):
        return "REPEAT ACROSS BATCHES"
    if (c.get("oi_delta_pct") or 0) >= 200:
        return "LARGE OI EXPANSION"
    if c.get("sweeps") and c.get("blocks") and (c.get("ask_pct") or 0) >= 60:
        return "AGGRESSIVE ASK SWEEPS"
    if (c.get("repeat_count") or 0) >= 8:
        return "REPEAT OPENING FLOW"
    if c.get("sweeps") and c.get("blocks"):
        return "GOLDEN SWEEP"
    if (c.get("premium") or 0) >= 3e6:
        return "HIGH PREMIUM + CONFIRMATION"
    return "LIQUID REPEAT FLOW"


# ── 5. diversified Top-N ────────────────────────────────────────────────

def top_n(elig, n_batches):
    liq_rank = {"A": 0, "B": 1, "C": 2, "D": 3, "F": 4}
    elig.sort(key=lambda c: (
        -c["daily_score"],
        -c["batch_count"],
        -(c.get("premium") or 0),
        liq_rank.get((c.get("liquidity") or "C").upper(), 2),
        c.get("last_seen") or "",
        contract_key(c),
    ))
    out, per_tk = [], {}
    for c in elig:
        if per_tk.get(c["ticker"], 0) >= CFG["per_ticker_cap"]:
            continue
        out.append(c)
        per_tk[c["ticker"]] = per_tk.get(c["ticker"], 0) + 1
        if len(out) >= CFG["top_n"]:
            break
    return out


def build_payload(acc, ranked, n_elig, n_total):
    n_b = len(acc["batches_seen"])
    rows = []
    for i, c in enumerate(ranked, 1):
        rows.append({
            "rank": i, "ticker": c["ticker"], "type": c["type"],
            "strike": c["strike"], "expiry": c["expiry"],
            "dte": c.get("dte"), "bias": bias_of(c),
            "score": c["daily_score"],
            "premium": c.get("premium"), "last_price": c.get("last_price"),
            "vol_oi": c.get("vol_oi"),
            "oi_delta": c.get("oi_delta"),
            "batch_count": c["batch_count"],
            "obs_count": c.get("repeat_count"),
            "first_seen": c.get("first_seen"),
            "last_seen": c.get("last_seen"),
            "last_print_ts": c.get("last_print_ts"),
            "ask_pct": c.get("ask_pct"), "liquidity": c.get("liquidity"),
            "iv": c.get("iv"), "pct_otm": c.get("pct_otm"),
            "earnings_days": c.get("earnings_days"),
            "reason": reason_of(c, n_b),
        })
    return {
        "generated": acc["batches_seen"][-1] if acc["batches_seen"] else None,
        "et_date": acc["et_date"],
        "batches_today": n_b,
        "universe_total": n_total,
        "universe_eligible": n_elig,
        "rows": rows,
        "note": ("Cumulative ranking across all of today's scan batches "
                 "(delayed data). Contracts dedup by exact identity; "
                 "day-cumulative volume/premium merge as max across "
                 "batches. Max 2 per ticker. Educational, not advice."),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    snap = _load(LATEST_PATH)
    if not snap or not snap.get("rows"):
        print("  daily top: no uoa_latest.json rows — nothing to merge")
        return
    acc = _load(ACC_PATH) or {"et_date": None, "batches_seen": [],
                              "contracts": {}}
    acc, merged = merge_batch(acc, snap)
    n_b = len(acc["batches_seen"])
    elig = score_all(acc["contracts"], n_b)
    ranked = top_n(elig, n_b)
    payload = build_payload(acc, ranked, len(elig), len(acc["contracts"]))

    print("  daily top: %s · %d batches · %d contracts tracked · "
          "%d eligible · top %d published%s" %
          (acc["et_date"], n_b, len(acc["contracts"]), len(elig),
           len(ranked), "" if merged else " (batch already merged)"))
    if args.dry_run:
        for r in payload["rows"][:10]:
            print("   #%2d %-5s %-4s %8g %s  score=%d prem=$%.1fM bat=%d %s"
                  % (r["rank"], r["ticker"], r["type"].upper(), r["strike"],
                     r["expiry"], r["score"], (r["premium"] or 0) / 1e6,
                     r["batch_count"], r["reason"]))
        return
    os.makedirs(os.path.dirname(ACC_PATH), exist_ok=True)
    with open(ACC_PATH, "w", encoding="utf-8") as f:
        json.dump(acc, f, separators=(",", ":"))
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    print("  Wrote uoa_top25_daily.json + accumulator")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
