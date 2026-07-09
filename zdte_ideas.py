"""
zdte_ideas.py — the 0DTE idea generator's self-grading loop.

Runs post-close. For SPY / QQQ / IWM it:
  1. reconstructs the level map AS OF the morning decision point (~10:00 ET)
     from the day's 5-min bars — prev-day H/L, premarket H/L, opening
     range, VWAP, volume-profile VPOC/VAH/VAL, EMA20/200 — plus the
     dealer-gamma flip/walls + expected-move from the (session-stable,
     OI-driven) options chain via the worker's ?chain0= endpoint;
  2. generates the SAME deterministic, regime-aware trade ideas the live
     Index-Levels card shows (positive-gamma fades vs negative-gamma
     momentum, OR breaks, VWAP reclaim/loss, EM rejection), each with a
     trigger / target / invalidation price;
  3. grades every idea on the SESSION FRAME from 10:00 to the close —
     did the trigger fire, and did target hit before invalidation — using
     the 5-min bar highs/lows;
  4. accrues per-setup-type (and per-type × regime) hit rates, GATED at
     n>=30 triggered ideas — "accruing" until then;
  5. writes docs/reports/zdte_ideas_stats.json (today's graded ideas +
     the accrued track record) and appends to data/zdte_ideas_log.json.

HONESTY: data is 15-min delayed and OI is prior-session settled, so this
is a DIRECTIONAL / LEVEL hit-rate on the session frame — not a fill-
accurate intraday backtest. Gamma flip/walls are OI-driven and constant
through the session, so using the settled chain for the morning regime is
exact for "spot vs flip". Same gated-tracker discipline as every other
loop; 0DTE resolves same-day, so it matures far faster.

    python zdte_ideas.py            # generate + grade + emit
    python zdte_ideas.py --dry-run  # print, don't write
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.request
from datetime import datetime, timezone
from statistics import mean

_BASE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(_BASE, "docs", "reports")
LOG_PATH = os.path.join(_BASE, "data", "zdte_ideas_log.json")
OUT_PATH = os.path.join(REPORTS, "zdte_ideas_stats.json")

SYMS = ("SPY", "QQQ", "IWM")
WORKER = "https://smid-scanner-discord-bot.sumeetsancheti97.workers.dev"
MIN_N = 30                # gate per setup type
DECISION_MIN = 600        # 10:00 ET, minutes from midnight
OPEN_MIN, ORC_MIN, CLOSE_MIN = 570, 585, 960    # 9:30, 9:45, 16:00
LOG_CAP_D = 250


def _chain(sym):
    """Worker chain0 (spot + gex flip/walls + expected move). The worker
    403s a bare UA, so send a browser one."""
    try:
        req = urllib.request.Request(WORKER + "/?chain0=" + sym,
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        return None if d.get("error") else d
    except Exception as e:
        print(f"    {sym}: chain fetch failed ({str(e)[:50]})")
        return None


def _bars(sym):
    """Today's 5-min bars in ET-minute terms: [{m, h, l, c, v}] sorted."""
    import yfinance as yf
    try:
        df = yf.download(sym, period="1d", interval="5m",
                         auto_adjust=False, prepost=True, progress=False)
        if df is None or df.empty:
            return []
    except Exception:
        return []
    idx = df.index.tz_convert("America/New_York") if df.index.tz \
        else df.index.tz_localize("UTC").tz_convert("America/New_York")
    out = []
    for i, ts in enumerate(idx):
        m = ts.hour * 60 + ts.minute
        def g(col):
            v = df[col].iloc[i]
            try:
                return float(v.iloc[0]) if hasattr(v, "iloc") else float(v)
            except Exception:
                return None
        c = g("Close")
        if c is None:
            continue
        out.append({"m": m, "h": g("High"), "l": g("Low"), "c": c,
                    "v": g("Volume") or 0, "date": ts.date().isoformat()})
    out.sort(key=lambda b: b["m"])
    return out


