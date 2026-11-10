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

Measurement rules (2026-11 accuracy audit — every published aggregate):
  • ONE ALERT = one contract per US/Eastern flag day. The scanner re-logs a
    still-flagged contract on every scan (up to ~16×/day incl. after-close
    runs); those re-logs are the SAME alert, so aggregates keep only the
    day's FIRST flag (earliest entry, the score users first saw). The full
    day's volume is kept as the OI-confirmation baseline.
  • DIRECTION-ADJUSTED: a bought put is a bearish call on the stock, so its
    return / hit / excursion are sign-flipped. Hit rate, EV, MFE/MAE, IC and
    calibration grade only directional flow (calls bought = bullish, puts
    bought = bearish); premium selling and mixed/unknown prints have no
    stock direction to grade and are excluded (the direction split grades
    sellers separately on their lean).
  • MFE/MAE averages use only signals whose full 20-session window matured.
  • Confidence intervals are clustered by flag date (alerts on the same day
    share the same market move, so they are not independent trades).
  • PRICE BASIS: daily bars are split-adjusted; the flag-time spot is not.
    The flag price is split-adjusted, and a signal whose flag price still
    disagrees with its flag-day close by > ~1.6× is excluded as a bad print.
  • NEXT-DAY OI is read once, on the first weekday after the flag, and
    frozen — not re-read from whatever the chain shows weeks later.
"""

import itertools
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from math import sqrt
from statistics import mean, median, pstdev

import polygon_data as pg

_BASE = os.path.dirname(os.path.abspath(__file__))
# Internal ETL state (ledger + alpha cache) lives in data/ so it stays OUT of
# the public Pages artifact. EDGE/SCORED stay in docs/reports (browser-served).
LEDGER_PATH = os.path.join(_BASE, "data", "uoa_signals.jsonl")
EDGE_PATH = os.path.join(_BASE, "docs", "reports", "uoa_edge.json")
SCORED_PATH = os.path.join(_BASE, "docs", "reports", "uoa_signals_scored.json")
ALPHA_CACHE_PATH = os.path.join(_BASE, "data", "uoa_alpha_cache.json")
# First post-flag OI read per alert (contract|ET flag date) — frozen so the
# "next-day OI held" stat measures the NEXT day, not whatever the chain
# shows when a 3-week-old signal is re-scored.
OI_NEXTDAY_PATH = os.path.join(_BASE, "data", "uoa_oi_nextday.json")

FINAL_AGE_DAYS = 35  # a signal this old has every horizon matured — freeze it
# A flag price that disagrees with the flag-day (split-adjusted) close by
# more than this factor is a price-basis error (unhandled split, bad print),
# not a market move — the signal is excluded from return statistics.
BASIS_MAX_RATIO = 1.6

# Trade-side books for the direction split: (book, sign of the stock move
# that pays the book). Mixed / unknown prints have no classified aggressor
# side and are reported separately, never folded into a buy book.
_BOOKS = {
    "call_buyer": ("call_buy", 1),
    "put_buyer": ("put_buy", -1),
    "put_seller": ("seller", 1),
    "call_seller": ("seller", -1),
}

METHOD_NOTE = (
    "Stock-based track record: every return, hit rate, MFE/MAE and EV is "
    "the UNDERLYING STOCK's move (vs SPY), not the option's P&L. One alert "
    "= a contract's first flag of the US/Eastern day (scan re-logs "
    "collapsed). Direction-adjusted: put buying is graded as a bearish "
    "call, so a falling stock is a win for it; premium selling and "
    "mixed/unknown prints carry no stock direction and are left out of hit "
    "rate / EV / MFE / MAE. MFE/MAE = best / worst stock move in the "
    "flow's direction over the 20 sessions after the flag. 95% CIs are "
    "clustered by flag date. Signals whose flag price disagrees with the "
    "split-adjusted bars are excluded. Buyer vs seller is an ESTIMATE from "
    "where the day's prints traded vs the day's average price (the data "
    "plan has no live bid/ask), so 'bought' means 'mostly traded above "
    "the day's average'."
)
WORKERS = 24  # fan out bar/OI pulls; modest — _oi_now pulls full chains

HORIZONS = [1, 3, 5, 10, 20]
SCORE_BUCKETS = [("80-100", 80, 101), ("65-79", 65, 80), ("55-64", 55, 65)]
SIGNAL_TYPES = ("golden_sweep", "sweep", "voloi")
DTE_BUCKETS = ("urgent", "swing", "positioning", "leaps")
ATTRIB_TAGS = ("Golden Sweep", "Sweep", "Block", "Size>OI", "Repeat", "Into ERN", "In Universe")


def _dte_bucket(dte):
    if dte is None:
        return "unknown"
    if dte <= 14:
        return "urgent"
    if dte <= 90:
        return "swing"
    if dte <= 365:
        return "positioning"
    return "leaps"


def _parse_occ(contract):
    """(contract_type, strike) from an OCC option ticker, e.g.
    'O:NVDA260529C00217500' -> ('call', 217.5). The last 8 digits are the
    strike x1000; the char before them is C/P. The underlying root is
    variable-length, so everything is read from the end. Returns
    (None, None) on any malformed input — callers treat that as unknown."""
    try:
        s = (contract or "").strip()
        cp = s[-9].upper()
        if cp not in ("C", "P"):
            return None, None
        strike = int(s[-8:]) / 1000.0
        if strike <= 0:
            return None, None
        return ("call" if cp == "C" else "put"), strike
    except ValueError, IndexError, TypeError:
        return None, None


def is_weekend_et(iso_ts):
    """True if a UTC ISO timestamp falls on a Sat/Sun in US Eastern time.
    ET, not UTC: a Friday-evening ET flag is Saturday in UTC."""
    try:
        dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        try:
            from zoneinfo import ZoneInfo

            et = dt.astimezone(ZoneInfo("America/New_York"))
        except Exception:
            et = dt - timedelta(hours=4)
        return et.weekday() >= 5
    except ValueError, TypeError:
        return False


def _et_date(iso_ts):
    """US/Eastern calendar date (YYYY-MM-DD) of a UTC ISO timestamp, or the
    raw first 10 chars if it can't be parsed. A 7:30 PM ET flag is the next
    day in UTC — alerts belong to the ET trading day."""
    try:
        dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        from zoneinfo import ZoneInfo

        return dt.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return str(iso_ts or "")[:10]


def _next_weekday(d):
    """The first weekday strictly after date d (Fri -> Mon)."""
    d = d + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _dir_sign(s):
    """+1 for a bullish directional bet (calls bought at the ask), -1 for a
    bearish one (puts bought at the ask), None for flow with no stock
    direction to grade (premium selling, mixed / unknown prints). Old ledger
    rows without `direction` fall back to `flow_side`."""
    d = s.get("direction")
    if d == "bullish":
        return 1
    if d == "bearish":
        return -1
    if d is None:
        fs = s.get("flow_side")
        if fs == "call_buyer":
            return 1
        if fs == "put_buyer":
            return -1
    return None


def _basis_ok(s):
    """False when a scored signal's flag price is on a different price basis
    than its bars (an unhandled split / bad print). Detected from the stored
    returns: the close-anchored excess (exc_c) and the flag-anchored excess
    imply the flag-day close / flag-price ratio. Signals without the needed
    fields pass (they were checked at scoring time, see forward_returns)."""
    if s.get("basis_suspect"):
        return False
    r1 = (s.get("returns") or {}).get(1) or (s.get("returns") or {}).get("1") or {}
    ret, exc, exc_c = r1.get("ret"), r1.get("excess"), r1.get("exc_c")
    if ret is None or exc is None or exc_c is None:
        return True
    try:
        # c1/p0 = 1+ret ; c1/p0c = 1+(exc_c+spy) with spy = ret-exc
        ratio = (1 + ret / 100) / (1 + (exc_c + ret - exc) / 100)
    except ZeroDivisionError:
        return False
    return (1 / BASIS_MAX_RATIO) <= ratio <= BASIS_MAX_RATIO


def unique_alerts(scored):
    """Collapse scan re-logs into one alert per (contract, ET flag date).

    The representative is the day's FIRST flag (earliest entry, the score
    users first saw). `day_volume` keeps the largest cumulative contract
    volume any scan saw that day — the right baseline for next-day OI —
    and the OI status is re-derived against it. `relogs` records how many
    ledger rows the alert had."""
    groups = {}
    order = []
    for s in scored:
        key = (s.get("contract") or s.get("id"), _et_date(s.get("flagged_at")))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(s)
    out = []
    for key in order:
        rows = groups[key]
        first = min(rows, key=lambda r: r.get("flagged_at") or "")
        rep = dict(first)
        vols = [r.get("volume") for r in rows if r.get("volume") is not None]
        rep["day_volume"] = max(vols) if vols else first.get("volume")
        rep["relogs"] = len(rows)
        oi = first.get("oi") or {}
        if oi.get("oi_change") is not None and rep["day_volume"]:
            rep["oi"] = dict(oi, **_oi_grade(oi["oi_change"], rep["day_volume"]))
        out.append(rep)
    return out


def _signed_returns(s):
    """Per-horizon returns in the flow's direction (bearish flow flipped), or
    None when the signal has no stock direction / a bad price basis."""
    sg = _dir_sign(s)
    if sg is None or not _basis_ok(s):
        return None
    out = {}
    for h, v in (s.get("returns") or {}).items():
        if not v:
            continue
        out[int(h)] = {
            k: (sg * v[k] if v.get(k) is not None else None) for k in ("ret", "excess", "exc_c")
        }
    return out


def _signed_excursion(s, require_full=True):
    """(favourable, adverse) excursion in the flow's direction, in % of the
    flag price: bullish = (MFE, MAE); bearish = (-MAE, -MFE) because a put
    buyer profits from the LOW and is hurt by the HIGH. None when not
    directional, bad basis, missing, or (require_full) the 20-session
    window hasn't fully matured."""
    sg = _dir_sign(s)
    if sg is None or not _basis_ok(s):
        return None
    exc = s.get("excursion") or {}
    mfe, mae = exc.get("mfe"), exc.get("mae")
    if mfe is None or mae is None:
        return None
    if require_full:
        rets = s.get("returns") or {}
        if not (rets.get(20) or rets.get("20")):
            return None
    return (mfe, mae) if sg > 0 else (-mae, -mfe)


