"""
report_outcomes.py — the report self-grading loop (Phase 2).

Every one-pager memo logs {rating, confidence, entry price} at generation
time (hook in scanner.run_single_ticker_lookup). This module runs nightly:
matures entries at +21 sessions (~30 calendar days) against actual closes,
grades direction-aware (BUY/WATCH win when up, SHORT/AVOID when down), and
publishes per-rating stats GATED at n>=30 — "accruing" until real. This is
what eventually turns "confidence 78" from an opinion into a measured
number.

    python report_outcomes.py            # mature + emit
    python report_outcomes.py --dry-run  # print, don't write
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from statistics import mean, median

_BASE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(_BASE, "data", "report_outcomes_log.json")
OUT_PATH = os.path.join(_BASE, "docs", "reports", "report_outcomes.json")

HOLD_S = 21          # sessions to maturation (~30 calendar days)
MIN_N = 30           # gate per rating bucket
MAX_MATURE = 40
BULLISH = ("BUY", "WATCH")


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def mature(entries):
    pending = {}
    for r in entries:
        if "ret" not in r and r.get("t") and r.get("date"):
            pending.setdefault(r["t"], []).append(r)
    if not pending:
        return 0
    import yfinance as yf
    tickers = list(pending.keys())[:MAX_MATURE]
    data = yf.download(tickers, period="6mo", interval="1d",
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
            # entry = close on/after report date (reports run intraday)
            later = [i for i, d in enumerate(dates) if d >= r["date"]]
            if not later:
                continue
            i0 = later[0]
            if i0 + HOLD_S >= len(vals) or vals[i0] <= 0:
                continue                       # not matured yet
            ret = 100.0 * (vals[i0 + HOLD_S] / vals[i0] - 1.0)
            r["ret"] = round(ret, 2)
            r["win"] = (ret > 0) if r.get("rating") in BULLISH else (ret < 0)
            graded += 1
    return graded


def stats(entries):
    by = {}
    for r in entries:
        if "ret" in r and r.get("rating"):
            by.setdefault(r["rating"], []).append(r)
    out, total = {}, 0
    for rating, rows in by.items():
        total += len(rows)
        if len(rows) < MIN_N:
            out[rating] = {"status": "accruing", "n": len(rows),
                           "activates_at": MIN_N}
            continue
        # direction-aware captured move so BUY and SHORT read the same way
        capt = [(-r["ret"] if rating not in BULLISH else r["ret"])
                for r in rows]
        confs = [r["confidence"] for r in rows
                 if isinstance(r.get("confidence"), (int, float))]
        out[rating] = {
            "status": "active", "n": len(rows),
            "win_rate": round(100 * sum(1 for r in rows if r["win"])
                              / len(rows)),
            "avg": round(mean(capt), 1), "median": round(median(capt), 1),
            "avg_stated_conf": round(mean(confs)) if confs else None,
            "hold_sessions": HOLD_S,
        }
    return out, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    log = _load(LOG_PATH) or {"reports": []}
    entries = log.get("reports") or []
    graded = mature(entries)
    st, total = stats(entries)
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "logged": len(entries), "total_graded": total,
        "hold_sessions": HOLD_S, "min_n": MIN_N,
        "ratings": st,
        "note": ("Memo ratings graded at +21 sessions vs actual closes, "
                 "direction-aware. Stats gated at n>=30 per rating; "
                 "avg_stated_conf vs win_rate is the calibration read — "
                 "nothing publishes before it's real."),
    }
    print(f"  report outcomes: {len(entries)} logged · {graded} newly "
          f"matured · {total} graded total")
    if args.dry_run:
        print(json.dumps(payload, indent=1)[:2000])
        return
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, separators=(",", ":"))
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"  Wrote {os.path.relpath(OUT_PATH, _BASE)}")


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
