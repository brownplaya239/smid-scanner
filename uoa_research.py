"""
uoa_research.py — nightly alpha-research engine for the options-flow ledger.

Runs nightly (momentum.yml). Scores the FULL signal ledger (monthly archives
plus the hot file) through uoa_alpha.compute_edge(), the same scoring path
the Options Flow track record uses, and publishes
docs/reports/uoa_research.json.

Measurement rules (2026-11 accuracy audit, shared with uoa_alpha):
  • ONE ALERT = a contract's first flag of the US/Eastern day. The scanner
    re-logs a still-flagged contract on every scan; uoa_alpha.unique_alerts
    collapses those re-logs.
  • Graded on the +5-session STOCK excess return vs SPY, signed in the
    flow's direction, by CLASSIFIED aggressor side only:
        calls bought (call_buyer) -> bullish book (+excess)
        puts bought  (put_buyer)  -> bearish book (-excess)
        premium sold (put_seller / call_seller) -> its own book, graded on
                     its lean (put sellers bullish, call sellers bearish)
    Mixed / unknown prints have no classified side: counted, not graded.
  • Price-basis guard (uoa_alpha._basis_ok) drops split / bad-print rows.
  • Walk-forward by ISO week with a 5-session EMBARGO: a +5d outcome takes
    5 sessions to resolve, so training rows flagged in the 5 sessions
    before a test period are dropped (their outcomes overlap it).
  • Every verdict string is built from the computed numbers, and says
    ACCRUING when the sample can't decide yet. Nothing here auto-applies
    to live ranking.

Sections:
  sample               n, span, sessions, unclassified / excluded counts
  attribution          per-book EV split into flagged-universe drift
                       (selection) and flow alpha, date-clustered 95% CIs
  loser_model          categorical odds model for bottom-quintile outcomes
  score2               empirical-Bayes cohort-EV score, weekly OOS IC
  stability / psi      weekly per-book EV; feature-mix drift (PSI)
  interactions         depth<=3 categorical interactions, review only
  avoidance_candidate  the pre-registered puts-bought 22-60 DTE pocket
  deployment           production status of every finding
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
from collections import defaultdict
from datetime import UTC, datetime
from itertools import combinations

import uoa_alpha as ua

_BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(_BASE, "docs", "reports", "uoa_research.json")

HORIZON = 5  # sessions; every EV here is the +5-session stock excess
EMBARGO_SESSIONS = 5  # = HORIZON: training outcomes must resolve before test
PREREG_DATE = "2026-07-21"  # avoidance-pocket hypothesis + gates frozen

CFG = {
    "shrink_k": 200,  # EB shrinkage strength (per-feature EV)
    "disaster_q": 0.20,  # bottom quintile = "disaster" label
    "drop_frac": 0.20,  # loser model excludes worst 20% by odds
    "min_train": 1000,  # walk-forward: rows needed to fit
    "min_test": 100,  # walk-forward: rows needed to score a week
    "min_cohort_n": 400,  # interaction search gate
    "holdout_frac": 0.30,  # time-ordered holdout share
    "pass_week_share": 0.60,  # a model must help in >= 60% of OOS weeks
}

BOOK_ORDER = ("call_buy", "put_buy", "seller")
BOOK_LABELS = {
    "call_buy": "Calls bought (bullish)",
    "put_buy": "Puts bought (bearish)",
    "seller": "Premium sold (graded on its lean: put sellers bullish, call sellers bearish)",
}


# ── small stats helpers ──────────────────────────────────────────────────


def _mean(v):
    return sum(v) / len(v) if v else None


def _r(v, nd=3):
    return round(v, nd) if v is not None else None


def _week(d):
    y, w, _ = datetime.strptime(d, "%Y-%m-%d").isocalendar()
    return "%d-W%02d" % (y, w)


def _clustered_ci(pairs):
    """Date-clustered 95% CI of the mean of (date, value) pairs, or None.
    Alerts on the same day share that day's market move, so they are not
    independent trades (uoa_alpha._cluster_se, CR1)."""
    if len(pairs) < 30:
        return None
    se = ua._cluster_se(pairs)
    if not se:
        return None
    m = _mean([v for _, v in pairs])
    return [round(m - 1.96 * se, 3), round(m + 1.96 * se, 3)]


def _ci95(v):
    """Trade-level (iid) 95% CI — kept for comparison only; it overstates
    certainty because same-day alerts are correlated."""
    n = len(v)
    if n < 30:
        return None
    m = _mean(v)
    var = sum((x - m) ** 2 for x in v) / (n - 1)
    se = (var / n) ** 0.5
    return [round(m - 1.96 * se, 3), round(m + 1.96 * se, 3)]


def _spans_zero(ci):
    return ci is None or (ci[0] <= 0 <= ci[1])


def _fmt(v, nd=2):
    return ("%+." + str(nd) + "f") % v if v is not None else "n/a"


def _fmt_ci(ci):
    return "[%+.2f, %+.2f]" % (ci[0], ci[1]) if ci else "n/a"


def _spearman(a, b):
    n = len(a)
    if n < 30:
        return None

    def ranks(x):
        order = sorted(range(n), key=lambda i: x[i])
        rk = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and x[order[j + 1]] == x[order[i]]:
                j += 1
            avg = (i + j) / 2.0
            for k2 in range(i, j + 1):
                rk[order[k2]] = avg
            i = j + 1
        return rk

    ra, rb = ranks(a), ranks(b)
    ma, mb = _mean(ra), _mean(rb)
    num = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    da = sum((v - ma) ** 2 for v in ra) ** 0.5
    db = sum((v - mb) ** 2 for v in rb) ** 0.5
    return round(num / (da * db), 4) if da and db else None


# ── rows ─────────────────────────────────────────────────────────────────


def _prem_bucket(p):
    p = p or 0
    return "<250K" if p < 250e3 else "250K-1M" if p < 1e6 else "1M-5M" if p < 5e6 else ">5M"


def _dte_bucket(d):
    if d is None:
        return "?"
    return "0-7" if d <= 7 else "8-21" if d <= 21 else "22-60" if d <= 60 else ">60"


def is_golden(s):
    """A golden sweep. The scanner tags it "Golden Sweep" (and then does NOT
    add the plain "Sweep" tag) and logs signal_type "golden_sweep"."""
    return "Golden Sweep" in (s.get("tags") or []) or s.get("signal_type") == "golden_sweep"


def features(book, s):
    """Categorical features, every one known at scan time, as dim:value."""
    return (
        "side:" + book,
        "dte:" + _dte_bucket(s.get("dte")),
        "cap:" + (s.get("cap_bucket") or "unknown"),
        "liq:" + (s.get("liquidity") or "C"),
        "prem:" + _prem_bucket(s.get("premium")),
        "golden:" + ("yes" if is_golden(s) else "no"),
    )


def _dim(f):
    return f.split(":", 1)[0]


def build_rows(scored):
    """Graded research rows from compute_edge's every-ledger-row list.

    Returns (rows, drift_rows, sample, spy, capmap):
      rows        one per unique, matured, basis-ok alert with a CLASSIFIED
                  aggressor side; `exc` is the +5d excess in the book's
                  direction, `raw` the unsigned stock excess
      drift_rows  every unique matured basis-ok alert (incl. unclassified)
                  — the flagged universe whose drift is the control
      sample      the counts behind the published n
      spy         {1: {date: SPY 1-session return}, 5: {date: SPY 5-session
                  return}} recovered as median(ret - excess) per flag date
      capmap      best-known cap bucket per ticker (for the adjusted PSI)
    """
    capmap = {}
    for s in scored:
        cb = s.get("cap_bucket")
        if cb and cb != "unknown" and s.get("ticker"):
            capmap[s["ticker"]] = cb
    alerts = ua.unique_alerts(scored)
    rows, drift_rows = [], []
    spy_acc = {1: defaultdict(list), 5: defaultdict(list)}
    basis_excluded = unmatured = unclassified = 0
    for s in alerts:
        if not ua._basis_ok(s):
            basis_excluded += 1
            continue
        rets = s.get("returns") or {}
        r5 = rets.get(HORIZON) or rets.get(str(HORIZON)) or {}
        raw = r5.get("excess")
        if raw is None:
            unmatured += 1
            continue
        date = ua._et_date(s.get("flagged_at"))
        for h in (1, 5):
            rh = rets.get(h) or rets.get(str(h)) or {}
            if rh.get("ret") is not None and rh.get("excess") is not None:
                spy_acc[h][date].append(rh["ret"] - rh["excess"])
        drift_rows.append({"date": date, "ticker": s.get("ticker"), "raw": raw})
        book = ua._BOOKS.get(s.get("flow_side") or "")
        if book is None:
            unclassified += 1
            continue
        name, sign = book
        tags = s.get("tags") or []
        score = s.get("raw_score")
        rows.append(
            {
                "date": date,
                "week": _week(date),
                "at": s.get("flagged_at") or "",
                "ticker": s.get("ticker"),
                "book": name,
                "side": s.get("flow_side"),
                "sign": sign,
                "raw": raw,
                "exc": sign * raw,
                "score": score if score is not None else (s.get("trade_score") or 0),
                "dte": s.get("dte"),
                "sector": s.get("sector") or "?",
                "liq": s.get("liquidity") or "C",
                "ern": "Into ERN" in tags,
                "premium": s.get("premium") or 0,
                "f": features(name, s),
            }
        )
    rows.sort(key=lambda r: (r["date"], r["at"]))
    spy = {h: {d: sorted(v)[len(v) // 2] for d, v in acc.items()} for h, acc in spy_acc.items()}
    by_book = defaultdict(int)
    for r in rows:
        by_book[r["book"]] += 1
    dates = sorted({r["date"] for r in rows})
    sample = {
        "ledger_rows": len(scored),
        "alerts": len(alerts),
        "graded": len(rows),
        "by_book": dict(by_book),
        "unclassified": unclassified,
        "basis_excluded": basis_excluded,
        "unmatured": unmatured,
        "span": [dates[0], dates[-1]] if dates else None,
        "sessions": len(dates),
        "weeks": len({_week(d) for d in dates}),
    }
    return rows, drift_rows, sample, spy, capmap


# ── time splits (with embargo) ───────────────────────────────────────────


def walk_forward(rows, min_prior_weeks=2):
    """Yield (week, train, test) per ISO week. Training uses only rows
    flagged at least EMBARGO_SESSIONS sessions before the test week's first
    session: a row flagged later has a +5d outcome that resolves inside the
    test week, so it would leak the test period's market move."""
    sessions = sorted({r["date"] for r in rows})
    pos = {d: i for i, d in enumerate(sessions)}
    byw = defaultdict(list)
    for r in rows:
        byw[r["week"]].append(r)
    weeks = sorted(byw)
    for i in range(min_prior_weeks, len(weeks)):
        test = byw[weeks[i]]
        k = pos[min(r["date"] for r in test)] - EMBARGO_SESSIONS
        if k < 1:
            continue
        cutoff = sessions[k]
        yield weeks[i], [r for r in rows if r["date"] < cutoff], test


