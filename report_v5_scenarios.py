#!/usr/bin/env python3
"""report_v5_scenarios.py — bear/base/bull scenario engine (v5 slice 2).

A scenario price is (multiple x trailing metric). Nothing else. The
multiples come from the historical-multiples engine (DER — the name's
own P25/P50/P75, filing-date aligned); the metric is the current
trailing figure from filed quarters (OBS-derived); the arithmetic is
written out so the validator can recompute every cell from the rendered
text.

ASSUMPTIONS (the only place judgment enters, and it is labelled)
    assumptions/<TICKER>.json, schema v5-assumptions/1. User-supplied
    multiple overrides, forward metrics, and scenario probabilities
    render as ASM with their basis and as-of date. An expired file is
    DROPPED — noted in the record, never silently used. Probabilities
    exist ONLY via this file: no file, no weights, no invented
    precision.

    {
      "schema": "v5-assumptions/1",
      "as_of": "2026-07-28", "expires": "2026-10-28",
      "source": "user", "currency": "USD",
      "units": "per-share", "fiscal_basis": "FY-Dec",
      "fields": {
        "base_multiple":  {"value": 17.0, "basis": "own 5yr median"},
        "bull_multiple":  {"value": 21.0, "basis": "re-rate to peers"},
        "bear_multiple":  {"value": 13.0, "basis": "2022 trough"},
        "forward_metric": {"value": 3.47, "basis": "FY17 EPS est"},
        "probabilities":  {"bear": 0.2, "base": 0.5, "bull": 0.3}
      }
    }
"""

import json
import os
from datetime import datetime, timezone

_BASE = os.path.dirname(os.path.abspath(__file__))
ASSUMPTIONS_DIR = os.path.join(_BASE, "assumptions")
SCHEMA = "v5-assumptions/1"

OBS, DER, ASM = "OBS", "DER", "ASM"

REQUIRED_ENVELOPE = ("schema", "as_of", "source", "currency", "units",
                     "fiscal_basis")


def load_assumptions(ticker, today=None):
    """The versioned contract. Returns (fields|None, note). Anything
    malformed or expired yields None plus a note that the report and the
    validation file both carry — a bad assumptions file must be visible,
    not silently ignored."""
    path = os.path.join(ASSUMPTIONS_DIR, "%s.json" % ticker.upper())
    if not os.path.exists(path):
        return None, None
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as e:
        return None, "assumptions file unreadable (%s) — ignored" % e
    if doc.get("schema") != SCHEMA:
        return None, ("assumptions schema %r is not %s — ignored"
                      % (doc.get("schema"), SCHEMA))
    missing = [k for k in REQUIRED_ENVELOPE if not doc.get(k)]
    if missing:
        return None, ("assumptions missing %s — ignored"
                      % ", ".join(missing))
    today = today or datetime.now(timezone.utc).date().isoformat()
    exp = doc.get("expires")
    if exp and str(exp) < today:
        return None, ("user assumptions dated %s expired %s — the "
                      "valuation legs revert to historical percentiles"
                      % (doc["as_of"], exp))
    fields = doc.get("fields") or {}
    for k, v in fields.items():
        if k == "probabilities":
            continue
        if not isinstance(v, dict) or v.get("value") is None \
                or not v.get("basis"):
            return None, ("assumption %r lacks value+basis — file "
                          "ignored" % k)
    meta = {k: doc[k] for k in REQUIRED_ENVELOPE}
    meta["expires"] = exp
    return {"meta": meta, "fields": fields}, None


def _cell(value, grade, basis):
    return {"value": value, "grade": grade, "basis": basis}


def anchor(record):
    """The central row — 'median' in historical mode, 'base' when
    underwritten. Downstream consumers use this instead of assuming a
    leg name that §2 removed from historical-range objects."""
    for r in (record or {}).get("rows") or []:
        if r["leg"] in ("median", "base"):
            return r
    return None


def row_by(record, *names):
    for r in (record or {}).get("rows") or []:
        if r["leg"] in names:
            return r
    return None


