"""Earnings Trade Ideas — deterministic, evidence-cited, self-grading.

Joins what the site already knows about each name reporting in the next
~7 days and emits idea cards for the Earnings tab:

  inputs (all local JSONs published by earlier pipeline steps):
    earnings_edge.json     implied vs realized history, drift, flow-into
    swing_latest_summary   swing grade, day chg, rvol, extension, themes
    technical_facts.json   RS ranks + trend (surfaced names only)
    uoa_meta_cache.json    market cap backup

  idea types (rule-based, every claim cites its number):
    momentum_into_print    A-tier / high-RS name with bullish flow into
                           the report and positive historical post-drift
    post_report_drift      the name historically drifts hard in the 5
                           sessions AFTER reporting (|drift| >= 1.5%,
                           n >= 6) — the after-print trade, no binary risk
    vol_rich / vol_cheap   options implied move vs the name's realized
                           history (needs a liquid implied read; emitted
                           only when earnings_edge captured one)
    binary_caution         big typical mover + big pre-print run-up —
                           the "size down or sit out" flag

LEARNED LAYER (house pattern — log → grade vs real closes → gate n>=30):
every published idea is logged; once the report is >=3 sessions old the
idea is graded against actual price action (yfinance closes):
    momentum_into_print    win = 3-session post-report move in bias dir
    post_report_drift      win = 5-session move matches drift sign
    vol_rich               win = |report-day move| < implied
    vol_cheap              win = |report-day move| > implied
Per-type hit rates publish at n>=30 ("accruing" until then) and render
as track-record chips on the cards — identical contract to the 0DTE
idea loop. Nothing is estimated; nothing publishes before it's real.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

_BASE = os.path.dirname(os.path.abspath(__file__))
R = lambda *p: os.path.join(_BASE, *p)
LOG_PATH = R("data", "earnings_ideas_log.json")
OUT_PATH = R("docs", "reports", "earnings_ideas.json")
MIN_N = 30
LOG_CAP = 1200
GRADE_AFTER_SESSIONS = 3


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _swing_map():
    s = _load(R("docs", "reports", "swing_latest_summary.json"), None) or \
        _load(R("docs", "reports", "swing_report.json"), None)
    out = {}
    if not s or not s.get("runs"):
        return out
    for g, arr in (s["runs"][-1].get("grades") or {}).items():
        for c in arr:
            out[c.get("t")] = {"grade": g, "chg": c.get("chg"),
                               "rvol": c.get("rvol"), "ext": c.get("ext"),
                               "th": c.get("th") or []}
    return out


def generate(names, swing, facts):
    """Idea cards for names reporting within the horizon."""
    ideas = []
    for n in names:
        t = n.get("t")
        if not t or n.get("days") is None or n["days"] > 7:
            continue
        sw = swing.get(t) or {}
        fa = (facts or {}).get(t) or {}
        ev = []                             # evidence strings, all sourced
        implied = n.get("implied")
        rmed = n.get("realized_med")
        nrep = n.get("n_reports") or 0
        drift = n.get("drift_5d")
        fl = n.get("flow_into") or {}
        rs = (fa.get("rs") or {})
        rs_rank = fa.get("rs_rank")
        big = (n.get("mcap") or 0) >= 10e9
        grade = sw.get("grade") or ""
        a_tier = grade.startswith("A")

        if grade:
            ev.append(grade + " swing grade")
        if rs_rank is not None:
            ev.append("RS rank " + str(rs_rank))
        if big:
            ev.append("$" + str(round(n["mcap"] / 1e9)) + "B cap")
        if fl.get("n"):
            ev.append(str(fl.get("bull", 0)) + " bull / " +
                      str(fl.get("bear", 0)) + " bear flow into print")
        if rmed is not None and nrep >= 4:
            ev.append("typically moves ±" + str(rmed) + "% (" +
                      str(nrep) + " reports)")

        typ = bias = thesis = None
        bull_flow = fl.get("n") and (fl.get("bull", 0) > fl.get("bear", 0))

        if implied is not None and rmed and nrep >= 4:
            ratio = implied / rmed
            if ratio >= 1.35:
                typ, bias = "vol_rich", "neutral"
                thesis = ("Options price a ±" + str(implied) + "% move vs a ±" +
                          str(rmed) + "% median across " + str(nrep) +
                          " past reports (" + str(round(ratio, 1)) +
                          "× rich) — historically favors defined-risk "
                          "premium sellers over buyers.")
            elif ratio <= 0.75:
                typ, bias = "vol_cheap", "neutral"
                thesis = ("Options price only ±" + str(implied) +
                          "% vs a ±" + str(rmed) + "% median move (" +
                          str(nrep) + " reports) — the move is cheap; "
                          "long-vol structures historically favored.")
        if typ is None and a_tier and bull_flow and (drift or 0) > 0:
            typ, bias = "momentum_into_print", "bull"
            thesis = ("Leader into the print: " + grade + " grade" +
                      (", RS " + str(rs_rank) if rs_rank is not None else "") +
                      ", bullish options flow, and this name has averaged +" +
                      str(drift) + "% in the 5 sessions after reporting — "
                      "strength into earnings has historically carried through.")
        if typ is None and drift is not None and abs(drift) >= 1.5 and nrep >= 6:
            typ = "post_report_drift"
            bias = "bull" if drift > 0 else "bear"
            thesis = ("The after-print trade: across " + str(nrep) +
                      " reports this name drifts " +
                      ("+" if drift > 0 else "") + str(drift) +
                      "% on average in the 5 sessions AFTER earnings — "
                      "reacting post-report sidesteps the binary night.")
        if typ is None and rmed is not None and rmed >= 6 and \
                (sw.get("chg") or 0) >= 4:
            typ, bias = "binary_caution", "neutral"
            thesis = ("Crowded binary: already ran +" + str(sw["chg"]) +
                      "% today and typically swings ±" + str(rmed) +
                      "% on results — size down or let the print happen "
                      "and trade the reaction.")
        if typ is None:
            continue
        ideas.append({
            "t": t, "date": n.get("date"), "days": n.get("days"),
            "session": n.get("session"), "type": typ, "bias": bias,
            "thesis": thesis, "evidence": ev[:5],
            "implied": implied, "realized_med": rmed,
            "mcap_b": round((n.get("mcap") or 0) / 1e9, 1) or None,
            "grade": grade or None, "rs_rank": rs_rank,
            "drift_5d": drift,
        })
    order = {"momentum_into_print": 0, "vol_rich": 1, "vol_cheap": 1,
             "post_report_drift": 2, "binary_caution": 3}
    ideas.sort(key=lambda i: (i["days"], order.get(i["type"], 9)))
    return ideas


# ── learned layer: log + grade + gate ────────────────────────────────────

def _closes_around(t, event_date):
    """Daily closes surrounding the event via yfinance; None on failure."""
    try:
        import yfinance as yf
        df = yf.Ticker(t).history(period="2mo", interval="1d",
                                  auto_adjust=False)
        if df is None or df.empty:
            return None
        rows = [(idx.date().isoformat(), float(r["Close"]))
                for idx, r in df.iterrows()]
        return rows
    except Exception:
        return None


def _grade(idea):
    """win/loss/None(not gradeable yet). Session frames per idea type."""
    ev = idea.get("date")
    if not ev:
        return None
    rows = _closes_around(idea["t"], ev)
    if not rows:
        return None
    # pre = last close strictly BEFORE the event date (AMC: the event-day
    # close is also pre-report, but the 1-day frame from the prior close
    # still brackets the reaction — coarse and stated, consistent for all)
    pre_i = None
    for i, (d, _) in enumerate(rows):
        if d < ev:
            pre_i = i
    if pre_i is None or pre_i + GRADE_AFTER_SESSIONS >= len(rows):
        return None                       # not enough post-event sessions
    pre = rows[pre_i][1]
    day1 = rows[pre_i + 1][1]
    d3 = rows[pre_i + 3][1] if pre_i + 3 < len(rows) else None
    d5 = rows[pre_i + 5][1] if pre_i + 5 < len(rows) else None
    move1 = (day1 / pre - 1) * 100
    typ = idea["type"]
    if typ == "vol_rich":
        return "win" if abs(move1) < (idea.get("implied") or 0) else "loss"
    if typ == "vol_cheap":
        return "win" if abs(move1) > (idea.get("implied") or 0) else "loss"
    if typ == "momentum_into_print" and d3 is not None:
        m = (d3 / pre - 1) * 100
        return "win" if (m > 0) == (idea["bias"] == "bull") else "loss"
    if typ == "post_report_drift" and d5 is not None:
        m = (d5 / day1 - 1) * 100         # drift measured AFTER the print
        return "win" if (m > 0) == (idea["bias"] == "bull") else "loss"
    return None                           # binary_caution isn't a trade


def main():
    edge = _load(R("docs", "reports", "earnings_edge.json"), {})
    facts = _load(R("docs", "reports", "technical_facts.json"), {})
    facts = facts.get("facts") or facts
    swing = _swing_map()
    ideas = generate(edge.get("names") or [], swing, facts)

    log = _load(LOG_PATH, {"ideas": []})
    today = datetime.now(timezone.utc).date().isoformat()
    seen = {(i.get("t"), i.get("date"), i.get("type"))
            for i in log["ideas"]}
    added = 0
    for i in ideas:
        key = (i["t"], i["date"], i["type"])
        if key in seen or i["type"] == "binary_caution":
            continue
        log["ideas"].append({"t": i["t"], "date": i["date"],
                             "type": i["type"], "bias": i["bias"],
                             "implied": i.get("implied"),
                             "published": today, "result": "pending"})
        added += 1

    # grade matured, ungraded ideas (event >= ~GRADE_AFTER_SESSIONS old)
    cutoff = (datetime.now(timezone.utc).date() -
              timedelta(days=GRADE_AFTER_SESSIONS + 4)).isoformat()
    graded = 0
    for i in log["ideas"]:
        if i.get("result") == "pending" and i.get("date") and \
                i["date"] <= cutoff:
            res = _grade(i)
            if res:
                i["result"] = res
                graded += 1
    log["ideas"] = log["ideas"][-LOG_CAP:]

    by_type = {}
    for i in log["ideas"]:
        if i.get("result") in ("win", "loss"):
            by_type.setdefault(i["type"], []).append(i["result"] == "win")
    stats = {}
    for k, wins in by_type.items():
        n = len(wins)
        stats[k] = ({"status": "active", "n": n,
                     "win_rate": round(100 * sum(wins) / n)}
                    if n >= MIN_N else
                    {"status": "accruing", "n": n, "activates_at": MIN_N})

    payload = {
        "generated": datetime.now(timezone.utc)
        .isoformat(timespec="seconds"),
        "count": len(ideas),
        "ideas": ideas,
        "by_type": stats,
        "total_graded": sum(len(v) for v in by_type.values()),
        "note": ("Deterministic idea cards from the site's own signals "
                 "(swing grade, RS, flow-into-print, implied vs realized "
                 "history, post-report drift). Self-graded against real "
                 "closes once each report is " +
                 str(GRADE_AFTER_SESSIONS) + "+ sessions old; per-type "
                 "hit rates publish at n>=" + str(MIN_N) +
                 ". Educational, not advice."),
    }
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, separators=(",", ":"))
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    print("  earnings ideas: %d published (%d new logged, %d graded this "
          "run, %d graded total)" % (len(ideas), added, graded,
                                     payload["total_graded"]))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
