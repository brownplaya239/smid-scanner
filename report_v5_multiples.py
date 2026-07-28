#!/usr/bin/env python3
"""report_v5_multiples.py — the historical-multiples engine (v5 slice 1).

The scenario table's bear/base/bull multiples need an anchor that is not
today's price (the v4 B4 circularity lesson). The anchor here is the
name's OWN trailing-multiple distribution: for every completed session in
the window, the trailing P/E (and EV/TTM-revenue) as it could actually
have been computed ON THAT DATE, then the P25/P50/P75 of that series.

POINT-IN-TIME RULE (the whole point of this module)
    A filed fact becomes usable the session AFTER its filing date. The
    trailing EPS used at date d is built only from quarters whose filing
    date is strictly before d — not from the fiscal periods they
    describe. A 10-Q filed 2026-07-23 contributes nothing to the multiple
    computed for 2026-07-23 itself; the market that day was still pricing
    the prior quarter's knowledge. Where a quarter was later restated,
    the AS-FIRST-REPORTED value is used, because that is what was known.

Q4 is never filed as a quarter: it becomes known when the 10-K is filed,
as FY minus the three 10-Q quarters, and its availability date is the
10-K's filing date — the same derivation v4's fundamentals use, made
point-in-time.

Everything returned is DER: window, coverage, exclusions and the exact
percentile values, with the inputs traceable to XBRL accessions.

    python report_v5_multiples.py NOW [--years 3]
"""

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WINDOW_YEARS = 3          # regime-relevant beats stale (design call (a))
MIN_COVERAGE = 0.60       # fraction of window sessions with a computable
                          # multiple below which the band is withheld
QTR_DAYS = (80, 100)      # duration bounds for one fiscal quarter
ANN_DAYS = (350, 380)     # ... and one fiscal year
MAX_TTM_SPAN = 380        # four "consecutive" quarters must fit in this


# ── point-in-time fact stream (pure; testable without a network) ──────

def _dt(s):
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def quarterly_events(rows):
    """XBRL duration facts -> [{end, val, available_from, accn}] for
    single-quarter facts, as first reported. available_from is the day
    AFTER the filing date."""
    seen = {}
    for r in rows:
        if r.get("form") not in ("10-Q", "10-K"):
            continue
        if r.get("val") is None or not r.get("start") or not r.get("end"):
            continue
        try:
            dur = (_dt(r["end"]) - _dt(r["start"])).days
        except ValueError:
            continue
        if not (QTR_DAYS[0] <= dur <= QTR_DAYS[1]):
            continue
        end = str(r["end"])
        filed = _dt(r.get("filed"))
        # as-first-reported: keep the EARLIEST filing that stated it
        if end not in seen or filed < seen[end]["_filed"]:
            seen[end] = {"end": end, "val": float(r["val"]),
                         "available_from": (filed + timedelta(days=1)
                                            ).isoformat(),
                         "accn": r.get("accn"), "_filed": filed}
    out = sorted(seen.values(), key=lambda x: x["end"])
    for o in out:
        o.pop("_filed", None)
    return out


def annual_events(rows):
    """Fiscal-year duration facts, as first reported."""
    seen = {}
    for r in rows:
        if r.get("form") != "10-K" or r.get("val") is None:
            continue
        if not r.get("start") or not r.get("end"):
            continue
        try:
            dur = (_dt(r["end"]) - _dt(r["start"])).days
        except ValueError:
            continue
        if not (ANN_DAYS[0] <= dur <= ANN_DAYS[1]):
            continue
        end = str(r["end"])
        filed = _dt(r.get("filed"))
        if end not in seen or filed < seen[end]["_filed"]:
            seen[end] = {"start": str(r["start"]), "end": end,
                         "val": float(r["val"]),
                         "available_from": (filed + timedelta(days=1)
                                            ).isoformat(),
                         "accn": r.get("accn"), "_filed": filed}
    out = sorted(seen.values(), key=lambda x: x["end"])
    for o in out:
        o.pop("_filed", None)
    return out


