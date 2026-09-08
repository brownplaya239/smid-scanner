"""
uoa_scorecard.py — matched-control flow scorecard (2026-09 redesign).

The prior scorecard measured "did flagged names beat SPY", which
conflates beta, momentum-universe drift, earnings-vol premium and
actual flow information. This module redesigns every KPI so the only
thing that can make it positive is information in the flow itself.

  #1 MATCHED-CONTROL BENCHMARK (the headline). For each graded signal,
     at signal date: up to 10 control stocks from the same liquid
     universe, same sector, market cap within a band, nearest by
     z-scored (63d momentum, 20d realized vol) — controls that had ANY
     flow signal that day, or are the signal's own ticker, are
     excluded. Excess = signal return - equal-weight control return,
     both close-anchored over the same window, direction-signed.
     Controls inherit the signal's beta, momentum-universe drift and
     (forward assignments, match v2) its days-to-earnings bucket.
  #2 INFERENCE: cluster block bootstrap by (ISO week x sector) — a
     week of signals in one sector is ~one effective observation for
     market-move purposes. Effective n published beside nominal n.
  #3 IC on the score actually used: ledger trade_score already bakes
     in the learned edge_adj at flag time (point-in-time by
     construction; raw = trade_score - edge_adj). Both ICs reported,
     on MATCHED excess, with horizon decay and regime cuts
     (VIX terciles x SPY 20d trend).
  #4 NET-OF-FRICTION EV: modeled round trip = 2 x (Corwin-Schultz
     half-spread estimate + square-root impact k*sigma*sqrt(Q/ADV),
     k=0.2, Q=$10k baseline clip). Spread-bucket cuts on every pocket.
  #5 ENTRY-LAG LADDER: EV entered at signal / +15min / +60min / next
     open (minute aggregates, incremental nightly sample) — answers
     "is the edge a latency race" directly.
  #6 CALIBRATION split: reliability curve (rolling 90d, cluster
     errors) separate from regime-conditional base rates; shrinkage
     sizing weight w = n_recent/(n_recent+K) published, not vibes.
  #7 NEW KPIs: turnover-adjusted capacity per pocket (Q* where impact
     eats 50% of gross edge); simulated equal-weight book drawdown
     profile (maxDD, underwater, worst month); crowding (EV vs
     premium/ADV conspicuousness, time-of-day clustering).
  #8 PROMOTION GATES spec (matched, net-of-cost, cluster-corrected,
     regime-robust) published with the card.

Data: pg.grouped_daily (one call per trading day, whole market),
uoa_meta_cache (mcap/sector/earnings_date), the append-only ledger,
yfinance ^VIX. Matched assignments freeze in data/uoa_matched.json.gz
(a signal's controls are assigned once and never reassigned). Output:
docs/reports/uoa_scorecard.json.

Honest limitations, stated on the card: sector/mcap come from the
current meta snapshot (sector ~static; mcap drifts); historical
matches are 4-feature (days-to-earnings joins forward-only, match v2);
close-anchored returns differ from the legacy flag-price anchoring by
the flag->close drift; cost model is an estimate, not NBBO truth.

    python uoa_scorecard.py                # full nightly build
    python uoa_scorecard.py --no-lag       # skip minute-agg sampling
    python uoa_scorecard.py --lag-budget N # ticker-days to fetch
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np

import polygon_data as pg
from trade_desk_validation import _load
from uoa_alpha import load_ledger, _dte_bucket

_BASE = os.path.dirname(os.path.abspath(__file__))
R = lambda *p: os.path.join(_BASE, *p)
MATCH_PATH = R("data", "uoa_matched.json.gz")
LAG_CACHE = R("data", "uoa_entry_lag_cache.json")
OUT_PATH = R("docs", "reports", "uoa_scorecard.json")

SC_VERSION = "uoa_scorecard_v1"
MATRIX_START = "2026-02-17"     # 63d momentum lookback before ledger start
HORIZONS = (1, 3, 5, 10, 20)
BASE_H = 5
N_CTRL = 10
MIN_CTRL = 5
CAP_BAND = 3.0                  # control mcap within [1/3, 3] x signal
CAP_BAND_WIDE = 5.0
BOOT_ITERS = 2000
IMPACT_K = 0.2
CLIP_Q = 10_000.0               # $ baseline clip for net-EV modeling
SPREAD_CAP = 0.05               # cap CS half-spread estimate at 5%
LAG_WINDOW_D = 45
LAG_BUDGET = 250                # new ticker-days fetched per run
SHRINK_K_SIZING = 400
ET = timezone(timedelta(hours=-4))


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


def build_matrix(pool):
    """(dates, tickers, C, V, H, L) numpy matrices for the control pool
    + every ledger ticker + SPY. NaN where missing."""
    today = datetime.now(ET).date().isoformat()
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
    mom63, vol20 (daily pct sd), adv20 ($), cs half-spread (20d)."""
    lo63 = max(0, di - 63)
    lo20 = max(0, di - 20)
    mom = C[di] / C[lo63] - 1.0
    rets = C[lo20 + 1:di + 1] / C[lo20:di] - 1.0
    with np.errstate(invalid="ignore"):
        vol = np.nanstd(rets, axis=0)
        adv = np.nanmean((C * V)[lo20:di + 1], axis=0)
    # Corwin-Schultz half-spread estimate from daily H/L pairs.
    hs = np.full(C.shape[1], np.nan)
    Hh, Ll = H[lo20:di + 1], L[lo20:di + 1]
    with np.errstate(invalid="ignore", divide="ignore"):
        lhl = np.log(Hh / Ll) ** 2
        beta = lhl[:-1] + lhl[1:]
        h2 = np.fmax(Hh[:-1], Hh[1:])
        l2 = np.fmin(Ll[:-1], Ll[1:])
        gamma = np.log(h2 / l2) ** 2
        sq2 = math.sqrt(2.0)
        alpha = (np.sqrt(2 * beta) - np.sqrt(beta)) / (3 - 2 * sq2) \
            - np.sqrt(gamma / (3 - 2 * sq2))
        s = 2 * (np.exp(alpha) - 1) / (1 + np.exp(alpha))
        s = np.clip(s, 0, SPREAD_CAP * 2)
        hs = np.nanmedian(s, axis=0) / 2.0
    return mom, vol, adv, hs


