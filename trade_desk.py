"""
trade_desk.py — AI Trade Desk engine: candidate generation, empirical
Alpha Score, qualification gates, immutable idea ledger, forward grading.

ALPHA IS PARAMOUNT. The pipeline is deterministic end-to-end; nothing
here is estimated by an LLM, and no strategy family surfaces an
actionable Top Idea unless its own measured, walk-forward-validated
record says it has edge. As of first ship, NO family qualifies:

  flow      trade_desk_research.json registry -> no champion (the
            setup-level purged walk-forward found no tail clearing its
            promotion criteria)
  earnings  earnings_ideas.json by_type: directional types need an
            event-date-clustered CI low above the modeled stock round
            trip; vol types are signal-level only
  momentum  data/setup_outcomes_log.json — the swing A-tier cohort the
            candidates are drawn from, de-duplicated, date-clustered CI

The correct output today is therefore "no trade meets the alpha
threshold" — candidates still generate, score, freeze into the
append-only ledger and grade forward (paper forward test, spec sec 58),
so the record that could unlock qualification accrues from day one.
Gates re-evaluate on every run from the measured files; no human
flips a constant to force ideas onto the page.

Pipeline (all inputs are local JSONs published earlier in the same CI
run — this module makes NO market-data API calls except yfinance closes
for grading matured ledger entries):

  generate_candidates()  flow (uoa_signals_scored) + earnings
                         (earnings_ideas) + momentum (latest swing run,
                         top names of each A grade)
  enrich()               features at flag time (shared with the
                         validation module — lockstep by import; regime
                         = the PRIOR session's label)
  hard_filters()         liquidity / premium / staleness / measured
                         regime conflict — every rejection has a reason
  rank()                 Research Rank (Alpha Score v1) = percentile of
                         expected +5d direction-signed excess within the
                         graded record (trade_desk_validation.json);
                         walk-forward skill ~zero, so it only sorts
  family gates           measured track record decides IDEA vs WATCH
  construct()            reference structure + risk framing (no invented
                         marks: fields the pipeline can't know are null)
  ledger + grading       data/trade_desk_log.jsonl (append-only), one
                         paper position per ticker+direction per open
                         5-session window and one per earnings event;
                         flow/momentum: +5-session direction-signed
                         excess vs SPY from the issue-day close;
                         earnings: the earnings loop's own grader (drift
                         from the reaction close) -> performance with
                         date-clustered CIs + Research Rank calibration
                         (sub-floor flow kept as a paper-only shadow so
                         every band can fill), gated at MIN_N

Outputs docs/reports/trade_desk.json for the site and the worker's
Ask-TickerDesk endpoint.

    python trade_desk.py            # full run
    python trade_desk.py --dry-run  # print, don't write
    python trade_desk.py --no-grade # skip yfinance grading (offline)
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, date, datetime, timedelta, timezone
from statistics import mean, median

import exchange_calendar
from earnings_ideas import _grade as earnings_loop_grade
from trade_desk_context import latest_swing_run
from trade_desk_validation import (
    MODEL_VERSION,
    STOCK_RT_COST_PP,
    _regime_map,
    cluster_ci95,
    features_at_flag,
    json_sanitize,
    prior_regime,
)

_BASE = os.path.dirname(os.path.abspath(__file__))
R = lambda *p: os.path.join(_BASE, *p)

LEDGER_PATH = R("data", "trade_desk_log.jsonl")
OUT_PATH = R("docs", "reports", "trade_desk.json")
VALIDATION_PATH = R("docs", "reports", "trade_desk_validation.json")
RESEARCH_PATH = R("docs", "reports", "trade_desk_research.json")
SETUP_LOG_PATH = R("data", "setup_outcomes_log.json")
SETUP_OUTCOMES_PATH = R("docs", "reports", "setup_outcomes.json")

ENGINE_VERSION = "trade_desk_v1"
MIN_N = 30  # house gate: no published stat below this
GRADE_HOLD_SESSIONS = 5
# Grade version 2 (2026-11 audit): earnings ideas are graded by the
# earnings loop's own grader (post_report_drift from the REACTION close),
# so desk and gate share one definition. Older earnings grades measured
# from the issue-day close — before the print — and no longer count.
GRADE_VERSION = 2
EARNINGS_GRADE_PER_RUN = 60  # one Yahoo history call each
MAX_IDEAS = 5  # hard ceiling on Top Ideas (spec: 0-5, never fill)
MAX_WATCH = 8  # display cap per tier (experimental: per family)
MIN_PREMIUM = 250_000  # flow candidates below this never rank
STALE_MIN = 90  # flow signal older than this (minutes) is stale
SCORE_FLOOR = 70  # candidates below never reach WATCH display
# Momentum candidates = the top names of each A grade in the latest
# swing run — exactly the bull cohort setup_outcomes.py logs and grades
# (PER_GRADE there), so the family's gate measures the same population.
MOMENTUM_GRADES = ("A+", "A", "A-")
MOMENTUM_PER_GRADE = 5
# Research Rank calibration bands. Sub-floor flow candidates are frozen
# as paper-only SHADOW ideas (never displayed) so the low band can fill
# and calibration is actually testable.
CAL_BANDS = (
    (90, 101, "90-100"),
    (80, 90, "80-89"),
    (SCORE_FLOOR, 80, f"{SCORE_FLOOR}-79"),
    (0, SCORE_FLOOR, f"<{SCORE_FLOOR} (shadow)"),
)
TARGET_EXCESS = "+5-session direction-signed excess vs SPY, from the issue-day close"
TARGETS = {
    "flow": TARGET_EXCESS,
    "momentum": TARGET_EXCESS,
    "earnings:post_report_drift": (
        "direction-signed raw 5-session return from the earnings-reaction "
        "close (the earnings gate's own definition, not vs SPY)"
    ),
    "earnings:momentum_into_print": (
        "direction-signed raw 3-session return from the close before the "
        "report (the earnings gate's own definition, not vs SPY)"
    ),
}

REJECT = {  # candidate-level rejections (spec sec 54)
    "LOW_ALPHA": 0,
    "BAD_LIQUIDITY": 0,
    "LOW_PREMIUM": 0,
    "REGIME_CONFLICT": 0,
    "STRATEGY_DEGRADED": 0,
    "STALE_DATA": 0,
    "INSUFFICIENT_DATA": 0,
}
# Print-level skips: individual flow prints that never become candidates
# (sellers / hedges / income). Counted apart — they are not candidates.
SKIPPED = {"NOT_DIRECTIONAL": 0}


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _now():
    return datetime.now(UTC)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _sessions_after(d0, d1, cap=None):
    """Trading sessions strictly after d0 up to and including d1
    (YYYY-MM-DD strings or dates). Uses the exchange calendar; outside
    its known years falls back to weekdays. Stops counting at `cap`."""
    a = date.fromisoformat(str(d0)[:10])
    b = date.fromisoformat(str(d1)[:10])
    n, cur = 0, a + timedelta(days=1)
    while cur <= b:
        try:
            ok = exchange_calendar.is_session(cur)
        except exchange_calendar.CalendarRangeError:
            ok = cur.weekday() < 5
        n += 1 if ok else 0
        if cap is not None and n >= cap:
            return n
        cur += timedelta(days=1)
    return n


def _overlaps(d0, d1):
    """True when a paper position opened at d0's close is still open at
    d1's close: its 5-session window has not finished, so a second idea
    on d1 would re-count the same move."""
    return _sessions_after(d0, d1, cap=GRADE_HOLD_SESSIONS) < GRADE_HOLD_SESSIONS


def _stock_gate(ev, ci, n, win, what, cluster):
    """Verdict for a family graded on STOCK-level returns. A bare
    positive average is not evidence: qualification needs the clustered
    95% CI low above the modeled round-trip cost. Net-of-cost EV <= 0 is
    degraded; anything between is watch."""
    cost = STOCK_RT_COST_PP
    ci_ok = isinstance(ci, list) and len(ci) == 2 and None not in ci
    if ev is None:
        verdict = "watch"
    elif ci_ok and ci[0] > cost:
        verdict = "qualified"
    elif ev - cost <= 0:
        verdict = "degraded"
    else:
        verdict = "watch"
    ci_txt = f"[{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci_ok else "unavailable"
    why = (
        f"{what}: EV {ev:+.2f} pp · {win}% win · n={n} · 95% CI {ci_txt} "
        f"({cluster}-clustered) · qualifies only when the CI low clears the "
        f"{cost} pp modeled round trip"
        if ev is not None
        else f"{what}: no measured EV"
    )
    return verdict, why


def _momentum_record(log=None):
    """The momentum family's own record: setup_outcomes' bull cohort
    (the top MOMENTUM_PER_GRADE names of each A grade per night — the
    population the candidates are drawn from). One position per ticker
    per open 5-session window; CI clustered by pick date. None when the
    per-pick log is unavailable (e.g. local runs without an R2 pull)."""
    log = _load(SETUP_LOG_PATH, None) if log is None else log
    if not log or not log.get("days"):
        return None
    picks = []
    for day in log["days"]:
        for r in day.get("rows") or []:
            ret = r.get("ret")
            if (
                r.get("dir") == "bull"
                and r.get("grade") in MOMENTUM_GRADES
                and isinstance(ret, (int, float))
                and r.get("t")
                and day.get("date")
            ):
                picks.append((day["date"], r["t"], float(ret)))
    picks.sort()
    kept, dups, opened = [], 0, {}
    for d, t, ret in picks:
        if t in opened and _overlaps(opened[t], d):
            dups += 1
            continue
        opened[t] = d
        kept.append((d, ret))
    ys = [y for _, y in kept]
    ci = cluster_ci95(kept)
    return {
        "n": len(ys),
        "duplicates_excluded": dups,
        "hit": round(100 * sum(1 for y in ys if y > 0) / len(ys)) if ys else None,
        "avg": ci["mean"],
        "med": round(median(ys), 2) if ys else None,
        "ci95": ci["ci95"],
        "dates": ci["clusters"],
    }


# ------------------------------------------------------------ families
# Each family's gate reads its own MEASURED record. Verdicts:
#   qualified  -> DIRECTIONAL qualified: a stock-level paper edge (CI low
#                 above the modeled round trip). Counts on its own tile,
#                 never as "Trade Qualified" (that is reserved for an
#                 executable option expression).
#   watch      -> ideas display in the not-yet-qualified rail only
#   degraded   -> family keeps generating ideas into the paper ledger,
#                 displayed in the collapsed Experimental tier


def family_gates():
    gates = {}

    # Flow gate authority: the champion/challenger registry from the
    # setup-level research (purged walk-forward, extreme-tail precision,
    # cluster-bootstrap CI). A challenger promotes to champion ONLY on
    # its predefined OOS criteria; no champion -> the family abstains.
    reg = (_load(RESEARCH_PATH, {}) or {}).get("registry") or {}
    champ = reg.get("champion") or {}
    if champ.get("name"):
        gates["flow"] = {
            "verdict": "qualified",
            "champion": champ.get("name"),
            "tail_pct": champ.get("tail_pct"),
            "why": "champion model holds validated extreme-tail edge (see research)",
            "evidence": (
                (reg.get("challengers") or {}).get(champ["name"], {}).get("verdict") or {}
            ).get("evidence"),
        }
    else:
        gates["flow"] = {
            "verdict": "watch",
            "why": "no champion: setup-level walk-forward "
            "found no tail that clears the "
            "promotion criteria (positive pooled "
            "avg+median, bootstrap CI low above the "
            f"{STOCK_RT_COST_PP} pp modeled round trip, "
            "positive in 2/3 folds, positive holdout)",
            "evidence": {"status": "no_champion"},
        }

    ei = _load(R("docs", "reports", "earnings_ideas.json"), {})
    by_type = ei.get("by_type") or {}
    # Gating ladder for vol types (reviewer-corrected): a validated
    # implied-vs-realized relationship is a QUALIFIED SIGNAL, not a
    # qualified trade — premium selling is where win rate conceals
    # negative skew. trade_qualified requires the option-P&L
    # reconstruction backtest to clear costs and tails.
    vol = _load(R("docs", "reports", "earnings_vol.json"), {})
    vol_types = vol.get("types") or {}
    # EXEC/EXEC decides (reviewer): only the NBBO execution study can
    # grant trade_qualified. The daily-bar backtest remains context.
    bt = _load(R("docs", "reports", "earnings_vol_exec.json"), {})
    ern = {}
    for t, st in by_type.items():
        unit = "pp"  # vol mv is pp of stock price too, not vol points
        if st.get("status") != "active" or st.get("n", 0) < MIN_N:
            ern[t] = {"verdict": "watch", "why": "accruing", "evidence": st}
        elif not t.startswith("vol_"):
            # Directional earnings types are stock trades: the record is a
            # raw signed return (not vs SPY), one grade per event, CI
            # clustered by event date (earnings_ideas by_type.ci95).
            verdict, why = _stock_gate(
                st.get("ev"),
                st.get("ci95"),
                st.get("n"),
                st.get("win_rate"),
                "raw signed return (not vs SPY)",
                "event-date",
            )
            ern[t] = {"verdict": verdict, "why": why, "evidence": st}
        elif (st.get("ev") or 0) > 0:
            if t.startswith("vol_"):
                vt = vol_types.get(t) or {}
                boot = vt.get("date_cluster_bootstrap") or {}
                if bt.get("types", {}).get(t, {}).get("trade_qualified"):
                    verdict, note = (
                        "trade_qualified",
                        "NBBO execution study cleared VOL_RICH_EXEC_V1",
                    )
                elif vt.get("signal_qualified"):
                    verdict = "signal_qualified"
                    ci = boot.get("ci95") or []
                    note = (
                        f"night-cluster CI {ci} on "
                        f"{boot.get('nights')} nights — trade "
                        "expression pending executable validation"
                    )
                else:
                    verdict, note = "watch", "signal CI not yet excluding zero at night level"
                ern[t] = {
                    "verdict": verdict,
                    "why": (
                        f"measured EV {st['ev']:+.2f} {unit} · "
                        f"{st.get('win_rate')}% win · "
                        f"n={st['n']} · {note}"
                    ),
                    "evidence": st,
                    "vol": vt
                    and {
                        "bootstrap": boot,
                        "move_ratio": vt.get("move_ratio"),
                        "tails": vt.get("tails"),
                    }
                    or None,
                }
        else:
            ern[t] = {
                "verdict": "degraded",
                "why": (f"measured EV {st['ev']:+.2f} {unit} on n={st['n']}"),
                "evidence": st,
            }
    gates["earnings"] = {"verdict": "per_type", "types": ern}
    gates["momentum"] = _momentum_gate()
    return gates


def _momentum_gate():
    """Gate for the momentum family (swing A-tier candidates), read from
    the record of that same cohort. Entry in that record is the close of
    the night the grade was computed — slightly optimistic, since the
    grade is only known after that close."""
    what = "raw +5-session long return, not vs SPY"
    base = {"label": "Momentum (swing A-tier)", "target": what}
    rec = _momentum_record()
    if rec is not None:
        if rec["n"] < MIN_N:
            return dict(base, verdict="watch", why=f"accruing (n={rec['n']})", evidence=rec)
        verdict, why = _stock_gate(rec["avg"], rec["ci95"], rec["n"], rec["hit"], what, "pick-date")
        why += f" · {rec['duplicates_excluded']} overlapping repeat picks excluded"
        return dict(base, verdict=verdict, why=why, evidence=rec)
    # No per-pick log: the published aggregate can show the direction of
    # the record, but without a CI it can never qualify.
    so = _load(SETUP_OUTCOMES_PATH, {})
    rows = [
        g
        for k, g in (so.get("grades") or {}).items()
        if k in MOMENTUM_GRADES and g.get("status") == "active" and g.get("avg") is not None
    ]
    n = sum(g.get("n") or 0 for g in rows)
    if n < MIN_N:
        return dict(base, verdict="watch", why="accruing", evidence={"n": n})
    avg = sum((g.get("n") or 0) * g["avg"] for g in rows) / n
    verdict = "degraded" if avg - STOCK_RT_COST_PP <= 0 else "watch"
    why = (
        f"{what}: EV {avg:+.2f} pp on n={n} (published per-grade aggregate, "
        "repeat picks not de-duplicated, no CI — cannot qualify)"
    )
    return dict(base, verdict=verdict, why=why, evidence={"n": n, "avg": round(avg, 2)})


# --------------------------------------------------------------- score


def _score_fn():
    """Alpha Score from the validation module's production model —
    percentile of expected +5d signed excess within the graded record.
    Returns (fn, available). Without a validation file the score is
    honestly unavailable (None), never guessed."""
    v = _load(VALIDATION_PATH, {})
    pm = v.get("production_model")
    if not pm or not pm.get("adj") or not pm.get("scale_anchors"):
        return (lambda feats: None), False
    base, adj, anchors = pm["base"], pm["adj"], pm["scale_anchors"]

    def score(feats):
        total = sum(adj.get(f + ":" + str(val), 0.0) for f, val in feats.items())
        pred = base + max(-6.0, min(6.0, total))
        # anchors = expected-excess values at percentiles 0,5,...,100
        if pred <= anchors[0]:
            return 0.0  # a percentile cannot go below 0
        lo = 0
        for i, a in enumerate(anchors):
            if pred >= a:
                lo = i
        if lo >= len(anchors) - 1:
            return 100.0
        a0, a1 = anchors[lo], anchors[lo + 1]
        frac = (pred - a0) / (a1 - a0) if a1 > a0 else 0.0
        return round(max(0.0, min(100.0, (lo + frac) * 5.0)), 1)

    return score, True


# ---------------------------------------------------------- candidates


def _flow_candidates(regimes, now):
    """Today's buyer-initiated directional flow, one candidate per
    ticker+direction: the largest-premium fresh signal anchors the
    cluster; sibling prints become supporting evidence."""
    d = _load(R("docs", "reports", "uoa_signals_scored.json"), {})
    sigs = d.get("signals") or []
    today = now.astimezone(timezone(timedelta(hours=-4))).date().isoformat()
    clusters = {}
    for s in sigs:
        if (s.get("flagged_at") or "")[:10] != today:
            continue
        # The scored feed omits the ledger's `direction` field — derive
        # it the same way the scanner does: buyer-initiated call=bullish,
        # put=bearish; sellers/hedges are non-directional.
        direction = s.get("direction")
        if direction not in ("bullish", "bearish"):
            side = s.get("flow_side") or ""
            if side.endswith("_buyer"):
                direction = {"call": "bullish", "put": "bearish"}.get(s.get("type"))
        if direction not in ("bullish", "bearish") or s.get("flow_side") in (
            "put_seller",
            "call_seller",
        ):
            SKIPPED["NOT_DIRECTIONAL"] += 1  # a print, not a candidate
            continue
        s = dict(s, direction=direction)
        key = (s.get("ticker"), direction)
        clusters.setdefault(key, []).append(s)
    out = []
    for (tk, direction), grp in clusters.items():
        grp.sort(key=lambda x: x.get("premium") or 0, reverse=True)
        anchor = grp[0]
        feats = features_at_flag(anchor, regimes)
        out.append(
            {
                "family": "flow",
                "ticker": tk,
                "direction": direction,
                "anchor": anchor,
                "n_prints": len(grp),
                "total_premium": sum(x.get("premium") or 0 for x in grp),
                "feats": feats,
                "catalyst": ("earnings" if feats["ern"] == "yes" else "flow"),
            }
        )
    return out


def _fresh(doc, now, max_h):
    """Source-file freshness guard — candidates from a stale upstream
    JSON must never surface (STALE_DATA), e.g. after a weekend or a
    paused pipeline."""
    try:
        gen = datetime.fromisoformat(
            (doc.get("generated") or doc.get("updated") or "").replace("Z", "+00:00")
        )
        if gen.tzinfo is None:
            gen = gen.replace(tzinfo=UTC)
        return (now - gen).total_seconds() <= max_h * 3600
    except ValueError, TypeError:
        return False


def _earnings_candidates(now):
    d = _load(R("docs", "reports", "earnings_ideas.json"), {})
    if not _fresh(d, now, 48):
        REJECT["STALE_DATA"] += len(d.get("ideas") or [])
        return []
    # n_reports (analogue count for Fair Move) lives in earnings_edge
    edge = _load(R("docs", "reports", "earnings_edge.json"), {})
    n_reports = {r.get("t"): r.get("n_reports") for r in edge.get("names") or []}
    out = []
    today = now.astimezone(timezone(timedelta(hours=-4))).date()
    for i in d.get("ideas") or []:
        # Pre-print idea types die once the report is out: a vol_rich
        # premium-sell into a print that already happened is not a
        # trade. post_report_drift is post-print and stays valid.
        if i.get("type") in ("vol_rich", "vol_cheap", "momentum_into_print"):
            try:
                ed = datetime.strptime(i.get("date") or "", "%Y-%m-%d").date()
            except ValueError:
                REJECT["STALE_DATA"] += 1
                continue
            if ed < today or (ed == today and i.get("session") == "BMO"):
                REJECT["STALE_DATA"] += 1
                continue
        bias = i.get("bias")
        direction = {"bull": "bullish", "bear": "bearish"}.get(bias)
        idea = dict(i)
        if idea.get("n_reports") is None:
            idea["n_reports"] = n_reports.get(i.get("t"))
        out.append(
            {
                "family": "earnings",
                "ticker": i.get("t"),
                "direction": direction or "neutral",
                "etype": i.get("type"),
                "idea": idea,
                "catalyst": "earnings",
            }
        )
    return out


def _momentum_candidates(now):
    """Top MOMENTUM_PER_GRADE names of each A grade in the LATEST swing
    run (swing_latest_summary.runs is [prior, latest]; grades live under
    run["grades"][grade] as cards keyed "t")."""
    s = _load(R("docs", "reports", "swing_latest_summary.json"), {})
    latest = latest_swing_run(s)
    if not latest or not _fresh(s, now, 48):
        return []
    grades = latest.get("grades") or {}
    out = []
    for g in MOMENTUM_GRADES:
        for r in (grades.get(g) or [])[:MOMENTUM_PER_GRADE]:
            tk = r.get("t")
            if not tk:
                continue
            out.append(
                {
                    "family": "momentum",
                    "ticker": tk,
                    "direction": "bullish",
                    "grade": g,
                    # slim card: no spark array in the published JSON
                    "row": {
                        "grade": g,
                        "swing_date": latest.get("date"),
                        "chg": r.get("chg"),
                        "ext": r.get("ext"),
                        "rvol": r.get("rvol"),
                        "sector": r.get("sec"),
                    },
                    "catalyst": "momentum",
                }
            )
    return out


# ------------------------------------------------------------- filters


def _regime_today(regimes, now=None):
    """Latest regime label — but only if it is recent. A label from a
    paused pipeline must not silently gate today's candidates."""
    if not regimes:
        return "unknown"
    latest = max(regimes)
    if now is not None:
        try:
            age = (now.date() - datetime.strptime(latest, "%Y-%m-%d").date()).days
            if age > 5:
                return "unknown"
        except ValueError:
            return "unknown"
    return regimes.get(latest) or "unknown"


