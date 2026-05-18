"""
uoa_alpha.py — Provable-alpha layer for the UOA dashboard.

Reads the signal ledger (uoa_signals.jsonl) that uoa_scanner appends to and
produces two outputs:

  uoa_edge.json          — aggregate realized edge: hit rate, avg/median
                           forward return vs SPY, by signal type / score
                           bucket / DTE bucket / flag, plus MFE-MAE and the
                           next-day OI-confirmation rate.
  uoa_signals_scored.json — per-signal scorecard: each ledger signal with its
                           forward returns, max favourable / adverse
                           excursion, and OI-confirmation status. Drives the
                           dashboard's Tracked-Signals view.

Underlying return is used (not the option's) — the testable thesis is
"this flow predicts the stock moves". Honest by design: it MEASURES whether
the flow works, including when it doesn't. The record builds live from
go-live; +5d stats become meaningful within ~2 weeks.
"""

import os
import json
from datetime import datetime, timezone
from statistics import mean, median

import polygon_data as pg

_BASE = os.path.dirname(os.path.abspath(__file__))
LEDGER_PATH = os.path.join(_BASE, "docs", "reports", "uoa_signals.jsonl")
EDGE_PATH   = os.path.join(_BASE, "docs", "reports", "uoa_edge.json")
SCORED_PATH = os.path.join(_BASE, "docs", "reports", "uoa_signals_scored.json")

HORIZONS = [1, 3, 5, 10, 20]
SCORE_BUCKETS = [("80-100", 80, 101), ("65-79", 65, 80), ("55-64", 55, 65)]
SIGNAL_TYPES = ("golden_sweep", "sweep", "voloi")
DTE_BUCKETS = ("urgent", "swing", "positioning", "leaps")
ATTRIB_TAGS = ("Golden Sweep", "Sweep", "Block", "Size>OI", "Repeat",
               "Into ERN", "In Universe")


def _dte_bucket(dte):
    if dte is None:  return "unknown"
    if dte <= 14:    return "urgent"
    if dte <= 90:    return "swing"
    if dte <= 365:   return "positioning"
    return "leaps"


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


def _bars(ticker, days=160):
    """{date_str: {c,h,l}} for a ticker from Polygon daily bars."""
    out = {}
    for b in pg.daily_bars(ticker, days=days):
        ts = b.get("t")
        if ts:
            d = datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%d")
            out[d] = {"c": b.get("c"), "h": b.get("h"), "l": b.get("l")}
    return out


def _base_index(signal, dates):
    """The flag's trading-day index in a sorted date list (or None)."""
    flagged = signal["flagged_at"][:10]
    if not dates:
        return None, None
    base = flagged if flagged in dates else next((d for d in dates if d >= flagged), None)
    if not base:
        return None, None
    return base, dates.index(base)


def forward_returns(signal, bars, spy_closes):
    """Underlying forward return at each horizon + excess vs SPY."""
    dates = sorted(bars)
    base, idx = _base_index(signal, dates)
    if base is None:
        return {}
    p0 = signal.get("underlying_px_at_flag") or bars[base]["c"]
    if not p0:
        return {}
    spy_dates = sorted(spy_closes)
    spy0 = spy_closes.get(base)
    spy_idx = spy_dates.index(base) if base in spy_closes else None
    out = {}
    for h in HORIZONS:
        if idx + h >= len(dates):
            continue
        ret = (bars[dates[idx + h]]["c"] / p0 - 1) * 100
        excess = None
        if spy0 and spy_idx is not None and spy_idx + h < len(spy_dates):
            spy_ret = (spy_closes[spy_dates[spy_idx + h]] / spy0 - 1) * 100
            excess = ret - spy_ret
        out[h] = {"ret": round(ret, 2),
                  "excess": round(excess, 2) if excess is not None else None}
    return out


