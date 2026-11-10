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
    vol_rich / vol_cheap   the options-implied move for the report session
                           alone (implied_event) vs the name's Fair Move =
                           MEAN |past reaction| (both from earnings_edge, on
                           the same basis — see earnings_move_math.py);
                           emitted only from a read taken before the report
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
    vol_rich               mv = implied_graded - |report reaction|
    vol_cheap              mv = |report reaction| - implied_graded
implied_graded is the event-only implied move from the LAST read taken
before the report (each run refreshes a pending vol idea's read until the
report), widened by one normal session when the report time is unknown
(the reaction is then graded close D-1 -> close D+1). Per-type hit rates
+ EV (mean mv) publish at n>=30 ("accruing" until then) and render as
track-record chips on the cards. A fairly priced straddle still "wins"
~58-63% of the time, so vol types also publish the fair-pricing baseline
win rate and the realized/implied ratio (1.00 = fair) next to the win
rate.

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
from datetime import UTC, date, datetime, timedelta

import earnings_move_math as EM

_BASE = os.path.dirname(os.path.abspath(__file__))
R = lambda *p: os.path.join(_BASE, *p)
LOG_PATH = R("data", "earnings_ideas_log.json")
OUT_PATH = R("docs", "reports", "earnings_ideas.json")
MIN_N = 30
LOG_CAP = 1200
GRADE_AFTER_SESSIONS = 3
# v2 (2026-11 accuracy audit): session-aware reaction frame. v1 measured
# prior close -> event-day close for EVERY idea, which for an after-close
# (AMC) report is the quiet day BEFORE the print — vol_rich "won" 98.6% of
# AMC ideas against a non-event day. Ideas graded under an older version
# are regraded (REGRADE_PER_RUN at a time, to stay under Yahoo's limits).
GRADE_VERSION = 2
# v3 for the vol types (2026-11 like-for-like fix): graded against the
# event-only implied move from the last pre-event read (not the raw
# straddle frozen at first logging, which also priced normal sessions and
# was sometimes a 1-sigma number). Legacy ideas without a recorded read
# are reconstructed from their first logged read (basis "reconstructed")
# or voided when that read cannot be shown to precede the report.
VOL_TYPES = ("vol_rich", "vol_cheap")
VOL_GRADE_VERSION = 3
REGRADE_PER_RUN = 250
# Fields copied from an earnings_edge row onto a vol idea / log entry.
READ_FIELDS = (
    "implied",
    "implied_src",
    "implied_expiry",
    "implied_asof",
    "nonevent_sessions",
    "daily_vol",
    "implied_event",
    "fair_move",
    "realized_med",
    "n_reports",
)


def grade_version(typ):
    """Current grading version for an idea type."""
    return VOL_GRADE_VERSION if typ in VOL_TYPES else GRADE_VERSION


MIN_MCAP = 1e9  # hard floor — no ideas on sub-$1B names
SHRINK_K = 30  # shrinkage constant: delta *= n / (n + K)
CLAMP_PP = 15.0  # max +/- percentage-point adjustment, per cohort AND total


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _swing_map():
    s = _load(R("docs", "reports", "swing_latest_summary.json"), None) or _load(
        R("docs", "reports", "swing_report.json"), None
    )
    out = {}
    if not s or not s.get("runs"):
        return out
    for g, arr in (s["runs"][-1].get("grades") or {}).items():
        for c in arr:
            out[c.get("t")] = {
                "grade": g,
                "chg": c.get("chg"),
                "rvol": c.get("rvol"),
                "ext": c.get("ext"),
                "th": c.get("th") or [],
            }
    return out


def _read_txt(n):
    """' (the ±9.8% straddle to 11/14, less 3 normal sessions)' — how the
    event-only move was derived from the raw option read."""
    raw, k = n.get("implied"), n.get("nonevent_sessions")
    if raw is None or k is None:
        return ""
    exp = str(n.get("implied_expiry") or "")
    p = exp.split("-")
    when = (" to " + str(int(p[1])) + "/" + str(int(p[2]))) if len(p) == 3 else ""
    what = (
        "a ±" + str(raw) + "% 1-sigma IV read" + when + ", restated as a straddle"
        if n.get("implied_src") == "iv_1sigma"
        else "the ±" + str(raw) + "% straddle" + when
    )
    less = (
        ", no other sessions"
        if k == 0
        else ", less " + str(k) + " normal session" + ("" if k == 1 else "s")
    )
    return " (" + what + less + ")"


