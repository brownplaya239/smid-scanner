"""
uoa_scanner.py — Unusual Options Activity screening engine.

Pipeline per run:
  1. Universe — liquid optionable US names (dollar-volume gated), with the
     SMID/IWM scanner names tagged for a Trade Score boost (hybrid mode).
  2. Snapshot pass — pull each underlying's option chain; flag contracts
     that are statistically unusual (vol/OI, $-premium, OTM, IV).
  3. Trade-tape pass — for the flagged shortlist ONLY, pull the executed
     trade feed and detect sweeps / blocks / per-trade premium / Golden
     Sweeps.
  4. Trade Score — 0-100 trade-worthiness: flow conviction + directional
     clarity + underlying confluence + catalyst proximity.
  5. Emit ranked UOA rows (JSON) + append flagged signals to the ledger
     (uoa_signals.jsonl) so the provable-alpha tracker can score them.

Requires Polygon Stocks + Options Developer. Snapshot + trades power
everything here; at/above-ask classification sharpens automatically once
the quotes entitlement is live (classify_trades upgrades itself).
"""

import os
import sys
import json
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import polygon_data as pg

_BASE = os.path.dirname(os.path.abspath(__file__))
LEDGER_PATH = os.path.join(_BASE, "docs", "reports", "uoa_signals.jsonl")

# ─── Screen thresholds (tunable) ──────────────────────────────────────────────

UNIVERSE_MIN_DOLLAR_VOL = 25_000_000   # underlying liquidity floor
MIN_VOL_OI        = 2.0       # day volume / open interest — new positions
MIN_DAY_VOLUME    = 500       # contracts — liquidity floor
MIN_OPEN_INTEREST = 50        # avoid divide-by-tiny noise
MIN_PREMIUM       = 50_000    # $ aggregate day premium — filters retail lottos
MIN_DTE           = 2         # skip expiry-day noise
MAX_DTE           = 400

SWEEP_WINDOW_NS   = 2_000_000_000   # 2s — trades of one parent order
SWEEP_MIN_EXCH    = 2               # distinct exchanges = a sweep
SWEEP_MIN_PREMIUM = 25_000          # $ — minimum cluster premium to log
BLOCK_MIN_PREMIUM = 100_000         # $ — single large print = a block

GOLDEN_PREMIUM    = 1_000_000       # $ — Golden Sweep premium floor
GOLDEN_MAX_DTE    = 30


# ─── Universe ─────────────────────────────────────────────────────────────────

def _boost_set():
    """SMID + IWM scanner names — these get a Trade Score boost (hybrid mode)."""
    names = set()
    try:
        import momentum_scanner as ms
        names |= set(ms.load_iwm_tickers(500))
        names |= set(ms.LARGE_CAPS)
    except Exception as e:
        print(f"  boost-set load failed (non-fatal): {e}")
    return names


def build_universe(min_dollar_vol=UNIVERSE_MIN_DOLLAR_VOL, ref_date=None):
    """Liquid optionable underlyings from the most recent grouped-daily bar,
    gated by dollar volume. Returns (underlyings, boost_set)."""
    ref_date = ref_date or _last_trading_day()
    grouped = pg.grouped_daily(ref_date)
    universe = []
    for tk, bar in grouped.items():
        if "." in tk or len(tk) > 5:
            continue
        dollar_vol = (bar.get("c", 0) or 0) * (bar.get("v", 0) or 0)
        if dollar_vol >= min_dollar_vol:
            universe.append(tk)
    print(f"  Universe: {len(universe)} liquid names (>= ${min_dollar_vol/1e6:.0f}M $-vol)")
    return universe, _boost_set()


def _last_trading_day():
    """Most recent weekday (UTC) — grouped-daily has no weekend bars."""
    d = datetime.now(timezone.utc).date()
    while d.weekday() >= 5:
        d = d.fromordinal(d.toordinal() - 1)
    return d.strftime("%Y-%m-%d")


# ─── Snapshot screen ──────────────────────────────────────────────────────────

def _dte(expiration):
    try:
        exp = datetime.strptime(expiration, "%Y-%m-%d").date()
        return (exp - datetime.now(timezone.utc).date()).days
    except Exception:
        return None


