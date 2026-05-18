"""
uoa_alpha.py — Provable-alpha layer for the UOA dashboard.

Reads the signal ledger (uoa_signals.jsonl) that uoa_scanner appends to,
computes each signal's UNDERLYING forward return at +1/+3/+5/+10/+20
trading days, benchmarks it against SPY over the same window, and
aggregates the realized edge — hit rate, average / median return —
sliced by signal type and Trade Score bucket.

This is what makes the dashboard 'provable': it does not claim the
signals work — it MEASURES it, honestly, including when they don't.
Underlying return is used (not the option's) because the testable
thesis is 'this flow predicts the stock moves' — option P&L is noisy
and leverage-distorted.

The track record builds live from go-live: +5d stats become meaningful
within ~2 weeks of signals accumulating.
"""

import os
import json
from datetime import datetime, timezone
from statistics import mean, median

import polygon_data as pg

_BASE = os.path.dirname(os.path.abspath(__file__))
LEDGER_PATH = os.path.join(_BASE, "docs", "reports", "uoa_signals.jsonl")
EDGE_PATH   = os.path.join(_BASE, "docs", "reports", "uoa_edge.json")

HORIZONS = [1, 3, 5, 10, 20]            # trading-day forward windows
SCORE_BUCKETS = [("80-100", 80, 101), ("65-79", 65, 80), ("55-64", 55, 65)]
SIGNAL_TYPES = ("golden_sweep", "sweep", "voloi")


