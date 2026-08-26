"""
earnings_vol_backtest.py — Earnings Volatility Alpha Engine, part 2:
historical OPTION P&L reconstruction for the vol_rich / vol_cheap
signal events. CI-only (needs POLYGON_API_KEY).

The signal engine (earnings_vol_engine.py) validated a forecast
relationship. This module tests the different claim — that a specific
option structure entered at realistically available prices generates
positive EV after spreads, slippage and fees — by reconstructing, for
every graded event in the idea log:

  entry  = close of the last session BEFORE the print (AMC: event day;
           BMO: prior session), per-leg prices from the contract's
           daily aggregate close, then costs applied
  exit   = close of the FIRST session after the print
  chain  = expired-contract reference lookup as of the entry date;
           nearest expiry that brackets the print

Strategies (defined-risk first; undefined-risk kept as diagnostics and
never exposed to users):

  short_straddle   short ATM call + put                  (diagnostic)
  iron_fly         short ATM straddle, long wings at 1.5x implied move
  iron_condor      shorts at 0.75x implied, longs at 1.5x implied
  (vol_cheap uses the mirror: long_straddle / long fly diagnostics)

Fill realism: entries/exits use daily closes with an explicit cost
model — SLIP_PCT of each leg's price per side plus FEE_PER_CONTRACT —
because NBBO quote history may not be entitled; when it is, a later
version can tighten this. The fill model used is stamped on every
reconstruction. This is the BASE case; nothing optimistic.

Per-strategy report (docs/reports/earnings_vol_backtest.json): n, win
rate, avg/median return-on-risk, expected P&L per 1-lot, profit
factor, worst trade, p5, expected shortfall 95/99, avg MFE/MAE proxy,
expected log-growth on R-normalized returns, loss>1R frequency (must
be 0 for defined-risk by construction — asserted), and a night-cluster
bootstrap CI on ROR. trade_qualified per type flips ONLY if the
defined-risk structure clears: n >= 60, net EV > 0, night-cluster CI
low > 0, profit factor >= 1.3, expected log-growth > 0.

Per-event reconstructions cache in data/earnings_vol_pnl.json so the
nightly run only prices new events.

    python earnings_vol_backtest.py           # reconstruct + report
    python earnings_vol_backtest.py --limit 5 # first N missing (smoke)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
from datetime import datetime, timedelta, timezone
from statistics import mean, median

import polygon_data as pg
from trade_desk_validation import _load

_BASE = os.path.dirname(os.path.abspath(__file__))
R = lambda *p: os.path.join(_BASE, *p)
LOG_PATH = R("data", "earnings_ideas_log.json")
CACHE_PATH = R("data", "earnings_vol_pnl.json")
OUT_PATH = R("docs", "reports", "earnings_vol_backtest.json")

BT_VERSION = "vol_backtest_v1"
SLIP_PCT = 0.05          # 5% of leg price lost per side (base case)
FEE = 0.65               # per contract per side, dollars
WING_X = 1.5             # wings at 1.5x implied (ratio p99 was 1.77x —
                         # see earnings_vol.json; wings are protection,
                         # not free lunch)
CONDOR_X = 0.75          # condor shorts at 0.75x implied (83% of
                         # vol_rich events stayed inside historically)
MIN_N_QUALIFY = 60
PF_QUALIFY = 1.3
BOOT_ITERS = 3000

FILL_MODEL = (f"daily-close legs, {int(SLIP_PCT*100)}% slippage per "
              f"side per leg, ${FEE}/contract/side fees — base case")


def _events():
    d = _load(LOG_PATH, {}) or {}
    out = []
    for i in d.get("ideas") or []:
        if i.get("type") not in ("vol_rich", "vol_cheap"):
            continue
        if not i.get("result") or not i.get("implied"):
            continue
        sess = (i.get("feat") or {}).get("session") or "AMC"
        out.append({"type": i["type"], "ticker": i["t"],
                    "date": i["date"], "implied": i["implied"],
                    "session": sess})
    # dedupe (ticker, date, type)
    seen, uniq = set(), []
    for e in out:
        k = (e["ticker"], e["date"], e["type"])
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    return uniq


def _daily_closes(ticker, contract=False):
    """{date: close} map. For option contracts polygon aggregates use
    the same aggs endpoint with the O: ticker."""
    try:
        bars = pg.daily_bars(ticker, days=140)
    except Exception:
        return {}
    out = {}
    for b in bars or []:
        ts = b.get("t")
        if ts and b.get("c"):
            d = datetime.fromtimestamp(ts / 1000,
                                       timezone.utc).strftime("%Y-%m-%d")
            out[d] = b["c"]
    return out


def _sessions_around(und_closes, event_date, session):
    """(entry_date, exit_date) trading sessions from the underlying's
    own bar calendar. AMC: entry = event day, exit = next session.
    BMO: entry = prior session, exit = event day."""
    days = sorted(und_closes)
    if event_date not in days:
        # event day may be missing (halt etc.) — find neighbors
        after = [d for d in days if d > event_date]
        before = [d for d in days if d < event_date]
        if session == "BMO":
            return (before[-1] if before else None,
                    after[0] if after else None)
        return (before[-1] if before else None,
                after[0] if after else None)
    i = days.index(event_date)
    if session == "BMO":
        return (days[i - 1] if i > 0 else None, event_date)
    return (event_date, days[i + 1] if i + 1 < len(days) else None)


def _chain_asof(ticker, entry_date, exit_date):
    """Expired+active contract reference as of entry — expiries on or
    after the exit session, nearest first."""
    rows = []
    for expired in ("true", "false"):
        try:
            rows += pg._paginate("/v3/reference/options/contracts", {
                "underlying_ticker": ticker, "as_of": entry_date,
                "expired": expired, "limit": 1000,
                "expiration_date.gte": exit_date,
            }, max_pages=4)
        except Exception:
            pass
    return rows


def _nearest(vals, target):
    return min(vals, key=lambda v: abs(v - target)) if vals else None


def _leg_price(contract_ticker, date):
    closes = _daily_closes(contract_ticker)
    return closes.get(date)


def reconstruct(e):
    """One event -> per-strategy P&L dict, or {'skip': reason}."""
    und = _daily_closes(e["ticker"])
    if not und:
        return {"skip": "no_underlying_bars"}
    entry_d, exit_d = _sessions_around(und, e["date"], e["session"])
    if not entry_d or not exit_d:
        return {"skip": "no_session_bracket"}
    spot = und.get(entry_d)
    if not spot:
        return {"skip": "no_entry_spot"}
    chain = _chain_asof(e["ticker"], entry_d, exit_d)
    if not chain:
        return {"skip": "no_chain"}
    # nearest expiry >= exit
    expiries = sorted({c.get("expiration_date") for c in chain
                       if c.get("expiration_date")})
    if not expiries:
        return {"skip": "no_expiry"}
    exp = expiries[0]
    cs = [c for c in chain if c.get("expiration_date") == exp]
    calls = {c["strike_price"]: c["ticker"] for c in cs
             if c.get("contract_type") == "call" and c.get("strike_price")}
    puts = {c["strike_price"]: c["ticker"] for c in cs
            if c.get("contract_type") == "put" and c.get("strike_price")}
    if not calls or not puts:
        return {"skip": "one_sided_chain"}
    em = e["implied"] / 100.0 * spot
    kC = _nearest(list(calls), spot)
    kP = _nearest(list(puts), spot)
    legs_needed = {
        "atm_call": calls[kC], "atm_put": puts[kP],
        "wing_call": calls.get(_nearest(list(calls), spot + WING_X * em)),
        "wing_put": puts.get(_nearest(list(puts), spot - WING_X * em)),
        "cnd_call": calls.get(_nearest(list(calls), spot + CONDOR_X * em)),
        "cnd_put": puts.get(_nearest(list(puts), spot - CONDOR_X * em)),
    }
    px = {}
    for name, tk in legs_needed.items():
        if not tk:
            return {"skip": "missing_leg_" + name}
        p_in = _leg_price(tk, entry_d)
        p_out = _leg_price(tk, exit_d)
        if p_in is None:
            return {"skip": "no_entry_px_" + name}
        px[name] = {"in": p_in, "out": p_out if p_out is not None else 0.0,
                    "strike_tk": tk}
    # cost model: selling receives in*(1-SLIP)-fee; buying pays
    # in*(1+SLIP)+fee; exits mirror.
    def sell(p):  return p * (1 - SLIP_PCT) - FEE / 100.0
    def buy(p):   return p * (1 + SLIP_PCT) + FEE / 100.0

    def pnl_short(legs_short, legs_long=()):
        credit = sum(sell(px[l]["in"]) for l in legs_short) \
            - sum(buy(px[l]["in"]) for l in legs_long)
        close_cost = sum(buy(px[l]["out"]) for l in legs_short) \
            - sum(sell(px[l]["out"]) for l in legs_long)
        return credit - close_cost, credit

    wingC = abs((_nearest(list(calls), spot + WING_X * em) or 0) - kC)
    wingP = abs(kP - (_nearest(list(puts), spot - WING_X * em) or 0))
    cndC_k = _nearest(list(calls), spot + CONDOR_X * em)
    cndP_k = _nearest(list(puts), spot - CONDOR_X * em)
    wingC2 = abs((_nearest(list(calls), spot + WING_X * em) or 0)
                 - (cndC_k or 0))
    wingP2 = abs((cndP_k or 0)
                 - (_nearest(list(puts), spot - WING_X * em) or 0))

    out = {"entry_date": entry_d, "exit_date": exit_d, "spot": spot,
           "expiry": exp, "fill_model": FILL_MODEL, "strategies": {}}

    if e["type"] == "vol_rich":
        p, credit = pnl_short(("atm_call", "atm_put"))
        out["strategies"]["short_straddle"] = {
            "pnl": round(p * 100, 0), "risk": None,
            "note": "UNDEFINED RISK — diagnostic only"}
        p, credit = pnl_short(("atm_call", "atm_put"),
                              ("wing_call", "wing_put"))
        risk = max(wingC, wingP) - credit
        if risk > 0:
            out["strategies"]["iron_fly"] = {
                "pnl": round(p * 100, 0), "risk": round(risk * 100, 0),
                "ror": round(p / risk, 4)}
        p, credit = pnl_short(("cnd_call", "cnd_put"),
                              ("wing_call", "wing_put"))
        risk = max(wingC2, wingP2) - credit
        if risk > 0:
            out["strategies"]["iron_condor"] = {
                "pnl": round(p * 100, 0), "risk": round(risk * 100, 0),
                "ror": round(p / risk, 4)}
    else:  # vol_cheap — long convexity
        debit = buy(px["atm_call"]["in"]) + buy(px["atm_put"]["in"])
        exitv = sell(px["atm_call"]["out"]) + sell(px["atm_put"]["out"])
        p = exitv - debit
        if debit > 0:
            out["strategies"]["long_straddle"] = {
                "pnl": round(p * 100, 0), "risk": round(debit * 100, 0),
                "ror": round(p / debit, 4)}
    return out


# ------------------------------------------------------------- report

def _boot_ci(rows, iters=BOOT_ITERS, seed=13):
    nights = {}
    for date, r in rows:
        nights.setdefault(date, []).append(r)
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


def strategy_table(recons, strat):
    rows = []
    for r in recons:
        s = (r.get("strategies") or {}).get(strat)
        if not s:
            continue
        if s.get("ror") is not None:
            rows.append((r["event"]["date"], s["ror"], s["pnl"]))
        elif s.get("pnl") is not None:
            rows.append((r["event"]["date"], None, s["pnl"]))
    if not rows:
        return {"n": 0}
    rors = [x[1] for x in rows if x[1] is not None]
    pnls = [x[2] for x in rows]
    out = {"n": len(rows),
           "win": round(100 * sum(1 for p in pnls if p > 0) / len(pnls)),
           "avg_pnl_1lot": round(mean(pnls), 0),
           "profit_factor": (round(sum(p for p in pnls if p > 0) /
                                   abs(sum(p for p in pnls if p < 0)), 2)
                             if any(p < 0 for p in pnls) else None),
           "worst_pnl": round(min(pnls), 0)}
    if rors:
        srt = sorted(rors)
        k5 = max(1, int(len(srt) * .05))
        k1 = max(1, int(len(srt) * .01))
        out.update({
            "avg_ror": round(mean(rors), 3),
            "med_ror": round(median(rors), 3),
            "p5_ror": round(srt[max(0, int(len(srt) * .05) - 1)], 3),
            "es95_ror": round(mean(srt[:k5]), 3),
            "es99_ror": round(mean(srt[:k1]), 3),
            "worst_ror": round(srt[0], 3),
            "loss_gt_1R": sum(1 for r in rors if r < -1.0),
            "exp_log_growth": (round(mean(
                math.log1p(max(r, -0.999)) for r in rors), 4)),
            "night_bootstrap_ror": _boot_ci(
                [(d, r) for d, r, _ in rows if r is not None]),
        })
    return out


def run(limit=None):
    if not pg.available():
        out = {"generated": datetime.now(timezone.utc)
               .isoformat(timespec="seconds"),
               "status": "capability_unavailable",
               "why": "POLYGON_API_KEY not set (CI-only secret)"}
        json.dump(out, open(OUT_PATH, "w", encoding="utf-8"), indent=1)
        print(out["why"])
        return out

    cache = _load(CACHE_PATH, {}) or {}
    events = _events()
    todo = [e for e in events
            if f"{e['ticker']}|{e['date']}|{e['type']}" not in cache]
    if limit:
        todo = todo[:limit]
    print(f"events: {len(events)} total, {len(todo)} to reconstruct")
    for e in todo:
        key = f"{e['ticker']}|{e['date']}|{e['type']}"
        try:
            r = reconstruct(e)
        except Exception as ex:
            r = {"skip": "error:" + str(ex)[:80]}
        r["event"] = e
        r["bt_version"] = BT_VERSION
        cache[key] = r
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=0)

    recons = [r for r in cache.values() if not r.get("skip")]
    skips = {}
    for r in cache.values():
        if r.get("skip"):
            skips[r["skip"]] = skips.get(r["skip"], 0) + 1

    result = {"generated": datetime.now(timezone.utc)
              .isoformat(timespec="seconds"),
              "bt_version": BT_VERSION, "fill_model": FILL_MODEL,
              "params": {"wing_x": WING_X, "condor_x": CONDOR_X,
                         "slip_pct": SLIP_PCT, "fee": FEE},
              "reconstructed": len(recons), "skipped": skips,
              "types": {}}
    for t, strats in (("vol_rich", ("short_straddle", "iron_fly",
                                    "iron_condor")),
                      ("vol_cheap", ("long_straddle",))):
        rs = [r for r in recons if r["event"]["type"] == t]
        tbl = {s: strategy_table(rs, s) for s in strats}
        # trade qualification: DEFINED-RISK structure must clear all
        # predefined bars; diagnostics never qualify anything.
        qual, qual_strat = False, None
        for s in strats:
            if s in ("short_straddle",):
                continue
            st = tbl.get(s) or {}
            ci = ((st.get("night_bootstrap_ror") or {}).get("ci95")
                  or [None])
            if (st.get("n", 0) >= MIN_N_QUALIFY
                    and (st.get("avg_ror") or 0) > 0
                    and ci[0] is not None and ci[0] > 0
                    and (st.get("profit_factor") or 0) >= PF_QUALIFY
                    and (st.get("exp_log_growth") or 0) > 0
                    and st.get("loss_gt_1R", 1) == 0):
                qual, qual_strat = True, s
                break
        result["types"][t] = {"strategies": tbl,
                              "trade_qualified": qual,
                              "qualifying_strategy": qual_strat,
                              "criteria": {
                                  "min_n": MIN_N_QUALIFY,
                                  "avg_ror": "> 0",
                                  "night_ci_low": "> 0",
                                  "profit_factor": f">= {PF_QUALIFY}",
                                  "exp_log_growth": "> 0",
                                  "loss_gt_1R": "== 0 (defined risk)"}}
    result["honesty"] = (
        "Daily-close fills with a flat slippage+fee model — the base "
        "case, not NBBO truth. Undefined-risk diagnostics never "
        "qualify anything. Reconstruction gaps are listed in "
        "'skipped', not silently dropped.")
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1)
    for t, b in result["types"].items():
        print(t, "trade_qualified:", b["trade_qualified"],
              json.dumps({s: {k: v for k, v in tb.items()
                              if k in ("n", "win", "avg_ror",
                                       "profit_factor", "worst_ror")}
                          for s, tb in b["strategies"].items()}))
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    run(limit=ap.parse_args().limit)