def screen_snapshot(underlying):
    """Pull an underlying's option chain and return the contracts that are
    statistically unusual on snapshot metrics alone (vol/OI, premium, OTM)."""
    chain = pg.option_chain(underlying)
    if not chain:
        return []
    flagged = []
    for c in chain:
        det   = c.get("details", {}) or {}
        day   = c.get("day", {}) or {}
        oi    = c.get("open_interest", 0) or 0
        vol   = day.get("volume", 0) or 0
        strike = det.get("strike_price", 0) or 0
        ctype  = det.get("contract_type", "")
        exp    = det.get("expiration_date", "")
        dte    = _dte(exp)

        if vol < MIN_DAY_VOLUME or oi < MIN_OPEN_INTEREST:
            continue
        if dte is None or dte < MIN_DTE or dte > MAX_DTE:
            continue
        vol_oi = vol / oi if oi else 0
        if vol_oi < MIN_VOL_OI:
            continue

        # price reference for premium: day VWAP, else last trade
        px = day.get("vwap") or (c.get("last_trade", {}) or {}).get("price") or 0
        premium = vol * px * 100
        if premium < MIN_PREMIUM:
            continue

        lq = c.get("last_quote", {}) or {}
        spot = ((c.get("underlying_asset", {}) or {}).get("price")) or 0
        pct_otm = None
        if spot and strike:
            if ctype == "call":
                pct_otm = round((strike / spot - 1) * 100, 1)
            elif ctype == "put":
                pct_otm = round((1 - strike / spot) * 100, 1)

        flagged.append({
            "underlying": underlying,
            "contract":   det.get("ticker", ""),
            "type":       ctype,
            "strike":     strike,
            "expiry":     exp,
            "dte":        dte,
            "spot":       round(spot, 2) if spot else None,
            "pct_otm":    pct_otm,
            "volume":     vol,
            "open_interest": oi,
            "vol_oi":     round(vol_oi, 2),
            "premium":    round(premium),
            "iv":         c.get("implied_volatility"),
            "px":         round(px, 2),
            "_bid":       lq.get("bid"),
            "_ask":       lq.get("ask"),
        })
    return flagged


# ─── Trade-tape analysis ──────────────────────────────────────────────────────

def detect_sweeps(trades):
    """Cluster the executed-trade feed into sweeps. A sweep = trades within a
    short time window spanning multiple exchanges (one parent order routed
    across venues for urgency). Returns (sweeps, blocks)."""
    sweeps, blocks = [], []
    if not trades:
        return sweeps, blocks
    trades = sorted(trades, key=lambda t: t.get("sip_timestamp", 0))

    cluster = []
    def _flush(cl):
        if not cl:
            return
        exch = {t.get("exchange") for t in cl}
        size = sum(t.get("size", 0) or 0 for t in cl)
        prem = sum((t.get("price", 0) or 0) * (t.get("size", 0) or 0) * 100 for t in cl)
        if len(exch) >= SWEEP_MIN_EXCH and prem >= SWEEP_MIN_PREMIUM:
            sweeps.append({
                "trades": len(cl), "exchanges": len(exch),
                "size": size, "premium": round(prem),
                "ts": cl[0].get("sip_timestamp"),
            })

    for t in trades:
        ts = t.get("sip_timestamp", 0)
        # single large print = a block
        prem1 = (t.get("price", 0) or 0) * (t.get("size", 0) or 0) * 100
        if prem1 >= BLOCK_MIN_PREMIUM:
            blocks.append({"size": t.get("size"), "premium": round(prem1),
                           "exchange": t.get("exchange"), "ts": ts})
        if cluster and ts - cluster[0].get("sip_timestamp", 0) > SWEEP_WINDOW_NS:
            _flush(cluster)
            cluster = []
        cluster.append(t)
    _flush(cluster)
    return sweeps, blocks


