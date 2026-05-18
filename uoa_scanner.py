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
LATEST_PATH = os.path.join(_BASE, "docs", "reports", "uoa_latest.json")

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

# Major ETF / index products — excluded. Hedging vehicles, not directional
# single-name smart-money flow (your spec: single-name equities preferred).
EXCLUDE_ETFS = {
    "SPY","QQQ","DIA","IWM","VOO","VTI","RSP","MDY","VXX","UVXY","SVXY","VIXY",
    "XLK","XLF","XLE","XLV","XLI","XLY","XLP","XLU","XLB","XLC","XLRE",
    "TQQQ","SQQQ","SOXL","SOXS","TNA","TZA","SPXL","SPXS","UPRO","SPXU","SDOW",
    "UDOW","TMF","TMV","LABU","LABD","FAS","FAZ","YINN","YANG","NUGT","DUST",
    "TLT","IEF","SHY","HYG","LQD","AGG","BND","TIP","MUB","BIL",
    "GLD","SLV","USO","UNG","GDX","GDXJ","IAU","DBC","CPER",
    "EEM","EFA","FXI","EWZ","VEA","VWO","INDA","EWJ","EWT","EWY",
    "ARKK","ARKG","ARKW","SMH","SOXX","IGV","XBI","IBB","KRE","KBE",
    "ITB","XHB","XOP","OIH","XME","JETS","TAN","ICLN","HACK","BOTZ",
    "SCHD","DGRO","VYM","JEPI","JEPQ","QYLD","VIG","VT","ACWI","EFV",
    "BITO","IBIT","FBTC","GBTC","ETHE","KWEB","FXY","UUP","FXE",
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
    """(spread_pct, grade) — how followable/tradeable the contract is.
    Graded on open interest + day volume (always available). Bid/ask spread%
    is included only once the quotes entitlement is live; when present a wide
    spread can knock an otherwise-liquid grade down."""
    oi  = row.get("open_interest", 0) or 0
    vol = row.get("volume", 0) or 0
    bid = row.get("_bid") or 0
    ask = row.get("_ask") or 0
    spread_pct = None
    if bid and ask and (bid + ask) > 0:
        spread_pct = round((ask - bid) / ((bid + ask) / 2) * 100, 1)

    if   oi >= 2000 and vol >= 2000: grade = "A"
    elif oi >= 500  and vol >= 1000: grade = "B"
    elif oi >= 100:                  grade = "C"
    else:                            grade = "D"
    if spread_pct is not None and spread_pct > 20 and grade in ("A", "B"):
        grade = "C"
    return spread_pct, grade


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
    sweeps, blocks, biggest single print, aggressor side."""
    contract = row["contract"]
    trades = pg.option_trades(contract)
    sweeps, blocks = detect_sweeps(trades)
    side = classify_trades(trades, row)
    biggest = 0
    for t in trades:
        biggest = max(biggest, (t.get("price", 0) or 0) * (t.get("size", 0) or 0) * 100)
    sweep_prem = sum(s["premium"] for s in sweeps)
    max_sweep_size = max((s["size"] for s in sweeps), default=0)
    return {
        "trade_count":    len(trades),
        "sweeps":         len(sweeps),
        "sweep_premium":  round(sweep_prem),
        "blocks":         len(blocks),
        "biggest_print":  round(biggest),
        "max_sweep_size": max_sweep_size,
        "side":           side,
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
    if liq == "D":                                  penalty += 5    # illiquid
    if direction == "hedge":                        penalty += 5    # mixed / ambiguous
    otm = row.get("pct_otm")
    if otm is not None and otm > 25:                penalty += 5    # deep-OTM lotto
    if row.get("flow_side") in ("call_seller", "put_seller"):
        penalty += 4                                                # premium sale, not a buy

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


def _earnings_days(ticker):
    """Calendar days until the next earnings date (yfinance calendar — the
    fixed date/datetime parse). None if unavailable."""
    try:
        import yfinance as yf
        from datetime import date as _date
        cal = yf.Ticker(ticker).calendar
        dates = cal.get("Earnings Date", []) if isinstance(cal, dict) else []
        if not dates:
            return None
        ed = dates[0]
        if isinstance(ed, datetime):
            ed = ed.date()
        elif not isinstance(ed, _date):
            return None
        return (ed - datetime.now(timezone.utc).date()).days
    except Exception:
        return None


# ─── Orchestration ────────────────────────────────────────────────────────────

def scan(universe=None, boost=None, large_caps=None, max_underlyings=None, workers=10):
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

    # Repeat-flow map (recent ledger) + earnings dates for the flagged shortlist
    repeat_map = _load_repeat_map()
    uniq = sorted({r["underlying"] for r in flagged})
    print(f"  Fetching earnings dates for {len(uniq)} underlyings...")
    earnings_map = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for tk, ed in ex.map(lambda t: (t, _earnings_days(t)), uniq):
            earnings_map[tk] = ed

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
            row["earnings_days"] = earnings_map.get(row["underlying"])
            # bias / opening / liquidity / trade-plan — all are score inputs,
            # so they must be computed BEFORE trade_score()
            row["flow_side"], row["direction"] = _bias(row["type"], flow.get("side"))
            row["opening"]   = _opening(row)
            sp, lg = _liquidity(row)
            row["spread_pct"] = sp
            row["liquidity"]  = lg
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
                "volume":      r["volume"],          # flag-day contract volume
                "open_interest": r["open_interest"], # flag-day OI — baseline for
                                                     # next-day OI-retention check
                "tags":        r["tags"],
            }) + "\n")
            n += 1
    print(f"  Ledger: appended {n} signals (score >= {min_score})")


def emit_latest(rows):
    """Write the ranked UOA rows as JSON for the dashboard tab to render."""
    os.makedirs(os.path.dirname(LATEST_PATH), exist_ok=True)
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
            "iv":            r.get("iv"),
            "sweeps":        flow.get("sweeps", 0),
            "blocks":        flow.get("blocks", 0),
            "sweep_premium": flow.get("sweep_premium", 0),
            "biggest_print": flow.get("biggest_print", 0),
            "size_gt_oi":    r.get("size_gt_oi", False),
            "repeat_count":  r.get("repeat_count", 0),
            "earnings_days": r.get("earnings_days"),
            "ask_pct":       side.get("ask_pct", 0),
            "bid_pct":       side.get("bid_pct", 0),
            "side_method":   side.get("method", "none"),
            "golden":        r["golden"],
            "in_universe":   r["in_universe"],
            "trade_score":   r["trade_score"],
            "tier":          r.get("tier", "C"),
            "flow_side":     r.get("flow_side", "unknown"),
            "direction":     r.get("direction", "hedge"),
            "why":           r.get("why", ""),
            "opening":       r.get("opening", "mixed"),
            "liquidity":     r.get("liquidity", "C"),
            "spread_pct":    r.get("spread_pct"),
            "break_even":    r.get("break_even"),
            "be_distance_pct":   r.get("be_distance_pct"),
            "expected_move_pct": r.get("expected_move_pct"),
            "catalyst":      r.get("catalyst", ""),
            "tags":          r["tags"],
        })
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count":     len(out),
        "rows":      out,
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