def embargo_split(items, holdout_frac, date_of=lambda r: r["date"]):
    """Time-ordered (dev, holdout) split at a session boundary, with the
    EMBARGO_SESSIONS sessions before the holdout dropped from dev. `items`
    must be sorted by date. Returns (dev, holdout, embargoed_n)."""
    if not items:
        return [], [], 0
    sessions = sorted({date_of(r) for r in items})
    cut_date = date_of(items[min(len(items) - 1, int(len(items) * (1 - holdout_frac)))])
    k = sessions.index(cut_date) - EMBARGO_SESSIONS
    dev_end = sessions[k] if k >= 1 else None
    hold = [r for r in items if date_of(r) >= cut_date]
    dev = [r for r in items if dev_end is not None and date_of(r) < dev_end]
    return dev, hold, len(items) - len(dev) - len(hold)


# ── attribution ──────────────────────────────────────────────────────────


def universe_drift(drift_rows):
    """Per flag date, the mean +5d excess of the UNIQUE flagged underlyings
    (each ticker's alerts averaged first), direction-free."""
    by_day = defaultdict(lambda: defaultdict(list))
    for r in drift_rows:
        by_day[r["date"]][r["ticker"]].append(r["raw"])
    return {d: _mean([_mean(v) for v in tk.values()]) for d, tk in by_day.items()}


