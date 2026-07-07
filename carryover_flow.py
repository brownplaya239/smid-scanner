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


# ── Conviction-dashboard enrichment ─────────────────────────────────────
# Turns each confirmed print into an interpretable, ranked signal. Every
# derived field is baked into the JSON so the Desk tile + Daily Brief render
# the same numbers (single source of truth) and the frontend only sorts.
CONV_PREM_FULL = 50_000_000    # premium ($) that maxes the 15% size weight
BIG_PREMIUM    = 2_000_000     # "large" print for A/A+ priority
MID_PREMIUM    = 1_000_000
NEAR_EXPIRY_DAYS = 7


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _fmt_prem(p) -> str:
    if not p and p != 0:
        return ""
    p = float(p)
    if p >= 1e6:
        return f"${p/1e6:.1f}M"
    if p >= 1e3:
        return f"${round(p/1e3)}K"
    return f"${int(p)}"


def _direction(typ: str | None, side: str | None):
    """(direction, label) for a bullish/bearish/hedge badge. `side` is the
    ledger's flow_side; falls back to the option type's natural lean."""
    is_call, is_put = (typ == "call"), (typ == "put")
    s = (side or "").lower()
    base = s if s in ("bullish", "bearish") else (
        "bullish" if is_call else "bearish" if is_put else "unknown")
    if base == "unknown":
        return "unknown", "Unknown direction"
    default = "bullish" if is_call else "bearish" if is_put else None
    if default and base != default:          # e.g. a call flagged bearish → sold/hedge
        return "hedge", "Potential hedge"
    kind = "Call" if is_call else "Put" if is_put else ""
    return base, (f"Bullish {kind}" if base == "bullish" else f"Bearish {kind}").strip()


def _dte(expiry: str | None, ref_iso: str) -> int | None:
    try:
        e = datetime.strptime(expiry, "%Y-%m-%d").date()
        r = datetime.strptime(ref_iso, "%Y-%m-%d").date()
        return (e - r).days
    except Exception:
        return None


def _moneyness_stability(spot, strike) -> float:
    """Proxy for the 5% 'price/IV stability' weight — near-the-money strikes
    are more durable than deep OTM lottery tickets. 0.6 when spot unknown."""
    try:
        if not spot or not strike:
            return 0.6
        m = abs(float(spot) - float(strike)) / float(spot)
        return 1.0 if m <= 0.10 else 0.6 if m <= 0.25 else 0.3
    except Exception:
        return 0.6


def _enrich(c: dict, ref_iso: str) -> dict:
    """Add the conviction-dashboard fields to a confirmed contract in place."""
    delta   = c.get("delta")
    volume  = c.get("volume") or 0
    premium = c.get("premium") or 0
    flag_oi = c.get("flag_oi") or 0
    typ     = c.get("type")

    direction, dir_label = _direction(typ, c.get("side"))
    c["direction"], c["dir_label"] = direction, dir_label
    warnings: list[str] = []

    oi_conf = whale_held = net_new_oi = rem = prem_rem = rel_oi = conviction = None
    priority = "Pending"

    if c.get("today_oi") is not None and volume:
        oi_conf     = delta / volume                       # can be <0 or >1
        whale_held  = _clamp(oi_conf)                       # capped 0..1
        net_new_oi  = int(delta - volume)                  # follow-on beyond whale
        rem         = int(_clamp(delta, 0, volume))         # whale contracts still open
        prem_rem    = round(premium * whale_held)
        rel_oi      = (delta / flag_oi) if flag_oi else (1.0 if delta > 0 else 0.0)

        conviction = round(
            whale_held * 35 +
            _clamp(oi_conf) * 35 +
            _clamp(premium / CONV_PREM_FULL) * 15 +
            _clamp(rel_oi) * 10 +
            _moneyness_stability(c.get("spot_at_flag"), c.get("strike")) * 5)

        big = premium >= BIG_PREMIUM
        if whale_held >= 0.70 and oi_conf >= 0.70 and big:
            priority = "A+"
        elif whale_held >= 0.50 and oi_conf >= 0.60 and premium >= MID_PREMIUM:
            priority = "A"
        elif oi_conf >= 0.40:
            priority = "B"
        else:
            priority = "Avoid"

        if oi_conf < 0.25:
            warnings.append("Weak OI follow-through — opening trade not confirmed (possible day-trade/close).")
        if delta <= 0:
            warnings.append("Open interest fell vs the flag day — position likely closed or rolled, not opened.")
        if flag_oi and volume > flag_oi * 3 and oi_conf < 0.5:
            warnings.append("Print dwarfed prior OI but OI barely moved — confirm cautiously.")
    else:
        warnings.append("OI not yet OCC-settled for this contract — confirmation pending.")

    dte = _dte(c.get("expiry"), ref_iso)
    if dte is not None and dte < 0:
        warnings.append("Already expired — treat as historical.")
    elif dte is not None and dte <= NEAR_EXPIRY_DAYS:
        warnings.append(f"Near expiry ({dte}d) — may be gamma/speculation, not durable positioning.")

    c["oi_confirmed"]        = round(oi_conf, 3) if oi_conf is not None else None
    c["whale_held"]          = round(whale_held, 3) if whale_held is not None else None
    c["net_new_oi"]          = net_new_oi
    c["contracts_remaining"] = rem
    c["premium_remaining"]   = prem_rem
    c["rel_oi_increase"]     = round(rel_oi, 3) if rel_oi is not None else None
    c["conviction"]          = conviction
    c["priority"]            = priority
    c["dte"]                 = dte
    c["warnings"]            = warnings
    return c


