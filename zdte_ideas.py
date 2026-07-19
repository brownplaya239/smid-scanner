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
WORKER = "https://api.tickerdesk.io"
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


def _vix_close():
    """Latest ^VIX daily close (best-effort; None on any failure)."""
    try:
        import yfinance as yf
        df = yf.Ticker("^VIX").history(period="5d", interval="1d")
        if len(df):
            return round(float(df["Close"].iloc[-1]), 2)
    except Exception:
        pass
    return None


def _event_day(date_iso):
    """True when the econ calendar lists a high-impact event today."""
    try:
        p = os.path.join(_BASE, "docs", "reports", "economic_calendar.json")
        with open(p, encoding="utf-8") as f:
            cal = json.load(f)
        ff = date_iso[5:7] + "-" + date_iso[8:10] + "-" + date_iso[0:4]
        return any(e.get("date") == ff
                   and "high" in (e.get("impact") or "").lower()
                   for e in cal.get("events") or [])
    except Exception:
        return None


def _session_observations(bars, L, decision_m, date):
    """Learning-loop raw material: which level was touched FIRST after
    the decision bar, and which levels broke cleanly (a close beyond by
    >0.1% with the session also CLOSING beyond on that side)."""
    seq = [b for b in bars if b["date"] == date
           and decision_m <= b["m"] < 960]
    if not seq or not L:
        return {}
    lv = {k: v for k, v in L.items() if isinstance(v, (int, float))}
    first = None
    for b in seq:
        hits = [k for k, v in lv.items() if b["l"] <= v <= b["h"]]
        if hits:
            first = min(hits, key=lambda k: abs(lv[k] - b["c"]))
            break
    close = seq[-1]["c"]
    breaks = []
    for k, v in lv.items():
        if close > v and any(b["c"] > v * 1.001 for b in seq):
            breaks.append(k + "+")
        elif close < v and any(b["c"] < v * 0.999 for b in seq):
            breaks.append(k + "-")
    return {"first_touch": first, "clean_breaks": sorted(breaks)}


def _ivem_snapshot(ch):
    """EOD IV/EM snapshot from the chain payload: expected move as % of
    spot + ATM IV (avg of the nearest-expiry call/put closest to spot)."""
    try:
        spot = ch.get("spot")
        em = (ch.get("expected_move") or {}).get("usd")
        rows = ch.get("rows") or []
        exps = ch.get("expiries") or []
        out = {}
        if spot and em:
            out["em_pct"] = round(em / spot * 100, 3)
        if spot and rows and exps:
            near = [r for r in rows if r.get("e") == exps[0]
                    and r.get("iv") is not None]
            if near:
                atm_k = min({r["k"] for r in near},
                            key=lambda k: abs(k - spot))
                ivs = [r["iv"] for r in near if r["k"] == atm_k]
                if ivs:
                    out["atm_iv"] = round(sum(ivs) / len(ivs), 2)
        return out or None
    except Exception:
        return None


IVEM_LOG = os.path.join(_BASE, "data", "iv_em_log.json")
IVEM_OUT = os.path.join(_BASE, "docs", "reports", "iv_em_context.json")
IVEM_CAP_D = 120
IVEM_MIN_N = 20           # sessions before the EM/IV context publishes


