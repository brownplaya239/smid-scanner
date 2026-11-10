"""
uoa_scorecard.py — matched-control flow scorecard (v2, 2026-11 accuracy fix).

The question this card answers: when the scanner flags directional option
flow on a stock, does that STOCK then beat similar stocks that had no flow?
Everything is measured on the stock (shares), never on the option.

  UNIT. One row per (ticker, US/Eastern date, direction). The ledger
     re-logs every contract on every ~30-min scan plus the after-close
     runs, so raw rows over-count each alert many times. Rows are first
     collapsed to one alert per contract per ET day
     (uoa_alpha.unique_alerts), then to one stock bet per ticker per day
     per direction: every alert on the same stock and day shares the same
     stock return, so counting them separately is pseudo-replication. A
     unit's score is the highest score any of its alerts reached that day;
     it is in a pocket (golden sweep, into earnings) if any alert was.
  MATCHED CONTROLS. Up to 10 control stocks at the signal date, nearest
     by z-scored 63-day momentum and 20-day realized vol, searched in
     fallback tiers until at least 5 qualify: same sector within 3x market
     cap, same sector within 5x, any sector within 3x, any sector within
     10x. The tier used is recorded per unit. Excluded as controls: every
     ticker with ANY ledger row that ET day (buyers, sellers, hedges and
     mixed prints alike), the signal itself, SPY, and the same company's
     other share class (same sector and either a cap within 0.5% or a
     symbol pair like GOOG/GOOGL, FOX/FOXA, BRK.A/BRK.B).
     Market cap is point-in-time: today's cap scaled by the price ratio.
     Excess = sign x (signal return - equal-weight control return), both
     close-to-close from the same split-adjusted bar matrix.
  FROZEN, VERSIONED STORE. Control assignments live in
     data/uoa_matched_v2.json.gz keyed by unit and are assigned ONCE and
     never reassigned. v1 (data/uoa_matched.json.gz) is left untouched as
     an archive and no longer read: it stored controls as positions in a
     ticker list that changed every night (so later grades read the wrong
     stocks), keyed raw re-logged rows, dated signals by UTC date and only
     masked buyer tickers. Only the assignments are frozen; returns are
     recomputed from fresh split-adjusted bars on every run.
  INFERENCE. Cluster block bootstrap by (ISO week x sector); effective n
     published beside nominal n.
  COSTS. Net = gross minus modelled SHARE-trading costs (stock, not
     option): round trip = 2 x (half-spread + square-root impact). The
     Corwin-Schultz high/low estimate reads intraday volatility as spread
     (~10x real quoted spreads for liquid names), so it is clipped into a
     band set by 20-day dollar volume: $1B+ 1-2bp, $100M-1B 2-3bp,
     $20-100M 5-10bp, under $20M 15-25bp half-spread.
  SAME-ROWS SPY COMPARISON. sign x (signal return - SPY return) on exactly
     the rows of the matched headline, from the same price table, so the
     two numbers differ only by the benchmark.
  ENTRY-LAG LADDER. Over the last 45 days, on the common subset of units
     first flagged 9:30 AM-3:00 PM ET that have every entry price: the
     scanner's logged price (an up-to-15-min-delayed snapshot from before
     the alert existed — a reference, not an achievable fill), the first
     minute bar at the alert, +15 min, +60 min and the next open.
  REGIMES. VIX tercile against its own trailing year (point-in-time) x
     SPY 20-day trend.

Data: pg.grouped_daily (one call per trading day, whole market),
uoa_meta_cache (mcap/sector/earnings_date), the append-only ledger,
yfinance ^VIX. Output: docs/reports/uoa_scorecard.json.

    uv run uoa_scorecard.py                # full nightly build
    uv run uoa_scorecard.py --no-lag       # skip minute-bar sampling
    uv run uoa_scorecard.py --lag-budget N # ticker-days to fetch
    uv run uoa_scorecard.py --dry-run      # compute, write nothing
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import json
import math
import os
import random
import warnings
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np

import polygon_data as pg
from trade_desk_validation import _load
from uoa_alpha import BASIS_MAX_RATIO, _basis_ok, _dir_sign, _et_date, load_ledger, unique_alerts

_BASE = os.path.dirname(os.path.abspath(__file__))
R = lambda *p: os.path.join(_BASE, *p)
MATCH_PATH = R("data", "uoa_matched_v2.json.gz")
LEGACY_MATCH_PATH = R("data", "uoa_matched.json.gz")  # v1 archive: never read or written
LAG_CACHE = R("data", "uoa_entry_lag_cache.json")
OUT_PATH = R("docs", "reports", "uoa_scorecard.json")

SC_VERSION = "uoa_scorecard_v2"
MATCH_VERSION = 2
MATRIX_START = "2026-02-17"  # 63d momentum lookback before ledger start
VIX_START = "2025-01-02"  # a year of VIX history before the ledger, for point-in-time terciles
VIX_LOOKBACK = 252  # trading days in the trailing VIX window
VIX_MIN_HIST = 60
HORIZONS = (1, 3, 5, 10, 20)
BASE_H = 5
N_CTRL = 10
MIN_CTRL = 5
# (tier, same sector?, market-cap band) — tried in order until MIN_CTRL clean peers
MATCH_TIERS = (
    ("sector_3x", True, 3.0),
    ("sector_5x", True, 5.0),
    ("any_sector_3x", False, 3.0),
    ("any_sector_10x", False, 10.0),
)
TWIN_CAP_TOL = 0.005  # same sector + caps within 0.5% = same company, other share class
ERN_FRESH_DAYS = 3  # earnings dates are current-only, so the ERN bucket joins fresh units only
BOOT_ITERS = 2000
IMPACT_K = 0.2
CLIP_Q = 10_000.0  # $ baseline clip for net-EV modeling
CS_CLIP = 0.10  # raw Corwin-Schultz full-spread clip before the tier band
# Liquidity tiers by 20-day average dollar volume: (key, min $ADV,
# half-spread floor, half-spread cap), fractions of price.
LIQ_TIERS = (
    ("adv_1b_plus", 1e9, 0.0001, 0.0002),
    ("adv_100m_1b", 1e8, 0.0002, 0.0003),
    ("adv_20m_100m", 2e7, 0.0005, 0.0010),
    ("adv_under_20m", 0.0, 0.0015, 0.0025),
)
COST_LABEL = "net of modelled share-trading costs (stock, not option)"
LAG_WINDOW_D = 45
LAG_BUDGET = 250  # new ticker-days fetched per run
REG_OPEN_MIN = 9 * 60 + 30
REG_CLOSE_MIN = 16 * 60
LAG_LAST_FLAG_MIN = 15 * 60  # a 3:00 PM+ flag has no +60m bar before the close
LAG_RUNGS = ("logged_snapshot", "+0m", "+15m", "+60m", "next_open")
SHRINK_K_SIZING = 400
ET = ZoneInfo("America/New_York")


# ------------------------------------------------------------ time


def _et_dt(iso_ts):
    """Timezone-aware US/Eastern datetime of a UTC ISO stamp, or None."""
    try:
        dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
    except ValueError, TypeError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ET)


def _et_minute(iso_ts):
    """Minutes after midnight ET (DST-correct), or None."""
    dt = _et_dt(iso_ts)
    return None if dt is None else dt.hour * 60 + dt.minute


def _session(iso_ts):
    m = _et_minute(iso_ts)
    if m is None:
        return "unknown"
    if m < REG_OPEN_MIN:
        return "pre_open"
    if m < REG_CLOSE_MIN:
        return "regular"
    return "after_close"


def _close_ms(day):
    """Epoch ms of the 4:00 PM ET close on an ET calendar day."""
    d = date.fromisoformat(day)
    return int(datetime.combine(d, time(16, 0), tzinfo=ET).timestamp() * 1000)


# ------------------------------------------------------------ matrix


def _trading_days(start, end):
    """Actual trading days = dates where grouped_daily returns data.
    We just iterate calendar days and keep non-empty responses."""
    days = []
    d = datetime.strptime(start, "%Y-%m-%d").date()
    endd = datetime.strptime(end, "%Y-%m-%d").date()
    out = {}
    while d <= endd:
        if d.weekday() < 5:
            g = pg.grouped_daily(d.isoformat())
            if g:
                days.append(d.isoformat())
                out[d.isoformat()] = g
        d += timedelta(days=1)
    return days, out


def build_matrix(pool, today):
    """(dates, tickers, C, V, H, L, O) numpy matrices for the control pool
    + every ledger ticker + every stored control + SPY. NaN where missing."""
    days, raw = _trading_days(MATRIX_START, today)
    tickers = sorted(pool | {"SPY"})
    ti = {t: i for i, t in enumerate(tickers)}
    n_d, n_t = len(days), len(tickers)
    C = np.full((n_d, n_t), np.nan)
    V = np.full((n_d, n_t), np.nan)
    H = np.full((n_d, n_t), np.nan)
    L = np.full((n_d, n_t), np.nan)
    O = np.full((n_d, n_t), np.nan)
    for di, day in enumerate(days):
        g = raw[day]
        for t, i in ti.items():
            row = g.get(t)
            if row:
                C[di, i] = row.get("c") or np.nan
                V[di, i] = row.get("v") or np.nan
                H[di, i] = row.get("h") or np.nan
                L[di, i] = row.get("l") or np.nan
                O[di, i] = row.get("o") or np.nan
    return days, tickers, ti, C, V, H, L, O


def features_asof(di, C, V, H, L):
    """Cross-sectional features at date index di (using data <= di):
    mom63, vol20 (daily pct sd), adv20 ($), Corwin-Schultz half-spread
    (20d median, raw — see half_spread() for the tier band)."""
    with warnings.catch_warnings():  # all-NaN columns (new listings) are expected
        warnings.simplefilter("ignore", RuntimeWarning)
        return _features(di, C, V, H, L)


def _features(di, C, V, H, L):
    lo63 = max(0, di - 63)
    lo20 = max(0, di - 20)
    mom = C[di] / C[lo63] - 1.0
    rets = C[lo20 + 1 : di + 1] / C[lo20:di] - 1.0
    with np.errstate(invalid="ignore"):
        vol = np.nanstd(rets, axis=0)
        adv = np.nanmean((C * V)[lo20 : di + 1], axis=0)
    hs = np.full(C.shape[1], np.nan)
    Hh, Ll = H[lo20 : di + 1], L[lo20 : di + 1]
    with np.errstate(invalid="ignore", divide="ignore"):
        lhl = np.log(Hh / Ll) ** 2
        beta = lhl[:-1] + lhl[1:]
        h2 = np.fmax(Hh[:-1], Hh[1:])
        l2 = np.fmin(Ll[:-1], Ll[1:])
        gamma = np.log(h2 / l2) ** 2
        sq2 = math.sqrt(2.0)
        alpha = (np.sqrt(2 * beta) - np.sqrt(beta)) / (3 - 2 * sq2) - np.sqrt(gamma / (3 - 2 * sq2))
        s = 2 * (np.exp(alpha) - 1) / (1 + np.exp(alpha))
        s = np.clip(s, 0, CS_CLIP)
        hs = np.nanmedian(s, axis=0) / 2.0
    return mom, vol, adv, hs


def _sig_day_index(day, days):
    """Matrix index for an ET flag date (previous trading day if the date
    is a market holiday)."""
    i = bisect.bisect_right(days, day) - 1
    return i if i >= 0 else None


# ------------------------------------------------------------ ledger


def build_units(ledger):
    """Collapse the ledger to one scorecard unit per (ticker, ET date,
    direction). Returns (units, counts).

    Directional = calls or puts bought (uoa_alpha._dir_sign); premium
    selling, hedges and mixed prints have no stock direction to grade.
    Rows on a bad price basis (_basis_ok) are dropped. Scan re-logs collapse
    first to one alert per contract per ET day (unique_alerts, earliest flag
    kept), then alerts collapse to the stock-day-direction unit."""
    directional = [
        s
        for s in ledger
        if s.get("ticker") and s.get("flagged_at") and _dir_sign(s) is not None and _basis_ok(s)
    ]
    # the day's premium on a contract = the largest cumulative premium any scan saw
    prem_day = {}
    for s in directional:
        k = (s.get("contract") or s.get("id"), _et_date(s.get("flagged_at")))
        p = s.get("premium") or 0
        if p > prem_day.get(k, 0):
            prem_day[k] = p
    alerts = unique_alerts(directional)
    units = {}
    for a in alerts:
        day = _et_date(a["flagged_at"])
        sign = _dir_sign(a)
        direction = "bullish" if sign == 1 else "bearish"
        key = f"{a['ticker']}|{day}|{direction}"
        u = units.get(key)
        if u is None:
            u = units[key] = {
                "key": key,
                "tk": a["ticker"],
                "et_day": day,
                "dir": direction,
                "sign": 1.0 if sign == 1 else -1.0,
                "first_ts": a["flagged_at"],
                "first_px": a.get("underlying_px_at_flag"),
                "n_alerts": 0,
                "n_rows": 0,
                "score": None,
                "raw": None,
                "has_adj": False,
                "ern": False,
                "golden": False,
                "prem": 0.0,
            }
        u["n_alerts"] += 1
        u["n_rows"] += a.get("relogs") or 1
        if a["flagged_at"] < u["first_ts"]:
            u["first_ts"], u["first_px"] = a["flagged_at"], a.get("underlying_px_at_flag")
        sc = a.get("trade_score") or 0
        if u["score"] is None or sc > u["score"]:
            u["score"] = sc
            u["raw"] = sc - (a.get("edge_adj") or 0)
            u["has_adj"] = "edge_adj" in a
        u["ern"] = u["ern"] or "Into ERN" in (a.get("tags") or [])
        u["golden"] = u["golden"] or a.get("signal_type") == "golden_sweep"
        u["prem"] += prem_day.get((a.get("contract") or a.get("id"), day), a.get("premium") or 0)
    out = list(units.values())
    for u in out:
        u["session"] = _session(u["first_ts"])
    counts = {
        "ledger_directional_rows": len(directional),
        "unique_alerts": len(alerts),
        "ticker_days": len(out),
        "distinct_ticker_dates": len({(u["tk"], u["et_day"]) for u in out}),
        "first_flagged_after_close_pct": (
            round(100 * sum(u["session"] == "after_close" for u in out) / len(out), 1)
            if out
            else None
        ),
    }
    return out, counts


def flagged_by_day(ledger):
    """Every ticker with ANY ledger row on each ET date — buyers, sellers,
    hedges and mixed prints alike. None of them may serve as a control
    that day: a control must be a stock with no flagged flow."""
    out = defaultdict(set)
    for s in ledger:
        tk = s.get("ticker")
        if tk and s.get("flagged_at"):
            out[_et_date(s["flagged_at"])].add(tk)
    return out


# ----------------------------------------------------------- matching


def load_matched():
    """The v2 assignment store: {unit key: record}."""
    try:
        with gzip.open(MATCH_PATH, "rt", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {}
    return d.get("units") or {} if isinstance(d, dict) else {}


def save_matched(m):
    os.makedirs(os.path.dirname(MATCH_PATH), exist_ok=True)
    with gzip.open(MATCH_PATH, "wt", encoding="utf-8") as f:
        json.dump(
            {"v": MATCH_VERSION, "tiers": [t for t, _, _ in MATCH_TIERS], "units": m},
            f,
            separators=(",", ":"),
        )


def _ern_bucket(edate, day):
    try:
        dd = (
            datetime.strptime(edate, "%Y-%m-%d").date() - datetime.strptime(day, "%Y-%m-%d").date()
        ).days
    except TypeError, ValueError:
        return "unk"
    if 0 <= dd <= 5:
        return "0-5"
    if 5 < dd <= 20:
        return "6-20"
    return "far"


def _z(a):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        mu, sd = np.nanmean(a), np.nanstd(a)
    return (a - mu) / sd if sd and sd > 0 else a * 0


def _last_close(C):
    """Last finite close per column (NaN if none)."""
    out = np.full(C.shape[1], np.nan)
    for di in range(C.shape[0]):
        row = C[di]
        ok = np.isfinite(row)
        out[ok] = row[ok]
    return out


def assign_controls(units, days, tickers, ti, C, V, H, L, meta, matched, flagged, today=None):
    """Assign controls to units not yet in the v2 store. Assignments FREEZE:
    a unit is attempted once (matched or recorded unmatchable) and never
    reassigned. Units whose ET date is not yet in the matrix wait for a
    later run. Returns the number of units matched this run."""
    if not days:
        return 0
    today = today or datetime.now(ET).date().isoformat()
    m_now = np.array([(meta.get(t) or {}).get("mkt_cap") or np.nan for t in tickers], dtype=float)
    ctx = {
        "days": days,
        "tickers": tickers,
        "ti": ti,
        "m_now": m_now,
        "sec": np.array([(meta.get(t) or {}).get("sector") or "" for t in tickers], dtype=object),
        "edate": {t: (meta.get(t) or {}).get("earnings_date") for t in tickers},
        "twins": share_class_twins(tickers),
        "fresh": (date.fromisoformat(today) - timedelta(days=ERN_FRESH_DAYS)).isoformat(),
    }
    last_c = _last_close(C)
    spy = ti.get("SPY")

    flag_by_di = defaultdict(set)
    for day, tks in flagged.items():
        di = _sig_day_index(day, days)
        if di is not None and day <= days[-1]:
            flag_by_di[di] |= tks

    by_day = defaultdict(list)
    for u in units:
        if u["key"] in matched or u["et_day"] > days[-1]:
            continue
        di = _sig_day_index(u["et_day"], days)
        if di is not None:
            by_day[di].append(u)

    n_new = 0
    for di, group in sorted(by_day.items()):
        mom, vol, _, _ = features_asof(di, C, V, H, L)
        with np.errstate(invalid="ignore", divide="ignore"):
            m_pit = m_now * C[di] / last_c
        clean = np.isfinite(mom) & np.isfinite(vol) & np.isfinite(C[di]) & np.isfinite(m_pit)
        for t in flag_by_di.get(di, ()):
            j = ti.get(t)
            if j is not None:
                clean[j] = False
        if spy is not None:
            clean[spy] = False
        day_ctx = dict(ctx, di=di, zmom=_z(mom), zvol=_z(vol), m_pit=m_pit, clean=clean)
        for u in group:
            rec = _match_one(u["tk"], day_ctx)
            matched[u["key"]] = rec
            if rec.get("c"):
                n_new += 1
    return n_new


def share_class_twins(tickers):
    """{index: {indices}} of symbols that look like another share class of
    the same company: the same root before a '.' (BRK.A/BRK.B) or one extra
    trailing letter (GOOG/GOOGL, FOX/FOXA). Only applied within a sector; a
    false positive merely drops one candidate control."""
    ti = {t: i for i, t in enumerate(tickers)}
    out = defaultdict(set)
    roots = defaultdict(list)
    for t, i in ti.items():
        if "." in t:
            roots[t.split(".")[0]].append(i)
        j = ti.get(t[:-1]) if len(t) > 1 else None
        if j is not None:
            out[i].add(j)
            out[j].add(i)
    for root, idx in roots.items():
        if root in ti:
            idx = [*idx, ti[root]]
        for i in idx:
            out[i] |= {j for j in idx if j != i}
    return dict(out)


def _match_one(tk, x):
    """Controls for one unit from the day context x. Tiers are tried in
    order until MIN_CTRL clean peers exist; nearest by z-scored momentum
    and vol. Returns the record to freeze."""
    days, tickers, di = x["days"], x["tickers"], x["di"]
    zmom, zvol, m_pit, sec = x["zmom"], x["zvol"], x["m_pit"], x["sec"]
    rec: dict = {"d": days[di], "t": None, "c": []}
    si = x["ti"].get(tk)
    if si is None or not np.isfinite(zmom[si]) or not np.isfinite(zvol[si]):
        rec["why"] = "no_price_history"
        return rec
    sig_sec, cap = sec[si], m_pit[si]
    if not sig_sec or not np.isfinite(cap) or cap <= 0:
        rec["why"] = "no_cap_or_sector"
        return rec
    ok = x["clean"].copy()
    ok[si] = False
    same_sec = sec == sig_sec
    m_now = x["m_now"]
    with np.errstate(invalid="ignore", divide="ignore"):
        twin = same_sec & (np.abs(m_now / m_now[si] - 1.0) < TWIN_CAP_TOL)
    for j in x["twins"].get(si, ()):
        if same_sec[j]:
            twin[j] = True
    ok &= ~twin
    cand, tier = None, None
    for name, same_sector, band in MATCH_TIERS:
        m = ok & (m_pit >= cap / band) & (m_pit <= cap * band)
        if same_sector:
            m &= same_sec
        idx = np.flatnonzero(m)
        if len(idx) >= MIN_CTRL:
            cand, tier = idx, name
            break
    if cand is None:
        rec["why"] = "too_few_clean_peers"
        return rec
    e = 0
    edate = x["edate"]
    b = _ern_bucket(edate.get(tk), days[di])
    if days[di] >= x["fresh"] and b != "unk":  # no earnings date -> nothing to match on
        c2 = [int(j) for j in cand if _ern_bucket(edate.get(tickers[j]), days[di]) == b]
        if len(c2) >= MIN_CTRL:
            cand, e = np.array(c2, dtype=int), 1
    dist = (zmom[cand] - zmom[si]) ** 2 + (zvol[cand] - zvol[si]) ** 2
    order = np.argsort(dist, kind="stable")[:N_CTRL]
    rec["t"] = tier
    rec["c"] = [tickers[int(cand[k])] for k in order]
    rec["e"] = e
    return rec


def excess_at(C, di, h, si, cj, sign, min_ctrl=MIN_CTRL):
    """sign x (signal return - equal-weight benchmark return) in %, close
    to close from di to di+h; None if not matured or too few benchmark
    prices."""
    if di + h >= C.shape[0] or not len(cj):
        return None
    c0, ch = C[di, si], C[di + h, si]
    if not (np.isfinite(c0) and np.isfinite(ch)) or c0 <= 0:
        return None
    idx = np.asarray(cj, dtype=int)
    with np.errstate(invalid="ignore", divide="ignore"):
        legs = C[di + h, idx] / C[di, idx] - 1.0
    legs = legs[np.isfinite(legs)]
    if len(legs) < min_ctrl:
        return None
    return round(100 * sign * ((ch / c0 - 1.0) - float(legs.mean())), 3)


def grade_units(units, matched, days, ti, C):
    """Matched excess and same-rows excess vs SPY per horizon for every
    matched unit. Recomputed from the current split-adjusted matrix each
    run — only the control assignments are frozen."""
    dix = {d: i for i, d in enumerate(days)}
    spy = ti.get("SPY")
    out = []
    for u in units:
        rec = matched.get(u["key"])
        if not rec or not rec.get("c"):
            continue
        di, si = dix.get(rec["d"]), ti.get(u["tk"])
        if di is None or si is None:
            continue
        cj = [ti[c] for c in rec["c"] if c in ti]
        x, xs = {}, {}
        for h in HORIZONS:
            v = excess_at(C, di, h, si, cj, u["sign"])
            if v is not None:
                x[h] = v
                if spy is not None:
                    w = excess_at(C, di, h, si, [spy], u["sign"], min_ctrl=1)
                    if w is not None:
                        xs[h] = w
        out.append(dict(u, day=rec["d"], di=di, si=si, tier=rec.get("t"), x=x, spy=xs))
    return out


def match_coverage(units, matched):
    """How many attempted units found controls, by tier."""
    att = [matched[u["key"]] for u in units if u["key"] in matched]
    ok = [r for r in att if r.get("c")]
    tiers = Counter(r.get("t") for r in ok)
    return {
        "version": MATCH_VERSION,
        "units_attempted": len(att),
        "assigned": len(ok),
        "unmatchable": len(att) - len(ok),
        "coverage_pct": round(100 * len(ok) / len(att), 1) if att else None,
        "by_tier": {t: tiers.get(t, 0) for t, _, _ in MATCH_TIERS},
        "unmatched_reasons": dict(
            Counter(r.get("why") or "unknown" for r in att if not r.get("c"))
        ),
        "earnings_bucket_matched": sum(1 for r in ok if r.get("e")),
    }


# ------------------------------------------------------------- stats


def _stats(vals):
    v = [x for x in vals if x is not None]
    if not v:
        return {"n": 0}
    a = np.array(v)
    return {
        "n": len(v),
        "avg": round(float(a.mean()), 3),
        "med": round(float(np.median(a)), 3),
        "hit": round(100 * float((a > 0).mean())),
    }


def cluster_boot(rows, iters=BOOT_ITERS, seed=7):
    """rows: (cluster_key, value). Block bootstrap by cluster.
    Returns CI, #clusters and variance-ratio effective n."""
    cl = defaultdict(list)
    for k, v in rows:
        cl[k].append(v)
    keys = list(cl)
    vals = np.array([v for _, v in rows], dtype=float)
    if len(keys) < 8 or len(vals) < 50:
        return {"status": "insufficient_clusters", "clusters": len(keys)}
    rng = random.Random(seed)
    means = []
    for _ in range(iters):
        acc = []
        for _ in range(len(keys)):
            acc.extend(cl[rng.choice(keys)])
        means.append(float(np.mean(acc)))
    means.sort()
    se_boot = float(np.std(means))
    sd = float(vals.std())
    n_eff = int((sd / se_boot) ** 2) if se_boot > 0 else None
    return {
        "clusters": len(keys),
        "ci95": [round(means[int(iters * 0.025)], 3), round(means[int(iters * 0.975)], 3)],
        "se_cluster": round(se_boot, 4),
        "n_eff": n_eff,
    }


