"""
trade_desk_context.py — point-in-time context logger for setup research.

The 2026-08 setup-level research (trade_desk_research.py) found no
persistent extreme-tail edge in the ledger's OWN columns — and the
conditional hypotheses that matter (flow x relative strength x earnings
state x volatility x sector) are untestable because the ledger never
captured those columns. This module starts capturing them, point-in-
time, so the interactions become testable once the record accrues.

Once per session (idempotent per ticker-date), for every ticker with
directional flow today plus every name reporting earnings within 7
days, append a context row assembled from the JSONs already published
by earlier pipeline steps — nothing is fetched, nothing is estimated,
absent values are null:

  data/setup_context.jsonl   (append-only; joined by (ticker, date)
                              in future research runs)

Row: date, ticker, rs_rank / rs5 / rs20 / trend / rsi / vol_ratio /
atr_pct / ema20 / ema50 (technical_facts — surfaced universe only),
swing_grade (swing summary), sector / mcap / days_to_earnings /
earnings_session (uoa_meta_cache), implied_move / realized_med
(earnings_edge), market_regime (regime_history).

    python trade_desk_context.py            # append today's rows
    python trade_desk_context.py --dry-run  # print, don't write
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone

from trade_desk_validation import _regime_map, _load

_BASE = os.path.dirname(os.path.abspath(__file__))
R = lambda *p: os.path.join(_BASE, *p)
OUT_PATH = R("data", "setup_context.jsonl")


def _swing_grades():
    s = _load(R("docs", "reports", "swing_latest_summary.json"), {})
    runs = s.get("runs") or []
    out = {}
    if runs:
        for grade, rows in (runs[0].get("grades") or {}).items():
            for r in rows or []:
                t = r.get("t")
                if t and t not in out:
                    out[t] = grade
    return out


def _days_to_earnings(meta_row, today):
    ed = (meta_row or {}).get("earnings_date")
    try:
        d = datetime.strptime(ed, "%Y-%m-%d").date()
        return (d - today).days
    except (TypeError, ValueError):
        return None


def build_rows(now=None):
    now = now or datetime.now(timezone.utc)
    today = now.date()
    today_s = today.isoformat()

    scored = _load(R("docs", "reports", "uoa_signals_scored.json"), {})
    flow_tickers = {s.get("ticker") for s in scored.get("signals") or []
                    if (s.get("flagged_at") or "")[:10] == today_s
                    and s.get("direction") in ("bullish", "bearish")}
    edge = _load(R("docs", "reports", "earnings_edge.json"), {})
    ern_rows = {r.get("t"): r for r in edge.get("names") or []
                if r.get("t") and (r.get("days") or 99) <= 7}
    tickers = sorted((flow_tickers | set(ern_rows)) - {None})
    if not tickers:
        return []

    facts = (_load(R("docs", "reports", "technical_facts.json"), {})
             .get("facts") or {})
    meta = _load(R("docs", "reports", "uoa_meta_cache.json"), {})
    grades = _swing_grades()
    regimes = _regime_map()
    regime = regimes.get(max(regimes)) if regimes else None

    rows = []
    for tk in tickers:
        f = facts.get(tk) or {}
        rs = f.get("rs") or {}
        m = meta.get(tk) or {}
        e = ern_rows.get(tk) or {}
        rows.append({
            "date": today_s, "ticker": tk,
            "logged_at": now.isoformat(timespec="seconds"),
            "rs_rank": f.get("rs_rank"),
            "rs5": rs.get("d5"), "rs20": rs.get("d20"),
            "trend": f.get("trend"), "rsi": f.get("rsi14"),
            "vol_ratio": f.get("vol_ratio"), "atr_pct": f.get("atr_pct"),
            "ema20": f.get("ema20"), "ema50": f.get("ema50"),
            "swing_grade": grades.get(tk),
            "sector": m.get("sector"), "mcap": m.get("mkt_cap"),
            "days_to_earnings": _days_to_earnings(m, today),
            "earnings_session": m.get("earnings_session"),
            "implied_move": e.get("implied"),
            "realized_med": e.get("realized_med"),
            "market_regime": regime,
        })
    return rows


def run(dry=False):
    rows = build_rows()
    # idempotent per (ticker, date)
    seen = set()
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    seen.add((d.get("ticker"), d.get("date")))
                except Exception:
                    pass
    new = [r for r in rows if (r["ticker"], r["date"]) not in seen]
    if not dry and new:
        os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
        with open(OUT_PATH, "a", encoding="utf-8") as f:
            for r in new:
                f.write(json.dumps(r, separators=(",", ":"),
                                   ensure_ascii=False) + "\n")
    filled = sum(1 for r in new if r.get("rs_rank") is not None)
    print(f"context: {len(rows)} tickers today, {len(new)} appended "
          f"({filled} with technicals), {len(seen)} already logged")
    return new


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    run(dry=ap.parse_args().dry_run)
