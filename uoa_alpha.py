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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from math import sqrt
from statistics import mean, median, pstdev

import polygon_data as pg

_BASE = os.path.dirname(os.path.abspath(__file__))
# Internal ETL state (ledger + alpha cache) lives in data/ so it stays OUT of
# the public Pages artifact. EDGE/SCORED stay in docs/reports (browser-served).
LEDGER_PATH = os.path.join(_BASE, "data", "uoa_signals.jsonl")
EDGE_PATH   = os.path.join(_BASE, "docs", "reports", "uoa_edge.json")
SCORED_PATH = os.path.join(_BASE, "docs", "reports", "uoa_signals_scored.json")
ALPHA_CACHE_PATH = os.path.join(_BASE, "data", "uoa_alpha_cache.json")

FINAL_AGE_DAYS = 35   # a signal this old has every horizon matured — freeze it
WORKERS = 24          # fan out bar/OI pulls; modest — _oi_now pulls full chains

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
    except (ValueError, IndexError, TypeError):
        return None, None


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


def _pending_oi():
    return {"status": "pending", "oi_change": None, "retained_pct": None}


def _is_final(signal, today):
    """True once every forward horizon has matured (flagged >= FINAL_AGE_DAYS
    ago) — the signal's score is frozen and can be served from cache."""
    try:
        fd = datetime.strptime(signal["flagged_at"][:10], "%Y-%m-%d").date()
    except (KeyError, ValueError, TypeError):
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
        ret = (bars[dates[idx + h]]["c"] / p0 - 1) * 100
        excess = exc_c = None
        if spy0 and spy_idx is not None and spy_idx + h < len(spy_dates):
            spy_ret = (spy_closes[spy_dates[spy_idx + h]] / spy0 - 1) * 100
            excess = ret - spy_ret
            if p0c:
                exc_c = (bars[dates[idx + h]]["c"] / p0c - 1) * 100 - spy_ret
        out[h] = {"ret": round(ret, 2),
                  "excess": round(excess, 2) if excess is not None else None,
                  "exc_c": round(exc_c, 2) if exc_c is not None else None}
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
    # Pick the deepest horizon that actually has data for this cohort
    rets = [s.get("returns", {}) for s in items]
    horizon = None
    for h in (5, 3, 1):
        if any(r.get(h, {}).get("ret") is not None for r in rets):
            horizon = h
            break
    if horizon is None:
        return {"score": None, "horizon": None, "n": 0,
                "verdict": "no data", "tradeable": False}
    a = _agg(rets, horizon)
    n = a["n"] or 0
    if n < 5:
        return {"score": None, "horizon": horizon, "n": n,
                "verdict": "insufficient sample", "tradeable": False}
    hit  = a["hit_rate"] or 0
    avgx = a["avg_excess"] if a["avg_excess"] is not None else 0.0
    # Excursion penalty: cohorts where MAE swamps MFE get capped
    mfe_vals = [s["excursion"]["mfe"] for s in items
                if s.get("excursion") and s["excursion"].get("mfe") is not None]
    mae_vals = [s["excursion"]["mae"] for s in items
                if s.get("excursion") and s["excursion"].get("mae") is not None]
    mfe = sum(mfe_vals) / len(mfe_vals) if mfe_vals else 0
    mae = sum(mae_vals) / len(mae_vals) if mae_vals else 0
    mfe_mae = max(0.0, min(1.0, (mfe + mae) / max(1.0, mfe)))
    sample_f = min(1.0, n / 30.0)
    dir_f    = max(0.0, (hit - 50.0) / 50.0)
    alpha_f  = max(0.0, min(1.0, avgx / 5.0))
    if hit < 50 or avgx <= 0:
        score = 0
        verdict = "anti-alpha" if hit < 50 else "neutral"
    else:
        raw = 100 * sample_f * (0.4 * dir_f + 0.5 * alpha_f + 0.1 * mfe_mae)
        score = round(max(0, min(100, raw)))
        if   score >= 60: verdict = "high alpha"
        elif score >= 40: verdict = "moderate alpha"
        elif score >= 20: verdict = "low alpha"
        else:             verdict = "marginal"
    return {
        "score":     score,
        "horizon":   horizon,
        "n":         n,
        "hit_rate":  hit,
        "avg_excess": avgx,
        "verdict":   verdict,
        "tradeable": (n >= 10 and hit > 50 and avgx > 0),
    }


def _oi_summary(items):
    """Next-day OI-confirmation rate for a group of scored signals. This is
    an INTERIM quality read — available the day after the flag, long before
    price returns mature — so the Performance tab has something real to show
    while the +5d window is still filling."""
    oc = [s["oi"]["status"] for s in items
          if s.get("oi") and s["oi"].get("status") not in (None, "pending")]
    confirmed = sum(1 for x in oc if x == "confirmed")
    return {
        "checked":      len(oc),
        "confirmed":    confirmed,
        "confirm_rate": round(100 * confirmed / len(oc)) if oc else None,
    }