def _week(day):
    d = datetime.strptime(day, "%Y-%m-%d").date().isocalendar()
    return f"{d[0]}w{d[1]:02d}"


def _spearman(xs, ys):
    xa, ya = np.array(xs, dtype=float), np.array(ys, dtype=float)

    def rank(a):
        order = a.argsort()
        r = np.empty_like(order, dtype=float)
        r[order] = np.arange(len(a))
        # average ties
        _, inv, cnt = np.unique(a, return_inverse=True, return_counts=True)
        sums = np.zeros(len(cnt))
        np.add.at(sums, inv, r)
        return sums[inv] / cnt[inv]

    rx, ry = rank(xa), rank(ya)
    rx -= rx.mean()
    ry -= ry.mean()
    den = math.sqrt((rx**2).sum() * (ry**2).sum())
    return round(float((rx * ry).sum() / den), 4) if den else 0.0


def vix_regimes_pit(dates, values, lookback=VIX_LOOKBACK, min_hist=VIX_MIN_HIST):
    """{date: low|mid|high}: each day's VIX close against the terciles of
    its own trailing `lookback` closes (that day included) — no later data
    enters a label. Days with less than `min_hist` history are unlabelled."""
    out = {}
    for i, (d, v) in enumerate(zip(dates, values, strict=True)):
        win = values[max(0, i - lookback + 1) : i + 1]
        if len(win) < min_hist:
            continue
        t1, t2 = np.percentile(win, [100 / 3, 200 / 3])
        out[d] = "low" if v < t1 else "mid" if v < t2 else "high"
    return out


