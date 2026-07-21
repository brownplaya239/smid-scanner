"""
flow_lifecycle.py — did the flagged flow actually STICK?

Tracks every contract that reaches a daily Top-list and follows its
open interest forward, so the pane can mark a position OPEN / PARTIAL /
CLOSED instead of leaving the reader to guess whether the "whale" is
still there.

WHAT OI CAN AND CANNOT TELL US (the honesty spine of this module):
  * OI settles OVERNIGHT at the OCC. It is NOT live. A contract flagged
    today carries yesterday's settled OI all day — today's flow shows up
    in tomorrow's number. So a same-day verdict is IMPOSSIBLE and every
    day-0 row is PENDING by construction, never guessed.
  * OI is NET across ALL market participants. If the original buyer sold
    5,000 and a different fund bought 5,000, OI is unchanged. A decline
    therefore means "positions in this contract were net reduced" — it
    can NEVER be attributed to the specific trader we flagged.
  * Exercise/assignment also removes OI and is indistinguishable from a
    close.
  * OI necessarily goes to zero at expiry: contracts past expiry are
    marked EXPIRED, never UNWOUND.

Verdicts (thresholds mirror uoa_alpha.oi_status so the pane and the
ledger never disagree):
  first settlement after the flag, retained = dOI / flag-day volume
    >= 50%  OPEN            flow opened positions that stuck
    15-50%  PARTIAL         some stuck, the rest closed intraday
    <  15%  CLOSED          day-trade / not a new position
  subsequent sessions, versus the position's PEAK OI
    >= 80% of peak  HELD
    30-80%          TRIMMED
    <  30%          UNWOUND

  data/flow_lifecycle.json          tracking state (repo-committed)
  docs/reports/flow_lifecycle.json  compact lookup for the Top pane

    python flow_lifecycle.py            # nightly: register + verdict
    python flow_lifecycle.py --dry-run  # print, write nothing
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytz

ET = pytz.timezone("America/New_York")
_BASE = os.path.dirname(os.path.abspath(__file__))
LATEST = os.path.join(_BASE, "docs", "reports", "uoa_latest.json")
TOPLIST = os.path.join(_BASE, "docs", "reports", "uoa_top25_daily.json")
STATE = os.path.join(_BASE, "data", "flow_lifecycle.json")
OUT = os.path.join(_BASE, "docs", "reports", "flow_lifecycle.json")

CFG = {
    "open_pct": 50,        # >= this share of flow volume stuck -> OPEN
    "partial_pct": 15,     # >= this -> PARTIAL, below -> CLOSED
    "held_frac": 0.80,     # >= this share of peak OI -> HELD
    "trimmed_frac": 0.30,  # >= this -> TRIMMED, below -> UNWOUND
    "retain_days": 45,     # drop tracked contracts older than this
    "publish_window_days": 45,  # lookup window (matches archive depth)
    "min_stats_n": 30,     # gate for published aggregate stats
    "max_fetch_tickers": 60,   # per-night chain fetches (API budget)
}


def _load(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _et_today():
    return datetime.now(ET).strftime("%Y-%m-%d")


def key_of(ticker, expiry, typ, strike):
    """Same identity the Top pane uses, so the frontend joins directly."""
    return "%s|%s|%s|%g" % (ticker, expiry, typ, float(strike))


# ── 1. register today's Top-list contracts with their flag baseline ─────

def register(state, today):
    """Baseline for a contract flagged today: OI as settled at the PRIOR
    close (uoa_latest.open_interest during the session) + the day's
    cumulative flow volume. Both are point-in-time on the flag date; the
    verdict needs tomorrow's settlement."""
    top = _load(TOPLIST) or {}
    snap = _load(LATEST) or {}
    rows = {r.get("contract"): r for r in (snap.get("rows") or [])
            if r.get("contract")}
    by_key = {}
    for r in rows.values():
        if r.get("expiry") and r.get("type") and r.get("strike") is not None:
            by_key[key_of(r["ticker"], r["expiry"], r["type"],
                          r["strike"])] = r
    added = 0
    for row in (top.get("rows") or []):
        k = key_of(row["ticker"], row["expiry"], row["type"], row["strike"])
        src = by_key.get(k)
        if not src:
            continue          # not in the latest batch — no fresh OI read
        c = state["contracts"].get(k)
        if c is None:
            state["contracts"][k] = {
                "occ": src.get("contract"),
                "ticker": row["ticker"], "type": row["type"],
                "strike": row["strike"], "expiry": row["expiry"],
                "flag_date": today,
                "baseline_oi": src.get("open_interest"),
                "flow_vol": src.get("volume"),
                "side": row.get("side"),
                "liquidity": row.get("liquidity"),
                "best_rank": row.get("rank"),
                "peak_oi": src.get("open_interest"),
                "series": [],
                "status": "PENDING",
            }
            added += 1
        else:
            # re-flagged on a later day: keep the ORIGINAL baseline (the
            # question is whether the FIRST position stuck), but track
            # the larger cumulative flow and the best rank achieved.
            c["flow_vol"] = max(c.get("flow_vol") or 0,
                                src.get("volume") or 0)
            if row.get("rank") and row["rank"] < (c.get("best_rank") or 999):
                c["best_rank"] = row["rank"]
    return added