def _book_verdict(alpha, ci):
    if alpha is None:
        return "accruing"
    if _spans_zero(ci):
        return "flow alpha %s%% is not distinguishable from zero (95%% CI %s)" % (
            _fmt(alpha),
            _fmt_ci(ci),
        )
    if alpha > 0:
        return (
            "following this flow's direction added %s%% beyond the day's flagged-universe drift (95%% CI %s)"
            % (
                _fmt(alpha),
                _fmt_ci(ci),
            )
        )
    return (
        "following this flow's direction lost %s%% vs the day's flagged-universe drift (95%% CI %s)"
        % (
            _fmt(alpha),
            _fmt_ci(ci),
        )
    )


def attribution(rows, drift_rows):
    """Split each book's direction-signed EV into the flagged universe's own
    drift that day (selection) and what reading the flow's direction added
    beyond it (flow alpha)."""
    drift = universe_drift(drift_rows)
    out = {
        "universe_drift_ev": _r(_mean(list(drift.values()))),
        "drift_days": len(drift),
        "books": {},
    }
    for book in BOOK_ORDER:
        sel = [r for r in rows if r["book"] == book]
        sides = defaultdict(int)
        for r in sel:
            sides[r["side"]] += 1
        if len(sel) < 30:
            out["books"][book] = {
                "status": "accruing",
                "label": BOOK_LABELS[book],
                "n": len(sel),
                "sides": dict(sides),
            }
            continue
        dir_pairs = [(r["date"], r["exc"]) for r in sel]
        alpha_pairs = [(r["date"], r["sign"] * (r["raw"] - drift[r["date"]])) for r in sel]
        dir_ev = _mean([v for _, v in dir_pairs])
        alpha = _mean([v for _, v in alpha_pairs])
        alpha_ci = _clustered_ci(alpha_pairs)
        out["books"][book] = {
            "status": "active",
            "label": BOOK_LABELS[book],
            "n": len(sel),
            "days": len({r["date"] for r in sel}),
            "sides": dict(sides),
            "hit": round(100 * sum(1 for r in sel if r["exc"] > 0) / len(sel), 1),
            "dir_ev": _r(dir_ev),
            "dir_ev_ci95": _clustered_ci(dir_pairs),
            "flow_alpha": _r(alpha),
            "flow_alpha_ci95": alpha_ci,
            "selection_component": _r(dir_ev - alpha),
            "verdict": _book_verdict(alpha, alpha_ci),
        }
    out["note"] = (
        "Direction-signed +5-session stock excess vs SPY, one row per alert. "
        "flow_alpha = each alert's return net of that day's flagged-universe "
        "drift (mean excess of the unique flagged underlyings), signed in the "
        "book's direction. selection_component = dir_ev - flow_alpha. CIs "
        "are clustered by flag date."
    )
    return out


# ── loser model (walk-forward) ───────────────────────────────────────────


def _odds_model(train):
    cut = sorted(r["exc"] for r in train)[int(len(train) * CFG["disaster_q"])]
    dis = [r for r in train if r["exc"] <= cut]
    n_d, n_a = len(dis), len(train)
    fd, fa = defaultdict(int), defaultdict(int)
    for r in train:
        for f in r["f"]:
            fa[f] += 1
    for r in dis:
        for f in r["f"]:
            fd[f] += 1

    def score(r):
        s = 0.0
        for f in r["f"]:
            pd = (fd[f] + 1) / (n_d + 2)
            pa = (fa[f] + 1) / (n_a + 2)
            s += (pd / pa) - 1.0  # >0 = disaster-enriched feature
        return s

    return score


def loser_decision(lm):
    """Plain verdict for the loser model, from its own numbers."""
    if lm.get("status") != "active":
        return "ACCRUING — %s" % lm.get("needs", "not enough walk-forward weeks yet")
    k, n = lm["weeks_improved"], lm["weeks_total"]
    base, kept = lm["ev_base"], lm["ev_after_exclusion"]
    facts = (
        "improved %d/%d OOS weeks; pooled EV %s%% -> %s%% after excluding the predicted worst %d%%"
        % (
            k,
            n,
            _fmt(base),
            _fmt(kept),
            int(CFG["drop_frac"] * 100),
        )
    )
    if kept > base and k >= CFG["pass_week_share"] * n:
        return "CANDIDATE — " + facts + "; review only, not applied"
    return "REJECTED — " + facts + "; never applied"


def loser_model(rows):
    per_week, base_all, kept_all = [], [], []
    for week, train, test in walk_forward(rows):
        if len(train) < CFG["min_train"] or len(test) < CFG["min_test"]:
            continue
        sc = _odds_model(train)
        ranked = sorted(test, key=sc, reverse=True)
        kept = ranked[int(len(ranked) * CFG["drop_frac"]) :]
        per_week.append(
            {
                "week": week,
                "n_train": len(train),
                "n": len(test),
                "ev_base": round(_mean([r["exc"] for r in test]), 3),
                "ev_kept": round(_mean([r["exc"] for r in kept]), 3),
            }
        )
        base_all += [r["exc"] for r in test]
        kept_all += [r["exc"] for r in kept]
    if not per_week:
        out = {
            "status": "accruing",
            "weeks": len({r["week"] for r in rows}),
            "needs": "3+ ISO weeks with %d+ training rows (after the %d-session embargo) and %d+ test rows"
            % (CFG["min_train"], EMBARGO_SESSIONS, CFG["min_test"]),
        }
    else:
        out = {
            "status": "active",
            "oos_weeks": per_week,
            "ev_base": round(_mean(base_all), 3),
            "ev_after_exclusion": round(_mean(kept_all), 3),
            "kept_frac": round(1 - CFG["drop_frac"], 2),
            "weeks_improved": sum(1 for w in per_week if w["ev_kept"] > w["ev_base"]),
            "weeks_total": len(per_week),
            "pass_rule": "pooled EV after exclusion > baseline AND improved in >= %d%% of OOS weeks"
            % int(CFG["pass_week_share"] * 100),
            "note": (
                "Categorical odds model trained on prior weeks only (%d-session "
                "embargo before each test week); excludes the predicted-worst %d%% "
                "of each OOS week." % (EMBARGO_SESSIONS, int(CFG["drop_frac"] * 100))
            ),
        }
    out["decision"] = loser_decision(out)
    return out