# ------------------------------------------------------------ ledger

def graded_signals(days_set):
    """Deduped directional buyer signals with a matrix-covered flag
    date. Direction-signed like the rest of the stack."""
    out = []
    seen = set()
    for s in load_ledger():
        if s.get("direction") not in ("bullish", "bearish"):
            continue
        if s.get("flow_side") in ("put_seller", "call_seller"):
            continue
        sid = s.get("id")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        day = (s.get("flagged_at") or "")[:10]
        if day not in days_set:
            # weekend/holiday stamp — map to previous trading day later
            pass
        out.append(s)
    return out


def _sig_day_index(day, days):
    """Matrix index for a signal's flag date (previous trading day if
    the stamp is a non-trading date)."""
    import bisect
    i = bisect.bisect_right(days, day) - 1
    return i if i >= 0 else None


# ----------------------------------------------------------- matching

def load_matched():
    try:
        with gzip.open(MATCH_PATH, "rt", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_matched(m):
    os.makedirs(os.path.dirname(MATCH_PATH), exist_ok=True)
    with gzip.open(MATCH_PATH, "wt", encoding="utf-8") as f:
        json.dump(m, f, separators=(",", ":"))


def _ern_bucket(edate, day):
    try:
        dd = (datetime.strptime(edate, "%Y-%m-%d").date()
              - datetime.strptime(day, "%Y-%m-%d").date()).days
    except (TypeError, ValueError):
        return "unk"
    if 0 <= dd <= 5:
        return "0-5"
    if 5 < dd <= 20:
        return "6-20"
    return "far"


def assign_controls(sigs, days, tickers, ti, C, V, H, L, meta, matched):
    """Assign controls for signals not yet in the matched store.
    Assignments FREEZE — never recomputed. Returns count assigned."""
    mcap = {t: (meta.get(t) or {}).get("mkt_cap") for t in tickers}
    sector = {t: (meta.get(t) or {}).get("sector") for t in tickers}
    edate = {t: (meta.get(t) or {}).get("earnings_date")
             for t in tickers}
    today = datetime.now(ET).date().isoformat()
    fresh_cut = (datetime.now(ET).date()
                 - timedelta(days=3)).isoformat()

    by_day = defaultdict(list)
    for s in sigs:
        if s["id"] in matched:
            continue
        day = s["flagged_at"][:10]
        di = _sig_day_index(day, days)
        if di is None or di >= len(days):
            continue
        by_day[di].append(s)

    # numpy-aligned lookups for the vectorized candidate filter
    mcap_arr = np.array([mcap.get(t) or np.nan for t in tickers])
    sec_groups = {}
    for t, j in ti.items():
        if t == "SPY":
            continue
        sec_groups.setdefault(sector.get(t), []).append(j)
    sec_groups = {k: np.array(v) for k, v in sec_groups.items()
                  if k is not None}

    n_new = 0
    for di, group in sorted(by_day.items()):
        mom, vol, adv, hs = features_asof(di, C, V, H, L)
        flag_mask = np.zeros(len(tickers), dtype=bool)
        for x in group:
            j = ti.get(x["ticker"])
            if j is not None:
                flag_mask[j] = True
        # z-score cross-section
        def z(a):
            mu, sd = np.nanmean(a), np.nanstd(a)
            return (a - mu) / sd if sd and sd > 0 else a * 0
        zmom, zvol = z(mom), z(vol)
        valid = ~(np.isnan(mom) | np.isnan(vol) | np.isnan(C[di]))
        for s in group:
            tk = s["ticker"]
            si = ti.get(tk)
            rec = {"mv": 1, "d": days[di], "nc": 0, "x": {}}
            if si is None or not valid[si] or not mcap.get(tk):
                matched[s["id"]] = rec
                continue
            sec = sector.get(tk)
            base_idx = sec_groups.get(sec)
            if base_idx is None or not len(base_idx):
                matched[s["id"]] = rec
                continue
            cand = []
            for band in (CAP_BAND, CAP_BAND_WIDE):
                m = (valid[base_idx]
                     & np.isfinite(mcap_arr[base_idx])
                     & (mcap_arr[base_idx] >= mcap[tk] / band)
                     & (mcap_arr[base_idx] <= mcap[tk] * band)
                     & ~flag_mask[base_idx])
                cand = [int(j) for j in base_idx[m] if j != si]
                if len(cand) >= MIN_CTRL:
                    break
            mv = 1
            if days[di] >= fresh_cut and len(cand) >= MIN_CTRL:
                b = _ern_bucket(edate.get(tk), days[di])
                cand2 = [j for j in cand
                         if _ern_bucket(edate.get(tickers[j]),
                                        days[di]) == b]
                if len(cand2) >= MIN_CTRL:
                    cand, mv = cand2, 2
            if len(cand) < MIN_CTRL:
                matched[s["id"]] = rec
                continue
            dist = (zmom[cand] - zmom[si]) ** 2 \
                + (zvol[cand] - zvol[si]) ** 2
            order = np.argsort(dist)[:N_CTRL]
            ctrl = [cand[k] for k in order]
            matched[s["id"]] = {"mv": mv, "d": days[di],
                                "nc": len(ctrl),
                                "ci": [int(j) for j in ctrl], "x": {}}
            n_new += 1
    return n_new


def grade_matched(sigs, days, tickers, ti, C, matched):
    """Fill matched excess for every horizon that has matured and is
    still ungraded. Signal + control legs close-anchored from the SAME
    matrix. Direction-signed."""
    n_d = len(days)
    sig_by_id = {s["id"]: s for s in sigs}
    n_graded = 0
    for sid, rec in matched.items():
        s = sig_by_id.get(sid)
        if not s or "ci" not in rec:
            continue
        di = _sig_day_index(rec["d"], days)
        si = ti.get(s["ticker"])
        if di is None or si is None:
            continue
        sign = 1.0 if s["direction"] == "bullish" else -1.0
        for h in HORIZONS:
            key = str(h)
            if key in rec["x"] or di + h >= n_d:
                continue
            c0s, chs = C[di, si], C[di + h, si]
            if not (np.isfinite(c0s) and np.isfinite(chs)):
                rec["x"][key] = None
                continue
            sig_ret = chs / c0s - 1.0
            crs = []
            for j in rec["ci"]:
                c0, ch = C[di, j], C[di + h, j]
                if np.isfinite(c0) and np.isfinite(ch):
                    crs.append(ch / c0 - 1.0)
            if len(crs) < MIN_CTRL:
                rec["x"][key] = None
                continue
            rec["x"][key] = round(
                100 * sign * (sig_ret - float(np.mean(crs))), 3)
            if h == BASE_H:
                n_graded += 1
    return n_graded


# ------------------------------------------------------------- stats

def _stats(vals):
    v = [x for x in vals if x is not None]
    if not v:
        return {"n": 0}
    a = np.array(v)
    return {"n": len(v), "avg": round(float(a.mean()), 3),
            "med": round(float(np.median(a)), 3),
            "hit": round(100 * float((a > 0).mean()))}


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
    return {"clusters": len(keys),
            "ci95": [round(means[int(iters * .025)], 3),
                     round(means[int(iters * .975)], 3)],
            "se_cluster": round(se_boot, 4),
            "n_eff": n_eff}


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
        _, inv, cnt = np.unique(a, return_inverse=True,
                                return_counts=True)
        sums = np.zeros(len(cnt))
        np.add.at(sums, inv, r)
        return sums[inv] / cnt[inv]
    rx, ry = rank(xa), rank(ya)
    rx -= rx.mean()
    ry -= ry.mean()
    den = math.sqrt((rx ** 2).sum() * (ry ** 2).sum())
    return round(float((rx * ry).sum() / den), 4) if den else 0.0


# ------------------------------------------------------------- costs

def cost_model(hs_pct, vol_d, adv_d, q=CLIP_Q, k=IMPACT_K):
    """Round-trip cost in return %: 2 x (half-spread + sqrt impact)."""
    if not (np.isfinite(hs_pct) and np.isfinite(vol_d)
            and np.isfinite(adv_d)) or adv_d <= 0:
        return None
    hs = min(float(hs_pct), SPREAD_CAP)
    impact = k * float(vol_d) * math.sqrt(q / float(adv_d))
    return round(100 * 2 * (hs + impact), 4)


# ---------------------------------------------------------------- run

def run(no_lag=False, lag_budget=LAG_BUDGET, dry=False):
    if not pg.available():
        out = {"generated": datetime.now(timezone.utc)
               .isoformat(timespec="seconds"),
               "status": "capability_unavailable",
               "why": "POLYGON_API_KEY not set"}
        json.dump(out, open(OUT_PATH, "w", encoding="utf-8"), indent=1)
        print(out["why"])
        return out

    meta = _load(R("docs", "reports", "uoa_meta_cache.json"), {}) or {}
    pool = {t for t, v in meta.items()
            if v.get("mkt_cap") and v.get("sector")}
    ledger = load_ledger()
    ledger_tickers = {s.get("ticker") for s in ledger if s.get("ticker")}
    print(f"pool {len(pool)} | ledger tickers {len(ledger_tickers)}")

    days, tickers, ti, C, V, H, L, O = build_matrix(pool | ledger_tickers)
    days_set = set(days)
    print(f"matrix: {len(days)} trading days x {len(tickers)} tickers")

    sigs = graded_signals(days_set)
    matched = load_matched()
    n_new = assign_controls(sigs, days, tickers, ti, C, V, H, L,
                            meta, matched)
    n_graded = grade_matched(sigs, days, tickers, ti, C, matched)
    save_matched(matched)
    print(f"controls: +{n_new} assigned, +{n_graded} graded at +{BASE_H}d")

    sig_by_id = {s["id"]: s for s in sigs}
    sector = {t: (meta.get(t) or {}).get("sector") for t in tickers}

    # joined rows for panels
    rows = []
    for sid, rec in matched.items():
        s = sig_by_id.get(sid)
        x5 = (rec.get("x") or {}).get(str(BASE_H))
        if not s or x5 is None:
            continue
        day = rec["d"]
        di = _sig_day_index(day, days)
        si = ti.get(s["ticker"])
        rows.append({
            "id": sid, "day": day, "di": di, "si": si,
            "tk": s["ticker"],
            "sector": sector.get(s["ticker"]) or "unk",
            "dir": s["direction"],
            "score": s.get("trade_score") or 0,
            "raw": (s.get("trade_score") or 0)
                   - (s.get("edge_adj") or 0),
            "has_adj": "edge_adj" in s,
            "ern": "Into ERN" in (s.get("tags") or []),
            "golden": s.get("signal_type") == "golden_sweep",
            "prem": s.get("premium") or 0,
            "dte": _dte_bucket(s.get("dte")),
            "mv": rec.get("mv"),
            "x": {int(h): v for h, v in (rec.get("x") or {}).items()
                  if v is not None},
        })
    print(f"panel rows (matched, graded +{BASE_H}d): {len(rows)}")

    # legacy raw excess for the same ids (decomposition headline)
    # -> from x we can't recover raw-vs-SPY; publish alongside from
    #    uoa_edge for context instead of recomputing.
    edge_legacy = _load(R("docs", "reports", "uoa_edge.json"), {}) or {}

    # per-signal cost + features at signal date
    day_feat = {}
    for r in rows:
        if r["di"] not in day_feat:
            day_feat[r["di"]] = features_asof(r["di"], C, V, H, L)
        mom, vol, adv, hs = day_feat[r["di"]]
        j = r["si"]
        r["hs"] = float(hs[j]) if j is not None and np.isfinite(hs[j]) \
            else None
        r["vold"] = float(vol[j]) if j is not None \
            and np.isfinite(vol[j]) else None
        r["adv"] = float(adv[j]) if j is not None \
            and np.isfinite(adv[j]) else None
        r["cost"] = cost_model(r["hs"], r["vold"], r["adv"]) \
            if r["hs"] is not None else None
        r["net"] = round(r["x"][BASE_H] - r["cost"], 3) \
            if (r["cost"] is not None and BASE_H in r["x"]) else None
        r["premadv"] = (r["prem"] / r["adv"]) if r["adv"] else None
        r["spread_bkt"] = (None if r["hs"] is None else
                           ("<10bp" if r["hs"] < 0.0010 else
                            "10-25bp" if r["hs"] < 0.0025 else
                            ">25bp"))

    def pocket(rs, name):
        vals = [r["x"].get(BASE_H) for r in rs]
        st = _stats(vals)
        if st["n"] >= 50:
            st["bootstrap"] = cluster_boot(
                [(_week(r["day"]) + "|" + r["sector"],
                  r["x"][BASE_H]) for r in rs if BASE_H in r["x"]])
        nets = [r["net"] for r in rs if r["net"] is not None]
        st["net"] = _stats(nets)
        by_spread = {}
        for b in ("<10bp", "10-25bp", ">25bp"):
            sub = [r for r in rs if r["spread_bkt"] == b]
            by_spread[b] = {
                "gross": _stats([r["x"].get(BASE_H) for r in sub]),
                "net": _stats([r["net"] for r in sub
                               if r["net"] is not None])}
        st["by_spread"] = by_spread
        st["name"] = name
        return st

    pockets = {
        "overall": pocket(rows, "overall"),
        "into_earnings": pocket([r for r in rows if r["ern"]],
                                "into_earnings"),
        "golden_sweeps": pocket([r for r in rows if r["golden"]],
                                "golden_sweeps"),
        "score_80_plus": pocket([r for r in rows if r["score"] >= 80],
                                "score_80_plus"),
        "put_buys": pocket([r for r in rows if r["dir"] == "bearish"],
                           "put_buys"),
        "call_buys": pocket([r for r in rows if r["dir"] == "bullish"],
                            "call_buys"),
    }

    # ---- IC panel (matched excess) ----
    def ic_block(rs, key):
        out = {}
        for h in HORIZONS:
            sub = [(r[key], r["x"][h]) for r in rs if h in r["x"]]
            if len(sub) >= 500:
                out[f"+{h}d"] = _spearman([a for a, _ in sub],
                                          [b for _, b in sub])
        return out
    adj_rows = [r for r in rows if r["has_adj"]]
    # regimes: VIX terciles + SPY 20d trend
    vix = {}
    try:
        import yfinance as yf
        vs = yf.download("^VIX", start=MATRIX_START,
                         progress=False, auto_adjust=False)["Close"]
        try:
            vs = vs.squeeze()
        except Exception:
            pass
        vs = vs.dropna()
        vals = [float(v) for v in vs.values]
        t1, t2 = np.percentile(vals, [33.3, 66.7])
        for idx, v in zip(vs.index, vals):
            d = str(idx)[:10]
            vix[d] = ("low" if v < t1 else
                      "mid" if v < t2 else "high")
    except Exception as e:
        print("VIX fetch failed:", str(e)[:80])
    spy_i = ti.get("SPY")
    spy_trend = {}
    if spy_i is not None:
        for di in range(20, len(days)):
            spy_trend[days[di]] = ("up" if C[di, spy_i]
                                   >= C[di - 20, spy_i] else "down")
    def regime_of(day):
        return f"vix_{vix.get(day, 'unk')}|spy_{spy_trend.get(day, 'unk')}"
    ic_regime = {}
    for r in adj_rows:
        ic_regime.setdefault(regime_of(r["day"]), []).append(r)
    ic_by_regime = {k: {"n": len(v),
                        "ic": _spearman([r["score"] for r in v],
                                        [r["x"][BASE_H] for r in v])}
                    for k, v in ic_regime.items() if len(v) >= 500}

    # decile table on matched excess (adjusted score)
    dec = {}
    if len(adj_rows) >= 2000:
        srt = sorted(adj_rows, key=lambda r: r["score"])
        n10 = len(srt) // 10
        dtab = []
        for dgt in range(10):
            seg = srt[dgt * n10:(dgt + 1) * n10 if dgt < 9 else len(srt)]
            dtab.append({"d": dgt + 1,
                         "avg": round(float(np.mean(
                             [r["x"][BASE_H] for r in seg])), 3),
                         "hit": round(100 * float(np.mean(
                             [r["x"][BASE_H] > 0 for r in seg])))})
        dec = {"deciles": dtab,
               "top_minus_bottom": round(
                   dtab[-1]["avg"] - dtab[0]["avg"], 3),
               "monotonic_steps": sum(
                   1 for i in range(9)
                   if dtab[i + 1]["avg"] >= dtab[i]["avg"])}

    ic_panel = {
        "adjusted_score": {
            "n": len(adj_rows),
            "note": "ledger trade_score bakes in edge_adj at flag "
                    "time (point-in-time); raw = trade_score - "
                    "edge_adj",
            "matched_ic_by_horizon": ic_block(adj_rows, "score"),
            "raw_matched_ic_by_horizon": ic_block(adj_rows, "raw"),
            "by_regime": ic_by_regime,
            "decile_matched": dec,
        }}

    # ---- calibration ----
    cutoff90 = (datetime.now(ET).date()
                - timedelta(days=90)).isoformat()
    recent = [r for r in adj_rows if r["day"] >= cutoff90]
    rel = []
    if len(recent) >= 1000:
        srt = sorted(recent, key=lambda r: r["score"])
        nb = len(srt) // 10
        for b in range(10):
            seg = srt[b * nb:(b + 1) * nb if b < 9 else len(srt)]
            boot = cluster_boot([(_week(r["day"]) + "|" + r["sector"],
                                  1.0 if r["x"][BASE_H] > 0 else 0.0)
                                 for r in seg], iters=500, seed=b)
            rel.append({"bin": b + 1,
                        "score_lo": seg[0]["score"],
                        "score_hi": seg[-1]["score"],
                        "hit": round(100 * float(np.mean(
                            [r["x"][BASE_H] > 0 for r in seg]))),
                        "ci": boot.get("ci95")})
    base_by_regime = {}
    for k, v in ic_regime.items():
        if len(v) >= 300:
            base_by_regime[k] = {
                "n": len(v),
                "hit": round(100 * float(np.mean(
                    [r["x"][BASE_H] > 0 for r in v]))),
                "avg": round(float(np.mean(
                    [r["x"][BASE_H] for r in v])), 3)}
    n_recent = len(recent)
    calibration = {
        "reliability_rolling90": rel,
        "regime_base_rates": base_by_regime,
        "sizing_shrinkage": {
            "w_recent": round(n_recent / (n_recent + SHRINK_K_SIZING),
                              3),
            "rule": "sizing weight = w*recent + (1-w)*trained, "
                    f"w = n_recent/(n_recent+{SHRINK_K_SIZING})"}}

    # ---- crowding ----
    def crowd(rs):
        buckets = {"<0.1%": [], "0.1-0.5%": [], "0.5-2%": [],
                   ">2%": []}
        for r in rs:
            pa = r["premadv"]
            if pa is None or BASE_H not in r["x"]:
                continue
            b = ("<0.1%" if pa < 0.001 else
                 "0.1-0.5%" if pa < 0.005 else
                 "0.5-2%" if pa < 0.02 else ">2%")
            buckets[b].append(r["x"][BASE_H])
        return {b: _stats(v) for b, v in buckets.items()}
    tod = defaultdict(list)
    for r in rows:
        s = sig_by_id.get(r["id"])
        try:
            hr = datetime.fromisoformat(
                s["flagged_at"]).astimezone(ET).hour
            tod[f"{hr:02d}h"].append(r["x"][BASE_H])
        except Exception:
            pass
    crowding = {
        "ev_vs_premium_over_adv": {
            "all": crowd(rows),
            "golden_sweeps": crowd([r for r in rows if r["golden"]]),
            "score_80_plus": crowd([r for r in rows
                                    if r["score"] >= 80])},
        "ev_by_hour_et": {k: _stats(v) for k, v in sorted(tod.items())
                          if len(v) >= 300},
        "hypothesis": "if EV falls as premium/ADV conspicuousness "
                      "rises, the best-looking prints are crowded and "
                      "the quiet qualifying signals carry the edge"}

    # ---- capacity ----
    def capacity(rs, name):
        st = pockets.get(name) or {}
        edge = (st.get("avg") or 0) / 100.0
        if edge <= 0:
            return {"edge_nonpositive": True}
        qs = []
        for r in rs:
            if r["vold"] and r["adv"] and r["vold"] > 0:
                q = r["adv"] * (0.5 * edge / (IMPACT_K * r["vold"])) ** 2
                qs.append(q)
        if not qs:
            return {"n": 0}
        qa = np.array(qs)
        return {"per_name_q50_$": int(np.median(qa)),
                "per_name_q25_$": int(np.percentile(qa, 25)),
                "note": "clip size where sqrt-impact consumes 50% of "
                        "gross matched edge"}
    cap_panel = {n: capacity([r for r in rows if (
        n == "overall" or
        (n == "into_earnings" and r["ern"]) or
        (n == "put_buys" and r["dir"] == "bearish"))], n)
        for n in ("overall", "into_earnings", "put_buys")}

    # ---- book simulation ----
    def book(rs, name):
        # one unit per ticker-day-direction, hold BASE_H days,
        # matched excess spread evenly across the window minus costs
        # amortized at entry. Daily P&L approximated as x5/BASE_H per
        # open position (close-anchored path detail unavailable
        # without another matrix pass — documented approximation).
        daily = defaultdict(list)
        seen = set()
        for r in rs:
            k = (r["tk"], r["day"], r["dir"])
            if k in seen or BASE_H not in r["x"]:
                continue
            seen.add(k)
            di = r["di"]
            per_day = r["x"][BASE_H] / BASE_H
            costadj = (r["cost"] or 0) / BASE_H
            for d2 in range(di + 1, min(di + 1 + BASE_H, len(days))):
                daily[d2].append(per_day - costadj)
        if not daily:
            return {"n_days": 0}
        eq, peak, dd, uw, uw_max = 0.0, 0.0, 0.0, 0, 0
        months = defaultdict(float)
        for di in sorted(daily):
            ret = float(np.mean(daily[di]))
            eq += ret
            months[days[di][:7]] += ret
            if eq >= peak:
                peak, uw = eq, 0
            else:
                uw += 1
                uw_max = max(uw_max, uw)
                dd = min(dd, eq - peak)
        worst_m = min(months.items(), key=lambda kv: kv[1]) \
            if months else (None, 0)
        return {"n_days": len(daily),
                "total_matched_excess_pp": round(eq, 2),
                "max_drawdown_pp": round(dd, 2),
                "longest_underwater_days": uw_max,
                "worst_month": {"month": worst_m[0],
                                "pp": round(worst_m[1], 2)},
                "note": "equal-weight, net of modeled costs, "
                        "hold-5d overlap; per-day P&L approximated "
                        "as x5/5 per open position"}
    book_panel = {"overall": book(rows, "overall"),
                  "into_earnings": book(
                      [r for r in rows if r["ern"]], "into_earnings")}

    # ---- entry-lag ladder ----
    lag_panel = {"status": "skipped"} if no_lag else entry_lag(
        rows, sig_by_id, days, ti, C, O, lag_budget)

    result = {
        "generated": datetime.now(timezone.utc)
        .isoformat(timespec="seconds"),
        "version": SC_VERSION,
        "matrix": {"days": len(days), "tickers": len(tickers),
                   "from": days[0] if days else None,
                   "to": days[-1] if days else None},
        "matched": {
            "assigned": len([1 for v in matched.values() if "ci" in v]),
            "unmatchable": len([1 for v in matched.values()
                                if "ci" not in v]),
            "graded_5d": len(rows),
            "match_v2_forward": len([1 for v in matched.values()
                                     if v.get("mv") == 2]),
        },
        "headline": {
            "legacy_vs_spy_avg_excess_5d":
                ((edge_legacy.get("overall") or {}).get("5")
                 or {}).get("avg_excess"),
            "matched_avg_excess_5d": pockets["overall"].get("avg"),
            "matched_into_earnings_5d":
                pockets["into_earnings"].get("avg"),
            "note": "matched excess is the flow alpha; the spread to "
                    "the legacy SPY number is beta + universe drift + "
                    "event premium",
        },
        "pockets": pockets,
        "ic": ic_panel,
        "calibration": calibration,
        "crowding": crowding,
        "capacity": cap_panel,
        "book_sim": book_panel,
        "entry_lag": lag_panel,
        "cost_model": {
            "half_spread": "Corwin-Schultz 20d median estimate "
                           f"(cap {SPREAD_CAP:.0%})",
            "impact": f"k*sigma_d*sqrt(Q/ADV), k={IMPACT_K}, "
                      f"Q=${int(CLIP_Q):,}",
            "roundtrip": "2x(half_spread + impact)"},
        "promotion_gates": {
            "spec": [
                "positive matched-excess NET-of-cost EV over >= 8 "
                "unseen weeks",
                "cluster-corrected |t| > 2 on effective n",
                "monotone decile spread OOS if the change touches "
                "ranking",
                "no degradation in the worst regime tercile",
            ],
            "meta_kpi": "publish promoted/demoted counts as they "
                        "accrue (none yet under these gates)"},
        "honesty": (
            "Matched controls inherit beta, momentum drift and (match "
            "v2, forward) days-to-earnings; sector/mcap are from the "
            "current meta snapshot (sector ~static, mcap drifts); "
            "historical matches are 4-feature. Returns are "
            "close-anchored both legs — legacy flag-price numbers "
            "include flag->close drift and are not comparable. Costs "
            "are modeled (CS spread + sqrt impact), not NBBO truth. "
            "Cluster unit = ISO week x sector; effective n is a "
            "variance-ratio estimate."),
    }
    if not dry:
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1)
    print("headline:", json.dumps(result["headline"]))
    print("overall:", json.dumps({k: pockets['overall'].get(k)
                                  for k in ('n', 'avg', 'med', 'hit',
                                            'bootstrap', 'net')}))
    print("into_earnings:", json.dumps(
        {k: pockets['into_earnings'].get(k)
         for k in ('n', 'avg', 'bootstrap')}))
    return result


