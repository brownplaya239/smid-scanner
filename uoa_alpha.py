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
from datetime import datetime, timezone
from statistics import mean, median

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
                            "alpha_confidence": _alpha_confidence(items)}

    by_score = {}
    for label, lo, hi in SCORE_BUCKETS:
        items = [s for s in scored if lo <= s.get("trade_score", 0) < hi]
        if items:
            by_score[label] = {"signals": len(items), "h": _group(items),
                               "oi": _oi_summary(items),
                               "alpha_confidence": _alpha_confidence(items)}

    by_dte = {}
    for b in DTE_BUCKETS:
        items = [s for s in scored if _dte_bucket(s.get("dte")) == b]
        if items:
            by_dte[b] = {"signals": len(items), "h": _group(items),
                         "oi": _oi_summary(items),
                         "alpha_confidence": _alpha_confidence(items)}

    by_tag = {}
    for tag in ATTRIB_TAGS:
        items = [s for s in scored if tag in (s.get("tags") or [])]
        if items:
            by_tag[tag] = {"signals": len(items), "h": _group(items),
                           "oi": _oi_summary(items),
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
        "excursion":     _excursion_avg(scored),
        "by_type":       by_type,
        "by_score":      by_score,
        "by_dte":        by_dte,
        "by_tag":        by_tag,
        "by_ticker":     by_ticker,
        "by_theme":      by_theme,
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
    SITE_MAX = 15000
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


def _ew_features(s):
    """Feature keys for one signal — must be computable at SCAN time from the
    same fields, so the scanner can mirror this exactly."""
    feats = [f"type:{s.get('signal_type')}",
             f"dte:{_dte_bucket(s.get('dte'))}",
             f"cap:{s.get('cap_bucket') or 'unknown'}",
             f"liq:{s.get('liquidity') or 'C'}"]
    feats += [f"tag:{t}" for t in (s.get("tags") or []) if t in ATTRIB_TAGS]
    return feats


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

    payload = {
        "version":      datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "prev_version": prev_version,
        "prior":        round(prior, 4),
        "graded":       len(graded),
        "params": {"min_n": EW_MIN_N, "shrink_k": EW_SHRINK_K,
                   "scale": EW_SCALE, "per_max": EW_PER_MAX},
        "features":     features,
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
    with open(EDGE_PATH, "w", encoding="utf-8") as f:
        json.dump(edge, f, indent=1)
    _emit_scored(scored)
    try:
        _emit_edge_weights(scored)
    except Exception as e:
        print(f"  edge_weights failed (non-fatal): {e}")
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
