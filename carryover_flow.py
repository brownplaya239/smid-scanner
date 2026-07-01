"""
carryover_flow.py — "Noteworthy flow from yesterday (OI confirmed)"

Every notable UOA print is appended to the signals ledger
(data/uoa_signals.jsonl) with its flag-day contract VOLUME and OPEN
INTEREST. Options clear overnight through the OCC, so the next morning we
can re-read each contract's open interest and prove whether the whale
actually HELD the position or bailed intraday:

    delta     = today_OI - flag_OI          # net new open interest
    retention = delta / flag_volume         # ≥ 0.50 → held more than half ✅

This is the @flowgod / @salmaogs "OI confirmed" check. Runs pre-open (the
Daily Brief workflow) so the fresh, OCC-settled OI is available. Writes
docs/reports/carryover_flow.json for the pre-open Daily Brief email and
the Today's Desk "Overnight Flow" tile.

Requires POLYGON_API_KEY. Safe no-op (writes an empty payload) when the
ledger or key is missing.

    python carryover_flow.py            # generate for the last session
    python carryover_flow.py --dry-run  # print, don't write
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import polygon_data as pg

ET = ZoneInfo("America/New_York")
_BASE = os.path.dirname(os.path.abspath(__file__))
LEDGER_PATH = os.path.join(_BASE, "data", "uoa_signals.jsonl")
OUT_PATH = os.path.join(_BASE, "docs", "reports", "carryover_flow.json")

TOP_N = 8               # how many prints to carry into the morning
MIN_PREMIUM = 250_000   # ignore small prints — carryover is a whale story
HELD_THRESHOLD = 0.50   # retention ≥ this → "held more than half" ✅
TAIL_LINES = 12_000     # ledger lines to scan (covers several sessions)

_OCC = re.compile(r"^O:([A-Z.]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


def _parse_occ(contract: str):
    """OCC ticker → (expiry_date 'YYYY-MM-DD', 'call'|'put', strike float)."""
    m = _OCC.match(contract or "")
    if not m:
        return None, None, None
    _, yy, mm, dd, cp, strike = m.groups()
    exp = f"20{yy}-{mm}-{dd}"
    typ = "call" if cp == "C" else "put"
    return exp, typ, int(strike) / 1000.0


def _et_date(iso_utc: str) -> str:
    """UTC ISO timestamp → 'YYYY-MM-DD' calendar date in US/Eastern."""
    try:
        dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ET).date().isoformat()
    except Exception:
        return ""


def _tail(path: str, n: int) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return f.readlines()[-n:]


def _load_session_prints(target_date: str | None):
    """Return (session_date, [ledger rows]) for the flow session to carry.

    session_date = the requested date, else the most recent ledger date
    STRICTLY BEFORE today (ET) — i.e. "yesterday's" session, weekend/holiday
    aware (Friday's flow surfaces on Monday)."""
    if not os.path.exists(LEDGER_PATH):
        return None, []
    today = datetime.now(ET).date().isoformat()
    by_date: dict[str, list] = {}
    for line in _tail(LEDGER_PATH, TAIL_LINES):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        d = _et_date(r.get("flagged_at", ""))
        if not d:
            continue
        by_date.setdefault(d, []).append(r)
    if not by_date:
        return None, []
    if target_date and target_date in by_date:
        return target_date, by_date[target_date]
    prior = sorted(d for d in by_date if d < today)
    if not prior:
        return None, []
    sd = prior[-1]
    return sd, by_date[sd]


def _top_prints(rows: list) -> list:
    """Dedup ledger rows to one-per-contract (keep the biggest premium),
    filter to whale-size, sort by premium desc, take TOP_N."""
    best: dict[str, dict] = {}
    for r in rows:
        c = r.get("contract")
        if not c or (r.get("premium") or 0) < MIN_PREMIUM:
            continue
        if c not in best or (r.get("premium") or 0) > (best[c].get("premium") or 0):
            best[c] = r
    ranked = sorted(best.values(), key=lambda r: r.get("premium") or 0, reverse=True)
    return ranked[:TOP_N]


def _confirm(row: dict) -> dict | None:
    """Fetch the contract's current (OCC-settled) OI and compute retention."""
    contract = row.get("contract")
    ticker = row.get("ticker")
    exp, typ, strike = _parse_occ(contract)
    flag_oi = row.get("open_interest") or 0
    volume = row.get("volume") or 0
    premium = row.get("premium") or 0
    avg_px = round(premium / (volume * 100), 2) if volume else None

    snap = pg.option_contract(ticker, contract)
    today_oi = None
    if snap:
        # Explicit None checks — a real open_interest of 0 (position fully
        # closed) must stay 0, not fall through to the details fallback via
        # `or` and become None ("pending"). 0 → ❌ closed; None → pending.
        today_oi = snap.get("open_interest")
        if today_oi is None:
            today_oi = (snap.get("details") or {}).get("open_interest")
    if today_oi is None:
        # No fresh OI (contract expired / not returned) — carry the print
        # but mark the confirmation as pending rather than dropping it.
        delta = None
        retention = None
        held = None
    else:
        delta = int(today_oi) - int(flag_oi)
        retention = round(delta / volume, 3) if volume else None
        held = (retention is not None and retention >= HELD_THRESHOLD)

    return {
        "ticker":     ticker,
        "contract":   contract,
        "type":       typ or row.get("type"),
        "strike":     strike,
        "expiry":     exp,
        "premium":    premium,
        "avg_px":     avg_px,
        "volume":     volume,
        "flag_oi":    flag_oi,
        "today_oi":   today_oi,
        "delta":      delta,
        "retention":  retention,
        "held":       held,
        "side":       row.get("flow_side", "unknown"),
        "score":      row.get("trade_score"),
        "spot_at_flag": row.get("underlying_px_at_flag"),
    }


def build(target_date: str | None = None) -> dict:
    session_date, rows = _load_session_prints(target_date)
    prints = _top_prints(rows) if rows else []
    contracts = []
    for r in prints:
        try:
            contracts.append(_confirm(r))
        except Exception as e:
            print(f"  confirm failed for {r.get('contract')}: {e}")
    held_n = sum(1 for c in contracts if c.get("held") is True)
    return {
        "generated":    datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_date": session_date,
        "held_count":   held_n,
        "total":        len(contracts),
        "held_threshold": HELD_THRESHOLD,
        "contracts":    contracts,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None,
                    help="Force a specific session date (YYYY-MM-DD)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the payload, don't write the file")
    args = ap.parse_args()

    if not pg.available():
        print("  POLYGON_API_KEY not set — writing empty carryover payload.")

    payload = build(args.date)
    print(f"  Carryover: session {payload['session_date']} · "
          f"{payload['held_count']}/{payload['total']} held ≥"
          f"{int(HELD_THRESHOLD*100)}%")
    if args.dry_run:
        print(json.dumps(payload, indent=1))
        return
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"  Wrote {os.path.relpath(OUT_PATH, _BASE)}")


if __name__ == "__main__":
    main()