def generate(names, swing, facts):
    """Idea cards for names reporting within the horizon."""
    ideas = []
    for n in names:
        t = n.get("t")
        if not t or n.get("days") is None or n["days"] > 7:
            continue
        if (n.get("mcap") or 0) < MIN_MCAP:
            continue  # tradable size only
        sw = swing.get(t) or {}
        fa = (facts or {}).get(t) or {}
        ev = []  # evidence strings, all sourced
        # like-for-like pair: event-only implied vs MEAN |past reaction|
        implied = n.get("implied_event")
        fair = n.get("fair_move")
        rmed = n.get("realized_med")
        nrep = n.get("n_reports") or 0
        drift = n.get("drift_5d")
        fl = n.get("flow_into") or {}
        rs = fa.get("rs") or {}
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
            ev.append(
                str(fl.get("bull", 0))
                + " bull / "
                + str(fl.get("bear", 0))
                + " bear flow into print"
            )
        if fair is not None and nrep >= 4:
            ev.append(
                "averages ±"
                + str(fair)
                + "% on reports (median ±"
                + str(rmed)
                + "%, "
                + str(nrep)
                + " reports)"
            )

        typ = bias = thesis = None
        bull_flow = fl.get("n") and (fl.get("bull", 0) > fl.get("bear", 0))

        # a mean that is really one outsized report makes no rich/cheap call
        if implied is not None and fair and nrep >= 4 and not n.get("fair_move_dominated"):
            ratio = implied / fair
            how = _read_txt(n)
            if ratio >= 1.35:
                typ, bias = "vol_rich", "neutral"
                thesis = (
                    "Options price a ±"
                    + str(round(implied, 1))
                    + "% move for the report itself"
                    + how
                    + " vs a ±"
                    + str(fair)
                    + "% average reaction across "
                    + str(nrep)
                    + " past reports (median ±"
                    + str(rmed)
                    + "%): "
                    + str(round(ratio, 2))
                    + "× rich — premium sellers hold the pricing edge "
                    "if history repeats."
                )
            elif ratio <= 0.75:
                typ, bias = "vol_cheap", "neutral"
                thesis = (
                    "Options price only ±"
                    + str(round(implied, 1))
                    + "% for the report itself"
                    + how
                    + " vs a ±"
                    + str(fair)
                    + "% average reaction ("
                    + str(nrep)
                    + " reports, median ±"
                    + str(rmed)
                    + "%): "
                    + str(round(ratio, 2))
                    + "× — the move is cheap; long-vol structures hold "
                    "the pricing edge if history repeats."
                )
        if typ is None and a_tier and bull_flow and (drift or 0) > 0:
            typ, bias = "momentum_into_print", "bull"
            thesis = (
                "Leader into the print: "
                + grade
                + " grade"
                + (", RS " + str(rs_rank) if rs_rank is not None else "")
                + ", bullish options flow, and this name has averaged +"
                + str(drift)
                + "% in the 5 sessions after reporting — "
                "strength into earnings has historically carried through."
            )
        if typ is None and drift is not None and abs(drift) >= 1.5 and nrep >= 6:
            typ = "post_report_drift"
            bias = "bull" if drift > 0 else "bear"
            thesis = (
                "The after-print trade: across "
                + str(nrep)
                + " reports this name drifts "
                + ("+" if drift > 0 else "")
                + str(drift)
                + "% on average in the 5 sessions AFTER earnings — "
                "reacting post-report sidesteps the binary night."
            )
        if typ is None and rmed is not None and rmed >= 6 and (sw.get("chg") or 0) >= 4:
            typ, bias = "binary_caution", "neutral"
            thesis = (
                "Crowded binary: already ran +"
                + str(sw["chg"])
                + "% today and typically swings ±"
                + str(rmed)
                + "% on results — size down or let the print happen "
                "and trade the reaction."
            )
        if typ is None:
            continue
        cap = n.get("mcap") or 0
        ideas.append(
            {
                "t": t,
                "date": n.get("date"),
                "days": n.get("days"),
                "session": n.get("session"),
                "type": typ,
                "bias": bias,
                "thesis": thesis,
                "evidence": ev[:5],
                # the read, verbatim from earnings_edge (implied = RAW read,
                # implied_event = like-for-like; see READ_FIELDS)
                **{k: n.get(k) for k in READ_FIELDS},
                "n_reports": nrep or None,
                "mcap_b": round(cap / 1e9, 1) or None,
                "grade": grade or None,
                "rs_rank": rs_rank,
                "drift_5d": drift,
                # feature bands — logged with the idea so graded outcomes can
                # be bucketed into learnable cohorts (fail-open on "na")
                "feat": {
                    "cap": (">100B" if cap >= 100e9 else "10-100B" if cap >= 10e9 else "1-10B"),
                    "rs": (
                        "na"
                        if rs_rank is None
                        else "hi"
                        if rs_rank >= 80
                        else "mid"
                        if rs_rank >= 50
                        else "lo"
                    ),
                    "session": n.get("session") or "na",
                },
            }
        )
    order = {
        "momentum_into_print": 0,
        "vol_rich": 1,
        "vol_cheap": 1,
        "post_report_drift": 2,
        "binary_caution": 3,
    }
    ideas.sort(key=lambda i: (i["days"], order.get(i["type"], 9)))
    return ideas


