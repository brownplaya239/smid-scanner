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
    trailing multiple, its position in the 52-week P/E band, a peer table
    when a peer set is available, and re-rating scenarios on unchanged
    EPS. The 12-month consensus target and the price-target bridge are
    withheld on a target-gated tier — a scenario range built from a
    multiple band is NOT a price target and is labelled as such."""
    val = snap.get("valuation") or {}
    lv = snap.get("levels") or {}
    fu = snap.get("fundamentals") or {}
    pe_t = _fv(val.get("pe_trailing"))
    eps = _fv(fu.get("eps_ttm"))
    # The 52-week closing range. The snapshot surfaces the high as
    # `resistance_major` and the low as `support_major`; the plain hi52/lo52
    # keys are a fallback for any snapshot that carries them directly.
    hi52 = _fv(lv.get("resistance_major")) or _fv(lv.get("hi52"))
    lo52 = _fv(lv.get("support_major")) or _fv(lv.get("lo52"))

    # Name the actual reason the band is withheld — a missing EPS and a
    # missing 52-week range are different gaps, and a reader deciding
    # whether the hole is fixable needs to know which one it is.
    if not (eps and eps > 0):
        band_reason = "no positive TTM EPS to build a P/E band"
    elif not (hi52 and lo52):
        band_reason = ("no 52-week closing range in the snapshot to build a "
                       "P/E band")
    else:
        band_reason = "P/E band unavailable"
    band = _withheld(band_reason)
    scenarios = _withheld("no P/E band to derive scenarios from")
    if eps and eps > 0 and hi52 and lo52 and price:
        pe_hi, pe_lo = hi52 / eps, lo52 / eps
        pe_now = price / eps
        pos = ((price - lo52) / (hi52 - lo52)
               if hi52 > lo52 else None)
        band = {"available": True, "pe_now": round(pe_now, 1),
                "pe_low": round(pe_lo, 1), "pe_high": round(pe_hi, 1),
                "eps_ttm": eps, "hi52": hi52, "lo52": lo52,
                "position_pct": round(100.0 * pos, 0) if pos is not None
                else None,
                "basis": "the band is the trailing P/E measured over the "
                         "52-week closing range, holding the current TTM EPS "
                         "constant", "grade": DERIVED}
        # Re-rating scenarios on unchanged EPS. Because the band edges are
        # price/eps, the implied prices are the 52-week closing range
        # itself — which is the honest ceiling of what can be said without
        # a forward estimate, and it is labelled a re-rating range, not a
        # target.
        scenarios = {"available": True, "eps_ttm": eps,
                     "bear": {"pe": round(pe_lo, 1), "price": round(lo52, 2)},
                     "base": {"pe": round(pe_now, 1),
                              "price": round(price, 2)},
                     "bull": {"pe": round(pe_hi, 1), "price": round(hi52, 2)},
                     "basis": "re-rating to the 52-week P/E band on "
                              "UNCHANGED TTM EPS; not a forward estimate or "
                              "a price target", "grade": DERIVED}

    return {
        "pe_trailing": pe_t,
        "historical_band": band,
        "scenarios": scenarios,
        "peers": peers if (peers and peers.get("rows")) else _withheld(
            "no admitted peer set" if peers is None else
            (peers.get("reason") or "peer multiples unavailable")),
        "target_bridge": _withheld(
            "no admitted price target to bridge to on this estimate tier"),
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
    if event["state"] in (EV.RELEASED_PRE_CALL, EV.CALL_IN_PROGRESS):
        out.append({"text": "The earnings call has not concluded; management "
                            "guidance on the call may still revise the read.",
                    "grade": OBSERVED})
    elif event["state"] == EV.POST_CALL_UNVERIFIED:
        out.append({"text": "The call has concluded, but its transcript is "
                            "not yet verified; any guidance nuance beyond the "
                            "filed release is unconfirmed.", "grade": OBSERVED})
    d = _fv(fu.get("debt"))
    if d and _fv(fu.get("cash")) is not None and d > (_fv(fu.get("cash"))
                                                      or 0):
        out.append({"text": "Long-term debt exceeds cash on hand; refinancing "
                            "terms matter if rates stay elevated.",
                    "grade": OBSERVED})
    return out[:3]


# ── page 6: catalysts, variant, monitoring ──────────────────────────────

_BULL = {"Buy", "Outperform"}
_BEAR = {"Underperform", "Sell"}
_STRONG_TAPE = {"Constructive", "Improving"}
_WEAK_TAPE = {"Cautious", "Weak"}


def variant_perception(ratings, snap):
    """The one thing this report says that the tape or the Street does not.

    A variant is not a slogan — it is the specific disagreement between the
    two lenses we actually hold: the Street's consensus rating and our read
    of the tape. When they diverge, that gap IS the variant. When they
    agree, the honest variant is that there is none, and the risk is a
    crowded consensus. With no estimate feed we fall back to the tension
    between the reported fundamentals and the tape. Always DERIVED — it is
    our synthesis, never an observed fact."""
    fr = ratings.get("fundamental") or {}
    tr = ratings.get("tactical") or {}
    tape = tr.get("band") if tr.get("available") else None
    street = fr.get("band") if fr.get("available") else None

    if street and tape:
        if street in _BULL and tape in _WEAK_TAPE:
            text = ("The Street rates the name %s, but price sits below its "
                    "own structure. Our variant is that the fundamental bull "
                    "case is not yet confirmed by the trend — the tape has "
                    "not validated the rating, so an entry waits on a "
                    "reclaim rather than fronts it." % street)
        elif street in _BEAR and tape in _STRONG_TAPE:
            text = ("The Street rates the name %s while the tape is %s. Our "
                    "variant is that price is repairing ahead of the "
                    "consensus view — the technical turn leads the estimates "
                    "here." % (street, tape.lower()))
        elif street in _BULL and tape in _STRONG_TAPE:
            text = ("Consensus (%s) and our tape read (%s) agree. The honest "
                    "variant is that there isn't one: the view is crowded, "
                    "and the risk is owning what everyone already owns into "
                    "any disappointment." % (street, tape.lower()))
        else:
            text = ("Consensus is %s and the tape is %s; the two are not far "
                    "apart. The variant, such as it is, sits in execution and "
                    "timing rather than direction." % (street, tape.lower()))
        return {"available": True, "text": text, "grade": DERIVED}

    # No consensus feed: contrast the reported fundamentals with the tape.
    fu = snap.get("fundamentals") or {}
    fcf = _fv(fu.get("free_cash_flow"))
    nm = _fv(fu.get("net_margin"))
    if tape and (fcf is not None or nm is not None):
        cash = ("positive free cash flow" if (fcf or 0) > 0
                else "negative free cash flow")
        text = ("No admitted consensus to lean on, so the variant is ours to "
                "carry: the business shows %s while the tape is %s. The "
                "report weights the filed cash economics over the momentum, "
                "and says so." % (cash, tape.lower()))
        return {"available": True, "text": text, "grade": DERIVED}
    return _withheld("no consensus and no tape read to form a variant from")


def _report_date(report_time):
    try:
        return EV._et_date(EV._parse(report_time))
    except Exception:
        return None


def fix_catalysts(cat, report_time):
    """Never present a past date as a 'next' event. The vendor next-earnings
    estimate is often the just-released quarter's own date; once that date
    is on or before the report, it is not 'next'. Drop past next-events and,
    when none remain, estimate the following print roughly one quarter after
    the last release, clearly labelled as an estimate."""
    import datetime as _dt
    cat = dict(cat or {})
    rt = _report_date(report_time)
    nxt = cat.get("next") or []

    def _d(x):
        try:
            return _dt.date.fromisoformat(str(x.get("when"))[:10])
        except Exception:
            return None

    future = [n for n in nxt if not (rt and _d(n) and _d(n) <= rt)]
    dropped = len(nxt) - len(future)
    cat["next"] = future
    if dropped:
        cat["dropped_past_next"] = dropped

    if not future:
        lr = cat.get("last_reported") or {}
        base = None
        try:
            base = _dt.date.fromisoformat(str(lr.get("when_utc")
                                              or lr.get("when"))[:10])
        except Exception:
            base = None
        if base:
            est = base + _dt.timedelta(days=91)
            cat["next"] = [{
                "what": "next earnings (estimated ~one quarter after the "
                        "last release; not company-confirmed)",
                "when": est.isoformat(), "when_utc": est.isoformat(),
                "confirmation": "estimated from the last release date",
                "grade": DERIVED, "estimated": True}]
            cat["next_estimated"] = True
    return cat


def earnings_marker_dates(snap):
    """The earnings-release dates a chart may honestly mark: only ones the
    snapshot actually carries as events, never a guessed cadence. The
    verified latest release is the catalyst event; the 8-K acceptance is
    the same event's filing. Deduped by day."""
    out = []
    cat = snap.get("catalyst") or {}
    if cat.get("event_kind") in ("primary_release", "earnings") and \
            cat.get("event_dt"):
        out.append(str(cat["event_dt"])[:10])
    ex = snap.get("exhibit") or {}
    if ex.get("accepted"):
        out.append(str(ex["accepted"])[:10])
    seen, uniq = set(), []
    for d in out:
        if d and d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq


def monitoring(snap):
    """The confirm/break triggers and the checklist, taken from the v3
    decision block — the same dated, evidence-linked levels. One thing is
    sanitised: v3's monitor_next prose can carry a '; next earnings
    estimated <date>' tail with the just-released quarter's own date, which
    would render a past 'next' event. v4 shows next earnings in its own
    catalyst section, so that redundant clause is stripped here rather than
    left to contradict the event state."""
    import re as _re
    dec = snap.get("decision") or {}
    mn = dec.get("monitor_next")
    if isinstance(mn, str):
        mn = _re.sub(r";?\s*next earnings estimated \d{4}-\d{2}-\d{2}",
                     "", mn).strip().rstrip(";").strip()
    return {
        "upgrade_trigger": dec.get("upgrade_trigger"),
        "downside_confirmation": dec.get("downside_confirmation"),
        "monitor_next": mn,
        "recovery_stages": dec.get("recovery_stages") or [],
        "review_date": dec.get("review_date"),
    }


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

    # thesis_facts returns {"facts": [...], "dropped_social": N} where a
    # fact is a bare string or a {text, grade} dict. Normalise to a list
    # of {text, grade} so every consumer reads one shape.
    _tf = M3.thesis_facts(snap, limit=3)
    thesis = []
    for f in (_tf.get("facts") or []):
        if isinstance(f, dict):
            thesis.append({"text": f.get("text") or "",
                           "grade": f.get("grade") or OBSERVED})
        else:
            thesis.append({"text": str(f), "grade": OBSERVED})

    ratings = {
        "fundamental": fundamental_rating(estimates, event),
        "tactical": tactical_rating(snap),
        "target": price_target(estimates, event, price),
    }

    return {
        "ticker": snap.get("ticker"),
        "price": price,
        "event": event,
        "flash": event.get("flash"),
        "ratings": ratings,
        "thesis": thesis,
        "risks": risks(snap, event),
        "business": M3.business_description(snap),
        "financials": financials(snap, estimates),
        "valuation": valuation(snap, estimates, peers, price),
        "catalysts": fix_catalysts(M3.catalysts(snap), report_time),
        "variant": variant_perception(ratings, snap),
        "monitoring": monitoring(snap),
        "chart": {"earnings_dates": earnings_marker_dates(snap)},
        "insiders": M3.insider_view(snap),
        "ownership": M3.ownership_view(snap),
        "options": M3.options_view(snap),
        "horizon": "12-month fundamental view; swing (2-8 week) tactical "
                   "overlay",
        "estimates_configured": bool((estimates or {}).get("configured")),
    }
