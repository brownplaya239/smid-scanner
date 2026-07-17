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

Universe: hard floor at $1B market cap — names below it (or with no cap
on file) never generate ideas. Sub-$1B earnings reactions are dominated
by liquidity/squeeze noise the signal stack can't price.

LEARNED LAYER (house pattern — log → grade vs real closes → gate n>=30):
every published idea is logged with its feature bands; once the report
is >=3 sessions old the idea is graded against actual price action
(yfinance closes) AND the graded move itself is stored as a uniform
margin metric `mv` (positive == win by construction):
    momentum_into_print    mv = signed 3-session post-report move in bias dir
    post_report_drift      mv = signed 5-session drift (from day-1 close)
    vol_rich               mv = implied - |report reaction|
    vol_cheap              mv = |report reaction| - implied
Per-type hit rates + EV (mean mv) publish at n>=30 ("accruing" until
then) and render as track-record chips on the cards.

FEEDBACK LOOP (same guardrail contract as the UOA edge-weight loop —
guardrails sacred: min-N 30 / shrinkage n/(n+30) / clamp ±15pp /
fail-open): graded outcomes are bucketed into cohorts (type, type×bias,
type×cap-band, type×RS-band, type×session). Cohorts with n>=30 real
grades produce a shrunk, clamped win-rate delta; each new idea's `conf`
score = 50 + the summed deltas of its matching cohorts, and ideas
re-rank by conf within each day. No cohort gated in yet → no conf, no
re-rank, cards unchanged. Nothing is estimated; nothing publishes
before it's real.
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
MIN_MCAP = 1e9          # hard floor — no ideas on sub-$1B names
SHRINK_K = 30           # shrinkage constant: delta *= n / (n + K)
CLAMP_PP = 15.0         # max +/- percentage-point adjustment, per cohort AND total


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
        if (n.get("mcap") or 0) < MIN_MCAP:
            continue                        # tradable size only
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
        cap = n.get("mcap") or 0
        ideas.append({
            "t": t, "date": n.get("date"), "days": n.get("days"),
            "session": n.get("session"), "type": typ, "bias": bias,
            "thesis": thesis, "evidence": ev[:5],
            "implied": implied, "realized_med": rmed,
            "mcap_b": round(cap / 1e9, 1) or None,
            "grade": grade or None, "rs_rank": rs_rank,
            "drift_5d": drift,
            # feature bands — logged with the idea so graded outcomes can
            # be bucketed into learnable cohorts (fail-open on "na")
            "feat": {
                "cap": (">100B" if cap >= 100e9 else
                        "10-100B" if cap >= 10e9 else "1-10B"),
                "rs": ("na" if rs_rank is None else
                       "hi" if rs_rank >= 80 else
                       "mid" if rs_rank >= 50 else "lo"),
                "session": n.get("session") or "na",
            },
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
    """(result, mv) — result win/loss/None(not gradeable yet).

    mv is a uniform margin metric: the graded move expressed so that
    mv > 0 == win for every idea type. Storing it makes the learned
    layer EV-capable (mean mv per cohort), not just hit-rate-capable.
    Session frames per idea type."""
    ev = idea.get("date")
    if not ev:
        return None, None
    rows = _closes_around(idea["t"], ev)
    if not rows:
        return None, None
    # pre = last close strictly BEFORE the event date (AMC: the event-day
    # close is also pre-report, but the 1-day frame from the prior close
    # still brackets the reaction — coarse and stated, consistent for all)
    pre_i = None
    for i, (d, _) in enumerate(rows):
        if d < ev:
            pre_i = i
    if pre_i is None or pre_i + GRADE_AFTER_SESSIONS >= len(rows):
        return None, None                 # not enough post-event sessions
    pre = rows[pre_i][1]
    day1 = rows[pre_i + 1][1]
    d3 = rows[pre_i + 3][1] if pre_i + 3 < len(rows) else None
    d5 = rows[pre_i + 5][1] if pre_i + 5 < len(rows) else None
    move1 = (day1 / pre - 1) * 100
    typ = idea["type"]
    sign = 1 if idea.get("bias") == "bull" else -1
    mv = None
    if typ == "vol_rich":
        mv = (idea.get("implied") or 0) - abs(move1)
    elif typ == "vol_cheap":
        mv = abs(move1) - (idea.get("implied") or 0)
    elif typ == "momentum_into_print" and d3 is not None:
        mv = sign * (d3 / pre - 1) * 100
    elif typ == "post_report_drift" and d5 is not None:
        mv = sign * (d5 / day1 - 1) * 100  # drift measured AFTER the print
    if mv is None:
        return None, None                 # binary_caution isn't a trade
    return ("win" if mv > 0 else "loss"), round(mv, 2)


# ── feedback loop: graded outcomes → cohort deltas → conf on new ideas ──

def _cohort_keys(e):
    """Cohort keys for a log entry OR a fresh idea (same fields).
    Fail-open: missing fields simply produce fewer cohorts."""
    typ = e.get("type")
    if not typ:
        return []
    keys = ["type:" + typ]
    if e.get("bias"):
        keys.append("type:%s|bias:%s" % (typ, e["bias"]))
    f = e.get("feat") or {}
    for k in ("cap", "rs", "session"):
        if f.get(k) and f[k] != "na":
            keys.append("type:%s|%s:%s" % (typ, k, f[k]))
    return keys


def _learned(entries):
    """Per-cohort learned stats from REAL graded outcomes only.

    Guardrails (same contract as the UOA edge-weight loop — sacred):
    min-N 30, shrinkage n/(n+SHRINK_K) toward zero, clamp ±CLAMP_PP,
    fail-open (cohort below gate → simply absent → no adjustment)."""
    coh = {}
    for e in entries:
        r = e.get("result")
        if r not in ("win", "loss"):
            continue
        for k in _cohort_keys(e):
            coh.setdefault(k, []).append((r == "win", e.get("mv")))
    out = {}
    for k, obs in coh.items():
        n = len(obs)
        if n < MIN_N:
            continue
        wr = 100.0 * sum(1 for w, _ in obs if w) / n
        delta = (wr - 50.0) * (n / (n + SHRINK_K))
        mvs = [m for _, m in obs if m is not None]
        out[k] = {"n": n, "wr": round(wr, 1),
                  "delta": round(max(-CLAMP_PP, min(CLAMP_PP, delta)), 2),
                  "ev": round(sum(mvs) / len(mvs), 2) if mvs else None}
    return out


def _apply_conf(ideas, learned):
    """conf = 50 + summed cohort deltas (clamped total), only when at
    least one matching cohort has gated in. Re-rank by conf within each
    day. With no active cohorts this is a no-op — cards unchanged."""
    if not learned:
        return
    any_conf = False
    for i in ideas:
        hits = [learned[k] for k in _cohort_keys(i) if k in learned]
        if not hits:
            continue
        adj = max(-CLAMP_PP, min(CLAMP_PP, sum(h["delta"] for h in hits)))
        i["conf"] = int(round(50 + adj))
        i["conf_cohorts"] = len(hits)
        any_conf = True
    if any_conf:                          # stable — preserves type order
        ideas.sort(key=lambda i: (i["days"], -(i.get("conf") or 50)))


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
                             "feat": i.get("feat"),
                             "published": today, "result": "pending"})
        added += 1

    # grade matured, ungraded ideas (event >= ~GRADE_AFTER_SESSIONS old)
    cutoff = (datetime.now(timezone.utc).date() -
              timedelta(days=GRADE_AFTER_SESSIONS + 4)).isoformat()
    graded = 0
    for i in log["ideas"]:
        if i.get("result") == "pending" and i.get("date") and \
                i["date"] <= cutoff:
            res, mv = _grade(i)
            if res:
                i["result"] = res
                if mv is not None:
                    i["mv"] = mv
                graded += 1
    log["ideas"] = log["ideas"][-LOG_CAP:]

    by_type = {}
    for i in log["ideas"]:
        if i.get("result") in ("win", "loss"):
            by_type.setdefault(i["type"], []).append(
                (i["result"] == "win", i.get("mv")))
    stats = {}
    for k, obs in by_type.items():
        n = len(obs)
        if n >= MIN_N:
            mvs = [m for _, m in obs if m is not None]
            stats[k] = {"status": "active", "n": n,
                        "win_rate": round(100 * sum(1 for w, _ in obs
                                                    if w) / n),
                        "ev": (round(sum(mvs) / len(mvs), 2)
                               if mvs else None)}
        else:
            stats[k] = {"status": "accruing", "n": n, "activates_at": MIN_N}

    # feedback loop: graded cohorts adjust conf on today's ideas
    learned = _learned(log["ideas"])
    _apply_conf(ideas, learned)

    payload = {
        "generated": datetime.now(timezone.utc)
        .isoformat(timespec="seconds"),
        "count": len(ideas),
        "ideas": ideas,
        "by_type": stats,
        "total_graded": sum(len(v) for v in by_type.values()),
        "min_mcap_b": round(MIN_MCAP / 1e9),
        "learned": {
            "status": "active" if learned else "accruing",
            "cohorts_active": len(learned),
            "activates_at": MIN_N,
            # publish the gated cohorts themselves — transparency over
            # a black box (small dict; only n>=MIN_N cohorts appear)
            "cohorts": learned,
        },
        "note": ("Deterministic idea cards from the site's own signals "
                 "(swing grade, RS, flow-into-print, implied vs realized "
                 "history, post-report drift). $" +
                 str(round(MIN_MCAP / 1e9)) + "B market-cap floor. "
                 "Self-graded against real closes once each report is " +
                 str(GRADE_AFTER_SESSIONS) + "+ sessions old; per-type "
                 "hit rates + EV publish at n>=" + str(MIN_N) +
                 "; graded cohorts feed back into each idea's conf score "
                 "(shrunk, clamped, fail-open). Educational, not advice."),
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