# ------------------------------------------------------------- costs


def liquidity_tier(adv_d):
    """Tier key from 20-day average dollar volume, or None if unknown."""
    if adv_d is None or not np.isfinite(adv_d) or adv_d <= 0:
        return None
    for key, lo, _, _ in LIQ_TIERS:
        if adv_d >= lo:
            return key
    return LIQ_TIERS[-1][0]


def half_spread(cs_hs, adv_d):
    """Modelled half-spread (fraction of price): the Corwin-Schultz
    estimate clipped into its liquidity tier's band. A missing estimate
    takes the band's cap (the conservative end)."""
    tier = liquidity_tier(adv_d)
    if tier is None:
        return None
    _, _, lo, hi = next(t for t in LIQ_TIERS if t[0] == tier)
    if cs_hs is None or not np.isfinite(cs_hs):
        return hi
    return min(max(float(cs_hs), lo), hi)


def cost_model(cs_hs, vol_d, adv_d, q=CLIP_Q, k=IMPACT_K):
    """Round-trip SHARE-trading cost in return %: 2 x (modelled half-spread
    + square-root impact k*sigma_d*sqrt(Q/ADV)). Stock only — the option's
    own bid/ask is not modelled. None when vol or ADV is unknown."""
    if vol_d is None or adv_d is None or not (np.isfinite(vol_d) and np.isfinite(adv_d)):
        return None
    if adv_d <= 0:
        return None
    hs = half_spread(cs_hs, adv_d)
    if hs is None:
        return None
    impact = k * float(vol_d) * math.sqrt(q / float(adv_d))
    return round(100 * 2 * (hs + impact), 4)