def load_ledger():
    """Read the append-only signal ledger: monthly gzip archives (in
    order) then the hot file. Rotation (ledger_rotate.py) moves lines
    older than its keep-window into data/uoa_signals_archive_YYYYMM
    .jsonl.gz so the hot file stays under GitHub's 100 MB hard limit
    (the 2026-09-16 publish outage); nothing is ever deleted."""
    out = []

    # Weekend-dated rows are re-logged copies of Friday's snapshot (see
    # uoa_scanner weekend guard). They stay in the append-only files but
    # are excluded here, so every consumer reads the clean record.
    def _read(fh):
        for line in fh:
            line = line.strip()
            if line:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if is_weekend_et(row.get("flagged_at")):
                    continue
                out.append(row)

    import glob
    import gzip

    for path in sorted(glob.glob(os.path.join(_BASE, "data", "uoa_signals_archive_*.jsonl.gz"))):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                _read(f)
        except Exception:
            pass
    if os.path.exists(LEDGER_PATH):
        with open(LEDGER_PATH, encoding="utf-8") as f:
            _read(f)
    return out


def _pending_oi():
    return {"status": "pending", "oi_change": None, "retained_pct": None}


def _is_final(signal, today):
    """True once every forward horizon has matured (flagged >= FINAL_AGE_DAYS
    ago) — the signal's score is frozen and can be served from cache."""
    try:
        fd = datetime.strptime(signal["flagged_at"][:10], "%Y-%m-%d").date()
    except KeyError, ValueError, TypeError:
        return False
    return (today - fd).days >= FINAL_AGE_DAYS


def _restore_returns(d):
    """JSON turns the integer horizon keys into strings on round-trip — put
    them back to int so _agg / _group can index them."""
    return {int(k): v for k, v in (d or {}).items()}


def _load_alpha_cache():
    """Frozen scores for signals past FINAL_AGE_DAYS, persisted in
    docs/reports/ so they survive across CI runs and never re-hit the API."""
    try:
        with open(ALPHA_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_alpha_cache(cache):
    try:
        os.makedirs(os.path.dirname(ALPHA_CACHE_PATH), exist_ok=True)
        with open(ALPHA_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=0)
    except Exception as e:
        print(f"  alpha cache save failed (non-fatal): {e}")


def _bars(ticker, days=160):
    """{date_str: {c,h,l}} for a ticker from Polygon daily bars."""
    out = {}
    for b in pg.daily_bars(ticker, days=days):
        ts = b.get("t")
        if ts:
            d = datetime.fromtimestamp(ts / 1000, UTC).strftime("%Y-%m-%d")
            out[d] = {"c": b.get("c"), "h": b.get("h"), "l": b.get("l")}
    return out


def _base_index(signal, dates):
    """The flag's trading-day index in a sorted date list (or None)."""
    flagged = _et_date(signal["flagged_at"])
    if not dates:
        return None, None
    base = flagged if flagged in dates else next((d for d in dates if d >= flagged), None)
    if not base:
        return None, None
    return base, dates.index(base)


def split_factor(splits, flag_date, today=None):
    """Factor that puts a flag-time (unadjusted) price on the split-adjusted
    basis of daily bars fetched today: the product of split_from/split_to
    over every split EXECUTED after the flag date (and not after today —
    Polygon also lists announced future splits, which bars don't reflect
    yet). A 4-for-1 split after the flag turns a $400 flag price into $100."""
    today = today or datetime.now(UTC).date().isoformat()
    f = 1.0
    for sp in splits or []:
        ex = str(sp.get("execution_date") or "")
        fr, to = sp.get("split_from"), sp.get("split_to")
        if ex and flag_date < ex <= today and fr and to:
            f *= fr / to
    return f


def _flag_price(signal, bars, base, sfac=1.0):
    """(p0, basis_ok): the flag-time spot on the bars' split-adjusted basis
    (or the flag-day close when no spot was logged), and whether it agrees
    with the flag-day close within BASIS_MAX_RATIO. A disagreement beyond
    that is an unhandled split / bad print, not a market move."""
    p0c = bars[base].get("c")
    raw = signal.get("underlying_px_at_flag")
    p0 = raw * sfac if raw else p0c
    if not p0:
        return None, False
    if p0c:
        r = p0c / p0
        if not (1 / BASIS_MAX_RATIO) <= r <= BASIS_MAX_RATIO:
            return p0, False
    return p0, True


def forward_returns(signal, bars, spy_closes, sfac=1.0):
    """Underlying forward return at each horizon + excess vs SPY. Returns {}
    when the flag price is on a different basis than the bars (see
    _flag_price) — such a signal must not enter any statistic."""
    dates = sorted(bars)
    base, idx = _base_index(signal, dates)
    if base is None:
        return {}
    p0, ok = _flag_price(signal, bars, base, sfac)
    if not p0 or not ok:
        return {}
    spy0 = spy_closes.get(base)
    # Dual anchor (2026-07 bias audit): p0 is the INTRADAY flag price but
    # the SPY leg anchors at that day's CLOSE — asymmetric, and the
    # flag->close drift of day 0 leaks into ret. Keep p0 for continuity
    # (it's what a subscriber acting on the intraday alert could get) but
    # ALSO store a close-anchored excess (exc_c) so the inflation is
    # measurable instead of assumed. Same-anchor legs on both sides.
    p0c = bars[base]["c"]
    out = {}
    for h in HORIZONS:
        if idx + h >= len(dates):
            continue
        d_h = dates[idx + h]
        ret = (bars[d_h]["c"] / p0 - 1) * 100
        excess = exc_c = None
        # SPY on the SAME session as the stock's h-th bar (aligning by
        # index would drift whenever the stock is missing a bar).
        spy_h = spy_closes.get(d_h)
        if spy0 and spy_h:
            spy_ret = (spy_h / spy0 - 1) * 100
            excess = ret - spy_ret
            if p0c:
                exc_c = (bars[d_h]["c"] / p0c - 1) * 100 - spy_ret
        out[h] = {
            "ret": round(ret, 2),
            "excess": round(excess, 2) if excess is not None else None,
            "exc_c": round(exc_c, 2) if exc_c is not None else None,
        }
    return out


def excursions(signal, bars, sfac=1.0):
    """Max favourable / adverse excursion of the underlying over the +20d
    window, from daily highs/lows — how far the trade could have run, and
    how much heat it took, before settling. Raw stock terms (high = mfe,
    low = mae); aggregates flip them for bearish flow (_signed_excursion)."""
    dates = sorted(bars)
    base, idx = _base_index(signal, dates)
    if base is None:
        return {"mfe": None, "mae": None}
    p0, ok = _flag_price(signal, bars, base, sfac)
    if not p0 or not ok:
        return {"mfe": None, "mae": None}
    window = dates[idx + 1 : idx + 1 + 20]
    highs = [bars[d]["h"] for d in window if bars[d].get("h")]
    lows = [bars[d]["l"] for d in window if bars[d].get("l")]
    return {
        "mfe": round((max(highs) / p0 - 1) * 100, 1) if highs else None,
        "mae": round((min(lows) / p0 - 1) * 100, 1) if lows else None,
    }


def _oi_grade(change, vol):
    """Grade an OI change against the flag-day contract volume. OI rising by
    a large share of that volume = the flow opened NEW positions that
    STUCK.  confirmed (>50%) / weak (>15%) / closed."""
    vol = vol or 1
    if change > 0.50 * vol:
        status = "confirmed"
    elif change > 0.15 * vol:
        status = "weak"
    else:
        status = "closed"
    return {"status": status, "retained_pct": round(100 * change / vol)}


def _oi_key(signal):
    return f"{signal.get('contract')}|{_et_date(signal.get('flagged_at'))}"


def oi_status(signal, oi_map, nextday=None, today_et=None):
    """Per-signal NEXT-DAY OI status.

    A signal flagged on ET day D recorded the contract's OI (as of D's open —
    OCC publishes OI overnight) and its flag-day volume. The first read
    taken on the first weekday after D shows OI after D's trading; that one
    read is frozen in `nextday` ({contract|D: {"oi", "read"}}) and graded.
    Reading the chain again weeks later would measure something else (the
    contract's whole later life), so a signal whose first read came late is
    graded but marked basis "late" and kept out of the next-day stat.
    confirmed / weak / closed / pending."""
    pending = {"status": "pending", "oi_change": None, "retained_pct": None}
    if signal.get("open_interest") is None or signal.get("volume") is None:
        return pending
    try:
        fd = datetime.strptime(_et_date(signal["flagged_at"]), "%Y-%m-%d").date()
    except Exception:
        return pending
    if today_et is None:
        today_et = datetime.strptime(_et_date(datetime.now(UTC).isoformat()), "%Y-%m-%d").date()
    if (today_et - fd).days < 1:
        return pending
    key = _oi_key(signal)
    rec = nextday.get(key) if nextday is not None else None
    if rec is None:
        cur = oi_map.get(signal["contract"])
        if cur is None:
            return pending
        rec = {"oi": cur, "read": today_et.isoformat()}
        if nextday is not None:
            nextday[key] = rec
    change = rec["oi"] - signal["open_interest"]
    basis = "next_day" if rec["read"] == _next_weekday(fd).isoformat() else "late"
    return {"oi_change": change, "basis": basis, **_oi_grade(change, signal["volume"])}


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
    vals = [
        r[horizon]["ret"] for r in returns_list if r.get(horizon) and r[horizon]["ret"] is not None
    ]
    exc = [
        r[horizon]["excess"]
        for r in returns_list
        if r.get(horizon) and r[horizon].get("excess") is not None
    ]
    if not vals:
        return {"n": 0, "hit_rate": None, "avg": None, "median": None, "avg_excess": None}
    return {
        "n": len(vals),
        "hit_rate": round(100 * sum(1 for v in vals if v > 0) / len(vals)),
        "avg": round(mean(vals), 2),
        "median": round(median(vals), 2),
        "avg_excess": round(mean(exc), 2) if exc else None,
    }


def _group(scored):
    """Per-horizon edge stats for a list of scored signals — DIRECTION-
    ADJUSTED (bearish flow flipped) over directional signals only. A put
    buyer is right when the stock falls; grading it on the raw stock
    return counted every correct bearish call as a loss."""
    rets = [r for r in (_signed_returns(s) for s in scored) if r is not None]
    return {str(h): _agg(rets, h) for h in HORIZONS}


def _excursion_avg(scored):
    """Average favourable / adverse 20-session excursion in the flow's
    direction (MFE = the best move the stock offered in the flow's
    direction; MAE = the worst move against it). Only fully-matured
    20-session windows count — partial windows understate both."""
    ex = [e for e in (_signed_excursion(s) for s in scored) if e is not None]
    return {
        "avg_mfe": round(mean(e[0] for e in ex), 1) if ex else None,
        "avg_mae": round(mean(e[1] for e in ex), 1) if ex else None,
        "n": len(ex),
    }


def _alpha_confidence(items):
    """Single 0-100 score per cohort answering "is this signal pattern
    statistically worth trading?" Combines four discipline-checks:

        sample factor    = min(1, N / 30)        -- N=30 is "enough"
        direction factor = (hit_rate - 50) / 50  -- 50% is coin-flip
        alpha factor     = avg_excess / 5        -- 5%+ excess = max credit
        adverse cap      = penalty if MAE swamps MFE

    Score = 100 * sample * (0.4 * direction + 0.5 * alpha + 0.1 * mfe_mae)
    Returns 0 if hit rate < 50 OR avg excess <= 0 (cohort is anti-alpha).
    Uses the 5-day horizon as the canonical window — long enough to be
    meaningful, short enough to be measurable. Falls back to 3d if 5d
    is empty, then 1d, so live-data cohorts still get a score.
    """
    # Pick the deepest horizon that actually has data for this cohort.
    # Direction-adjusted, directional signals only (see _group).
    rets = [r for r in (_signed_returns(s) for s in items) if r is not None]
    horizon = None
    for h in (5, 3, 1):
        if any(r.get(h, {}).get("ret") is not None for r in rets):
            horizon = h
            break
    if horizon is None:
        return {"score": None, "horizon": None, "n": 0, "verdict": "no data", "tradeable": False}
    a = _agg(rets, horizon)
    n = a["n"] or 0
    if n < 5:
        return {
            "score": None,
            "horizon": horizon,
            "n": n,
            "verdict": "insufficient sample",
            "tradeable": False,
        }
    hit = a["hit_rate"] or 0
    avgx = a["avg_excess"] if a["avg_excess"] is not None else 0.0
    # Excursion penalty: cohorts where MAE swamps MFE get capped
    # (direction-adjusted, matured 20-session windows only)
    ex = [e for e in (_signed_excursion(s) for s in items) if e is not None]
    mfe = sum(e[0] for e in ex) / len(ex) if ex else 0
    mae = sum(e[1] for e in ex) / len(ex) if ex else 0
    mfe_mae = max(0.0, min(1.0, (mfe + mae) / max(1.0, mfe)))
    sample_f = min(1.0, n / 30.0)
    dir_f = max(0.0, (hit - 50.0) / 50.0)
    alpha_f = max(0.0, min(1.0, avgx / 5.0))
    if hit < 50 or avgx <= 0:
        score = 0
        verdict = "anti-alpha" if hit < 50 else "neutral"
    else:
        raw = 100 * sample_f * (0.4 * dir_f + 0.5 * alpha_f + 0.1 * mfe_mae)
        score = round(max(0, min(100, raw)))
        if score >= 60:
            verdict = "high alpha"
        elif score >= 40:
            verdict = "moderate alpha"
        elif score >= 20:
            verdict = "low alpha"
        else:
            verdict = "marginal"
    return {
        "score": score,
        "horizon": horizon,
        "n": n,
        "hit_rate": hit,
        "avg_excess": avgx,
        "verdict": verdict,
        "tradeable": (n >= 10 and hit > 50 and avgx > 0),
    }


def _oi_summary(items):
    """Next-day OI-confirmation rate for a group of scored signals. This is
    an INTERIM quality read — available the day after the flag, long before
    price returns mature — so the Performance tab has something real to show
    while the +5d window is still filling."""
    oc = _nextday_statuses(items)
    confirmed = sum(1 for x in oc if x == "confirmed")
    return {
        "checked": len(oc),
        "confirmed": confirmed,
        "confirm_rate": round(100 * confirmed / len(oc)) if oc else None,
    }


def _nextday_statuses(items):
    """OI statuses that were actually read on the NEXT day. Statuses from
    late reads (and pre-2026-11 cached ones, which re-read the chain at any
    age) are excluded so 'next-day OI held' means exactly that."""
    return [
        s["oi"]["status"]
        for s in items
        if s.get("oi")
        and s["oi"].get("status") not in (None, "pending")
        and s["oi"].get("basis") == "next_day"
    ]


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, separators=(",", ":"))
    except Exception as e:
        print(f"  write {os.path.basename(path)} failed (non-fatal): {e}")