# ── empirical-Bayes score2 + OOS IC comparison ───────────────────────────


def _eb_deltas(train):
    ev_all = _mean([r["exc"] for r in train])
    by_f = defaultdict(list)
    for r in train:
        for f in r["f"]:
            by_f[f].append(r["exc"])
    K = CFG["shrink_k"]
    return {f: (len(v) / (len(v) + K)) * (_mean(v) - ev_all) for f, v in by_f.items()}, ev_all


def score2_decision(s2):
    if s2.get("status") != "active":
        return "ACCRUING — not enough walk-forward weeks with a measurable IC yet"
    ics = [w["ic_score2"] for w in s2["weekly"]]
    pos = sum(1 for v in ics if v > 0)
    avg, old = s2["avg_ic_score2"], s2["avg_ic_ledger_score"]
    facts = "avg OOS IC %+.3f, positive in %d/%d weeks, vs %s for the ledger score" % (
        avg,
        pos,
        len(ics),
        "%+.3f" % old if old is not None else "n/a",
    )
    if avg > 0 and pos >= CFG["pass_week_share"] * len(ics) and (old is None or avg > old):
        return "CANDIDATE — " + facts + "; not applied to live ranking"
    return "REJECTED for ranking — " + facts


def score2_eval(rows):
    ics = []
    for week, train, test in walk_forward(rows):
        if len(train) < CFG["min_train"] or len(test) < CFG["min_test"]:
            continue
        deltas, _ = _eb_deltas(train)
        s2 = [sum(deltas.get(f, 0.0) for f in r["f"]) for r in test]
        y = [r["exc"] for r in test]
        ic = _spearman(s2, y)
        if ic is not None:
            ics.append(
                {
                    "week": week,
                    "n": len(test),
                    "ic_score2": ic,
                    "ic_ledger_score": _spearman([r["score"] for r in test], y),
                }
            )
    if not ics:
        out = {"status": "accruing"}
    else:
        old = [w["ic_ledger_score"] for w in ics if w["ic_ledger_score"] is not None]
        out = {
            "status": "active",
            "weekly": ics,
            "avg_ic_score2": round(_mean([w["ic_score2"] for w in ics]), 4),
            "avg_ic_ledger_score": round(_mean(old), 4) if old else None,
            "pass_rule": "avg OOS IC > 0, positive in >= %d%% of weeks, and above the ledger score's"
            % int(CFG["pass_week_share"] * 100),
            "note": (
                "score2 = shrunken sum of per-feature EV deltas (K=%d), trained on "
                "prior weeks only with a %d-session embargo." % (CFG["shrink_k"], EMBARGO_SESSIONS)
            ),
        }
    out["decision"] = score2_decision(out)
    return out


# ── stability + PSI ──────────────────────────────────────────────────────


def _psi(recent, prior, feats=lambda r: r["f"]):
    """(total, per-feature contributions) population stability index."""
    fr, fp = defaultdict(int), defaultdict(int)
    for r in recent:
        for f in feats(r):
            fr[f] += 1
    for r in prior:
        for f in feats(r):
            fp[f] += 1
    total, parts = 0.0, []
    for f in sorted(set(fr) | set(fp)):
        a = max(fr[f] / len(recent), 1e-4)
        b = max(fp[f] / len(prior), 1e-4)
        c = (a - b) * math.log(a / b)
        total += c
        parts.append(
            {
                "feature": f,
                "psi": round(c, 4),
                "share_recent": round(a, 3),
                "share_prior": round(b, 3),
            }
        )
    return total, parts


def _recent_prior(rows):
    weeks = sorted({r["week"] for r in rows})
    last2 = set(weeks[-2:])
    return [r for r in rows if r["week"] in last2], [r for r in rows if r["week"] not in last2]


def stability(rows):
    byw = defaultdict(list)
    for r in rows:
        byw[r["week"]].append(r)
    side_ev = []
    for w in sorted(byw):
        e = {"week": w, "n": len(byw[w])}
        for book in BOOK_ORDER:
            v = [r["exc"] for r in byw[w] if r["book"] == book]
            if len(v) >= 30:
                e[book] = round(_mean(v), 2)
        side_ev.append(e)
    recent, prior = _recent_prior(rows)
    psi = None
    if len(recent) > 500 and len(prior) > 500:
        psi = round(_psi(recent, prior)[0], 4)
    return {
        "weekly_side_ev": side_ev,
        "psi_last2w_vs_prior": psi,
        "psi_note": "PSI <0.1 stable · 0.1-0.25 shifting · >0.25 major drift",
    }


def psi_detail(rows, capmap=None):
    """Per-feature PSI contributions + the deployment gate. Publishes raw PSI
    and a data-quality-ADJUSTED PSI (unknown caps remapped to the ticker's
    later-known cap, since early-history 'unknown' caps were metadata-cache
    misses). The gate keys on the raw number."""
    recent, prior = _recent_prior(rows)
    if len(recent) < 500 or len(prior) < 500:
        return {"status": "accruing", "promotion_blocked": False}
    total, contribs = _psi(recent, prior)
    contribs.sort(key=lambda x: -x["psi"])
    adj_total = None
    if capmap:

        def fixed(r):
            return [
                ("cap:" + capmap.get(r["ticker"], "unknown")) if f == "cap:unknown" else f
                for f in r["f"]
            ]

        adj_total = round(_psi(recent, prior, fixed)[0], 4)
    return {
        "status": "active",
        "psi_total": round(total, 4),
        "psi_adjusted": adj_total,
        "promotion_blocked": total >= 0.25,
        "gate": "PSI >= 0.25 BLOCKS automatic challenger promotion (deployment gate, not just an alarm)",
        "top_contributors": contribs[:6],
        "ev_before_drift": round(_mean([r["exc"] for r in prior]), 3),
        "ev_after_drift": round(_mean([r["exc"] for r in recent]), 3),
    }