def build(ticker, multiples, spot, assumptions=None, note=None,
          valuation_policy=None):
    """The scenario table.

    multiples : record from report_v5_multiples.build()
    spot      : last completed close (OBS)
    Returns {available, metric_kind, rows, weighted, assumptions_note,
             arithmetic[]} — or a withheld record naming the reason.
    Selection: P/E when its band survived coverage, else P/S, else
    withheld. The metric is ALWAYS the trailing figure the band was
    computed against, so multiple x metric is internally consistent —
    unless a forward_metric assumption replaces it, labelled ASM."""
    out = {"ticker": ticker.upper(), "spot": spot,
           "assumptions_note": note}
    # §2: the sector adapter governs which multiple kinds are
    # economically supportable — a method the model's economics cannot
    # support is never selected just because a band computed.
    pol = valuation_policy or {}
    allowed = tuple(pol.get("valuation_allowed", ("pe", "ps")))
    out["valuation_policy"] = {
        "adapter": pol.get("adapter"),
        "allowed": list(allowed),
        "reason": pol.get("valuation_reason"),
    }
    if not allowed:
        out.update({"available": False,
                    "reason": pol.get("valuation_reason")
                    or "no valuation method is supportable for this "
                       "business model"})
        return out
    band = None
    for kind in allowed:
        b = multiples.get(kind) or {}
        if b.get("available"):
            band, out["metric_kind"] = b, kind
            break
    if band is None:
        reasons = ["%s: %s" % (k.upper(),
                               (multiples.get(k) or {}).get("reason")
                               or "band unavailable") for k in allowed]
        out.update({"available": False,
                    "reason": "no permitted multiple band survived its "
                              "coverage floor (%s)" % "; ".join(reasons)})
        return out

    # trailing metric implied by the band's own current multiple — the
    # exact series the percentiles came from, no re-derivation drift
    cur_mult = band.get("current")
    if not cur_mult or not spot:
        out.update({"available": False,
                    "reason": "no current multiple/spot to anchor the "
                              "metric"})
        return out
    metric = round(spot / cur_mult, 4)
    metric_cell = _cell(metric, DER,
                        "spot / current trailing multiple — the trailing "
                        "%s the band itself was computed on"
                        % ("EPS" if out["metric_kind"] == "pe"
                           else "revenue/share"))

    fields = (assumptions or {}).get("fields") or {}
    fmeta = (assumptions or {}).get("meta") or {}
    stamp = ("user-supplied %s" % fmeta.get("as_of")) if fields else None
    fwd = fields.get("forward_metric")
    if fwd:
        metric_cell = _cell(float(fwd["value"]), ASM,
                            "%s (%s)" % (fwd["basis"], stamp))
        metric = float(fwd["value"])

    # MODE: without user operating assumptions AND probabilities this is
    # a HISTORICAL VALUATION RANGE — percentiles of the name's own past
    # multiples applied to a constant trailing metric. It is context,
    # not a forecast, and its legs ARE p25/median/p75: no bear/base/bull
    # leg exists on a historical-range object (§2). Only a forward
    # metric plus probabilities upgrades the mode to "underwritten" and
    # earns Bear/Base/Bull scenario legs.
    underwritten = bool(fields.get("forward_metric")
                        and fields.get("probabilities"))
    out["mode"] = "underwritten" if underwritten else "historical_range"
    # (leg name, display label, percentile source, assumption-field key)
    LEG_SPECS = ([("bear", "Bear", "p25", "bear_multiple"),
                  ("base", "Base", "p50", "base_multiple"),
                  ("bull", "Bull", "p75", "bull_multiple")]
                 if underwritten else
                 [("p25", "P25", "p25", "bear_multiple"),
                  ("median", "Median", "p50", "base_multiple"),
                  ("p75", "P75", "p75", "bull_multiple")])
    rows, arithmetic = [], []
    for leg, label, pkey, fkey in LEG_SPECS:
        ov = fields.get(fkey)
        if ov:
            mult_cell = _cell(float(ov["value"]), ASM,
                              "%s (%s)" % (ov["basis"], stamp))
        else:
            mult_cell = _cell(band.get(pkey), DER,
                              "own %d-yr %s of daily trailing %s"
                              % (band["window_years"],
                                 {"p25": "P25", "p50": "P50",
                                  "p75": "P75"}[pkey],
                                 out["metric_kind"].upper()))
        price = round(mult_cell["value"] * metric, 2)
        rows.append({"leg": leg, "label": label,
                     "multiple": mult_cell,
                     "metric": metric_cell, "price": price,
                     "vs_spot_pct": round(100.0 * (price / spot - 1), 1)})
        arithmetic.append("%s: %.2f x %.4f = %.2f"
                          % (label, mult_cell["value"], metric, price))

    # probabilities exist only via the assumptions file, only when they
    # cover all three legs and sum to ~1, and ONLY in underwritten mode:
    # a historical range with no operating forecast cannot be
    # probability-weighted into an expected value.
    weighted = None
    probs = fields.get("probabilities")
    if isinstance(probs, dict) and not underwritten:
        out["assumptions_note"] = ((out.get("assumptions_note") or "")
            + " probabilities ignored: no forward_metric (operating "
              "forecast) — a historical range carries no "
              "probabilities").strip()
    elif isinstance(probs, dict):
        vals = [probs.get(l) for l in ("bear", "base", "bull")]
        if all(isinstance(v, (int, float)) for v in vals) \
                and abs(sum(vals) - 1.0) < 0.01:
            wp = sum(r["price"] * p for r, p in zip(rows, vals))
            weighted = {"price": round(wp, 2),
                        "probabilities": {l: probs[l] for l in
                                          ("bear", "base", "bull")},
                        "grade": ASM,
                        "basis": "user-supplied probabilities (%s)"
                                 % fmeta.get("as_of")}
        else:
            out["assumptions_note"] = ((out.get("assumptions_note") or "")
                + " probabilities ignored: must cover bear/base/bull and "
                  "sum to 1").strip()

    # ── asymmetry + expected value (phase E; underwritten mode only —
    # a historical range carries no asymmetry/EV vocabulary) ─────────
    asym = None
    if underwritten:
        rd = {r["leg"]: r for r in rows}
        bear_p, bull_p = rd["bear"]["price"], rd["bull"]["price"]
        downside = round(100.0 * (bear_p / spot - 1), 1)
        upside = round(100.0 * (bull_p / spot - 1), 1)
        asym = {"downside_to_bear_pct": downside,
                "upside_to_bull_pct": upside,
                "up_down_ratio": (round(abs(upside) / abs(downside), 2)
                                  if downside < 0 and upside > 0
                                  else None),
                "basis": "bull/bear scenario prices vs spot"}
    if weighted:
        probs = weighted["probabilities"]
        ev = weighted["price"]
        weighted["expected_value_contribution"] = {
            l: round(rd[l]["price"] * probs[l], 2)
            for l in ("bear", "base", "bull")}
        weighted["expected_return_pct"] = round(
            100.0 * (ev / spot - 1), 1)
        hz = fields.get("horizon_years")
        if hz and isinstance(hz.get("value"), (int, float))                 and hz["value"] > 0:
            yrs = float(hz["value"])
            weighted["annualized_return_pct"] = round(
                100.0 * ((ev / spot) ** (1.0 / yrs) - 1), 1)
            weighted["horizon_years"] = yrs
        else:
            weighted["annualized_return_pct"] = None
        weighted["caveat"] = ("probability weighting reflects the "
                              "stated judgments; it does not remove "
                              "model uncertainty")

    out.update({"available": True, "rows": rows, "weighted": weighted,
                "asymmetry": asym,
                "band_ref": {k: band.get(k) for k in
                             ("kind", "window_years", "actual_years",
                              "window_start", "window_end", "coverage")},
                "arithmetic": arithmetic})
    return out