def _today_et():
    return datetime.strptime(_et_date(datetime.now(UTC).isoformat()), "%Y-%m-%d").date()


def compute_edge(rebuild=False):
    """Score the ledger; build aggregates + per-signal scorecards. Signals
    older than FINAL_AGE_DAYS are frozen in uoa_alpha_cache.json — every
    horizon has matured, so they're scored once and never re-fetched. Only
    the rolling active window is recomputed, and its bar/OI pulls fan out
    across a thread pool.

    rebuild=True re-scores the frozen signals' returns + excursions from
    fresh split-adjusted bars (fixes values frozen before the price-basis
    guard existed). Their frozen OI reads are kept — history can't be
    re-read. Returns (edge, scored) where `scored` is every ledger row;
    the published aggregates use unique_alerts(scored)."""
    ledger = load_ledger()
    print(f"  Ledger: {len(ledger)} rows")
    if not ledger:
        return _empty_edge(), []

    today = datetime.now(UTC).date()
    today_et = _today_et()
    cache = _load_alpha_cache()

    # cached-final signals reuse their frozen score; the rest recompute
    scored, fresh = [], []
    for s in ledger:
        c = cache.get(s.get("id") or "")
        if c and _is_final(s, today) and not rebuild:
            s2 = dict(s)
            s2["returns"] = _restore_returns(c.get("returns"))
            s2["excursion"] = c.get("excursion") or {"mfe": None, "mae": None}
            s2["oi"] = c.get("oi") or _pending_oi()
            s2["basis_suspect"] = bool(c.get("basis_suspect"))
            scored.append(s2)
        else:
            if c and c.get("oi"):
                s = dict(s)
                s["oi"] = c["oi"]
            fresh.append(s)
    print(f"  {len(scored)} final (cached), {len(fresh)} to score...")

    # forward returns + excursions for the active window — daily bars and
    # split history fetched concurrently, one call each per unique ticker
    if fresh:
        oldest = min(_et_date(s.get("flagged_at")) for s in fresh)
        try:
            span = (today - datetime.strptime(oldest, "%Y-%m-%d").date()).days + 45
        except ValueError:
            span = 0
        days = max(160, span)
        spy_closes = {d: b["c"] for d, b in _bars("SPY", days).items()}
        tks = sorted({s.get("ticker", "") for s in fresh if s.get("ticker")})
        bar_cache, split_cache = {}, {}

        def _splits(t):
            try:
                return pg.splits(t, limit=50)
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for tk, bars in ex.map(lambda t: (t, _bars(t, days)), tks):
                bar_cache[tk] = bars
            for tk, sp in ex.map(lambda t: (t, _splits(t)), tks):
                split_cache[tk] = sp
        suspect = 0
        for s in fresh:
            tk = s.get("ticker", "")
            bars = bar_cache.get(tk, {})
            sfac = split_factor(
                split_cache.get(tk), _et_date(s.get("flagged_at")), today.isoformat()
            )
            s2 = dict(s)
            base, _ = _base_index(s, sorted(bars))
            s2["basis_suspect"] = bool(base) and not _flag_price(s, bars, base, sfac)[1]
            suspect += s2["basis_suspect"]
            s2["returns"] = forward_returns(s, bars, spy_closes, sfac)
            s2["excursion"] = excursions(s, bars, sfac)
            scored.append(s2)
        if suspect:
            print(f"  price-basis guard: {suspect} signals excluded (flag price vs bars mismatch)")

    # next-day OI status — current option chain fetched concurrently, only
    # for active-window tickers with a confirmable signal. The first read on
    # the next weekday is frozen in OI_NEXTDAY_PATH.
    def _age_et(s):
        try:
            fd = datetime.strptime(_et_date(s["flagged_at"]), "%Y-%m-%d").date()
        except Exception:
            return None
        return (today_et - fd).days

    def _confirmable(s):
        if s.get("open_interest") is None or s.get("volume") is None:
            return False
        a = _age_et(s)
        return a is not None and a >= 1

    oi_tickers = sorted({s["ticker"] for s in fresh if s.get("ticker") and _confirmable(s)})
    oi_cache = {}
    if oi_tickers:
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for tk, oi in ex.map(lambda t: (t, _oi_now(t)), oi_tickers):
                oi_cache[tk] = oi
    nextday = _load_json(OI_NEXTDAY_PATH, {})
    for s in scored:
        if "oi" in s:  # cached-final already carries it
            continue
        s["oi"] = (
            oi_status(s, oi_cache.get(s["ticker"], {}), nextday, today_et)
            if _confirmable(s)
            else _pending_oi()
        )
    keep_from = (today_et - timedelta(days=FINAL_AGE_DAYS + 10)).isoformat()
    nextday = {k: v for k, v in nextday.items() if k.rsplit("|", 1)[-1] >= keep_from}
    _save_json(OI_NEXTDAY_PATH, nextday)

    # freeze newly-final signals so future runs skip them entirely
    new_final = 0
    for s in scored:
        sid = s.get("id")
        if sid and (rebuild or sid not in cache) and _is_final(s, today):
            cache[sid] = {
                "returns": s["returns"],
                "excursion": s["excursion"],
                "oi": s["oi"],
                "basis_suspect": bool(s.get("basis_suspect")),
            }
            new_final += 1
    if new_final:
        _save_alpha_cache(cache)
        print(f"  Froze {new_final} newly-final signals into the cache")

    # ── Every published aggregate below is computed over UNIQUE alerts
    #    (one per contract per ET day), direction-adjusted. `scored` (all
    #    ledger rows) is still returned for research consumers.
    ledger_rows = len(scored)
    all_rows = scored
    scored = unique_alerts(all_rows)
    graded = [s for s in scored if _signed_returns(s) is not None]
    matured_5d = sum(1 for s in graded if s["returns"].get(5))
    basis_excluded = sum(1 for s in scored if _dir_sign(s) is not None and not _basis_ok(s))

    # Each group carries per-horizon return stats (h) AND an OI-confirmation
    # summary (oi) — the latter is meaningful before any price horizon matures.
    # alpha_confidence is the single sortable "trade this?" score that ties
    # every cohort table together. All slices now carry it.
    by_type = {}
    for typ in SIGNAL_TYPES:
        items = [s for s in scored if s.get("signal_type") == typ]
        if items:
            by_type[typ] = {
                "signals": len(items),
                "h": _group(items),
                "oi": _oi_summary(items),
                "rich": _rich(items),
                "excursion": _excursion_avg(items),
                "alpha_confidence": _alpha_confidence(items),
            }

    by_score = {}
    for label, lo, hi in SCORE_BUCKETS:
        items = [s for s in scored if lo <= (s.get("trade_score") or 0) < hi]
        if items:
            by_score[label] = {
                "signals": len(items),
                "h": _group(items),
                "oi": _oi_summary(items),
                "rich": _rich(items),
                "excursion": _excursion_avg(items),
                "alpha_confidence": _alpha_confidence(items),
            }

    by_dte = {}
    for b in DTE_BUCKETS:
        items = [s for s in scored if _dte_bucket(s.get("dte")) == b]
        if items:
            by_dte[b] = {
                "signals": len(items),
                "h": _group(items),
                "oi": _oi_summary(items),
                "rich": _rich(items),
                "excursion": _excursion_avg(items),
                "alpha_confidence": _alpha_confidence(items),
            }

    by_tag = {}
    for tag in ATTRIB_TAGS:
        items = [s for s in scored if tag in (s.get("tags") or [])]
        if items:
            by_tag[tag] = {
                "signals": len(items),
                "h": _group(items),
                "oi": _oi_summary(items),
                "rich": _rich(items),
                "excursion": _excursion_avg(items),
                "alpha_confidence": _alpha_confidence(items),
            }

    # ── Per-ticker and per-theme cohorts — power the dashboard's
    #    "hone in on what actually works" filtering. Only emit cohorts
    #    with N >= MIN_COHORT_N so we don't surface 2-signal noise.
    MIN_COHORT_N = 5
    by_ticker = {}
    tk_groups = {}
    for s in scored:
        tk = s.get("ticker")
        if tk:
            tk_groups.setdefault(tk, []).append(s)
    for tk, items in tk_groups.items():
        if len(items) < MIN_COHORT_N:
            continue
        by_ticker[tk] = {
            "signals": len(items),
            "h": _group(items),
            "oi": _oi_summary(items),
            "excursion": _excursion_avg(items),
            "alpha_confidence": _alpha_confidence(items),
        }

    # Theme attribution joins ticker -> themes via the curated taxonomy.
    by_theme = {}
    try:
        import themes

        theme_groups = {}
        for s in scored:
            tk = s.get("ticker")
            if not tk:
                continue
            for th in themes.themes_for(tk):
                theme_groups.setdefault(th, []).append(s)
        for th, items in theme_groups.items():
            if len(items) < MIN_COHORT_N:
                continue
            by_theme[th] = {
                "signals": len(items),
                "h": _group(items),
                "oi": _oi_summary(items),
                "excursion": _excursion_avg(items),
                "alpha_confidence": _alpha_confidence(items),
            }
    except Exception as e:
        print(f"  by_theme aggregation skipped: {e}")

    # Direction split — EV by trade side, +5d excess vs SPY in the side's
    # own direction: calls bought earn +excess, puts bought earn -excess,
    # premium sellers are graded on their lean (put sellers bullish, call
    # sellers bearish). Only prints with a classified aggressor side count —
    # "mixed"/"unknown" prints were previously lumped into the BUY books by
    # contract type, which mixed non-buyers into the call/put-buying EV.
    direction_split = {}
    try:
        side_rows = {}
        unclassified = 0
        for s in scored:
            r5 = (s.get("returns") or {}).get(5) or (s.get("returns") or {}).get("5")
            exc = (r5 or {}).get("excess")
            if exc is None or not _basis_ok(s):
                continue
            book = _BOOKS.get(s.get("flow_side") or "")
            if book is None:
                unclassified += 1
                continue
            side, sign = book
            side_rows.setdefault(side, []).append(sign * exc)
        for side, v in side_rows.items():
            if len(v) < 30:
                direction_split[side] = {"status": "accruing", "n": len(v)}
                continue
            wins = sum(1 for x in v if x > 0)
            direction_split[side] = {
                "status": "active",
                "n": len(v),
                "ev": round(sum(v) / len(v), 2),
                "hit": round(100 * wins / len(v)),
            }
        direction_split["unclassified_n"] = unclassified
    except Exception as e:
        print(f"  direction_split skipped: {e}")

    oc = _nextday_statuses(scored)
    # Find the oldest signal so the client can compute "expected mature
    # on YYYY-MM-DD" for horizons that haven't accumulated samples yet
    # (e.g. 10d / 20d when the scanner has only been running a few weeks).
    # Without this, the admin panel says "needs more backfill" without
    # any concrete date the user can plan against.
    flagged_ts = [s.get("flagged_at") for s in scored if s.get("flagged_at")]
    oldest_signal_ts = min(flagged_ts) if flagged_ts else None
    edge = {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "total_signals": len(scored),
        "ledger_rows": ledger_rows,
        "graded_signals": len(graded),
        "basis_excluded": basis_excluded,
        "matured_5d": matured_5d,
        "oldest_signal_ts": oldest_signal_ts,
        "horizons": HORIZONS,
        "overall": _group(scored),
        "rich_overall": {str(h): _rich(scored, h) for h in (1, 3, 5, 10, 20)},
        "ic": _ic_suite(scored),
        "by_regime": _by_regime(scored),
        "calibration": _calibration(scored),
        "lifecycle": _lifecycle(scored, oi_cache, today_et),
        "excursion": _excursion_avg(scored),
        "by_type": by_type,
        "by_score": by_score,
        "by_dte": by_dte,
        "by_tag": by_tag,
        "by_ticker": by_ticker,
        "by_theme": by_theme,
        "direction_split": direction_split,
        "oi_confirmation": {
            "checked": len(oc),
            "confirmed": sum(1 for x in oc if x == "confirmed"),
            "weak": sum(1 for x in oc if x == "weak"),
            "closed": sum(1 for x in oc if x == "closed"),
            "confirm_rate": round(100 * sum(1 for x in oc if x == "confirmed") / len(oc))
            if oc
            else None,
            "basis": "first read on the weekday after the flag",
        },
        "method": METHOD_NOTE,
    }
    return edge, all_rows


