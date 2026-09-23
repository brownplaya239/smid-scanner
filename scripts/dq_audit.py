#!/usr/bin/env python3
"""Live data-quality audit for tickerdesk.io.

A green pipeline says nothing about the data (this month: rejected
pushes with green jobs, NaN that blanked a page, a lost ledger window).
This audits the OUTCOME — what the site actually serves — on the axes
that failed before:

  FRESH   every artifact's timestamp vs its own cadence, time-aware
          (intraday feeds only need to be fresh relative to the last
          RTH batch; dailies within ~30h; weeklies within 8d)
  PARSE   browser-strict JSON (NaN/Infinity rejected — python's json
          tolerates them and lied to us on 2026-09-21)
  SHAPE   expected keys / non-empty collections / row counts
  XREF    cross-artifact consistency (same-batch stamps, count fields
          equal to array lengths, matched n == pocket n, ledger has
          rows for the last sessions, regime label current)
  WORKER  live quote endpoint math (change% == price/prevClose-1,
          market_state matches the clock, no stale flags at RTH),
          candles prevClose sanity, econ calendar populated

Prints PASS/WARN/FAIL per check; exit 1 if any FAIL. No secrets.

    python scripts/dq_audit.py            # audit tickerdesk.io
    python scripts/dq_audit.py --base URL # audit another origin
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://tickerdesk.io"
API = "https://api.tickerdesk.io"
ET = timezone(timedelta(hours=-4))       # EDT (all current dates)
NOW = datetime.now(timezone.utc)

RESULTS = []


def rec(level, area, name, msg):
    RESULTS.append((level, area, name, msg))


def fetch(url, timeout=25):
    req = urllib.request.Request(
        url + ("&" if "?" in url else "?") + "cb=" + str(int(NOW.timestamp())),
        headers={"User-Agent": "tickerdesk-dq-audit"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def strict_json(raw):
    def boom(c):
        raise ValueError("non-finite constant " + c)
    return json.loads(raw.decode("utf-8"), parse_constant=boom)


def ts_of(d):
    """Best-effort artifact timestamp -> aware datetime."""
    if not isinstance(d, dict):
        return None
    for k in ("generated", "updated", "generated_at", "as_of",
              "data_cutoff", "version", "date"):
        v = d.get(k)
        if isinstance(v, str) and len(v) >= 10:
            try:
                s = v.replace("Z", "+00:00")
                dt = datetime.fromisoformat(s) if len(s) > 10 \
                    else datetime.fromisoformat(s + "T20:00:00+00:00")
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue
    days = d.get("days")
    if isinstance(days, list) and days and isinstance(days[-1], dict):
        v = days[-1].get("date")
        if v:
            return datetime.fromisoformat(v + "T21:00:00+00:00")
    runs = d.get("runs")
    if isinstance(runs, list) and runs and isinstance(runs[0], dict):
        return ts_of(runs[0])
    return None


def last_rth_batch():
    """Most recent expected intraday batch time (~3:55 PM ET of the
    last weekday, or now-ish if inside RTH)."""
    n = NOW.astimezone(ET)
    d = n
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    open_ = d.replace(hour=9, minute=35, second=0, microsecond=0)
    close_ = d.replace(hour=15, minute=55, second=0, microsecond=0)
    if d.date() == n.date() and open_ <= n <= close_:
        return n - timedelta(minutes=75)      # within cadence + cron lag
    if d.date() == n.date() and n < open_:
        d -= timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d.replace(hour=15, minute=55, second=0, microsecond=0)
    return close_


# artifact -> (cadence, required top-level keys, non-empty collection)
ARTIFACTS = {
    "uoa_latest":            ("intraday", ["generated"], "rows"),
    "uoa_desk":              ("intraday", ["generated"], "rows"),
    "uoa_edge":              ("intraday", ["generated", "overall"], None),
    "uoa_signals_scored":    ("intraday", ["generated", "signals"], "signals"),
    "uoa_top25_daily":       ("intraday", [], None),
    "trade_desk":            ("intraday", ["generated", "desk_counts",
                                           "gates"], None),
    "earnings_vol":          ("intraday", ["generated", "types"], None),
    "fair_move_lab":         ("intraday", ["generated"], None),
    "hedge_monitor":         ("daily", ["generated"], None),   # report JSON is nightly; batches only append the data/ record
    "edge_weights":          ("intraday", ["features"], "features"),
    "earnings_anticipated":  ("daily", [], None),
    "earnings_edge":         ("daily", ["generated", "names"], "names"),
    "earnings_ideas":        ("daily", ["generated", "ideas"], None),
    "economic_calendar":     ("daily", [], None),
    "regime_history":        ("daily", ["days"], "days"),
    "evening_review":        ("daily", [], None),
    "carryover_flow":        ("daily", [], None),
    "missed_opportunities":  ("daily", [], None),
    "technical_facts":       ("daily", ["facts"], "facts"),
    "grade_engine":          ("daily", ["generated"], None),
    "swing_report":          ("daily", ["runs"], "runs"),
    "swing_latest_summary":  ("daily", ["runs"], "runs"),
    "momentum_qm":           ("daily", [], None),
    "momentum_stockbee":     ("daily", [], None),
    "scan_outcomes":         ("daily", ["generated"], None),
    "level_stats":           ("daily", [], None),
    "manifest":              ("daily", [], None),
    "uoa_scorecard":         ("nightly", ["generated", "pockets"], None),
    "earnings_vol_backtest": ("nightly", ["generated"], None),
    "earnings_vol_exec":     ("nightly", ["generated"], None),
    "trade_desk_validation": ("intraday", ["generated"], None),
    "trade_desk_research":   ("intraday", ["generated"], None),
    "hypothesis_lab":        ("weekly", ["generated"], None),
    "uoa_meta_cache":        ("weekly", [], None),
    "country_etfs":          ("daily", [], None),
    "dealer_positioning":    ("daily", [], None),
    "iv_em_context":         ("daily", [], None),
    "whisper_tweet":         ("weekly", ["updated"], None),    # 2026-09-23: found 39d stale — source 429s
}


def freshness_limit(cadence):
    if cadence == "intraday":
        return last_rth_batch() - timedelta(minutes=30)
    if cadence == "daily":
        return NOW - timedelta(hours=30 if NOW.weekday() < 6 else 54)
    if cadence == "nightly":
        return NOW - timedelta(hours=36 if NOW.weekday() not in (0, 6)
                               else 84)
    return NOW - timedelta(days=8)


def audit_artifacts():
    docs = {}
    for name, (cad, keys, coll) in ARTIFACTS.items():
        url = f"{BASE}/reports/{name}.json"
        try:
            status, raw = fetch(url)
        except Exception as e:
            rec("FAIL", "FETCH", name, f"unreachable: {str(e)[:60]}")
            continue
        if status != 200:
            rec("FAIL", "FETCH", name, f"HTTP {status}")
            continue
        try:
            d = strict_json(raw)
        except Exception as e:
            rec("FAIL", "PARSE", name,
                f"browser-strict parse fails: {str(e)[:70]}")
            try:
                d = json.loads(raw)
            except Exception:
                continue
        docs[name] = d
        if b"&#x" in raw or "Ã¢".encode("utf-8") in raw:
            rec("FAIL", "PARSE", name, "entity/mojibake artifacts")
        missing = [k for k in keys if k not in d]
        if missing:
            rec("FAIL", "SHAPE", name, f"missing keys {missing}")
        if coll:
            c = d.get(coll)
            n = len(c) if hasattr(c, "__len__") else 0
            if not n:
                rec("FAIL", "SHAPE", name, f"'{coll}' is empty")
        ts = ts_of(d)
        lim = freshness_limit(cad)
        if ts is None:
            rec("WARN", "FRESH", name, "no timestamp field found")
        elif ts < lim:
            age_h = (NOW - ts).total_seconds() / 3600
            rec("FAIL" if cad in ("intraday", "daily") else "WARN",
                "FRESH", name,
                f"{age_h:.1f}h old (cadence {cad}; limit "
                f"{(NOW - lim).total_seconds()/3600:.1f}h)")
        else:
            rec("PASS", "FRESH", name,
                f"{(NOW - ts).total_seconds()/3600:.1f}h old")
    return docs


def audit_xref(docs):
    ul, ue = docs.get("uoa_latest"), docs.get("uoa_edge")
    if ul and ue:
        a, b = ts_of(ul), ts_of(ue)
        if a and b:
            gap = abs((a - b).total_seconds()) / 60
            rec("PASS" if gap <= 10 else "WARN", "XREF",
                "uoa_latest~uoa_edge",
                f"batch stamps {gap:.0f} min apart")
    td = docs.get("trade_desk")
    if td:
        c = td.get("desk_counts") or {}
        top = td.get("top_ideas") or []
        n_sig = sum(1 for i in top if i.get("status") == "SIGNAL_QUALIFIED")
        n_tr = sum(1 for i in top
                   if i.get("status") in ("TRADE_QUALIFIED", "QUALIFIED"))
        ok = (c.get("signal_qualified") == n_sig
              and c.get("trade_qualified") == n_tr
              and c.get("research_watch") == len(td.get("watch") or [])
              and c.get("experimental") == len(td.get("experimental")
                                               or []))
        rec("PASS" if ok else "FAIL", "XREF", "trade_desk counts",
            f"{c} vs arrays sig={n_sig} tr={n_tr} "
            f"watch={len(td.get('watch') or [])} "
            f"exp={len(td.get('experimental') or [])}")
        bad_status = [i.get("status") for i in
                      top + (td.get("watch") or []) +
                      (td.get("experimental") or [])
                      if i.get("status") not in
                      ("TRADE_QUALIFIED", "QUALIFIED", "SIGNAL_QUALIFIED",
                       "WATCH", "EXPERIMENTAL")]
        if bad_status:
            rec("FAIL", "XREF", "trade_desk statuses",
                f"unknown statuses {bad_status}")
        reg = td.get("market_regime")
        rec("PASS" if reg in ("risk_on", "risk_off", "mixed") else "WARN",
            "XREF", "trade_desk regime", f"regime={reg}")
    sc = docs.get("uoa_scorecard")
    if sc:
        g = (sc.get("matched") or {}).get("graded_5d")
        n = ((sc.get("pockets") or {}).get("overall") or {}).get("n")
        rec("PASS" if g == n else "FAIL", "XREF", "scorecard n",
            f"matched.graded_5d={g} pockets.overall.n={n}")
    ei = docs.get("earnings_ideas")
    if ei:
        bt = ei.get("by_type") or {}
        tot = sum((v or {}).get("n", 0) for v in bt.values())
        tg = ei.get("total_graded")
        rec("PASS" if tg is None or tot <= tg else "FAIL", "XREF",
            "earnings_ideas graded", f"by_type sum={tot} total={tg}")
        for t, v in bt.items():
            ev = (v or {}).get("ev")
            if isinstance(ev, float) and ev != ev:
                rec("FAIL", "XREF", "earnings_ideas ev", f"{t} ev is NaN")
    ss = docs.get("uoa_signals_scored")
    if ss:
        from collections import Counter
        days = Counter((s.get("flagged_at") or "")[:10]
                       for s in ss.get("signals") or [])
        last = sorted(days)[-3:]
        rec("PASS", "XREF", "signals/day (recent)",
            ", ".join(f"{d}:{days[d]}" for d in last))
        # Under-sampled session: a day with < 40% of the trailing-10
        # session median means batches were missed (scheduler outage,
        # rejected publishes) — the record is thinner than it looks.
        hist = [days[d] for d in sorted(days)[-11:-1] if days[d] > 0]
        if len(hist) >= 5:
            med = sorted(hist)[len(hist) // 2]
            for d in sorted(days)[-3:]:
                if 0 < days[d] < 0.4 * med:
                    rec("WARN", "XREF", f"under-sampled {d}",
                        f"{days[d]} signals vs trailing median {med}")
        # last trading day must have rows
        n_et = NOW.astimezone(ET)
        d = n_et
        if n_et.hour < 10:
            d -= timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        key = d.date().isoformat()
        rec("PASS" if days.get(key, 0) > 0 else "FAIL", "XREF",
            "signals last session", f"{key}: {days.get(key, 0)} rows")
    rh = docs.get("regime_history")
    if rh:
        last = ((rh.get("days") or [{}])[-1]).get("date")
        n_et = NOW.astimezone(ET)
        d = n_et - timedelta(days=1) if n_et.hour < 19 else n_et
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        exp = d.date().isoformat()
        rec("PASS" if last and last >= exp else "WARN", "XREF",
            "regime last day", f"{last} (expected >= {exp})")
    ea = docs.get("earnings_anticipated")
    if ea:
        raw = json.dumps(ea)
        today = NOW.astimezone(ET).date().isoformat()
        rec("PASS" if today[:7] in raw else "WARN", "XREF",
            "earnings_anticipated month", f"mentions {today[:7]}")


def audit_worker():
    # quotes
    try:
        st, raw = fetch(f"{API}/?quotes=SPY,QQQ,AAPL,NVDA")
        d = strict_json(raw)
        n_et = NOW.astimezone(ET)
        rows = d if isinstance(d, list) else \
            (d.get("quotes") or d.get("data") or d)
        items = rows.items() if isinstance(rows, dict) else \
            [(r.get("symbol"), r) for r in rows]
        seen = 0
        for sym, q in items:
            if not isinstance(q, dict):
                continue
            seen += 1
            px, pc = q.get("price"), q.get("prevClose")
            chg = q.get("change_pct") if "change_pct" in q else q.get("change")
            if not px or not pc:
                rec("FAIL", "WORKER", f"quote {sym}",
                    f"price={px} prevClose={pc}")
                continue
            implied = (px / pc - 1) * 100
            if chg is not None and abs(implied - float(chg)) > 0.2 \
                    and abs(chg) < 50:
                rec("FAIL", "WORKER", f"quote {sym}",
                    f"change {chg} vs price/prevClose {implied:.2f}")
            else:
                rec("PASS", "WORKER", f"quote {sym}",
                    f"{px} ({implied:+.2f}%) state={q.get('market_state')}"
                    + (" STALE" if q.get("stale") else ""))
            ms = q.get("market_state")
            weekday = n_et.weekday() < 5
            h = n_et.hour + n_et.minute / 60
            expect = ("regular" if weekday and 9.5 <= h < 16 else
                      "pre" if weekday and 4 <= h < 9.5 else
                      "post" if weekday and 16 <= h < 20 else "closed")
            if ms and ms != expect:
                rec("WARN", "WORKER", f"quote {sym} state",
                    f"market_state={ms} clock says {expect}")
        if not seen:
            rec("FAIL", "WORKER", "quotes", "no quote rows")
    except Exception as e:
        rec("FAIL", "WORKER", "quotes", str(e)[:80])
    # candles prevClose sanity
    try:
        st, raw = fetch(f"{API}/?candles=SPY&range=5d&interval=1d")
        d = strict_json(raw)
        bars = d.get("bars") or []
        pc = d.get("prevClose")
        closes = [b.get("c") for b in bars if b.get("c")]
        ok = bool(closes) and pc and any(abs(pc - c) < 1e-6 for c in closes)
        rec("PASS" if ok else "WARN", "WORKER", "candles SPY",
            f"prevClose={pc} bars={len(bars)} "
            f"{'matches a bar close' if ok else 'not among bar closes'}")
    except Exception as e:
        rec("WARN", "WORKER", "candles", str(e)[:80])
    # econ calendar
    try:
        st, raw = fetch(f"{API}/?econ-calendar=1")
        d = strict_json(raw)
        ev = d.get("events") or d.get("rows") or d.get("items") or []
        rec("PASS" if len(ev) else "WARN", "WORKER", "econ-calendar",
            f"{len(ev)} events")
    except Exception as e:
        rec("WARN", "WORKER", "econ-calendar", str(e)[:80])


def main():
    global BASE, API
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--api", default=API)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    BASE, API = a.base, a.api
    docs = audit_artifacts()
    audit_xref(docs)
    audit_worker()
    order = {"FAIL": 0, "WARN": 1, "PASS": 2}
    RESULTS.sort(key=lambda r: (order[r[0]], r[1], r[2]))
    counts = {k: sum(1 for r in RESULTS if r[0] == k) for k in order}
    print(f"tickerdesk DQ audit @ {NOW.astimezone(ET):%Y-%m-%d %H:%M} ET"
          f" — {counts['FAIL']} FAIL · {counts['WARN']} WARN · "
          f"{counts['PASS']} PASS\n")
    for lvl, area, name, msg in RESULTS:
        if lvl == "PASS" and "--verbose" not in sys.argv:
            continue
        print(f"{lvl:4s} {area:6s} {name:28s} {msg}")
    if "--verbose" in sys.argv:
        pass
    sys.exit(1 if counts["FAIL"] else 0)


if __name__ == "__main__":
    main()