# ── 2. current OI for tracked contracts needing a verdict ───────────────

def _needs_oi(c, today):
    if c.get("flag_date") == today:
        return False                     # settles tonight — nothing to read
    if c.get("expiry") and c["expiry"] < today:
        return False                     # expired: handled without a fetch
    last = (c.get("series") or [{}])[-1].get("date") if c.get("series") else None
    return last != today                 # already read today?


def fetch_oi(state, today):
    """One chain snapshot per unique ticker covers all of its tracked
    contracts. Budget-capped; CI-only in practice (needs POLYGON_API_KEY
    — locally this no-ops and the module still runs)."""
    need = [c for c in state["contracts"].values() if _needs_oi(c, today)]
    if not need:
        return 0, 0
    tickers = []
    for c in need:
        if c["ticker"] not in tickers:
            tickers.append(c["ticker"])
    tickers = tickers[:CFG["max_fetch_tickers"]]
    try:
        import polygon_data as pg
    except Exception:
        return 0, len(need)
    oi_map = {}
    fetched = 0
    for t in tickers:
        try:
            for con in pg.option_chain(t):
                ct = (con.get("details") or {}).get("ticker")
                if ct:
                    oi_map[ct] = con.get("open_interest") or 0
            fetched += 1
        except Exception:
            continue
    updated = 0
    for c in need:
        oi = oi_map.get(c.get("occ"))
        if oi is None:
            continue
        c.setdefault("series", []).append({"date": today, "oi": oi})
        c["series"] = c["series"][-12:]
        c["peak_oi"] = max(c.get("peak_oi") or 0, oi)
        updated += 1
    return updated, len(need) - updated


# ── 3. classify ────────────────────────────────────────────────────────

def classify(state, today):
    for c in state["contracts"].values():
        # expiry dominates: OI necessarily decays to 0 at expiration, so
        # an expired contract is never reported as "unwound"
        if c.get("expiry") and c["expiry"] < today:
            if c.get("status") in ("PENDING", None):
                c["status"] = "EXPIRED_UNRESOLVED"
            elif not str(c.get("status", "")).startswith("EXPIRED"):
                c["status"] = "EXPIRED_" + c["status"]
            continue
        ser = c.get("series") or []
        if not ser:
            c["status"] = "PENDING"
            continue
        base = c.get("baseline_oi")
        vol = c.get("flow_vol") or 0
        if base is None or vol <= 0:
            c["status"] = "UNKNOWN"
            continue
        first = ser[0]["oi"]
        retained = round(100.0 * (first - base) / vol)
        c["retained_pct"] = retained
        if retained >= CFG["open_pct"]:
            verdict = "OPEN"
        elif retained >= CFG["partial_pct"]:
            verdict = "PARTIAL"
        else:
            verdict = "CLOSED"
        c["first_verdict"] = verdict
        cur = ser[-1]["oi"]
        c["current_oi"] = cur
        if verdict == "CLOSED" or len(ser) == 1:
            c["status"] = verdict
            continue
        peak = c.get("peak_oi") or first or 1
        frac = cur / peak if peak else 0
        if cur <= (base or 0):
            c["status"] = "UNWOUND"
        elif frac >= CFG["held_frac"]:
            c["status"] = "HELD"
        elif frac >= CFG["trimmed_frac"]:
            c["status"] = "TRIMMED"
        else:
            c["status"] = "UNWOUND"
        c["pct_of_peak"] = round(100 * frac)


