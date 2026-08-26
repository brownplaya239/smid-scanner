"""
trade_desk.py — AI Trade Desk engine: candidate generation, empirical
Alpha Score, qualification gates, immutable idea ledger, forward grading.

ALPHA IS PARAMOUNT. The pipeline is deterministic end-to-end; nothing
here is estimated by an LLM, and no strategy family surfaces an
actionable Top Idea unless its own measured, walk-forward-validated
record says it has edge. As of first ship, NO family qualifies:

  flow      trade_desk_validation.json -> "no_validated_edge"
            (walk-forward IC negative in 3 of 4 windows on 27.5k
             graded signals; every fixed sub-family flips sign across
             fortnights)
  earnings  earnings_ideas.json by_type: post_report_drift n=82
            EV -0.9 (degraded); other types accruing (< 30 graded)
  momentum  scan_outcomes.json: QM PF 0.86 / Stockbee PF 0.60 (degraded)

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
                         (earnings_ideas) + momentum (swing summary)
  enrich()               features at flag time (shared with the
                         validation module — lockstep by import)
  hard_filters()         liquidity / premium / staleness / regime
                         conflict — every rejection carries a reason
  rank()                 Alpha Score v1 = train-percentile of expected
                         +5d direction-signed excess (production model
                         from trade_desk_validation.json)
  family gates           measured track record decides IDEA vs WATCH
  construct()            reference structure + risk framing (no invented
                         marks: fields the pipeline can't know are null)
  ledger + grading       data/trade_desk_log.jsonl (append-only) ->
                         +5-session direction-signed excess vs SPY ->
                         performance + Alpha-Score calibration, gated
                         at MIN_N

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
from datetime import datetime, timezone, timedelta

from statistics import mean, median

from trade_desk_validation import (features_at_flag, _regime_map,
                                   MODEL_VERSION)

_BASE = os.path.dirname(os.path.abspath(__file__))
R = lambda *p: os.path.join(_BASE, *p)

LEDGER_PATH = R("data", "trade_desk_log.jsonl")
OUT_PATH = R("docs", "reports", "trade_desk.json")
VALIDATION_PATH = R("docs", "reports", "trade_desk_validation.json")
RESEARCH_PATH = R("docs", "reports", "trade_desk_research.json")

ENGINE_VERSION = "trade_desk_v1"
MIN_N = 30              # house gate: no published stat below this
GRADE_HOLD_SESSIONS = 5
MAX_IDEAS = 5           # hard ceiling on Top Ideas (spec: 0-5, never fill)
MAX_WATCH = 8
MIN_PREMIUM = 250_000   # flow candidates below this never rank
STALE_MIN = 90          # flow signal older than this (minutes) is stale
SCORE_FLOOR = 70        # candidates below never reach WATCH display

REJECT = {  # observability vocabulary (spec sec 54)
    "LOW_ALPHA": 0, "BAD_LIQUIDITY": 0, "LOW_PREMIUM": 0,
    "REGIME_CONFLICT": 0, "STRATEGY_DEGRADED": 0, "STALE_DATA": 0,
    "INSUFFICIENT_DATA": 0, "NOT_DIRECTIONAL": 0,
}


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


# ------------------------------------------------------------ families
# Each family's gate reads its own MEASURED record. Verdicts:
#   qualified  -> ideas may surface as Top Ideas
#   watch      -> ideas display in the not-yet-qualified rail only
#   degraded   -> family generates candidates for the ledger but they
#                 are rejected from display (STRATEGY_DEGRADED)

def family_gates():
    gates = {}

    # Flow gate authority: the champion/challenger registry from the
    # setup-level research (purged walk-forward, extreme-tail precision,
    # cluster-bootstrap CI). A challenger promotes to champion ONLY on
    # its predefined OOS criteria; no champion -> the family abstains.
    reg = (_load(RESEARCH_PATH, {}) or {}).get("registry") or {}
    champ = reg.get("champion") or {}
    if champ.get("name"):
        gates["flow"] = {"verdict": "qualified",
                         "champion": champ.get("name"),
                         "tail_pct": champ.get("tail_pct"),
                         "why": "champion model holds validated "
                                "extreme-tail edge (see research)",
                         "evidence": ((reg.get("challengers") or {})
                                      .get(champ["name"], {})
                                      .get("verdict") or {})
                                     .get("evidence")}
    else:
        gates["flow"] = {"verdict": "watch",
                         "why": "no champion: setup-level walk-forward "
                                "found no tail that clears the "
                                "promotion criteria (positive pooled "
                                "avg+median, bootstrap CI low > 0, "
                                "positive holdout)",
                         "evidence": {"status": "no_champion"}}

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
        unit = "vol-pts" if t.startswith("vol_") else "pp"
        if st.get("status") != "active" or st.get("n", 0) < MIN_N:
            ern[t] = {"verdict": "watch", "why": "accruing",
                      "evidence": st}
        elif (st.get("ev") or 0) > 0:
            if t.startswith("vol_"):
                vt = vol_types.get(t) or {}
                boot = vt.get("date_cluster_bootstrap") or {}
                if (bt.get("types", {}).get(t, {})
                        .get("trade_qualified")):
                    verdict, note = "trade_qualified", \
                        "NBBO execution study cleared VOL_RICH_EXEC_V1"
                elif vt.get("signal_qualified"):
                    verdict = "signal_qualified"
                    ci = boot.get("ci95") or []
                    note = (f"night-cluster CI {ci} on "
                            f"{boot.get('nights')} nights — trade "
                            "expression pending executable validation")
                else:
                    verdict, note = "watch", \
                        "signal CI not yet excluding zero at night level"
                ern[t] = {"verdict": verdict,
                          "why": (f"measured EV {st['ev']:+.2f} {unit} · "
                                  f"{st.get('win_rate')}% win · "
                                  f"n={st['n']} · {note}"),
                          "evidence": st, "vol": vt and {
                              "bootstrap": boot,
                              "move_ratio": vt.get("move_ratio"),
                              "tails": vt.get("tails")} or None}
            else:
                ern[t] = {"verdict": "qualified",
                          "why": (f"measured EV {st['ev']:+.2f} {unit} · "
                                  f"{st.get('win_rate')}% win · "
                                  f"n={st['n']}"),
                          "evidence": st}
        else:
            ern[t] = {"verdict": "degraded",
                      "why": (f"measured EV {st['ev']:+.2f} {unit} "
                              f"on n={st['n']}"),
                      "evidence": st}
    gates["earnings"] = {"verdict": "per_type", "types": ern}

    so = _load(R("docs", "reports", "scan_outcomes.json"), {})
    overall = (so.get("overall") or {})
    if overall.get("status") == "active" and overall.get("n", 0) >= MIN_N:
        verdict = "qualified" if (overall.get("ev") or 0) > 0 else "degraded"
        why = (f"measured EV {overall.get('ev'):+.2f} "
               f"PF {overall.get('profit_factor')} on n={overall.get('n')}")
    else:
        verdict, why = "watch", "accruing"
    gates["momentum"] = {"verdict": verdict, "why": why,
                         "evidence": overall}
    return gates


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
        total = sum(adj.get(f + ":" + str(val), 0.0)
                    for f, val in feats.items())
        pred = base + max(-6.0, min(6.0, total))
        # anchors = expected-excess values at percentiles 0,5,...,100
        lo = 0
        for i, a in enumerate(anchors):
            if pred >= a:
                lo = i
        if lo >= len(anchors) - 1:
            return 100.0
        a0, a1 = anchors[lo], anchors[lo + 1]
        frac = (pred - a0) / (a1 - a0) if a1 > a0 else 0.0
        return round((lo + frac) * 5.0, 1)
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
                direction = {"call": "bullish",
                             "put": "bearish"}.get(s.get("type"))
        if direction not in ("bullish", "bearish") \
                or s.get("flow_side") in ("put_seller", "call_seller"):
            REJECT["NOT_DIRECTIONAL"] += 1
            continue
        s = dict(s, direction=direction)
        key = (s.get("ticker"), direction)
        clusters.setdefault(key, []).append(s)
    out = []
    for (tk, direction), grp in clusters.items():
        grp.sort(key=lambda x: x.get("premium") or 0, reverse=True)
        anchor = grp[0]
        feats = features_at_flag(anchor, regimes)
        out.append({
            "family": "flow", "ticker": tk, "direction": direction,
            "anchor": anchor, "n_prints": len(grp),
            "total_premium": sum(x.get("premium") or 0 for x in grp),
            "feats": feats,
            "catalyst": ("earnings" if feats["ern"] == "yes" else "flow"),
        })
    return out


def _fresh(doc, now, max_h):
    """Source-file freshness guard — candidates from a stale upstream
    JSON must never surface (STALE_DATA), e.g. after a weekend or a
    paused pipeline."""
    try:
        gen = datetime.fromisoformat(
            (doc.get("generated") or doc.get("updated") or "")
            .replace("Z", "+00:00"))
        if gen.tzinfo is None:
            gen = gen.replace(tzinfo=timezone.utc)
        return (now - gen).total_seconds() <= max_h * 3600
    except (ValueError, TypeError):
        return False


def _earnings_candidates(now):
    d = _load(R("docs", "reports", "earnings_ideas.json"), {})
    if not _fresh(d, now, 48):
        REJECT["STALE_DATA"] += len(d.get("ideas") or [])
        return []
    # n_reports (analogue count for Fair Move) lives in earnings_edge
    edge = _load(R("docs", "reports", "earnings_edge.json"), {})
    n_reports = {r.get("t"): r.get("n_reports")
                 for r in edge.get("names") or []}
    out = []
    today = now.astimezone(timezone(timedelta(hours=-4))).date()
    for i in d.get("ideas") or []:
        # Pre-print idea types die once the report is out: a vol_rich
        # premium-sell into a print that already happened is not a
        # trade. post_report_drift is post-print and stays valid.
        if i.get("type") in ("vol_rich", "vol_cheap",
                             "momentum_into_print"):
            try:
                ed = datetime.strptime(i.get("date") or "",
                                       "%Y-%m-%d").date()
            except ValueError:
                REJECT["STALE_DATA"] += 1
                continue
            if ed < today or (ed == today
                              and i.get("session") == "BMO"):
                REJECT["STALE_DATA"] += 1
                continue
        bias = i.get("bias")
        direction = {"bull": "bullish", "bear": "bearish"}.get(bias)
        idea = dict(i)
        if idea.get("n_reports") is None:
            idea["n_reports"] = n_reports.get(i.get("t"))
        out.append({
            "family": "earnings", "ticker": i.get("t"),
            "direction": direction or "neutral",
            "etype": i.get("type"), "idea": idea,
            "catalyst": "earnings",
        })
    return out


def _momentum_candidates(now):
    s = _load(R("docs", "reports", "swing_latest_summary.json"), {})
    runs = s.get("runs") or []
    if not runs or not _fresh(s, now, 48):
        return []
    latest = runs[0] if isinstance(runs, list) else {}
    out = []
    for r in (latest.get("names") or latest.get("rows") or [])[:20]:
        grade = (r.get("grade") or r.get("g") or "")
        if not str(grade).startswith("A"):
            continue
        out.append({"family": "momentum",
                    "ticker": r.get("t") or r.get("ticker"),
                    "direction": "bullish", "row": r,
                    "catalyst": "momentum"})
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
            age = (now.date()
                   - datetime.strptime(latest, "%Y-%m-%d").date()).days
            if age > 5:
                return "unknown"
        except ValueError:
            return "unknown"
    return regimes.get(latest) or "unknown"


def hard_filter(c, regime, score, now):
    """Returns None to keep, or a rejection reason string."""
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
        except (KeyError, ValueError, TypeError):
            return "STALE_DATA"
        # Measured regime conflict: bullish flow-following in risk_off /
        # mixed regimes graded EV -3.08 / -4.52 (uoa_edge by_regime,
        # n=15,445 / 5,872) — hard block, not a style preference.
        if c["direction"] == "bullish" and regime in ("risk_off", "mixed"):
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
            "expression": "underlying" if a.get("liquidity") in (None, "C")
                          else "reference_contract",
            "reference_contract": a.get("contract"),
            "contract_liquidity": a.get("liquidity"),
            "dte": a.get("dte"),
            "flag_premium": a.get("premium"),
            "flag_volume": a.get("volume"),
            "flag_oi": a.get("open_interest"),
            "underlying_at_flag": a.get("underlying_px_at_flag"),
            "option_mark": None,          # not knowable here — honest null
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
        for e in events:
            f.write(json.dumps(e, separators=(",", ":"),
                               ensure_ascii=False) + "\n")


def freeze_ideas(display, now):
    """Append today's displayed candidates (top + watch) to the
    append-only ledger — once per (family, ticker, direction, day).
    Existing entries are never modified (spec sec 10-11)."""
    events = _read_ledger()
    seen = {e.get("id") for e in events if e.get("ev") == "issued"}
    day = now.date().isoformat()
    new = []
    for it in display:
        iid = f"{it['family']}:{it['ticker']}:{it['direction']}:{day}"
        if iid in seen:
            continue
        new.append({"ev": "issued", "id": iid, "as_of": _iso(now),
                    "data_cutoff": _iso(now),
                    "engine_version": ENGINE_VERSION,
                    "model_version": MODEL_VERSION,
                    "idea": it})
    _append_ledger(new)
    return events + new


def grade_ledger(events, now, do_grade=True):
    """Mature issued ideas at +GRADE_HOLD_SESSIONS vs real closes,
    direction-signed excess vs SPY. Neutral-direction ideas (vol theses)
    are graded |move| vs implied by the earnings loop already — here
    they are skipped rather than force-fit."""
    issued = {e["id"]: e for e in events if e.get("ev") == "issued"}
    graded = {e["id"] for e in events if e.get("ev") == "graded"}
    todo = []
    for iid, e in issued.items():
        if iid in graded:
            continue
        d0 = datetime.fromisoformat(e["as_of"]).date()
        # +5 sessions needs ~9 calendar days for weekends/holidays
        if (now.date() - d0).days < 9:
            continue
        direction = e["idea"].get("direction")
        if direction not in ("bullish", "bearish"):
            continue
        todo.append((iid, e, d0, direction))
    if not todo or not do_grade:
        return events, 0

    import yfinance as yf
    tickers = sorted({e["idea"]["ticker"] for _, e, _, _ in todo} | {"SPY"})
    start = (min(d for _, _, d, _ in todo) - timedelta(days=4)).isoformat()
    try:
        px = yf.download(tickers, start=start, progress=False,
                         auto_adjust=True)["Close"]
    except Exception:
        return events, 0
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
            ret = 100 * (float(col.iloc[end_idx]) /
                         float(col.iloc[base_idx]) - 1)
            sb = spy.index.searchsorted(str(d0))
            se = sb + GRADE_HOLD_SESSIONS
            if se >= len(spy):
                continue
            sret = 100 * (float(spy.iloc[se]) / float(spy.iloc[sb]) - 1)
            exc = ret - sret
            y = exc if direction == "bullish" else -exc
            new.append({"ev": "graded", "id": iid,
                        "graded_at": _iso(now),
                        "hold_sessions": GRADE_HOLD_SESSIONS,
                        "ret": round(ret, 2), "excess": round(exc, 2),
                        "y": round(y, 2)})
        except Exception:
            continue
    _append_ledger(new)
    return events + new, len(new)


def performance(events):
    """Published stats from the ledger, gated at MIN_N — plus the
    Alpha-Score calibration table (spec sec 12) regardless of gate so
    the accrual is visible."""
    issued = {e["id"]: e for e in events if e.get("ev") == "issued"}
    rows = []
    for e in events:
        if e.get("ev") != "graded":
            continue
        src = issued.get(e["id"])
        if not src:
            continue
        rows.append({"family": src["idea"].get("family"),
                     "score": src["idea"].get("alpha_score"),
                     "status": src["idea"].get("status"),
                     "y": e.get("y")})
    out = {"tracked": len(issued), "graded": len(rows), "min_n": MIN_N}
    if len(rows) >= MIN_N:
        ys = [r["y"] for r in rows if r["y"] is not None]
        out["overall"] = {
            "n": len(ys),
            "hit": round(100 * sum(1 for y in ys if y > 0) / len(ys)),
            "avg": round(mean(ys), 2), "med": round(median(ys), 2)}
        byfam = {}
        for r in rows:
            byfam.setdefault(r["family"], []).append(r["y"])
        out["by_family"] = {
            f: {"n": len(v),
                "hit": round(100 * sum(1 for y in v if y > 0) / len(v)),
                "avg": round(mean(v), 2)}
            for f, v in byfam.items() if len(v) >= MIN_N}
        bystat = {}
        for r in rows:
            bystat.setdefault(r.get("status") or "?", []).append(r["y"])
        # North-star: forward realized alpha per QUALIFIED setup. Other
        # tiers report too so the tiers can be compared honestly.
        out["by_status"] = {
            s: {"n": len(v),
                "hit": round(100 * sum(1 for y in v if y > 0) / len(v)),
                "avg": round(mean(v), 2), "med": round(median(v), 2)}
            for s, v in bystat.items() if len(v) >= MIN_N}
    else:
        out["status"] = "accruing"
    cal = {}
    for lo, hi, label in ((80, 101, "80-100"), (60, 80, "60-79"),
                          (40, 60, "40-59"), (0, 40, "<40")):
        ys = [r["y"] for r in rows
              if r["score"] is not None and lo <= r["score"] < hi]
        cal[label] = ({"n": len(ys),
                       "hit": round(100 * sum(1 for y in ys if y > 0)
                                    / len(ys)),
                       "avg": round(mean(ys), 2)}
                      if len(ys) >= MIN_N else
                      {"n": len(ys), "status": "accruing"})
    out["score_calibration"] = cal
    return out


def _ledgers(perf):
    """Three separate scoreboards (reviewer: do not blend targets).
    Forecast = earnings-vol implied-vs-realized; Directional = +5D
    direction-signed excess (flow/momentum families in the idea
    ledger); Executable = actual trade-qualified record (empty is a
    statement, not an absence)."""
    ev = _load(R("docs", "reports", "earnings_vol.json"), {})
    fml = _load(R("docs", "reports", "fair_move_lab.json"), {})
    vr = (ev.get("types") or {}).get("vol_rich") or {}
    lab = ((fml.get("standings") or {}).get("overall") or {})         .get("v1_ticker_median") or {}
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
        "target": "+5-session direction-signed excess vs SPY",
        "tracked": perf.get("tracked"), "graded": perf.get("graded"),
        "overall": perf.get("overall"),
        "status": perf.get("status"),
    }
    executable = {
        "trade_qualified_strategies": 0,
        "frozen_live_trades": 0,
        "statement": "TickerDesk has not yet qualified an executable "
                     "strategy.",
    }
    return {"forecast": forecast, "directional": directional,
            "executable": executable}


# ------------------------------------------------------- what changed

def what_changed(prev, ideas_all):
    if not prev:
        return []
    old = {(i.get("family"), i.get("ticker"), i.get("direction")): i
           for i in (prev.get("top_ideas") or []) + (prev.get("watch") or [])}
    changes = []
    for i in ideas_all:
        k = (i["family"], i["ticker"], i["direction"])
        o = old.pop(k, None)
        if o is None:
            changes.append({"t": i["ticker"], "change": "new",
                            "to": i["status"]})
        elif o.get("status") != i["status"]:
            changes.append({"t": i["ticker"], "change": "status",
                            "from": o.get("status"), "to": i["status"]})
        elif (o.get("alpha_score") is not None
              and i.get("alpha_score") is not None
              and abs(o["alpha_score"] - i["alpha_score"]) >= 5):
            changes.append({"t": i["ticker"], "change": "score",
                            "from": o["alpha_score"],
                            "to": i["alpha_score"]})
    for (fam, tk, d), o in old.items():
        changes.append({"t": tk, "change": "dropped",
                        "from": o.get("status")})
    return changes[:12]


# ---------------------------------------------------------------- run

def run(dry=False, do_grade=True):
    now = _now()
    regimes = _regime_map()
    regime = _regime_today(regimes, now)
    gates = family_gates()
    score, score_ok = _score_fn()

    candidates = (_flow_candidates(regimes, now)
                  + _earnings_candidates(now)
                  + _momentum_candidates(now))

    n_universe = len(candidates)
    display = []
    for c in candidates:
        sc = score(c["feats"]) if (c["family"] == "flow" and score_ok) \
            else None
        reason = hard_filter(c, regime, sc, now)
        if reason:
            REJECT[reason] = REJECT.get(reason, 0) + 1
            continue
        # Family gate -> tier. Three tiers, visually unmistakable in the
        # UI (spec + reviewer): QUALIFIED (validated edge only), WATCH
        # (research watchlist — interesting, not cleared), EXPERIMENTAL
        # (family/model actively under forward validation, incl.
        # degraded families rehabilitating on the paper ledger).
        if c["family"] == "flow":
            g = gates["flow"]
            status = "QUALIFIED" if g["verdict"] == "qualified" else "WATCH"
            gate_why = g["why"]
        elif c["family"] == "earnings":
            g = (gates["earnings"]["types"].get(c.get("etype")) or
                 {"verdict": "watch", "why": "accruing"})
            status = {"qualified": "QUALIFIED",
                      "trade_qualified": "TRADE_QUALIFIED",
                      "signal_qualified": "SIGNAL_QUALIFIED",
                      "degraded": "EXPERIMENTAL"}.get(g["verdict"],
                                                      "WATCH")
            gate_why = g["why"]
        else:
            g = gates["momentum"]
            status = {"qualified": "QUALIFIED",
                      "degraded": "EXPERIMENTAL"}.get(g["verdict"],
                                                      "WATCH")
            gate_why = g["why"]

        item = {
            "family": c["family"], "ticker": c["ticker"],
            "direction": c["direction"], "status": status,
            "alpha_score": sc, "gate": gate_why,
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
            item["evidence"] = {"thesis": i.get("thesis"),
                                "cited": i.get("evidence"),
                                "etype": c.get("etype"),
                                "grade": i.get("grade"),
                                "mcap_b": i.get("mcap_b")}
        display.append(item)

    display.sort(key=lambda x: (x["alpha_score"] is None,
                                -(x["alpha_score"] or 0)))
    TOP_STATUSES = ("TRADE_QUALIFIED", "QUALIFIED", "SIGNAL_QUALIFIED")
    top = [i for i in display
           if i["status"] in TOP_STATUSES][:MAX_IDEAS]
    watch = [i for i in display if i["status"] == "WATCH"][:MAX_WATCH]
    experimental = [i for i in display
                    if i["status"] == "EXPERIMENTAL"][:MAX_WATCH]

    # Desk counts — evidence-level tallies (reviewer: never let a
    # signal-qualified observation masquerade as a qualified trade).
    n_trade_q = sum(1 for i in top if i["status"] == "TRADE_QUALIFIED")
    n_signal_q = sum(1 for i in top
                     if i["status"] == "SIGNAL_QUALIFIED")
    n_plain_q = sum(1 for i in top if i["status"] == "QUALIFIED")
    desk_counts = {
        "trade_qualified": n_trade_q + n_plain_q,
        "signal_qualified": n_signal_q,
        "research_watch": len(watch),
        "experimental": len(experimental),
    }
    # Multiple independent pipelines, not one funnel.
    n_flow_cand = sum(1 for c in candidates if c["family"] == "flow")
    n_ern_cand = sum(1 for c in candidates
                     if c["family"] == "earnings")
    pipelines = [
        {"name": "Earnings Volatility",
         "line": f"{n_ern_cand} scanned → {n_signal_q} signal-qualified"
                 f" → {n_trade_q} trade-qualified"},
        {"name": "Options Flow",
         "line": f"{n_flow_cand} candidates → "
                 f"{sum(1 for i in watch if i['family']=='flow')} watch"
                 " → 0 qualified (no champion)"},
    ]
    so = _load(R("docs", "reports", "scan_outcomes.json"), {})
    so_n = ((so.get("overall") or {}).get("n"))
    if so_n:
        pipelines.append({"name": "Momentum",
                          "line": f"{so_n} graded observations → "
                                  "DEGRADED"})
    drift = ((_load(R("docs", "reports", "earnings_ideas.json"), {})
              .get("by_type") or {}).get("post_report_drift") or {})
    if drift.get("n"):
        pipelines.append({"name": "Post-Report Drift",
                          "line": f"{drift['n']} graded → DEGRADED"})
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
    vol_tops = [i for i in top
                if (i.get("construct") or {}).get("vol_edge_pp")
                is not None]
    if vol_tops:
        rich = max(vol_tops,
                   key=lambda i: i["construct"]["vol_edge_pp"])
        c = rich["construct"]
        headlines.append({
            "h": f"{rich['ticker']} is today's richest earnings-vol "
                 "setup",
            "d": f"Market-implied move is {c['implied_move']}% versus "
                 f"TickerDesk Fair Move of {c['fair_move']}%, a "
                 f"+{c['vol_edge_pp']}pp discrepancy."})
    if n_signal_q and not (n_trade_q + n_plain_q):
        headlines.append({
            "h": "Vol-rich remains signal-qualified, not "
                 "trade-qualified",
            "d": "The expression gate still has no champion; "
                 "defined-risk structures remain rejected."})
    if any(i["family"] == "flow" for i in watch):
        headlines.append({
            "h": "Options flow remains research-only",
            "d": f"{sum(1 for i in watch if i['family']=='flow')} "
                 "setups entered Watch; the family has no validated "
                 "walk-forward champion."})

    events = freeze_ideas(top + watch + experimental, now)
    events, n_graded = grade_ledger(events, now, do_grade=do_grade)
    perf = performance(events)

    out = {
        "generated": _iso(now),
        "engine_version": ENGINE_VERSION,
        "model_version": MODEL_VERSION,
        "data_cutoff": _iso(now),
        "market_regime": regime,
        "score_available": score_ok,
        "gates": gates,
        "top_ideas": top,
        "watch": watch,
        "experimental": experimental,
        "funnel": funnel,
        "desk_counts": desk_counts,
        "pipelines": pipelines,
        "headlines": headlines,
        "ledgers": _ledgers(perf),
        "abstention": (desk_counts["trade_qualified"] == 0),
        "abstention_note": (
            ("No executable trade clears TickerDesk's production gate "
             "today. " +
             (f"{desk_counts['signal_qualified']} earnings-volatility "
              "markets do exhibit a validated forecasting discrepancy, "
              "but no option structure has demonstrated sufficient "
              "after-cost expectancy."
              if desk_counts["signal_qualified"] else
              "Candidates below are tracked paper-forward; families "
              "qualify automatically when their own record clears "
              "their gates."))
            if desk_counts["trade_qualified"] == 0 else None),
        "rejections": {k: v for k, v in REJECT.items() if v},
        "what_changed": changes,
        "performance": perf,
        "honesty": ("Every displayed statistic is computed from "
                    "point-in-time data or a disclosed deterministic "
                    "model; no values are invented by the LLM. Flow "
                    "Rank is a relative research rank among today's "
                    "flow candidates — the options-flow family has NOT "
                    "demonstrated out-of-sample alpha. TickerDesk Fair "
                    "Move is a disclosed forecast (v1: ticker median "
                    "realized move), not a measurement."),
    }
    if not dry:
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1, ensure_ascii=False)
    print("regime:", regime, "| top:", len(top), "| watch:", len(watch),
          "| rejections:", out["rejections"],
          "| ledger graded now:", n_graded,
          "| tracked:", perf.get("tracked"))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-grade", action="store_true")
    a = ap.parse_args()
    run(dry=a.dry_run, do_grade=not a.no_grade)
