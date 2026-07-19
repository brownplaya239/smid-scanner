"""
scan_outcomes.py — learning loop: momentum-scan historical performance.

Extends the outcome DB (UOA flow, swing setups) to the two independent
momentum breakout scans that were never graded: QM Monthly and Stockbee
Weekly. Every night it logs each scan's current names, then matures each
pick at +HOLD_D sessions against real closes — per-scan win rate, avg /
median forward return, and a close-based drawdown proxy. All names are
long/bullish momentum, so a win is simply a positive forward move.

Unlike the other loops, this one SEEDS from history: momentum_qm.json and
momentum_stockbee.json each carry ~49 dated runs, so the first execution
grades the whole backlog and can publish real stats immediately instead
of accruing for weeks.

GATED like every learned quantity here: per-scan stats publish only at
n >= MIN_N; until then "accruing" with the honest count.

  data/scan_outcomes_log.json      append-only pick log (repo-committed)
  docs/reports/scan_outcomes.json  gated stats for the site

    python scan_outcomes.py            # log + seed + mature + emit
    python scan_outcomes.py --dry-run  # print, don't write
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from statistics import mean, median

import yfinance as yf

_BASE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(_BASE, "docs", "reports")
LOG_PATH = os.path.join(_BASE, "data", "scan_outcomes_log.json")
OUT_PATH = os.path.join(REPORTS, "scan_outcomes.json")

SCANS = {
    "qm":       ("momentum_qm.json",       "QM Monthly"),
    "stockbee": ("momentum_stockbee.json", "Stockbee Weekly"),
}
TOP_N      = 10          # names logged per scan per run
HOLD_D     = 5           # maturation horizon, sessions
MIN_N      = 30          # gate: no stats below this
LOG_CAP_D  = 250         # log retention, days
CHUNK      = 100         # yfinance download batch size


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ── 1. build the pick log from ALL runs in both scan files ──────────────

def collect_picks():
    """Every (scan, ticker, entry_date) across the full run history of both
    scan files. Deduped; the scan JSONs are the source of truth so re-runs
    are idempotent."""
    seen = set()
    days = {}                       # date -> list of pick rows
    for scan, (fname, _label) in SCANS.items():
        d = _load(os.path.join(REPORTS, fname))
        for run in (d or {}).get("runs") or []:
            date = run.get("date")
            if not date:
                continue
            for c in (run.get("rows") or [])[:TOP_N]:
                tk = c.get("ticker")
                if not tk:
                    continue
                key = (scan, tk, date)
                if key in seen:
                    continue
                seen.add(key)
                days.setdefault(date, []).append(
                    {"t": tk, "scan": scan})
    return days


def merge_log(day_map):
    """Merge freshly-collected picks into the log, preserving any already-
    matured outcomes on rows that recur."""
    log = _load(LOG_PATH) or {"days": []}
    by_date = {d["date"]: d for d in log.get("days") or []}
    for date, rows in day_map.items():
        existing = {(r["scan"], r["t"]): r
                    for r in (by_date.get(date, {}).get("rows") or [])}
        merged = []
        for r in rows:
            prior = existing.get((r["scan"], r["t"]))
            merged.append(prior if prior else r)
        by_date[date] = {"date": date, "rows": merged}
    days = sorted(by_date.values(), key=lambda d: d["date"])[-LOG_CAP_D:]
    return {"days": days}


# ── 2. mature picks at +HOLD_D sessions ─────────────────────────────────

def mature(log):
    pending = {}                    # ticker -> list of (date, row)
    for day in log["days"]:
        for r in day["rows"]:
            if "ret" not in r:
                pending.setdefault(r["t"], []).append((day["date"], r))
    if not pending:
        return 0
    tickers = list(pending.keys())
    graded = 0
    for i in range(0, len(tickers), CHUNK):
        batch = tickers[i:i + CHUNK]
        try:
            data = yf.download(batch, period="10mo", interval="1d",
                               auto_adjust=True, group_by="ticker",
                               progress=False, threads=True)
        except Exception:
            continue
        for tk in batch:
            try:
                df = data[tk].dropna(how="all") if len(batch) > 1 else data
                closes = df["Close"].dropna()
            except Exception:
                continue
            dates = [d.date().isoformat() for d in closes.index]
            vals = [float(v) for v in closes.tolist()]
            for flag_date, r in pending[tk]:
                if flag_date not in dates:
                    continue
                i0 = dates.index(flag_date)
                if i0 + HOLD_D >= len(vals):
                    continue                  # not matured yet
                e, x = vals[i0], vals[i0 + HOLD_D]
                if e <= 0:
                    continue
                ret = 100.0 * (x / e - 1.0)
                window = vals[i0 + 1: i0 + HOLD_D + 1]
                dd = 100.0 * (min(window) / e - 1.0)
                r["ret"] = round(ret, 2)
                r["dd"] = round(dd, 2)
                r["win"] = ret > 0
                graded += 1
    return graded


# ── 3. gated per-scan stats ─────────────────────────────────────────────

def _rich(rows):
    rets = [r["ret"] for r in rows]
    dds = [r["dd"] for r in rows]
    wins = [r for r in rows if r["win"]]
    losses = [r for r in rows if not r["win"]]
    avg_win = mean(r["ret"] for r in wins) if wins else 0.0
    avg_loss = mean(r["ret"] for r in losses) if losses else 0.0
    gp = sum(r["ret"] for r in wins)
    gl = -sum(r["ret"] for r in losses)
    return {
        "status": "active", "n": len(rows),
        "win_rate": round(100.0 * len(wins) / len(rows)),
        "ev": round(mean(rets), 2),
        "median": round(median(rets), 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(gp / gl, 2) if gl > 0 else None,
        "avg_dd": round(mean(dds), 2) if dds else None,
        "hold_d": HOLD_D,
    }


def stats(log):
    by_scan = {}
    allrows = []
    for day in log["days"]:
        for r in day["rows"]:
            if "ret" in r:
                by_scan.setdefault(r["scan"], []).append(r)
                allrows.append(r)
    out = {}
    for scan, rows in by_scan.items():
        out[scan] = (_rich(rows) if len(rows) >= MIN_N
                     else {"status": "accruing", "n": len(rows),
                           "activates_at": MIN_N})
    overall = (_rich(allrows) if len(allrows) >= MIN_N
               else {"status": "accruing", "n": len(allrows),
                     "activates_at": MIN_N})
    return out, overall, len(allrows)


def build():
    day_map = collect_picks()
    if not day_map:
        print("  scan outcomes: no scan runs found — skipping")
        return None, None
    log = merge_log(day_map)
    graded = mature(log)
    per_scan, overall, total = stats(log)
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hold_days": HOLD_D,
        "min_n": MIN_N,
        "logged_days": len(log["days"]),
        "total_graded": total,
        "labels": {k: v[1] for k, v in SCANS.items()},
        "by_scan": per_scan,
        "overall": overall,
        "note": ("Per-scan next-" + str(HOLD_D) + "-session outcomes of the "
                 "QM Monthly and Stockbee momentum scans (long/bullish). "
                 "Seeded from the full run history; stats gated at n>=" +
                 str(MIN_N) + " — 'accruing' until real. Educational, not "
                 "advice."),
    }
    print("  scan outcomes: %d picks logged across %d days, matured %d, "
          "total graded %d" % (sum(len(d["rows"]) for d in log["days"]),
                               len(log["days"]), graded, total))
    return log, payload


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    log, payload = build()
    if payload is None:
        return
    if args.dry_run:
        print(json.dumps(payload, indent=1)[:3000])
        return
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, separators=(",", ":"))
    os.makedirs(REPORTS, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    print("  Wrote %s + log" % os.path.relpath(OUT_PATH, _BASE))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
