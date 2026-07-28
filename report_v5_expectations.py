#!/usr/bin/env python3
"""report_v5_expectations.py — canonical Expectations engine (v5.5
phase C).

ONE Expectations object per report. No other module computes
expectations independently; claims, scenarios and the matrix all read
from here.

Per material KPI, the object records every voice with its provenance:

  metric, period,
  company_guidance          (admitted 8-K exhibit, OBS)
  consensus + source/as_of  (estimate feed; premium-gated states kept)
  tickerdesk_estimate       (assumptions file only, ASM)
  valuation_implied_value   (what today's price implies at the own-
                             history median multiple, DER)
  historical_baseline       (trailing filed value, OBS/DER)
  differences + notes

And answers, only from sourced inputs:
  * what must happen merely to justify today's price
  * where the sourced voices disagree (the expectations gap)

A VARIANT PERCEPTION requires a sourced, current market expectation
for the same metric. Absent one, the honest label is "business
insight" / "open hypothesis" — this module is where that distinction
is decided, once, for the whole report.
"""

from datetime import datetime, timezone

SCHEMA = "v5-expectations/1"


def _leg(scenarios, name):
    for r in (scenarios or {}).get("rows") or []:
        if r["leg"] == name:
            return r
    return None


def build(snap, grid, multiples, scenarios, estimates, assumptions=None):
    est = estimates or {}
    ex = snap.get("exhibit") or {}
    kpis = []
    gaps = []

    ttm = (grid or {}).get("ttm") or {}
    period = "TTM through %s" % (ttm.get("through") or "latest filed")

    # ── the trailing metric every scenario is priced on ──────────────
    sc = scenarios or {}
    base = _leg(sc, "base")
    metric_kind = sc.get("metric_kind")
    cur_metric = (base or {}).get("metric", {}).get("value") \
        if base else None
    implied = None
    if sc.get("available") and base and sc.get("spot") \
            and base["multiple"]["value"]:
        # metric that justifies TODAY's price at the median multiple
        implied = round(sc["spot"] / base["multiple"]["value"], 4)

    # consensus KPI estimates: free tier gates them; the STATE is the
    # honest record, never a substitute number
    eps_next = est.get("eps_estimate_next")
    cov = est.get("coverage") or {}
    consensus_val = (eps_next or {}).get("avg") if eps_next else None
    consensus_state = ("ok" if consensus_val is not None
                       else cov.get("eps_estimate") or "absent")

    td_est = None
    fwd = ((assumptions or {}).get("fields") or {}).get("forward_metric")
    if fwd:
        td_est = {"value": fwd.get("value"),
                  "basis": fwd.get("basis"), "grade": "ASM"}

    if metric_kind and cur_metric is not None:
        label = ("diluted EPS (trailing)" if metric_kind == "pe"
                 else "revenue per share (trailing)")
        diff = (round(implied - cur_metric, 4)
                if implied is not None else None)
        kpis.append({
            "metric": label, "period": period,
            "company_guidance": None,
            "consensus": consensus_val,
            "consensus_source": "finnhub /stock/eps-estimate"
                                if consensus_val is not None else None,
            "consensus_as_of": est.get("as_of")
                               if consensus_val is not None else None,
            "consensus_state": consensus_state,
            "tickerdesk_estimate": td_est,
            "valuation_implied_value": implied,
            "historical_baseline": cur_metric,
            "difference_absolute": diff,
            "difference_percentage": (round(100.0 * diff / cur_metric, 1)
                                      if diff is not None and cur_metric
                                      else None),
            "evidence_grade": "DER",
            "comparability_notes": "implied value = spot / own-history "
                                   "median multiple; baseline = the "
                                   "trailing figure the band prices",
        })
        if diff is not None and cur_metric:
            pct = 100.0 * diff / cur_metric
            gaps.append({
                "topic": "what today's price asks of the %s" % label,
                "market": "price implies %.4g (%+.1f%% vs trailing) at "
                          "the median multiple" % (implied, pct),
                "sourced": True,
                "grade": "DER",
            })

    # ── guided KPIs from the admitted exhibit ────────────────────────
    if ex.get("disposition") == "ADMITTED":
        for k, g in (ex.get("guidance_highlights") or {}).items():
            if k == "fx_commentary" or not isinstance(g, dict) \
                    or g.get("low") is None:
                continue
            kpis.append({
                "metric": g.get("label") or k,
                "period": "guided period (see exhibit)",
                "company_guidance": {"low": g["low"], "high": g["high"],
                                     "unit": g.get("unit")},
                "consensus": None,
                "consensus_source": None,
                "consensus_as_of": None,
                "consensus_state": cov.get("rev_estimate") or "absent",
                "tickerdesk_estimate": None,
                "valuation_implied_value": None,
                "historical_baseline": None,
                "difference_absolute": None,
                "difference_percentage": None,
                "evidence_grade": "OBS",
                "comparability_notes": "issuer outlook range, filed "
                                       "release",
            })

    # ── variant availability: decided HERE, once ─────────────────────
    # A variant needs the SAME metric carrying both a sourced market
    # expectation and a TickerDesk estimate that differs.
    variant = {"available": False,
               "reason": "no KPI carries both a sourced market "
                         "expectation and a TickerDesk estimate"}
    for k in kpis:
        mkt = (k["consensus"] if k["consensus"] is not None
               else (k["company_guidance"] or {}).get("low"))
        td = (k["tickerdesk_estimate"] or {}).get("value") \
            if k["tickerdesk_estimate"] else None
        if mkt is not None and td is not None and mkt:
            gap_pct = 100.0 * (td / mkt - 1)
            variant = {"available": True, "metric": k["metric"],
                       "market": mkt, "tickerdesk": td,
                       "gap_pct": round(gap_pct, 1),
                       "source": k["consensus_source"]
                       or "issuer guidance (filed exhibit)"}
            break

    matrix = []
    for k in kpis[:6]:
        market = None
        if k["company_guidance"]:
            cg = k["company_guidance"]

            def _gfmt(v, unit):
                if unit == "%":
                    return "%.1f%%" % v
                if unit == "USD" or (isinstance(v, (int, float))
                                     and abs(v) >= 1e6):
                    a = abs(v)
                    if a >= 1e9:
                        return "$%.3gB" % (v / 1e9)
                    if a >= 1e6:
                        return "$%.0fM" % (v / 1e6)
                return "%.4g" % v
            u = cg.get("unit")
            market = "guides %s-%s" % (_gfmt(cg["low"], u),
                                       _gfmt(cg["high"], u))
        elif k["consensus"] is not None:
            market = "consensus %.4g (%s)" % (k["consensus"],
                                              k["consensus_as_of"])
        else:
            market = "consensus %s" % k["consensus_state"]
        td = (k["tickerdesk_estimate"] or {})
        matrix.append({
            "topic": k["metric"],
            "market": market,
            "tickerdesk": ("%.4g [ASM] (%s)" % (td["value"], td["basis"])
                           if td.get("value") is not None
                           else "not available"),
            "evidence": k["evidence_grade"],
            "implication": (
                "price implies %.4g" % k["valuation_implied_value"]
                if k["valuation_implied_value"] is not None else
                "informational"),
        })

    return {"schema": SCHEMA,
            "as_of": datetime.now(timezone.utc
                                  ).isoformat(timespec="seconds"),
            "kpis": kpis, "matrix": matrix, "gaps": gaps,
            "variant": variant,
            "justify_price": (
                "at the historical median multiple, today's price "
                "corresponds to a trailing metric of %.4g "
                "(%+.1f%% vs today's %.4g)"
                % (implied, 100.0 * (implied / cur_metric - 1),
                   cur_metric)
                if implied is not None and cur_metric else None)}
