"""
missed_opportunities.py — learning loop 5: "what did we miss, and why?"

Post-close self-audit. Takes the session's biggest tradable movers from the
full-market grouped-daily bars, checks each against everything the site
surfaced today (swing grades, options-flow rows, momentum lists), and
attributes every miss to the specific gate that hid it. The goal is to make
the scan pipeline explain its own blind spots — over time the by_reason
counts show which filter is costing the most coverage and deserves tuning.

Writes docs/reports/missed_opportunities.json for the Today's Desk EOD
card. Safe no-op (writes an empty payload) when POLYGON_API_KEY is missing.

    python missed_opportunities.py            # audit the latest session
    python missed_opportunities.py --dry-run  # print, don't write
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone

import polygon_data as pg

_BASE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(_BASE, "docs", "reports", "missed_opportunities.json")
REPORTS = os.path.join(_BASE, "docs", "reports")

MIN_MOVE_PCT   = 5.0            # a "big mover" = |close-to-close| >= this
MIN_PRICE      = 3.0            # skip pennies
MIN_DOLLAR_VOL = 25_000_000     # same tradability floor as the UOA universe
TOP_N          = 25             # movers to audit per session


def _load(name):
    try:
        with open(os.path.join(REPORTS, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _last_sessions():
    """(today, prev) most recent weekday pair — grouped-daily has no
    weekend bars. Holidays fall through: grouped_daily returns {} and the
    audit skips gracefully."""
    d = datetime.now(timezone.utc).date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    p = d - timedelta(days=1)
    while p.weekday() >= 5:
        p -= timedelta(days=1)
    return d.isoformat(), p.isoformat()


def _surfaced_sets():
    """Everything the site showed today, keyed by HOW it was shown."""
    graded = {}                              # ticker -> swing grade
    sw = _load("swing_latest_summary.json") or _load("swing_report.json")
    if sw and sw.get("runs"):
        r = sw["runs"][-1]
        for g, cards in (r.get("grades") or {}).items():
            for c in cards or []:
                if c.get("t"):
                    graded[c["t"]] = g
    flow = set()
    uoa = _load("uoa_latest.json")
    for row in (uoa or {}).get("rows") or []:
        if row.get("ticker"):
            flow.add(row["ticker"])
    momentum = set()
    for name in ("momentum_qm.json", "momentum_stockbee.json"):
        d = _load(name)
        if d and d.get("runs"):
            for row in d["runs"][-1].get("rows") or []:
                if row.get("ticker"):
                    momentum.add(row["ticker"])
    return graded, flow, momentum


# Grades that ARE surfaced prominently (bull ladder, bear ladder, movers on
# the desk). Mid-grades exist in the data but no pane leads with them.
_SURFACED_GRADES = ("A", "B", "F", "G")


def classify(tk, graded, flow, momentum):
    """(covered_by list, missed_reason or None) for one mover."""
    covered = []
    g = graded.get(tk)
    if g and g[:1] in _SURFACED_GRADES:
        covered.append(f"swing {g}")
    if tk in flow:
        covered.append("options flow")
    if tk in momentum:
        covered.append("momentum list")
    if covered:
        return covered, None
    if g:                                    # scanned, graded mid-tier, never shown
        return [], f"graded {g} — below surfacing tier"
    return [], "outside scan universe — liquidity/coverage floor"


def build(dry=False):
    today, prev = _last_sessions()
    empty = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "session_date": today, "checked": 0, "covered_count": 0,
             "missed_count": 0, "by_reason": {}, "missed": [], "covered": []}
    if not pg.available():
        print("  POLYGON_API_KEY not set — writing empty missed payload.")
        return empty

    bars_t = pg.grouped_daily(today)
    bars_p = pg.grouped_daily(prev)
    if not bars_t or not bars_p:
        print(f"  grouped-daily empty ({today} / {prev}) — holiday? skipping.")
        return empty

    movers = []
    for tk, b in bars_t.items():
        if "." in tk or len(tk) > 5:
            continue                          # pref/warrant/unit classes
        pb = bars_p.get(tk)
        c, v = (b.get("c") or 0), (b.get("v") or 0)
        pc = (pb or {}).get("c") or 0
        if not pb or c < MIN_PRICE or pc <= 0 or c * v < MIN_DOLLAR_VOL:
            continue
        pct = 100.0 * (c - pc) / pc
        if abs(pct) >= MIN_MOVE_PCT:
            movers.append({"ticker": tk, "pct": round(pct, 1),
                           "price": round(c, 2),
                           "dollar_vol": round(c * v)})
    movers.sort(key=lambda m: -abs(m["pct"]))
    movers = movers[:TOP_N]

    graded, flow, momentum = _surfaced_sets()
    missed, covered, by_reason = [], [], {}
    for m in movers:
        cov, reason = classify(m["ticker"], graded, flow, momentum)
        if reason:
            m["reason"] = reason
            missed.append(m)
            key = reason.split(" — ")[-1]
            by_reason[key] = by_reason.get(key, 0) + 1
        else:
            m["covered_by"] = cov
            covered.append(m)

    payload = {
        "generated":     datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_date":  today,
        "checked":       len(movers),
        "covered_count": len(covered),
        "missed_count":  len(missed),
        "by_reason":     by_reason,
        "missed":        missed,
        "covered":       covered[:10],
        "params": {"min_move_pct": MIN_MOVE_PCT, "min_price": MIN_PRICE,
                   "min_dollar_vol": MIN_DOLLAR_VOL, "top_n": TOP_N},
    }
    print(f"  Missed-opportunity audit: {len(movers)} big movers -> "
          f"{len(covered)} covered / {len(missed)} missed "
          f"({', '.join(f'{k}: {v}' for k, v in by_reason.items()) or 'none'})")
    return payload


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    payload = build(dry=args.dry_run)
    if args.dry_run:
        print(json.dumps(payload, indent=1)[:4000])
        return
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"  Wrote {os.path.relpath(OUT_PATH, _BASE)}")


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