def _empty_edge():
    return {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "total_signals": 0,
        "matured_5d": 0,
        "horizons": HORIZONS,
        "overall": {str(h): _agg([], h) for h in HORIZONS},
        "excursion": {"avg_mfe": None, "avg_mae": None, "n": 0},
        "by_type": {},
        "by_score": {},
        "by_dte": {},
        "by_tag": {},
        "by_ticker": {},
        "by_theme": {},
        "oi_confirmation": {
            "checked": 0,
            "confirmed": 0,
            "weak": 0,
            "closed": 0,
            "confirm_rate": None,
        },
        "method": METHOD_NOTE,
    }


# ─── EV-first decision-support stats (Flow redesign) ─────────────────────────
# Everything below is computed from the matured ledger — no estimates, no
# hand-set numbers. Cohorts below their sample gate publish {"status":
# "accruing"} instead of thin statistics.


def _r2(v):
    return round(v, 2)


def _cluster_se(pairs):
    """Cluster-robust (CR1) standard error of a mean, clustering on the
    first element of each (cluster, value) pair. Alerts flagged on the same
    day ride the same market move — treating them as independent trades
    shrinks the CI by ~sqrt(alerts per day). None with < 2 clusters."""
    n = len(pairs)
    if n < 2:
        return None
    mu = sum(v for _, v in pairs) / n
    resid = {}
    for k, v in pairs:
        resid[k] = resid.get(k, 0.0) + (v - mu)
    g = len(resid)
    if g < 2:
        return None
    var = sum(r * r for r in resid.values()) / (n * n) * g / (g - 1)
    return sqrt(var)


