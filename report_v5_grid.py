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
           # §2 review fix: the SECTOR DASHBOARD consumes this — the
           # latest validated quarter of the adapter-aware revenue
           # stream, period-qualified, so the visible dashboard can
           # never show a stale generic-tag quarter while the grid
           # validates a different, current stream
           "latest_quarter": (
               {"value": q_rev[-1]["val"], "end": q_rev[-1]["end"],
                "concept": rev_tag}
               if q_rev and (_today - M._dt(q_rev[-1]["end"])).days
               <= M.TTM_MAX_END_AGE else None),
           "integrity": integrity}

    return {"ticker": ticker.upper(),
            "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "rows": ROWS, "years": years, "columns": columns, "ttm": ttm,
            "gaps": gaps,
            "basis": "annual 10-K facts as first reported (per-share "
                     "rows rebased to the current share basis); TTM from "
                     "the last four filed quarters; derived rows carry "
                     "their formula"}


# ── v5.8 review fix: current balance-sheet instants ──────────────────

# Instant (point-in-time) concept candidates, combined at ONE
# reporting date with explicit non-overlap rules:
#   long-term base : LongTermDebtNoncurrent (+ LongTermDebtCurrent at
#                    the same date), else LongTermDebt (the total-LTD
#                    tag — current maturities included per taxonomy,
#                    so LongTermDebtCurrent is NOT re-added), else
#                    NotesPayable.
#   short-term add : ONE of DebtCurrent > ShortTermBorrowings >
#                    CommercialPaper at the same date — these nest
#                    (commercial paper sits inside short-term
#                    borrowings), so exactly one is taken. Review
#                    finding: an issuer whose long-term tag stood
#                    alone (ServiceNow: LongTermDebt $5.4B) was
#                    missing its live $2.1B short-term borrowings.
INSTANT_DEBT_LT_COMPONENTS = ("LongTermDebtNoncurrent",
                              "LongTermDebtCurrent")
INSTANT_DEBT_LT_SINGLE = ("LongTermDebt", "NotesPayable",
                          "DebtLongtermAndShorttermCombinedAmount")
INSTANT_DEBT_ST = ("DebtCurrent", "ShortTermBorrowings",
                   "CommercialPaper")
INSTANT_CASH_TAGS = ("CashAndCashEquivalentsAtCarryingValue",
                     "CashCashEquivalentsRestrictedCashAndRestricted"
                     "CashEquivalents")


def _instants(RL, cik, tag):
    """As-first-reported instant facts {end: (val, accn)} for one tag."""
    out = {}
    for r in RL.concept(cik, tag):
        if r.get("start") or r.get("val") is None or not r.get("end"):
            continue
        if r.get("form") not in ("10-Q", "10-K", "20-F", "40-F"):
            continue
        e = str(r["end"])[:10]
        filed = r.get("filed") or "9999"
        if e not in out or filed < out[e][2]:
            out[e] = (float(r["val"]), r.get("accn"), filed)
    return out


def fresh_instant(ticker, kind):
    """-> a snapshot-shaped fact dict for the FRESHEST filed cash or
    debt position, or None. Debt prefers the component pair
    (noncurrent + current) at one common date; single-tag concepts are
    the fallback. Universal: concept candidates only, never tickers."""
    import research_live as RL
    cik = RL.cik_for_filed(ticker)[0]
    best = None
    if kind == "cash":
        for tag in INSTANT_CASH_TAGS:
            s = _instants(RL, cik, tag)
            if not s:
                continue
            end = max(s)
            cand = {"end": end, "value": s[end][0], "concepts": [tag],
                    "refs": ["XBRL-%s-us-gaap:%s-%s"
                             % (s[end][1] or "na", tag, end)],
                    "basis": "us-gaap:%s" % tag}
            if best is None or cand["end"] > best["end"]:
                best = cand
    else:
        # long-term base first: the component pair when the issuer
        # files it, else a total tag — freshest reporting date wins
        nc = _instants(RL, cik, "LongTermDebtNoncurrent")
        cu = _instants(RL, cik, "LongTermDebtCurrent")
        base = None
        if nc:
            end = max(nc)
            parts = [("LongTermDebtNoncurrent", nc[end])]
            if end in cu:
                parts.append(("LongTermDebtCurrent", cu[end]))
            base = {"end": end, "parts": parts}
        for tag in INSTANT_DEBT_LT_SINGLE:
            s = _instants(RL, cik, tag)
            if s and (base is None or max(s) > base["end"]):
                end = max(s)
                base = {"end": end, "parts": [(tag, s[end])]}
        if base is not None:
            end = base["end"]
            parts = list(base["parts"])
            # exactly ONE short-term instrument at the SAME date —
            # DebtCurrent > ShortTermBorrowings > CommercialPaper
            # (they nest; summing two double-counts). DebtCurrent also
            # SUBSUMES the current-LTD portion, so when the base
            # already carries LongTermDebtCurrent only the narrower
            # instruments may add.
            _has_cu = any(p[0] == "LongTermDebtCurrent" for p in parts)
            _st = INSTANT_DEBT_ST[1:] if _has_cu else INSTANT_DEBT_ST
            for tag in _st:
                s = _instants(RL, cik, tag)
                if end in s:
                    parts.append((tag, s[end]))
                    break
            best = {
                "end": end,
                "value": sum(p[1][0] for p in parts),
                "concepts": [p[0] for p in parts],
                "refs": ["XBRL-%s-us-gaap:%s-%s"
                         % (p[1][1] or "na", p[0], end)
                         for p in parts],
                "basis": " + ".join("us-gaap:%s" % p[0]
                                    for p in parts)
                + (" (same reporting date)" if len(parts) > 1
                   else ""),
            }
    if best is None:
        return None
    from datetime import datetime as _dtm, timezone as _tz
    return {"v": best["value"], "unit": "USD",
            "period_end": best["end"],
            "source": "SEC XBRL companyconcept (as first reported)",
            "evidence_refs": best["refs"],
            "basis": best["basis"],
            "calc_version": "v5.8-instants/1",
            "quality": "verified",
            "retrieved_at": _dtm.now(_tz.utc).isoformat(
                timespec="seconds")}


def refresh_balance_instants(snap, ticker, max_age_days=400):
    """Replace stale (or missing) snapshot cash/debt instants with the
    issuer's freshest filed position. Only ever REPLACES with strictly
    newer facts; records what changed. The snapshot's own facts stay
    untouched when already current."""
    import report_v5_checks as CK
    from datetime import date, timedelta
    fu = snap.setdefault("fundamentals", {})
    floor = (date.today() - timedelta(days=max_age_days)).isoformat()
    changed = {}
    for key in ("cash", "debt"):
        cur = fu.get(key) if isinstance(fu.get(key), dict) else None
        cur_end = CK._fact_period(cur)
        if cur_end and cur_end >= floor:
            continue
        try:
            fresh = fresh_instant(ticker, key)
        except Exception:
            fresh = None
        if fresh and (cur_end is None
                      or fresh["period_end"] > cur_end):
            fu[key] = fresh
            changed[key] = {"was": cur_end,
                            "now": fresh["period_end"],
                            "basis": fresh["basis"]}
    return changed
