"""
technical_facts.py — the nightly per-ticker indicator fact table.

One computation, many consumers: grade decomposition ("why is this an A-?"),
the Daily Brief technical snapshot, relative-strength panels, upgrade/
downgrade reasons, and ATR-derived trade levels all read from this file
instead of re-deriving (or worse, hand-waving) technicals.

Universe: only the names the product actually surfaces tonight — top swing
grades (bull + bear ladders), grade movers, evening-review picks, and
conviction-board tickers. Capped so the yfinance batch stays fast.

Per ticker:
  close          last daily close
  ema20/50/200   "above" | "below" + signed % distance
  rsi14          Wilder RSI
  atr_pct        Wilder ATR(14) as % of close — the risk unit for levels
  vol_ratio      last volume / 20d avg volume
  rs             return minus SPY return over 1/5/20/60 sessions (pp)
  rs_rank        percentile of 60d RS within THIS universe (0-100; labeled
                 universe-relative, not an IBD-style all-market rank)
  trend          strong-up / up / mixed / down from the EMA stack

    python technical_facts.py             # generate
    python technical_facts.py --dry-run   # print, don't write
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone

import yfinance as yf

_BASE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(_BASE, "docs", "reports")
OUT_PATH = os.path.join(REPORTS, "technical_facts.json")

MAX_NAMES   = 60
HIST_PERIOD = "1y"
TOP_PER_GRADE = 8          # bull ladder depth pulled per grade
BEAR_GRADES = ("F", "F+", "F-", "G", "G+")
BULL_GRADES = ("A+", "A", "A-")


def _load(name):
    try:
        with open(os.path.join(REPORTS, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def build_universe() -> list[str]:
    """The names tonight's surfaces actually show, most-important first —
    if the cap bites, it drops the least-surfaced names, not the picks."""
    ordered: list[str] = []

    def add(tk):
        if tk and tk not in ordered:
            ordered.append(tk)

    ev = _load("evening_review.json") or {}
    for p in ev.get("tomorrow") or []:
        add(p.get("t"))

    cf = _load("carryover_flow.json") or {}
    for c in cf.get("contracts") or []:
        add(c.get("ticker"))

    sw = _load("swing_latest_summary.json") or _load("swing_report.json")
    if sw and sw.get("runs"):
        run = sw["runs"][-1]
        grades = run.get("grades") or {}
        for g in BULL_GRADES + BEAR_GRADES:
            for c in (grades.get(g) or [])[:TOP_PER_GRADE]:
                add(c.get("t"))
        # grade movers vs prior run (top 5 each way by ladder distance)
        if len(sw["runs"]) >= 2:
            ladder = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-",
                      "D+", "D", "D-", "E+", "E", "E-", "F+", "F", "F-",
                      "G+", "G"]
            def gmap(r):
                m = {}
                for gi, g in enumerate(ladder):
                    for c in (r.get("grades") or {}).get(g) or []:
                        if c.get("t"):
                            m[c["t"]] = gi
                return m
            now, was = gmap(sw["runs"][-1]), gmap(sw["runs"][-2])
            moves = [(t, was[t] - now[t]) for t in now
                     if t in was and now[t] != was[t]]
            moves.sort(key=lambda x: -abs(x[1]))
            ups = [t for t, d in moves if d > 0][:5]
            dns = [t for t, d in moves if d < 0][:5]
            for t in ups + dns:
                add(t)

    return ordered[:MAX_NAMES]


# ── indicator math (plain lists — no pandas dependency beyond yfinance) ──

def _ema(vals, span):
    if len(vals) < span:
        return None
    k = 2.0 / (span + 1)
    e = sum(vals[:span]) / span
    for v in vals[span:]:
        e = v * k + e * (1 - k)
    return e


def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def _atr_pct(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    c = closes[-1]
    return 100.0 * atr / c if c else None


def _ret(closes, n):
    if len(closes) <= n or not closes[-1 - n]:
        return None
    return 100.0 * (closes[-1] / closes[-1 - n] - 1.0)


def facts_for(df, spy_rets) -> dict | None:
    closes = [float(v) for v in df["Close"].dropna().tolist()]
    highs = [float(v) for v in df["High"].dropna().tolist()]
    lows = [float(v) for v in df["Low"].dropna().tolist()]
    vols = [float(v) for v in df["Volume"].dropna().tolist()]
    if len(closes) < 60:
        return None
    c = closes[-1]
    out = {"close": round(c, 2)}
    for span in (20, 50, 200):
        e = _ema(closes, span)
        if e:
            out[f"ema{span}"] = "above" if c >= e else "below"
            out[f"ema{span}_dist"] = round(100.0 * (c / e - 1.0), 1)
    r = _rsi(closes)
    out["rsi14"] = round(r, 0) if r is not None else None
    a = _atr_pct(highs, lows, closes)
    out["atr_pct"] = round(a, 2) if a is not None else None
    if len(vols) >= 21 and sum(vols[-21:-1]) > 0:
        out["vol_ratio"] = round(vols[-1] / (sum(vols[-21:-1]) / 20.0), 1)
    rs = {}
    for label, n in (("d1", 1), ("d5", 5), ("d20", 20), ("d60", 60)):
        tr, sr = _ret(closes, n), spy_rets.get(label)
        if tr is not None and sr is not None:
            rs[label] = round(tr - sr, 1)
    out["rs"] = rs
    above = sum(1 for s in (20, 50, 200) if out.get(f"ema{s}") == "above")
    out["trend"] = ("strong-up" if above == 3 else
                    "up" if above == 2 else
                    "down" if above == 0 else "mixed")
    return out


def build():
    universe = build_universe()
    if not universe:
        print("  technical facts: empty universe — nothing to compute")
        return None
    tickers = universe + ["SPY"]
    print(f"  technical facts: {len(universe)} names + SPY, {HIST_PERIOD} daily")
    data = yf.download(tickers, period=HIST_PERIOD, interval="1d",
                       auto_adjust=True, group_by="ticker",
                       progress=False, threads=True)

    def frame(tk):
        try:
            df = data[tk].dropna(how="all")
            return df if not df.empty else None
        except Exception:
            return None

    spy = frame("SPY")
    if spy is None:
        print("  SPY history missing — aborting (RS needs the anchor)")
        return None
    spy_closes = [float(v) for v in spy["Close"].dropna().tolist()]
    spy_rets = {lab: _ret(spy_closes, n)
                for lab, n in (("d1", 1), ("d5", 5), ("d20", 20), ("d60", 60))}

    facts, skipped = {}, 0
    for tk in universe:
        df = frame(tk)
        f = facts_for(df, spy_rets) if df is not None else None
        if f:
            facts[tk] = f
        else:
            skipped += 1

    # universe-relative RS rank on 60d relative return
    ranked = sorted([t for t in facts if facts[t]["rs"].get("d60") is not None],
                    key=lambda t: facts[t]["rs"]["d60"])
    for i, t in enumerate(ranked):
        facts[t]["rs_rank"] = round(100.0 * (i + 1) / len(ranked)) if len(ranked) > 1 else 50

    print(f"  computed {len(facts)} names ({skipped} skipped — thin history)")
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "universe_note": ("rs_rank is percentile WITHIN tonight's surfaced "
                          "universe, not an all-market rank"),
        "count": len(facts),
        "facts": facts,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    payload = build()
    if payload is None:
        return
    if args.dry_run:
        print(json.dumps(payload, indent=1)[:3000])
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