# ── learned layer: log + grade + gate ────────────────────────────────────


_CLOSES_CACHE = {}


def _closes_around(t, event_date):
    """Daily closes from ~11 weeks before to ~4 weeks after the event via
    yfinance (a window AROUND the event, so old ideas can be regraded — the
    old "last 2 months" fetch had no bars for events older than that; the
    pre-event stretch also feeds the daily volatility a legacy vol idea's
    read is reconstructed with). Cached per (ticker, event) within a run;
    None on failure."""
    key = (t, event_date)
    if key in _CLOSES_CACHE:
        return _CLOSES_CACHE[key]
    rows = None
    try:
        import yfinance as yf

        ev = datetime.strptime(event_date, "%Y-%m-%d").date()
        df = yf.Ticker(t).history(
            start=(ev - timedelta(days=80)).isoformat(),
            end=(ev + timedelta(days=28)).isoformat(),
            interval="1d",
            auto_adjust=False,
        )
        if df is not None and not df.empty:
            rows = [(idx.date().isoformat(), float(r["Close"])) for idx, r in df.iterrows()]
    except Exception:
        rows = None
    _CLOSES_CACHE[key] = rows
    return rows


def _cluster_se(pairs):
    """Cluster-robust (CR1) standard error of a mean over (cluster, value)
    pairs; None with < 2 clusters."""
    n = len(pairs)
    if n < 2:
        return None
    mu = sum(v for _, v in pairs) / n
    resid = {}
    for k, v in pairs:
        resid[k] = resid.get(k, 0.0) + (v - mu)
    g = len(resid)
    if g < 2:
        return None
    return (sum(r * r for r in resid.values()) / (n * n) * g / (g - 1)) ** 0.5


def _reaction_frame(rows, ev, session):
    """(pre_i, day1_i): indexes of the closes that bracket the earnings
    reaction, or (None, None).

      BMO (before the open) on D : close D-1 -> close D
      AMC (after the close) on D : close D   -> close D+1
      unknown session            : close D-1 -> close D+1 (brackets both)
    An event dated on a non-session day reacts on the next session."""
    on_i = next((i for i, (d, _) in enumerate(rows) if d >= ev), None)
    if on_i is None:
        return None, None
    on_event_day = rows[on_i][0] == ev
    s = str(session or "").upper()
    if not on_event_day or s == "BMO":
        pre_i, day1_i = on_i - 1, on_i
    elif s == "AMC":
        pre_i, day1_i = on_i, on_i + 1
    else:
        pre_i, day1_i = on_i - 1, on_i + 1
    if pre_i < 0 or day1_i >= len(rows):
        return None, None
    return pre_i, day1_i


def _session_of(idea):
    return idea.get("session") or (idea.get("feat") or {}).get("session")