def capacity_q(edge_pct, hs, vol_d, adv_d, k=IMPACT_K):
    """$ per-name SHARE clip at which round-trip square-root impact eats
    half of the edge left after the modelled spread:
    2*k*sigma*sqrt(Q/ADV) = 0.5*(edge - 2*hs)
      -> Q = ADV * ((edge - 2*hs) / (4*k*sigma))**2.
    None when the spread alone eats the edge or inputs are missing."""
    if hs is None or not vol_d or not adv_d or vol_d <= 0 or adv_d <= 0:
        return None
    room = edge_pct / 100.0 - 2 * hs
    if room <= 0:
        return None
    return adv_d * (room / (4 * k * vol_d)) ** 2


# --------------------------------------------------------- entry lag


def _bar_at(mins, target_ms, close_ms):
    """Close of the first regular-session minute bar starting at/after
    target_ms and before the close; None if there is none."""
    best = None
    for t, c in mins.items():
        t = int(t)
        if target_ms <= t < close_ms and (best is None or t < best[0]):
            best = (t, c)
    return best[1] if best else None


def lag_candidates(rows, cutoff):
    """Units eligible for the ladder: first flagged on/after `cutoff`
    (ET), graded at +BASE_H, during 9:30 AM-3:00 PM ET on the anchor
    trading day (later flags have no +60m bar before the close)."""
    out = []
    for r in rows:
        if r["et_day"] < cutoff or BASE_H not in r["x"] or r["day"] != r["et_day"]:
            continue
        m = _et_minute(r["first_ts"])
        if m is None or not (REG_OPEN_MIN <= m < LAG_LAST_FLAG_MIN):
            continue
        out.append(r)
    return out