def derive_q4(quarters, annuals):
    """Q4 events: FY minus the three filed quarters inside that fiscal
    year, available when the 10-K was. Skipped when the year does not
    have exactly three interior quarters (fiscal change, gap)."""
    out = []
    for a in annuals:
        fy_start, fy_end = a["start"], a["end"]
        inside = [q for q in quarters if fy_start <= q["end"] < fy_end]
        if len(inside) != 3:
            continue
        out.append({"end": fy_end,
                    "val": round(a["val"] - sum(q["val"] for q in inside), 4),
                    "available_from": a["available_from"],
                    "accn": a["accn"], "derived": "fy_minus_3q"})
    return out


# Point-in-time integrity thresholds (v5.7 §1), named so reasons can
# cite them. A quarter-to-quarter gap outside [60, 130] days means the
# four ends are not four CONTIGUOUS fiscal quarters; a newest end more
# than 200 days before the valuation date means the "trailing year" is
# stale relative to that session (90-day quarter + normal filing lag,
# with headroom for 52/53-week calendars).
QTR_GAP_DAYS = (60, 130)
TTM_MAX_END_AGE = 200


def ttm_integrity(events, d):
    """The §1 record for the TTM as of date d: which quarters, whether
    they are contiguous, and whether the newest is current relative to
    d. ttm_at() enforces exactly these rules — this is the auditable
    form the validator and the grid disclose."""
    known = [e for e in events if _dt(e["available_from"]) <= d]
    by_end = {}
    for e in known:
        by_end.setdefault(e["end"], e)
    ends = sorted(by_end)[-4:]
    rec = {"quarters": ends, "as_of": d.isoformat(),
           "contiguous": None, "current": None, "ok": False,
           "reasons": []}
    if len(ends) < 4:
        rec["reasons"].append("only %d filed quarter(s) knowable"
                              % len(ends))
        return rec
    gaps = [(_dt(b) - _dt(a)).days for a, b in zip(ends, ends[1:])]
    rec["contiguous"] = all(QTR_GAP_DAYS[0] <= g <= QTR_GAP_DAYS[1]
                            for g in gaps)
    if not rec["contiguous"]:
        rec["reasons"].append("quarter ends are not contiguous "
                              "(gaps %s days)" % gaps)
    age = (d - _dt(ends[-1])).days
    rec["current"] = age <= TTM_MAX_END_AGE
    rec["newest_end_age_days"] = age
    if not rec["current"]:
        rec["reasons"].append("newest quarter end %s is %d days before "
                              "the valuation date (max %d) — the "
                              "concept's filed series has gone stale"
                              % (ends[-1], age, TTM_MAX_END_AGE))
    rec["ok"] = rec["contiguous"] and rec["current"]
    return rec


def ttm_at(events, d):
    """Sum of the last four known quarters as of date d (a date object),
    or None when four CONTIGUOUS, CURRENT quarters were not knowable —
    a gap or a dead concept must suppress the value, never produce a
    stale trailing year (v5.7 §1)."""
    known = [e for e in events if _dt(e["available_from"]) <= d]
    # newest fact per quarter-end
    by_end = {}
    for e in known:
        by_end.setdefault(e["end"], e)
    ends = sorted(by_end)[-4:]
    if len(ends) < 4:
        return None
    if (_dt(ends[-1]) - _dt(ends[0])).days > MAX_TTM_SPAN:
        return None                       # a gap, not a trailing year
    gaps = [(_dt(b) - _dt(a)).days for a, b in zip(ends, ends[1:])]
    if not all(QTR_GAP_DAYS[0] <= g <= QTR_GAP_DAYS[1] for g in gaps):
        return None                       # non-contiguous quarters
    if (d - _dt(ends[-1])).days > TTM_MAX_END_AGE:
        return None                       # stale relative to this date
    return sum(by_end[e]["val"] for e in ends)


# ── the band (pure) ───────────────────────────────────────────────────

def _pct(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return round(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo])
                 * (k - lo), 2)


