"""
earnings_vol_exec.py — Earnings Volatility Alpha Engine, part 3:
EXECUTION-QUALITY study on historical NBBO quotes. CI-only.

V2 established: the vol_rich signal produces positive gross THEORETICAL
P&L when marked at the next-session open, before realistic execution
costs (~$69/lot frictionless on the straddle; gone by the close; gone
at 5%-of-price slippage). Whether that survives ACTUAL executable
opening-market friction on sufficiently liquid contracts is the whole
question now. This module answers it with quote-level reconstruction:

  NBBO SNAPSHOTS (immutable cache data/earnings_vol_quotes.json,
  keyed contract|date|window|qv1):
    entry  "1555"  last NBBO in 15:45-16:00 ET on the entry session
    exit   "0930"  last NBBO in 09:30:00-09:31 ET   (the ugly open)
           "0935"  last NBBO in 09:31-09:35
           "0945"  last NBBO in 09:35-09:45
           "1000"  last NBBO in 09:45-10:00
  Every snapshot stores bid/ask/sizes/timestamp. Capability-probed:
  if the quotes entitlement is absent, the report says so and stops.

  PACKAGE-LEVEL FILLS — the trader submits one complex order, not four
  legs. Package mid = signed sum of leg mids; package natural = shorts
  at bid / longs at ask (reversed to exit). Fill ladder, entry AND
  exit, fees included:
    f = 0.00  MID            (excellent, not assumed achievable)
    f = 0.25  MID - 25% of the mid->natural distance
    f = 0.50  MID - 50%      (canonical EXEC)
    f = 1.00  NATURAL        (worst case)

  BREAK-EVEN MID CAPTURE — per event, the entry credit solving
  P&L = 0 given the f=0.50 exit, expressed as a fraction of package
  mid; published as a distribution (median/p25/p75/p90). "97% capture
  required" is actionable; "3% slippage" is not.

  LIQUIDITY CONDITIONING (predeclared buckets, causal candidate):
  ATM package spread% (<=2 / 2-5 / 5-10 / >10) and ATM leg mid price
  (<0.5 / 0.5-2 / >=2 $): gross vs executable PF per bucket.

  GROSS EDGE CAPTURE = net executable P&L / frictionless P&L.

  Frozen structure set (V2 finding: timing+friction dominate structure
  choice — no new structures): short_straddle (diagnostic only),
  iron_fly_1.5, iron_condor_0.75_1.5, iron_condor_0.9_1.5. vol_cheap
  is excluded: frictionless-negative in V2, nothing to execute.

  VOL_RICH_EXEC_V1 gate (trade_desk reads THIS report's verdict):
    at f=0.25 entry+exit: n>=60 · avg ROR>0 · median ROR>0 ·
    capital-weighted ROR>0 · PF>=1.3 · night-cluster CI low>0 ·
    exp log-growth>0 · >=2/3 chrono folds positive AND latest>0 ·
    maxDD (1R curve) > -5R · loss>1R == 0 · AND f=0.50 avg ROR > 0.
    MID-only profitability does not qualify (if it only works at
    exact midpoint, there is no trade).

    python earnings_vol_exec.py             # fetch quotes + report
    python earnings_vol_exec.py --limit 10  # first N missing events
    python earnings_vol_exec.py --report-only
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
from datetime import datetime, timezone, timedelta
from statistics import mean, median

import polygon_data as pg
from trade_desk_validation import _load

_BASE = os.path.dirname(os.path.abspath(__file__))
R = lambda *p: os.path.join(_BASE, *p)
PNL_CACHE = R("data", "earnings_vol_pnl.json")
Q_CACHE = R("data", "earnings_vol_quotes.json")
OUT_PATH = R("docs", "reports", "earnings_vol_exec.json")

EXEC_VERSION = "vol_exec_v1"
BT_SOURCE = "vol_backtest_v2"      # legs come from these immutable rows
FEE = 0.65
FILLS = (0.0, 0.25, 0.50, 1.0)
QUAL_FILL = 0.25                   # qualification ladder rung
CANON_FILL = 0.50                  # canonical EXEC for edge-capture
EXIT_WINDOWS = ("0930", "0935", "0945", "1000")
BASE_EXIT_WINDOW = "0935"          # crush retained, spread normalized
STRUCTS = ("short_straddle", "iron_fly_1.5",
           "iron_condor_0.75_1.5", "iron_condor_0.9_1.5")
MIN_N_QUALIFY = 60
PF_QUALIFY = 1.3
MAX_DD_R = -5.0
BOOT_ITERS = 3000

# All 2026 events sit in EDT; a tz-database dependency is avoided on
# purpose (Windows CI images may lack tzdata). Revisit if the record
# ever spans a DST change.
ET = timezone(timedelta(hours=-4))
WINDOW_BOUNDS = {"1555": ("15:45:00", "16:00:00"),
                 "0930": ("09:30:00", "09:31:00"),
                 "0935": ("09:31:00", "09:35:00"),
                 "0945": ("09:35:00", "09:45:00"),
                 "1000": ("09:45:00", "10:00:00")}


def _ns(date_str, hms):
    dt = datetime.strptime(date_str + " " + hms,
                           "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
    return int(dt.timestamp() * 1e9)


def _last_quote(contract, date_str, window):
    """Last NBBO inside the window — one small descending query."""
    lo, hi = WINDOW_BOUNDS[window]
    data = pg._get(f"/v3/quotes/{contract}", {
        "timestamp.gte": _ns(date_str, lo),
        "timestamp.lte": _ns(date_str, hi),
        "order": "desc", "sort": "timestamp", "limit": 5,
    })
    if not data:
        return None
    res = data.get("results") or []
    for q in res:
        b, a = q.get("bid_price"), q.get("ask_price")
        if b is not None and a is not None and a >= b > 0:
            return {"bid": b, "ask": a,
                    "bs": q.get("bid_size"), "as": q.get("ask_size"),
                    "ts": q.get("sip_timestamp")}
    return None


def _quotes_entitled():
    """Capability probe. A NOT_AUTHORIZED plan returns HTTP failures
    (pg._get -> None); an ENTITLED plan returns a JSON envelope even
    when the window is empty ({"status":"OK","results":[]}). Probe up
    to 12 ATM short legs across distinct events so one thin contract
    cannot fake a 403."""
    cache = _load(PNL_CACHE, {}) or {}
    tried = 0
    for r in cache.values():
        if r.get("bt_version") != BT_SOURCE or r.get("skip"):
            continue
        s = (r.get("strategies") or {}).get("short_straddle")
        for l in (s or {}).get("legs") or []:
            lo, hi = WINDOW_BOUNDS["1555"]
            data = pg._get(f"/v3/quotes/{l['contract']}", {
                "timestamp.gte": _ns(r["entry_date"], lo),
                "timestamp.lte": _ns(r["entry_date"], hi),
                "order": "desc", "sort": "timestamp", "limit": 1,
            })
            if isinstance(data, dict):
                return True          # endpoint authorized
            tried += 1
            if tried >= 12:
                return False         # 12 straight HTTP failures = 403
            break                    # one leg per event is enough
    return False


# ------------------------------------------------------------ fetching

def _events_v2():
    cache = _load(PNL_CACHE, {}) or {}
    out = []
    for key, r in cache.items():
        if r.get("bt_version") != BT_SOURCE or r.get("skip"):
            continue
        if r["event"]["type"] != "vol_rich":
            continue
        out.append((key, r))
    out.sort(key=lambda kr: kr[1]["event"]["date"])
    return out


def _legs_for(r):
    """Distinct contracts across the frozen structure set."""
    legs = {}
    for sname in STRUCTS:
        s = (r.get("strategies") or {}).get(sname)
        for l in (s or {}).get("legs") or []:
            legs[l["contract"]] = l
    return legs


def fetch_quotes(limit=None):
    qc = _load(Q_CACHE, {}) or {}
    events = _events_v2()
    fetched = missing = 0
    done_events = 0
    for key, r in events:
        needed = []
        for contract in _legs_for(r):
            needed.append((contract, r["entry_date"], "1555"))
            for w in EXIT_WINDOWS:
                needed.append((contract, r["exit_date"], w))
        todo = [(c, d, w) for c, d, w in needed
                if f"{c}|{d}|{w}|{EXEC_VERSION}" not in qc]
        if todo:
            if limit is not None and done_events >= limit:
                continue
            done_events += 1
            for c, d, w in todo:
                q = _last_quote(c, d, w)
                qc[f"{c}|{d}|{w}|{EXEC_VERSION}"] = q or {"missing": True}
                fetched += 1
                if q is None:
                    missing += 1
    with open(Q_CACHE, "w", encoding="utf-8") as f:
        json.dump(qc, f, indent=0)
    print(f"quotes: fetched {fetched} ({missing} missing) across "
          f"{done_events} newly-touched events")
    return qc


# ------------------------------------------------------------- pricing

def _q(qc, contract, date, window):
    q = qc.get(f"{contract}|{date}|{window}|{EXEC_VERSION}")
    return None if not q or q.get("missing") else q


def package(qc, legs, date, window):
    """(mid, natural, spread_sum) signed from the ENTRY side convention:
    credit positive. natural = shorts at bid, longs at ask."""
    mid = nat = spread = 0.0
    for l in legs:
        q = _q(qc, l["contract"], date, window)
        if q is None:
            return None
        m = (q["bid"] + q["ask"]) / 2
        spread += (q["ask"] - q["bid"])
        if l["side"] < 0:
            mid += m
            nat += q["bid"]
        else:
            mid -= m
            nat -= q["ask"]
    return {"mid": mid, "natural": nat, "leg_spread_sum": spread}


def _fill(pkg, f):
    """Entry-side fill at capture fraction f of mid->natural."""
    return pkg["mid"] - f * (pkg["mid"] - pkg["natural"])


def price_event(qc, r, sname, window, f):
    """Net P&L per share for structure sname: enter at 15:55 NBBO
    package fill (credit), exit at the window's package fill (debit =
    the same package priced from the closing side). Fees per contract
    per side included."""
    s = (r.get("strategies") or {}).get(sname)
    if not s:
        return None
    legs = s["legs"]
    p_in = package(qc, legs, r["entry_date"], "1555")
    # exit: closing the position = opposite side; reuse package() with
    # flipped sides so "natural" prices the buy-back conservatively.
    flipped = [dict(l, side=-l["side"]) for l in legs]
    p_out = package(qc, flipped, r["exit_date"], window)
    if p_in is None or p_out is None:
        return None
    credit = _fill(p_in, f)
    debit = -_fill(p_out, f)      # cost to close (positive number)
    fees = 2 * len(legs) * FEE / 100.0
    pnl = credit - debit - fees
    # Defined-risk max loss = wing width - THIS entry credit. Only
    # meaningful when both wings exist (4-leg structures); the naked
    # straddle stays risk=None (diagnostic, never ROR-rated).
    risk = None
    kinds = [(l["kind"], l["side"], l["strike"]) for l in legs]
    wc = [k for kk, sd, k in kinds if kk == "call" and sd > 0]
    sc = [k for kk, sd, k in kinds if kk == "call" and sd < 0]
    wp = [k for kk, sd, k in kinds if kk == "put" and sd > 0]
    sp = [k for kk, sd, k in kinds if kk == "put" and sd < 0]
    if wc and sc and wp and sp:
        width = max(wc[0] - sc[0], sp[0] - wp[0])
        if width > credit > 0:
            risk = width - credit
    return {"pnl": pnl, "credit": credit, "debit": debit,
            "risk": risk, "mid_in": p_in["mid"],
            "natural_in": p_in["natural"],
            "spread_pct_in": (100 * p_in["leg_spread_sum"]
                              / abs(p_in["mid"])
                              if p_in["mid"] else None)}


# ------------------------------------------------------------- metrics

def _boot_ci(rows, iters=BOOT_ITERS, seed=17):
    nights = {}
    for d, r in rows:
        nights.setdefault(d, []).append(r)
    keys = list(nights)
    if len(keys) < 8:
        return {"status": "insufficient_nights", "nights": len(keys)}
    rng = random.Random(seed)
    means = []
    for _ in range(iters):
        ys = []
        for _ in range(len(keys)):
            ys.extend(nights[rng.choice(keys)])
        means.append(mean(ys))
    means.sort()
    return {"nights": len(keys),
            "ci95": [round(means[int(iters * .025)], 3),
                     round(means[int(iters * .975)], 3)]}


def _max_dd_r(rors):
    eq = peak = dd = 0.0
    for r in rors:
        eq += r
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return round(dd, 2)


def table(prices):
    """prices: list of (date, pnl$, risk$|None)."""
    if not prices:
        return {"n": 0}
    pnls = [p for _, p, _ in prices]
    riskd = [(d, p, k) for d, p, k in prices if k]
    pos = sum(p for p in pnls if p > 0)
    neg = abs(sum(p for p in pnls if p < 0))
    out = {"n": len(pnls),
           "win": round(100 * sum(1 for p in pnls if p > 0) / len(pnls)),
           "avg_pnl_1lot": round(mean(pnls) * 100),
           "profit_factor": round(pos / neg, 2) if neg else None}
    if riskd:
        rors = sorted((d, p / k) for d, p, k in riskd)
        rs = [r for _, r in rors]
        folds = []
        if len(rors) >= 9:
            third = len(rors) // 3
            folds = [round(mean(r for _, r in
                                rors[i * third:(i + 1) * third if i < 2
                                     else len(rors)]), 3)
                     for i in range(3)]
        out.update({
            "avg_ror": round(mean(rs), 3),
            "med_ror": round(median(rs), 3),
            "cap_wt_ror": round(sum(p for _, p, _ in riskd)
                                / sum(k for _, _, k in riskd), 3),
            "worst_ror": round(min(rs), 3),
            "loss_gt_1R": sum(1 for r in rs if r < -1.0001),
            "exp_log_growth": round(mean(
                math.log1p(max(r, -0.999)) for r in rs), 4),
            "max_dd_r": _max_dd_r([r for _, r in sorted(rors)]),
            "fold_avg_ror": folds,
            "night_bootstrap_ror": _boot_ci(rors),
        })
    return out


def _qualifies(st, st50):
    ci = ((st.get("night_bootstrap_ror") or {}).get("ci95") or [None])
    folds = st.get("fold_avg_ror") or []
    return (st.get("n", 0) >= MIN_N_QUALIFY
            and (st.get("avg_ror") or 0) > 0
            and (st.get("med_ror") or 0) > 0
            and (st.get("cap_wt_ror") or 0) > 0
            and ci[0] is not None and ci[0] > 0
            and (st.get("profit_factor") or 0) >= PF_QUALIFY
            and (st.get("exp_log_growth") or 0) > 0
            and st.get("loss_gt_1R", 1) == 0
            and (st.get("max_dd_r") or -99) > MAX_DD_R
            and len(folds) == 3
            and sum(1 for x in folds if x > 0) >= 2 and folds[-1] > 0
            and (st50.get("avg_ror") or 0) > 0)


# --------------------------------------------- minute-agg fallback

_MIN_CACHE_PATH = R("data", "earnings_vol_minute.json")
MINUTE_MARKS = ("09:31", "09:35", "09:45", "10:00")


def _minute_closes(contract, date_str):
    """{HH:MM: close} for the predeclared morning marks + the entry
    15:55-15:59 mark, from one minute-agg call per contract-day."""
    data = pg._get(f"/v2/aggs/ticker/{contract}/range/1/minute/"
                   f"{date_str}/{date_str}", {"limit": 500})
    res = (data or {}).get("results") or []
    out = {}
    for b in res:
        ts = b.get("t")
        if ts is None or b.get("c") is None:
            continue
        hm = datetime.fromtimestamp(ts / 1000, ET).strftime("%H:%M")
        out[hm] = b["c"]
    return out


def minute_timing_study(limit=None):
    """Fallback when NBBO is unavailable: gross MARK P&L (minute closes,
    fees only, NO spread model) for the frozen structures at the four
    morning marks. Answers the exit-TIMING question only; says nothing
    about executability and can never qualify anything."""
    mc = _load(_MIN_CACHE_PATH, {}) or {}
    events = _events_v2()
    touched = 0
    for key, r in events:
        contracts = list(_legs_for(r))
        need = [(c, d) for c in contracts
                for d in (r["entry_date"], r["exit_date"])
                if f"{c}|{d}|min1" not in mc]
        if need:
            if limit is not None and touched >= limit:
                continue
            touched += 1
            for c, d in need:
                try:
                    mc[f"{c}|{d}|min1"] = _minute_closes(c, d)
                except Exception:
                    mc[f"{c}|{d}|min1"] = {}
    with open(_MIN_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(mc, f, indent=0)

    def leg_mark(contract, date, hm_targets):
        m = mc.get(f"{contract}|{date}|min1") or {}
        for hm in hm_targets:
            if hm in m:
                return m[hm]
        return None

    ENTRY_MARKS = ("15:58", "15:59", "15:57", "15:56", "15:55")
    summary = {}
    for sname in STRUCTS:
        per_mark = {}
        for mark in MINUTE_MARKS:
            # accept the exact minute or the nearest earlier minute
            hh, mm = mark.split(":")
            cands = [mark] + [f"{hh}:{int(mm)-k:02d}"
                              for k in (1, 2, 3) if int(mm) - k >= 0]
            rows = []
            for key, r in events:
                s = (r.get("strategies") or {}).get(sname)
                if not s:
                    continue
                pnl = 0.0
                ok = True
                for l in s["legs"]:
                    ein = leg_mark(l["contract"], r["entry_date"],
                                   ENTRY_MARKS)
                    eout = leg_mark(l["contract"], r["exit_date"], cands)
                    if ein is None or eout is None:
                        ok = False
                        break
                    fee = 2 * FEE / 100.0
                    pnl += (-l["side"]) * (ein - eout) - fee
                if ok:
                    rows.append((r["night_id"], pnl))
            if rows:
                pnls = [p for _, p in rows]
                pos = sum(p for p in pnls if p > 0)
                neg = abs(sum(p for p in pnls if p < 0))
                per_mark[mark] = {
                    "n": len(pnls),
                    "avg_$1lot": round(mean(pnls) * 100),
                    "win": round(100 * sum(1 for p in pnls if p > 0)
                                 / len(pnls)),
                    "pf": round(pos / neg, 2) if neg else None,
                    "night_ci": _boot_ci(rows)}
            else:
                per_mark[mark] = {"n": 0}
        summary[sname] = per_mark
    return {"summary": summary,
            "honesty": "minute-close MARKS, fees only, no spread model "
                       "— timing evidence, not executability evidence. "
                       "Qualification is impossible from this table."}


# ---------------------------------------------------------------- main

def run(limit=None, report_only=False):
    if not report_only:
        if not pg.available():
            out = {"generated": datetime.now(timezone.utc)
                   .isoformat(timespec="seconds"),
                   "status": "capability_unavailable",
                   "why": "POLYGON_API_KEY not set (CI-only secret)"}
            json.dump(out, open(OUT_PATH, "w", encoding="utf-8"),
                      indent=1)
            print(out["why"])
            return out
        if not _quotes_entitled():
            out = {"generated": datetime.now(timezone.utc)
                   .isoformat(timespec="seconds"),
                   "status": "quotes_entitlement_unavailable",
                   "why": "historical NBBO quotes endpoint is not "
                          "authorized on this plan tier (12/12 probe "
                          "queries failed at HTTP level). The "
                          "spread/fill study cannot run; "
                          "trade_qualified stays false. (Outcome: "
                          "undetermined, NOT a pass.)",
                   "minute_timing_study": minute_timing_study(limit)}
            json.dump(out, open(OUT_PATH, "w", encoding="utf-8"),
                      indent=1)
            print(out["why"])
            print("minute timing:", json.dumps(
                out["minute_timing_study"].get("summary", {}))[:400])
            return out
        fetch_quotes(limit=limit)

    qc = _load(Q_CACHE, {}) or {}
    events = _events_v2()
    result = {"generated": datetime.now(timezone.utc)
              .isoformat(timespec="seconds"),
              "exec_version": EXEC_VERSION, "source": BT_SOURCE,
              "fills": list(FILLS), "qual_fill": QUAL_FILL,
              "canonical_fill": CANON_FILL,
              "exit_windows": list(EXIT_WINDOWS),
              "base_exit_window": BASE_EXIT_WINDOW,
              "structures": {}}

    frictionless_by_event = {}
    for sname in STRUCTS:
        by_window = {}
        for w in EXIT_WINDOWS:
            ladder = {}
            for f in FILLS:
                rows = []
                for key, r in events:
                    pr = price_event(qc, r, sname, w, f)
                    if pr is None:
                        continue
                    rows.append((r["night_id"], pr["pnl"], pr["risk"]))
                    if f == 0.0 and w == BASE_EXIT_WINDOW:
                        frictionless_by_event.setdefault(
                            sname, {})[key] = pr["pnl"]
                ladder[f"f{int(f*100)}"] = table(rows)
            by_window[w] = ladder
        result["structures"][sname] = by_window

    # Gross Edge Capture at the canonical fill/base window
    capture = {}
    for sname in STRUCTS:
        gross = net = 0.0
        for key, r in events:
            g = (frictionless_by_event.get(sname) or {}).get(key)
            pr = price_event(qc, r, sname, BASE_EXIT_WINDOW, CANON_FILL)
            if g is None or pr is None:
                continue
            gross += g
            net += pr["pnl"]
        capture[sname] = (round(net / gross, 3)
                          if gross > 0 else None)
    result["gross_edge_capture"] = {
        "definition": "net executable P&L (f=0.50, base window) / "
                      "frictionless mid P&L, summed over events",
        "by_structure": capture}

    # Break-even mid capture distribution (entry side), f=0.50 exit
    be = {}
    for sname in STRUCTS:
        vals = []
        for key, r in events:
            s = (r.get("strategies") or {}).get(sname)
            if not s:
                continue
            legs = s["legs"]
            p_in = package(qc, legs, r["entry_date"], "1555")
            flipped = [dict(l, side=-l["side"]) for l in legs]
            p_out = package(qc, flipped, r["exit_date"],
                            BASE_EXIT_WINDOW)
            if p_in is None or p_out is None or not p_in["mid"]:
                continue
            debit = -_fill(p_out, CANON_FILL)
            fees = 2 * len(legs) * FEE / 100.0
            req_credit = debit + fees
            vals.append(req_credit / p_in["mid"])
        if vals:
            vs = sorted(vals)
            pct = lambda q: round(vs[min(len(vs) - 1,
                                         int(q / 100 * (len(vs) - 1)))], 3)
            be[sname] = {"n": len(vs), "median": pct(50),
                         "p25": pct(25), "p75": pct(75), "p90": pct(90),
                         "share_over_100pct": round(
                             100 * sum(1 for v in vs if v > 1) / len(vs))}
    result["breakeven_mid_capture"] = {
        "definition": "entry credit required for P&L=0 (exit f=0.50 at "
                      "base window, fees in) as a fraction of package "
                      "mid — >1.0 means better-than-mid entry needed",
        "by_structure": be}

    # Liquidity conditioning — predeclared buckets on entry package
    # spread% of mid, gross (f=0) vs executable (f=0.25).
    liq = {}
    for sname in STRUCTS:
        buckets = {"<=2%": [], "2-5%": [], "5-10%": [], ">10%": []}
        for key, r in events:
            pr0 = price_event(qc, r, sname, BASE_EXIT_WINDOW, 0.0)
            pr25 = price_event(qc, r, sname, BASE_EXIT_WINDOW, 0.25)
            if pr0 is None or pr25 is None \
                    or pr0.get("spread_pct_in") is None:
                continue
            sp = pr0["spread_pct_in"]
            b = "<=2%" if sp <= 2 else ("2-5%" if sp <= 5 else
                                        ("5-10%" if sp <= 10 else ">10%"))
            buckets[b].append((r["night_id"], pr0["pnl"], pr25["pnl"]))
        liq[sname] = {}
        for b, rows in buckets.items():
            if len(rows) < 10:
                liq[sname][b] = {"n": len(rows), "status": "thin"}
                continue
            g = [p for _, p, _ in rows]
            x = [p for _, _, p in rows]
            def pf(v):
                pos = sum(p for p in v if p > 0)
                neg = abs(sum(p for p in v if p < 0))
                return round(pos / neg, 2) if neg else None
            liq[sname][b] = {"n": len(rows), "gross_pf": pf(g),
                             "exec_pf_f25": pf(x),
                             "avg_exec_$": round(mean(x) * 100)}
    result["liquidity_buckets"] = liq

    # ------------------------- VOL_RICH_EXEC_V1 qualification -------
    verdicts = {}
    for sname in STRUCTS:
        if sname == "short_straddle":
            continue   # diagnostic: undefined risk never qualifies
        base = result["structures"][sname][BASE_EXIT_WINDOW]
        st25 = base.get(f"f{int(QUAL_FILL*100)}") or {}
        st50 = base.get(f"f{int(CANON_FILL*100)}") or {}
        verdicts[sname] = {"qualified": _qualifies(st25, st50),
                           "at_f25": {k: st25.get(k) for k in
                                      ("n", "avg_ror", "med_ror",
                                       "cap_wt_ror", "profit_factor",
                                       "exp_log_growth", "max_dd_r",
                                       "fold_avg_ror")},
                           "f50_avg_ror": st50.get("avg_ror")}
    qualified = next((s for s, v in verdicts.items() if v["qualified"]),
                     None)
    result["types"] = {"vol_rich": {
        "trade_qualified": bool(qualified),
        "qualifying_strategy": qualified,
        "gate": "VOL_RICH_EXEC_V1",
        "verdicts": verdicts,
        "criteria": {"fill": f"f={QUAL_FILL} entry+exit, NBBO package",
                     "min_n": MIN_N_QUALIFY, "avg_ror": "> 0",
                     "med_ror": "> 0", "cap_wt_ror": "> 0",
                     "night_ci_low": "> 0",
                     "profit_factor": f">= {PF_QUALIFY}",
                     "exp_log_growth": "> 0",
                     "folds": ">= 2/3 positive AND latest > 0",
                     "max_dd_r": f"> {MAX_DD_R}",
                     "f50_check": "avg ROR > 0 at 50% degradation"}},
        "vol_cheap": {
            "trade_qualified": False,
            "why": "excluded from the execution study: V2 found long "
                   "premium frictionless-NEGATIVE at every exit — "
                   "there is no gross edge to execute. The predictive "
                   "signal is retained; the naive trade expression is "
                   "killed."}}
    result["honesty"] = (
        "Quote-level, package-priced, fees-in. Entry = last NBBO in "
        "15:45-16:00 ET; exits = last NBBO in each predeclared "
        "morning window. Fill fractions are of the mid->natural "
        "distance per package side — a complex-order proxy, stamped, "
        "not exchange truth. Missing quotes are skipped and counted, "
        "never zero-filled. Structure set frozen at V2's candidates; "
        "no new structures were searched.")
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1)
    print("exec verdicts:", json.dumps(
        {s: v["qualified"] for s, v in verdicts.items()}))
    print("edge capture:", capture)
    print("breakeven capture:", json.dumps(be)[:400])
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--report-only", action="store_true")
    a = ap.parse_args()
    run(limit=a.limit, report_only=a.report_only)