def excursions(signal, bars):
    """Max favourable / adverse excursion of the underlying over the +20d
    window, from daily highs/lows — how far the trade could have run, and
    how much heat it took, before settling."""
    dates = sorted(bars)
    base, idx = _base_index(signal, dates)
    if base is None:
        return {"mfe": None, "mae": None}
    p0 = signal.get("underlying_px_at_flag") or bars[base]["c"]
    if not p0:
        return {"mfe": None, "mae": None}
    window = dates[idx + 1: idx + 1 + 20]
    highs = [bars[d]["h"] for d in window if bars[d].get("h")]
    lows  = [bars[d]["l"] for d in window if bars[d].get("l")]
    return {
        "mfe": round((max(highs) / p0 - 1) * 100, 1) if highs else None,
        "mae": round((min(lows)  / p0 - 1) * 100, 1) if lows  else None,
    }


def oi_status(signal, oi_map):
    """Per-signal next-day OI status. A signal flagged on day D recorded the
    contract's flag-day OI + volume; once a day has passed we compare current
    OI. OI rising by a large share of the flag-day volume = the flow opened
    NEW positions that STUCK.  confirmed / weak / closed / pending."""
    if signal.get("open_interest") is None or signal.get("volume") is None:
        return {"status": "pending", "oi_change": None, "retained_pct": None}
    try:
        fd = datetime.strptime(signal["flagged_at"][:10], "%Y-%m-%d").date()
    except Exception:
        return {"status": "pending", "oi_change": None, "retained_pct": None}
    if (datetime.now(timezone.utc).date() - fd).days < 1:
        return {"status": "pending", "oi_change": None, "retained_pct": None}
    cur = oi_map.get(signal["contract"])
    if cur is None:
        return {"status": "pending", "oi_change": None, "retained_pct": None}
    vol = signal["volume"] or 1
    change = cur - signal["open_interest"]
    retained = round(100 * change / vol)
    if change > 0.50 * vol:   status = "confirmed"
    elif change > 0.15 * vol: status = "weak"
    else:                     status = "closed"
    return {"status": status, "oi_change": change, "retained_pct": retained}


def _oi_now(ticker):
    """{contract_ticker: open_interest} from the current option chain."""
    out = {}
    for c in pg.option_chain(ticker):
        ct = (c.get("details", {}) or {}).get("ticker")
        if ct:
            out[ct] = c.get("open_interest", 0) or 0
    return out


# ─── Aggregation ──────────────────────────────────────────────────────────────

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


def _excursion_avg(scored):
    mfe = [s["excursion"]["mfe"] for s in scored
           if s.get("excursion") and s["excursion"].get("mfe") is not None]
    mae = [s["excursion"]["mae"] for s in scored
           if s.get("excursion") and s["excursion"].get("mae") is not None]
    return {
        "avg_mfe": round(mean(mfe), 1) if mfe else None,
        "avg_mae": round(mean(mae), 1) if mae else None,
        "n":       len(mfe),
    }


def compute_edge():
    """Score the whole ledger; build aggregates + per-signal scorecards."""
    ledger = load_ledger()
    print(f"  Ledger: {len(ledger)} signals")
    if not ledger:
        return _empty_edge(), []

    spy = _bars("SPY")
    spy_closes = {d: b["c"] for d, b in spy.items()}

    bar_cache, scored = {}, []
    for s in ledger:
        tk = s.get("ticker", "")
        if tk and tk not in bar_cache:
            bar_cache[tk] = _bars(tk)
        s2 = dict(s)
        s2["returns"]   = forward_returns(s, bar_cache.get(tk, {}), spy_closes)
        s2["excursion"] = excursions(s, bar_cache.get(tk, {}))
        scored.append(s2)

    # next-day OI status — fetch current OI only for tickers with a signal
    # old enough to be confirmable
    today = datetime.now(timezone.utc).date()
    def _confirmable(s):
        if s.get("open_interest") is None or s.get("volume") is None:
            return False
        try:
            fd = datetime.strptime(s["flagged_at"][:10], "%Y-%m-%d").date()
        except Exception:
            return False
        return (today - fd).days >= 1
    oi_cache = {}
    for s in scored:
        if _confirmable(s):
            tk = s["ticker"]
            if tk not in oi_cache:
                oi_cache[tk] = _oi_now(tk)
            s["oi"] = oi_status(s, oi_cache[tk])
        else:
            s["oi"] = {"status": "pending", "oi_change": None, "retained_pct": None}

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

    by_dte = {}
    for b in DTE_BUCKETS:
        items = [s for s in scored if _dte_bucket(s.get("dte")) == b]
        if items:
            by_dte[b] = {"signals": len(items), "h": _group(items)}

    by_tag = {}
    for tag in ATTRIB_TAGS:
        items = [s for s in scored if tag in (s.get("tags") or [])]
        if items:
            by_tag[tag] = {"signals": len(items), "h5": _agg([x["returns"] for x in items], 5)}

    oc = [s["oi"]["status"] for s in scored if s["oi"]["status"] != "pending"]
    edge = {
        "generated":     datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_signals": len(scored),
        "matured_5d":    matured_5d,
        "horizons":      HORIZONS,
        "overall":       _group(scored),
        "excursion":     _excursion_avg(scored),
        "by_type":       by_type,
        "by_score":      by_score,
        "by_dte":        by_dte,
        "by_tag":        by_tag,
        "oi_confirmation": {
            "checked":      len(oc),
            "confirmed":    sum(1 for x in oc if x == "confirmed"),
            "weak":         sum(1 for x in oc if x == "weak"),
            "closed":       sum(1 for x in oc if x == "closed"),
            "confirm_rate": round(100 * sum(1 for x in oc if x == "confirmed") / len(oc))
                            if oc else None,
        },
    }
    return edge, scored


