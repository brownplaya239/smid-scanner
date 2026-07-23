#!/usr/bin/env python3
"""report_v4_model.py — the Equity Research v4 view model.

This assembles one page-ready view from four inputs — the snapshot, the
event gate (report_v4_event), the estimates provider (estimates_provider)
and the peer feed — and it makes the honesty rules structural rather than
a matter of renderer discipline:

  * The fundamental rating is the Street CONSENSUS from the estimates
    feed, dated, labelled as a vendor observation — never our own opinion
    dressed as analysis. The tactical rating is our technical read of the
    tape we hold. Two lenses, neither invented.
  * A directional rating is emitted ONLY when the event gate permits it.
    In DATA HOLD or CALL IN PROGRESS the view carries the flash and no
    rating, because the gate says the inputs are not verified.
  * Anything the feed cannot supply on the current tier — a 12-month
    target, forward estimates, peer multiples — is a WITHHELD marker with
    the reason, not an omission and never a guess.

Everything reusable is composed from report_v3_model, not reimplemented:
the technical state, the business description, the insider and catalyst
views are the same computations v3 validated.

The model is pure: it takes already-fetched inputs and returns a dict, so
it runs offline against a cached snapshot with the estimates/peers feeds
absent — which is exactly the free-tier local state.
"""

import report_v3_model as M3
import research_snapshot as rs
import report_v4_event as EV

OBSERVED, DERIVED, INFERRED = M3.OBSERVED, M3.DERIVED, M3.INFERRED


def _withheld(reason):
    return {"available": False, "reason": reason}


def _fv(x):
    return rs.fv(x)


# ── ratings ─────────────────────────────────────────────────────────────

def fundamental_rating(estimates, event):
    """Street consensus, dated — an observed vendor fact. Withheld when
    the feed is unconfigured or the event gate refuses a rating."""
    if not event.get("rating_allowed"):
        return _withheld("event state %s does not permit a rating"
                         % event["state"])
    est = estimates or {}
    if not est.get("configured"):
        return _withheld("no admitted estimate source (%s)"
                         % (est.get("reason") or "estimates feed absent"))
    rec = est.get("recommendation")
    if not rec:
        return _withheld("estimate feed returned no recommendation "
                         "consensus")
    n = (rec["strong_buy"] + rec["buy"] + rec["hold"] + rec["sell"]
         + rec["strong_sell"])
    return {"available": True, "band": rec["band"], "score": rec["score"],
            "n_analysts": n, "as_of": rec["as_of"], "grade": OBSERVED,
            "detail": "%d strong-buy / %d buy / %d hold / %d sell / "
                      "%d strong-sell" % (rec["strong_buy"], rec["buy"],
                                          rec["hold"], rec["sell"],
                                          rec["strong_sell"]),
            "source": "%s consensus" % est.get("provider", "vendor")}


def tactical_rating(snap):
    """Our own read of the tape, derived from bars we hold. Not an
    opinion about the company — a description of where price stands
    relative to its own structure."""
    lv = snap.get("levels") or {}
    px = M3.spot(snap)
    state, _ = M3.technical_state(lv, px)
    if not state:
        return _withheld("insufficient price history for a technical read")
    above = sum(1 for k in ("ma20", "ma50", "ma200")
                if _fv(lv.get(k)) is not None and px is not None
                and px > _fv(lv.get(k)))
    band = ("Constructive" if above == 3 else "Improving" if above == 2
            else "Cautious" if above == 1 else "Weak")
    return {"available": True, "band": band, "above_mas": above,
            "grade": DERIVED, "detail": state,
            "source": "technical, completed sessions"}


def price_target(estimates, event, price):
    """12-month consensus target and the expected return to it. Withheld
    on a tier that gates the price-target endpoint — never inferred from a
    recommendation band."""
    est = estimates or {}
    if not event.get("rating_allowed"):
        return _withheld("event state %s does not permit a target"
                         % event["state"])
    pt = est.get("price_target")
    if not pt:
        cov = (est.get("coverage") or {}).get("price_target")
        return _withheld("price target %s"
                         % ("requires premium estimates"
                            if cov == "premium-gated" else
                            "not available from the estimate feed"))
    mean = pt.get("mean")
    exp = (round(100.0 * (mean - price) / price, 1)
           if (mean and price) else None)
    return {"available": True, "mean": mean, "high": pt.get("high"),
            "low": pt.get("low"), "n_analysts": pt.get("n_analysts"),
            "as_of": pt.get("as_of"), "expected_return_pct": exp,
            "grade": OBSERVED, "source": "%s consensus target"
            % est.get("provider", "vendor")}


# ── financials ──────────────────────────────────────────────────────────