# -------------------------------------------------------- entry lag

def entry_lag(rows, sig_by_id, days, ti, C, O, budget):
    """EV(+5d close exit) if entered at signal / +15m / +60m / next
    open. Minute-agg sample, incremental cache, stratified toward
    into-earnings. Exit + control leg fixed (close-anchored), so the
    ladder isolates ENTRY price alone."""
    cache = _load(LAG_CACHE, {}) or {}
    cutoff = (datetime.now(ET).date()
              - timedelta(days=LAG_WINDOW_D)).isoformat()
    cands = [r for r in rows if r["day"] >= cutoff
             and BASE_H in r["x"]]
    random.Random(11).shuffle(cands)
    cands.sort(key=lambda r: not r["ern"])   # earnings first
    fetched = 0
    for r in cands:
        key = f"{r['tk']}|{r['day']}"
        if key in cache:
            continue
        if fetched >= budget:
            continue
        fetched += 1
        try:
            data = pg._get(
                f"/v2/aggs/ticker/{r['tk']}/range/1/minute/"
                f"{r['day']}/{r['day']}", {"limit": 800})
            res = (data or {}).get("results") or []
            cache[key] = {str(b["t"]): b["c"] for b in res
                          if b.get("t") and b.get("c")}
        except Exception:
            cache[key] = {}
    with open(LAG_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, separators=(",", ":"))

    def px_at(mins, ts_ms, lag_min):
        target = ts_ms + lag_min * 60_000
        best = None
        for t, c in mins.items():
            t = int(t)
            if t >= target and (best is None or t < best[0]):
                best = (t, c)
        return best[1] if best else None

    ladders = {"at_signal": [], "+15m": [], "+60m": [], "next_open": []}
    used = 0
    for r in cands:
        key = f"{r['tk']}|{r['day']}"
        mins = cache.get(key)
        s = sig_by_id.get(r["id"])
        if not mins or not s:
            continue
        try:
            ts_ms = int(datetime.fromisoformat(
                s["flagged_at"]).timestamp() * 1000)
        except Exception:
            continue
        di, si = r["di"], r["si"]
        if si is None or di + BASE_H >= len(days):
            continue
        exit_px = C[di + BASE_H, si]
        c0 = C[di, si]
        if not (np.isfinite(exit_px) and np.isfinite(c0)):
            continue
        sign = 1.0 if r["dir"] == "bullish" else -1.0
        ctrl_leg = (100 * sign * (exit_px / c0 - 1.0)) - r["x"][BASE_H]
        entries = {
            "at_signal": s.get("underlying_px_at_flag"),
            "+15m": px_at(mins, ts_ms, 15),
            "+60m": px_at(mins, ts_ms, 60),
            "next_open": (float(O[di + 1, si])
                          if di + 1 < len(days)
                          and np.isfinite(O[di + 1, si]) else None),
        }
        any_used = False
        for name, e in entries.items():
            if e and e > 0:
                ev = 100 * sign * (float(exit_px) / float(e) - 1.0) \
                    - ctrl_leg
                ladders[name].append(ev)
                any_used = True
        if any_used:
            used += 1
    out = {"window_days": LAG_WINDOW_D, "sampled_ticker_days": used,
           "new_fetched": fetched,
           "ladder": {k: _stats(v) for k, v in ladders.items()},
           "note": "exit (+5d close) and control leg fixed; the "
                   "ladder isolates entry price alone. next_open = "
                   "next session's official daily open."}
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-lag", action="store_true")
    ap.add_argument("--lag-budget", type=int, default=LAG_BUDGET)
    ap.add_argument("--dry-run", action="store_true")
    run(no_lag=ap.parse_args().no_lag,
        lag_budget=ap.parse_args().lag_budget,
        dry=ap.parse_args().dry_run)
