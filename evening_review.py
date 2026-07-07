"""
evening_review.py — the nightly self-grade ("what worked, what failed, why").

Runs post-close after swing_report.py + missed_opportunities.py:

  1. PICKS   — compute tonight's top-conviction picks (server-side mirror of
               the desk's Opportunities logic: confluence of swing grade,
               flow direction, momentum lists) and append them to
               data/picks_history.json. These are tomorrow's graded cohort.
  2. GRADE   — grade the PRIOR session's picks against today's actual
               close-to-close move (swing run cards carry day %). Bull picks
               win when the name closed up; bear picks when it closed down.
               Next-day grading is the honest first horizon — +5d arrives
               via the signal-outcome tracker separately.
  3. LEARN   — surface what the edge-weight learner currently likes /
               dislikes and any feature whose recent hit rate is decaying.
  4. MISSED  — fold in tonight's missed-opportunity audit summary.
  5. EMIT    — docs/reports/evening_review.json for the Desk EOD card.

No network calls — everything reads files the earlier EOD steps produced,
so this step can never rate-limit or flake independently.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

_BASE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(_BASE, "docs", "reports")
PICKS_PATH = os.path.join(_BASE, "data", "picks_history.json")
OUT_PATH = os.path.join(REPORTS, "evening_review.json")

TOP_BULLS = 5
TOP_BEARS = 2
PICKS_CAP_DAYS = 120


def _load(name, base=REPORTS):
    try:
        with open(os.path.join(base, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ── 1. tonight's picks — server-side confluence mirror ─────────────────────

_GRADE_PTS = {"A+": 4, "A": 3, "A-": 2, "B+": 1,
              "G": 4, "G+": 3, "F": 2, "F+": 2, "F-": 2}


def compute_picks(swing, uoa, qm, sb):
    """Top confluence names tonight. Mirrors the desk's ranking closely
    enough to be graded as 'the system's picks' (grade pts + flow agreement
    + momentum-list membership), without duplicating the client verbatim."""
    if not swing or not swing.get("runs"):
        return []
    run = swing["runs"][-1]
    flow_dir = {}
    best_flow = {}
    for r in (uoa or {}).get("rows") or []:
        t = r.get("ticker")
        if t and (t not in best_flow or
                  (r.get("trade_score") or 0) > (best_flow[t].get("trade_score") or 0)):
            best_flow[t] = r
    for t, r in best_flow.items():
        flow_dir[t] = (r.get("direction") or "").lower()
    momentum = set()
    for d in (qm, sb):
        if d and d.get("runs"):
            for row in d["runs"][-1].get("rows") or []:
                if row.get("ticker"):
                    momentum.add(row["ticker"])

    bulls, bears = [], []
    for g, cards in (run.get("grades") or {}).items():
        pts = _GRADE_PTS.get(g)
        if pts is None:
            continue
        bearish_grade = g[:1] in ("F", "G")
        for c in cards or []:
            t = c.get("t")
            if not t:
                continue
            fd = flow_dir.get(t)
            score, sigs, ev = pts, 1, [f"swing {g}"]
            if fd == ("bearish" if bearish_grade else "bullish"):
                score += 2
                sigs += 1
                ev.append(f"{fd} flow")
            elif fd and fd != "mixed":
                continue                     # trend/flow conflict — not a pick
            if t in momentum and not bearish_grade:
                score += 1
                sigs += 1
                ev.append("momentum list")
            if sigs < 2:
                continue                     # confluence requires >=2 signals
            conviction = min(96, 38 + score * 6 + sigs * 4)
            entry = {"t": t, "dir": "bear" if bearish_grade else "bull",
                     "conviction": conviction, "evidence": ev}
            (bears if bearish_grade else bulls).append(entry)
    bulls.sort(key=lambda x: -x["conviction"])
    bears.sort(key=lambda x: -x["conviction"])
    return bulls[:TOP_BULLS] + bears[:TOP_BEARS]


def append_picks(date, picks):
    hist = []
    try:
        with open(PICKS_PATH, encoding="utf-8") as f:
            hist = json.load(f).get("days") or []
    except Exception:
        pass
    hist = [d for d in hist if d.get("date") != date]
    hist.append({"date": date, "picks": picks})
    hist.sort(key=lambda d: d.get("date") or "")
    hist = hist[-PICKS_CAP_DAYS:]
    os.makedirs(os.path.dirname(PICKS_PATH), exist_ok=True)
    with open(PICKS_PATH, "w", encoding="utf-8") as f:
        json.dump({"days": hist}, f, separators=(",", ":"))
    return hist


# ── 2. grade the prior session's picks ─────────────────────────────────────

def grade_prior(hist, today, chg_by_ticker):
    """Grade the most recent prior day's picks AND persist the outcomes back
    into that day's history entry — the accumulating record is what powers
    calibration + the rolling track record."""
    prior = [d for d in hist if (d.get("date") or "") < today]
    if not prior:
        return None
    day = prior[-1]
    results = []
    for p in day.get("picks") or []:
        chg = chg_by_ticker.get(p["t"])
        win = None
        if isinstance(chg, (int, float)):
            win = (chg > 0) if p["dir"] == "bull" else (chg < 0)
        results.append({**p, "chg": chg, "win": win})
    graded = [r for r in results if r["win"] is not None]
    wins = sum(1 for r in graded if r["win"])
    # persist outcomes onto the history entry (idempotent per date)
    day["graded_results"] = [{"t": r["t"], "dir": r["dir"],
                              "conviction": r["conviction"],
                              "chg": r["chg"], "win": r["win"]}
                             for r in results]
    try:
        with open(PICKS_PATH, "w", encoding="utf-8") as f:
            json.dump({"days": hist}, f, separators=(",", ":"))
    except Exception:
        pass
    return {"date": day["date"], "n": len(graded), "wins": wins,
            "results": results}


# ── 2b. calibration + rolling record from the accumulated history ──────────
# Calibration: displayed conviction vs REALIZED next-day win rate, per band.
# GATED like every learned quantity: nothing is published until >=30 graded
# picks exist and a band has >=10 of its own — small-sample calibration is
# worse than none. Until then the payload says "accruing", honestly.
CAL_MIN_TOTAL = 30
CAL_MIN_BAND  = 10
CAL_BANDS = ((0, 69, "<70"), (70, 79, "70-79"), (80, 89, "80-89"),
             (90, 200, "90+"))
RECORD_WINDOW_D = 30


def calibration_and_record(hist):
    graded = []
    for d in hist:
        for r in d.get("graded_results") or []:
            if r.get("win") is not None:
                graded.append(r)
    rec_days = [d for d in hist if d.get("graded_results")][-RECORD_WINDOW_D:]
    rec_g = [r for d in rec_days for r in d.get("graded_results") or []
             if r.get("win") is not None]
    record = {"sessions": len(rec_days), "n": len(rec_g),
              "wins": sum(1 for r in rec_g if r["win"])} if rec_g else None
    if len(graded) < CAL_MIN_TOTAL:
        return ({"status": "accruing", "graded": len(graded),
                 "activates_at": CAL_MIN_TOTAL}, record)
    bands = {}
    for lo, hi, label in CAL_BANDS:
        rows = [r for r in graded if lo <= (r.get("conviction") or 0) <= hi]
        if len(rows) < CAL_MIN_BAND:
            continue
        bands[label] = {"n": len(rows),
                        "win_rate": round(100 * sum(1 for r in rows
                                                    if r["win"]) / len(rows))}
    if not bands:
        return ({"status": "accruing", "graded": len(graded),
                 "activates_at": CAL_MIN_TOTAL}, record)
    return ({"status": "active", "graded": len(graded), "bands": bands},
            record)


# ── 3+4. learning + missed summaries ───────────────────────────────────────

def learning_summary():
    ew = _load("edge_weights.json")
    if not ew:
        return None
    feats = sorted((ew.get("features") or {}).items(),
                   key=lambda x: -abs(x[1].get("adj") or 0))
    top = [{"f": k, "adj": v["adj"]} for k, v in feats[:4] if v.get("adj")]
    dying = [{"f": k, "prior": v["prior_hit"], "recent": v["recent_hit"]}
             for k, v in sorted((ew.get("decay") or {}).items(),
                                key=lambda x: x[1]["delta"])
             if v["delta"] <= -0.08][:3]
    regimes = (ew.get("regimes") or {}).get("status")
    return {"top": top, "dying": dying, "regimes": regimes,
            "version": ew.get("version")}


def build():
    swing = _load("swing_latest_summary.json") or _load("swing_report.json")
    uoa = _load("uoa_latest.json")
    qm = _load("momentum_qm.json")
    sb = _load("momentum_stockbee.json")
    missed = _load("missed_opportunities.json")

    today = (swing["runs"][-1].get("date")
             if swing and swing.get("runs") else
             datetime.now(timezone.utc).date().isoformat())
    chg = {}
    if swing and swing.get("runs"):
        for g, cards in (swing["runs"][-1].get("grades") or {}).items():
            for c in cards or []:
                if c.get("t") and isinstance(c.get("chg"), (int, float)):
                    chg[c["t"]] = c["chg"]

    picks = compute_picks(swing, uoa, qm, sb)
    hist = append_picks(today, picks)
    graded = grade_prior(hist, today, chg)
    calibration, record = calibration_and_record(hist)

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_date": today,
        "graded": graded,
        "calibration": calibration,
        "record": record,
        "learning": learning_summary(),
        "missed": ({"missed_count": missed.get("missed_count"),
                    "checked": missed.get("checked"),
                    "by_reason": missed.get("by_reason")}
                   if missed and missed.get("checked") else None),
        "tomorrow": picks,
    }
    if graded:
        print(f"  Evening review: {graded['wins']}/{graded['n']} of "
              f"{graded['date']}'s picks worked next-day")
    else:
        print("  Evening review: first run — grading starts next session")
    print(f"  Tomorrow's watchlist: "
          f"{', '.join(p['t'] for p in picks) or '(no confluence picks)'}")
    return payload


def main():
    payload = build()
    os.makedirs(REPORTS, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"  Wrote {os.path.relpath(OUT_PATH, _BASE)}")


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