def _rich(items, horizon=5):
    """Expected-value-first stats for one cohort at one horizon, with
    uncertainty. Hit rate alone is misleading — a 49% hit rate with big
    winners and small losers is a positive-EV system. Uses EXCESS return
    vs SPY so beta doesn't masquerade as edge, DIRECTION-ADJUSTED (a put
    buyer earns the stock's negative excess), over directional signals
    only. The 95% CI is clustered by flag date."""
    pairs = []
    for s in items:
        r = _signed_returns(s)
        v = ((r or {}).get(horizon) or {}).get("excess")
        if v is not None:
            pairs.append((_et_date(s.get("flagged_at")), v))
    xs = [v for _, v in pairs]
    n = len(xs)
    if n < 20:
        return {"n": n, "status": "accruing"}
    srt = sorted(xs)
    q = lambda p: srt[min(n - 1, int(p * n))]
    wins = [v for v in xs if v > 0]
    losses = [v for v in xs if v <= 0]
    avg = mean(xs)
    sd = pstdev(xs) if n > 1 else 0.0
    se = _cluster_se(pairs)
    days = len({k for k, _ in pairs})
    gross_w = sum(wins)
    gross_l = abs(sum(losses))
    sig = abs(avg) > 1.96 * se if se else False
    return {
        "n": n,
        "days": days,  # independent flag dates behind the CI
        "status": "active",
        "ev": _r2(avg),  # expected excess per alert (%), stock terms
        "median": _r2(q(0.5)),
        "std": _r2(sd),
        "win_rate": round(100 * len(wins) / n),
        "avg_win": _r2(mean(wins)) if wins else None,
        "avg_loss": _r2(mean(losses)) if losses else None,
        "profit_factor": _r2(gross_w / gross_l) if gross_l else None,
        "ci95": [_r2(avg - 1.96 * se), _r2(avg + 1.96 * se)] if se else None,
        "p25": _r2(q(0.25)),
        "p75": _r2(q(0.75)),
        "p90": _r2(q(0.90)),
        "ir": _r2(avg / sd) if sd else None,  # per-alert information ratio
        "significant": sig,
        # independent-days + significance confidence badge
        "confidence": ("high" if days >= 60 and sig else "medium" if days >= 20 else "low"),
    }


def _spearman(pairs):
    """Spearman rank correlation of (score, outcome) pairs. Crude ranks
    (ties broken by order) — fine at these sample sizes."""
    n = len(pairs)
    if n < 50:
        return None

    def ranks(vals):
        order = sorted(range(n), key=lambda i: vals[i])
        r = [0] * n
        for rank, i in enumerate(order):
            r[i] = rank
        return r

    a = ranks([p[0] for p in pairs])
    b = ranks([p[1] for p in pairs])
    ma, mb = mean(a), mean(b)
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = sqrt(sum((v - ma) ** 2 for v in a))
    db = sqrt(sum((v - mb) ** 2 for v in b))
    return round(num / (da * db), 3) if da and db else None


def _ic_suite(scored):
    """Institutional ranking quality: does a higher trade_score actually
    predict a better +5d excess return IN THE FLOW'S DIRECTION? IC ~0 = the
    score ranks noise. trade_score is the displayed score (it includes the
    nightly learned adjustment)."""
    pairs = []
    for s in scored:
        sc = s.get("trade_score")
        x = (((_signed_returns(s) or {}).get(5)) or {}).get("excess")
        if sc is not None and x is not None:
            pairs.append((sc, x, s.get("flagged_at") or ""))
    base = [(p[0], p[1]) for p in pairs]
    now = datetime.now(UTC)

    def window(days):
        cut = (now - timedelta(days=days)).isoformat()
        return _spearman([(p[0], p[1]) for p in pairs if p[2] >= cut])

    return {
        "n": len(base),
        "ic_spearman": _spearman(base),
        "ic_30d": window(30),
        "ic_90d": window(90),
        "note": (
            "Spearman rank IC of the displayed trade_score (incl. learned "
            "adjustment) vs direction-adjusted +5d excess, one row per "
            "alert. For daily signals, |IC| >= 0.05 is meaningful; the "
            "score_deciles block carries the top/bottom decile spread."
        ),
    }


def _by_regime(scored):
    """Cohort stats sliced by the market regime on the FLAG day (labels
    from regime_history.json — loop 7's dataset). History only reaches
    back to when regime logging started, so most cells accrue honestly."""
    try:
        with open(
            os.path.join(_BASE, "docs", "reports", "regime_history.json"), encoding="utf-8"
        ) as f:
            hist = json.load(f)
        lab = {d.get("date"): d.get("label") for d in hist.get("days") or [] if d.get("date")}
    except Exception:
        return {}
    groups = {}
    for s in scored:
        d = (s.get("flagged_at") or "")[:10]
        lb = lab.get(d)
        if lb:
            groups.setdefault(lb, []).append(s)
    out = {}
    for lb, items in groups.items():
        r = _rich(items)
        out[lb] = r if r.get("n", 0) >= 200 else {"n": r.get("n", 0), "status": "accruing"}
    return out


def _calibration(scored):
    """Time-split calibration per score band: 'predicted' = the band's
    win rate on signals older than 45 days (training window), 'observed'
    = the last 45 days. Drift = observed - predicted. A well-calibrated
    band drifts near zero; big drift = the band's meaning has changed."""
    cut = (datetime.now(UTC) - timedelta(days=45)).isoformat()
    out = {}
    for label, lo, hi in SCORE_BUCKETS:
        items = [s for s in scored if lo <= (s.get("trade_score") or 0) < hi]

        def wr(sub):
            # direction-adjusted +5d excess win rate, directional alerts only
            xs = [(((_signed_returns(s) or {}).get(5)) or {}).get("excess") for s in sub]
            xs = [v for v in xs if v is not None]
            return (
                (round(100 * sum(1 for v in xs if v > 0) / len(xs)), len(xs)) if xs else (None, 0)
            )

        pred, n_tr = wr([s for s in items if (s.get("flagged_at") or "") < cut])
        obs, n_lv = wr([s for s in items if (s.get("flagged_at") or "") >= cut])
        if n_tr >= 200 and n_lv >= 50 and pred is not None and obs is not None:
            out[label] = {
                "predicted": pred,
                "observed": obs,
                "drift": obs - pred,
                "n_train": n_tr,
                "n_live": n_lv,
            }
        else:
            out[label] = {"status": "accruing", "n_train": n_tr, "n_live": n_lv}
    return out


LIFECYCLE_PATH = os.path.join(_BASE, "data", "oi_lifecycle.json")
LIFECYCLE_KEEP_DAYS = 30  # alerts tracked this long (calendar days)