def classify_trades(trades, snapshot_row):
    """Classify aggressor side: at/above-ask (bullish-conviction buying) vs
    at/below-bid. Two backends behind one seam:
      precise — per-trade NBBO match (needs the quotes endpoint)
      approx  — compare to the contract's snapshot bid/ask (used until the
                quotes entitlement is live)
    Returns {ask_pct, bid_pct, mid_pct, method}."""
    if not trades:
        return {"ask_pct": 0, "bid_pct": 0, "mid_pct": 0, "method": "none"}

    contract = snapshot_row.get("contract", "")
    quotes = pg.option_quotes(contract) if contract else []
    if quotes:
        return _classify_precise(trades, quotes)
    return _classify_approx(trades, snapshot_row)


def _classify_approx(trades, snapshot_row):
    """Approximate side classification from the single snapshot bid/ask.
    Imprecise for older trades — labelled method='approx' so the dashboard
    can flag the confidence."""
    bid = snapshot_row.get("_bid") or 0
    ask = snapshot_row.get("_ask") or 0
    if not (bid and ask):
        return {"ask_pct": 0, "bid_pct": 0, "mid_pct": 0, "method": "approx"}
    a = b = m = 0
    for t in trades:
        p = t.get("price", 0) or 0
        if p >= ask:   a += 1
        elif p <= bid: b += 1
        else:          m += 1
    n = max(a + b + m, 1)
    return {"ask_pct": round(100 * a / n), "bid_pct": round(100 * b / n),
            "mid_pct": round(100 * m / n), "method": "approx"}


def _classify_precise(trades, quotes):
    """Per-trade NBBO classification — each trade matched to the quote in
    effect at its timestamp. Active once the quotes endpoint is entitled."""
    quotes = sorted(quotes, key=lambda q: q.get("sip_timestamp", 0))
    qts = [q.get("sip_timestamp", 0) for q in quotes]
    import bisect
    a = b = m = 0
    for t in trades:
        ts = t.get("sip_timestamp", 0)
        i = bisect.bisect_right(qts, ts) - 1
        if i < 0:
            m += 1
            continue
        q = quotes[i]
        bid = q.get("bid_price", 0) or 0
        ask = q.get("ask_price", 0) or 0
        p = t.get("price", 0) or 0
        if ask and p >= ask:   a += 1
        elif bid and p <= bid: b += 1
        else:                  m += 1
    n = max(a + b + m, 1)
    return {"ask_pct": round(100 * a / n), "bid_pct": round(100 * b / n),
            "mid_pct": round(100 * m / n), "method": "precise"}


def analyze_flow(row):
    """Pull the trade tape for one flagged contract and summarise the flow:
    sweeps, blocks, biggest single print, aggressor side."""
    contract = row["contract"]
    trades = pg.option_trades(contract)
    sweeps, blocks = detect_sweeps(trades)
    side = classify_trades(trades, row)
    biggest = 0
    for t in trades:
        biggest = max(biggest, (t.get("price", 0) or 0) * (t.get("size", 0) or 0) * 100)
    sweep_prem = sum(s["premium"] for s in sweeps)
    return {
        "trade_count":   len(trades),
        "sweeps":        len(sweeps),
        "sweep_premium": round(sweep_prem),
        "blocks":        len(blocks),
        "biggest_print": round(biggest),
        "side":          side,
    }


def is_golden_sweep(row, flow):
    """Golden Sweep: a sweep on a single stock, >$1M premium, <30 DTE.
    Ask-side confirmation is added once the quotes entitlement is live."""
    if flow["sweeps"] < 1:
        return False
    if flow["sweep_premium"] < GOLDEN_PREMIUM:
        return False
    if row["dte"] is None or row["dte"] > GOLDEN_MAX_DTE:
        return False
    side = flow["side"]
    if side["method"] == "precise" and side["ask_pct"] < 60:
        return False
    return True


# ─── Trade Score ──────────────────────────────────────────────────────────────