def _reconstruct_read(idea, rows):
    """Legacy vol idea (logged before reads were recorded): rebuild its
    event-only implied move from the FIRST logged read.

    Returns (read_dict, None) or (None, why). Assumptions, stated in the
    payload: the logged value was a straddle (implied_direct has been the
    primary read since 2026-07-27); it was priced at the close of the
    `published` date (the post-close job's UTC date); its expiry was the
    first weekly within 5 days of the report (implied_direct's window);
    daily volatility = the 40 sessions up to that close."""
    raw, pub, ev = idea.get("implied"), idea.get("published"), idea.get("date")
    session = _session_of(idea)
    if not raw or raw <= 0 or not pub:
        return None, "no logged read"
    try:
        read = date.fromisoformat(str(pub)[:10])
    except ValueError:
        return None, "unreadable logging date"
    if not EM._is_session(read):
        read = EM.previous_session(read)
    if not EM.is_pre_event(read, ev, session):
        # first logged on/after the report: the read may already reflect
        # the result (e.g. a tiny post-report straddle flagged "cheap")
        return None, "first read not provably before the report"
    exp = EM.assumed_weekly_expiry(ev, session)
    if exp is None:
        return None, "no weekly expiry within 5 days of the report"
    k = EM.nonevent_sessions(read, exp, ev, session)
    if k is None:
        return None, "assumed expiry does not cover the report"
    closes = [c for d, c in rows if d <= read.isoformat()]
    dv = EM.daily_vol_pct(closes)
    evm = EM.event_move(raw, "straddle", k, dv)
    if evm.get("implied_event") is None:
        return None, evm.get("why") or "no event-only move"
    return {
        "implied_event": evm["implied_event"],
        "daily_vol": dv,
        "nonevent_sessions": k,
        "implied_expiry": exp.isoformat(),
        "implied_asof": read.isoformat(),
    }, None


def _grade_full(idea):
    """dict of grade fields, or None when not gradeable YET (missing or
    too few closes — retried next run).

    Always carries result (win/loss, or void for a vol idea whose read
    cannot be put on the like-for-like basis) and, when graded, mv: a
    uniform margin metric, the graded move expressed so that mv > 0 ==
    win for every idea type (EV-capable learned layer). Vol ideas also
    carry abs_move, implied_graded, frame_sessions and basis
    (exact = the recorded last pre-event read; reconstructed = legacy)."""
    ev = idea.get("date")
    if not ev:
        return None
    rows = _closes_around(idea["t"], ev)
    if not rows:
        return None
    # Session-aware: the reaction to an AMC print is the NEXT session.
    session = _session_of(idea)
    pre_i, day1_i = _reaction_frame(rows, ev, session)
    if pre_i is None or day1_i is None or pre_i + GRADE_AFTER_SESSIONS >= len(rows):
        return None  # not enough post-event sessions
    import math as _math

    def _fin(x):
        return x if isinstance(x, (int, float)) and _math.isfinite(x) and x > 0 else None

    pre = _fin(rows[pre_i][1])
    day1 = _fin(rows[day1_i][1])
    d3 = _fin(rows[pre_i + 3][1]) if pre_i + 3 < len(rows) else None
    # drift: reaction close -> +5 sessions (the 5-session window the drift
    # ideas are selected on in earnings_edge)
    d5 = _fin(rows[day1_i + 5][1]) if day1_i + 5 < len(rows) else None
    if pre is None or day1 is None:
        return None  # NaN closes (halt/missing bar): not gradeable
    move1 = (day1 / pre - 1) * 100
    typ = idea["type"]
    sign = 1 if idea.get("bias") == "bull" else -1
    out = {}
    mv = None
    if typ in VOL_TYPES:
        if idea.get("implied_event") is not None:
            out["basis"] = "exact"
            imp_ev, dv = idea["implied_event"], idea.get("daily_vol")
        else:
            rec, why = _reconstruct_read(idea, rows)
            if rec is None:
                return {"result": "void", "void_why": why}
            out["basis"] = "reconstructed"
            out.update(rec)
            imp_ev, dv = rec["implied_event"], rec["daily_vol"]
        frame = day1_i - pre_i  # 1 session, or 2 when the report time is unknown
        imp = EM.frame_move(imp_ev, dv, frame - 1)
        if imp is None:
            return {"result": "void", "void_why": "no daily volatility for a two-session frame"}
        out.update(
            abs_move=round(abs(move1), 2),
            implied_graded=round(imp, 2),
            frame_sessions=frame,
        )
        mv = imp - abs(move1) if typ == "vol_rich" else abs(move1) - imp
    elif typ == "momentum_into_print" and d3 is not None:
        mv = sign * (d3 / pre - 1) * 100
    elif typ == "post_report_drift" and d5 is not None:
        mv = sign * (d5 / day1 - 1) * 100  # drift measured AFTER the print
    if mv is None:
        return None  # binary_caution isn't a trade
    out.update(result="win" if mv > 0 else "loss", mv=round(mv, 2))
    return out


