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
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import polygon_data as pg
import themes

_BASE = os.path.dirname(os.path.abspath(__file__))
LEDGER_PATH = os.path.join(_BASE, "docs", "reports", "uoa_signals.jsonl")
LATEST_PATH = os.path.join(_BASE, "docs", "reports", "uoa_latest.json")
META_CACHE_PATH = os.path.join(_BASE, "docs", "reports", "uoa_meta_cache.json")
# OI history cache — one entry per contract OCC ticker. Each scan reads
# the previous day's OI for delta computation, then writes today's.
# Schema: { "O:NVDA260620C00220000": {"date": "2026-05-28", "oi": 1234}, ... }
# Persisted across runs so the 6×/day cadence doesn't lose state.
OI_HISTORY_PATH = os.path.join(_BASE, "docs", "reports", "uoa_oi_history.json")

# ─── Screen thresholds (tunable) ──────────────────────────────────────────────

UNIVERSE_MIN_DOLLAR_VOL = 25_000_000   # underlying liquidity floor
MIN_VOL_OI        = 2.0       # day volume / open interest — new positions
MIN_DAY_VOLUME    = 500       # contracts — liquidity floor
MIN_OPEN_INTEREST = 50        # avoid divide-by-tiny noise
MIN_PREMIUM       = 100_000   # $ aggregate day premium — "live" floor
MIN_DTE           = 2         # skip expiry-day noise
MAX_DTE           = 730       # include LEAPS (365+)
DEEP_ITM_PCT      = -15.0     # exclude contracts this far in-the-money (noise)

PREMIUM_CLEAN     = 300_000   # $ — "clean signal" tier
PREMIUM_HIGH      = 500_000   # $ — "high conviction" tier

SWEEP_WINDOW_NS   = 2_000_000_000   # 2s — trades of one parent order
SWEEP_MIN_EXCH    = 2               # distinct exchanges = a sweep
SWEEP_MIN_PREMIUM = 25_000          # $ — minimum cluster premium to log
BLOCK_MIN_PREMIUM = 100_000         # $ — single large print = a block

GOLDEN_PREMIUM    = 1_000_000       # $ — Golden Sweep premium floor
GOLDEN_MAX_DTE    = 30
EARNINGS_WINDOW   = 10              # flag flow into earnings within N days
REPEAT_LOOKBACK_DAYS = 5            # ledger window for repeat-flow detection
META_REFRESH_DAYS    = 10           # days a cached earnings/cap/sector entry lives

# Major ETF / index products — excluded. Hedging vehicles, not directional
# single-name smart-money flow (your spec: single-name equities preferred).
# Also covers the rapidly-growing class of 2x / -1x / -2x SINGLE-STOCK
# leveraged ETFs (Tradr, GraniteShares, Direxion, Defiance). These show
# up in grouped_daily looking like normal small-caps but are synthetic
# daily-reset vehicles that decay over multi-day holds — they should
# NEVER be in a swing-grade universe. The list below is curated; new
# launches need to be added manually (TODO: replace with a yfinance
# quoteType=="ETF" check during universe build).
EXCLUDE_ETFS = {
    # Broad-market index + sector
    "SPY","QQQ","DIA","IWM","VOO","VTI","RSP","MDY","VXX","UVXY","SVXY","VIXY",
    "XLK","XLF","XLE","XLV","XLI","XLY","XLP","XLU","XLB","XLC","XLRE",
    # Leveraged/inverse broad indices
    "TQQQ","SQQQ","SOXL","SOXS","TNA","TZA","SPXL","SPXS","UPRO","SPXU","SDOW",
    "UDOW","TMF","TMV","LABU","LABD","FAS","FAZ","YINN","YANG","NUGT","DUST",
    # Bonds / commodities / FX / international
    "TLT","IEF","SHY","HYG","LQD","AGG","BND","TIP","MUB","BIL",
    "GLD","SLV","USO","UNG","GDX","GDXJ","IAU","DBC","CPER",
    "EEM","EFA","FXI","EWZ","VEA","VWO","INDA","EWJ","EWT","EWY",
    # Thematic / sector
    "ARKK","ARKG","ARKW","SMH","SOXX","IGV","XBI","IBB","KRE","KBE",
    "ITB","XHB","XOP","OIH","XME","JETS","TAN","ICLN","HACK","BOTZ",
    "SCHD","DGRO","VYM","JEPI","JEPQ","QYLD","VIG","VT","ACWI","EFV",
    "BITO","IBIT","FBTC","GBTC","ETHE","KWEB","FXY","UUP","FXE",
    # ────────────────────────────────────────────────────────────────
    # Single-stock leveraged ETFs — Tradr (1x/2x/3x daily reset)
    # These look like normal small-caps in the universe screen but
    # are synthetic vehicles. The bug that triggered this list: NVTX
    # (2x NVTS) showed up as a +39.5% "breakout" alongside the real
    # NVTS at +20%, double-counting the trade.
    "NVDU","NVDD","NVDL","NVDX",          # NVDA 2x/-2x family
    "NVTU","NVTX",                          # NVTS 2x family
    "TSLL","TSLZ","TSLS","TSLR","TSLY","TSDD","TSLG","TSLT","TSLQ",  # TSLA 2x/-2x/income
    "AAPU","AAPD","AAPB","AAPX",          # AAPL 2x/-2x
    "AMZU","AMZD","AMZZ",                  # AMZN 2x/-2x
    "METU","METD","FBL","FBX",            # META/FB 2x/-2x
    "MSFU","MSFD","MSFL","MSFX",          # MSFT 2x/-2x
    "GGLL","GGLS",                          # GOOGL 2x/-2x
    "NFXL","NFXS",                          # NFLX 2x/-2x
    "CONL","CONI",                          # COIN 2x/-2x
    "MSTU","MSTZ","MSTX",                  # MSTR 2x/-2x
    "AMDL","AMDY",                          # AMD 2x
    "HOOX","HOOXL",                         # HOOD 2x
    "PLTU","PLTD",                          # PLTR 2x
    "DELU","DELD",                          # DELL 2x (if launched)
    "PYPL","SHOL","SHOY",                  # PYPL/SHOP 2x
    # Old single-stock leveraged ETFs (Direxion, GraniteShares 1x/2x)
    "BABX","BABL","BBAX",                  # BABA leveraged
    "DISL","DISD",                          # DIS leveraged
    "WMTL","WMTD",                          # WMT leveraged
}


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
        if "." in tk or len(tk) > 5 or tk in EXCLUDE_ETFS:
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


