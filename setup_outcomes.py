"""
setup_outcomes.py — learning loop: setup-level historical performance.

The UOA outcome DB answers "do flow signals work?". This module answers the
same question for SWING SETUPS: every night it logs the top-graded names
(bull A-tier ladder, bear F/G ladder), then matures each cohort at +5
sessions against actual closes — win rate, avg/median return, and a
close-based drawdown proxy, PER GRADE.

GATED like every learned quantity on this site: per-grade stats publish
only at n >= MIN_N; until then the payload reports "accruing" with the
honest count. No number is ever shown before it exists.

  data/setup_outcomes_log.json    the append-only cohort log (repo-committed)
  docs/reports/setup_outcomes.json  gated stats for the email + site

    python setup_outcomes.py            # log today's cohort + mature + emit
    python setup_outcomes.py --dry-run  # print, don't write
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from statistics import mean, median

import yfinance as yf

_BASE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(_BASE, "docs", "reports")
LOG_PATH = os.path.join(_BASE, "data", "setup_outcomes_log.json")
OUT_PATH = os.path.join(REPORTS, "setup_outcomes.json")

BULL_GRADES = ("A+", "A", "A-")
BEAR_GRADES = ("F", "F+", "F-", "G", "G+")
PER_GRADE   = 5          # names logged per grade per night
HOLD_D      = 5          # maturation horizon, sessions
MIN_N       = 30         # gate: no stats below this
LOG_CAP_D   = 250        # log retention, days
MAX_MATURE  = 40         # tickers matured per run (yfinance budget)


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ── 1. log tonight's cohort ─────────────────────────────────────────────

def todays_cohort():
    sw = (_load(os.path.join(REPORTS, "swing_latest_summary.json"))
          or _load(os.path.join(REPORTS, "swing_report.json")))
    if not sw or not sw.get("runs"):
        return None, []
    run = sw["runs"][-1]
    date = run.get("date") or datetime.now(timezone.utc).date().isoformat()
    rows = []
    grades = run.get("grades") or {}
    for g in BULL_GRADES:
        for c in (grades.get(g) or [])[:PER_GRADE]:
            if c.get("t"):
                rows.append({"t": c["t"], "grade": g, "dir": "bull"})
    for g in BEAR_GRADES:
        for c in (grades.get(g) or [])[:PER_GRADE]:
            if c.get("t"):
                rows.append({"t": c["t"], "grade": g, "dir": "bear"})
    return date, rows


def append_log(date, rows):
    log = _load(LOG_PATH) or {"days": []}
    days = [d for d in log.get("days") or [] if d.get("date") != date]
    days.append({"date": date, "rows": rows})
    days.sort(key=lambda d: d.get("date") or "")
    days = days[-LOG_CAP_D:]
    return {"days": days}


# ── 2. mature cohorts at +HOLD_D sessions ───────────────────────────────

def mature(log):
    """Fill outcomes for day-entries that are >= HOLD_D sessions old and not
    yet graded. Entry = close on flag date; exit = close HOLD_D sessions on;
    dd = worst close vs entry during the hold (sign-adjusted per direction)."""
    pending = {}
    for day in log["days"]:
        for r in day["rows"]:
            if "ret" not in r:
                pending.setdefault(r["t"], []).append((day["date"], r))
    if not pending:
        return 0
    tickers = list(pending.keys())[:MAX_MATURE]
    data = yf.download(tickers, period="4mo", interval="1d",
                       auto_adjust=True, group_by="ticker",
                       progress=False, threads=True)
    graded = 0
    for tk in tickers:
        try:
            df = data[tk].dropna(how="all")
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
                continue                      # not matured yet
            e, x = vals[i0], vals[i0 + HOLD_D]
            if e <= 0:
                continue
            ret = 100.0 * (x / e - 1.0)
            window = vals[i0 + 1: i0 + HOLD_D + 1]
            if r["dir"] == "bull":
                dd = 100.0 * (min(window) / e - 1.0)
                win = ret > 0
            else:
                dd = 100.0 * (max(window) / e - 1.0)   # adverse = rally
                win = ret < 0
            r["ret"] = round(ret, 2)
            r["dd"] = round(dd, 2)
            r["win"] = win
            graded += 1
    return graded


# ── 3. gated per-grade stats ────────────────────────────────────────────

def stats(log):
    by_grade = {}
    for day in log["days"]:
        for r in day["rows"]:
            if "ret" in r:
                by_grade.setdefault(r["grade"], []).append(r)
    out, total = {}, 0
    for g, rows in by_grade.items():
        total += len(rows)
        if len(rows) < MIN_N:
            out[g] = {"status": "accruing", "n": len(rows),
                      "activates_at": MIN_N}
            continue
        rets = [r["ret"] for r in rows]
        # bear cohorts win when the name FALLS — report the direction-aware
        # captured move so bull and bear stats read the same way
        capt = [(-x if rows[0]["dir"] == "bear" else x) for x in rets]
        dds = [r["dd"] for r in rows]
        out[g] = {
            "status": "active", "n": len(rows),
            "win_rate": round(100.0 * sum(1 for r in rows if r["win"])
                              / len(rows)),
            "avg": round(mean(capt), 1),
            "median": round(median(capt), 1),
            "avg_dd": round(mean(abs(d) for d in dds)
                            if dds else 0, 1) * -1,
            "hold_d": HOLD_D,
        }
    return out, total


def build(dry=False):
    date, rows = todays_cohort()
    if date is None:
        print("  setup outcomes: no swing run — skipping")
        return None, None
    log = append_log(date, rows)
    graded = mature(log)
    st, total_graded = stats(log)
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hold_days": HOLD_D,
        "min_n": MIN_N,
        "logged_days": len(log["days"]),
        "total_graded": total_graded,
        "grades": st,
        "note": ("Per-grade next-" + str(HOLD_D) + "-session outcomes of the "
                 "top-" + str(PER_GRADE) + " names per grade. Stats gated at "
                 "n>=" + str(MIN_N) + " — 'accruing' until real."),
    }
    print(f"  setup outcomes: logged {len(rows)} names for {date}, "
          f"matured {graded}, total graded {total_graded}")
    return log, payload


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    log, payload = build(dry=args.dry_run)
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
    print(f"  Wrote {os.path.relpath(OUT_PATH, _BASE)} + log")


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