def _grade(idea):
    """(result, mv) — result win/loss/void/None(not gradeable yet)."""
    g = _grade_full(idea)
    if not g:
        return None, None
    return g["result"], g.get("mv")


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
    for e in _current_grades(entries):
        for k in _cohort_keys(e):
            coh.setdefault(k, []).append((e["result"] == "win", e.get("mv"), _ratio(e)))
    out = {}
    for k, obs in coh.items():
        n = len(obs)
        if n < MIN_N:
            continue
        wr = 100.0 * sum(1 for w, _, _ in obs if w) / n
        # Vol ideas are measured against the win rate a FAIRLY priced
        # straddle would score with this cohort's dispersion (~58-63%),
        # not 50% — otherwise a fair market reads as an edge.
        base = 50.0
        if k.split("|")[0].removeprefix("type:") in VOL_TYPES:
            fb = EM.fair_win_baseline([r for _, _, r in obs if r is not None])
            base = fb if fb is not None else 100.0 * EM.NORMAL_FAIR_WIN
        delta = (wr - base) * (n / (n + SHRINK_K))
        mvs = [m for _, m, _ in obs if m is not None and m == m and abs(m) != float("inf")]
        out[k] = {
            "n": n,
            "wr": round(wr, 1),
            "base": round(base, 1),
            "delta": round(max(-CLAMP_PP, min(CLAMP_PP, delta)), 2),
            "ev": round(sum(mvs) / len(mvs), 2) if mvs else None,
        }
    return out


def _current_grades(entries):
    """Win/loss entries graded under their type's CURRENT version — a stale
    v1 AMC grade measured the day before the report; a v2 vol grade used
    the raw straddle frozen at first logging."""
    return [
        e
        for e in entries
        if e.get("result") in ("win", "loss") and e.get("gv") == grade_version(e.get("type"))
    ]


def _ratio(e):
    """|realized| / implied for a graded vol entry (None otherwise)."""
    a, imp = e.get("abs_move"), e.get("implied_graded")
    if a is None or not imp:
        return None
    return a / imp


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
    if any_conf:  # stable — preserves type order
        ideas.sort(key=lambda i: (i["days"], -(i.get("conf") or 50)))


def apply_read(entry, row):
    """Copy an earnings_edge read onto a pending vol log entry when it is a
    usable like-for-like read taken BEFORE the report. Each nightly run
    calls this, so a pending idea is graded on the last pre-event close,
    not on the read that first flagged it (up to ~7 days earlier).
    Returns True when the entry changed."""
    if not row or row.get("implied_event") is None or not row.get("implied_asof"):
        return False
    session = row.get("session") or _session_of(entry)
    try:
        if not EM.is_pre_event(row["implied_asof"], entry["date"], session):
            return False
    except ValueError:
        return False
    if entry.get("implied_asof") == row["implied_asof"] and entry.get("implied_event") is not None:
        return False
    for k in READ_FIELDS:
        entry[k] = row.get(k)
    if row.get("session"):
        entry["session"] = row["session"]
    entry["reads"] = int(entry.get("reads") or 0) + 1
    return True