def compute_ladder(cands, cache, days, C, O):
    """Matched EV to the +BASE_H close by entry point, on the COMMON subset
    of units that have every entry price, so every rung has the same n.
    Exit and control leg are fixed; the ladder isolates the entry alone."""
    ladders = {k: [] for k in LAG_RUNGS}
    excluded = Counter()
    used = []
    for r in cands:
        mins = cache.get(f"{r['tk']}|{r['et_day']}")
        if not mins:
            excluded["no minute bars"] += 1
            continue
        dt = _et_dt(r["first_ts"])
        di, si = r["di"], r["si"]
        if dt is None or si is None or di + BASE_H >= len(days):
            excluded["exit not settled"] += 1
            continue
        exit_px, c0 = C[di + BASE_H, si], C[di, si]
        if not (np.isfinite(exit_px) and np.isfinite(c0)) or c0 <= 0:
            excluded["exit not settled"] += 1
            continue
        ts = int(dt.timestamp() * 1000)
        close = _close_ms(r["et_day"])
        nxt = O[di + 1, si] if di + 1 < len(days) else np.nan
        entries = {
            "logged_snapshot": r.get("first_px"),
            "+0m": _bar_at(mins, ts, close),
            "+15m": _bar_at(mins, ts + 15 * 60_000, close),
            "+60m": _bar_at(mins, ts + 60 * 60_000, close),
            "next_open": float(nxt) if np.isfinite(nxt) else None,
        }
        if any(not e or e <= 0 for e in entries.values()):
            excluded["missing an entry price"] += 1
            continue
        if any(
            not (1 / BASIS_MAX_RATIO <= float(e) / float(c0) <= BASIS_MAX_RATIO)
            for e in entries.values()
        ):
            excluded["price basis mismatch"] += 1
            continue
        sign = r["sign"]
        ctrl_leg = 100 * sign * (float(exit_px) / float(c0) - 1.0) - r["x"][BASE_H]
        for name, e in entries.items():
            ladders[name].append(100 * sign * (float(exit_px) / float(e) - 1.0) - ctrl_leg)
        used.append(r)
    return {
        "units": len(used),
        "ticker_days": len({(r["tk"], r["et_day"]) for r in used}),
        "ladder": {k: _stats(v) for k, v in ladders.items()},
        "excluded": dict(excluded),
    }