def _regime_conflicts():
    """(regime|direction) cells the validation record measured as
    significantly worse than directional flow overall
    (trade_desk_validation.json regime_conflict). Nothing measured ->
    nothing blocked; no hard-coded cells."""
    rc = (_load(VALIDATION_PATH, {}) or {}).get("regime_conflict") or {}
    return frozenset(rc.get("conflicts") or [])


def hard_filter(c, regime, score, now, conflicts=frozenset()):
    """Returns None to keep, or a rejection reason string. `regime` is
    the PRIOR session's label (what was knowable during today's session);
    `conflicts` the measured regime|direction cells to block."""
    if c["family"] == "flow":
        a = c["anchor"]
        if (a.get("liquidity") or "D") == "D":
            return "BAD_LIQUIDITY"
        if (c.get("total_premium") or 0) < MIN_PREMIUM:
            return "LOW_PREMIUM"
        try:
            flagged = datetime.fromisoformat(a["flagged_at"])
            if (now - flagged).total_seconds() > STALE_MIN * 60:
                return "STALE_DATA"
        except KeyError, ValueError, TypeError:
            return "STALE_DATA"
        # Measured regime conflict, derived each run from the graded
        # record (date-clustered CI wholly below all-flow average) — no
        # constants: the old cited EVs could not be reproduced.
        if f"{regime}|{c['direction']}" in conflicts:
            return "REGIME_CONFLICT"
        if score is None:
            return "INSUFFICIENT_DATA"
        if score < SCORE_FLOOR:
            return "LOW_ALPHA"
    elif c["family"] == "earnings":
        if not c["ticker"]:
            return "INSUFFICIENT_DATA"
    return None