def compute_edge():
    """Score the ledger; build aggregates + per-signal scorecards. Signals
    older than FINAL_AGE_DAYS are frozen in uoa_alpha_cache.json — every
    horizon has matured, so they're scored once and never re-fetched. Only
    the rolling active window is recomputed, and its bar/OI pulls fan out
    across a thread pool."""
    ledger = load_ledger()
    print(f"  Ledger: {len(ledger)} signals")
    if not ledger:
        return _empty_edge(), []

    today = datetime.now(timezone.utc).date()
    cache = _load_alpha_cache()

    # cached-final signals reuse their frozen score; the rest recompute
    scored, fresh = [], []
    for s in ledger:
        c = cache.get(s.get("id") or "")
        if c and _is_final(s, today):
            s2 = dict(s)
            s2["returns"]   = _restore_returns(c.get("returns"))
            s2["excursion"] = c.get("excursion") or {"mfe": None, "mae": None}
            s2["oi"]        = c.get("oi") or _pending_oi()
            scored.append(s2)
        else:
            fresh.append(s)
    print(f"  {len(scored)} final (cached), {len(fresh)} to score...")

    # forward returns + excursions for the active window — daily bars fetched
    # concurrently, one call per unique ticker
    if fresh:
        spy_closes = {d: b["c"] for d, b in _bars("SPY").items()}
        tks = sorted({s.get("ticker", "") for s in fresh if s.get("ticker")})
        bar_cache = {}
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for tk, bars in ex.map(lambda t: (t, _bars(t)), tks):
                bar_cache[tk] = bars
        for s in fresh:
            bars = bar_cache.get(s.get("ticker", ""), {})
            s2 = dict(s)
            s2["returns"]   = forward_returns(s, bars, spy_closes)
            s2["excursion"] = excursions(s, bars)
            scored.append(s2)

    # next-day OI status — current option chain fetched concurrently, only
    # for active-window tickers with a confirmable signal
    def _confirmable(s):
        if s.get("open_interest") is None or s.get("volume") is None:
            return False
        try:
            fd = datetime.strptime(s["flagged_at"][:10], "%Y-%m-%d").date()
        except Exception:
            return False
        return (today - fd).days >= 1
    oi_tickers = sorted({s["ticker"] for s in fresh
                         if s.get("ticker") and _confirmable(s)})
    oi_cache = {}
    if oi_tickers:
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for tk, oi in ex.map(lambda t: (t, _oi_now(t)), oi_tickers):
                oi_cache[tk] = oi
    for s in scored:
        if "oi" in s:                       # cached-final already carries it
            continue
        s["oi"] = (oi_status(s, oi_cache.get(s["ticker"], {}))
                   if _confirmable(s) else _pending_oi())

    # freeze newly-final signals so future runs skip them entirely
    new_final = 0
    for s in scored:
        sid = s.get("id")
        if sid and sid not in cache and _is_final(s, today):
            cache[sid] = {"returns": s["returns"], "excursion": s["excursion"],
                          "oi": s["oi"]}
            new_final += 1
    if new_final:
        _save_alpha_cache(cache)
        print(f"  Froze {new_final} newly-final signals into the cache")

    matured_5d = sum(1 for s in scored if s["returns"].get(5))

    # Each group carries per-horizon return stats (h) AND an OI-confirmation
    # summary (oi) — the latter is meaningful before any price horizon matures.
    # alpha_confidence is the single sortable "trade this?" score that ties
    # every cohort table together. All slices now carry it.
    by_type = {}
    for typ in SIGNAL_TYPES:
        items = [s for s in scored if s.get("signal_type") == typ]
        if items:
            by_type[typ] = {"signals": len(items), "h": _group(items),
                            "oi": _oi_summary(items),
                            "rich": _rich(items),
                            "alpha_confidence": _alpha_confidence(items)}

    by_score = {}
    for label, lo, hi in SCORE_BUCKETS:
        items = [s for s in scored if lo <= s.get("trade_score", 0) < hi]
        if items:
            by_score[label] = {"signals": len(items), "h": _group(items),
                               "oi": _oi_summary(items),
                               "rich": _rich(items),
                               "excursion": _excursion_avg(items),
                               "alpha_confidence": _alpha_confidence(items)}

    by_dte = {}
    for b in DTE_BUCKETS:
        items = [s for s in scored if _dte_bucket(s.get("dte")) == b]
        if items:
            by_dte[b] = {"signals": len(items), "h": _group(items),
                         "oi": _oi_summary(items),
                         "rich": _rich(items),
                         "alpha_confidence": _alpha_confidence(items)}

    by_tag = {}
    for tag in ATTRIB_TAGS:
        items = [s for s in scored if tag in (s.get("tags") or [])]
        if items:
            by_tag[tag] = {"signals": len(items), "h": _group(items),
                           "oi": _oi_summary(items),
                           "rich": _rich(items),
                           "alpha_confidence": _alpha_confidence(items)}

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
            "signals":   len(items),
            "h":         _group(items),
            "oi":        _oi_summary(items),
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
                "signals":   len(items),
                "h":         _group(items),
                "oi":        _oi_summary(items),
                "excursion": _excursion_avg(items),
                "alpha_confidence": _alpha_confidence(items),
            }
    except Exception as e:
        print(f"  by_theme aggregation skipped: {e}")

    # Direction split — the 2026-07 alpha decomposition's headline: the
    # book's EV separates almost entirely by trade side. Published gated
    # (n>=30) so the site can show call-book vs put-follow vs seller EV
    # honestly. Dir-signed +5d excess: bullish sides earn +excess,
    # bearish earn -excess; sellers graded on their income lean.
    direction_split = {}
    try:
        side_rows = {}
        for s in scored:
            r5 = (s.get("returns") or {}).get(5) or \
                 (s.get("returns") or {}).get("5")
            exc = (r5 or {}).get("excess")
            if exc is None:
                continue
            side = _side_of_signal(s)
            t = s.get("type") or ""
            seller = side == "seller"
            bull = (t != "put") != seller
            side_rows.setdefault(side, []).append(exc if bull else -exc)
        for side, v in side_rows.items():
            if len(v) < 30:
                direction_split[side] = {"status": "accruing", "n": len(v)}
                continue
            wins = sum(1 for x in v if x > 0)
            direction_split[side] = {
                "status": "active", "n": len(v),
                "ev": round(sum(v) / len(v), 2),
                "hit": round(100 * wins / len(v)),
            }
    except Exception as e:
        print(f"  direction_split skipped: {e}")

    oc = [s["oi"]["status"] for s in scored if s["oi"]["status"] != "pending"]
    # Find the oldest signal so the client can compute "expected mature
    # on YYYY-MM-DD" for horizons that haven't accumulated samples yet
    # (e.g. 10d / 20d when the scanner has only been running a few weeks).
    # Without this, the admin panel says "needs more backfill" without
    # any concrete date the user can plan against.
    flagged_ts = [s.get("flagged_at") for s in scored if s.get("flagged_at")]
    oldest_signal_ts = min(flagged_ts) if flagged_ts else None
    edge = {
        "generated":     datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_signals": len(scored),
        "matured_5d":    matured_5d,
        "oldest_signal_ts": oldest_signal_ts,
        "horizons":      HORIZONS,
        "overall":       _group(scored),
        "rich_overall":  {str(h): _rich(scored, h) for h in (1, 3, 5, 10, 20)},
        "ic":            _ic_suite(scored),
        "by_regime":     _by_regime(scored),
        "calibration":   _calibration(scored),
        "lifecycle":     _lifecycle(scored),
        "excursion":     _excursion_avg(scored),
        "by_type":       by_type,
        "by_score":      by_score,
        "by_dte":        by_dte,
        "by_tag":        by_tag,
        "by_ticker":     by_ticker,
        "by_theme":      by_theme,
        "direction_split": direction_split,
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
        "by_ticker":     {},
        "by_theme":      {},
        "oi_confirmation": {"checked": 0, "confirmed": 0, "weak": 0,
                            "closed": 0, "confirm_rate": None},
    }