def load_ledger():
    """Read the append-only signal ledger (one JSON object per line)."""
    out = []
    if not os.path.exists(LEDGER_PATH):
        return out
    with open(LEDGER_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def _closes(ticker, days=160):
    """{date_str: close} for a ticker from Polygon daily bars."""
    out = {}
    for b in pg.daily_bars(ticker, days=days):
        ts = b.get("t")
        if ts:
            d = datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%d")
            out[d] = b.get("c")
    return out


def forward_returns(signal, closes, spy_closes):
    """Underlying forward return at each horizon + excess vs SPY.
    Returns {horizon: {ret, excess}} ; a horizon that hasn't elapsed is omitted."""
    flagged = signal["flagged_at"][:10]
    dates = sorted(closes)
    if not dates:
        return {}
    base = flagged if flagged in closes else next((d for d in dates if d >= flagged), None)
    if not base:
        return {}
    p0 = signal.get("underlying_px_at_flag") or closes.get(base)
    if not p0:
        return {}
    idx = dates.index(base)

    spy_dates = sorted(spy_closes)
    spy0 = spy_closes.get(base)
    spy_idx = spy_dates.index(base) if base in spy_closes else None

    out = {}
    for h in HORIZONS:
        if idx + h >= len(dates):
            continue                          # not matured yet
        ret = (closes[dates[idx + h]] / p0 - 1) * 100
        excess = None
        if spy0 and spy_idx is not None and spy_idx + h < len(spy_dates):
            spy_ret = (spy_closes[spy_dates[spy_idx + h]] / spy0 - 1) * 100
            excess = ret - spy_ret
        out[h] = {"ret": round(ret, 2),
                  "excess": round(excess, 2) if excess is not None else None}
    return out


def _agg(returns_list, horizon):
    """Aggregate forward-return dicts at one horizon into an edge stat."""
    vals = [r[horizon]["ret"] for r in returns_list
            if r.get(horizon) and r[horizon]["ret"] is not None]
    exc  = [r[horizon]["excess"] for r in returns_list
            if r.get(horizon) and r[horizon].get("excess") is not None]
    if not vals:
        return {"n": 0, "hit_rate": None, "avg": None, "median": None, "avg_excess": None}
    return {
        "n":          len(vals),
        "hit_rate":   round(100 * sum(1 for v in vals if v > 0) / len(vals)),
        "avg":        round(mean(vals), 2),
        "median":     round(median(vals), 2),
        "avg_excess": round(mean(exc), 2) if exc else None,
    }


def _group(scored):
    """Per-horizon edge stats for a list of scored signals."""
    rets = [s["returns"] for s in scored]
    return {str(h): _agg(rets, h) for h in HORIZONS}


def _oi_now(ticker):
    """{contract_ticker: open_interest} from the current option chain."""
    out = {}
    for c in pg.option_chain(ticker):
        ct = (c.get("details", {}) or {}).get("ticker")
        if ct:
            out[ct] = c.get("open_interest", 0) or 0
    return out


def oi_confirmation(ledger):
    """Next-day OI-retention check — the second provable-alpha axis.

    A signal flagged on day D recorded the contract's flag-day OI and volume.
    Once a day has passed, pull the contract's current OI: if it rose by more
    than half the flag-day volume, the flow opened NEW positions that STUCK
    (not day-traded out) — the position is real. Returns a confirm rate."""
    today = datetime.now(timezone.utc).date()
    pending = []
    for s in ledger:
        if s.get("open_interest") is None or s.get("volume") is None:
            continue                      # pre-v2 signal — no baseline
        try:
            fd = datetime.strptime(s["flagged_at"][:10], "%Y-%m-%d").date()
        except Exception:
            continue
        if (today - fd).days >= 1:
            pending.append(s)
    if not pending:
        return {"checked": 0, "confirmed": 0, "confirm_rate": None}

    oi_cache, confirmed, checked = {}, 0, 0
    for s in pending:
        tk = s["ticker"]
        if tk not in oi_cache:
            oi_cache[tk] = _oi_now(tk)
        cur = oi_cache[tk].get(s["contract"])
        if cur is None:
            continue                      # contract expired / not found
        checked += 1
        if cur - s["open_interest"] > 0.5 * s["volume"]:
            confirmed += 1
    return {
        "checked":      checked,
        "confirmed":    confirmed,
        "confirm_rate": round(100 * confirmed / checked) if checked else None,
    }


def compute_edge():
    """Score the whole ledger and aggregate the realized edge."""
    ledger = load_ledger()
    print(f"  Ledger: {len(ledger)} signals")
    if not ledger:
        return _empty_edge()

    spy = _closes("SPY")
    cache, scored = {}, []
    for s in ledger:
        tk = s.get("ticker", "")
        if tk and tk not in cache:
            cache[tk] = _closes(tk)
        s2 = dict(s)
        s2["returns"] = forward_returns(s, cache.get(tk, {}), spy)
        scored.append(s2)

    matured_5d = sum(1 for s in scored if s["returns"].get(5))

    by_type = {}
    for typ in SIGNAL_TYPES:
        items = [s for s in scored if s.get("signal_type") == typ]
        if items:
            by_type[typ] = {"signals": len(items), "h": _group(items)}

    by_score = {}
    for label, lo, hi in SCORE_BUCKETS:
        items = [s for s in scored if lo <= s.get("trade_score", 0) < hi]
        if items:
            by_score[label] = {"signals": len(items), "h": _group(items)}

    edge = {
        "generated":     datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_signals": len(scored),
        "matured_5d":    matured_5d,
        "horizons":      HORIZONS,
        "overall":       _group(scored),
        "by_type":       by_type,
        "by_score":      by_score,
        "oi_confirmation": oi_confirmation(ledger),
    }
    return edge


def _empty_edge():
    return {
        "generated":     datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_signals": 0,
        "matured_5d":    0,
        "horizons":      HORIZONS,
        "overall":       {str(h): _agg([], h) for h in HORIZONS},
        "by_type":       {},
        "by_score":      {},
        "oi_confirmation": {"checked": 0, "confirmed": 0, "confirm_rate": None},
    }


def run():
    """Compute the edge stats and publish uoa_edge.json for the dashboard."""
    edge = compute_edge()
    os.makedirs(os.path.dirname(EDGE_PATH), exist_ok=True)
    with open(EDGE_PATH, "w", encoding="utf-8") as f:
        json.dump(edge, f, indent=1)
    o5 = edge["overall"].get("5", {})
    print(f"  Wrote uoa_edge.json — {edge['total_signals']} signals, "
          f"{edge['matured_5d']} matured to +5d  "
          f"(overall +5d: hit {o5.get('hit_rate')}%, avg {o5.get('avg')}%)")
    return edge


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    run()