# ── guarded interaction search (depth <= 3) ──────────────────────────────


def interactions(rows):
    n = len(rows)
    if n < 5000:
        return {"status": "accruing", "n": n, "tested": 0, "survivors": []}
    dev, hold, embargoed = embargo_split(rows, CFG["holdout_frac"])
    if len(dev) < 1000 or len(hold) < 500:
        return {"status": "accruing", "n": n, "tested": 0, "survivors": []}
    ev_dev = _mean([r["exc"] for r in dev])
    ev_hold = _mean([r["exc"] for r in hold])
    # feature -> row indices, so a combo's cohort is a set intersection
    idx_dev, idx_hold = defaultdict(set), defaultdict(set)
    for i, r in enumerate(dev):
        for f in r["f"]:
            idx_dev[f].add(i)
    for i, r in enumerate(hold):
        for f in r["f"]:
            idx_hold[f].add(i)
    feats = sorted(idx_dev)
    survivors, tested = [], 0
    for depth in (2, 3):
        for combo in combinations(feats, depth):
            if len({_dim(c) for c in combo}) != depth:
                continue  # side:x + side:y can't co-occur
            sel_i = set.intersection(*(idx_dev[c] for c in combo))
            if len(sel_i) < CFG["min_cohort_n"]:
                continue
            tested += 1
            sel = [dev[i]["exc"] for i in sorted(sel_i)]  # time order
            ev = _mean(sel)
            lift = ev - ev_dev
            if abs(lift) < 0.5:
                continue
            # gate 1: split-half (by time) sign agreement
            h = len(sel) // 2
            a, b = _mean(sel[:h]), _mean(sel[h:])
            if a is None or b is None or (a - ev_dev) * (b - ev_dev) <= 0:
                continue
            # gate 2: embargoed time-ordered holdout, same sign, >= 0.3pp
            hsel = [hold[i]["exc"] for i in set.intersection(*(idx_hold[c] for c in combo))]
            if len(hsel) < 100:
                continue
            hlift = _mean(hsel) - ev_hold
            if hlift * lift <= 0 or abs(hlift) < 0.3:
                continue
            survivors.append(
                {
                    "combo": list(combo),
                    "n_dev": len(sel),
                    "ev_dev": round(ev, 3),
                    "lift_dev": round(lift, 3),
                    "n_holdout": len(hsel),
                    "lift_holdout": round(hlift, 3),
                }
            )
    survivors.sort(key=lambda s: -abs(s["lift_holdout"]))
    return {
        "status": "active",
        "tested": tested,
        "n_dev": len(dev),
        "n_holdout": len(hold),
        "embargoed": embargoed,
        "survivors_total": len(survivors),
        "survivors": survivors[:12],
        "note": (
            "Depth<=3 categorical interactions with n>=%d in the development "
            "slice; gates: split-half sign agreement + same-sign lift >=0.3pp "
            "in the last %d%% of sessions (held out after a %d-session gap). "
            "With %d combinations tested, some survivors will be luck — review "
            "candidates only, nothing auto-applies."
            % (CFG["min_cohort_n"], int(CFG["holdout_frac"] * 100), EMBARGO_SESSIONS, tested)
        ),
    }


# ── pre-registered avoidance pocket: puts bought, 22-60 DTE ──────────────


def _in_pocket(r, lo=22, hi=60):
    return r["book"] == "put_buy" and r["dte"] is not None and lo <= r["dte"] <= hi


def adverse_episodes(spy1):
    """Adverse-market episodes in the sample, hedge_monitor's frozen
    definition: a session whose SPY close-to-close return is <= -1.0%;
    adverse sessions within 2 sessions of each other are ONE episode.
    spy1 maps a flag date to SPY's return over the next session."""
    sessions = sorted(spy1)
    day_ret = {sessions[j + 1]: spy1[sessions[j]] for j in range(len(sessions) - 1)}
    pos = {d: i for i, d in enumerate(sessions)}
    episodes = []
    for d in sorted(day_ret):
        if day_ret[d] > -1.0:
            continue
        if episodes and pos[d] - pos[episodes[-1][-1]] <= 2:
            episodes[-1].append(d)
        else:
            episodes.append([d])
    return episodes


