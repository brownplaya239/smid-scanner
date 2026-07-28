#!/usr/bin/env python3
"""report_v5_grid.py — the financial dashboard grid (v5 slice 4b).

The TPX-style left page: filed fiscal years side by side, a TTM column,
and the guidance the exhibit parser admitted. Rules from review:

* Render only the years that exist under a consistent fiscal basis —
  a 2021 IPO gets a shorter grid with the gap STATED once, never a
  backfilled column.
* Every dollar figure is as-filed (10-K annual facts, as first
  reported, split-rebased for per-share rows); derived rows (growth,
  margins, FCF) carry their formula.
* TTM = last four filed quarters from the same point-in-time event
  machinery the multiples engine uses.
"""

from datetime import datetime, timezone

import report_v5_multiples as M

MAX_YEARS = 5

ROWS = (
    # (key, label, kind)  kind: money | pct | pershare | derived-pct
    ("revenue", "Revenue", "money"),
    ("revenue_growth", "Revenue growth", "derived-pct"),
    ("gross_margin", "Gross margin", "derived-pct"),
    ("net_income", "Net income", "money"),
    ("net_margin", "Net margin", "derived-pct"),
    ("eps", "Diluted EPS", "pershare"),
    ("ocf", "Operating cash flow", "money"),
    ("fcf", "Free cash flow (OCF - capex)", "money"),
)

TAGS = {
    "revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax",
                "Revenues",
                "RevenueFromContractWithCustomerIncludingAssessedTax"),
    "gross_profit": ("GrossProfit",),
    "net_income": ("NetIncomeLoss",),
    "eps": ("EarningsPerShareDiluted",),
    "ocf": ("NetCashProvidedByUsedInOperatingActivities",),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment",),
}


def _annuals(RL, cik, tags, unit="USD", adj=None, per_share=False):
    """Latest-coverage tag's fiscal-year events, keyed by year end."""
    best = []
    for t in tags:
        rows = RL.concept(cik, t, unit=unit)
        ev = M.annual_events(rows)
        if ev and (not best or max(e["end"] for e in ev)
                   > max(e["end"] for e in best)):
            best = ev
    if adj:
        best = M._rebase(best, adj, per_share=per_share)
    return {e["end"]: e for e in best}


def _quarters(RL, cik, tags, unit="USD", adj=None, per_share=False):
    """Quarterly events with Q4 derived AFTER split rebasing — deriving
    first mixes bases across a split (the exact ServiceNow 5:1 failure
    the multiples engine fixed)."""
    best = []
    for t in tags:
        rows = RL.concept(cik, t, unit=unit)
        ev = M.quarterly_events(rows)
        ann = M.annual_events(rows)
        if adj:
            ev = M._rebase(ev, adj, per_share=per_share)
            ann = M._rebase(ann, adj, per_share=per_share)
        if ev and (not best or max(e["end"] for e in ev)
                   > max(e["end"] for e in best)):
            best = sorted(ev + M.derive_q4(ev, ann), key=lambda x: x["end"])
    return best


def _ytd_quarters(RL, cik, tags, unit="USD"):
    """Cash-flow statements file CUMULATIVE year-to-date figures, so a
    quarter is YTD(n) minus YTD(n-1) sharing the same fiscal-year start
    (Q1 = its own YTD). As-first-reported, available the day after the
    later filing."""
    from datetime import timedelta
    best = []
    for t in tags:
        rows = RL.concept(cik, t, unit=unit)
        seen = {}
        for r in rows:
            if r.get("form") not in ("10-Q", "10-K")                     or r.get("val") is None                     or not r.get("start") or not r.get("end"):
                continue
            key = (str(r["start"]), str(r["end"]))
            filed = M._dt(r.get("filed"))
            if key not in seen or filed < seen[key][1]:
                seen[key] = (float(r["val"]), filed)
        by_start = {}
        for (st, en), (val, filed) in seen.items():
            by_start.setdefault(st, []).append((en, val, filed))
        ev = []
        for st, items in by_start.items():
            items.sort()
            prev_val = 0.0
            prev_filed = None
            for en, val, filed in items:
                dur = (M._dt(en) - M._dt(st)).days
                if dur > 380:
                    continue
                avail = max(filed, prev_filed) if prev_filed else filed
                ev.append({"end": en, "val": val - prev_val,
                           "available_from": (avail + timedelta(days=1)
                                              ).isoformat(),
                           "accn": "YTD-DIFF"})
                prev_val, prev_filed = val, filed
        if ev and (not best or max(e["end"] for e in ev)
                   > max(e["end"] for e in best)):
            best = sorted(ev, key=lambda x: x["end"])
    return best