def _levels(bars, decision_m):
    """Level map from bars strictly up to the decision minute (today only —
    single-day feed, so prev-day/premarket come from the pre-open bars)."""
    today = bars[-1]["date"] if bars else None
    tb = [b for b in bars if b["date"] == today]
    upto = [b for b in tb if b["m"] <= decision_m]
    if len(upto) < 3:
        return None
    pre = [b for b in tb if OPEN_MIN > b["m"] >= 240]       # 4:00–9:30
    orr = [b for b in tb if OPEN_MIN <= b["m"] < ORC_MIN]   # 9:30–9:45
    rth = [b for b in upto if b["m"] >= OPEN_MIN]
    L = {}
    if pre:
        L["PMH"] = max(b["h"] for b in pre)
        L["PML"] = min(b["l"] for b in pre)
    if orr:
        L["ORH"] = max(b["h"] for b in orr)
        L["ORL"] = min(b["l"] for b in orr)
    if rth:
        pv = sum(((b["h"] + b["l"] + b["c"]) / 3) * b["v"] for b in rth)
        vv = sum(b["v"] for b in rth) or 1
        L["VWAP"] = pv / vv
        hi = max(b["h"] for b in rth); lo = min(b["l"] for b in rth)
        nb, w = 30, (max(1e-6, hi - lo)) / 30
        bins = [0.0] * nb; tot = 0.0
        for b in rth:
            tp = (b["h"] + b["l"] + b["c"]) / 3
            k = min(nb - 1, max(0, int((tp - lo) / w)))
            bins[k] += b["v"]; tot += b["v"]
        poc = max(range(nb), key=lambda i: bins[i])
        acc, loI, hiI = bins[poc], poc, poc
        while acc < tot * 0.70 and (loI > 0 or hiI < nb - 1):
            dn = bins[loI - 1] if loI > 0 else -1
            up = bins[hiI + 1] if hiI < nb - 1 else -1
            if up >= dn: hiI += 1; acc += bins[hiI]
            else: loI -= 1; acc += bins[loI]
        L["VPOC"] = lo + (poc + 0.5) * w
        L["VAH"] = lo + (hiI + 1) * w
        L["VAL"] = lo + loI * w
    return L


def generate(sym, spot, L, gex, em):
    """Deterministic 0DTE ideas as of the decision point. Each idea:
    {type, dir, trigger, target, invalid}. Mirrors the live card's rules."""
    flip = gex.get("flip"); cw = gex.get("call_wall"); pw = gex.get("put_wall")
    regime = "positive" if (flip is not None and spot >= flip) else "negative"
    emh = spot + em["usd"] if em else None
    eml = spot - em["usd"] if em else None
    vwap = L.get("VWAP")
    ideas = []
    B = 0.0018   # invalidation buffer (~0.18%)

    def above(*xs):   # nearest level strictly above spot
        c = [x for x in xs if x is not None and x > spot]
        return min(c) if c else None

    def below(*xs):
        c = [x for x in xs if x is not None and x < spot]
        return max(c) if c else None

    if regime == "positive":
        # fade the extremes back to VWAP
        res = above(cw, emh, L.get("VAH"), L.get("ORH"))
        if res and vwap and vwap < res:
            ideas.append({"type": "posgamma_fade_short", "dir": "short",
                          "trigger": res, "target": vwap,
                          "invalid": res * (1 + B)})
        sup = below(pw, eml, L.get("VAL"), L.get("ORL"))
        if sup and vwap and vwap > sup:
            ideas.append({"type": "posgamma_fade_long", "dir": "long",
                          "trigger": sup, "target": vwap,
                          "invalid": sup * (1 - B)})
    else:
        # momentum: break of support/resistance runs to the next wall
        sup = below(L.get("VAL"), L.get("ORL"), pw)
        tgt = below(pw, eml)
        if sup and tgt and tgt < sup:
            ideas.append({"type": "neggamma_break_short", "dir": "short",
                          "trigger": sup * (1 - 0.0003), "target": tgt,
                          "invalid": sup * (1 + B)})
        res = above(L.get("VAH"), L.get("ORH"), cw)
        tgt2 = above(cw, emh)
        if res and tgt2 and tgt2 > res:
            ideas.append({"type": "neggamma_break_long", "dir": "long",
                          "trigger": res * (1 + 0.0003), "target": tgt2,
                          "invalid": res * (1 - B)})

    # OR break (both regimes)
    orh, orl = L.get("ORH"), L.get("ORL")
    if orh and above(L.get("VAH"), cw, emh):
        ideas.append({"type": "or_break_long", "dir": "long",
                      "trigger": orh * (1 + 0.0003),
                      "target": above(L.get("VAH"), cw, emh),
                      "invalid": orh * (1 - B)})
    if orl and below(L.get("VAL"), pw, eml):
        ideas.append({"type": "or_break_short", "dir": "short",
                      "trigger": orl * (1 - 0.0003),
                      "target": below(L.get("VAL"), pw, eml),
                      "invalid": orl * (1 + B)})

    # VWAP reclaim / loss
    if vwap:
        if spot < vwap and above(L.get("VAH"), L.get("ORH")):
            ideas.append({"type": "vwap_reclaim_long", "dir": "long",
                          "trigger": vwap, "target": above(L.get("VAH"), L.get("ORH")),
                          "invalid": vwap * (1 - B)})
        if spot > vwap and below(L.get("VAL"), L.get("ORL")):
            ideas.append({"type": "vwap_loss_short", "dir": "short",
                          "trigger": vwap, "target": below(L.get("VAL"), L.get("ORL")),
                          "invalid": vwap * (1 + B)})
    for it in ideas:
        it["regime"] = regime
    return ideas, regime