def multiple_band(bars, events, years=WINDOW_YEARS, kind="pe"):
    """Daily trailing multiple over the window -> percentile band.

    bars   : [(date_iso, close)] completed sessions, oldest first
    events : quarterly (incl. derived Q4) per-share metric events
    Returns the band record, or a withheld record with the reason —
    never a number computed from insufficient coverage."""
    if not bars:
        return {"available": False, "reason": "no price history"}
    cutoff = _dt(bars[-1][0]) - timedelta(days=int(years * 365.25))
    window = [(d, c) for d, c in bars if _dt(d) >= cutoff]
    series, neg_days, no_ttm = [], 0, 0
    for d, close in window:
        ttm = ttm_at(events, _dt(d))
        if ttm is None:
            no_ttm += 1
            continue
        if ttm <= 0:
            neg_days += 1
            continue
        series.append(round(close / ttm, 3))
    n = len(series)
    coverage = n / len(window) if window else 0.0
    actual_years = (round(((_dt(window[-1][0]) - _dt(window[0][0])).days)
                          / 365.25, 1) if window else 0.0)
    base = {
        "kind": kind, "window_years": years,
        "actual_years": actual_years,
        "window_start": window[0][0] if window else None,
        "window_end": window[-1][0] if window else None,
        "sessions_in_window": len(window),
        "sessions_computable": n,
        "coverage": round(coverage, 3),
        "excluded_negative_ttm": neg_days,
        "excluded_no_ttm": no_ttm,
        "basis": ("daily close / trailing 4-quarter %s, each day using "
                  "only filings available before that session (as first "
                  "reported)" % ("EPS" if kind == "pe" else "revenue/share")),
    }
    if coverage < MIN_COVERAGE:
        base.update({"available": False,
                     "reason": "only %.0f%% of window sessions had a "
                               "computable positive trailing metric "
                               "(minimum %.0f%%)"
                               % (coverage * 100, MIN_COVERAGE * 100)})
        return base
    s = sorted(series)
    base.update({"available": True,
                 "p25": _pct(s, 0.25), "p50": _pct(s, 0.50),
                 "p75": _pct(s, 0.75),
                 "min": round(s[0], 2), "max": round(s[-1], 2),
                 "current": series[-1] if series else None})
    return base


# ── fetch wrapper (network) ───────────────────────────────────────────

def split_adjuster(split_rows):
    """-> adj(filed_date_iso) = cumulative split factor for facts filed
    before each split's execution date. Per-share values stated in an
    old basis divide by this; share counts multiply.

    The basis a filing used follows its FILING date, not the period it
    describes: a 10-K filed after a split restates per-share figures in
    the new basis retroactively, while the same fiscal year's 10-Qs, as
    first reported before the split, are in the old one. Missing this is
    exactly how ServiceNow's 5:1 (2025-12-18) turned the derived Q4 into
    FY(new basis) minus 3xQ(old basis) = a large negative, poisoning 64
    sessions of TTM."""
    evs = [(str(r.get("execution_date")),
            (r.get("split_to") or 1) / (r.get("split_from") or 1))
           for r in split_rows or [] if r.get("execution_date")]

    def adj(filed_iso):
        f = 1.0
        for ex, ratio in evs:
            if str(filed_iso) < ex:
                f *= ratio
        return f
    return adj


def _rebase(events, adj, per_share=True):
    """Events -> today's share basis. available_from is filed+1, so the
    filing date is recovered by stepping back a day."""
    out = []
    for e in events:
        filed = (_dt(e["available_from"]) - timedelta(days=1)).isoformat()
        f = adj(filed)
        v = e["val"] / f if per_share else e["val"] * f
        out.append({**e, "val": v})
    return out