# ------------------------------------------------------- construction


def construct(c):
    """Deterministic expression framing. No invented marks — the
    pipeline knows the anchor contract's premium/vol/OI at flag time but
    NOT its live bid/ask; those fields are null and the UI must render
    'Unavailable' (spec sec 50)."""
    if c["family"] == "flow":
        a = c["anchor"]
        return {
            "expression": "underlying"
            if a.get("liquidity") in (None, "C")
            else "reference_contract",
            "reference_contract": a.get("contract"),
            "contract_liquidity": a.get("liquidity"),
            "dte": a.get("dte"),
            "flag_premium": a.get("premium"),
            "flag_volume": a.get("volume"),
            "flag_oi": a.get("open_interest"),
            "underlying_at_flag": a.get("underlying_px_at_flag"),
            "option_mark": None,  # not knowable here — honest null
            "max_risk_note": "defined-risk if expressed via long option "
            "(100% of premium); underlying expression "
            "risk is stop-managed",
        }
    if c["family"] == "earnings":
        i = c["idea"]
        out = {
            "expression": "per_thesis",
            "implied_move": i.get("implied"),
            "realized_median": i.get("realized_med"),
            "session": i.get("session"),
            "event_date": i.get("date"),
            "option_mark": None,
        }
        # TickerDesk Fair Move — the ticker's own median |earnings
        # move| across its reported history. Volatility Edge and
        # Richness derive from it; the vol stance states WHICH side
        # the measured edge favors. Displayed only when both legs
        # exist — never computed from a guess.
        imp, fair = i.get("implied"), i.get("realized_med")
        if imp and fair and fair > 0:
            out["fair_move"] = fair
            out["vol_edge_pp"] = round(imp - fair, 1)
            out["richness_pct"] = round(100 * (imp / fair - 1))
            out["analogues"] = i.get("n_reports")
        if c.get("etype") == "vol_rich":
            out["vol_stance"] = "SELL VOL"
        elif c.get("etype") == "vol_cheap":
            out["vol_stance"] = "BUY VOL"
        # Historical behavior — the ticker's own graded vol events when
        # >= 3 exist (rare: one report per quarter), else the vol-rich
        # cohort, labeled. Powers the card's volatility-intelligence
        # block; numbers come straight from earnings_vol.json.
        ev = _load(R("docs", "reports", "earnings_vol.json"), {})
        th = (ev.get("ticker_history") or {}).get(c["ticker"])
        if th:
            out["history"] = dict(th, scope="ticker")
        elif ev.get("cohort_history"):
            out["history"] = dict(ev["cohort_history"], scope="cohort")
        return out
    return {"expression": "underlying", "option_mark": None}