def _ttm(events, today=None):
    """§1: four CONTIGUOUS quarters whose newest end is CURRENT
    relative to today — a dead concept or a filing gap suppresses the
    value rather than producing a stale trailing year."""
    if len({e["end"] for e in events}) < 4:
        return None
    by_end = {}
    for e in events:
        by_end.setdefault(e["end"], e)
    ends = sorted(by_end)[-4:]
    if (M._dt(ends[-1]) - M._dt(ends[0])).days > M.MAX_TTM_SPAN:
        return None
    gaps = [(M._dt(b) - M._dt(a)).days for a, b in zip(ends, ends[1:])]
    if not all(M.QTR_GAP_DAYS[0] <= g <= M.QTR_GAP_DAYS[1]
               for g in gaps):
        return None
    if today is None:
        from datetime import date
        today = date.today()
    if (today - M._dt(ends[-1])).days > M.TTM_MAX_END_AGE:
        return None
    return sum(by_end[e]["val"] for e in ends)


def build(ticker):
    """-> {years: [...], columns: {...}, ttm: {...}, gaps: [...],
           guidance: [...]}  — everything OBS/DER, no backfill."""
    import research_live as RL
    try:
        import polygon_data as PG
        adj = M.split_adjuster(PG.splits(ticker))
    except Exception:
        adj = M.split_adjuster([])
    cik = RL.cik_for(ticker)

    rev = _annuals(RL, cik, TAGS["revenue"])
    gp = _annuals(RL, cik, TAGS["gross_profit"])
    ni = _annuals(RL, cik, TAGS["net_income"])
    eps = _annuals(RL, cik, TAGS["eps"], unit="USD/shares", adj=adj,
                   per_share=True)
    ocf = _annuals(RL, cik, TAGS["ocf"])
    capex = _annuals(RL, cik, TAGS["capex"])

    years = sorted(rev)[-MAX_YEARS:]
    gaps = []
    if 0 < len(rev) < MAX_YEARS:
        first = min(rev)
        gaps.append("filed annual history begins with the fiscal year "
                    "ended %s — earlier periods were not comparably "
                    "filed and are not shown" % first)

    prev_rev = {}
    all_years = sorted(rev)
    for i, y in enumerate(all_years):
        if i:
            prev_rev[y] = rev[all_years[i - 1]]["val"]

    columns = {}
    for y in years:
        r = rev[y]["val"]
        col = {"revenue": r,
               "revenue_growth": (100.0 * (r / prev_rev[y] - 1)
                                  if y in prev_rev and prev_rev[y]
                                  else None),
               "gross_margin": (100.0 * gp[y]["val"] / r
                                if y in gp and r else None),
               "net_income": ni[y]["val"] if y in ni else None,
               "net_margin": (100.0 * ni[y]["val"] / r
                              if y in ni and r else None),
               "eps": eps[y]["val"] if y in eps else None,
               "ocf": ocf[y]["val"] if y in ocf else None,
               "fcf": (ocf[y]["val"] - capex[y]["val"]
                       if y in ocf and y in capex else None),
               "accn": rev[y].get("accn")}
        columns[y] = col

    # TTM from quarterly streams (revenue + net income + ocf; margins
    # derived; EPS TTM from the rebased quarterly EPS events)
    q_rev = _quarters(RL, cik, TAGS["revenue"])
    q_ni = _quarters(RL, cik, TAGS["net_income"])
    q_ocf = _ytd_quarters(RL, cik, TAGS["ocf"])
    q_capex = _ytd_quarters(RL, cik, TAGS["capex"])
    q_eps = _quarters(RL, cik, TAGS["eps"], unit="USD/shares", adj=adj,
                      per_share=True)
    t_rev, t_ni = _ttm(q_rev), _ttm(q_ni)
    t_ocf, t_capex = _ttm(q_ocf), _ttm(q_capex)
    from datetime import date as _date
    integrity = M.ttm_integrity(q_rev, _date.today()) if q_rev else \
        {"ok": False, "reasons": ["no quarterly revenue events"]}
    if t_rev is None and q_rev:
        gaps.append("TTM suppressed: %s"
                    % "; ".join(integrity.get("reasons")
                                or ["four contiguous current quarters "
                                    "not available"]))
    ttm = {"revenue": t_rev,
           "revenue_growth": None,
           "gross_margin": None,
           "net_income": t_ni,
           "net_margin": (100.0 * t_ni / t_rev
                          if t_ni is not None and t_rev else None),
           "eps": _ttm(q_eps),
           "ocf": t_ocf,
           "fcf": (t_ocf - t_capex
                   if t_ocf is not None and t_capex is not None else None),
           # "through" only when a valid current TTM exists — a stale
           # quarter end must never be labelled the trailing period
           "through": (max((e["end"] for e in q_rev), default=None)
                       if t_rev is not None else None),
           "integrity": integrity}

    return {"ticker": ticker.upper(),
            "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "rows": ROWS, "years": years, "columns": columns, "ttm": ttm,
            "gaps": gaps,
            "basis": "annual 10-K facts as first reported (per-share "
                     "rows rebased to the current share basis); TTM from "
                     "the last four filed quarters; derived rows carry "
                     "their formula"}