def entry_lag(rows, days, C, O, budget, today, write=True):
    """Fetch minute bars (incremental cache, earnings first) for eligible
    units and compute the ladder. The cache keeps only the window."""
    cache = _load(LAG_CACHE, {}) or {}
    cutoff = (date.fromisoformat(today) - timedelta(days=LAG_WINDOW_D)).isoformat()
    cache = {k: v for k, v in cache.items() if k.split("|")[-1] >= cutoff}  # outside the window
    cands = lag_candidates(rows, cutoff)
    random.Random(11).shuffle(cands)
    cands.sort(key=lambda r: not r["ern"])  # earnings first
    fetched = 0
    for r in cands:
        key = f"{r['tk']}|{r['et_day']}"
        if key in cache or fetched >= budget:
            continue
        fetched += 1
        try:
            data = pg._get(
                f"/v2/aggs/ticker/{r['tk']}/range/1/minute/{r['et_day']}/{r['et_day']}",
                {"limit": 1000},
            )
            res = (data or {}).get("results") or []
            cache[key] = {str(b["t"]): b["c"] for b in res if b.get("t") and b.get("c")}
        except Exception:
            cache[key] = {}
    if write:
        with open(LAG_CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f, separators=(",", ":"))
    out = compute_ladder(cands, cache, days, C, O)
    used_days = sorted(r["et_day"] for r in cands)
    out.update(
        {
            "window_days": LAG_WINDOW_D,
            "window": {"from": cutoff, "to": used_days[-1] if used_days else None},
            "eligible_units": len(cands),
            "new_fetched": fetched,
            "rungs": {
                "logged_snapshot": "the underlying price the scanner logged with the first "
                "alert: a snapshot up to ~15 min delayed, i.e. the market BEFORE the alert "
                "existed. A reference point, not a fill anyone could get.",
                "+0m": "first minute bar at the first alert's time",
                "+15m": "first minute bar 15 min after the alert",
                "+60m": "first minute bar 60 min after the alert",
                "next_open": "next session's official open",
            },
            "note": "one unit per stock, ET day and direction, first flagged 9:30 AM-3:00 PM "
            "ET within the last 45 days; every rung is computed on the same units. Exit "
            "(+5d close) and control leg are fixed, so the ladder isolates the entry "
            "price. Minute bars are fetched earnings-first when the budget binds — compare "
            "rungs with each other, not with the pockets above.",
        }
    )
    return out


# ---------------------------------------------------------------- run


def _fetch_vix():
    try:
        import yfinance as yf

        vs = yf.download("^VIX", start=VIX_START, progress=False, auto_adjust=False)["Close"]
        try:
            vs = vs.squeeze()
        except Exception:
            pass
        vs = vs.dropna()
        return [str(i)[:10] for i in vs.index], [float(v) for v in vs.values]
    except Exception as e:
        print("VIX fetch failed:", str(e)[:80])
        return [], []


