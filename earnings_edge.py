"""
earnings_edge.py — "trade, watch, or avoid this report?"

For every covered name reporting within the next 14 days (from the
uoa_meta_cache the Earnings tile already uses), compute the only earnings
numbers a trader can act on:

  realized_med   median |reaction move| over the last <=8 reports — what the
                 stock ACTUALLY does (close-before -> first close after the
                 report timestamp, BMO/AMC aware via yfinance timestamps)
  implied        the options market's priced move — taken from the flow
                 scanner's expected_move_pct (best-scored row per ticker)
  edge_ratio     implied / realized_med:  >=1.35 rich · <=0.85 cheap
  drift_5d       avg signed move from reaction close to +5 sessions — does
                 the move continue or fade?
  flow_into      count + direction of "Into ERN" tagged flow on the ledger
  verdict        Trade / Watch / Avoid — with the reason spelled out

Data: yfinance (keyless) for report history + daily bars; repo JSON for
implied + flow. Runs in momentum.yml post-close, non-fatal.

    python earnings_edge.py             # generate
    python earnings_edge.py --dry-run   # print, don't write
"""

from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from statistics import mean, median

import yfinance as yf

_BASE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(_BASE, "docs", "reports")
OUT_PATH = os.path.join(REPORTS, "earnings_edge.json")

HORIZON_D   = 14      # names reporting within this many days
MAX_NAMES   = 40      # runtime cap (2 yf calls per name)
MAX_REPORTS = 8       # realized history depth
RICH_RATIO  = 1.35    # implied/realized >= this -> options rich
CHEAP_RATIO = 0.85    # <= this -> options cheap
MIN_REPORTS = 4       # minimum history before we trust realized stats
WORKERS     = 8