def build(ticker, years=WINDOW_YEARS):
    """P/E and EV/TTM-revenue bands for one ticker. Bars are the same
    licensed, listing-date-truncated series every other v4/v5 figure
    uses; multiples derive from close prices, so the split-adjusted
    basis matches the split-adjusted per-share XBRL facts."""
    import research_live as RL
    cik = RL.cik_for_filed(ticker)[0]
    mk = RL.fetch_market(ticker)
    dates = [d.isoformat() for d in mk["dates"]]
    closes = mk["closes"]
    if mk.get("partial_session"):
        dates, closes = dates[:-1], closes[:-1]
    bars = list(zip(dates, closes))

    try:
        import polygon_data as PG
        adj = split_adjuster(PG.splits(ticker))
    except Exception:
        adj = split_adjuster([])

    eps_rows = RL.concept(cik, "EarningsPerShareDiluted", unit="USD/shares")
    # Rebase BEFORE deriving Q4, so FY-minus-3Q subtracts like from like.
    q = _rebase(quarterly_events(eps_rows), adj)
    ann = _rebase(annual_events(eps_rows), adj)
    q4 = derive_q4(q, ann)
    eps_events = sorted(q + q4, key=lambda x: x["end"])

    # Revenue per share for EV-free EV/S proxy: price / TTM-revenue-per-
    # share equals marketcap / TTM revenue under the same share count —
    # the honest form available without point-in-time debt history.
    # ONE concept per stream (§1): candidates are tried and the single
    # concept with the freshest filed series wins outright — events from
    # different concepts are never mixed into one TTM.
    rev_events, rev_concept = [], None
    for tag in ("RevenueFromContractWithCustomerExcludingAssessedTax",
                "Revenues",
                "RevenueFromContractWithCustomerIncludingAssessedTax"):
        rows = RL.concept(cik, tag)
        qq = quarterly_events(rows)
        if qq and (not rev_events
                   or max(e["end"] for e in qq)
                   > max(e["end"] for e in rev_events)):
            rev_events = sorted(qq + derive_q4(qq, annual_events(rows)),
                                key=lambda x: x["end"])
            rev_concept = tag
    sh_rows = RL.concept(cik, "WeightedAverageNumberOfDilutedSharesOutstanding",
                         unit="shares")
    sh_events = _rebase(quarterly_events(sh_rows), adj, per_share=False)
    rps_events = _per_share(rev_events, sh_events)

    return {
        "ticker": ticker.upper(),
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bar_source": mk.get("bar_source"),
        "pe": multiple_band(bars, eps_events, years, "pe"),
        "ps": multiple_band(bars, rps_events, years, "ps"),
        # Filed-history depth for the archetype router: distinct filed
        # quarterly periods, straight from the same event streams the
        # bands were computed on.
        "n_eps_quarters": len({e["end"] for e in eps_events}),
        "n_rev_quarters": len({e["end"] for e in rev_events}),
        "eps_events": eps_events[-10:],
        # §1 auditable TTM integrity as of the last completed session:
        # the quarters used, contiguity, and currency, per stream.
        "ttm_integrity": {
            "pe": dict(ttm_integrity(eps_events, _dt(dates[-1]))
                       if dates else {},
                       concept="us-gaap:EarningsPerShareDiluted"),
            "ps": dict(ttm_integrity(rev_events, _dt(dates[-1]))
                       if dates else {},
                       concept=("us-gaap:%s" % rev_concept)
                       if rev_concept else None),
        },
        "point_in_time_rule": "facts usable from the day after filing; "
                              "as-first-reported values; TTM requires "
                              "four contiguous, current quarters of one "
                              "concept",
    }


def _per_share(metric_events, share_events):
    """Divide a per-quarter dollar metric by that quarter's diluted share
    count. Quarters without a matching share fact are dropped (no shares,
    no per-share number)."""
    sh_by_end = {e["end"]: e for e in share_events}
    out = []
    for e in metric_events:
        sh = sh_by_end.get(e["end"])
        if not sh or not sh["val"]:
            continue
        out.append({"end": e["end"], "val": e["val"] / sh["val"],
                    "available_from": max(e["available_from"],
                                          sh["available_from"]),
                    "accn": e["accn"]})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--years", type=int, default=WINDOW_YEARS)
    a = ap.parse_args()
    rec = build(a.ticker, a.years)
    print(json.dumps({k: v for k, v in rec.items() if k != "eps_events"},
                     indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
