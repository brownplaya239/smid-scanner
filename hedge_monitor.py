"""
hedge_monitor.py — append-only prospective monitor for the potential
hedge cohort (put_buy, 22<=dte<=60). Two entry points:

  python hedge_monitor.py --record     # per scan batch (uoa.yml)
  python hedge_monitor.py --evaluate   # nightly (momentum.yml)

FROZEN WINDOW DISCIPLINE: the cohort definition, thresholds, episode
definition, and promotion gates below are FROZEN as of 2026-07-21.
Every record carries the sha256 of the frozen spec + of this file's
source + of the record schema. The evaluator only pools records whose
frozen-spec hash matches the current one — ANY change to the spec
creates a new hm_ver whose prospective evaluation RESTARTS from zero.
Retrospective tuning inside a version is therefore structurally
impossible: old records are immutable (append-only JSONL) and a tuned
spec cannot claim them.

PROMOTION GATES (frozen): the cohort is NOT promoted to a hedge sleeve
unless the prospective window observes >= 2 INDEPENDENT adverse
episodes AND achieves payoff/carry >= 1.0 under the executable
assumptions recorded at scan time. Alpha eligibility is a SEPARATE
conclusion with its own (already-published) gates — never merged.

  data/hedge_monitor_log.jsonl       append-only point-in-time records
  docs/reports/hedge_monitor.json    gated evaluation for the site
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

_BASE = os.path.dirname(os.path.abspath(__file__))
LATEST = os.path.join(_BASE, "docs", "reports", "uoa_latest.json")
REGIME = os.path.join(_BASE, "docs", "reports", "regime_history.json")
LOG = os.path.join(_BASE, "data", "hedge_monitor_log.jsonl")
OUT = os.path.join(_BASE, "docs", "reports", "hedge_monitor.json")
WORKER = "https://api.tickerdesk.io/"

# ── FROZEN SPEC (2026-07-21) — changing ANY value forks a new version ──
FROZEN = {
    "hm_ver": "v1-2026-07-21",
    "cohort": "type==put AND flow_side not seller AND 22<=dte<=60",
    "adverse_episode": ("SPY close-to-close day return <= -1.0%; "
                        "adverse dates within 2 trading days of each "
                        "other merge into ONE episode"),
    "strength_metric": "cohort premium share of batch total premium",
    "horizons_d": [1, 3, 5],
    "fill_assumption": ("last trade price per contract, 15-min delayed; "
                        "spread haircut by liquidity grade on entry+exit: "
                        "A 0.4 / B 0.7 / C 1.5 / D 2.5 pp of underlying-"
                        "equivalent; index puts: recorded ATM quote, "
                        "0.3pp haircut"),
    "hedge_budget_usd": 10_000,
    "promotion_gates": {
        "hedge_sleeve": [">=2 independent adverse episodes observed",
                          "payoff/carry >= 1.0 executable"],
        "alpha": "separate conclusion — see uoa_research avoidance gates",
    },
}
FROZEN_HASH = hashlib.sha256(
    json.dumps(FROZEN, sort_keys=True).encode()).hexdigest()[:16]

# ── MEASUREMENT SEMANTICS (separate role from the hypothesis) ──────────
# How quantities are MEASURED: parsing, normalization, quote selection,
# fill construction, signal calculation, and the daily-aggregation rule.
# The evaluator pools records only when BOTH frozen_spec_hash AND
# measurement_hash match. A measurement change (even with the hypothesis
# untouched) forks meas_ver; nonsemantic code changes (comments, logging,
# refactors) explicitly RETAIN the measurement version. code_hash is an
# audit trail only and never gates pooling.
MEASUREMENT = {
    "meas_ver": "m1-2026-07-21",
    "membership_parse": ("uoa_latest rows: type=='put' AND flow_side not "
                         "in (put_seller, call_seller) AND 22<=dte<=60"),
    "strength_calc": "sum(member premium) / sum(all-row premium)",
    "fill_construction": ("member last_price as logged (15-min delayed); "
                          "haircuts A0.4/B0.7/C1.5/D2.5 pp applied at "
                          "evaluation, not recording"),
    "index_put_selection": ("worker chain0 rows: t=='P', furthest of the "
                            "<=2 served expiries, nearest strike at/below "
                            "spot, px=last. PROXY_ONLY: short-dated "
                            "(endpoint serves nearest 2 expiries) — can "
                            "NEVER satisfy a duration-matched "
                            "comparative-efficiency promotion claim. "
                            "Genuine 22-60 DTE quotes require a new "
                            "meas_ver."),
    "daily_aggregation": ("FIRST eligible record per ET session date is "
                          "THE day's independent observation — fixed "
                          "ex-ante; the strongest intraday signal is "
                          "never retrospectively selected"),
    "market_quotes": "yfinance daily closes; VIX ^VIX; trend = 5d pct",
}
MEAS_HASH = hashlib.sha256(
    json.dumps(MEASUREMENT, sort_keys=True).encode()).hexdigest()[:16]
HAIRCUT = {"A": 0.4, "B": 0.7, "C": 1.5, "D": 2.5}
EXCLUSIONS = os.path.join(_BASE, "data", "hedge_monitor_exclusions.jsonl")

SCHEMA_FIELDS = sorted([
    "ts", "hm_ver", "frozen_hash", "meas_ver", "measurement_hash",
    "code_hash", "schema_hash",
    "batch_generated", "cohort_n", "cohort_premium", "batch_premium",
    "strength", "members", "spy", "qqq", "vix", "vix_chg",
    "spy_trend5", "breadth_regime", "batch_putcall_prem",
    "book_bull_share", "index_put_spy", "index_put_qqq",
])
SCHEMA_HASH = hashlib.sha256(
    ",".join(SCHEMA_FIELDS).encode()).hexdigest()[:16]


def _code_hash():
    try:
        with open(os.path.abspath(__file__), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except Exception:
        return "?"


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _yf_quotes():
    """SPY/QQQ/VIX point-in-time via yfinance (keyless, already a repo
    dependency). Returns dict or Nones on failure — fail-open."""
    out = {"spy": None, "qqq": None, "vix": None, "vix_chg": None,
           "spy_trend5": None}
    try:
        import yfinance as yf
        h = yf.download(["SPY", "QQQ", "^VIX"], period="10d",
                        interval="1d", auto_adjust=True,
                        group_by="ticker", progress=False, threads=True)
        spy = h["SPY"]["Close"].dropna()
        qqq = h["QQQ"]["Close"].dropna()
        vix = h["^VIX"]["Close"].dropna()
        out["spy"] = round(float(spy.iloc[-1]), 2)
        out["qqq"] = round(float(qqq.iloc[-1]), 2)
        if len(spy) >= 6:
            out["spy_trend5"] = round(
                100 * (float(spy.iloc[-1]) / float(spy.iloc[-6]) - 1), 2)
        out["vix"] = round(float(vix.iloc[-1]), 2)
        if len(vix) >= 2:
            out["vix_chg"] = round(float(vix.iloc[-1]) -
                                   float(vix.iloc[-2]), 2)
    except Exception:
        pass
    return out


def _index_put(sym):
    """Nearest-expiry ~ATM put from the worker chain snapshot — the
    executable index-hedge quote recorded point-in-time. Fail-open."""
    try:
        req = urllib.request.Request(WORKER + "?chain0=" + sym,
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
        spot = d.get("spot")
        # worker shape: rows=[{k,t('C'/'P'),e(expiry),last,oi,iv,...}];
        # take the FURTHER of the (max two) expiries for ~matched
        # duration, nearest ATM strike at/below spot.
        expiries = d.get("expiries") or []
        want_e = expiries[-1] if expiries else None
        best = None
        for c in (d.get("rows") or []):
            if c.get("t") not in ("P", "put"):
                continue
            if want_e and c.get("e") != want_e:
                continue
            k = c.get("k")
            px = c.get("last")
            if not spot or k is None or not px or k > spot:
                continue
            if best is None or (spot - k) < (spot - best["strike"]):
                best = {"strike": k, "px": round(px, 2),
                        "expiry": c.get("e"), "spot": round(spot, 2),
                        "label": "PROXY_ONLY"}
        return best
    except Exception:
        return None


def _regime():
    d = _load(REGIME) or {}
    days = d.get("days") or d.get("history") or []
    if isinstance(days, list) and days:
        return (days[-1] or {}).get("label")
    return None


def record():
    snap = _load(LATEST)
    if not snap or not snap.get("rows"):
        print("  hedge monitor: no batch to record")
        return
    rows = snap["rows"]
    members = []
    coh_prem = 0.0
    for r in rows:
        if (r.get("type") == "put"
                and r.get("flow_side") not in ("put_seller", "call_seller")
                and r.get("dte") is not None and 22 <= r["dte"] <= 60):
            members.append({
                "ticker": r["ticker"], "strike": r["strike"],
                "expiry": r["expiry"], "dte": r["dte"],
                "premium": r.get("premium"),
                "last_price": r.get("last_price"),
                "liquidity": r.get("liquidity"),
                "spot": r.get("spot"),
            })
            coh_prem += r.get("premium") or 0
    batch_prem = sum(r.get("premium") or 0 for r in rows) or 1.0
    put_prem = sum(r.get("premium") or 0 for r in rows
                   if r.get("type") == "put")
    call_prem = sum(r.get("premium") or 0 for r in rows
                    if r.get("type") == "call")
    bull = sum(r.get("premium") or 0 for r in rows
               if (r.get("type") == "call") !=
               (r.get("flow_side") in ("put_seller", "call_seller")))
    q = _yf_quotes()
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hm_ver": FROZEN["hm_ver"],
        "frozen_hash": FROZEN_HASH,
        "meas_ver": MEASUREMENT["meas_ver"],
        "measurement_hash": MEAS_HASH,
        "code_hash": _code_hash(),      # audit trail only — never pools
        "schema_hash": SCHEMA_HASH,
        "batch_generated": snap.get("generated"),
        "cohort_n": len(members),
        "cohort_premium": round(coh_prem),
        "batch_premium": round(batch_prem),
        "strength": round(coh_prem / batch_prem, 4),
        "members": members,
        "spy": q["spy"], "qqq": q["qqq"],
        "vix": q["vix"], "vix_chg": q["vix_chg"],
        "spy_trend5": q["spy_trend5"],
        "breadth_regime": _regime(),
        "batch_putcall_prem": round(put_prem / call_prem, 3)
                              if call_prem else None,
        "book_bull_share": round(bull / batch_prem, 3),
        "index_put_spy": _index_put("SPY"),
        "index_put_qqq": _index_put("QQQ"),
    }
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:      # APPEND-ONLY
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    print("  hedge monitor: recorded batch %s · cohort n=%d strength=%.3f"
          % (rec["batch_generated"], rec["cohort_n"], rec["strength"]))


# ── evaluator ──────────────────────────────────────────────────────────

# two-sided t critical values (95%) for tiny df — small-sample correction
_TCRIT = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45,
          7: 2.36, 8: 2.31, 9: 2.26, 10: 2.23, 15: 2.13, 20: 2.09,
          30: 2.04}


def _tcrit(df):
    for k in sorted(_TCRIT):
        if df <= k:
            return _TCRIT[k]
    return 1.96


def _mean(v):
    return sum(v) / len(v) if v else None


def evaluate():
    # append-only exclusion manifest: quarantined records stay immutable
    # in the log but never pool; each exclusion documents its reason.
    excluded = {}
    if os.path.exists(EXCLUSIONS):
        with open(EXCLUSIONS, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                    excluded[e["record_ts"]] = e.get("reason", "?")
                except Exception:
                    continue
    recs, n_quarantined, n_meas_mismatch = [], 0, 0
    if os.path.exists(LOG):
        with open(LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("ts") in excluded:
                    n_quarantined += 1
                    continue
                # pool ONLY when hypothesis AND measurement semantics
                # both match — code_hash never gates (audit trail only)
                if r.get("frozen_hash") != FROZEN_HASH:
                    continue
                if r.get("measurement_hash") != MEAS_HASH:
                    n_meas_mismatch += 1
                    continue
                recs.append(r)
    # FIRST eligible record per ET session date = the day's independent
    # observation (fixed ex-ante in MEASUREMENT["daily_aggregation"])
    by_date = {}
    for r in recs:
        d = (r.get("batch_generated") or r.get("ts") or "")[:10]
        if d and d not in by_date:
            by_date[d] = r
    dates = sorted(by_date)
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hm_ver": FROZEN["hm_ver"],
        "frozen_hash": FROZEN_HASH,
        "meas_ver": MEASUREMENT["meas_ver"],
        "measurement_hash": MEAS_HASH,
        "frozen_spec": FROZEN,
        "measurement_semantics": MEASUREMENT,
        "records_pooled": len(recs),
        "records_quarantined": n_quarantined,
        "records_measurement_mismatch": n_meas_mismatch,
        "proxy_only_caveat": ("recorded index-put quotes are PROXY_ONLY "
                              "(short-dated) — no duration-matched "
                              "comparative-efficiency promotion claim "
                              "can rest on them"),
        "scan_dates": len(dates),
        "conclusions": {
            "alpha_eligibility": ("SHADOW — governed by uoa_research "
                                  "avoidance gates (separate conclusion)"),
            "hedge_efficiency": "ACCRUING",
        },
    }
    if len(dates) < 5:
        payload["status"] = "accruing"
        payload["note"] = ("Prospective window is %d scan dates old; "
                          "predictive tests, episode detection, and "
                          "hedge-implementation comparison publish as "
                          "they gate in. Nothing promotes before >=2 "
                          "independent adverse episodes AND "
                          "payoff/carry >= 1.0 executable." % len(dates))
        _write(payload)
        return
    # SPY daily closes over the window (for episodes + forward returns)
    try:
        import yfinance as yf
        h = yf.download(["SPY", "QQQ"], period="6mo", interval="1d",
                        auto_adjust=True, group_by="ticker",
                        progress=False, threads=True)
        spy_c = {d.strftime("%Y-%m-%d"): float(v)
                 for d, v in h["SPY"]["Close"].dropna().items()}
        qqq_c = {d.strftime("%Y-%m-%d"): float(v)
                 for d, v in h["QQQ"]["Close"].dropna().items()}
    except Exception as e:
        payload["status"] = "error_market_data"
        payload["note"] = "SPY/QQQ history unavailable: %s" % type(e).__name__
        _write(payload)
        return
    tdays = sorted(spy_c)
    ret1 = {tdays[i]: 100 * (spy_c[tdays[i + 1]] / spy_c[tdays[i]] - 1)
            for i in range(len(tdays) - 1)}
    # 1. adverse episodes (frozen definition), grouped
    adv = [d for d in tdays[:-1] if ret1[d] <= -1.0]
    episodes = []
    for d in adv:
        if episodes and tdays.index(d) - tdays.index(episodes[-1][-1]) <= 2:
            episodes[-1].append(d)
        else:
            episodes.append([d])
    in_window = [ep for ep in episodes if any(x >= dates[0] for x in ep)]
    payload["adverse_episodes"] = {
        "definition": FROZEN["adverse_episode"],
        "observed_in_window": len(in_window),
        "episodes": [{"dates": ep,
                      "spy_move": round(sum(ret1[x] for x in ep), 2)}
                     for ep in in_window],
    }
    # 2. does strength predict subsequent SPY weakness? (date-level,
    #    non-overlapping for each horizon via stride sampling)
    def fwd(d, h, closes):
        if d not in closes:
            return None
        i = tdays.index(d)
        if i + h >= len(tdays):
            return None
        return 100 * (closes[tdays[i + h]] / closes[d] - 1)
    pred = {}
    for h in FROZEN["horizons_d"]:
        pairs = []
        last_i = -99
        for d in dates:
            if d not in spy_c:
                continue
            i = tdays.index(d)
            if i - last_i < h:      # enforce NON-OVERLAPPING windows
                continue
            f = fwd(d, h, spy_c)
            if f is None:
                continue
            pairs.append((by_date[d]["strength"], f))
            last_i = i
        if len(pairs) < 6:
            pred["h%d" % h] = {"status": "accruing", "n": len(pairs)}
            continue
        pairs.sort(key=lambda x: x[0])
        lo = [f for _, f in pairs[:len(pairs) // 3]]
        hi = [f for _, f in pairs[-(len(pairs) // 3):]]
        n = len(pairs)
        pred["h%d" % h] = {
            "n_nonoverlapping": n,
            "spy_fwd_low_strength": round(_mean(lo), 3),
            "spy_fwd_high_strength": round(_mean(hi), 3),
            "p_adverse_high_strength": round(
                sum(1 for f in hi if f <= -1) / len(hi), 2),
            "small_sample_tcrit": _tcrit(n - 2),
        }
    payload["predictive"] = pred
    payload["incremental_controls"] = {
        "status": "accruing",
        "plan": ("strength-vs-outcome within strata of {breadth regime, "
                 "VIX tercile, SPY trend sign, batch put/call} once each "
                 "stratum holds >=10 non-overlapping dates — controls "
                 "recorded point-in-time in every record"),
    }
    # 4. hedge implementations on executable $ P&L — needs contracts'
    #    subsequent marks; v1 marks index puts via underlying moves at
    #    +5d with the recorded quote (assumption-labeled), cohort via
    #    underlying-equivalent move minus liquidity haircut.
    impl = {"status": "accruing" if len(in_window) < 2 else "computable",
            "note": ("$%d premium budget per scan date; cohort fills at "
                     "recorded delayed last_price minus liq haircut; "
                     "index puts marked via recorded ATM quote + "
                     "underlying move (approximation, labeled). Full "
                     "comparison publishes at >=2 episodes."
                     % FROZEN["hedge_budget_usd"])}
    payload["implementations"] = impl
    ge = len(in_window) >= 2
    payload["promotion"] = {
        "episodes_gate": "%d/2" % len(in_window),
        "payoff_carry_gate": "accruing",
        "promoted": False,
        "rule": FROZEN["promotion_gates"]["hedge_sleeve"],
    }
    payload["status"] = "active"
    _write(payload)


def _write(payload):
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    print("  hedge monitor: wrote hedge_monitor.json (%s · %s scan dates)"
          % (payload.get("status"), payload.get("scan_dates")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--evaluate", action="store_true")
    a = ap.parse_args()
    if a.record:
        record()
    elif a.evaluate:
        evaluate()
    else:
        print("usage: hedge_monitor.py --record | --evaluate")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