def _load(name):
    try:
        with open(os.path.join(REPORTS, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def upcoming_names():
    """(ticker, earnings_date, session) reporting within HORIZON_D days,
    from the same meta cache the desk Earnings tile renders."""
    meta = _load("uoa_meta_cache.json") or {}
    today = datetime.now(timezone.utc).date()
    out = []
    for t, m in meta.items():
        d = (m or {}).get("earnings_date")
        if not d:
            continue
        try:
            dt = datetime.strptime(d, "%Y-%m-%d").date()
        except Exception:
            continue
        days = (dt - today).days
        if 0 <= days <= HORIZON_D:
            out.append({"t": t, "date": d, "days": days,
                        "session": (m or {}).get("earnings_session") or "TBD",
                        "mcap": (m or {}).get("mkt_cap")})
    out.sort(key=lambda x: (x["days"], x["t"]))
    return out[:MAX_NAMES]


def flow_context():
    """Per-ticker option-move candidates + into-earnings flow from the live
    flow file. IMPORTANT: the scanner's expected_move_pct is the 1-sigma
    move over the CONTRACT'S remaining life (iv * sqrt(dte/365)) — only a
    front-expiry contract that just brackets the report is a fair proxy for
    the earnings-priced move, so we keep (em, dte) candidates and let the
    caller pick the bracketing expiry (or honestly report None)."""
    uoa = _load("uoa_latest.json") or {}
    cands, into = {}, {}
    for r in uoa.get("rows") or []:
        t = r.get("ticker")
        if not t:
            continue
        em, dte = r.get("expected_move_pct"), r.get("dte")
        if em is not None and dte is not None:
            cands.setdefault(t, []).append(
                (dte, em, r.get("trade_score") or 0))
        if "Into ERN" in (r.get("tags") or []):
            e = into.setdefault(t, {"n": 0, "bull": 0, "bear": 0})
            e["n"] += 1
            d = (r.get("direction") or "").lower()
            if d == "bullish":
                e["bull"] += 1
            elif d == "bearish":
                e["bear"] += 1
    return cands, into


def implied_for(cands, days_to_report, window=3):
    """The front-expiry read: only a contract expiring within `window` days
    AFTER the report is near-pure event pricing (longer-dated contracts add
    diffusion days + skew and systematically overstate the event move — the
    first cut with window=10 read JPM at ±5.8%, which no earnings straddle
    prices). No true bracketing contract -> None, honestly."""
    if not cands:
        return None
    ok = [(dte, em, sc) for dte, em, sc in cands
          if days_to_report <= dte <= days_to_report + window]
    if not ok:
        return None
    ok.sort(key=lambda x: (x[0], -x[2]))     # closest expiry, best score
    return ok[0][1]


def realized_stats(ticker):
    """(realized_med, realized_avg, n, drift_5d) from the last MAX_REPORTS
    reports. Reaction move = close strictly before the report timestamp ->
    first close after it (handles BMO vs AMC via the timestamp itself)."""
    tk = yf.Ticker(ticker)
    ed = tk.get_earnings_dates(limit=MAX_REPORTS + 6)
    if ed is None or ed.empty:
        return None
    now = datetime.now(timezone.utc)
    past = [ts for ts in ed.index if ts.tz_convert("UTC") < now]
    past = sorted(past, reverse=True)[:MAX_REPORTS]
    if len(past) < MIN_REPORTS:
        return None
    hist = tk.history(period="2y", interval="1d", auto_adjust=True)
    if hist is None or hist.empty:
        return None
    closes = hist["Close"]
    idx = closes.index
    dates = [d.date() for d in idx]
    moves, drifts = [], []
    for ts in past:
        # SESSION-AWARE pairing. Daily bars are stamped at midnight, so a
        # naive "strictly before the report timestamp" comparison includes
        # the report day itself for BMO prints — measuring the day-AFTER
        # drift instead of the reaction (caught in validation: DAL's
        # reactions read ±1% when the true prints are several %).
        #   BMO (hour < 12): reaction = prev session close -> report-day close
        #   AMC (hour >= 12): reaction = report-day close -> next session close
        rd = ts.date()
        bmo = ts.hour < 12
        later = [i for i, d in enumerate(dates) if d >= rd]
        if not later:
            continue
        ri = later[0]                       # report-day (or next session) bar
        if bmo:
            i0, i1 = ri - 1, ri
        else:
            i0, i1 = ri, ri + 1
        if i0 < 0 or i1 >= len(idx):
            continue
        c0, c1 = float(closes.iloc[i0]), float(closes.iloc[i1])
        if c0 <= 0:
            continue
        moves.append(100.0 * (c1 - c0) / c0)
        if i1 + 5 < len(idx):               # drift: reaction close -> +5 sessions
            c5 = float(closes.iloc[i1 + 5])
            drifts.append(100.0 * (c5 - c1) / c1)
    if len(moves) < MIN_REPORTS:
        return None
    absm = [abs(m) for m in moves]
    return {
        "realized_med": round(median(absm), 1),
        "realized_avg": round(mean(absm), 1),
        "n_reports":    len(moves),
        "drift_5d":     round(mean(drifts), 1) if drifts else None,
    }


def verdict_for(implied, stats, flow):
    """Transparent Trade / Watch / Avoid + the reason, no black box."""
    if not stats:
        return "Watch", "not enough report history to price the move"
    med = stats["realized_med"]
    if implied is None:
        return "Watch", (f"typically moves ±{med}% — no liquid option "
                         f"read to compare against")
    ratio = round(implied / med, 2) if med else None
    fl = flow or {}
    fdir = ("bullish" if fl.get("bull", 0) > fl.get("bear", 0) else
            "bearish" if fl.get("bear", 0) > fl.get("bull", 0) else None)
    if ratio is not None and ratio >= RICH_RATIO:
        return "Avoid", (f"options rich: implied ±{implied}% vs ±{med}% "
                         f"typical ({ratio}×) — long premium overpays; "
                         f"spreads only")
    if ratio is not None and ratio <= CHEAP_RATIO:
        why = (f"options cheap: implied ±{implied}% vs ±{med}% typical "
               f"({ratio}×)")
        if fdir:
            return "Trade", why + f" + {fdir} flow into the report"
        return "Trade", why + " — long premium has the edge"
    why = f"implied ±{implied}% ≈ ±{med}% typical — fairly priced"
    if fdir and fl.get("n", 0) >= 2:
        return "Trade", why + f"; {fl['n']} {fdir} into-earnings prints"
    return "Watch", why


def build():
    names = upcoming_names()
    cands_by, into_by = flow_context()
    print(f"  Earnings edge: {len(names)} names reporting ≤{HORIZON_D}d")
    results = []

    def work(n):
        try:
            stats = realized_stats(n["t"])
        except Exception as e:
            print(f"    {n['t']}: history failed ({str(e)[:60]})")
            stats = None
        implied = implied_for(cands_by.get(n["t"]), n["days"])
        flow = into_by.get(n["t"])
        v, why = verdict_for(implied, stats, flow)
        row = {**n, "implied": implied, "flow_into": flow,
               "verdict": v, "why": why}
        if stats:
            row.update(stats)
            if implied is not None and stats["realized_med"]:
                row["edge_ratio"] = round(implied / stats["realized_med"], 2)
        return row

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for row in ex.map(work, names):
            results.append(row)
    results.sort(key=lambda r: (r["days"], r["t"]))
    counts = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print("  verdicts: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "horizon_days": HORIZON_D,
        "count": len(results),
        "names": results,
        "params": {"rich_ratio": RICH_RATIO, "cheap_ratio": CHEAP_RATIO,
                   "max_reports": MAX_REPORTS, "min_reports": MIN_REPORTS},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    payload = build()
    if args.dry_run:
        print(json.dumps(payload, indent=1)[:4000])
        return
    os.makedirs(REPORTS, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"  Wrote {os.path.relpath(OUT_PATH, _BASE)}")


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