def _ivem_update(snaps, date):
    """Append today's IV/EM snapshots and build the gated context file
    the Index Levels NOW panel reads. Publishes real comparisons only
    once IVEM_MIN_N sessions are logged — 'accruing' until then."""
    try:
        with open(IVEM_LOG, encoding="utf-8") as f:
            ivlog = json.load(f)
    except Exception:
        ivlog = {}
    for sym, snap in (snaps or {}).items():
        if not snap:
            continue
        days = [d for d in ivlog.get(sym) or [] if d.get("date") != date]
        days.append({"date": date, **snap})
        days.sort(key=lambda d: d["date"])
        ivlog[sym] = days[-IVEM_CAP_D:]
    ctx = {"generated": datetime.now(timezone.utc)
           .isoformat(timespec="seconds"), "min_n": IVEM_MIN_N,
           "by_sym": {}}
    for sym, days in ivlog.items():
        ems = [d["em_pct"] for d in days if d.get("em_pct") is not None]
        ivs = [d["atm_iv"] for d in days if d.get("atm_iv") is not None]
        n = len(ems)
        if n < IVEM_MIN_N:
            ctx["by_sym"][sym] = {"status": "accruing", "n": n}
            continue
        today_em = ems[-1]
        avg20 = sum(ems[-20:]) / len(ems[-20:])
        entry = {"status": "active", "n": n,
                 "em_pct": round(today_em, 2),
                 "em_avg20": round(avg20, 2)}
        if len(ivs) >= IVEM_MIN_N:
            last_iv = ivs[-1]
            win = ivs[-60:]
            entry["iv_pctile"] = round(
                100 * sum(1 for v in win if v <= last_iv) / len(win))
        ctx["by_sym"][sym] = entry
    return ivlog, ctx


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
    B = 0.0018   # invalidation buffer (~0.18%)

    def up_of(x, *xs):   # nearest candidate strictly above reference x
        c = [v for v in xs if v is not None and v > x]
        return min(c) if c else None

    def dn_of(x, *xs):
        c = [v for v in xs if v is not None and v < x]
        return max(c) if c else None

    def s_up(*xs): return up_of(spot, *xs)
    def s_dn(*xs): return dn_of(spot, *xs)

    ideas = []

    def mk(type_, direction, trigger, target):
        # enforce valid ordering — long: invalid<trigger<target,
        # short: target<trigger<invalid — else DROP (a short can never
        # target upward; caught the OR-break-short target bug).
        if trigger is None or target is None:
            return
        inv = trigger * (1 - B) if direction == "long" else trigger * (1 + B)
        if direction == "long" and not (inv < trigger < target):
            return
        if direction == "short" and not (target < trigger < inv):
            return
        ideas.append({"type": type_, "dir": direction, "trigger": trigger,
                      "target": target, "invalid": inv, "regime": regime})

    if regime == "positive":
        mk("posgamma_fade_short", "short",
           s_up(cw, emh, L.get("VAH"), L.get("ORH")), vwap)
        mk("posgamma_fade_long", "long",
           s_dn(pw, eml, L.get("VAL"), L.get("ORL")), vwap)
    else:
        sup = s_dn(L.get("VAL"), L.get("ORL"), pw)
        if sup:
            mk("neggamma_break_short", "short", sup,
               dn_of(sup, pw, eml, L.get("PDL")))
        res = s_up(L.get("VAH"), L.get("ORH"), cw)
        if res:
            mk("neggamma_break_long", "long", res,
               up_of(res, cw, emh, L.get("PDH")))

    orh, orl = L.get("ORH"), L.get("ORL")
    if orh:
        mk("or_break_long", "long", orh,
           up_of(orh, L.get("VAH"), cw, emh, L.get("PDH")))
    if orl:
        mk("or_break_short", "short", orl,
           dn_of(orl, L.get("VAL"), pw, eml, L.get("PDL")))

    if vwap and spot < vwap:
        mk("vwap_reclaim_long", "long", vwap,
           up_of(vwap, L.get("VAH"), L.get("ORH"), cw))
    if vwap and spot > vwap:
        mk("vwap_loss_short", "short", vwap,
           dn_of(vwap, L.get("VAL"), L.get("ORL"), pw))
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
    obs_by_sym, ivem_snaps = {}, {}
    for sym in SYMS:
        ch = _chain(sym)
        bars = _bars(sym)
        if not ch or len(bars) < 5:
            print(f"  {sym}: insufficient data — skip")
            continue
        date = bars[-1]["date"]
        ivem_snaps[sym] = _ivem_snapshot(ch)
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
        obs = _session_observations(bars, L, dec["m"], date)
        if obs:
            obs["regime"] = regime
            obs_by_sym[sym] = obs
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
        # session context for the learning loop: VIX + event-day tags
        # condition the hit rates; first-touch / clean-break observations
        # accrue the level-behavior dataset the redesign review asked for
        day_ctx = {"vix": _vix_close(), "event_day": _event_day(date),
                   "by_sym": obs_by_sym}
        log["days"] = [d for d in log.get("days") or [] if d.get("date") != date]
        log["days"].append({"date": date, "ideas": today_out,
                            "ctx": day_ctx})
        log["days"].sort(key=lambda d: d["date"])
        log["days"] = log["days"][-LOG_CAP_D:]

    # accrue gated stats over all graded (triggered) ideas
    by_type, by_tr, by_cond, by_ev = {}, {}, {}, {}
    for d in log.get("days") or []:
        ctx = d.get("ctx") or {}
        vix = ctx.get("vix")
        vb = None if vix is None else (
            "low" if vix < 15 else "mid" if vix <= 20 else "high")
        ev = ctx.get("event_day")
        for r in d.get("ideas") or []:
            if r["result"] not in ("win", "loss"):
                continue
            w = r["result"] == "win"
            by_type.setdefault(r["type"], []).append(w)
            key = r["type"] + "|" + r["regime"]
            by_tr.setdefault(key, []).append(w)
            if vb:
                by_cond.setdefault("vix_" + vb + "|" + r["regime"] +
                                   "_gamma", []).append(w)
            if ev is not None:
                by_ev.setdefault("event_day" if ev else "no_event",
                                 []).append(w)

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
        "by_vix_regime": summarise(by_cond),
        "by_event_day": summarise(by_ev),
        "today": today_out,
        "note": ("Session-frame directional hit rate (trigger fired, then "
                 "target before invalidation) on 15-min-delayed data. Gated "
                 "at n>=30 triggered ideas per type. 0DTE resolves same-day, "
                 "so cohorts mature fast. VIX-bucket, gamma-regime and "
                 "event-day conditioning accrue from the per-day ctx tags "
                 "and publish only when their own n>=30. Educational, "
                 "not advice."),
    }
    print(f"  zdte ideas: {len(today_out)} today · {total_graded} graded "
          f"total · {len([1 for v in payload['by_type'].values() if v.get('status')=='active'])} "
          f"types active")
    ivem_log, ivem_ctx = _ivem_update(ivem_snaps, date) if date \
        else (None, None)
    return log, payload, ivem_log, ivem_ctx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    log, payload, ivem_log, ivem_ctx = build()
    if args.dry_run:
        print(json.dumps(payload, indent=1)[:3000])
        if ivem_ctx:
            print(json.dumps(ivem_ctx, indent=1)[:800])
        return
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, separators=(",", ":"))
    os.makedirs(REPORTS, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    if ivem_log is not None:
        with open(IVEM_LOG, "w", encoding="utf-8") as f:
            json.dump(ivem_log, f, separators=(",", ":"))
        with open(IVEM_OUT, "w", encoding="utf-8") as f:
            json.dump(ivem_ctx, f, separators=(",", ":"))
    print(f"  Wrote {os.path.relpath(OUT_PATH, _BASE)} + log + IV/EM context")


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