def log_new_ideas(log, ideas, today, edge_rows=None):
    """Append today's new ideas to the log (once per ticker/date/type).
    Vol ideas carry their read; returns the number added."""
    seen = {(i.get("t"), i.get("date"), i.get("type")) for i in log["ideas"]}
    added = 0
    for i in ideas:
        key = (i["t"], i["date"], i["type"])
        if key in seen or i["type"] == "binary_caution":
            continue
        e = {
            "t": i["t"],
            "date": i["date"],
            "type": i["type"],
            "bias": i["bias"],
            "implied": i.get("implied"),
            "feat": i.get("feat"),
            "published": today,
            "result": "pending",
        }
        if i["type"] in VOL_TYPES:
            row = (edge_rows or {}).get(key[:2]) or {k: i.get(k) for k in READ_FIELDS}
            row = dict(row, session=i.get("session"))
            if not apply_read(e, row):
                continue  # no usable pre-report read: nothing honest to grade
        log["ideas"].append(e)
        seen.add(key)
        added += 1
    return added


def refresh_pending_reads(log, edge_rows):
    """Move every pending vol idea onto tonight's read (if still before
    its report). Returns the number refreshed."""
    n = 0
    for e in log["ideas"]:
        if e.get("type") in VOL_TYPES and e.get("result") == "pending":
            n += apply_read(e, edge_rows.get((e.get("t"), e.get("date"))))
    return n


def grade_log(log, cutoff):
    """Grade matured pending ideas (event >= ~GRADE_AFTER_SESSIONS old) and
    regrade ideas graded under an older version of their type (capped per
    run; a failed regrade keeps the old grade and is retried next run).
    Returns (graded, regraded, voided)."""
    graded = regraded = voided = 0
    for i in log["ideas"]:
        if not (i.get("date") and i["date"] <= cutoff):
            continue
        gv = grade_version(i.get("type"))
        pending = i.get("result") == "pending"
        stale = i.get("result") in ("win", "loss") and i.get("gv") != gv
        if not (pending or (stale and regraded < REGRADE_PER_RUN)):
            continue
        g = _grade_full(i)
        if stale:
            regraded += 1
        if not g:
            continue
        if stale:  # keep the superseded grade for the audit trail
            i["prev_grade"] = {"result": i.get("result"), "mv": i.get("mv"), "gv": i.get("gv")}
        for k in ("mv", "abs_move", "implied_graded", "frame_sessions", "basis", "void_why"):
            i.pop(k, None)
        i.update(g)
        i["gv"] = gv
        if g["result"] == "void":
            voided += 1
        else:
            graded += 1
    return graded, regraded, voided


def type_stats(entries):
    """Per-type published stats from current-version grades."""
    by_type = {}
    for i in _current_grades(entries):
        by_type.setdefault(i["type"], []).append(i)
    stats = {}
    for k, obs in by_type.items():
        n = len(obs)
        if n < MIN_N:
            stats[k] = {"status": "accruing", "n": n, "activates_at": MIN_N}
            continue
        pairs = [
            (i.get("date"), i["mv"])
            for i in obs
            if isinstance(i.get("mv"), (int, float))
            and i["mv"] == i["mv"]
            and abs(i["mv"]) != float("inf")
        ]
        mvs = [m for _, m in pairs]
        ev = sum(mvs) / len(mvs) if mvs else None
        se = _cluster_se(pairs)
        st = {
            "status": "active",
            "n": n,
            "win_rate": round(100 * sum(1 for i in obs if i["result"] == "win") / n),
            "ev": (round(ev, 2) if ev is not None else None),
            # 95% CI clustered by event date (same-day reports share
            # one market move); gates need its low end above zero
            "ci95": (
                [round(ev - 1.96 * se, 2), round(ev + 1.96 * se, 2)]
                if ev is not None and se
                else None
            ),
        }
        if k in VOL_TYPES:
            ratios = [r for r in (_ratio(i) for i in obs) if r is not None]
            a = sum(i.get("abs_move") or 0 for i in obs)
            m = sum(i.get("implied_graded") or 0 for i in obs)
            st.update(
                # what a FAIRLY priced straddle would have scored on these
                # events (a vol_rich win rate is measured against this)
                fair_win_rate=EM.fair_win_baseline(ratios),
                fair_win_rate_normal=round(100 * EM.NORMAL_FAIR_WIN, 1),
                fair_win_rate_fat_tail=round(100 * EM.LAPLACE_FAIR_WIN, 1),
                # average |realized| / average event-only implied (1.00 = fair)
                realized_over_implied=round(a / m, 3) if m else None,
                n_exact=sum(1 for i in obs if i.get("basis") == "exact"),
                n_reconstructed=sum(1 for i in obs if i.get("basis") == "reconstructed"),
            )
            if st["fair_win_rate"] is not None:
                st["win_vs_fair_pp"] = round(st["win_rate"] - st["fair_win_rate"], 1)
        stats[k] = st
    for k in VOL_TYPES:
        nv = sum(1 for i in entries if i.get("type") == k and i.get("result") == "void")
        if nv:
            stats.setdefault(k, {"status": "accruing", "n": 0, "activates_at": MIN_N})
            stats[k]["n_void"] = nv
    return stats