def _lifecycle(alerts, oi_now, today_et):
    """Position-lifecycle accrual: each run snapshots whether a young
    alert's contract OI is STILL elevated (OI now - flag-day OI > 15% of the
    flag-day volume) at its current calendar-day offset, using the CURRENT
    chain (oi_now: {ticker: {contract: oi}}). Keyed per alert
    (contract|ET flag date); the first snapshot at each offset is kept.
    Entries age out by flag date — the old count cap evicted by insertion
    order, which thrashed the sample. Gates at n>=200 per offset."""
    log = _load_json(LIFECYCLE_PATH, {})
    if not isinstance(log, dict):
        log = {}
    for s in alerts:
        if s.get("open_interest") is None:
            continue
        cur = (oi_now.get(s.get("ticker")) or {}).get(s.get("contract"))
        if cur is None:
            continue
        try:
            fd = datetime.strptime(_et_date(s["flagged_at"]), "%Y-%m-%d").date()
        except Exception:
            continue
        off = (today_et - fd).days
        if off < 1 or off > 12:
            continue
        vol = s.get("day_volume") or s.get("volume") or 1
        e = log.setdefault(_oi_key(s), {})
        k = str(off)
        if k not in e:
            e[k] = 1 if (cur - s["open_interest"]) > 0.15 * vol else 0
    keep_from = (today_et - timedelta(days=LIFECYCLE_KEEP_DAYS)).isoformat()
    # keys are contract|YYYY-MM-DD; legacy id-keyed rows (no '|') drop out
    log = {k: v for k, v in log.items() if "|" in k and k.rsplit("|", 1)[1] >= keep_from}
    _save_json(LIFECYCLE_PATH, log)
    out = {}
    for off in ("1", "2", "3", "5", "10"):
        vals = [e[off] for e in log.values() if off in e]
        out[off] = (
            {"n": len(vals), "held_rate": round(100 * sum(vals) / len(vals))}
            if len(vals) >= 200
            else {"n": len(vals), "status": "accruing"}
        )
    out["note"] = (
        "Share of alerts whose contract OI was still above the flag-day "
        "level by >15% of the flag-day volume N calendar days after the "
        "flag, one row per alert, last 30 days. Publishes per offset at "
        "n>=200."
    )
    return out


def _emit_scored(scored):
    """Per-alert scorecard JSON for the dashboard's Tracked-Signals view.
    `scored` should be unique_alerts(...) — one row per contract per ET day.
    ret_* are the STOCK's own move (factual); `dir` (+1 bullish / -1
    bearish / 0 no direction) lets the site colour them by whether the
    stock moved the flow's way. mfe/mae are direction-adjusted (best / worst
    stock move in the flow's direction, window so far) and null for flow
    with no direction. A price-basis-suspect alert publishes no returns."""
    rows = []
    for s in scored:
        ok = _basis_ok(s)
        ret = (s.get("returns") or {}) if ok else {}
        r1, r3, r5 = ret.get(1) or {}, ret.get(3) or {}, ret.get(5) or {}
        sexc = _signed_excursion(s, require_full=False) if ok else None
        oi = s.get("oi") or {}
        # contract type + strike from the OCC ticker — works for every signal,
        # including older ones whose ledger row predates the `type` field.
        occ_type, strike = _parse_occ(s.get("contract"))
        spot = s.get("underlying_px_at_flag")
        is_otm = None
        if occ_type and strike and spot:
            is_otm = (strike >= spot) if occ_type == "call" else (strike <= spot)
        rows.append(
            {
                "id": s.get("id"),
                "flagged_at": s.get("flagged_at"),
                "ticker": s.get("ticker"),
                "contract": s.get("contract"),
                "signal_type": s.get("signal_type"),
                "trade_score": s.get("trade_score"),
                "premium": s.get("premium"),
                "dte": s.get("dte"),
                "tags": s.get("tags", []),
                # pass-through fields so the Tracked-Signals tab can offer the
                # same filters as Live Flow. `type` falls back to the OCC ticker
                # so the call/put filter works on pre-schema-expansion signals.
                "type": s.get("type") or occ_type,
                "is_otm": is_otm,
                "cap_bucket": s.get("cap_bucket"),
                "sector": s.get("sector"),
                "themes": s.get("themes", []),
                "opening": s.get("opening"),
                "liquidity": s.get("liquidity"),
                "flow_side": s.get("flow_side"),
                "ret_1d": r1.get("ret"),
                "ret_3d": r3.get("ret"),
                "ret_5d": r5.get("ret"),
                "excess_5d": r5.get("excess"),
                "dir": _dir_sign(s) or 0,
                "mfe": sexc[0] if sexc else None,
                "mae": sexc[1] if sexc else None,
                "relogs": s.get("relogs", 1),
                "oi_status": oi.get("status", "pending"),
                "oi_basis": oi.get("basis"),
                "oi_change": oi.get("oi_change"),
                "retained_pct": oi.get("retained_pct"),
            }
        )
    rows.sort(key=lambda r: r.get("flagged_at") or "", reverse=True)
    # Cap the SITE copy to the most recent signals + write COMPACT (no
    # indent). The full ledger stays in data/uoa_signals.jsonl and the edge
    # aggregates in uoa_edge.json are computed on ALL signals, so this only
    # trims the per-signal "Tracked Signals" list — while keeping the file
    # small enough that GitHub Pages can actually deploy it. (The old
    # indent=1 full dump ballooned to ~42MB and timed out the Pages deploy.)
    # 15000 rows made an 8.6MB payload whose parse + retained heap slowed
    # whole browsers on the Flow tab; the Tracked view renders at most 400
    # rows post-filter. Rows are unique alerts (re-logs collapsed), so 4000
    # covers roughly the last 2-3 weeks — enough for +5d returns to show
    # (raw re-logged rows covered only ~2 days, so +5d was always blank).
    SITE_MAX = 4000
    total = len(rows)
    rows = rows[:SITE_MAX]
    payload = {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "count": len(rows),
        "total": total,
        "signals": rows,
    }
    with open(SCORED_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))


# ── Learning loop: outcome-derived edge weights ──────────────────────────
# Turns the matured-outcome ledger into a small, bounded feedback signal the
# scanner consumes on its NEXT run. Walk-forward by construction: a signal's
# +5d outcome only exists ≥5 sessions after it was scored, so today's weights
# never see today's signals. Guardrails, in order of importance:
#   • min-N gate (EW_MIN_N) — features without a real sample emit nothing
#   • shrinkage toward the global prior (EW_SHRINK_K pseudo-observations) —
#     small cohorts barely move, huge cohorts converge to their true rate
#   • per-feature clamp (±EW_PER_MAX pts) and scanner-side total clamp —
#     learning can nudge a rank, never flip the board
#   • versioned output with previous-version adjs — every change observable,
#     rollback = git revert of one small JSON
EW_PATH = os.path.join(_BASE, "data", "edge_weights.json")
EW_SITE_PATH = os.path.join(_BASE, "docs", "reports", "edge_weights.json")
EW_MIN_N = 200  # min matured signals before a feature may emit
EW_SHRINK_K = 200  # pseudo-observations pulled toward the prior
EW_SCALE = 80  # 5% shrunk lift -> 4.0 score points
EW_PER_MAX = 4.0  # per-feature clamp (points)
# Signal-decay diagnostic: recent window vs prior window hit rates per
# feature. DISPLAY-ONLY for now — weights still learn from the full sample;
# decay is the early-warning that an edge is dying (and the evidence needed
# before ever switching the weights themselves to a recency window).
EW_DECAY_RECENT_D = 30
EW_DECAY_PRIOR_D = 90  # prior window = (recent, 90] days back
EW_DECAY_MIN_N = 50  # per window, per feature
# Regime-conditioned weights: GATED. Emitted only when enough labeled days
# from regime_history.json join matured outcomes — activates automatically
# as the (new) regime dataset accrues. Until then the file carries an
# honest {"status":"accruing"} and the scanner uses the global set.
REGIME_HISTORY_PATH = os.path.join(_BASE, "docs", "reports", "regime_history.json")
EW_REGIME_MIN_DAYS = 40  # labeled trading days required per activation
EW_REGIME_MIN_N = 150  # graded signals required per regime cohort


def _ew_decay(graded, now_utc):
    """Per-feature hit rate: last EW_DECAY_RECENT_D days vs the prior window.
    Only features with >= EW_DECAY_MIN_N graded signals in BOTH windows emit
    (a delta computed on thin windows is noise, not decay)."""
    recent, prior = {}, {}
    for s, w in graded:
        try:
            d = (now_utc - datetime.fromisoformat(s["flagged_at"].replace("Z", "+00:00"))).days
        except Exception:
            continue
        bucket = recent if d <= EW_DECAY_RECENT_D else (prior if d <= EW_DECAY_PRIOR_D else None)
        if bucket is None:
            continue
        for f in _ew_features(s):
            st = bucket.setdefault(f, [0, 0])
            st[0] += 1
            st[1] += 1 if w else 0
    out = {}
    for f in set(recent) & set(prior):
        rn, rw = recent[f]
        pn, pw = prior[f]
        if rn < EW_DECAY_MIN_N or pn < EW_DECAY_MIN_N:
            continue
        rh, ph = rw / rn, pw / pn
        out[f] = {
            "recent_n": rn,
            "recent_hit": round(rh, 3),
            "prior_n": pn,
            "prior_hit": round(ph, 3),
            "delta": round(rh - ph, 3),
        }
    return out