def _dte_bucket(dte):
    """Group days-to-expiry into trader-meaningful buckets."""
    if dte is None:  return "unknown"
    if dte <= 14:    return "urgent"        # 0-14   urgent / speculative
    if dte <= 90:    return "swing"         # 14-90  swing flow
    if dte <= 365:   return "positioning"   # 90-365 institutional positioning
    return "leaps"                          # 365+   LEAPS / high-conviction


def _premium_tier(premium):
    """Premium conviction tier — live / clean / high."""
    if premium >= PREMIUM_HIGH:   return "high"
    if premium >= PREMIUM_CLEAN:  return "clean"
    return "live"


def _tier(score, golden=False):
    """Same-day CONVICTION tier (A+/A/B/C) from the recalibrated Trade Score.
    Thresholds calibrated so A+ is genuinely rare flow, not routine mega-cap
    prints. Distinct from next-day OI confirmation (uoa_alpha)."""
    if score >= 90:
        return "A+"
    if score >= 76:
        return "A"
    if score >= 58:
        return "B"
    return "C"


def _bias(ctype, side):
    """Two-axis bias read — who did what, and what it implies.

      flow_side : call_buyer / put_buyer / call_seller / put_seller /
                  mixed / unknown
      direction : bullish / bearish / income / hedge

    A call BUYER is bullish; a call SELLER is selling premium (income, not a
    directional bet). Approximate until the quotes entitlement makes the
    aggressor-side classification exact."""
    if (side or {}).get("method", "none") == "none":
        return "unknown", "hedge"
    ask = side.get("ask_pct", 0)
    bid = side.get("bid_pct", 0)
    if ctype == "call":
        if ask >= 55: return "call_buyer",  "bullish"
        if bid >= 55: return "call_seller", "income"
        return "mixed", "hedge"
    else:                                  # put
        if ask >= 55: return "put_buyer",  "bearish"
        if bid >= 55: return "put_seller", "income"
        return "mixed", "hedge"


def _why(row, flow):
    """Plain-language thesis — why this contract is flagged."""
    bits = []
    if row.get("golden"):
        bits.append("Golden sweep")
    elif flow.get("sweeps", 0) > 0:
        n = flow["sweeps"]
        bits.append(f"{n} sweep" + ("s" if n > 1 else ""))
    if flow.get("blocks", 0) > 0:
        n = flow["blocks"]
        bits.append(f"{n} block" + ("s" if n > 1 else ""))
    prem = row.get("premium", 0)
    bits.append(f"${prem/1e6:.1f}M premium" if prem >= 1e6
                else f"${prem/1e3:.0f}k premium")
    bits.append(f"{row.get('vol_oi', 0):.1f}x OI")
    if row.get("size_gt_oi"):
        bits.append("sweep > OI")
    if row.get("repeat_count", 0) > 0:
        bits.append(f"repeat x{row['repeat_count']}")
    ed = row.get("earnings_days")
    if ed is not None and 0 <= ed <= EARNINGS_WINDOW:
        bits.append(f"earnings {ed}d")
    otm = row.get("pct_otm")
    if otm is not None and otm >= 10:
        bits.append(f"{otm:.0f}% OTM")
    return "  -  ".join(bits)


def _opening(row):
    """Likelihood the flow is OPENING new positions vs closing existing ones.
    Same-day estimate from vol/OI, sweep-size>OI and repeat flow; next-day OI
    (uoa_alpha) confirms it. likely_open / mixed / likely_close."""
    vol_oi = row.get("vol_oi", 0) or 0
    if vol_oi >= 3.5 or row.get("size_gt_oi") or row.get("repeat_count", 0) > 0:
        return "likely_open"
    if vol_oi < 2.5:
        return "likely_close"
    return "mixed"