def prune(state, today):
    cut = (datetime.strptime(today, "%Y-%m-%d")
           - timedelta(days=CFG["retain_days"])).strftime("%Y-%m-%d")
    for k in [k for k, c in state["contracts"].items()
              if (c.get("flag_date") or "") < cut]:
        del state["contracts"][k]


# ── 4. publish ─────────────────────────────────────────────────────────

def publish(state, today):
    # Covers the live Top list AND the archived sessions the date
    # toggle can reach, so looking back at an old list shows which of
    # those positions actually stuck. Compact array encoding keeps the
    # whole window ~40% the size of per-key objects:
    #   [status, retained_pct, pct_of_peak, baseline_oi, current_oi,
    #    flag_date]
    cutoff = (datetime.strptime(today, "%Y-%m-%d")
              - timedelta(days=CFG["publish_window_days"])
              ).strftime("%Y-%m-%d")
    lookup = {}
    for k, c in state["contracts"].items():
        if (c.get("flag_date") or "") < cutoff:
            continue
        lookup[k] = [c.get("status", "PENDING"), c.get("retained_pct"),
                     c.get("pct_of_peak"), c.get("baseline_oi"),
                     c.get("current_oi"), c.get("flag_date")]
    # gated aggregate: how often does flagged flow actually stick?
    resolved = [c for c in state["contracts"].values()
                if c.get("first_verdict")]
    stats = {"status": "accruing", "n": len(resolved),
             "activates_at": CFG["min_stats_n"]}
    if len(resolved) >= CFG["min_stats_n"]:
        cnt = Counter(c["first_verdict"] for c in resolved)
        n = len(resolved)
        by_side = {}
        for s in set(c.get("side") for c in resolved if c.get("side")):
            sub = [c for c in resolved if c.get("side") == s]
            if len(sub) >= CFG["min_stats_n"]:
                by_side[s] = round(
                    100 * sum(1 for c in sub
                              if c["first_verdict"] == "OPEN") / len(sub))
        stats = {
            "status": "active", "n": n,
            "open_pct": round(100 * cnt["OPEN"] / n),
            "partial_pct": round(100 * cnt["PARTIAL"] / n),
            "closed_pct": round(100 * cnt["CLOSED"] / n),
            "open_rate_by_side": by_side,
        }
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "et_date": today,
        "tracked_total": len(state["contracts"]),
        "encoding": ["status", "retained_pct", "pct_of_peak",
                     "baseline_oi", "current_oi", "flag_date"],
        "contracts": lookup,
        "stats": stats,
        "thresholds": {k: CFG[k] for k in
                       ("open_pct", "partial_pct", "held_frac",
                        "trimmed_frac")},
        "note": ("Open interest settles OVERNIGHT at the OCC — a "
                 "contract flagged today gets its verdict tomorrow, so "
                 "same-day rows read PENDING by construction. OI is NET "
                 "across ALL participants: a decline means positions in "
                 "the contract were net reduced and can never be "
                 "attributed to the specific trader flagged. "
                 "Exercise/assignment is indistinguishable from a close. "
                 "Contracts past expiry are marked EXPIRED, never "
                 "UNWOUND. Educational, not advice."),
    }
    return payload