def _ew_regime_sets(graded):
    """Per-regime feature weights, or an 'accruing' status while the regime
    dataset is too young. Join: signal's flagged date -> that day's regime
    label from regime_history.json. Same shrinkage math as the global set,
    but against each regime's own prior."""
    try:
        with open(REGIME_HISTORY_PATH, encoding="utf-8") as f:
            days = json.load(f).get("days") or []
    except Exception:
        days = []
    label_by_date = {
        d.get("date"): d.get("label") for d in days if d.get("date") and d.get("label")
    }
    joined = []
    for s, w in graded:
        lbl = label_by_date.get((s.get("flagged_at") or "")[:10])
        if lbl:
            joined.append((s, w, lbl))
    labeled_days_used = len({(s.get("flagged_at") or "")[:10] for s, w, _ in joined})
    if labeled_days_used < EW_REGIME_MIN_DAYS:
        return {
            "status": "accruing",
            "labeled_days": len(label_by_date),
            "labeled_days_with_outcomes": labeled_days_used,
            "activates_at_days": EW_REGIME_MIN_DAYS,
        }
    sets = {}
    for regime in ("risk_on", "risk_off", "mixed"):
        cohort = [(s, w) for s, w, l in joined if l == regime]
        if len(cohort) < EW_REGIME_MIN_N:
            continue
        prior = sum(1 for _, w in cohort if w) / len(cohort)
        stats = {}
        for s, w in cohort:
            for f in _ew_features(s):
                st = stats.setdefault(f, [0, 0])
                st[0] += 1
                st[1] += 1 if w else 0
        feats = {}
        for key, (n, wins) in stats.items():
            if n < EW_MIN_N // 2:  # regime cohorts are smaller by nature
                continue
            shrunk = (wins + EW_SHRINK_K * prior) / (n + EW_SHRINK_K)
            adj = round(max(-EW_PER_MAX, min(EW_PER_MAX, (shrunk - prior) * EW_SCALE)), 1)
            feats[key] = {"n": n, "adj": adj}
        if feats:
            sets[regime] = {"prior": round(prior, 4), "n": len(cohort), "features": feats}
    if not sets:
        return {
            "status": "accruing",
            "labeled_days": len(label_by_date),
            "labeled_days_with_outcomes": labeled_days_used,
            "activates_at_days": EW_REGIME_MIN_DAYS,
        }
    return {"status": "active", "labeled_days_with_outcomes": labeled_days_used, "sets": sets}


def _ew_win(s):
    """Direction-aware +5d excess win, or None if not gradeable: the stock
    beat SPY in the flow's direction. Same grading as every published stat
    (_signed_returns: directional flow only, price-basis guard), so the
    learner and the track record can't disagree about what a win is."""
    exc = (((_signed_returns(s) or {}).get(5)) or {}).get("excess")
    if exc is None:
        return None
    return exc > 0


def _ew_side(s):
    """Learner side feature: seller / call_buy / put_buy / unclassified.
    Unlike _side_of_signal (kept for the desk's research consumers), prints
    with no classified aggressor side (mixed / unknown) are NOT folded into
    a buy book. uoa_scanner.edge_adjust mirrors this exactly."""
    fs = s.get("flow_side")
    if fs in ("put_seller", "call_seller"):
        return "seller"
    if fs == "call_buyer":
        return "call_buy"
    if fs == "put_buyer":
        return "put_buy"
    return "unclassified"


def _side_of_signal(s):
    """Trade-side key: seller / call_buy / put_buy. The 2026-07 alpha
    decomposition showed this is the single largest EV differentiator in
    the ledger (call_buy ≈ +1.5%, put_buy ≈ -2.6% dir-signed +5d): put
    flow in this momentum universe is dominantly hedging, not directional
    conviction. Falls back to the OCC contract letter for early ledger
    rows that predate the `type` field."""
    if s.get("flow_side") in ("put_seller", "call_seller"):
        return "seller"
    t = s.get("type")
    if not t:
        con = s.get("contract") or ""
        t = "put" if len(con) > 9 and con[-9] == "P" else "call"
    return t + "_buy"


def _ew_features(s):
    """Feature keys for one signal — must be computable at SCAN time from the
    same fields, so the scanner can mirror this exactly."""
    feats = [
        f"type:{s.get('signal_type')}",
        f"side:{_ew_side(s)}",
        f"dte:{_dte_bucket(s.get('dte'))}",
        f"cap:{s.get('cap_bucket') or 'unknown'}",
        f"liq:{s.get('liquidity') or 'C'}",
    ]
    feats += [f"tag:{t}" for t in (s.get("tags") or []) if t in ATTRIB_TAGS]
    return feats


# ── Counterfactual engine ────────────────────────────────────────────────
# "If you had taken EVERY signal in this cohort and exited +5 sessions,
# equal-weight" — the graded ledger makes this a query, not a promise.
# Honest math: per-trade EXCESS return vs SPY (already computed per signal),
# equal-weight average × N, best/worst, hit rate. No compounding fiction,
# no cherry-picked windows: every matured signal in the cohort counts.
def _counterfactuals(scored):
    def graded_rows(pred):
        out = []
        for s in scored:
            if not pred(s):
                continue
            # direction-signed: a bearish signal "earns" when the stock falls
            exc = (((_signed_returns(s) or {}).get(5)) or {}).get("excess")
            if exc is None:
                continue
            out.append(exc)
        return out

    cohorts = {
        "score_80_plus": lambda s: (s.get("trade_score") or 0) >= 80,
        "score_65_plus": lambda s: (s.get("trade_score") or 0) >= 65,
        "golden_sweeps": lambda s: s.get("signal_type") == "golden_sweep",
        "into_earnings": lambda s: "Into ERN" in (s.get("tags") or []),
        "positioning_dte": lambda s: _dte_bucket(s.get("dte")) == "positioning",
    }
    out = {}
    for name, pred in cohorts.items():
        rets = graded_rows(pred)
        if len(rets) < 30:
            continue
        wins = sum(1 for r in rets if r > 0)
        out[name] = {
            "n": len(rets),
            "hit": round(100 * wins / len(rets)),
            "avg_exc": round(sum(rets) / len(rets), 2),
            "total_units": round(sum(rets), 1),  # sum of 1-unit trades
            "best": round(max(rets), 1),
            "worst": round(min(rets), 1),
        }
    return out


# ── Stop × target counterfactual grid ────────────────────────────────────
# "What if every signal ran with a X% stop and a Y% target?" — answerable
# from the per-signal MFE/MAE already in the ledger. Frame + assumptions
# (stated in the payload, and they matter):
#   • +20-session window (that's what MFE/MAE measure), RAW underlying
#     return (stops/targets act on price, not on SPY-excess)
#   • direction-aware: for bearish signals favorable = −MAE, adverse = −MFE
#   • PESSIMISTIC ordering: when both stop and target were breached inside
#     the window, the STOP is assumed to have hit first. Daily bars can't
#     order intraweek extremes, so we take the conservative branch — real
#     results can only be better than shown.
GRID_STOPS = (5.0, 8.0, 12.0)
GRID_TARGETS = (8.0, 15.0, 25.0)
GRID_MIN_N = 500


def _stop_target_grid(scored):
    rows = []
    for s in scored:
        # direction-adjusted: for bearish flow favourable = -MAE, adverse = -MFE
        ex = _signed_excursion(s)
        fin = (((_signed_returns(s) or {}).get(20)) or {}).get("ret")
        if ex is None or fin is None:
            continue
        rows.append((ex[0], ex[1], fin, (s.get("trade_score") or 0)))
    if len(rows) < GRID_MIN_N:
        return {"status": "accruing", "n": len(rows), "activates_at": GRID_MIN_N}

    def cell(rs, stop, target):
        outs = []
        for fav, adv, fin, _ in rs:
            if adv <= -stop:
                outs.append(-stop)  # pessimistic: stop first
            elif fav >= target:
                outs.append(target)
            else:
                outs.append(fin)
        n = len(outs)
        return {
            "n": n,
            "hit": round(100 * sum(1 for r in outs if r > 0) / n),
            "avg": round(sum(outs) / n, 2),
            "stopped_pct": round(100 * sum(1 for _, a, _f, _sc in rs if a <= -stop) / n),
        }

    def table(rs):
        base_rets = [f for _, _, f, _ in rs]
        base = {
            "n": len(base_rets),
            "hit": round(100 * sum(1 for r in base_rets if r > 0) / len(base_rets)),
            "avg": round(sum(base_rets) / len(base_rets), 2),
        }
        grid = {}
        for st in GRID_STOPS:
            for tg in GRID_TARGETS:
                grid[f"{st:g}/{tg:g}"] = cell(rs, st, tg)
        return {"baseline_hold20": base, "grid": grid}

    hi = [r for r in rows if r[3] >= 80]
    out = {
        "status": "active",
        "assumptions": (
            "one row per alert, 20-session window, stock (not option) "
            "return, direction-adjusted, pessimistic ordering (stop "
            "assumed first when both levels hit; fills at the level, "
            "gaps through it not modeled)"
        ),
        "stops": list(GRID_STOPS),
        "targets": list(GRID_TARGETS),
        "all": table(rows),
    }
    if len(hi) >= GRID_MIN_N:
        out["score_80_plus"] = table(hi)
    return out


# ── Rank-quality audit: outcomes by trade-score decile ───────────────────
# The whole product publishes a ranking; this checks the ranking is real.
# Deciles of trade_score vs direction-signed +5d excess. If the top decile
# doesn't beat the bottom, the scoring needs re-examination — better we
# find out here than a customer does.
DECILE_MIN_N = 1000