def financials(snap, estimates):
    """The reported quarter, its margins and cash, plus the surprise
    history the free tier does supply. Forward consensus and the SaaS
    operating KPIs are withheld with their reason."""
    fu = snap.get("fundamentals") or {}
    est = estimates or {}

    def f(key):
        return _fv(fu.get(key))

    reported = {
        "revenue_q": f("revenue_q"), "revenue_growth": f("revenue_growth"),
        "gross_margin": f("gross_margin"), "net_margin": f("net_margin"),
        "net_income_q": f("net_income_q"),
        "operating_cash_flow": f("operating_cash_flow"),
        "free_cash_flow": f("free_cash_flow"), "eps_ttm": f("eps_ttm"),
        "cash": f("cash"), "debt": f("debt"),
    }
    surprises = est.get("surprises") or []
    return {
        "reported": reported,
        "surprises": surprises,
        "forward_consensus": (
            {"available": True}
            if (est.get("eps_estimate_next") or est.get("rev_estimate_next"))
            else _withheld(
                "forward consensus %s"
                % ("requires premium estimates"
                   if (est.get("coverage") or {}).get("eps_estimate")
                   == "premium-gated" else "not available"))),
        "saas_kpis": _withheld(
            "cRPO/RPO, ACV, customers and net retention are not ingested "
            "by this report — they appear only as prose in filings, with "
            "no structured source"),
    }


# ── valuation ───────────────────────────────────────────────────────────

def valuation(snap, estimates, peers, price):
    """What the free tier and our own data can honestly value on: the
    trailing multiple and a peer table when a peer set is available. The
    12-month target, forward multiples and the price-target bridge are
    withheld on a target-gated tier; the historical band is computed in a
    later slice."""
    val = snap.get("valuation") or {}
    pe_t = _fv(val.get("pe_trailing"))
    return {
        "pe_trailing": pe_t,
        "peers": peers if (peers and peers.get("rows")) else _withheld(
            "no admitted peer set" if peers is None else
            (peers.get("reason") or "peer multiples unavailable")),
        "target_bridge": _withheld(
            "no admitted price target to bridge to on this estimate tier"),
        "historical_band": _withheld("computed in the valuation slice"),
        "grade": DERIVED,
    }


# ── risks ───────────────────────────────────────────────────────────────

def risks(snap, event):
    """Three ranked risks, each anchored to a filed fact or the event
    state — never a generic list. Ordered most-concrete first."""
    out = []
    fu = snap.get("fundamentals") or {}
    lv = snap.get("levels") or {}
    px = M3.spot(snap)
    nm = _fv(fu.get("net_margin"))
    if nm is not None and nm < 5:
        out.append({"text": "Thin GAAP profitability: net margin %.1f%% "
                            "leaves little cushion for a demand or cost "
                            "shock." % nm, "grade": OBSERVED})
    ma200 = _fv(lv.get("ma200"))
    if px is not None and ma200 and px < ma200:
        out.append({"text": "Price is below its 200-day average (%.2f), a "
                            "structural downtrend until reclaimed."
                            % ma200, "grade": DERIVED})
    ins = (snap.get("insiders") or {}).get("by_class") or {}
    if ins.get("open_market_sale"):
        out.append({"text": "%d open-market insider sale(s) filed in the "
                            "window; discretionary intent is not disclosed."
                            % ins["open_market_sale"], "grade": OBSERVED})
    if event["state"] in (EV.RESULTS_RELEASED,):
        out.append({"text": "The earnings call had not verifiably concluded "
                            "at report time; guidance colour may still "
                            "revise the read.", "grade": OBSERVED})
    d = _fv(fu.get("debt"))
    if d and _fv(fu.get("cash")) is not None and d > (_fv(fu.get("cash"))
                                                      or 0):
        out.append({"text": "Long-term debt exceeds cash on hand; refinancing "
                            "terms matter if rates stay elevated.",
                    "grade": OBSERVED})
    return out[:3]


# ── the whole view ──────────────────────────────────────────────────────

def build(snap, estimates=None, peers=None, report_time=None):
    """Assemble the v4 view. estimates/peers are injected so the model is
    testable without a key or a network; report_v4_run fetches them in
    production. Absent feeds produce WITHHELD markers, which is the honest
    free-tier state."""
    report_time = report_time or snap.get("report_time")
    cat = snap.get("catalyst") or {}
    exhibit = snap.get("exhibit") or {}
    event = EV.event_state(cat, exhibit, report_time=report_time)
    price = M3.spot(snap)

    return {
        "ticker": snap.get("ticker"),
        "price": price,
        "event": event,
        "flash": event.get("flash"),
        "ratings": {
            "fundamental": fundamental_rating(estimates, event),
            "tactical": tactical_rating(snap),
            "target": price_target(estimates, event, price),
        },
        "thesis": M3.thesis_facts(snap, limit=3),
        "risks": risks(snap, event),
        "business": M3.business_description(snap),
        "financials": financials(snap, estimates),
        "valuation": valuation(snap, estimates, peers, price),
        "catalysts": M3.catalysts(snap),
        "insiders": M3.insider_view(snap),
        "ownership": M3.ownership_view(snap),
        "options": M3.options_view(snap),
        "horizon": "12-month fundamental view; swing (2-8 week) tactical "
                   "overlay",
        "estimates_configured": bool((estimates or {}).get("configured")),
    }