def _interpret(contracts: list[dict]) -> str:
    """One-paragraph auto-summary that names the best follow-through idea,
    flags a notable fade, and calls out bearish (put) positioning."""
    confirmed = [c for c in contracts if c.get("conviction") is not None]
    if not confirmed:
        return ("No prints have OCC-settled OI confirmation yet — the board fills "
                "in as this morning's open interest settles.")
    top = confirmed[0]
    parts = [
        f"{top['ticker']} is the strongest follow-through candidate "
        f"(conviction {top['conviction']}/100): "
        f"{'whale held overnight' if top.get('held') else 'partial hold'}, "
        f"{round((top.get('oi_confirmed') or 0) * 100)}% of the print showed up as "
        f"new open interest, {_fmt_prem(top.get('premium'))} premium."
    ]
    fades = [c for c in confirmed if c.get("priority") == "Avoid"
             and (c.get("premium") or 0) >= 5_000_000]
    if fades:
        f = fades[0]
        parts.append(
            f"{f['ticker']} failed OI confirmation "
            f"({round((f.get('oi_confirmed') or 0) * 100)}%) despite a "
            f"{_fmt_prem(f.get('premium'))} print — likely closed/scalped, not a durable hold.")
    puts = [c for c in confirmed[:3] if c.get("direction") == "bearish"]
    if puts:
        parts.append(
            f"{puts[0]['ticker']} is a bearish put — treat as downside positioning "
            "unless price action invalidates.")
    return " ".join(parts)


def build(target_date: str | None = None) -> dict:
    session_date, rows = _load_session_prints(target_date)
    prints = _top_prints(rows) if rows else []
    contracts = []
    for r in prints:
        try:
            contracts.append(_confirm(r))
        except Exception as e:
            print(f"  confirm failed for {r.get('contract')}: {e}")

    # Enrich each print with the conviction-dashboard fields.
    ref_iso = datetime.now(ET).date().isoformat()
    for c in contracts:
        _enrich(c, ref_iso)

    # Cross-contract: flag possible rolls (same ticker, multiple prints).
    from collections import Counter
    tk_counts = Counter(c["ticker"] for c in contracts)
    for c in contracts:
        if tk_counts[c["ticker"]] > 1:
            c["warnings"].append(
                f"Possible roll — {c['ticker']} has multiple large prints; net exposure may differ.")

    # Rank: confirmed first, then conviction desc.
    contracts.sort(key=lambda c: (c.get("conviction") is None, -(c.get("conviction") or 0)))

    held_n = sum(1 for c in contracts if c.get("held") is True)
    conf = [c for c in contracts if c.get("conviction") is not None]
    return {
        "generated":      datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_date":   session_date,
        "held_count":     held_n,
        "total":          len(contracts),
        "confirmed_count": len(conf),
        "top_conviction": conf[0]["conviction"] if conf else None,
        "held_threshold": HELD_THRESHOLD,
        "interpretation": _interpret(contracts),
        "contracts":      contracts,
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
