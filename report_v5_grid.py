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


def _quarters(RL, cik, tags, unit="USD", adj=None, per_share=False,
              with_tag=False):
    """Quarterly events with Q4 derived AFTER split rebasing — deriving
    first mixes bases across a split (the exact ServiceNow 5:1 failure
    the multiples engine fixed). One tag's stream wins outright — the
    freshest coverage — so a TTM never mixes concepts."""
    best, best_tag = [], None
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
            best_tag = t
    return (best, best_tag) if with_tag else best


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


def _ttm_cell(events, today=None, concept=None):
    """§3 (v5.8): ONE displayed TTM cell, independently validated —
    four contiguous quarters of one concept whose newest end is current.
    Returns {"value","through","ok","reasons","quarters","concept"};
    a failing cell keeps its reasons so the footnote can state exactly
    why that metric's TTM is suppressed."""
    rec = {"value": None, "through": None, "ok": False,
           "reasons": [], "quarters": [], "concept": concept}
    if not events:
        rec["reasons"].append("no quarterly events for this concept")
        return rec
    by_end = {}
    for e in events:
        by_end.setdefault(e["end"], e)
    ends = sorted(by_end)
    rec["through"] = ends[-1]
    if len(ends) < 4:
        rec["reasons"].append("only %d distinct filed quarter end(s)"
                              % len(ends))
        return rec
    last4 = ends[-4:]
    rec["quarters"] = last4
    rec["through"] = last4[-1]
    if (M._dt(last4[-1]) - M._dt(last4[0])).days > M.MAX_TTM_SPAN:
        rec["reasons"].append("the last four quarter ends span %d days "
                              "— not one trailing year"
                              % (M._dt(last4[-1])
                                 - M._dt(last4[0])).days)
        return rec
    gaps = [(M._dt(b) - M._dt(a)).days
            for a, b in zip(last4, last4[1:])]
    if not all(M.QTR_GAP_DAYS[0] <= g <= M.QTR_GAP_DAYS[1]
               for g in gaps):
        rec["reasons"].append("quarter ends are not contiguous "
                              "(gaps %s days)" % gaps)
        return rec
    if today is None:
        from datetime import date
        today = date.today()
    age = (today - M._dt(last4[-1])).days
    if age > M.TTM_MAX_END_AGE:
        rec["reasons"].append("newest quarter end %s is %d days old "
                              "(limit %d) — not a current trailing "
                              "year" % (last4[-1], age,
                                        M.TTM_MAX_END_AGE))
        return rec
    rec["ok"] = True
    rec["value"] = sum(by_end[e]["val"] for e in last4)
    return rec


def _ttm(events, today=None):
    """Back-compat scalar wrapper over _ttm_cell."""
    return _ttm_cell(events, today)["value"]