def self_test():
    """Offline fixtures for every classification path, incl. the traps:
    same-day verdicts must stay PENDING, and expiry must never read as
    an unwind."""
    fails = []

    def chk(name, cond):
        print(("  PASS  " if cond else "  FAIL  ") + name)
        if not cond:
            fails.append(name)

    def mk(**kw):
        c = {"ticker": "TST", "type": "call", "strike": 100.0,
             "expiry": "2026-12-18", "flag_date": "2026-07-20",
             "baseline_oi": 1000, "flow_vol": 10000, "peak_oi": 1000,
             "series": [], "status": "PENDING"}
        c.update(kw)
        return c

    T = "2026-07-21"
    # 1. flagged today -> PENDING regardless of anything else
    s = {"contracts": {"a": mk(flag_date=T, series=[])}}
    classify(s, T)
    chk("1 same-day flag stays PENDING", s["contracts"]["a"]["status"] == "PENDING")

    # 2. OI rose by 60% of flow volume -> OPEN
    s = {"contracts": {"a": mk(series=[{"date": T, "oi": 7000}],
                               peak_oi=7000)}}
    classify(s, T)
    c = s["contracts"]["a"]
    chk("2 retained 60% -> OPEN", c["status"] == "OPEN" and c["retained_pct"] == 60)

    # 3. OI rose by 25% -> PARTIAL
    s = {"contracts": {"a": mk(series=[{"date": T, "oi": 3500}],
                               peak_oi=3500)}}
    classify(s, T)
    chk("3 retained 25% -> PARTIAL",
        s["contracts"]["a"]["status"] == "PARTIAL")

    # 4. OI barely moved -> CLOSED (day trade)
    s = {"contracts": {"a": mk(series=[{"date": T, "oi": 1050}],
                               peak_oi=1050)}}
    classify(s, T)
    chk("4 retained 0.5% -> CLOSED", s["contracts"]["a"]["status"] == "CLOSED")

    # 5. opened then decayed to 35% of peak -> TRIMMED
    s = {"contracts": {"a": mk(series=[{"date": "2026-07-21", "oi": 7000},
                                       {"date": T, "oi": 2450}],
                               peak_oi=7000)}}
    classify(s, T)
    c = s["contracts"]["a"]
    chk("5 35% of peak -> TRIMMED",
        c["status"] == "TRIMMED" and c["first_verdict"] == "OPEN")

    # 6. opened then back to baseline -> UNWOUND
    s = {"contracts": {"a": mk(series=[{"date": "2026-07-21", "oi": 7000},
                                       {"date": T, "oi": 950}],
                               peak_oi=7000)}}
    classify(s, T)
    chk("6 back to baseline -> UNWOUND",
        s["contracts"]["a"]["status"] == "UNWOUND")

    # 7. THE TRAP: an expired contract's OI necessarily goes to 0 —
    #    must read EXPIRED_*, never UNWOUND
    s = {"contracts": {"a": mk(expiry="2026-07-17", status="OPEN",
                               series=[{"date": T, "oi": 0}])}}
    classify(s, T)
    chk("7 expired contract is EXPIRED_*, not UNWOUND",
        s["contracts"]["a"]["status"].startswith("EXPIRED"))

    # 8. expired before any verdict
    s = {"contracts": {"a": mk(expiry="2026-07-17")}}
    classify(s, T)
    chk("8 expired w/o verdict -> EXPIRED_UNRESOLVED",
        s["contracts"]["a"]["status"] == "EXPIRED_UNRESOLVED")

    # 9. no OI read yet -> stays PENDING (never guessed)
    s = {"contracts": {"a": mk(series=[])}}
    classify(s, T)
    chk("9 no OI read -> PENDING", s["contracts"]["a"]["status"] == "PENDING")

    print("\n%d/9 fixtures passed" % (9 - len(fails)))
    return 0 if not fails else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        sys.exit(self_test())
    today = _et_today()
    state = _load(STATE) or {"contracts": {}}
    added = register(state, today)
    updated, missed = fetch_oi(state, today)
    classify(state, today)
    prune(state, today)
    payload = publish(state, today)
    cnt = Counter(c.get("status") for c in state["contracts"].values())
    print("  flow lifecycle: +%d new · %d OI reads (%d unresolved) · "
          "tracking %d · %s" %
          (added, updated, missed, len(state["contracts"]),
           " ".join("%s=%d" % (k, v) for k, v in sorted(cnt.items()))))
    if args.dry_run:
        print(json.dumps(payload["stats"], indent=1))
        return
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"))
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    print("  Wrote flow_lifecycle.json (%d in current top list)"
          % len(payload["contracts"]))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