def _empty_edge():
    return {
        "generated":     datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_signals": 0,
        "matured_5d":    0,
        "horizons":      HORIZONS,
        "overall":       {str(h): _agg([], h) for h in HORIZONS},
        "excursion":     {"avg_mfe": None, "avg_mae": None, "n": 0},
        "by_type":       {},
        "by_score":      {},
        "by_dte":        {},
        "by_tag":        {},
        "oi_confirmation": {"checked": 0, "confirmed": 0, "weak": 0,
                            "closed": 0, "confirm_rate": None},
    }


def _emit_scored(scored):
    """Per-signal scorecard JSON for the dashboard's Tracked-Signals view."""
    rows = []
    for s in scored:
        ret = s.get("returns") or {}
        r1, r3, r5 = ret.get(1) or {}, ret.get(3) or {}, ret.get(5) or {}
        exc = s.get("excursion") or {}
        oi  = s.get("oi") or {}
        rows.append({
            "id":           s.get("id"),
            "flagged_at":   s.get("flagged_at"),
            "ticker":       s.get("ticker"),
            "contract":     s.get("contract"),
            "signal_type":  s.get("signal_type"),
            "trade_score":  s.get("trade_score"),
            "premium":      s.get("premium"),
            "dte":          s.get("dte"),
            "tags":         s.get("tags", []),
            "ret_1d":       r1.get("ret"),
            "ret_3d":       r3.get("ret"),
            "ret_5d":       r5.get("ret"),
            "excess_5d":    r5.get("excess"),
            "mfe":          exc.get("mfe"),
            "mae":          exc.get("mae"),
            "oi_status":    oi.get("status", "pending"),
            "oi_change":    oi.get("oi_change"),
            "retained_pct": oi.get("retained_pct"),
        })
    rows.sort(key=lambda r: (r.get("flagged_at") or ""), reverse=True)
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count":     len(rows),
        "signals":   rows,
    }
    with open(SCORED_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)


def run():
    """Compute edge stats + per-signal scorecards; publish both JSON files."""
    edge, scored = compute_edge()
    os.makedirs(os.path.dirname(EDGE_PATH), exist_ok=True)
    with open(EDGE_PATH, "w", encoding="utf-8") as f:
        json.dump(edge, f, indent=1)
    _emit_scored(scored)
    o5 = edge["overall"].get("5", {})
    oc = edge["oi_confirmation"]
    print(f"  Wrote uoa_edge.json + uoa_signals_scored.json — "
          f"{edge['total_signals']} signals, {edge['matured_5d']} matured +5d "
          f"(+5d hit {o5.get('hit_rate')}%, avg {o5.get('avg')}%; "
          f"OI confirmed {oc.get('confirmed')}/{oc.get('checked')})")
    return edge


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    run()