GRADING_NOTE = (
    "Vol ideas (grade v3) compare like with like: the options-implied move "
    "for the report session alone (the ATM straddle of the first expiry "
    "after the report, or a 1-sigma IV read restated as a straddle, with "
    "the normal sessions to expiry stripped out using recent daily "
    "volatility) from the LAST read before the report, against the actual "
    "|reaction| (close before -> close after; reports of unknown time span "
    "two sessions and the implied move is widened by one normal session). "
    "A fairly priced straddle still wins ~58-63% of the time, so the "
    "win rate is shown next to fair_win_rate (what a fair price would "
    "have scored on these same events) and realized_over_implied (1.00 = "
    "fair). basis 'reconstructed' = ideas logged before reads were "
    "recorded, rebuilt from the first logged read (assumed straddle, "
    "priced at that day's close, first weekly expiry within 5 days of the "
    "report); ideas whose first read was not provably before the report "
    "are void and excluded."
)


def main():
    edge = _load(R("docs", "reports", "earnings_edge.json"), {})
    facts = _load(R("docs", "reports", "technical_facts.json"), {})
    facts = facts.get("facts") or facts
    swing = _swing_map()
    ideas = generate(edge.get("names") or [], swing, facts)
    edge_rows = {(r.get("t"), r.get("date")): r for r in edge.get("names") or []}

    log = _load(LOG_PATH, {"ideas": []})
    today = datetime.now(UTC).date().isoformat()
    refreshed = refresh_pending_reads(log, edge_rows)
    added = log_new_ideas(log, ideas, today, edge_rows)

    cutoff = (datetime.now(UTC).date() - timedelta(days=GRADE_AFTER_SESSIONS + 4)).isoformat()
    graded, regraded, voided = grade_log(log, cutoff)
    if regraded:
        print(f"  regraded {regraded} ideas to their current grade version")
    if voided:
        print(f"  voided {voided} vol ideas with no provably pre-report read")
    log["ideas"] = log["ideas"][-LOG_CAP:]

    stats = type_stats(log["ideas"])

    # feedback loop: graded cohorts adjust conf on today's ideas
    learned = _learned(log["ideas"])
    _apply_conf(ideas, learned)

    payload = {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "count": len(ideas),
        "ideas": ideas,
        "by_type": stats,
        "total_graded": len(_current_grades(log["ideas"])),
        "reads_refreshed": refreshed,
        "grading_note": GRADING_NOTE,
        "min_mcap_b": round(MIN_MCAP / 1e9),
        "learned": {
            "status": "active" if learned else "accruing",
            "cohorts_active": len(learned),
            "activates_at": MIN_N,
            # publish the gated cohorts themselves — transparency over
            # a black box (small dict; only n>=MIN_N cohorts appear)
            "cohorts": learned,
        },
        "note": (
            "Deterministic idea cards from the site's own signals "
            "(swing grade, RS, flow-into-print, implied vs realized "
            "history, post-report drift). $" + str(round(MIN_MCAP / 1e9)) + "B market-cap floor. "
            "Self-graded against real closes once each report is "
            + str(GRADE_AFTER_SESSIONS)
            + "+ sessions old; per-type "
            "hit rates + EV publish at n>="
            + str(MIN_N)
            + "; graded cohorts feed back into each idea's conf score "
            "(shrunk, clamped, fail-open). Educational, not advice."
        ),
    }
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, separators=(",", ":"))
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(
        "  earnings ideas: %d published (%d new logged, %d graded this "
        "run, %d graded total)" % (len(ideas), added, graded, payload["total_graded"])
    )
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