def _score_deciles(scored):
    rows = []
    for s in scored:
        signed = (((_signed_returns(s) or {}).get(5)) or {}).get("excess")
        if signed is None:
            continue
        rows.append(((s.get("trade_score") or 0), signed))
    if len(rows) < DECILE_MIN_N:
        return {"status": "accruing", "n": len(rows), "activates_at": DECILE_MIN_N}
    rows.sort(key=lambda x: x[0])
    n = len(rows)
    deciles = []
    for i in range(10):
        chunk = rows[i * n // 10 : (i + 1) * n // 10]
        if not chunk:
            continue
        vals = [v for _, v in chunk]
        deciles.append(
            {
                "d": i + 1,
                "score_lo": round(chunk[0][0]),
                "score_hi": round(chunk[-1][0]),
                "n": len(chunk),
                "hit": round(100 * sum(1 for v in vals if v > 0) / len(vals)),
                "avg_exc": round(sum(vals) / len(vals), 2),
            }
        )
    top, bot = deciles[-1], deciles[0]
    mono_up = sum(1 for a, b in itertools.pairwise(deciles) if b["avg_exc"] >= a["avg_exc"])
    return {
        "status": "active",
        "n": n,
        "deciles": deciles,
        "top_minus_bottom": round(top["avg_exc"] - bot["avg_exc"], 2),
        "monotonic_steps": f"{mono_up}/{len(deciles) - 1}",
        "note": (
            "direction-signed +5d excess vs SPY by displayed trade-score "
            "(incl. learned adjustment) decile, one row per alert — the "
            "ranking's own report card"
        ),
    }


def _emit_edge_weights(scored):
    """Compute per-feature adjustments from matured directional outcomes and
    publish the versioned weights file the scanner reads next run. Pass
    unique alerts (one per contract per ET day): training on raw ledger
    rows weighted each contract by how many scans re-logged it."""
    graded = [(s, w) for s in scored for w in [_ew_win(s)] if w is not None]
    if len(graded) < EW_MIN_N * 2:
        print(f"  edge_weights: only {len(graded)} gradeable signals — skipping")
        return None
    prior = sum(1 for _, w in graded if w) / len(graded)

    stats = {}
    for s, w in graded:
        for f in _ew_features(s):
            st = stats.setdefault(f, [0, 0])  # [n, wins]
            st[0] += 1
            st[1] += 1 if w else 0

    prev = {}
    prev_version = None
    try:
        with open(EW_PATH, encoding="utf-8") as f:
            old = json.load(f)
        prev_version = old.get("version")
        prev = {k: v.get("adj") for k, v in (old.get("features") or {}).items()}
    except Exception:
        pass

    features = {}
    for key, (n, wins) in sorted(stats.items()):
        if n < EW_MIN_N:
            continue
        shrunk = (wins + EW_SHRINK_K * prior) / (n + EW_SHRINK_K)
        lift = shrunk - prior
        adj = round(max(-EW_PER_MAX, min(EW_PER_MAX, lift * EW_SCALE)), 1)
        features[key] = {
            "n": n,
            "win": round(wins / n, 3),
            "shrunk": round(shrunk, 3),
            "lift": round(lift, 3),
            "adj": adj,
        }
        if prev.get(key) is not None and abs(adj - prev[key]) >= 0.5:
            print(f"  edge_weights Δ {key}: {prev[key]:+.1f} -> {adj:+.1f}")

    now_utc = datetime.now(UTC)
    decay = _ew_decay(graded, now_utc)
    dying = {k: v for k, v in decay.items() if v["delta"] <= -0.08}
    if dying:
        print(
            "  edge decay ⚠ "
            + ", ".join(
                f"{k} {v['prior_hit']:.0%}->{v['recent_hit']:.0%}"
                for k, v in sorted(dying.items(), key=lambda x: x[1]["delta"])[:4]
            )
        )
    regimes = _ew_regime_sets(graded)
    if regimes.get("status") == "accruing":
        print(
            f"  regime weights: accruing "
            f"({regimes.get('labeled_days_with_outcomes', 0)}/"
            f"{EW_REGIME_MIN_DAYS} labeled days with outcomes)"
        )
    else:
        print(f"  regime weights ACTIVE: {list(regimes['sets'].keys())}")

    payload = {
        "version": datetime.now(UTC).isoformat(timespec="seconds"),
        "prev_version": prev_version,
        "prior": round(prior, 4),
        "graded": len(graded),
        "params": {
            "min_n": EW_MIN_N,
            "shrink_k": EW_SHRINK_K,
            "scale": EW_SCALE,
            "per_max": EW_PER_MAX,
            "decay_windows_d": [EW_DECAY_RECENT_D, EW_DECAY_PRIOR_D],
            "regime_min_days": EW_REGIME_MIN_DAYS,
        },
        "features": features,
        "decay": decay,
        "regimes": regimes,
    }
    for path in (EW_PATH, EW_SITE_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=1)
    print(
        f"  Wrote edge_weights.json — {len(features)} features from "
        f"{len(graded)} graded signals (prior {prior:.1%})"
    )
    return payload


def run(rebuild=False):
    """Compute edge stats + per-signal scorecards; publish both JSON files.
    Published stats AND the learner use unique alerts."""
    edge, scored = compute_edge(rebuild=rebuild)
    alerts = unique_alerts(scored) if scored else []
    os.makedirs(os.path.dirname(EDGE_PATH), exist_ok=True)
    # Split the heavy per-ticker/per-theme cohorts into their own file —
    # no client code reads them from the main payload (verified), yet they
    # were ~90% of a 656 KB file fetched with the Flow tab. The cohorts
    # file exists for future lazy consumption (drilldowns, Performance).
    try:
        edge["counterfactuals"] = _counterfactuals(alerts)
        for k, v in edge["counterfactuals"].items():
            print(
                f"  counterfactual {k}: n={v['n']} hit={v['hit']}% "
                f"avg {v['avg_exc']:+.2f}%/trade (dir-signed +5d excess)"
            )
    except Exception as e:
        print(f"  counterfactuals failed (non-fatal): {e}")
    try:
        edge["stop_target_grid"] = _stop_target_grid(alerts)
        g = edge["stop_target_grid"]
        if g.get("status") == "active":
            b = g["all"]["baseline_hold20"]
            print(
                f"  stop/target grid: n={b['n']} baseline hold-20d "
                f"hit {b['hit']}% avg {b['avg']:+.2f}%"
            )
    except Exception as e:
        print(f"  stop_target_grid failed (non-fatal): {e}")
    try:
        edge["score_deciles"] = _score_deciles(alerts)
        sd = edge["score_deciles"]
        if sd.get("status") == "active":
            print(
                f"  rank audit: top-bottom decile spread "
                f"{sd['top_minus_bottom']:+.2f}pp "
                f"({sd['monotonic_steps']} steps monotonic, n={sd['n']})"
            )
    except Exception as e:
        print(f"  score_deciles failed (non-fatal): {e}")
    cohorts = {
        "generated": edge.get("generated"),
        "by_ticker": edge.pop("by_ticker", {}),
        "by_theme": edge.pop("by_theme", {}),
    }
    with open(
        os.path.join(_BASE, "docs", "reports", "uoa_edge_cohorts.json"), "w", encoding="utf-8"
    ) as f:
        json.dump(cohorts, f, separators=(",", ":"))
    with open(EDGE_PATH, "w", encoding="utf-8") as f:
        json.dump(edge, f, separators=(",", ":"))
    _emit_scored(alerts)
    try:
        _emit_edge_weights(alerts)
    except Exception as e:
        print(f"  edge_weights failed (non-fatal): {e}")
    # Nightly model-health report: one small JSON answering "is the
    # ranking engine itself healthy?" — rolling IC, calibration drift,
    # feature freshness counts, ledger depth. Read by the Flow tab.
    try:
        cal = edge.get("calibration") or {}
        drifts = [
            abs(v.get("drift", 0))
            for v in cal.values()
            if isinstance(v, dict) and v.get("drift") is not None
        ]
        try:
            with open(os.path.join(_BASE, "data", "edge_weights.json"), encoding="utf-8") as f:
                dec = json.load(f).get("decay") or {}
        except Exception:
            dec = {}
        fading = sum(1 for v in dec.values() if (v.get("delta") or 0) <= -0.08)
        improving = sum(1 for v in dec.values() if (v.get("delta") or 0) >= 0.08)
        health = {
            "generated": edge["generated"],
            "ledger": {"total": edge["total_signals"], "matured_5d": edge["matured_5d"]},
            "ic": edge.get("ic"),
            "calibration_max_drift": max(drifts) if drifts else None,
            "features": {"tracked": len(dec), "fading": fading, "improving": improving},
            "note": (
                "Nightly self-check of the ranking engine. IC = rank "
                "correlation of score vs realized +5d excess; drift = "
                "per-band predicted-vs-live win-rate gap. Nothing here "
                "auto-changes production weights."
            ),
        }
        with open(
            os.path.join(_BASE, "docs", "reports", "model_health.json"), "w", encoding="utf-8"
        ) as f:
            json.dump(health, f, separators=(",", ":"))
        ic = edge.get("ic") or {}
        print(
            f"  model health: IC {ic.get('ic_spearman')} "
            f"· 30d {ic.get('ic_30d')} "
            f"· fading {fading} / improving {improving}"
        )
    except Exception as e:
        print(f"  model_health failed (non-fatal): {e}")
    o5 = edge["overall"].get("5", {})
    oc = edge["oi_confirmation"]
    print(
        f"  Wrote uoa_edge.json + uoa_signals_scored.json — "
        f"{edge['total_signals']} signals, {edge['matured_5d']} matured +5d "
        f"(+5d hit {o5.get('hit_rate')}%, avg {o5.get('avg')}%; "
        f"OI confirmed {oc.get('confirmed')}/{oc.get('checked')})"
    )
    return edge


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    run(rebuild="--rebuild" in sys.argv)