def _liquidity(row):
    """Grade — how followable/tradeable the contract is.
    Graded on open interest + day volume (always available on Polygon
    Options Starter). Bid/ask spread is NOT included — Starter doesn't
    populate quotes and our previous attempts produced 100% null
    spread_pct, which leaked into the JSON as a dead field. If/when
    we upgrade Polygon to a quotes-enabled tier we can re-introduce
    spread-aware grading; until then, OI + volume is the honest signal."""
    oi  = row.get("open_interest", 0) or 0
    vol = row.get("volume", 0) or 0
    if   oi >= 2000 and vol >= 2000: return "A"
    elif oi >= 500  and vol >= 1000: return "B"
    elif oi >= 100:                  return "C"
    else:                            return "D"


def _trade_plan(row):
    """Objective trade-context math (no prescriptive entry/exit levels):
    break-even price, % the stock must move to break even, the option-implied
    1-sigma expected move over the contract's life, and the catalyst."""
    import math
    px     = row.get("px", 0) or 0
    strike = row.get("strike", 0) or 0
    spot   = row.get("spot") or 0
    ctype  = row.get("type", "")
    be = row.get("be_snap")                       # Polygon-computed break-even
    if not be and strike and px:                   # fallback: strike +/- premium
        be = round(strike + px, 2) if ctype == "call" else round(strike - px, 2)
    be_dist = None
    if be and spot:
        be_dist = round((be / spot - 1) * 100, 1) if ctype == "call" \
                  else round((1 - be / spot) * 100, 1)
    em = None
    iv, dte = row.get("iv"), row.get("dte")
    if iv and dte and dte > 0:
        em = round(iv * math.sqrt(dte / 365) * 100, 1)
    ed = row.get("earnings_days")
    catalyst = f"Earnings in {ed}d" if (ed is not None and 0 <= ed <= 21) else ""
    return be, be_dist, em, catalyst


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

        # exclude deep in-the-money noise
        if pct_otm is not None and pct_otm < DEEP_ITM_PCT:
            continue

        flagged.append({
            "underlying": underlying,
            "contract":   det.get("ticker", ""),
            "type":       ctype,
            "strike":     strike,
            "expiry":     exp,
            "dte":        dte,
            "dte_bucket": _dte_bucket(dte),
            "spot":       round(spot, 2) if spot else None,
            "pct_otm":    pct_otm,
            "is_otm":     (pct_otm is not None and pct_otm >= 0),
            "volume":     vol,
            "open_interest": oi,
            "vol_oi":     round(vol_oi, 2),
            "premium":    round(premium),
            "premium_tier": _premium_tier(premium),
            "iv":         c.get("implied_volatility"),
            "px":         round(px, 2),
            "be_snap":    c.get("break_even_price"),   # Polygon-computed BE
            "_bid":       lq.get("bid"),               # populates when the
            "_ask":       lq.get("ask"),               # quotes entitlement lands
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
    """Approximate side classification — until the quotes entitlement is live
    we lack the NBBO at each trade's timestamp. Heuristic: split trades by the
    contract's bid/ask midpoint (fallback: day VWAP). Trades above the mid
    lean buyer-aggressive, below lean seller-aggressive. Labelled
    method='approx' so the dashboard flags it as an estimate."""
    bid = snapshot_row.get("_bid") or 0
    ask = snapshot_row.get("_ask") or 0
    mid = (bid + ask) / 2 if (bid and ask) else (snapshot_row.get("px") or 0)
    if not mid:
        return {"ask_pct": 0, "bid_pct": 0, "mid_pct": 0, "method": "approx"}
    a = b = m = 0
    for t in trades:
        p = t.get("price", 0) or 0
        if not p:        m += 1
        elif p > mid:    a += 1
        elif p < mid:    b += 1
        else:            m += 1
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
    sweeps, blocks, biggest single print, aggressor side, plus the two
    timestamps that let the UI answer "did this trade already work or am
    I late?":

      last_print_ts    — most recent trade (ISO 8601 UTC)
      biggest_print_ts — timestamp of the marquee single-print
                         (size × price × 100). This is the one that
                         drives "printed Xm ago" + spot-delta math.

    sip_timestamp is nanoseconds-since-epoch; we convert to ISO seconds
    so the client can subtract from Date.now() without unit gymnastics.
    """
    contract = row["contract"]
    trades = pg.option_trades(contract)
    sweeps, blocks = detect_sweeps(trades)
    side = classify_trades(trades, row)
    biggest = 0
    biggest_ts_ns = 0
    last_ts_ns = 0
    for t in trades:
        prem = (t.get("price", 0) or 0) * (t.get("size", 0) or 0) * 100
        ts = t.get("sip_timestamp", 0) or 0
        if prem > biggest:
            biggest = prem
            biggest_ts_ns = ts
        if ts > last_ts_ns:
            last_ts_ns = ts
    sweep_prem = sum(s["premium"] for s in sweeps)
    max_sweep_size = max((s["size"] for s in sweeps), default=0)

    def _ns_to_iso(ns):
        if not ns:
            return None
        try:
            # sip_timestamp is nanoseconds since epoch UTC. Truncate to
            # microseconds (Python datetime cap) and round to whole sec
            # for compact serialization.
            return (datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)
                    .isoformat(timespec="seconds"))
        except (ValueError, OSError):
            return None

    return {
        "trade_count":      len(trades),
        "sweeps":           len(sweeps),
        "sweep_premium":    round(sweep_prem),
        "blocks":           len(blocks),
        "biggest_print":    round(biggest),
        "max_sweep_size":   max_sweep_size,
        "side":             side,
        "last_print_ts":    _ns_to_iso(last_ts_ns),
        "biggest_print_ts": _ns_to_iso(biggest_ts_ns),
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

def trade_score(row, flow):
    """0-100 trade-worthiness. Seven capped components + quality penalties,
    recalibrated so 90+ is genuinely RARE flow — not just a big mega-cap
    print. Components (max): premium 20 | vol/OI 20 | opening 15 | repeat 15
    | liquidity 10 | catalyst 10 | directional 10.

    Returns just the final integer score for backward compat. Mutates
    `row["score_components"]` with the per-factor breakdown so the
    client can render a popover ("Score 92 = 18 premium + 17 vol/OI +
    15 opening + …"). The breakdown is essential for trust — a single
    opaque number doesn't tell users WHY a contract scored high.

    Requires row to already carry opening / liquidity / direction / flow_side."""
    import math

    # Premium (0-20) — log-scaled on the biggest of aggregate / sweep / print
    prem = max(row.get("premium", 0), flow.get("sweep_premium", 0),
               flow.get("biggest_print", 0))
    premium_pts = max(0, min(20, 20 * math.log10(max(prem, 1) / 100_000) / math.log10(50)))

    # Vol/OI (0-20) — capped log curve so huge mega-cap ratios don't re-saturate
    voi = row.get("vol_oi", 0) or 0
    voi_pts = max(0, min(20, 20 * math.log10(max(voi, 1)) / math.log10(12)))

    # Opening likelihood (0-15) — new positions, not closing
    opening = row.get("opening", "mixed")
    open_pts = 15 if opening == "likely_open" else 6 if opening == "mixed" else 0

    # Repeat / aggregated flow (0-15) — a campaign beats a one-off
    repeat_pts = min(15, row.get("repeat_count", 0) * 7)

    # Liquidity (0-10) — is the contract actually followable
    liq = row.get("liquidity", "C")
    liq_pts = {"A": 10, "B": 7, "C": 4, "D": 0}.get(liq, 4)

    # Catalyst (0-10) — flow positioned into an earnings window
    ed = row.get("earnings_days")
    cat_pts = 10 if (ed is not None and 0 <= ed <= EARNINGS_WINDOW) else 0

    # Directional alignment (0-10) — clean buy-side conviction
    direction = row.get("direction", "hedge")
    dir_pts = 10 if direction in ("bullish", "bearish") else 4 if direction == "income" else 0

    base = (premium_pts + voi_pts + open_pts + repeat_pts +
            liq_pts + cat_pts + dir_pts)

    # Penalties — keep low-quality flow out of the top tier
    penalty = 0
    penalty_reasons = []
    if liq == "D":
        penalty += 5
        penalty_reasons.append("illiquid (D)")
    if direction == "hedge":
        penalty += 5
        penalty_reasons.append("ambiguous / hedge")
    otm = row.get("pct_otm")
    if otm is not None and otm > 25:
        penalty += 5
        penalty_reasons.append("deep OTM (>25%)")
    if row.get("flow_side") in ("call_seller", "put_seller"):
        penalty += 4
        penalty_reasons.append("seller-side (premium sale)")

    # Stash the breakdown on the row so the client can render a hover/
    # tap popover. Each component is rounded to an integer for clean UI
    # display; sum may be 1 off the final score due to rounding — call
    # it out in the client copy.
    row["score_components"] = {
        "premium":     round(premium_pts),
        "vol_oi":      round(voi_pts),
        "opening":     round(open_pts),
        "repeat":      round(repeat_pts),
        "liquidity":   round(liq_pts),
        "catalyst":    round(cat_pts),
        "directional": round(dir_pts),
        "penalty":     -penalty if penalty else 0,
        "penalty_reasons": penalty_reasons,
    }
    return round(max(0, min(100, base - penalty)))


def _load_repeat_map(lookback_days=REPEAT_LOOKBACK_DAYS):
    """Count recent ledger appearances per contract — repeat flow = a campaign."""
    from collections import Counter
    counts = Counter()
    if not os.path.exists(LEDGER_PATH):
        return counts
    cutoff = datetime.now(timezone.utc).date().toordinal() - lookback_days
    try:
        with open(LEDGER_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    d = datetime.strptime(rec["flagged_at"][:10], "%Y-%m-%d").date()
                    if d.toordinal() >= cutoff:
                        counts[rec.get("contract", "")] += 1
                except Exception:
                    pass
    except Exception:
        pass
    return counts


def _earnings_date(ticker):
    """Next earnings date + session as (YYYY-MM-DD, session) where session is
    'BMO' (before market open), 'AMC' (after market close), or None when the
    time-of-day is unknown / a midnight placeholder.

    yfinance exposes the announcement window two ways: `Ticker.calendar` gives
    only the date, while `Ticker.get_earnings_dates()` returns a DataFrame
    whose index includes the timestamp. We use the latter when available so
    BMO/AMC can be derived from the ET hour (<9:30 = BMO, >=16:00 = AMC)."""
    try:
        import yfinance as yf
        from datetime import date as _date
        tk = yf.Ticker(ticker)

        # First try get_earnings_dates() for the timestamp (gives session)
        try:
            df = tk.get_earnings_dates(limit=12)
        except Exception:
            df = None
        if df is not None and not df.empty:
            # The DataFrame is indexed by tz-aware Timestamps, newest first.
            # Walk from oldest to newest to find the next future event.
            today_utc = datetime.now(timezone.utc).date()
            future = []
            for ts in df.index:
                try:
                    ts_et = ts.tz_convert("America/New_York")
                except Exception:
                    continue
                if ts_et.date() >= today_utc:
                    future.append(ts_et)
            if future:
                ts_et = min(future)             # nearest upcoming
                hh, mm = ts_et.hour, ts_et.minute
                # yfinance uses placeholder 00:00 when session is unknown
                if hh == 0 and mm == 0:
                    session = None
                elif hh < 9 or (hh == 9 and mm < 30):
                    session = "BMO"
                elif hh >= 16:
                    session = "AMC"
                else:
                    session = None
                return ts_et.date().isoformat(), session

        # Fallback: calendar (date only, no session)
        cal = tk.calendar
        dates = cal.get("Earnings Date", []) if isinstance(cal, dict) else []
        if not dates:
            return None, None
        ed = dates[0]
        if isinstance(ed, datetime):
            ed = ed.date()
        elif not isinstance(ed, _date):
            return None, None
        return ed.isoformat(), None
    except Exception:
        return None, None


def _load_meta_cache():
    """Per-ticker earnings/cap/sector cache, persisted in docs/reports/ so it
    survives across CI runs. yfinance earnings lookups are slow and flaky;
    caching the absolute earnings date means most runs make zero yfinance
    calls — only stale or newly-seen tickers are fetched."""
    try:
        with open(META_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_meta_cache(cache):
    try:
        os.makedirs(os.path.dirname(META_CACHE_PATH), exist_ok=True)
        with open(META_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=0, sort_keys=True)
    except Exception as e:
        print(f"  meta cache save failed (non-fatal): {e}")


def _cap_bucket(mkt_cap):
    """Market-cap bucket — UOA reads very differently across cap tiers."""
    if not mkt_cap:           return "unknown"
    if mkt_cap >= 200e9:      return "mega"     # Mag-7-scale
    if mkt_cap >= 10e9:       return "large"
    if mkt_cap >= 2e9:        return "mid"
    return "small"


def _sic_sector(sic):
    """Coarse sector from a Polygon SIC code — SIC is granular, this rolls it
    up into ~16 trader-meaningful buckets for the dashboard filter."""
    try:
        c = int(sic)
    except (TypeError, ValueError):
        return "Other"
    if c == 3674:                              return "Semiconductors"
    if 7370 <= c <= 7379:                      return "Tech / Software"
    if c in (3571, 3572, 3576, 3577) or 3661 <= c <= 3679:
        return "Tech / Hardware"
    if 2833 <= c <= 2836 or c == 8731:         return "Healthcare / Biotech"
    if 8000 <= c <= 8099:                      return "Healthcare / Services"
    if 1310 <= c <= 1389 or c == 2911:         return "Energy"
    if 6000 <= c <= 6199:                      return "Financials / Banks"
    if 6200 <= c <= 6499:                      return "Financials"
    if 6500 <= c <= 6799:                      return "Real Estate"
    if 4900 <= c <= 4999:                      return "Utilities"
    if 4800 <= c <= 4899:                      return "Communications"
    if 1000 <= c <= 1099 or 3300 <= c <= 3399: return "Materials / Metals"
    if 2800 <= c <= 2899:                      return "Materials / Chemicals"
    if 3700 <= c <= 3799:                      return "Industrials / Transport Eq"
    if 3400 <= c <= 3599:                      return "Industrials / Machinery"
    if 1500 <= c <= 1799 or 4000 <= c <= 4799: return "Industrials"
    if 5200 <= c <= 5999:                      return "Consumer / Retail"
    if 2000 <= c <= 2199:                      return "Consumer / Food & Bev"
    if 7000 <= c <= 7299 or 7800 <= c <= 7999: return "Consumer / Services"
    return "Other"


def _meta_is_fresh(entry, today):
    """A cached metadata entry is reusable if it was fetched recently AND its
    earnings date hasn't already passed (a passed date means a newer one
    should be picked up). Entries missing required schema fields are also
    considered stale so a schema upgrade auto-refreshes the cache."""
    try:
        fetched = datetime.strptime(entry["fetched"], "%Y-%m-%d").date()
    except (KeyError, ValueError, TypeError):
        return False
    if (today - fetched).days > META_REFRESH_DAYS:
        return False
    # Schema-upgrade safety: re-fetch legacy entries that pre-date the
    # earnings_session field so BMO/AMC populates without a manual cache wipe.
    if "earnings_session" not in entry:
        return False
    ed = entry.get("earnings_date")
    if ed:
        try:
            if datetime.strptime(ed, "%Y-%m-%d").date() < today:
                return False
        except ValueError:
            return False
    return True


def _fetch_underlying_meta(ticker):
    """Cold metadata fetch for one ticker: earnings date + session (yfinance)
    plus market cap and coarse sector (Polygon ticker reference)."""
    ed, session = _earnings_date(ticker)
    entry = {
        "earnings_date": ed,
        "earnings_session": session,    # "BMO" / "AMC" / None
        "mkt_cap": None,
        "sector": "Other",
        "fetched": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    try:
        d = pg.ticker_details(ticker) or {}
        if d.get("market_cap"):
            entry["mkt_cap"] = d["market_cap"]
        entry["sector"] = _sic_sector(d.get("sic_code"))
    except Exception:
        pass
    return entry


# ─── Orchestration ────────────────────────────────────────────────────────────

def scan(universe=None, boost=None, large_caps=None, max_underlyings=None, workers=40):
    """Run the full UOA screen. Returns ranked rows (highest Trade Score first)."""
    if universe is None:
        universe, boost = build_universe()
    boost = boost or set()
    if large_caps is None:
        try:
            import momentum_scanner as ms
            large_caps = set(ms.LARGE_CAPS)
        except Exception:
            large_caps = set()
    if max_underlyings:
        universe = universe[:max_underlyings]

    print(f"  Snapshot screen across {len(universe)} underlyings...")
    flagged = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for hits in ex.map(screen_snapshot, universe):
            flagged.extend(hits)
    print(f"  Snapshot flagged {len(flagged)} unusual contracts")

    # Repeat-flow map (recent ledger) + per-underlying metadata. Metadata is
    # cached on disk (uoa_meta_cache.json) keyed by ticker — only stale or
    # newly-seen names trigger a slow, flaky yfinance earnings call.
    repeat_map = _load_repeat_map()
    uniq = sorted({r["underlying"] for r in flagged})
    today = datetime.now(timezone.utc).date()
    cache = _load_meta_cache()
    stale = [t for t in uniq if not _meta_is_fresh(cache.get(t, {}), today)]
    print(f"  Metadata for {len(uniq)} underlyings "
          f"({len(uniq) - len(stale)} cached, {len(stale)} to fetch)...")
    # yfinance bans bursty clients — keep its concurrency low even when the
    # Polygon passes run wide.
    meta_workers = max(1, min(workers, 8))
    with ThreadPoolExecutor(max_workers=meta_workers) as ex:
        for t, entry in ex.map(lambda t: (t, _fetch_underlying_meta(t)), stale):
            cache[t] = entry
    if stale:
        _save_meta_cache(cache)
    meta_map = {}
    for t in uniq:
        e = cache.get(t, {})
        ed = e.get("earnings_date")
        days = None
        if ed:
            try:
                days = (datetime.strptime(ed, "%Y-%m-%d").date() - today).days
            except ValueError:
                days = None
        meta_map[t] = {"earnings_days": days, "mkt_cap": e.get("mkt_cap"),
                       "sector": e.get("sector", "Other")}

    print(f"  Trade-tape analysis on {len(flagged)} contracts...")
    rows = []
    def _enrich(row):
        try:
            flow = analyze_flow(row)
            row["flow"]         = flow
            row["golden"]       = is_golden_sweep(row, flow)
            row["in_universe"]  = row["underlying"] in boost
            row["cap_class"]    = "large" if row["underlying"] in large_caps else "smid"
            row["size_gt_oi"]   = flow["max_sweep_size"] > (row["open_interest"] or 0)
            row["repeat_count"] = repeat_map.get(row["contract"], 0)
            _m = meta_map.get(row["underlying"], {})
            row["earnings_days"] = _m.get("earnings_days")
            row["mkt_cap"]    = _m.get("mkt_cap")
            row["cap_bucket"] = _cap_bucket(_m.get("mkt_cap"))
            row["sector"]     = _m.get("sector", "Other")
            row["themes"]     = themes.themes_for(row["underlying"])
            # bias / opening / liquidity / trade-plan — all are score inputs,
            # so they must be computed BEFORE trade_score()
            row["flow_side"], row["direction"] = _bias(row["type"], flow.get("side"))
            row["opening"]   = _opening(row)
            row["liquidity"] = _liquidity(row)
            be, bd, em, cat = _trade_plan(row)
            row["break_even"]        = be
            row["be_distance_pct"]   = bd
            row["expected_move_pct"] = em
            row["catalyst"]          = cat
            # score + tier
            row["trade_score"] = trade_score(row, flow)
            row["tier"]        = _tier(row["trade_score"], row["golden"])
            # tags + plain-language thesis
            tags = []
            if row["golden"]:            tags.append("Golden Sweep")
            elif flow["sweeps"] > 0:     tags.append("Sweep")
            if flow["blocks"] > 0:       tags.append("Block")
            if row["size_gt_oi"]:        tags.append("Size>OI")
            if row["repeat_count"] > 0:  tags.append("Repeat")
            ed = row["earnings_days"]
            if ed is not None and 0 <= ed <= EARNINGS_WINDOW:
                tags.append("Into ERN")
            if row["in_universe"]:       tags.append("In Universe")
            row["tags"] = tags
            row["why"]  = _why(row, flow)
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
                "type":        r["type"],
                "flow_side":   r.get("flow_side", "unknown"),
                "direction":   r.get("direction", "hedge"),
                "opening":     r.get("opening", "mixed"),
                "liquidity":   r.get("liquidity", "C"),
                "cap_bucket":  r.get("cap_bucket", "unknown"),
                "sector":      r.get("sector", "Other"),
                "themes":      r.get("themes", []),
                "volume":      r["volume"],          # flag-day contract volume
                "open_interest": r["open_interest"], # flag-day OI — baseline for
                                                     # next-day OI-retention check
                "tags":        r["tags"],
            }) + "\n")
            n += 1
    print(f"  Ledger: appended {n} signals (score >= {min_score})")


# ── OI history cache ──
# Stores yesterday's OI per contract so today's scan can emit a delta.
# Read once at scan start, written once at scan end. Survives across the
# 6×/day scans by snapshotting only when the calendar date changes — so
# the "previous" OI we compare against is always end-of-day yesterday,
# not the most recent intraday read which would always show near-zero
# delta. Date comparison is in US/Eastern so it lines up with NYSE.
def _load_oi_history():
    try:
        with open(OI_HISTORY_PATH, encoding="utf-8") as f:
            return json.load(f) or {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_oi_history(history):
    try:
        os.makedirs(os.path.dirname(OI_HISTORY_PATH), exist_ok=True)
        with open(OI_HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=1)
    except OSError:
        pass


def _enrich_with_oi_history(rows):
    """Mutates rows in-place adding `prev_oi`, `oi_delta`, `oi_delta_pct`.
    Then snapshots today's OI ONLY if the calendar date has advanced — so
    intraday scans don't overwrite end-of-day baseline with mid-day reads."""
    history = _load_oi_history()
    et_today = datetime.now(tz=ZoneInfo("America/New_York")).date().isoformat()
    for r in rows:
        key = r.get("contract")
        if not key:
            continue
        prev = history.get(key) or {}
        prev_date = prev.get("date")
        prev_oi = prev.get("oi")
        # Only treat prev_oi as legitimate if it's from a PREVIOUS day —
        # otherwise we'd compare today's mid-day OI against today's
        # earlier mid-day OI, producing meaningless near-zero deltas.
        if prev_date and prev_date < et_today and isinstance(prev_oi, int):
            r["prev_oi"] = prev_oi
            r["oi_delta"] = (r.get("open_interest", 0) or 0) - prev_oi
            if prev_oi > 0:
                r["oi_delta_pct"] = round(
                    100 * r["oi_delta"] / prev_oi, 1)
        # Snapshot today's OI for next run. Only OVERWRITE if today's
        # date differs from the stored date — preserves the EOD baseline
        # across intraday re-runs.
        if prev_date != et_today:
            history[key] = {"date": et_today,
                            "oi": r.get("open_interest", 0) or 0}
    _save_oi_history(history)


def emit_latest(rows):
    """Write the ranked UOA rows as JSON for the dashboard tab to render.

    PROTECTION: if THIS run produced 0 rows AND the existing file has
    rows from a recent successful run, refuse to overwrite. After-hours
    scans + transient Polygon errors regularly return empty results;
    overwriting good intraday data with empty results blanks the desk
    until the next RTH scan. The previous run's content stays live;
    the freshness pill (driven by `generated`) makes it obvious the
    data is from earlier."""
    os.makedirs(os.path.dirname(LATEST_PATH), exist_ok=True)
    # Empty-result guard
    if not rows:
        try:
            with open(LATEST_PATH, encoding="utf-8") as f:
                existing = json.load(f)
            existing_count = len(existing.get("rows", []))
            if existing_count > 0:
                attempt_iso = datetime.now(timezone.utc).isoformat(
                    timespec="seconds")
                print(f"  Scan produced 0 rows — PRESERVING existing "
                      f"{existing_count}-row payload (likely after-hours "
                      f"or transient Polygon hiccup; existing data was "
                      f"from {existing.get('generated', '?')}).")
                # Make the preserve VISIBLE in the workflow (it exits 0, so it
                # otherwise looks identical to a healthy run) and advance a
                # `last_attempt` stamp distinct from `generated`. The freshness
                # monitor reads both: scanner-ran-recently + data-stale ==
                # "upstream empty, intentionally preserved" (not our failure);
                # no recent attempt == "pipeline down". Rows/`generated` are
                # left untouched so the desk keeps showing the last good data.
                if os.environ.get("GITHUB_ACTIONS"):
                    print(f"::warning::UOA scan returned 0 rows — preserved "
                          f"{existing_count}-row payload from "
                          f"{existing.get('generated', '?')}. Scanner healthy; "
                          f"upstream (Polygon) came back empty. Data held "
                          f"intentionally — not a pipeline failure.")
                existing["last_attempt"] = attempt_iso
                existing["last_attempt_rows"] = 0
                try:
                    with open(LATEST_PATH, "w", encoding="utf-8") as f:
                        json.dump(existing, f, indent=1)
                except OSError:
                    pass
                return
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        print("  Scan produced 0 rows — no existing data to preserve, "
              "writing empty payload.")
    # Enrich with OI delta from yesterday's snapshot (cached locally).
    _enrich_with_oi_history(rows)
    # Compute total universe premium for the "% of total" column.
    total_premium = sum((r.get("premium") or 0) for r in rows) or 1
    out = []
    for r in rows:
        flow = r.get("flow", {}) or {}
        side = flow.get("side", {}) or {}
        out.append({
            "ticker":        r["underlying"],
            "contract":      r["contract"],
            "type":          r["type"],
            "strike":        r["strike"],
            "expiry":        r["expiry"],
            "dte":           r["dte"],
            "dte_bucket":    r.get("dte_bucket", "unknown"),
            "spot":          r["spot"],
            "pct_otm":       r.get("pct_otm"),
            "is_otm":        r.get("is_otm", False),
            "volume":        r["volume"],
            "open_interest": r["open_interest"],
            "vol_oi":        r["vol_oi"],
            "premium":       r["premium"],
            "premium_tier":  r.get("premium_tier", "live"),
            "cap_class":     r.get("cap_class", "smid"),
            "cap_bucket":    r.get("cap_bucket", "unknown"),
            "mkt_cap":       r.get("mkt_cap"),
            "sector":        r.get("sector", "Other"),
            "themes":        r.get("themes", []),
            "iv":            r.get("iv"),
            # Bid / ask quote at scan time — populates the Fill vs Spread
            # mini-bar on the UOA table. Polygon Options Starter doesn't
            # always populate last_quote.bid/ask (silver/gold tiers do
            # better); when absent the bar gracefully falls back to a
            # neutral display.
            "bid":           r.get("_bid"),
            "ask":           r.get("_ask"),
            "mid":           (round((r["_bid"] + r["_ask"]) / 2, 4)
                              if r.get("_bid") and r.get("_ask") else None),
            "last_price":    r.get("px"),
            # OI delta vs end-of-day yesterday. prev_oi is null on the
            # first run after a new contract appears; the UI shows "—".
            "prev_oi":       r.get("prev_oi"),
            "oi_delta":      r.get("oi_delta"),
            "oi_delta_pct":  r.get("oi_delta_pct"),
            # This contract's share of today's total flagged-universe
            # premium. Lets traders calibrate "is this size unusual
            # within today's flow?" The denominator is our filtered
            # universe (not market-wide) — labeled honestly in the UI.
            "pct_total_premium": round(
                100 * (r.get("premium") or 0) / total_premium, 2),
            # Execution venue flags — sweep already exists implicitly via
            # `golden`, but emit explicit booleans so the UI tag system
            # can render them per-row without re-deriving.
            "is_sweep":      bool(flow.get("sweeps", 0) > 0),
            "is_block":      bool(flow.get("blocks", 0) > 0),
            "is_golden":     bool(r.get("golden", False)),
            "sweeps":        flow.get("sweeps", 0),
            "blocks":        flow.get("blocks", 0),
            "sweep_premium": flow.get("sweep_premium", 0),
            "biggest_print": flow.get("biggest_print", 0),
            # ISO 8601 UTC. last_print_ts = most recent trade on the
            # contract; biggest_print_ts = timestamp of the marquee
            # premium print (most informative for "am I late?" UX).
            # spot_at_print = the snapshot's underlying spot at scan
            # time — close-enough proxy for "underlying when the
            # marquee print hit" since snapshots fire every ~75 min.
            "last_print_ts":    flow.get("last_print_ts"),
            "biggest_print_ts": flow.get("biggest_print_ts"),
            "spot_at_print":    r.get("spot"),
            "size_gt_oi":    r.get("size_gt_oi", False),
            "repeat_count":  r.get("repeat_count", 0),
            "earnings_days": r.get("earnings_days"),
            "ask_pct":       side.get("ask_pct", 0),
            "bid_pct":       side.get("bid_pct", 0),
            "side_method":   side.get("method", "none"),
            "golden":        r["golden"],
            "in_universe":   r["in_universe"],
            "trade_score":   r["trade_score"],
            "score_components": r.get("score_components", {}),
            "tier":          r.get("tier", "C"),
            "flow_side":     r.get("flow_side", "unknown"),
            "direction":     r.get("direction", "hedge"),
            "why":           r.get("why", ""),
            "opening":       r.get("opening", "mixed"),
            "liquidity":     r.get("liquidity", "C"),
            "break_even":    r.get("break_even"),
            "be_distance_pct":   r.get("be_distance_pct"),
            "expected_move_pct": r.get("expected_move_pct"),
            "catalyst":      r.get("catalyst", ""),
            "tags":          r["tags"],
        })
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = {
        "generated":    now_iso,
        # last_attempt == generated on a successful (non-empty) run; the
        # preserve guard advances last_attempt alone when a run comes back
        # empty, so the monitor can separate "upstream empty" from "down".
        "last_attempt": now_iso,
        "count":        len(out),
        "rows":         out,
    }
    with open(LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    print(f"  Wrote uoa_latest.json ({len(out)} rows)")


def run():
    """Production entry point — scan, publish JSON, append the signal ledger."""
    rows = scan()
    emit_latest(rows)
    append_ledger(rows)
    return rows


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:                                  # quick test: explicit tickers
        uni = [a.upper() for a in args]
        rows = scan(universe=uni, boost=set(uni))
    else:
        rows = run()
    for r in rows[:15]:
        f = r["flow"]
        print(f"  {r['trade_score']:3}  {r['underlying']:6} {r['type'][:1].upper()} "
              f"${r['strike']:<8} {r['expiry']}  vol/OI {r['vol_oi']:<5}  "
              f"prem ${r['premium']/1e6:.2f}M  sweeps {f['sweeps']}  "
              f"{'/'.join(r['tags'])}")