def hedge_utility(sel, rows, spy5, spy1):
    """Is the pocket poor alpha, insurance, or both? Dependence-aware CIs,
    EV conditional on SPY's own +5d move, payoff per unit of carry, and
    daily-portfolio variants."""
    rnd = random.Random(42)
    out = {}
    out["uncertainty"] = {
        "trade_level_ci95": _ci95([r["exc"] for r in sel]),
        "clustered_by_date": _clustered_ci([(r["date"], r["exc"]) for r in sel]),
        "clustered_by_ticker_date": _clustered_ci(
            [((r["ticker"], r["date"]), r["exc"]) for r in sel]
        ),
    }
    byw = defaultdict(list)
    for r in sel:
        byw[r["week"]].append(r["exc"])
    weeks = list(byw.values())
    if len(weeks) >= 4:
        boots = []
        for _ in range(2000):
            samp = [weeks[rnd.randrange(len(weeks))] for _ in range(len(weeks))]
            boots.append(_mean([x for w in samp for x in w]))
        boots.sort()
        out["uncertainty"]["week_block_bootstrap"] = {
            "weeks": len(weeks),
            "ci95": [
                round(boots[int(0.025 * len(boots))], 3),
                round(boots[int(0.975 * len(boots))], 3),
            ],
        }
    pocket_ids = {id(r) for r in sel}
    rest = [r for r in rows if id(r) not in pocket_ids]

    def cond(pred):
        v = [r["exc"] for r in sel if r["date"] in spy5 and pred(spy5[r["date"]])]
        b = [r["exc"] for r in rest if r["date"] in spy5 and pred(spy5[r["date"]])]
        if len(v) < 30:
            return {"n": len(v), "status": "accruing"}
        return {
            "n": len(v),
            "days": len({r["date"] for r in sel if r["date"] in spy5 and pred(spy5[r["date"]])}),
            "pocket_ev": round(_mean(v), 3),
            "rest_ev": round(_mean(b), 3) if len(b) >= 30 else None,
        }

    up, down = cond(lambda s: s >= 0), cond(lambda s: s < 0)
    episodes = adverse_episodes(spy1)
    out["conditional"] = {
        "spy5_up_or_flat": up,
        "spy5_down": down,
        "spy5_down_2pct_plus": cond(lambda s: s <= -2),
    }
    out["adverse_episodes"] = {
        "count": len(episodes),
        "first_sessions": [ep[0] for ep in episodes],
        "definition": "SPY session return <= -1.0%; adverse sessions within 2 sessions of each other are one episode",
    }
    carry, payoff = up.get("pocket_ev"), down.get("pocket_ev")
    out["carry_ev"] = carry
    out["payoff_ev"] = payoff
    # payoff per unit of carry only means something when the pocket COSTS
    # money in up/flat markets and PAYS in down markets
    out["payoff_per_unit_carry"] = (
        round(payoff / -carry, 2)
        if carry is not None and payoff is not None and carry < 0 and payoff > 0
        else None
    )

    def variant(wfn):
        D = defaultdict(list)
        for r in rows:
            D[r["date"]].append(r["exc"] * wfn(r))
        ser = [_mean(v) for _, v in sorted(D.items())]
        if len(ser) < 2:
            return None
        mu = _mean(ser)
        sd = (sum((x - mu) ** 2 for x in ser) / (len(ser) - 1)) ** 0.5
        return {
            "avg_daily_ev": round(mu, 3),
            "sharpe_daily": round(mu / sd, 3) if sd else None,
            "cvar5_daily": round(_mean(sorted(ser)[: max(1, len(ser) // 20)]), 2),
        }

    out["portfolio_variants"] = {
        "pocket_1.0x": variant(lambda r: 1.0),
        "pocket_excluded": variant(lambda r: 0.0 if id(r) in pocket_ids else 1.0),
        "pocket_0.2x": variant(lambda r: 0.2 if id(r) in pocket_ids else 1.0),
    }
    out["premium_observed_musd"] = round(sum(r.get("premium") or 0 for r in sel) / 1e6)
    return out


def confirmation_window(sel, spy5):
    """The pre-registered alpha gates, evaluated on pocket alerts flagged
    AFTER PREREG_DATE (unseen when the hypothesis was frozen)."""
    win = [r for r in sel if r["date"] > PREREG_DATE]
    pairs = [(r["date"], r["exc"]) for r in win]
    ci = _clustered_ci(pairs)
    ev = _mean([r["exc"] for r in win])
    dates = {r["date"] for r in win}
    adverse = sorted(d for d in dates if spy5.get(d) is not None and spy5[d] <= -1.0)
    gates = {
        "weeks_ge_4": len({r["week"] for r in win}) >= 4,
        "dates_ge_15": len(dates) >= 15,
        "adverse_period": bool(adverse),
        "clustered_ci_excludes_0": not _spans_zero(ci),
        "pooled_ev_negative": ev is not None and ev < 0,
    }
    return {
        "since": PREREG_DATE,
        "n": len(win),
        "weeks": len({r["week"] for r in win}),
        "dates": len(dates),
        "adverse_dates": len(adverse),
        "ev": _r(ev),
        "clustered_ci95": ci,
        "gates": gates,
    }


def alpha_verdict(cw):
    """Directional-alpha (avoid this pocket) verdict from the window."""
    g = cw["gates"]
    if not (g["weeks_ge_4"] and g["dates_ge_15"]) or cw["n"] < 30:
        return (
            "ACCRUING",
            "%d alerts over %d weeks / %d dates since %s; the gate needs >=4 weeks and >=15 dates"
            % (
                cw["n"],
                cw["weeks"],
                cw["dates"],
                cw["since"],
            ),
        )
    facts = "EV %s%% (date-clustered 95%% CI %s) over %d alerts, %d dates since %s" % (
        _fmt(cw["ev"]),
        _fmt_ci(cw["clustered_ci95"]),
        cw["n"],
        cw["dates"],
        cw["since"],
    )
    if not g["pooled_ev_negative"]:
        return (
            "NOT CONFIRMED",
            facts + " — the pocket did not lose money, so there is nothing to avoid",
        )
    if not g["clustered_ci_excludes_0"]:
        return "NOT CONFIRMED", facts + " — negative, but the CI spans zero"
    if not g["adverse_period"]:
        return "ACCRUING", facts + " — no adverse-market period (SPY 5d <= -1%) in the window yet"
    return "CONFIRMED", facts + " — every pre-registered alpha gate passed; still not auto-applied"


def hedge_verdict(hu):
    eps = hu["adverse_episodes"]["count"]
    carry, payoff, ratio = hu.get("carry_ev"), hu.get("payoff_ev"), hu.get("payoff_per_unit_carry")
    if carry is None or payoff is None:
        return "ACCRUING", "not enough pocket alerts in up and down markets yet"
    facts = (
        "pocket EV %s%% when SPY's 5-day return was >= 0 and %s%% when it was < 0; %d adverse episode(s) in sample"
        % (
            _fmt(carry),
            _fmt(payoff),
            eps,
        )
    )
    if payoff <= 0:
        return "NOT A HEDGE", facts + " — it did not pay in down markets"
    if carry >= 0:
        return (
            "NOT A HEDGE PROFILE",
            facts + " — it cost nothing in up markets, so payoff per unit of carry is undefined",
        )
    if eps < 2:
        return (
            "ACCRUING",
            facts + "; payoff/carry %.2f, but the gate needs >=2 adverse episodes" % ratio,
        )
    if ratio is not None and ratio >= 1.0:
        return (
            "PASSES IN STOCK TERMS",
            facts + "; payoff/carry %.2f >= 1.0 before option costs — the cost-inclusive "
            "test is hedge_monitor's" % ratio,
        )
    return "FAILS", facts + "; payoff/carry %.2f < 1.0" % (ratio or 0)


def avoidance_candidate(rows, spy, tested):
    """The puts-bought 22-60 DTE pocket, pre-registered on PREREG_DATE as an
    avoidance candidate (alpha mandate) and a potential hedge (separate
    mandate). Every verdict below is computed from tonight's numbers."""
    spy5, spy1 = spy.get(5, {}), spy.get(1, {})
    sel = [r for r in rows if _in_pocket(r)]
    definition = "puts bought (classified put_buyer) AND 22 <= DTE <= 60"
    if len(sel) < 200:
        return {"status": "accruing", "definition": definition, "n": len(sel)}
    byw = defaultdict(list)
    for r in sel:
        byw[r["week"]].append(r["exc"])
    h = len(sel) // 2
    _, hold, _ = embargo_split(sel, CFG["holdout_frac"])
    by_liq = {}
    for lg in ("A", "B", "C", "D"):
        v = [r["exc"] for r in sel if r["liq"] == lg]
        if len(v) >= 50:
            by_liq[lg] = {"n": len(v), "ev": round(_mean(v), 3)}
    tk, sec = defaultdict(int), defaultdict(int)
    for r in sel:
        tk[r["ticker"]] += 1
        sec[r["sector"]] += 1
    sens = {}
    for lo, hi in ((8, 21), (15, 45), (22, 60), (30, 75), (45, 90)):
        v = [r["exc"] for r in rows if _in_pocket(r, lo, hi)]
        if len(v) >= 100:
            sens["dte%d-%d" % (lo, hi)] = {"n": len(v), "ev": round(_mean(v), 3)}
    hu = hedge_utility(sel, rows, spy5, spy1)
    cw = confirmation_window(sel, spy5)
    a_status, a_text = alpha_verdict(cw)
    h_status, h_text = hedge_verdict(hu)
    pairs = [(r["date"], r["exc"]) for r in sel]
    return {
        "status": "shadow",
        "label": "Alpha (avoid): %s · Hedge: %s" % (a_status, h_status),
        "mandates": {
            "directional_alpha": {"status": a_status, "verdict": a_text},
            "hedge_sleeve": {"status": h_status, "verdict": h_text},
        },
        "definition": definition,
        "definition_note": (
            "Tightened 2026-11 to classified put buyers; before, every put that "
            "was not classified as sold (mixed / unknown prints too) was in it."
        ),
        "n": len(sel),
        "days": len({r["date"] for r in sel}),
        "ev_pooled": round(_mean([r["exc"] for r in sel]), 3),
        "ci95_clustered_by_date": _clustered_ci(pairs),
        "ci95_trade_level": _ci95([r["exc"] for r in sel]),
        "weekly": [
            {"week": w, "n": len(v), "ev": round(_mean(v), 3)} for w, v in sorted(byw.items())
        ],
        "split_half": [
            round(_mean([r["exc"] for r in sel[:h]]), 3),
            round(_mean([r["exc"] for r in sel[h:]]), 3),
        ],
        "holdout_last30pct": {"n": len(hold), "ev": _r(_mean([r["exc"] for r in hold]))},
        "by_liquidity": by_liq,
        "units": (
            "Every EV is the stock's +5-session excess return vs SPY in the "
            "flow's direction (percent of the stock price). Option spreads and "
            "premium are in different units and are not netted here."
        ),
        "concentration": {
            "top_tickers": [
                {"t": t, "n": n2, "share": round(n2 / len(sel), 3)}
                for t, n2 in sorted(tk.items(), key=lambda x: -x[1])[:5]
            ],
            "top_sectors": [
                {"s": s2, "share": round(n2 / len(sel), 3)}
                for s2, n2 in sorted(sec.items(), key=lambda x: -x[1])[:3]
            ],
            "into_earnings_share": round(sum(1 for r in sel if r["ern"]) / len(sel), 3),
        },
        "dte_boundary_sensitivity": sens,
        "hedge_utility": hu,
        "confirmation_window": cw,
        "multiple_testing": (
            "Selected from an interaction search (tonight's search tested %d "
            "combinations); the holdout gate mitigates but does not remove "
            "selection bias, hence the pre-registered confirmation window." % tested
        ),
        "preregistered_confirmation": {
            "frozen": PREREG_DATE
            + " (hypothesis + thresholds frozen before the window was observed)",
            "alpha_mandate_gates": [
                ">=4 unseen ISO weeks",
                ">=15 independent trade DATES in window",
                ">=1 adverse-market period (SPY 5d fwd <= -1%) represented in window",
                "date-CLUSTERED 95% CI excludes 0 (not trade-level)",
                "pooled EV < 0",
            ],
            "hedge_mandate_gates": [
                ">=2 distinct adverse episodes observed (insurance cannot be judged on one event)",
                "payoff_per_unit_carry >= 1.0",
                "evaluated as a separate sleeve, never via the alpha exclusion rule",
            ],
        },
    }


# ── deployment status (generated) ────────────────────────────────────────


def _oos_window(*sections):
    weeks = sorted(
        {
            w["week"]
            for s in sections
            for w in (s.get("oos_weeks") or s.get("weekly") or [])
            if isinstance(w, dict) and "week" in w
        }
    )
    return "%s..%s (%d weeks)" % (weeks[0], weeks[-1], len(weeks)) if weeks else "none yet"


def _status_word(decision):
    """'REJECTED — ...' -> 'REJECTED'."""
    return decision.split(" ", 1)[0] if decision else "ACCRUING"


def deployment_table(payload, generated):
    """Production status of every finding, built from tonight's sections."""
    lm, s2 = payload["loser_model"], payload["score2"]
    psi, ia, av = payload["psi_detail"], payload["interactions"], payload["avoidance_candidate"]
    sample = payload["sample"]
    span = sample.get("span") or ["?", "?"]
    if psi.get("status") == "active":
        psi_ev = "PSI %.3f now (%s)" % (
            psi["psi_total"],
            "promotion blocked" if psi["promotion_blocked"] else "below the 0.25 block",
        )
    else:
        psi_ev = "accruing"
    if av.get("status") == "shadow":
        av_ev = "alpha: %s · hedge: %s" % (
            av["mandates"]["directional_alpha"]["verdict"],
            av["mandates"]["hedge_sleeve"]["verdict"],
        )
    else:
        av_ev = "accruing (n=%d)" % av.get("n", 0)
    rows = [
        {
            "finding": "Walk-forward evaluation (weekly, %d-session embargo)" % EMBARGO_SESSIONS,
            "evidence": "%d graded alerts, %s to %s" % (sample["graded"], span[0], span[1]),
            "production": "LIVE",
            "gate": "n/a",
            "window": _oos_window(lm, s2),
            "rollback": "remove the momentum.yml step",
        },
        {
            "finding": "Drift monitoring (PSI + per-feature)",
            "evidence": psi_ev,
            "production": "LIVE",
            "gate": "PSI>=0.25 blocks challenger promotion",
            "window": "last 2 ISO weeks vs prior",
            "rollback": "n/a (read-only)",
        },
        {
            "finding": "Puts-bought 22-60 DTE pocket",
            "evidence": av_ev,
            "production": "SHADOW",
            "gate": (
                "alpha: 4 unseen wks + 15 trade dates + adverse period + clustered CI "
                "excl 0 + EV<0 · hedge: >=2 adverse episodes + payoff/carry >= 1"
            ),
            "window": "since " + PREREG_DATE,
            "rollback": "n/a (not applied)",
        },
        {
            "finding": "score2 ranking",
            "evidence": s2["decision"],
            "production": _status_word(s2["decision"]),
            "gate": s2.get("pass_rule") or "needs walk-forward weeks",
            "window": _oos_window(s2),
            "rollback": "n/a (never applied)",
        },
        {
            "finding": "Loser model (categorical odds)",
            "evidence": lm["decision"],
            "production": _status_word(lm["decision"]),
            "gate": lm.get("pass_rule") or "needs walk-forward weeks",
            "window": _oos_window(lm),
            "rollback": "n/a (never applied)",
        },
        {
            "finding": "Interaction search (depth <= 3)",
            "evidence": "%d tested, %d passed both gates"
            % (ia.get("tested", 0), ia.get("survivors_total", len(ia.get("survivors") or []))),
            "production": "REVIEW ONLY",
            "gate": "split-half sign + embargoed holdout",
            "window": "last %d%% of sessions held out" % int(CFG["holdout_frac"] * 100),
            "rollback": "n/a (not applied)",
        },
        {
            "finding": "Side-aware learner weights",
            "evidence": "computed nightly by uoa_alpha, skipped in the live score (uoa_scanner.edge_adjust); not evaluated by this engine",
            "production": "SHADOW",
            "gate": ">=4 unseen wks · pooled lift >=+0.15pp · better >=60% wks · PSI<0.25 at promotion",
            "window": "prospective",
            "rollback": "side: keys skipped in edge_adjust (current)",
        },
        {
            "finding": "score2-quintile sizing table",
            "evidence": (
                "removed 2026-11: it divided a 5-day EV by an unsigned 20-day stock "
                "drawdown and was not a Kelly fraction"
            ),
            "production": "RETIRED",
            "gate": "n/a",
            "window": "n/a",
            "rollback": "n/a",
        },
    ]
    return {
        "as_of": generated[:10],
        "rows": rows,
        "psi_promotion_blocked_now": bool(psi.get("promotion_blocked")),
    }


# ── payload ──────────────────────────────────────────────────────────────


def build_payload(rows, drift_rows, sample, spy, capmap):
    generated = datetime.now(UTC).isoformat(timespec="seconds")
    ia = interactions(rows)
    payload = {
        "generated": generated,
        "version": 2,
        "sample": sample,
        "attribution": attribution(rows, drift_rows),
        "loser_model": loser_model(rows),
        "score2": score2_eval(rows),
        "stability": stability(rows),
        "psi_detail": psi_detail(rows, capmap),
        "interactions": ia,
        "avoidance_candidate": avoidance_candidate(rows, spy, ia.get("tested", 0)),
        "pit_safety": {
            "side_feature": (
                "point-in-time safe: the side is the scanner's aggressor "
                "classification (flow_side) logged at flag time from the "
                "day-so-far prints, plus the contract symbol. No later prints, "
                "future OI or outcomes enter any feature."
            ),
        },
        "regime_conditioning_design": {
            "status": "DESIGN ONLY — not activated",
            "regimes": (
                "existing regime_history labels (breadth 58/42 thresholds) — "
                "point-in-time observable, definitions FROZEN as of 2026-07-06"
            ),
            "gates": (
                "min 150 matured/regime AND >=40 labeled days; UNKNOWN regime "
                "falls back to global weights; regime weights shrink toward "
                "global by n/(n+200); walk-forward evaluation vs the "
                "unconditioned incumbent required before activation"
            ),
        },
        "note": (
            "Nightly alpha-research engine over the full ledger. One row per "
            "alert (a contract's first flag of the ET day); stock excess vs SPY "
            "at +5 sessions in the flow's direction, classified aggressor sides "
            "only. OOS claims are walk-forward by ISO week with a 5-session "
            "embargo. Nothing here auto-applies to live ranking. Educational, "
            "not advice."
        ),
    }
    payload["deployment"] = deployment_table(payload, generated)
    return payload


def run(scored=None):
    if scored is None:
        _, scored = ua.compute_edge()
    rows, drift_rows, sample, spy, capmap = build_rows(scored)
    if len(rows) < 1000:
        print("  research: only %d graded alerts — skipping" % len(rows))
        return None
    payload = build_payload(rows, drift_rows, sample, spy, capmap)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    books = payload["attribution"]["books"]
    print(
        "  research: %d graded alerts (%s..%s, %d unclassified) · "
        % (sample["graded"], sample["span"][0], sample["span"][1], sample["unclassified"])
        + " · ".join(
            "%s EV %s alpha %s" % (b, books[b].get("dir_ev"), books[b].get("flow_alpha"))
            for b in BOOK_ORDER
            if b in books
        )
    )
    print("  loser model: " + payload["loser_model"]["decision"])
    print("  score2: " + payload["score2"]["decision"])
    print("  pocket: " + payload["avoidance_candidate"].get("label", "accruing"))
    print("  Wrote uoa_research.json")
    return payload


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    run()