def run(no_lag=False, lag_budget=LAG_BUDGET, dry=False):
    if not pg.available():
        out = {
            "generated": datetime.now(UTC).isoformat(timespec="seconds"),
            "status": "capability_unavailable",
            "why": "POLYGON_API_KEY not set",
        }
        if not dry:
            with open(OUT_PATH, "w", encoding="utf-8") as f:
                json.dump(out, f, indent=1)
        print(out["why"])
        return out

    today = datetime.now(ET).date().isoformat()
    meta = _load(R("docs", "reports", "uoa_meta_cache.json"), {}) or {}
    pool = {t for t, v in meta.items() if v.get("mkt_cap") and v.get("sector")}
    ledger = load_ledger()
    units, counts = build_units(ledger)
    flagged = flagged_by_day(ledger)
    matched = load_matched()
    stored = {c for rec in matched.values() for c in rec.get("c") or ()}
    print(
        f"pool {len(pool)} | {counts['ledger_directional_rows']} directional rows -> "
        f"{counts['unique_alerts']} alerts -> {counts['ticker_days']} ticker-days"
    )

    days, tickers, ti, C, V, H, L, O = build_matrix(pool | {u["tk"] for u in units} | stored, today)
    print(f"matrix: {len(days)} trading days x {len(tickers)} tickers")

    n_new = assign_controls(units, days, tickers, ti, C, V, H, L, meta, matched, flagged, today)
    if not dry:
        save_matched(matched)
    coverage = match_coverage(units, matched)
    print(f"controls: +{n_new} assigned this run; coverage {coverage['coverage_pct']}%")

    sector = {t: (meta.get(t) or {}).get("sector") for t in tickers}
    rows = [r for r in grade_units(units, matched, days, ti, C) if BASE_H in r["x"]]
    print(f"panel rows (ticker-days matched, graded +{BASE_H}d): {len(rows)}")

    # per-unit cost + features at the anchor date
    day_feat = {}
    for r in rows:
        r["sector"] = sector.get(r["tk"]) or "unk"
        if r["di"] not in day_feat:
            day_feat[r["di"]] = features_asof(r["di"], C, V, H, L)
        _, vol, adv, cs = day_feat[r["di"]]
        j = r["si"]
        r["vold"] = float(vol[j]) if np.isfinite(vol[j]) else None
        r["adv"] = float(adv[j]) if np.isfinite(adv[j]) else None
        r["liq"] = liquidity_tier(r["adv"])
        r["hs"] = half_spread(float(cs[j]), r["adv"]) if r["adv"] else None
        r["cost"] = cost_model(float(cs[j]), r["vold"], r["adv"])
        r["net"] = round(r["x"][BASE_H] - r["cost"], 3) if r["cost"] is not None else None
        r["premadv"] = (r["prem"] / r["adv"]) if r["adv"] else None

    def pocket(rs, name):
        st = _stats([r["x"][BASE_H] for r in rs])
        st["n_alerts"] = sum(r["n_alerts"] for r in rs)
        st["n_rows"] = sum(r["n_rows"] for r in rs)  # ledger rows behind these ticker-days
        if st["n"] >= 50:
            st["bootstrap"] = cluster_boot(
                [(_week(r["day"]) + "|" + r["sector"], r["x"][BASE_H]) for r in rs]
            )
        st["net"] = _stats([r["net"] for r in rs if r["net"] is not None])
        by_liq = {}
        for key, _, lo, hi in LIQ_TIERS:
            sub = [r for r in rs if r["liq"] == key]
            hss = [r["hs"] for r in sub if r["hs"] is not None]
            by_liq[key] = {
                "half_spread_band_bp": [round(lo * 1e4, 1), round(hi * 1e4, 1)],
                "median_half_spread_bp": round(float(np.median(hss)) * 1e4, 2) if hss else None,
                "gross": _stats([r["x"][BASE_H] for r in sub]),
                "net": _stats([r["net"] for r in sub if r["net"] is not None]),
            }
        st["by_liquidity"] = by_liq
        st["name"] = name
        return st

    pockets = {
        "overall": pocket(rows, "overall"),
        "into_earnings": pocket([r for r in rows if r["ern"]], "into_earnings"),
        "golden_sweeps": pocket([r for r in rows if r["golden"]], "golden_sweeps"),
        "score_80_plus": pocket([r for r in rows if r["score"] >= 80], "score_80_plus"),
        "put_buys": pocket([r for r in rows if r["dir"] == "bearish"], "put_buys"),
        "call_buys": pocket([r for r in rows if r["dir"] == "bullish"], "call_buys"),
    }
    sessions = {
        k: _stats([r["x"][BASE_H] for r in rows if r["session"] == k])
        for k in ("regular", "after_close")
    }

    # ---- headline: vs SPY on exactly the matched rows ----
    both = [r for r in rows if BASE_H in r["spy"]]
    vs_spy = _stats([r["spy"][BASE_H] for r in both])
    m_same = _stats([r["x"][BASE_H] for r in both])
    headline = {
        "n": vs_spy.get("n", 0),
        "vs_spy_5d": vs_spy.get("avg"),
        "matched_5d": m_same.get("avg"),
        "benchmark_gap_5d": (
            round(vs_spy["avg"] - m_same["avg"], 3) if vs_spy.get("n") and m_same.get("n") else None
        ),
        "matched_into_earnings_5d": pockets["into_earnings"].get("avg"),
        "note": "both numbers are sign x (stock return - benchmark return), close to close "
        "over the same 5 sessions, on the same ticker-days and the same price table; only "
        "the benchmark differs (SPY vs matched controls). The gap is what similar stocks "
        "did anyway: beta, momentum-universe drift and event premium.",
    }

    # ---- IC panel (matched excess) ----
    def ic_block(rs, key):
        out = {}
        for h in HORIZONS:
            sub = [(r[key], r["x"][h]) for r in rs if h in r["x"]]
            if len(sub) >= 500:
                out[f"+{h}d"] = _spearman([a for a, _ in sub], [b for _, b in sub])
        return out

    adj_rows = [r for r in rows if r["has_adj"]]
    vdates, vvals = _fetch_vix()
    vix = vix_regimes_pit(vdates, vvals)
    spy_i = ti.get("SPY")
    spy_trend = {}
    if spy_i is not None:
        for di in range(20, len(days)):
            spy_trend[days[di]] = "up" if C[di, spy_i] >= C[di - 20, spy_i] else "down"

    def regime_of(day):
        return f"vix_{vix.get(day, 'unk')}|spy_{spy_trend.get(day, 'unk')}"

    ic_regime = {}
    for r in adj_rows:
        ic_regime.setdefault(regime_of(r["day"]), []).append(r)
    ic_by_regime = {
        k: {"n": len(v), "ic": _spearman([r["score"] for r in v], [r["x"][BASE_H] for r in v])}
        for k, v in ic_regime.items()
        if len(v) >= 500
    }

    dec = {}
    if len(adj_rows) >= 2000:
        srt = sorted(adj_rows, key=lambda r: r["score"])
        n10 = len(srt) // 10
        dtab = []
        for dgt in range(10):
            seg = srt[dgt * n10 : (dgt + 1) * n10 if dgt < 9 else len(srt)]
            dtab.append(
                {
                    "d": dgt + 1,
                    "avg": round(float(np.mean([r["x"][BASE_H] for r in seg])), 3),
                    "hit": round(100 * float(np.mean([r["x"][BASE_H] > 0 for r in seg]))),
                }
            )
        dec = {
            "deciles": dtab,
            "top_minus_bottom": round(dtab[-1]["avg"] - dtab[0]["avg"], 3),
            "monotonic_steps": sum(1 for i in range(9) if dtab[i + 1]["avg"] >= dtab[i]["avg"]),
        }

    ic_panel = {
        "adjusted_score": {
            "n": len(adj_rows),
            "note": "one value per ticker-day: the highest trade_score any of its alerts "
            "reached (trade_score bakes in edge_adj at flag time, point-in-time); raw = "
            "that alert's trade_score - edge_adj",
            "matched_ic_by_horizon": ic_block(adj_rows, "score"),
            "raw_matched_ic_by_horizon": ic_block(adj_rows, "raw"),
            "by_regime": ic_by_regime,
            "regime_note": "VIX tercile vs its own trailing 252 sessions (point-in-time) x "
            "SPY above/below its level 20 sessions earlier",
            "decile_matched": dec,
        }
    }

    # ---- calibration ----
    cutoff90 = (date.fromisoformat(today) - timedelta(days=90)).isoformat()
    recent = [r for r in adj_rows if r["day"] >= cutoff90]
    rel = []
    if len(recent) >= 1000:
        srt = sorted(recent, key=lambda r: r["score"])
        nb = len(srt) // 10
        for b in range(10):
            seg = srt[b * nb : (b + 1) * nb if b < 9 else len(srt)]
            boot = cluster_boot(
                [
                    (_week(r["day"]) + "|" + r["sector"], 1.0 if r["x"][BASE_H] > 0 else 0.0)
                    for r in seg
                ],
                iters=500,
                seed=b,
            )
            ci = boot.get("ci95")
            rel.append(
                {
                    "bin": b + 1,
                    "n": len(seg),
                    "score_lo": seg[0]["score"],
                    "score_hi": seg[-1]["score"],
                    "hit_pct": round(100 * float(np.mean([r["x"][BASE_H] > 0 for r in seg]))),
                    "ci95_pct": [round(100 * ci[0], 1), round(100 * ci[1], 1)] if ci else None,
                }
            )
    base_by_regime = {}
    for k, v in ic_regime.items():
        if len(v) >= 300:
            base_by_regime[k] = {
                "n": len(v),
                "hit": round(100 * float(np.mean([r["x"][BASE_H] > 0 for r in v]))),
                "avg": round(float(np.mean([r["x"][BASE_H] for r in v])), 3),
            }
    n_recent = len(recent)
    calibration = {
        "reliability_rolling90": rel,
        "regime_base_rates": base_by_regime,
        "sizing_shrinkage": {
            "w_recent": round(n_recent / (n_recent + SHRINK_K_SIZING), 3),
            "rule": "sizing weight = w*recent + (1-w)*trained, "
            f"w = n_recent/(n_recent+{SHRINK_K_SIZING})",
        },
    }

    # ---- crowding ----
    def crowd(rs):
        buckets = {"<0.1%": [], "0.1-0.5%": [], "0.5-2%": [], ">2%": []}
        for r in rs:
            pa = r["premadv"]
            if pa is None:
                continue
            b = (
                "<0.1%"
                if pa < 0.001
                else "0.1-0.5%"
                if pa < 0.005
                else "0.5-2%"
                if pa < 0.02
                else ">2%"
            )
            buckets[b].append(r["x"][BASE_H])
        return {b: _stats(v) for b, v in buckets.items()}

    tod = defaultdict(list)
    for r in rows:
        dt = _et_dt(r["first_ts"])
        if dt is not None:
            tod[f"{dt.hour:02d}h"].append(r["x"][BASE_H])
    crowding = {
        "ev_vs_premium_over_adv": {
            "all": crowd(rows),
            "golden_sweeps": crowd([r for r in rows if r["golden"]]),
            "score_80_plus": crowd([r for r in rows if r["score"] >= 80]),
        },
        "ev_by_hour_et": {k: _stats(v) for k, v in sorted(tod.items()) if len(v) >= 200},
        "note": "premium = the day's total flagged premium on the stock in that direction "
        "(largest cumulative premium per contract, summed); hour = first flag, US/Eastern",
        "hypothesis": "if EV falls as premium/ADV conspicuousness "
        "rises, the best-looking prints are crowded and "
        "the quiet qualifying signals carry the edge",
    }

    # ---- capacity (shares) ----
    def capacity(rs, name):
        edge = (pockets.get(name) or {}).get("avg") or 0
        if edge <= 0:
            return {"edge_nonpositive": True}
        qs = [q for r in rs if (q := capacity_q(edge, r["hs"], r["vold"], r["adv"])) is not None]
        if not qs:
            return {"n": 0, "spread_eats_edge": True}
        qa = np.array(qs)
        return {
            "n_names_with_room": len(qs),
            "n": len(rs),
            "per_name_q50_$": int(np.median(qa)),
            "per_name_q25_$": int(np.percentile(qa, 25)),
            "note": "SHARE capacity (stock, not option): $ per name per trade at which "
            "round-trip square-root impact eats half of the matched edge left after the "
            "modelled spread",
        }

    cap_panel = {
        "overall": capacity(rows, "overall"),
        "into_earnings": capacity([r for r in rows if r["ern"]], "into_earnings"),
        "put_buys": capacity([r for r in rows if r["dir"] == "bearish"], "put_buys"),
    }

    # ---- book simulation ----
    def book(rs):
        # one unit per ticker-day-direction (already), hold BASE_H days,
        # matched excess spread evenly across the window minus costs
        # amortized at entry. Daily P&L approximated as x5/BASE_H per open
        # position (documented approximation).
        daily = defaultdict(list)
        dcost = defaultdict(list)
        for r in rs:
            per_day = r["x"][BASE_H] / BASE_H
            costadj = (r["cost"] or 0) / BASE_H
            for d2 in range(r["di"] + 1, min(r["di"] + 1 + BASE_H, len(days))):
                daily[d2].append(per_day - costadj)
                dcost[d2].append(costadj)
        if not daily:
            return {"n_days": 0}
        eq, peak, dd, uw, uw_max, cost_tot = 0.0, 0.0, 0.0, 0, 0, 0.0
        months = defaultdict(float)
        for di in sorted(daily):
            ret = float(np.mean(daily[di]))
            cost_tot += float(np.mean(dcost[di]))
            eq += ret
            months[days[di][:7]] += ret
            if eq >= peak:
                peak, uw = eq, 0
            else:
                uw += 1
                uw_max = max(uw_max, uw)
                dd = min(dd, eq - peak)
        worst_m = min(months.items(), key=lambda kv: kv[1]) if months else (None, 0)
        return {
            "n_days": len(daily),
            "total_matched_excess_pp": round(eq, 2),
            "modelled_cost_pp": round(cost_tot, 2),
            "max_drawdown_pp": round(dd, 2),
            "longest_underwater_days": uw_max,
            "worst_month": {"month": worst_m[0], "pp": round(worst_m[1], 2)},
            "note": f"equal-weight, {COST_LABEL}, hold-5d overlap; per-day P&L "
            "approximated as x5/5 per open position",
        }

    book_panel = {
        "overall": book(rows),
        "into_earnings": book([r for r in rows if r["ern"]]),
    }

    # ---- entry-lag ladder ----
    lag_panel = (
        {"status": "skipped"}
        if no_lag
        else entry_lag(rows, days, C, O, 0 if dry else lag_budget, today, write=not dry)
    )

    result = {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "version": SC_VERSION,
        "matrix": {
            "days": len(days),
            "tickers": len(tickers),
            "from": days[0] if days else None,
            "to": days[-1] if days else None,
        },
        "counts": counts,
        "matched": dict(
            coverage,
            graded_5d=len(rows),
            n_unique=len(rows),
            unit="one row per ticker, US/Eastern date and direction",
        ),
        "session_split": sessions,
        "headline": headline,
        "pockets": pockets,
        "ic": ic_panel,
        "calibration": calibration,
        "crowding": crowding,
        "capacity": cap_panel,
        "book_sim": book_panel,
        "entry_lag": lag_panel,
        "matching": {
            "tiers": [
                {"tier": t, "same_sector": s, "cap_band": f"{b:g}x"} for t, s, b in MATCH_TIERS
            ],
            "excluded_controls": "every ticker with any ledger row that ET day (buyers, "
            "sellers, hedges, mixed), the signal itself, its other share class, SPY",
            "store": "data/uoa_matched_v2.json.gz — assigned once per ticker-day, never "
            "reassigned; v1 (data/uoa_matched.json.gz) archived, not read",
        },
        "cost_model": {
            "label": COST_LABEL,
            "half_spread": "Corwin-Schultz 20-day median estimate clipped into a band set by "
            "20-day average dollar volume (missing estimate = top of the band)",
            "bands": [
                {
                    "tier": k,
                    "min_adv_$": int(lo),
                    "half_spread_bp": [round(a * 1e4, 1), round(b * 1e4, 1)],
                }
                for k, lo, a, b in LIQ_TIERS
            ],
            "impact": f"k*sigma_d*sqrt(Q/ADV), k={IMPACT_K}, Q=${int(CLIP_Q):,}",
            "roundtrip": "2x(half_spread + impact), shares only",
        },
        "promotion_gates": {
            "spec": [
                "positive matched-excess NET-of-cost EV over >= 8 unseen weeks",
                "cluster-corrected |t| > 2 on effective n",
                "monotone decile spread OOS if the change touches ranking",
                "no degradation in the worst regime tercile",
            ],
            "meta_kpi": "publish promoted/demoted counts as they "
            "accrue (none yet under these gates)",
        },
        "honesty": (
            "One row per stock, day and direction: scan re-logs and several contracts on "
            "the same stock and day count once. Controls come from fallback tiers (same "
            "sector first; cross-sector when a mega cap has too few clean peers) and every "
            "ticker flagged that day is excluded; sector comes from the current meta "
            "snapshot and market cap is today's cap scaled by price. Earnings-date matching "
            "applies only to ticker-days matched within 3 days of the flag. Returns are "
            "close to close, so a ticker-day first flagged after the close is graded from a "
            "close that came before the alert; the entry-lag ladder shows achievable "
            "entries. Net figures subtract modelled share-trading costs (stock, not option: "
            "tiered half-spread + square-root impact), not real fills. Cluster unit = ISO "
            "week x sector; effective n is a variance-ratio estimate."
        ),
    }
    if not dry:
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1)
    print("headline:", json.dumps(result["headline"]))
    print(
        "overall:",
        json.dumps(
            {k: pockets["overall"].get(k) for k in ("n", "avg", "med", "hit", "bootstrap", "net")}
        ),
    )
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-lag", action="store_true")
    ap.add_argument("--lag-budget", type=int, default=LAG_BUDGET)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    run(no_lag=a.no_lag, lag_budget=a.lag_budget, dry=a.dry_run)