# ─── EV-first decision-support stats (Flow redesign) ─────────────────────────
# Everything below is computed from the matured ledger — no estimates, no
# hand-set numbers. Cohorts below their sample gate publish {"status":
# "accruing"} instead of thin statistics.

def _r2(v):
    return round(v, 2)


def _rich(items, horizon=5):
    """Expected-value-first stats for one cohort at one horizon, with
    uncertainty. Hit rate alone is misleading — a 49% hit rate with big
    winners and small losers is a positive-EV system. Uses EXCESS return
    vs SPY so beta doesn't masquerade as edge."""
    xs = [s.get("returns", {}).get(horizon, {}).get("excess") for s in items]
    xs = [v for v in xs if v is not None]
    n = len(xs)
    if n < 20:
        return {"n": n, "status": "accruing"}
    srt = sorted(xs)
    q = lambda p: srt[min(n - 1, int(p * n))]
    wins = [v for v in xs if v > 0]
    losses = [v for v in xs if v <= 0]
    avg = mean(xs)
    sd = pstdev(xs) if n > 1 else 0.0
    se = sd / sqrt(n) if n else 0.0
    gross_w = sum(wins)
    gross_l = abs(sum(losses))
    sig = abs(avg) > 1.96 * se if se else False
    return {
        "n": n, "status": "active",
        "ev": _r2(avg),                      # expected excess per trade (%)
        "median": _r2(q(0.5)),
        "std": _r2(sd),
        "win_rate": round(100 * len(wins) / n),
        "avg_win": _r2(mean(wins)) if wins else None,
        "avg_loss": _r2(mean(losses)) if losses else None,
        "profit_factor": _r2(gross_w / gross_l) if gross_l else None,
        "ci95": [_r2(avg - 1.96 * se), _r2(avg + 1.96 * se)],
        "p25": _r2(q(0.25)), "p75": _r2(q(0.75)), "p90": _r2(q(0.90)),
        "ir": _r2(avg / sd) if sd else None,  # per-trade information ratio
        "significant": sig,
        # sample-size + significance confidence badge
        "confidence": ("high" if n >= 800 and sig
                       else "medium" if n >= 200 else "low"),
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
    predict a better +5d excess return? IC ~0 = the score ranks noise."""
    pairs = []
    for s in scored:
        sc = s.get("trade_score")
        x = s.get("returns", {}).get(5, {}).get("excess")
        if sc is not None and x is not None:
            pairs.append((sc, x, s.get("flagged_at") or ""))
    base = [(p[0], p[1]) for p in pairs]
    now = datetime.now(timezone.utc)
    def window(days):
        cut = (now - timedelta(days=days)).isoformat()
        return _spearman([(p[0], p[1]) for p in pairs if p[2] >= cut])
    return {
        "n": len(base),
        "ic_spearman": _spearman(base),
        "ic_30d": window(30),
        "ic_90d": window(90),
        "note": ("Spearman rank IC of trade_score vs +5d excess. For daily "
                 "signals, |IC| >= 0.05 is meaningful; the score_deciles "
                 "block carries the top/bottom decile spread."),
    }


def _by_regime(scored):
    """Cohort stats sliced by the market regime on the FLAG day (labels
    from regime_history.json — loop 7's dataset). History only reaches
    back to when regime logging started, so most cells accrue honestly."""
    try:
        with open(os.path.join(_BASE, "docs", "reports",
                               "regime_history.json"), encoding="utf-8") as f:
            hist = json.load(f)
        lab = {d.get("date"): d.get("label")
               for d in hist.get("days") or [] if d.get("date")}
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
        out[lb] = r if r.get("n", 0) >= 200 else \
            {"n": r.get("n", 0), "status": "accruing"}
    return out


def _calibration(scored):
    """Time-split calibration per score band: 'predicted' = the band's
    win rate on signals older than 45 days (training window), 'observed'
    = the last 45 days. Drift = observed - predicted. A well-calibrated
    band drifts near zero; big drift = the band's meaning has changed."""
    cut = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    out = {}
    for label, lo, hi in SCORE_BUCKETS:
        items = [s for s in scored if lo <= s.get("trade_score", 0) < hi]
        def wr(sub):
            xs = [s.get("returns", {}).get(5, {}).get("excess") for s in sub]
            xs = [v for v in xs if v is not None]
            return (round(100 * sum(1 for v in xs if v > 0) / len(xs)),
                    len(xs)) if xs else (None, 0)
        pred, n_tr = wr([s for s in items if (s.get("flagged_at") or "") < cut])
        obs, n_lv = wr([s for s in items if (s.get("flagged_at") or "") >= cut])
        if n_tr >= 200 and n_lv >= 50 and pred is not None and obs is not None:
            out[label] = {"predicted": pred, "observed": obs,
                          "drift": obs - pred, "n_train": n_tr, "n_live": n_lv}
        else:
            out[label] = {"status": "accruing", "n_train": n_tr, "n_live": n_lv}
    return out


LIFECYCLE_PATH = os.path.join(_BASE, "data", "oi_lifecycle.json")
LIFECYCLE_CAP = 8000


def _lifecycle(scored):
    """Position-lifecycle accrual: each nightly run snapshots whether a
    young signal's contract OI is still elevated (confirmed/weak) at its
    current day-offset. Aggregates into a retention curve — held +1d,
    +2d, +3d, +5d, +10d. Accrues going forward from first deploy; gates
    at n>=200 per offset. Calendar-day offsets (noted in the payload)."""
    try:
        with open(LIFECYCLE_PATH, encoding="utf-8") as f:
            log = json.load(f)
    except Exception:
        log = {}
    today = datetime.now(timezone.utc).date()
    for s in scored:
        oi = s.get("oi") or {}
        if oi.get("status") in (None, "pending"):
            continue
        sid = s.get("id")
        fd = (s.get("flagged_at") or "")[:10]
        if not sid or not fd:
            continue
        try:
            off = (today - datetime.strptime(fd, "%Y-%m-%d").date()).days
        except Exception:
            continue
        if off < 1 or off > 12:
            continue
        e = log.setdefault(str(sid), {})
        k = str(off)
        if k not in e:
            e[k] = 1 if oi["status"] in ("confirmed", "weak") else 0
    if len(log) > LIFECYCLE_CAP:                    # drop oldest entries
        for k in list(log)[:len(log) - LIFECYCLE_CAP]:
            del log[k]
    try:
        os.makedirs(os.path.dirname(LIFECYCLE_PATH), exist_ok=True)
        with open(LIFECYCLE_PATH, "w", encoding="utf-8") as f:
            json.dump(log, f, separators=(",", ":"))
    except Exception as e:
        print(f"  lifecycle log write failed (non-fatal): {e}")
    out = {}
    for off in ("1", "2", "3", "5", "10"):
        vals = [e[off] for e in log.values() if off in e]
        out[off] = ({"n": len(vals),
                     "held_rate": round(100 * sum(vals) / len(vals))}
                    if len(vals) >= 200
                    else {"n": len(vals), "status": "accruing"})
    out["note"] = ("Share of tracked signals whose contract OI stayed "
                   "elevated N calendar days after the flag. Accrues from "
                   "2026-07-14 forward; publishes per offset at n>=200.")
    return out


def _emit_scored(scored):
    """Per-signal scorecard JSON for the dashboard's Tracked-Signals view."""
    rows = []
    for s in scored:
        ret = s.get("returns") or {}
        r1, r3, r5 = ret.get(1) or {}, ret.get(3) or {}, ret.get(5) or {}
        exc = s.get("excursion") or {}
        oi  = s.get("oi") or {}
        # contract type + strike from the OCC ticker — works for every signal,
        # including older ones whose ledger row predates the `type` field.
        occ_type, strike = _parse_occ(s.get("contract"))
        spot = s.get("underlying_px_at_flag")
        is_otm = None
        if occ_type and strike and spot:
            is_otm = (strike >= spot) if occ_type == "call" else (strike <= spot)
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
            # pass-through fields so the Tracked-Signals tab can offer the
            # same filters as Live Flow. `type` falls back to the OCC ticker
            # so the call/put filter works on pre-schema-expansion signals.
            "type":         s.get("type") or occ_type,
            "is_otm":       is_otm,
            "cap_bucket":   s.get("cap_bucket"),
            "sector":       s.get("sector"),
            "themes":       s.get("themes", []),
            "opening":      s.get("opening"),
            "liquidity":    s.get("liquidity"),
            "flow_side":    s.get("flow_side"),
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
    # Cap the SITE copy to the most recent signals + write COMPACT (no
    # indent). The full ledger stays in data/uoa_signals.jsonl and the edge
    # aggregates in uoa_edge.json are computed on ALL signals, so this only
    # trims the per-signal "Tracked Signals" list — while keeping the file
    # small enough that GitHub Pages can actually deploy it. (The old
    # indent=1 full dump ballooned to ~42MB and timed out the Pages deploy.)
    # 15000 rows made an 8.6MB payload whose parse + retained heap slowed
    # whole browsers on the Flow tab; the Tracked view renders at most 400
    # rows post-filter, so 4000 recent signals (~2 months) lose nothing
    # user-visible. Deep history stays in the ledger + edge aggregates.
    SITE_MAX = 4000
    total = len(rows)
    rows = rows[:SITE_MAX]
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count":     len(rows),
        "total":     total,
        "signals":   rows,
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
EW_PATH        = os.path.join(_BASE, "data", "edge_weights.json")
EW_SITE_PATH   = os.path.join(_BASE, "docs", "reports", "edge_weights.json")
EW_MIN_N       = 200    # min matured signals before a feature may emit
EW_SHRINK_K    = 200    # pseudo-observations pulled toward the prior
EW_SCALE       = 80     # 5% shrunk lift -> 4.0 score points
EW_PER_MAX     = 4.0    # per-feature clamp (points)
# Signal-decay diagnostic: recent window vs prior window hit rates per
# feature. DISPLAY-ONLY for now — weights still learn from the full sample;
# decay is the early-warning that an edge is dying (and the evidence needed
# before ever switching the weights themselves to a recency window).
EW_DECAY_RECENT_D = 30
EW_DECAY_PRIOR_D  = 90     # prior window = (recent, 90] days back
EW_DECAY_MIN_N    = 50     # per window, per feature
# Regime-conditioned weights: GATED. Emitted only when enough labeled days
# from regime_history.json join matured outcomes — activates automatically
# as the (new) regime dataset accrues. Until then the file carries an
# honest {"status":"accruing"} and the scanner uses the global set.
REGIME_HISTORY_PATH = os.path.join(_BASE, "docs", "reports", "regime_history.json")
EW_REGIME_MIN_DAYS  = 40   # labeled trading days required per activation
EW_REGIME_MIN_N     = 150  # graded signals required per regime cohort


def _ew_decay(graded, now_utc):
    """Per-feature hit rate: last EW_DECAY_RECENT_D days vs the prior window.
    Only features with >= EW_DECAY_MIN_N graded signals in BOTH windows emit
    (a delta computed on thin windows is noise, not decay)."""
    recent, prior = {}, {}
    for s, w in graded:
        try:
            d = (now_utc - datetime.fromisoformat(
                s["flagged_at"].replace("Z", "+00:00"))).days
        except Exception:
            continue
        bucket = recent if d <= EW_DECAY_RECENT_D else (
            prior if d <= EW_DECAY_PRIOR_D else None)
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
        out[f] = {"recent_n": rn, "recent_hit": round(rh, 3),
                  "prior_n": pn, "prior_hit": round(ph, 3),
                  "delta": round(rh - ph, 3)}
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
    label_by_date = {d.get("date"): d.get("label") for d in days
                     if d.get("date") and d.get("label")}
    joined = []
    for s, w in graded:
        lbl = label_by_date.get((s.get("flagged_at") or "")[:10])
        if lbl:
            joined.append((s, w, lbl))
    labeled_days_used = len({(s.get("flagged_at") or "")[:10]
                             for s, w, _ in joined})
    if labeled_days_used < EW_REGIME_MIN_DAYS:
        return {"status": "accruing", "labeled_days": len(label_by_date),
                "labeled_days_with_outcomes": labeled_days_used,
                "activates_at_days": EW_REGIME_MIN_DAYS}
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
            if n < EW_MIN_N // 2:      # regime cohorts are smaller by nature
                continue
            shrunk = (wins + EW_SHRINK_K * prior) / (n + EW_SHRINK_K)
            adj = round(max(-EW_PER_MAX, min(EW_PER_MAX,
                                             (shrunk - prior) * EW_SCALE)), 1)
            feats[key] = {"n": n, "adj": adj}
        if feats:
            sets[regime] = {"prior": round(prior, 4), "n": len(cohort),
                            "features": feats}
    if not sets:
        return {"status": "accruing", "labeled_days": len(label_by_date),
                "labeled_days_with_outcomes": labeled_days_used,
                "activates_at_days": EW_REGIME_MIN_DAYS}
    return {"status": "active", "labeled_days_with_outcomes": labeled_days_used,
            "sets": sets}


def _ew_win(s):
    """Direction-aware +5d excess win, or None if not gradeable. Bullish
    wins when excess > 0; bearish when excess < 0. Income/hedge structures
    aren't graded by underlying direction, so they're excluded."""
    d = s.get("direction")
    if d not in ("bullish", "bearish"):
        return None
    r5 = (s.get("returns") or {}).get(5) or (s.get("returns") or {}).get("5")
    exc = (r5 or {}).get("excess")
    if exc is None:
        return None
    return (exc > 0) if d == "bullish" else (exc < 0)


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
    feats = [f"type:{s.get('signal_type')}",
             f"side:{_side_of_signal(s)}",
             f"dte:{_dte_bucket(s.get('dte'))}",
             f"cap:{s.get('cap_bucket') or 'unknown'}",
             f"liq:{s.get('liquidity') or 'C'}"]
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
            w = _ew_win(s)
            if w is None:
                continue
            r5 = (s.get("returns") or {}).get(5) or \
                 (s.get("returns") or {}).get("5")
            exc = (r5 or {}).get("excess")
            d = s.get("direction")
            if exc is None:
                continue
            # direction-signed: a bearish signal "earns" when the stock falls
            out.append(exc if d == "bullish" else -exc)
        return out

    cohorts = {
        "score_80_plus":  lambda s: (s.get("trade_score") or 0) >= 80,
        "score_65_plus":  lambda s: (s.get("trade_score") or 0) >= 65,
        "golden_sweeps":  lambda s: s.get("signal_type") == "golden_sweep",
        "into_earnings":  lambda s: "Into ERN" in (s.get("tags") or []),
        "positioning_dte": lambda s: _dte_bucket(s.get("dte")) == "positioning",
    }
    out = {}
    for name, pred in cohorts.items():
        rets = graded_rows(pred)
        if len(rets) < 30:
            continue
        wins = sum(1 for r in rets if r > 0)
        out[name] = {
            "n":        len(rets),
            "hit":      round(100 * wins / len(rets)),
            "avg_exc":  round(sum(rets) / len(rets), 2),
            "total_units": round(sum(rets), 1),   # sum of 1-unit trades
            "best":     round(max(rets), 1),
            "worst":    round(min(rets), 1),
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
GRID_STOPS   = (5.0, 8.0, 12.0)
GRID_TARGETS = (8.0, 15.0, 25.0)
GRID_MIN_N   = 500


def _stop_target_grid(scored):
    rows = []
    for s in scored:
        d = s.get("direction")
        if d not in ("bullish", "bearish"):
            continue
        exc = s.get("excursion") or {}
        r20 = (s.get("returns") or {}).get(20) or \
              (s.get("returns") or {}).get("20")
        mfe, mae = exc.get("mfe"), exc.get("mae")
        fin = (r20 or {}).get("ret")
        if mfe is None or mae is None or fin is None:
            continue
        if d == "bullish":
            rows.append((mfe, mae, fin, (s.get("trade_score") or 0)))
        else:
            rows.append((-mae, -mfe, -fin, (s.get("trade_score") or 0)))
    if len(rows) < GRID_MIN_N:
        return {"status": "accruing", "n": len(rows),
                "activates_at": GRID_MIN_N}

    def cell(rs, stop, target):
        outs = []
        for fav, adv, fin, _ in rs:
            if adv <= -stop:
                outs.append(-stop)          # pessimistic: stop first
            elif fav >= target:
                outs.append(target)
            else:
                outs.append(fin)
        n = len(outs)
        return {"n": n,
                "hit": round(100 * sum(1 for r in outs if r > 0) / n),
                "avg": round(sum(outs) / n, 2),
                "stopped_pct": round(100 * sum(1 for _, a, _f, _sc in rs
                                               if a <= -stop) / n)}

    def table(rs):
        base_rets = [f for _, _, f, _ in rs]
        base = {"n": len(base_rets),
                "hit": round(100 * sum(1 for r in base_rets if r > 0)
                             / len(base_rets)),
                "avg": round(sum(base_rets) / len(base_rets), 2)}
        grid = {}
        for st in GRID_STOPS:
            for tg in GRID_TARGETS:
                grid[f"{st:g}/{tg:g}"] = cell(rs, st, tg)
        return {"baseline_hold20": base, "grid": grid}

    hi = [r for r in rows if r[3] >= 80]
    out = {"status": "active",
           "assumptions": ("20-session window, raw underlying return, "
                           "direction-aware, pessimistic ordering (stop "
                           "assumed first when both levels hit)"),
           "stops": list(GRID_STOPS), "targets": list(GRID_TARGETS),
           "all": table(rows)}
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
        w = _ew_win(s)
        if w is None:
            continue
        r5 = (s.get("returns") or {}).get(5) or \
             (s.get("returns") or {}).get("5")
        exc = (r5 or {}).get("excess")
        if exc is None:
            continue
        signed = exc if s.get("direction") == "bullish" else -exc
        rows.append(((s.get("trade_score") or 0), signed))
    if len(rows) < DECILE_MIN_N:
        return {"status": "accruing", "n": len(rows),
                "activates_at": DECILE_MIN_N}
    rows.sort(key=lambda x: x[0])
    n = len(rows)
    deciles = []
    for i in range(10):
        chunk = rows[i * n // 10:(i + 1) * n // 10]
        if not chunk:
            continue
        vals = [v for _, v in chunk]
        deciles.append({
            "d": i + 1,
            "score_lo": round(chunk[0][0]), "score_hi": round(chunk[-1][0]),
            "n": len(chunk),
            "hit": round(100 * sum(1 for v in vals if v > 0) / len(vals)),
            "avg_exc": round(sum(vals) / len(vals), 2),
        })
    top, bot = deciles[-1], deciles[0]
    mono_up = sum(1 for a, b in zip(deciles, deciles[1:])
                  if b["avg_exc"] >= a["avg_exc"])
    return {"status": "active", "n": n, "deciles": deciles,
            "top_minus_bottom": round(top["avg_exc"] - bot["avg_exc"], 2),
            "monotonic_steps": f"{mono_up}/{len(deciles) - 1}",
            "note": ("direction-signed +5d excess vs SPY by trade-score "
                     "decile — the ranking's own report card")}


def _emit_edge_weights(scored):
    """Compute per-feature adjustments from matured directional outcomes and
    publish the versioned weights file the scanner reads next run."""
    graded = [(s, w) for s in scored for w in [_ew_win(s)] if w is not None]
    if len(graded) < EW_MIN_N * 2:
        print(f"  edge_weights: only {len(graded)} gradeable signals — skipping")
        return None
    prior = sum(1 for _, w in graded if w) / len(graded)

    stats = {}
    for s, w in graded:
        for f in _ew_features(s):
            st = stats.setdefault(f, [0, 0])          # [n, wins]
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
            "n": n, "win": round(wins / n, 3), "shrunk": round(shrunk, 3),
            "lift": round(lift, 3), "adj": adj,
        }
        if prev.get(key) is not None and abs(adj - prev[key]) >= 0.5:
            print(f"  edge_weights Δ {key}: {prev[key]:+.1f} -> {adj:+.1f}")

    now_utc = datetime.now(timezone.utc)
    decay = _ew_decay(graded, now_utc)
    dying = {k: v for k, v in decay.items() if v["delta"] <= -0.08}
    if dying:
        print("  edge decay ⚠ " + ", ".join(
            f"{k} {v['prior_hit']:.0%}->{v['recent_hit']:.0%}"
            for k, v in sorted(dying.items(), key=lambda x: x[1]["delta"])[:4]))
    regimes = _ew_regime_sets(graded)
    if regimes.get("status") == "accruing":
        print(f"  regime weights: accruing "
              f"({regimes.get('labeled_days_with_outcomes', 0)}/"
              f"{EW_REGIME_MIN_DAYS} labeled days with outcomes)")
    else:
        print(f"  regime weights ACTIVE: {list(regimes['sets'].keys())}")

    payload = {
        "version":      datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "prev_version": prev_version,
        "prior":        round(prior, 4),
        "graded":       len(graded),
        "params": {"min_n": EW_MIN_N, "shrink_k": EW_SHRINK_K,
                   "scale": EW_SCALE, "per_max": EW_PER_MAX,
                   "decay_windows_d": [EW_DECAY_RECENT_D, EW_DECAY_PRIOR_D],
                   "regime_min_days": EW_REGIME_MIN_DAYS},
        "features":     features,
        "decay":        decay,
        "regimes":      regimes,
    }
    for path in (EW_PATH, EW_SITE_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=1)
    print(f"  Wrote edge_weights.json — {len(features)} features from "
          f"{len(graded)} graded signals (prior {prior:.1%})")
    return payload


def run():
    """Compute edge stats + per-signal scorecards; publish both JSON files."""
    edge, scored = compute_edge()
    os.makedirs(os.path.dirname(EDGE_PATH), exist_ok=True)
    # Split the heavy per-ticker/per-theme cohorts into their own file —
    # no client code reads them from the main payload (verified), yet they
    # were ~90% of a 656 KB file fetched with the Flow tab. The cohorts
    # file exists for future lazy consumption (drilldowns, Performance).
    try:
        edge["counterfactuals"] = _counterfactuals(scored)
        for k, v in edge["counterfactuals"].items():
            print(f"  counterfactual {k}: n={v['n']} hit={v['hit']}% "
                  f"avg {v['avg_exc']:+.2f}%/trade (dir-signed +5d excess)")
    except Exception as e:
        print(f"  counterfactuals failed (non-fatal): {e}")
    try:
        edge["stop_target_grid"] = _stop_target_grid(scored)
        g = edge["stop_target_grid"]
        if g.get("status") == "active":
            b = g["all"]["baseline_hold20"]
            print(f"  stop/target grid: n={b['n']} baseline hold-20d "
                  f"hit {b['hit']}% avg {b['avg']:+.2f}%")
    except Exception as e:
        print(f"  stop_target_grid failed (non-fatal): {e}")
    try:
        edge["score_deciles"] = _score_deciles(scored)
        sd = edge["score_deciles"]
        if sd.get("status") == "active":
            print(f"  rank audit: top-bottom decile spread "
                  f"{sd['top_minus_bottom']:+.2f}pp "
                  f"({sd['monotonic_steps']} steps monotonic, n={sd['n']})")
    except Exception as e:
        print(f"  score_deciles failed (non-fatal): {e}")
    cohorts = {"generated": edge.get("generated"),
               "by_ticker": edge.pop("by_ticker", {}),
               "by_theme":  edge.pop("by_theme", {})}
    with open(os.path.join(_BASE, "docs", "reports",
                           "uoa_edge_cohorts.json"), "w",
              encoding="utf-8") as f:
        json.dump(cohorts, f, separators=(",", ":"))
    with open(EDGE_PATH, "w", encoding="utf-8") as f:
        json.dump(edge, f, separators=(",", ":"))
    _emit_scored(scored)
    try:
        _emit_edge_weights(scored)
    except Exception as e:
        print(f"  edge_weights failed (non-fatal): {e}")
    # Nightly model-health report: one small JSON answering "is the
    # ranking engine itself healthy?" — rolling IC, calibration drift,
    # feature freshness counts, ledger depth. Read by the Flow tab.
    try:
        cal = edge.get("calibration") or {}
        drifts = [abs(v.get("drift", 0)) for v in cal.values()
                  if isinstance(v, dict) and v.get("drift") is not None]
        try:
            with open(os.path.join(_BASE, "data", "edge_weights.json"),
                      encoding="utf-8") as f:
                dec = (json.load(f).get("decay") or {})
        except Exception:
            dec = {}
        fading = sum(1 for v in dec.values()
                     if (v.get("delta") or 0) <= -0.08)
        improving = sum(1 for v in dec.values()
                        if (v.get("delta") or 0) >= 0.08)
        health = {
            "generated": edge["generated"],
            "ledger": {"total": edge["total_signals"],
                       "matured_5d": edge["matured_5d"]},
            "ic": edge.get("ic"),
            "calibration_max_drift": max(drifts) if drifts else None,
            "features": {"tracked": len(dec), "fading": fading,
                         "improving": improving},
            "note": ("Nightly self-check of the ranking engine. IC = rank "
                     "correlation of score vs realized +5d excess; drift = "
                     "per-band predicted-vs-live win-rate gap. Nothing here "
                     "auto-changes production weights."),
        }
        with open(os.path.join(_BASE, "docs", "reports",
                               "model_health.json"), "w",
                  encoding="utf-8") as f:
            json.dump(health, f, separators=(",", ":"))
        ic = edge.get("ic") or {}
        print(f"  model health: IC {ic.get('ic_spearman')} "
              f"· 30d {ic.get('ic_30d')} "
              f"· fading {fading} / improving {improving}")
    except Exception as e:
        print(f"  model_health failed (non-fatal): {e}")
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