def trade_score(row, flow, in_universe):
    """0-100 trade-worthiness. Flow conviction (45) + directional clarity
    (25) + underlying confluence (20) + structure (10)."""
    import math
    score = 0.0

    # Flow conviction — premium (log-scaled), sweeps, vol/OI
    prem = max(row["premium"], flow["sweep_premium"], flow["biggest_print"])
    score += min(25, 25 * math.log10(max(prem, 1) / 50_000) / math.log10(40))
    score += min(12, flow["sweeps"] * 4)
    score += min(8, (row["vol_oi"] - 2) * 1.5)

    # Directional clarity — OTM with sane DTE
    if row.get("pct_otm") is not None and 0 < row["pct_otm"] < 25:
        score += 14
    if row["dte"] is not None and 7 <= row["dte"] <= 60:
        score += 11

    # Underlying confluence — in the scanner universe (hybrid boost)
    if in_universe:
        score += 12
    # aggressor agreement: ask-side calls / bid-side puts = conviction
    side = flow["side"]
    if side["method"] != "none":
        lean = side["ask_pct"] if row["type"] == "call" else side["bid_pct"]
        score += min(8, lean / 12.5)

    # Structure — block confirmation
    if flow["blocks"] > 0:
        score += 10

    return round(min(100, max(0, score)))


# ─── Orchestration ────────────────────────────────────────────────────────────

def scan(universe=None, boost=None, max_underlyings=None, workers=10):
    """Run the full UOA screen. Returns ranked rows (highest Trade Score first)."""
    if universe is None:
        universe, boost = build_universe()
    boost = boost or set()
    if max_underlyings:
        universe = universe[:max_underlyings]

    print(f"  Snapshot screen across {len(universe)} underlyings...")
    flagged = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for hits in ex.map(screen_snapshot, universe):
            flagged.extend(hits)
    print(f"  Snapshot flagged {len(flagged)} unusual contracts")

    print(f"  Trade-tape analysis on {len(flagged)} contracts...")
    rows = []
    def _enrich(row):
        try:
            flow = analyze_flow(row)
            row["flow"]    = flow
            row["golden"]  = is_golden_sweep(row, flow)
            row["in_universe"] = row["underlying"] in boost
            row["trade_score"] = trade_score(row, flow, row["in_universe"])
            tags = []
            if row["golden"]:            tags.append("Golden Sweep")
            elif flow["sweeps"] > 0:     tags.append("Sweep")
            if flow["blocks"] > 0:       tags.append("Block")
            if row["in_universe"]:       tags.append("In Universe")
            row["tags"] = tags
            return row
        except Exception as e:
            print(f"  flow analysis failed for {row.get('contract')}: {e}")
            return None
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(_enrich, flagged):
            if r:
                rows.append(r)

    rows.sort(key=lambda r: r["trade_score"], reverse=True)
    print(f"  {len(rows)} ranked UOA rows  "
          f"(golden sweeps: {sum(1 for r in rows if r['golden'])})")
    return rows


def append_ledger(rows, min_score=55):
    """Append high-conviction signals to the ledger so the provable-alpha
    tracker can score their forward returns. One JSON object per line."""
    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n = 0
    with open(LEDGER_PATH, "a", encoding="utf-8") as f:
        for r in rows:
            if r["trade_score"] < min_score:
                continue
            f.write(json.dumps({
                "id":          f"{r['contract']}_{now}",
                "flagged_at":  now,
                "ticker":      r["underlying"],
                "signal_type": "golden_sweep" if r["golden"]
                               else ("sweep" if r["flow"]["sweeps"] else "voloi"),
                "contract":    r["contract"],
                "underlying_px_at_flag": r["spot"],
                "trade_score": r["trade_score"],
                "premium":     r["premium"],
                "dte":         r["dte"],
                "tags":        r["tags"],
            }) + "\n")
            n += 1
    print(f"  Ledger: appended {n} signals (score >= {min_score})")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:                                  # quick test: explicit tickers
        uni = [a.upper() for a in args]
        rows = scan(universe=uni, boost=set(uni))
    else:
        rows = scan()
    for r in rows[:15]:
        f = r["flow"]
        print(f"  {r['trade_score']:3}  {r['underlying']:6} {r['type'][:1].upper()} "
              f"${r['strike']:<8} {r['expiry']}  vol/OI {r['vol_oi']:<5}  "
              f"prem ${r['premium']/1e6:.2f}M  sweeps {f['sweeps']}  "
              f"{'/'.join(r['tags'])}")