# -------------------------------------------------------------- ledger


def _read_ledger():
    events = []
    if os.path.exists(LEDGER_PATH):
        with open(LEDGER_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except Exception:
                        pass
    return events


def _append_ledger(events):
    if not events:
        return
    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    with open(LEDGER_PATH, "a", encoding="utf-8") as f:
        f.writelines(
            json.dumps(e, separators=(",", ":"), ensure_ascii=False) + "\n" for e in events
        )


def _issue_day(e):
    """Issue date (YYYY-MM-DD) of an 'issued' ledger event."""
    d = (e.get("as_of") or "")[:10]
    return d or str(e.get("id") or "").rsplit(":", 1)[-1][:10]


def _etype(idea):
    return (idea.get("evidence") or {}).get("etype") or idea.get("etype")


def _event_date(idea):
    return (idea.get("construct") or {}).get("event_date") or (idea.get("entry") or {}).get(
        "event_date"
    )


def _directional(idea):
    return idea.get("direction") in ("bullish", "bearish")


def _family_key(idea):
    fam = idea.get("family")
    return f"earnings:{_etype(idea)}" if fam == "earnings" else fam


def _event_key(idea, fallback):
    """One earnings idea per event: (type, ticker, direction, report date)."""
    return (
        _etype(idea),
        idea.get("ticker"),
        idea.get("direction"),
        _event_date(idea) or fallback,
    )


def _pos_key(idea):
    return (idea.get("family"), idea.get("ticker"), idea.get("direction"))


def idea_id(idea, day):
    """Ledger id. Earnings ideas get one id per EVENT, not per day the
    card is shown (a drift idea used to be re-issued every day until the
    print). Other families keep the per-day id; the open-window rule
    (_dedup) keeps repeats from counting twice."""
    if idea.get("family") == "earnings" and _event_date(idea):
        return f"earnings:{_etype(idea)}:{idea['ticker']}:{idea['direction']}:{_event_date(idea)}"
    return f"{idea['family']}:{idea['ticker']}:{idea['direction']}:{day}"


def _dedup(issued):
    """Collapse 'issued' events into independent paper positions.
    Returns (kept, n_directional_duplicates):
      - earnings ideas: one per event (the first issue wins);
      - other directional ideas: one per (family, ticker, direction) per
        open 5-session window — a repeat issued before the first
        position's window closes would re-count the same move;
      - other non-directional ideas pass through.
    Works on legacy per-day ids too, so the published record is
    de-duplicated retroactively without rewriting the ledger."""
    kept, dup = [], 0
    seen_events, opened = set(), {}
    for e in sorted(issued, key=lambda x: (_issue_day(x), x.get("as_of") or "")):
        it = e.get("idea") or {}
        if it.get("family") == "earnings":
            k = _event_key(it, e.get("id"))
            if k in seen_events:
                dup += 1 if _directional(it) else 0
                continue
            seen_events.add(k)
            kept.append(e)
            continue
        if not _directional(it):
            kept.append(e)
            continue
        k, d1 = _pos_key(it), _issue_day(e)
        if k in opened and _overlaps(opened[k], d1):
            dup += 1
            continue
        opened[k] = d1
        kept.append(e)
    return kept, dup


def _open_index(kept):
    """(latest kept issue day per position key, set of earnings events)."""
    last, events = {}, set()
    for e in kept:
        it = e.get("idea") or {}
        if it.get("family") == "earnings":
            events.add(_event_key(it, e.get("id")))
        elif _directional(it):
            k, d = _pos_key(it), _issue_day(e)
            if d > last.get(k, ""):
                last[k] = d
    return last, events


def _is_shadow(e):
    return (e.get("idea") or {}).get("status") == "SHADOW"


def freeze_ideas(display, now):
    """Append candidates to the append-only ledger as paper positions.
    Existing entries are never modified (spec sec 10-11). Skipped:
    the same id again (idempotent re-runs), an earnings EVENT already
    on file, and a ticker+direction whose earlier position is still
    inside its 5-session window. Displayed tiers de-duplicate among
    themselves; SHADOW (sub-floor, paper-only) ideas against everything,
    so the calibration pool never double-counts a move."""
    events = _read_ledger()
    issued = [e for e in events if e.get("ev") == "issued"]
    seen = {e.get("id") for e in issued}
    day = now.date().isoformat()
    disp_last, disp_events = _open_index(_dedup([e for e in issued if not _is_shadow(e)])[0])
    all_last, all_events = _open_index(_dedup(issued)[0])
    new = []
    for it in display:
        iid = idea_id(it, day)
        if iid in seen:
            continue
        shadow = it.get("status") == "SHADOW"
        last, evs = (all_last, all_events) if shadow else (disp_last, disp_events)
        if it.get("family") == "earnings":
            k = _event_key(it, iid)
            if k in evs:
                continue
            for s in (evs, all_events):
                s.add(k)
        elif _directional(it):
            k = _pos_key(it)
            if k in last and _overlaps(last[k], day):
                continue
            for m in (last, all_last):
                m[k] = day
        seen.add(iid)
        new.append(
            {
                "ev": "issued",
                "id": iid,
                "as_of": _iso(now),
                "data_cutoff": _iso(now),
                "engine_version": ENGINE_VERSION,
                "model_version": MODEL_VERSION,
                "idea": it,
            }
        )
    _append_ledger(new)
    return events + new


def _grade_counts(g, issued_event):
    """Earnings grades from before GRADE_VERSION 2 measured from the
    issue-day close (before the print) and are superseded."""
    it = (issued_event or {}).get("idea") or {}
    if it.get("family") == "earnings":
        return (g.get("gv") or 1) >= GRADE_VERSION
    return True


def _canonical_ids(issued_events):
    disp = _dedup([e for e in issued_events if not _is_shadow(e)])[0]
    allp = _dedup(issued_events)[0]
    return {e.get("id") for e in disp} | {e.get("id") for e in allp}


def grade_ledger(events, now, do_grade=True):
    """Grade matured, de-duplicated positions (append-only 'graded' lines):
      - flow / momentum: +GRADE_HOLD_SESSIONS direction-signed excess vs
        SPY from the issue-day close (documented conservative fill);
      - earnings directional types: the earnings loop's own grader
        (earnings_ideas._grade) — post_report_drift from the REACTION
        close, momentum_into_print from the pre-report close — so the
        desk's record and the gate share one definition.
    Neutral (vol) ideas are graded |move| vs implied by the earnings
    loop; they are not force-fit here."""
    issued = {e["id"]: e for e in events if e.get("ev") == "issued"}
    done = {
        e["id"]
        for e in events
        if e.get("ev") == "graded" and _grade_counts(e, issued.get(e.get("id")))
    }
    stock, ern = [], []
    for iid in _canonical_ids(list(issued.values())):
        if iid in done or iid not in issued:
            continue
        e = issued[iid]
        it = e.get("idea") or {}
        if not _directional(it):
            continue
        if it.get("family") == "earnings":
            try:
                d_ev = date.fromisoformat(str(_event_date(it))[:10])
            except ValueError:
                continue
            age = (now.date() - d_ev).days
            # +5 sessions after the reaction needs ~9 calendar days; the
            # loop's price window ends 28 days after the report
            if 9 <= age <= 150:
                ern.append((iid, e, d_ev))
            continue
        try:
            d0 = date.fromisoformat(_issue_day(e))
        except ValueError:
            continue
        # +5 sessions needs ~9 calendar days for weekends/holidays
        if (now.date() - d0).days >= 9:
            stock.append((iid, e, d0, it["direction"]))
    if not do_grade or not (stock or ern):
        return events, 0
    new = _grade_stock(stock, now) + _grade_earnings(ern, now)
    _append_ledger(new)
    return events + new, len(new)


def _grade_stock(todo, now):
    if not todo:
        return []
    import yfinance as yf

    tickers = sorted({e["idea"]["ticker"] for _, e, _, _ in todo} | {"SPY"})
    start = (min(d for _, _, d, _ in todo) - timedelta(days=4)).isoformat()
    try:
        px = yf.download(tickers, start=start, progress=False, auto_adjust=True)["Close"]
    except Exception:
        return []
    new = []
    for iid, e, d0, direction in todo:
        tk = e["idea"]["ticker"]
        try:
            col = px[tk] if tk in px else None
            spy = px["SPY"]
            col = col.dropna()
            spy = spy.dropna()
            base_idx = col.index.searchsorted(str(d0))
            if base_idx >= len(col):
                continue
            # entry basis = close of issue day (documented realistic-fill
            # convention; no intraday fill assumed)
            end_idx = base_idx + GRADE_HOLD_SESSIONS
            if end_idx >= len(col):
                continue  # not matured yet
            ret = 100 * (float(col.iloc[end_idx]) / float(col.iloc[base_idx]) - 1)
            sb = spy.index.searchsorted(str(d0))
            se = sb + GRADE_HOLD_SESSIONS
            if se >= len(spy):
                continue
            sret = 100 * (float(spy.iloc[se]) / float(spy.iloc[sb]) - 1)
            exc = ret - sret
            y = exc if direction == "bullish" else -exc
            new.append(
                {
                    "ev": "graded",
                    "id": iid,
                    "graded_at": _iso(now),
                    "gv": GRADE_VERSION,
                    "basis": "issue_close_excess_vs_spy",
                    "hold_sessions": GRADE_HOLD_SESSIONS,
                    "ret": round(ret, 2),
                    "excess": round(exc, 2),
                    "y": round(y, 2),
                }
            )
        except Exception:
            continue
    return new


def _grade_earnings(todo, now, grader=None):
    """Newest events first, capped per run (one price-history call each);
    ungradeable-yet events simply retry next run."""
    grader = grader or earnings_loop_grade
    new = []
    for iid, e, d_ev in sorted(todo, key=lambda x: x[2], reverse=True)[:EARNINGS_GRADE_PER_RUN]:
        it = e["idea"]
        c = it.get("construct") or {}
        probe = {
            "t": it["ticker"],
            "date": d_ev.isoformat(),
            "type": _etype(it),
            "bias": "bull" if it["direction"] == "bullish" else "bear",
            "session": c.get("session"),
            "implied": c.get("implied_move"),
        }
        try:
            _res, mv = grader(probe)
        except Exception:
            continue
        if mv is None:
            continue
        new.append(
            {
                "ev": "graded",
                "id": iid,
                "graded_at": _iso(now),
                "gv": GRADE_VERSION,
                "basis": "earnings_loop_grader",
                "y": round(mv, 2),
            }
        )
    return new


def _stats(pairs):
    """Published stats for (cluster_date, y) pairs, gated at MIN_N; the
    95% CI is clustered by date (CR1, the earnings gate's estimator)."""
    ys = [y for _, y in pairs]
    if len(ys) < MIN_N:
        return {"status": "accruing", "n": len(ys)}
    ci = cluster_ci95(pairs)
    return {
        "status": "active",
        "n": len(ys),
        "hit": round(100 * sum(1 for y in ys if y > 0) / len(ys)),
        "avg": round(mean(ys), 2),
        "med": round(median(ys), 2),
        "ci95": ci["ci95"],
        "clusters": ci["clusters"],
    }


def performance(events):
    """Published stats from the ledger, de-duplicated into independent
    positions and gated at MIN_N, each family on its own target (never
    blended). Plus the Research Rank calibration table, whose bands all
    fill because sub-floor flow is kept as paper-only SHADOW ideas."""
    issued = {e["id"]: e for e in events if e.get("ev") == "issued"}
    grade = {}
    for e in events:
        if e.get("ev") == "graded" and e.get("id") in issued and _grade_counts(e, issued[e["id"]]):
            grade[e["id"]] = e  # latest valid grade wins
    kept_disp, dup_disp = _dedup([e for e in issued.values() if not _is_shadow(e)])
    kept_all, _ = _dedup(list(issued.values()))

    rows, neutral = [], 0
    for e in kept_disp:
        it = e.get("idea") or {}
        if not _directional(it):
            neutral += 1
            continue
        earn = it.get("family") == "earnings"
        rows.append(
            {
                "fam": _family_key(it),
                "y": (grade.get(e["id"]) or {}).get("y"),
                # cluster = the date the position's move starts
                "cluster": (_event_date(it) if earn else None) or _issue_day(e),
            }
        )
    graded = [r for r in rows if r["y"] is not None]
    out = {
        "min_n": MIN_N,
        "hold_sessions": GRADE_HOLD_SESSIONS,
        "tracked": len(rows),
        "graded": len(graded),
        "duplicates_excluded": dup_disp,
        "neutral_frozen": neutral,
        "shadow_tracked": sum(1 for e in kept_all if _is_shadow(e)),
    }
    by_family = {}
    for fam in sorted({r["fam"] for r in rows}, key=str):
        fr = [r for r in rows if r["fam"] == fam]
        by_family[fam] = dict(
            _stats([(r["cluster"], r["y"]) for r in fr if r["y"] is not None]),
            target=TARGETS.get(fam, TARGET_EXCESS),
            tracked=len(fr),
        )
    out["by_family"] = by_family
    overall = _stats(
        [(r["cluster"], r["y"]) for r in graded if TARGETS.get(r["fam"]) == TARGET_EXCESS]
    )
    if overall["status"] == "active":
        out["overall"] = dict(overall, target=TARGET_EXCESS)
    if not any(v.get("status") == "active" for v in by_family.values()):
        out["status"] = "accruing"

    flow = [
        e
        for e in kept_all
        if (e.get("idea") or {}).get("family") == "flow"
        and (e.get("idea") or {}).get("alpha_score") is not None
    ]
    cal = {}
    for lo, hi, label in CAL_BANDS:
        band = [e for e in flow if lo <= e["idea"]["alpha_score"] < hi]
        pairs = [
            (_issue_day(e), grade[e["id"]]["y"])
            for e in band
            if grade.get(e["id"], {}).get("y") is not None
        ]
        cal[label] = dict(_stats(pairs), tracked=len(band))
    out["score_calibration"] = cal
    return out


def _ledgers(perf, gates=None, events=None):
    """Three separate scoreboards (reviewer: do not blend targets).
    Forecast = earnings-vol implied-vs-realized; Directional = the
    paper idea ledger, each family on its own target; Executable =
    actual trade-qualified record (empty is a statement, not an
    absence)."""
    ev = _load(R("docs", "reports", "earnings_vol.json"), {})
    fml = _load(R("docs", "reports", "fair_move_lab.json"), {})
    vr = (ev.get("types") or {}).get("vol_rich") or {}
    lab = ((fml.get("standings") or {}).get("overall") or {}).get("v1_ticker_median") or {}
    forecast = {
        "target": "market implied move - realized move",
        "events": vr.get("n"),
        "nights": (vr.get("date_cluster_bootstrap") or {}).get("nights"),
        "implied_gt_realized_pct": vr.get("win"),
        "avg_vol_edge_pp": vr.get("ev_vol_pts"),
        "night_ci": (vr.get("date_cluster_bootstrap") or {}).get("ci95"),
        "fair_move_model": "v1",
        "fair_move_mae": lab.get("mae"),
        "fair_move_bias": lab.get("bias"),
        "edge_sign_pct": lab.get("edge_sign_pct"),
    }
    directional = {
        "target": (
            f"flow & momentum: {TARGET_EXCESS}; earnings types: the earnings "
            "gate's own definition (see each row)"
        ),
        "tracked": perf.get("tracked"),
        "graded": perf.get("graded"),
        "duplicates_excluded": perf.get("duplicates_excluded"),
        "neutral_frozen": perf.get("neutral_frozen"),
        "overall": perf.get("overall"),
        "by_family": perf.get("by_family"),
        "status": perf.get("status"),
    }
    types = ((gates or {}).get("earnings") or {}).get("types") or {}
    tq = sorted(t for t, g in types.items() if g.get("verdict") == "trade_qualified")
    live = _dedup(
        [
            e
            for e in events or []
            if e.get("ev") == "issued" and (e.get("idea") or {}).get("status") == "TRADE_QUALIFIED"
        ]
    )[0]
    executable = {
        "trade_qualified_strategies": len(tq),
        "strategies": tq,
        "frozen_live_trades": len(live),
        "statement": (
            "TickerDesk has not yet qualified an executable strategy."
            if not tq
            else "Executable strategies qualified: " + ", ".join(tq).replace("_", " ") + "."
        ),
    }
    return {"forecast": forecast, "directional": directional, "executable": executable}


# ------------------------------------------------------- what changed


def what_changed(prev, ideas_all):
    if not prev:
        return []
    old = {
        (i.get("family"), i.get("ticker"), i.get("direction")): i
        for i in (prev.get("top_ideas") or []) + (prev.get("watch") or [])
    }
    changes = []
    for i in ideas_all:
        k = (i["family"], i["ticker"], i["direction"])
        o = old.pop(k, None)
        if o is None:
            changes.append({"t": i["ticker"], "change": "new", "to": i["status"]})
        elif o.get("status") != i["status"]:
            changes.append(
                {"t": i["ticker"], "change": "status", "from": o.get("status"), "to": i["status"]}
            )
        elif (
            o.get("alpha_score") is not None
            and i.get("alpha_score") is not None
            and abs(o["alpha_score"] - i["alpha_score"]) >= 5
        ):
            changes.append(
                {
                    "t": i["ticker"],
                    "change": "score",
                    "from": o["alpha_score"],
                    "to": i["alpha_score"],
                }
            )
    for (fam, tk, d), o in old.items():
        changes.append({"t": tk, "change": "dropped", "from": o.get("status")})
    return changes[:12]


# ---------------------------------------------------------------- run


def _et_today(now):
    return now.astimezone(timezone(timedelta(hours=-4))).date().isoformat()


def _tier(c, gates):
    """Family gate -> (status, why). Tiers, visually unmistakable in the
    UI: TRADE_QUALIFIED (executable expression validated), QUALIFIED
    (directional: stock-level paper edge), SIGNAL_QUALIFIED (forecast
    edge, no trade), WATCH (research watchlist), EXPERIMENTAL (family
    degraded / rehabilitating on the paper ledger)."""
    if c["family"] == "flow":
        g = gates["flow"]
        return ("QUALIFIED" if g["verdict"] == "qualified" else "WATCH"), g["why"]
    if c["family"] == "earnings":
        g = gates["earnings"]["types"].get(c.get("etype")) or {
            "verdict": "watch",
            "why": "accruing",
        }
        status = {
            "qualified": "QUALIFIED",
            "trade_qualified": "TRADE_QUALIFIED",
            "signal_qualified": "SIGNAL_QUALIFIED",
            "degraded": "EXPERIMENTAL",
        }.get(g["verdict"], "WATCH")
        return status, g["why"]
    g = gates["momentum"]
    status = {"qualified": "QUALIFIED", "degraded": "EXPERIMENTAL"}.get(g["verdict"], "WATCH")
    return status, g["why"]


def _item(c, status, gate_why, sc, now):
    item = {
        "family": c["family"],
        "ticker": c["ticker"],
        "direction": c["direction"],
        "status": status,
        "alpha_score": sc,
        "gate": gate_why,
        "catalyst": c.get("catalyst"),
        "construct": construct(c),
        "as_of": _iso(now),
    }
    if c["family"] == "flow":
        item["evidence"] = {
            "n_prints": c["n_prints"],
            "total_premium": c["total_premium"],
            "feats": c["feats"],
            "anchor_score": c["anchor"].get("trade_score"),
            "tags": c["anchor"].get("tags"),
            "sector": c["anchor"].get("sector"),
        }
    elif c["family"] == "earnings":
        i = c["idea"]
        item["evidence"] = {
            "thesis": i.get("thesis"),
            "cited": i.get("evidence"),
            "etype": c.get("etype"),
            "grade": i.get("grade"),
            "mcap_b": i.get("mcap_b"),
        }
    else:
        item["evidence"] = dict(c.get("row") or {})
    # How the paper position is entered and graded (see grade_ledger).
    if c["direction"] in ("bullish", "bearish"):
        if c["family"] == "earnings":
            i = c["idea"]
            item["entry"] = {
                "rule": (
                    "reaction_close"
                    if c.get("etype") == "post_report_drift"
                    else "pre_report_close"
                ),
                "event_date": i.get("date"),
                "session": i.get("session"),
            }
        else:
            item["entry"] = {"rule": "issue_close"}
    return item


def run(dry=False, do_grade=True):
    for k in REJECT:
        REJECT[k] = 0
    for k in SKIPPED:
        SKIPPED[k] = 0
    now = _now()
    regimes = _regime_map()
    regime = _regime_today(regimes, now)  # latest label, for display
    # Filters and features use the PRIOR session's label: today's label is
    # only written after the close, so it is unknowable during the session.
    regime_prior = prior_regime(regimes, _et_today(now))
    conflicts = _regime_conflicts()
    gates = family_gates()
    score, score_ok = _score_fn()

    candidates = (
        _flow_candidates(regimes, now) + _earnings_candidates(now) + _momentum_candidates(now)
    )

    n_universe = len(candidates)
    display, shadow = [], []
    for c in candidates:
        sc = score(c["feats"]) if (c["family"] == "flow" and score_ok) else None
        reason = hard_filter(c, regime_prior, sc, now, conflicts)
        if reason:
            REJECT[reason] = REJECT.get(reason, 0) + 1
            if reason == "LOW_ALPHA":
                # Paper-only shadow: never displayed, frozen so the
                # Research Rank's low band can be graded (calibration).
                shadow.append(_item(c, "SHADOW", "below the display floor", sc, now))
            continue
        status, gate_why = _tier(c, gates)
        display.append(_item(c, status, gate_why, sc, now))

    display.sort(key=lambda x: (x["alpha_score"] is None, -(x["alpha_score"] or 0)))
    TOP_STATUSES = ("TRADE_QUALIFIED", "QUALIFIED", "SIGNAL_QUALIFIED")
    top_all = [i for i in display if i["status"] in TOP_STATUSES]
    top = top_all[:MAX_IDEAS]
    watch_all = [i for i in display if i["status"] == "WATCH"]
    watch = watch_all[:MAX_WATCH]
    exp_all = [i for i in display if i["status"] == "EXPERIMENTAL"]
    per_fam = {}
    experimental = []
    for i in exp_all:  # display cap per family, so one family can't hide another
        per_fam[i["family"]] = per_fam.get(i["family"], 0) + 1
        if per_fam[i["family"]] <= MAX_WATCH:
            experimental.append(i)

    # Desk counts — evidence-level tallies (reviewer: never let a
    # signal-qualified observation masquerade as a qualified trade, nor a
    # directional paper edge as an executable trade).
    n_trade_q = sum(1 for i in top if i["status"] == "TRADE_QUALIFIED")
    n_signal_q = sum(1 for i in top if i["status"] == "SIGNAL_QUALIFIED")
    n_dir_q = sum(1 for i in top if i["status"] == "QUALIFIED")
    desk_counts = {
        "trade_qualified": n_trade_q,
        "directional_qualified": n_dir_q,
        "signal_qualified": n_signal_q,
        # distinct validated edges behind the signal-qualified setups
        "signal_qualified_edges": len(
            {
                (i.get("evidence") or {}).get("etype")
                for i in top
                if i["status"] == "SIGNAL_QUALIFIED"
            }
        ),
        "research_watch": len(watch),
        "experimental": len(experimental),
    }
    # Multiple independent pipelines, not one funnel — every verdict read
    # from the gates, never typed in.
    n_flow_cand = sum(1 for c in candidates if c["family"] == "flow")
    n_ern_cand = sum(1 for c in candidates if c["family"] == "earnings")
    n_mom_cand = sum(1 for c in candidates if c["family"] == "momentum")
    fg = gates["flow"]
    pipelines = [
        {
            "name": "Earnings Volatility",
            "line": f"{n_ern_cand} scanned → {n_signal_q} signal-qualified"
            f" → {n_trade_q} trade-qualified",
        },
        {
            "name": "Options Flow",
            "line": f"{n_flow_cand} candidates → "
            f"{sum(1 for i in watch_all if i['family'] == 'flow')} watch → "
            f"{sum(1 for i in top if i['family'] == 'flow')} qualified ("
            + (f"champion {fg.get('champion')}" if fg.get("champion") else "no champion")
            + ")",
        },
        {
            "name": gates["momentum"].get("label") or "Momentum",
            "line": f"{n_mom_cand} candidates → gate "
            f"{str(gates['momentum'].get('verdict')).upper()}",
        },
    ]
    drift = (gates["earnings"]["types"] or {}).get("post_report_drift")
    if drift:
        pipelines.append(
            {
                "name": "Post-Report Drift",
                "line": f"{(drift.get('evidence') or {}).get('n')} graded → gate "
                f"{str(drift.get('verdict')).upper()}",
            }
        )
    funnel = {  # retained for compatibility; UI now prefers desk_counts
        "universe_events": n_universe,
        "research_candidates": len(display),
        "watch_setups": len(watch),
        "experimental": len(experimental),
        "qualified": len(top),
    }

    prev = _load(OUT_PATH, None)
    changes = what_changed(prev, top + watch + experimental)
    # Deterministic headline summaries (templates over facts — the LLM
    # is never in this path).
    headlines = []
    vol_tops = [i for i in top if (i.get("construct") or {}).get("vol_edge_pp") is not None]
    if vol_tops:
        rich = max(vol_tops, key=lambda i: i["construct"]["vol_edge_pp"])
        c = rich["construct"]
        headlines.append(
            {
                "h": f"{rich['ticker']} is today's richest earnings-vol setup",
                "d": f"Market-implied move is {c['implied_move']}% versus "
                f"TickerDesk Fair Move of {c['fair_move']}%, a "
                f"+{c['vol_edge_pp']}pp discrepancy.",
            }
        )
    if n_signal_q and not n_trade_q:
        headlines.append(
            {
                "h": "Vol-rich remains signal-qualified, not trade-qualified",
                "d": "The expression gate still has no champion; "
                "defined-risk structures remain rejected.",
            }
        )
    if any(i["family"] == "flow" for i in watch):
        headlines.append(
            {
                "h": "Options flow remains research-only",
                "d": f"{sum(1 for i in watch if i['family'] == 'flow')} "
                "setups entered Watch; the family has no validated "
                "walk-forward champion.",
            }
        )

    # Every tier member is frozen (display caps are for the page only),
    # plus the sub-floor shadow — so the record covers the whole family.
    events = freeze_ideas(top_all + watch_all + exp_all + shadow, now)
    events, n_graded = grade_ledger(events, now, do_grade=do_grade)
    perf = performance(events)

    note = None
    if desk_counts["trade_qualified"] == 0:
        parts = ["No executable trade clears TickerDesk's production gate today."]
        if n_signal_q:
            parts.append(
                f"{n_signal_q} earnings-volatility setups belong to a "
                "signal-qualified forecasting edge, but no option structure "
                "has demonstrated sufficient after-cost expectancy."
            )
        if n_dir_q:
            parts.append(
                f"{n_dir_q} directional setups qualified on their family's "
                "own stock-level record — a paper edge, not an executable "
                "option trade."
            )
        if not (n_signal_q or n_dir_q):
            parts.append(
                "Candidates below are tracked paper-forward; families "
                "qualify automatically when their own record clears "
                "their gates."
            )
        note = " ".join(parts)

    out = {
        "generated": _iso(now),
        "engine_version": ENGINE_VERSION,
        "model_version": MODEL_VERSION,
        "data_cutoff": _iso(now),
        "market_regime": regime,
        "regime_for_filters": regime_prior,
        "score_available": score_ok,
        "gates": gates,
        "top_ideas": top,
        "watch": watch,
        "experimental": experimental,
        "funnel": funnel,
        "desk_counts": desk_counts,
        "pipelines": pipelines,
        "headlines": headlines,
        "ledgers": _ledgers(perf, gates, events),
        "abstention": (desk_counts["trade_qualified"] == 0),
        "abstention_note": note,
        # candidate-level only; print-level skips are reported apart
        "rejections": {k: v for k, v in REJECT.items() if v},
        "skipped_prints": {k: v for k, v in SKIPPED.items() if v},
        "what_changed": changes,
        "performance": perf,
        "honesty": (
            "Every displayed statistic is computed from "
            "point-in-time data or a disclosed deterministic "
            "model; no values are invented by the LLM. "
            "The Research Rank is a percentile of a model's expected "
            "return and its walk-forward skill is near zero — a sort "
            "order, not a forecast. Fair Move is a disclosed forecast, "
            "not a measurement."
        ),
    }
    out = json_sanitize(out)
    if not dry:
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1, ensure_ascii=False)
    print(
        "regime:",
        regime,
        "(filters use",
        regime_prior + ")",
        "| top:",
        len(top),
        "| watch:",
        len(watch),
        "| rejections:",
        out["rejections"],
        "| ledger graded now:",
        n_graded,
        "| tracked:",
        perf.get("tracked"),
    )
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-grade", action="store_true")
    a = ap.parse_args()
    run(dry=a.dry_run, do_grade=not a.no_grade)