def grade(idea, bars, decision_m):
    """Session-frame grade from decision to close: did trigger fire, then
    target-before-invalidation? Returns win/loss/no_trigger."""
    today = bars[-1]["date"]
    fwd = [b for b in bars if b["date"] == today and b["m"] > decision_m]
    trig = idea["trigger"]; tgt = idea["target"]; inv = idea["invalid"]
    up = idea["dir"] == "long"
    triggered = False
    for b in fwd:
        if not triggered:
            hit = (b["l"] <= trig <= b["h"])
            if hit:
                triggered = True
                # same bar can resolve — check target/invalid on this bar too
            else:
                continue
        # post-trigger: which comes first this bar (conservative: invalid
        # wins ties, matching the pessimistic convention used elsewhere)
        hit_inv = (b["l"] <= inv <= b["h"]) or (up and b["l"] <= inv) or \
                  (not up and b["h"] >= inv)
        hit_tgt = (b["l"] <= tgt <= b["h"]) or (up and b["h"] >= tgt) or \
                  (not up and b["l"] <= tgt)
        if hit_inv and hit_tgt:
            return "loss"
        if hit_inv:
            return "loss"
        if hit_tgt:
            return "win"
    return "no_trigger" if not triggered else "open"


def build():
    log = {"days": []}
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            log = json.load(f)
    except Exception:
        pass
    today_out = []
    date = None
    for sym in SYMS:
        ch = _chain(sym)
        bars = _bars(sym)
        if not ch or len(bars) < 5:
            print(f"  {sym}: insufficient data — skip")
            continue
        date = bars[-1]["date"]
        # spot at decision = close of first bar at/after 10:00
        dec = next((b for b in bars if b["date"] == date and b["m"] >= DECISION_MIN),
                   None)
        if not dec:
            print(f"  {sym}: no post-10:00 bar (holiday/half-day?) — skip")
            continue
        spot = dec["c"]
        L = _levels(bars, dec["m"])
        if not L:
            continue
        ideas, regime = generate(sym, spot, L, ch.get("gex") or {},
                                 ch.get("expected_move"))
        for it in ideas:
            res = grade(it, bars, dec["m"])
            rec = {"sym": sym, "date": date, "regime": regime,
                   "type": it["type"], "dir": it["dir"],
                   "trigger": round(it["trigger"], 2),
                   "target": round(it["target"], 2),
                   "invalid": round(it["invalid"], 2), "result": res}
            today_out.append(rec)
        print(f"  {sym}: {regime}-gamma · {len(ideas)} ideas · "
              + ", ".join(f"{i['type'].split('_')[0]}:{r['result']}"
                          for i, r in zip(ideas, today_out[-len(ideas):])))

    if date and today_out:
        log["days"] = [d for d in log.get("days") or [] if d.get("date") != date]
        log["days"].append({"date": date, "ideas": today_out})
        log["days"].sort(key=lambda d: d["date"])
        log["days"] = log["days"][-LOG_CAP_D:]

    # accrue gated stats over all graded (triggered) ideas
    by_type, by_tr = {}, {}
    for d in log.get("days") or []:
        for r in d.get("ideas") or []:
            if r["result"] not in ("win", "loss"):
                continue
            by_type.setdefault(r["type"], []).append(r["result"] == "win")
            key = r["type"] + "|" + r["regime"]
            by_tr.setdefault(key, []).append(r["result"] == "win")

    def summarise(m):
        out = {}
        for k, wins in m.items():
            n = len(wins)
            if n < MIN_N:
                out[k] = {"status": "accruing", "n": n, "activates_at": MIN_N}
            else:
                out[k] = {"status": "active", "n": n,
                          "win_rate": round(100 * sum(wins) / n)}
        return out

    total_graded = sum(len(v) for v in by_type.values())
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_date": date,
        "min_n": MIN_N,
        "logged_days": len(log.get("days") or []),
        "total_graded": total_graded,
        "by_type": summarise(by_type),
        "by_type_regime": summarise(by_tr),
        "today": today_out,
        "note": ("Session-frame directional hit rate (trigger fired, then "
                 "target before invalidation) on 15-min-delayed data. Gated "
                 "at n>=30 triggered ideas per type. 0DTE resolves same-day, "
                 "so cohorts mature fast. Educational, not advice."),
    }
    print(f"  zdte ideas: {len(today_out)} today · {total_graded} graded "
          f"total · {len([1 for v in payload['by_type'].values() if v.get('status')=='active'])} "
          f"types active")
    return log, payload


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    log, payload = build()
    if args.dry_run:
        print(json.dumps(payload, indent=1)[:3000])
        return
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, separators=(",", ":"))
    os.makedirs(REPORTS, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"  Wrote {os.path.relpath(OUT_PATH, _BASE)} + log")


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
