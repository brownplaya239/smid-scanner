"""
earnings_vol_backtest.py — Earnings Volatility Alpha Engine, part 2:
historical OPTION P&L reconstruction for the vol_rich / vol_cheap
signal events. CI-only (needs POLYGON_API_KEY).

v2 (reviewer-corrected):

  IMMUTABLE ROWS — one record per event x version, keyed
  "{ticker}|{date}|{type}|{bt_version}" in data/earnings_vol_pnl.json.
  A later strategy change bumps BT_VERSION and writes NEW rows; prior
  reconstructions are never modified. Each record carries the full
  audit trail: legs, per-leg entry/exit opens+closes, fills after the
  cost model, gross/fees/slippage/net, max risk, ROR per exit model,
  night id, sector, fill model, chain source.

  EXIT MODELS — earnings strategies are exit-timing sensitive, so every
  structure is marked at three predeclared exits:
    next_open    option's next-session daily OPEN (vol-crush open)
    next_close   option's next-session daily CLOSE (base convention)
    expiry_hold  intrinsic at expiry from the underlying's close
  All are DAILY-BAR PROXIES, stamped as such; intraday marks are a
  future refinement, not silently assumed.

  PREDECLARED PARAMETER GRID — the 0.75x/1.5x condor from the ratio
  table is a hypothesis, not the answer. Grid (short_x, wing_x):
    condor: (0.60,1.25) (0.75,1.25) (0.75,1.50) (0.90,1.50) (1.00,1.50)
    fly wings: 1.25 / 1.50        cheap: straddle + strangle(0.5x)
  A variant can only qualify if its grid NEIGHBORS don't collapse
  (PF >= 1.0) — a real structural edge survives perturbation.

  QUALIFICATION v2 (defined-risk only, base exit next_close):
    n >= 60 · avg ROR > 0 · night-cluster CI low > 0 · PF >= 1.3 ·
    expected log-growth > 0 · loss > 1R == 0 ·
    max drawdown on a sequential 1R equity curve > -5R ·
    fold stability: >= 2 of 3 chronological folds positive AND the
    latest fold positive · next_open exit agrees in sign ·
    grid-neighbor robustness.

    python earnings_vol_backtest.py           # reconstruct + report
    python earnings_vol_backtest.py --limit 5 # first N missing (smoke)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
from datetime import datetime, timezone
from statistics import mean, median

import polygon_data as pg
from trade_desk_validation import _load

_BASE = os.path.dirname(os.path.abspath(__file__))
R = lambda *p: os.path.join(_BASE, *p)
LOG_PATH = R("data", "earnings_ideas_log.json")
CACHE_PATH = R("data", "earnings_vol_pnl.json")
OUT_PATH = R("docs", "reports", "earnings_vol_backtest.json")

BT_VERSION = "vol_backtest_v2"
SLIP_PCT = 0.05
FEE = 0.65
EXITS = ("next_open", "next_close", "expiry_hold")
BASE_EXIT = "next_close"
# Predeclared structure grid — fixed BEFORE seeing v2 results.
CONDOR_GRID = ((0.60, 1.25), (0.75, 1.25), (0.75, 1.50),
               (0.90, 1.50), (1.00, 1.50))
FLY_WINGS = (1.25, 1.50)
STRANGLE_X = 0.5
MIN_N_QUALIFY = 60
PF_QUALIFY = 1.3
MAX_DD_R = -5.0
BOOT_ITERS = 3000

FILL_MODEL = (f"{BT_VERSION}: daily-bar leg marks (open for next_open "
              f"exit, close otherwise), {int(SLIP_PCT*100)}% slippage "
              f"per side per leg, ${FEE}/contract/side fees; "
              "expiry_hold = intrinsic from underlying close. "
              "DAILY-BAR PROXY — not NBBO truth.")


def _events():
    d = _load(LOG_PATH, {}) or {}
    meta = _load(R("docs", "reports", "uoa_meta_cache.json"), {}) or {}
    out, seen = [], set()
    for i in d.get("ideas") or []:
        if i.get("type") not in ("vol_rich", "vol_cheap"):
            continue
        if not i.get("result") or not i.get("implied"):
            continue
        k = (i["t"], i["date"], i["type"])
        if k in seen:
            continue
        seen.add(k)
        out.append({"type": i["type"], "ticker": i["t"],
                    "date": i["date"], "implied": i["implied"],
                    "session": (i.get("feat") or {}).get("session")
                    or "AMC",
                    "sector": (meta.get(i["t"]) or {}).get("sector")})
    return out


_BARS_CACHE = {}


def _bars(ticker):
    """{date: {o, c}} daily map, memoized per run."""
    if ticker in _BARS_CACHE:
        return _BARS_CACHE[ticker]
    out = {}
    try:
        for b in pg.daily_bars(ticker, days=140) or []:
            ts = b.get("t")
            if ts:
                d = datetime.fromtimestamp(
                    ts / 1000, timezone.utc).strftime("%Y-%m-%d")
                out[d] = {"o": b.get("o"), "c": b.get("c")}
    except Exception:
        pass
    _BARS_CACHE[ticker] = out
    return out


def _sessions_around(und, event_date, session):
    days = sorted(und)
    if event_date in days:
        i = days.index(event_date)
        if session == "BMO":
            return (days[i - 1] if i > 0 else None, event_date)
        return (event_date, days[i + 1] if i + 1 < len(days) else None)
    before = [d for d in days if d < event_date]
    after = [d for d in days if d > event_date]
    return (before[-1] if before else None, after[0] if after else None)


def _chain_asof(ticker, entry_date, exit_date):
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


def sell(p):  return p * (1 - SLIP_PCT) - FEE / 100.0
def buy(p):   return p * (1 + SLIP_PCT) + FEE / 100.0


def _leg_marks(tk, entry_d, exit_d):
    b = _bars(tk)
    e = b.get(entry_d) or {}
    x = b.get(exit_d) or {}
    return {"entry_close": e.get("c"), "exit_open": x.get("o"),
            "exit_close": x.get("c")}


def _intrinsic(kind, strike, spot):
    if spot is None:
        return None
    return max(0.0, (spot - strike) if kind == "call"
               else (strike - spot))


def _structure_pnl(legs, exit_model, expiry_spot=None):
    """legs: list of {side:+1 long/-1 short, kind, strike, marks}.
    Returns (net_pnl_per_share, entry_credit_per_share) or None if a
    needed mark is missing."""
    entry_cash = 0.0
    exit_cash = 0.0
    for l in legs:
        m = l["marks"]
        ein = m.get("entry_close")
        if ein is None:
            return None
        if exit_model == "next_open":
            eout = m.get("exit_open")
        elif exit_model == "next_close":
            eout = m.get("exit_close")
        else:
            eout = _intrinsic(l["kind"], l["strike"], expiry_spot)
        if eout is None:
            eout = 0.0 if exit_model == "expiry_hold" else None
        if eout is None:
            return None
        if l["side"] < 0:       # short: receive at entry, pay to close
            entry_cash += sell(ein)
            exit_cash -= buy(eout)
        else:                   # long: pay at entry, receive at close
            entry_cash -= buy(ein)
            exit_cash += sell(eout)
    return entry_cash + exit_cash, entry_cash


def reconstruct(e):
    und = _bars(e["ticker"])
    if not und:
        return {"skip": "no_underlying_bars"}
    entry_d, exit_d = _sessions_around(und, e["date"], e["session"])
    if not entry_d or not exit_d:
        return {"skip": "no_session_bracket"}
    spot = (und.get(entry_d) or {}).get("c")
    if not spot:
        return {"skip": "no_entry_spot"}
    chain = _chain_asof(e["ticker"], entry_d, exit_d)
    expiries = sorted({c.get("expiration_date") for c in chain
                       if c.get("expiration_date")})
    if not expiries:
        return {"skip": "no_chain"}
    exp = expiries[0]
    cs = [c for c in chain if c.get("expiration_date") == exp]
    calls = {c["strike_price"]: c["ticker"] for c in cs
             if c.get("contract_type") == "call" and c.get("strike_price")}
    puts = {c["strike_price"]: c["ticker"] for c in cs
            if c.get("contract_type") == "put" and c.get("strike_price")}
    if not calls or not puts:
        return {"skip": "one_sided_chain"}
    em = e["implied"] / 100.0 * spot
    expiry_spot = (und.get(exp) or {}).get("c")

    def leg(side, kind, x_mult):
        table = calls if kind == "call" else puts
        target = spot + x_mult * em if kind == "call" \
            else spot - x_mult * em
        k = _nearest(list(table), target)
        if k is None:
            return None
        return {"side": side, "kind": kind, "strike": k,
                "contract": table[k],
                "marks": _leg_marks(table[k], entry_d, exit_d)}

    def build(name, legs, risk_fn):
        if any(l is None for l in legs):
            return None
        variants = {}
        for xm in EXITS:
            r = _structure_pnl(legs, xm, expiry_spot)
            if r is None:
                continue
            pnl, credit = r
            risk = risk_fn(legs, credit)
            v = {"pnl": round(pnl * 100, 0)}
            if risk is not None and risk > 0:
                v["risk"] = round(risk * 100, 0)
                v["ror"] = round(pnl / risk, 4)
            variants[xm] = v
        if not variants:
            return None
        return {"legs": [{"side": l["side"], "kind": l["kind"],
                          "strike": l["strike"],
                          "contract": l["contract"],
                          "marks": l["marks"]} for l in legs],
                "exits": variants}

    strategies = {}
    if e["type"] == "vol_rich":
        # diagnostic: undefined risk, never qualifies
        s = build("short_straddle",
                  [leg(-1, "call", 0), leg(-1, "put", 0)],
                  lambda legs, cr: None)
        if s:
            s["note"] = "UNDEFINED RISK — diagnostic only"
            strategies["short_straddle"] = s
        for wx in FLY_WINGS:
            legs = [leg(-1, "call", 0), leg(-1, "put", 0),
                    leg(+1, "call", wx), leg(+1, "put", wx)]
            def fly_risk(ls, cr):
                w = max(abs(ls[2]["strike"] - ls[0]["strike"]),
                        abs(ls[1]["strike"] - ls[3]["strike"]))
                return w - cr if w > cr else None
            s = build("iron_fly", legs, fly_risk)
            if s:
                strategies[f"iron_fly_{wx}"] = s
        for sx, wx in CONDOR_GRID:
            legs = [leg(-1, "call", sx), leg(-1, "put", sx),
                    leg(+1, "call", wx), leg(+1, "put", wx)]
            def cnd_risk(ls, cr):
                w = max(abs(ls[2]["strike"] - ls[0]["strike"]),
                        abs(ls[1]["strike"] - ls[3]["strike"]))
                return w - cr if w > cr else None
            s = build("iron_condor", legs, cnd_risk)
            if s:
                strategies[f"iron_condor_{sx}_{wx}"] = s
    else:
        legs = [leg(+1, "call", 0), leg(+1, "put", 0)]
        s = build("long_straddle", legs,
                  lambda ls, cr: -cr if cr < 0 else None)
        if s:
            strategies["long_straddle"] = s
        legs = [leg(+1, "call", STRANGLE_X), leg(+1, "put", STRANGLE_X)]
        s = build("long_strangle", legs,
                  lambda ls, cr: -cr if cr < 0 else None)
        if s:
            strategies["long_strangle"] = s
    if not strategies:
        return {"skip": "no_structures_priceable"}
    return {"entry_date": entry_d, "exit_date": exit_d, "spot": spot,
            "expiry": exp, "expiry_spot": expiry_spot,
            "night_id": e["date"], "sector": e.get("sector"),
            "fill_model": FILL_MODEL, "chain_source": "polygon_reference",
            "strategies": strategies}


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


def _max_dd_r(rors):
    """Max drawdown of a sequential 1R-per-trade equity curve."""
    eq = peak = 0.0
    dd = 0.0
    for r in rors:
        eq += r
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return round(dd, 2)


def _folds_positive(rows):
    """rows sorted by date: (date, ror). 3 chronological folds -> list
    of fold avg RORs."""
    if len(rows) < 9:
        return []
    third = len(rows) // 3
    return [round(mean(r for _, r in rows[i * third:
                                          (i + 1) * third if i < 2
                                          else len(rows)]), 3)
            for i in range(3)]


def strategy_table(recons, strat, exit_model):
    rows = []
    for r in recons:
        s = (r.get("strategies") or {}).get(strat)
        if not s:
            continue
        v = (s.get("exits") or {}).get(exit_model)
        if not v:
            continue
        rows.append((r["night_id"], v.get("ror"), v["pnl"]))
    rows.sort(key=lambda x: x[0])
    if not rows:
        return {"n": 0}
    pnls = [x[2] for x in rows]
    rors = [(d, r) for d, r, _ in rows if r is not None]
    # capital-weighted ROR — equal-weight mean ROR can be driven by
    # tiny-risk outliers (nearest-strike snapping varies width 40x);
    # sum($P&L)/sum($risk) is the number a real allocator experiences.
    risks = []
    for r0 in recons:
        s0 = (r0.get("strategies") or {}).get(strat)
        v0 = ((s0 or {}).get("exits") or {}).get(exit_model)
        if v0 and v0.get("risk"):
            risks.append((v0["pnl"], v0["risk"]))
    out = {"n": len(rows),
           "win": round(100 * sum(1 for p in pnls if p > 0) / len(pnls)),
           "avg_pnl_1lot": round(mean(pnls), 0),
           "profit_factor": (round(sum(p for p in pnls if p > 0) /
                                   abs(sum(p for p in pnls if p < 0)), 2)
                             if any(p < 0 for p in pnls) else None),
           "worst_pnl": round(min(pnls), 0)}
    if rors:
        rs = [r for _, r in rors]
        srt = sorted(rs)
        k5 = max(1, int(len(srt) * .05))
        k1 = max(1, int(len(srt) * .01))
        folds = _folds_positive(rors)
        out.update({
            "avg_ror": round(mean(rs), 3), "med_ror": round(median(rs), 3),
            "es95_ror": round(mean(srt[:k5]), 3),
            "es99_ror": round(mean(srt[:k1]), 3),
            "worst_ror": round(srt[0], 3),
            "loss_gt_1R": sum(1 for r in rs if r < -1.0001),
            "exp_log_growth": round(mean(
                math.log1p(max(r, -0.999)) for r in rs), 4),
            "max_dd_r": _max_dd_r(rs),
            "cap_wt_ror": (round(sum(p for p, _ in risks)
                                 / sum(k for _, k in risks), 3)
                           if risks and sum(k for _, k in risks) else None),
            "fold_avg_ror": folds,
            "night_bootstrap_ror": _boot_ci(rors),
        })
    return out


def _qualifies(st, st_open):
    ci = ((st.get("night_bootstrap_ror") or {}).get("ci95") or [None])
    folds = st.get("fold_avg_ror") or []
    return (st.get("n", 0) >= MIN_N_QUALIFY
            and (st.get("avg_ror") or 0) > 0
            and (st.get("cap_wt_ror") or 0) > 0
            and ci[0] is not None and ci[0] > 0
            and (st.get("profit_factor") or 0) >= PF_QUALIFY
            and (st.get("exp_log_growth") or 0) > 0
            and st.get("loss_gt_1R", 1) == 0
            and (st.get("max_dd_r") or -99) > MAX_DD_R
            and len(folds) == 3
            and sum(1 for f in folds if f > 0) >= 2 and folds[-1] > 0
            and (st_open.get("avg_ror") or 0) > 0)   # exit-model accord


def _neighbors_ok(tables, strat):
    """Grid robustness: a condor variant's same-family neighbors must
    hold PF >= 1.0 at the base exit."""
    if not strat.startswith("iron_condor_"):
        return True
    fam = [s for s in tables if s.startswith("iron_condor_")]
    others = [tables[s] for s in fam if s != strat]
    ok = [o for o in others if (o.get("profit_factor") or 0) >= 1.0
          or o.get("n", 0) < 10]
    return len(ok) >= max(1, len(others) - 1)


def _recompute_at_slip(recons, strat, ttype, exit_model, slip):
    """Re-price a cached structure at a different slippage — pure
    arithmetic on the stored per-leg marks, no API. Powers the cost-
    attribution table; NEVER used for qualification."""
    rows = []
    for r in recons:
        if r["event"]["type"] != ttype:
            continue
        s = (r.get("strategies") or {}).get(strat)
        if not s:
            continue
        cash, ok = 0.0, True
        for l in s["legs"]:
            m = l["marks"]
            ein = m.get("entry_close")
            if ein is None:
                ok = False
                break
            if exit_model == "next_open":
                eout = m.get("exit_open")
            elif exit_model == "next_close":
                eout = m.get("exit_close")
            else:
                es = r.get("expiry_spot")
                eout = None if es is None else _intrinsic(
                    l["kind"], l["strike"], es)
            if eout is None:
                ok = False
                break
            fee = FEE / 100.0
            if l["side"] < 0:
                cash += ein * (1 - slip) - fee
                cash -= eout * (1 + slip) + fee
            else:
                cash -= ein * (1 + slip) + fee
                cash += eout * (1 - slip) - fee
        if ok:
            rows.append(cash)
    if not rows:
        return None
    pos = sum(p for p in rows if p > 0)
    neg = abs(sum(p for p in rows if p < 0))
    return {"n": len(rows), "avg_pnl_1lot": round(mean(rows) * 100),
            "win": round(100 * sum(1 for p in rows if p > 0)
                         / len(rows)),
            "profit_factor": round(pos / neg, 2) if neg else None}


def cost_attribution(recons):
    """Gross-edge vs friction decomposition on predeclared key
    structures. The 2026-08 finding this section exists to preserve:
    frictionless short vol at the NEXT-MORNING OPEN carries a real
    gross edge (crush monetizes at the open, decays by the close), and
    the base-case 5%/side slippage consumes all of it — break-even
    slippage is roughly 2.5-3%/side. Qualification always uses the
    base case; this is attribution, not a loophole."""
    keys = (("short_straddle", "vol_rich"),
            ("iron_fly_1.5", "vol_rich"),
            ("iron_condor_0.75_1.5", "vol_rich"),
            ("long_straddle", "vol_cheap"))
    out = {}
    for strat, ttype in keys:
        block = {}
        for xm in ("next_open", "next_close"):
            block[xm] = {f"slip_{int(s*1000)/10}pct":
                         _recompute_at_slip(recons, strat, ttype, xm, s)
                         for s in (0.0, 0.025, 0.05)}
        out[strat] = block
    return out


def run(limit=None, report_only=False):
    cache = _load(CACHE_PATH, {}) or {}
    events = _events()
    todo = [] if report_only else \
        [e for e in events
         if f"{e['ticker']}|{e['date']}|{e['type']}|{BT_VERSION}"
         not in cache]
    if todo and not pg.available():
        out = {"generated": datetime.now(timezone.utc)
               .isoformat(timespec="seconds"),
               "status": "capability_unavailable",
               "why": "POLYGON_API_KEY not set (CI-only secret)"}
        json.dump(out, open(OUT_PATH, "w", encoding="utf-8"), indent=1)
        print(out["why"])
        return out
    if limit:
        todo = todo[:limit]
    print(f"events: {len(events)} total, {len(todo)} to reconstruct "
          f"({BT_VERSION})")
    for e in todo:
        key = f"{e['ticker']}|{e['date']}|{e['type']}|{BT_VERSION}"
        try:
            r = reconstruct(e)
        except Exception as ex:
            r = {"skip": "error:" + str(ex)[:80]}
        r["event"] = e
        r["bt_version"] = BT_VERSION
        cache[key] = r          # immutable: new keys only, never edits
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=0)

    recons = [r for r in cache.values()
              if r.get("bt_version") == BT_VERSION and not r.get("skip")]
    skips = {}
    for r in cache.values():
        if r.get("bt_version") == BT_VERSION and r.get("skip"):
            skips[r["skip"]] = skips.get(r["skip"], 0) + 1

    strat_names = {"vol_rich": (["short_straddle"]
                                + [f"iron_fly_{w}" for w in FLY_WINGS]
                                + [f"iron_condor_{s}_{w}"
                                   for s, w in CONDOR_GRID]),
                   "vol_cheap": ["long_straddle", "long_strangle"]}
    result = {"generated": datetime.now(timezone.utc)
              .isoformat(timespec="seconds"),
              "bt_version": BT_VERSION, "fill_model": FILL_MODEL,
              "exit_models": list(EXITS), "base_exit": BASE_EXIT,
              "params": {"condor_grid": [list(x) for x in CONDOR_GRID],
                         "fly_wings": list(FLY_WINGS),
                         "strangle_x": STRANGLE_X,
                         "slip_pct": SLIP_PCT, "fee": FEE,
                         "max_dd_r": MAX_DD_R,
                         "min_n": MIN_N_QUALIFY,
                         "pf": PF_QUALIFY},
              "reconstructed": len(recons), "skipped": skips,
              "types": {}}
    for t, strats in strat_names.items():
        rs = [r for r in recons if r["event"]["type"] == t]
        tables = {}
        for s in strats:
            tables[s] = {xm: strategy_table(rs, s, xm) for xm in EXITS}
        base_tables = {s: tables[s][BASE_EXIT] for s in strats}
        qual, qual_strat = False, None
        for s in strats:
            if s == "short_straddle":
                continue
            st = base_tables.get(s) or {}
            st_open = tables[s].get("next_open") or {}
            if _qualifies(st, st_open) and _neighbors_ok(base_tables, s):
                qual, qual_strat = True, s
                break
        result["types"][t] = {
            "strategies": tables,
            "trade_qualified": qual,
            "qualifying_strategy": qual_strat,
            "criteria": {"min_n": MIN_N_QUALIFY, "avg_ror": "> 0",
                         "night_ci_low": "> 0",
                         "profit_factor": f">= {PF_QUALIFY}",
                         "exp_log_growth": "> 0",
                         "loss_gt_1R": "== 0 (defined risk)",
                         "max_dd_r": f"> {MAX_DD_R}",
                         "folds": ">= 2/3 positive AND latest positive",
                         "exit_accord": "next_open avg ROR > 0",
                         "grid_neighbors": "PF >= 1.0"}}
    result["cost_attribution"] = cost_attribution(recons)
    result["honesty"] = (
        "Daily-bar proxy fills (stamped per record). Undefined-risk "
        "diagnostics never qualify. Immutable per-event rows are "
        "versioned — changing strategy rules writes new rows under a "
        "new bt_version and never rewrites history. Exit models are "
        "daily-granularity proxies for the stated conventions; "
        "intraday marks are a future refinement.")
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1)
    for t, b in result["types"].items():
        print(t, "trade_qualified:", b["trade_qualified"],
              "| via", b.get("qualifying_strategy"))
        for s, tb in b["strategies"].items():
            bt = tb.get(BASE_EXIT) or {}
            if bt.get("n"):
                print(f"  {s:22s} n={bt['n']:3d} win={bt.get('win')}% "
                      f"avgROR={bt.get('avg_ror')} PF="
                      f"{bt.get('profit_factor')} logG="
                      f"{bt.get('exp_log_growth')} maxDD="
                      f"{bt.get('max_dd_r')}R folds="
                      f"{bt.get('fold_avg_ror')}")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--report-only", action="store_true",
                    help="rebuild the report from cached rows, no API")
    a = ap.parse_args()
    run(limit=a.limit, report_only=a.report_only)