def build(ticker, revenue_tags=None):
    """-> {years: [...], columns: {...}, ttm: {...}, gaps: [...],
           guidance: [...]}  — everything OBS/DER, no backfill.

    `revenue_tags` (§3, v5.8): the sector adapter's revenue-concept
    candidates (e.g. a bank's net-revenue concepts) tried ahead of the
    generic defaults — concept selection stays freshest-coverage-wins,
    never mixed."""
    import research_live as RL
    rev_tags = tuple(revenue_tags or ()) + tuple(
        t for t in TAGS["revenue"] if t not in (revenue_tags or ()))
    try:
        import polygon_data as PG
        adj = M.split_adjuster(PG.splits(ticker))
    except Exception:
        adj = M.split_adjuster([])
    cik = RL.cik_for_filed(ticker)[0]

    rev = _annuals(RL, cik, rev_tags)
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

    # §3 (v5.8): every displayed TTM cell validated INDEPENDENTLY —
    # four contiguous quarters of one concept, current end — and the
    # populated cells must share one column endpoint. A dead revenue
    # concept suppresses the revenue cell with its reason; it never
    # silently invalidates (or spares) the other metrics' cells.
    q_rev, rev_tag = _quarters(RL, cik, rev_tags, with_tag=True)
    q_ni = _quarters(RL, cik, TAGS["net_income"])
    q_ocf = _ytd_quarters(RL, cik, TAGS["ocf"])
    q_capex = _ytd_quarters(RL, cik, TAGS["capex"])
    q_eps = _quarters(RL, cik, TAGS["eps"], unit="USD/shares", adj=adj,
                      per_share=True)
    from datetime import date as _date
    _today = _date.today()
    cells = {
        "revenue": _ttm_cell(q_rev, _today, concept=rev_tag),
        "net_income": _ttm_cell(q_ni, _today,
                                concept=TAGS["net_income"][0]),
        "eps": _ttm_cell(q_eps, _today, concept=TAGS["eps"][0]),
        "ocf": _ttm_cell(q_ocf, _today, concept=TAGS["ocf"][0]),
        "capex": _ttm_cell(q_capex, _today, concept=TAGS["capex"][0]),
    }
    # one common column endpoint: the newest through-date among valid
    # cells; a valid cell with a different endpoint is suppressed too
    valid_throughs = [c["through"] for c in cells.values() if c["ok"]]
    column_through = max(valid_throughs) if valid_throughs else None
    for name, c in cells.items():
        if c["ok"] and c["through"] != column_through:
            c["ok"] = False
            c["reasons"].append("trailing year ends %s — not the TTM "
                                "column date %s; mixed endpoints are "
                                "never shown in one unlabeled column"
                                % (c["through"], column_through))
            c["value"] = None

    def _cv(name):
        return cells[name]["value"] if cells[name]["ok"] else None

    t_rev, t_ni = _cv("revenue"), _cv("net_income")
    t_ocf, t_capex = _cv("ocf"), _cv("capex")
    _HUMAN = {"revenue": "revenue", "net_income": "net income",
              "eps": "diluted EPS", "ocf": "operating cash flow",
              "capex": "capital expenditure"}
    suppressed = [{"metric": n, "label": _HUMAN.get(n, n),
                   "reasons": list(c["reasons"])}
                  for n, c in cells.items()
                  if not c["ok"] and (q_rev if n == "revenue" else
                                      {"net_income": q_ni, "eps": q_eps,
                                       "ocf": q_ocf, "capex": q_capex
                                       }.get(n))]
    if suppressed:
        gaps.append("TTM suppressed for %s: %s"
                    % (", ".join(s["label"] for s in suppressed),
                       "; ".join("%s — %s"
                                 % (s["label"],
                                    "; ".join(s["reasons"][:1]))
                                 for s in suppressed)))
    integrity = M.ttm_integrity(q_rev, _today) if q_rev else \
        {"ok": False, "reasons": ["no quarterly revenue events"]}
    ttm = {"revenue": t_rev,
           "revenue_growth": None,
           "gross_margin": None,
           "net_income": t_ni,
           # derived rows only from cells sharing the column endpoint
           "net_margin": (100.0 * t_ni / t_rev
                          if t_ni is not None and t_rev else None),
           "eps": _cv("eps"),
           "ocf": t_ocf,
           "fcf": (t_ocf - t_capex
                   if t_ocf is not None and t_capex is not None else None),
           # the displayed through-date IS the common endpoint of the
           # populated cells — never a stale stream's max end
           "through": column_through,
           "cells": cells,
           "suppressed": suppressed,
           "revenue_concept": rev_tag,
           "integrity": integrity}

    return {"ticker": ticker.upper(),
            "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "rows": ROWS, "years": years, "columns": columns, "ttm": ttm,
            "gaps": gaps,
            "basis": "annual 10-K facts as first reported (per-share "
                     "rows rebased to the current share basis); TTM from "
                     "the last four filed quarters; derived rows carry "
                     "their formula"}
