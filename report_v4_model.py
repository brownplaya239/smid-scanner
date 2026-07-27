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
        # Never surface the provider's raw reason (it can name the missing
        # API-key env var) in a client-facing withheld line.
        return _withheld("no admitted estimate source (estimates feed not "
                         "configured for this run)")
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
    # A name too young for a 50- or 200-day average is scored against the
    # averages it HAS. The count of those is published so the renderer
    # never claims "0 of the 20/50/200" about a stock with one average.
    n_mas = sum(1 for k in ("ma20", "ma50", "ma200")
                if _fv(lv.get(k)) is not None)
    above = sum(1 for k in ("ma20", "ma50", "ma200")
                if _fv(lv.get(k)) is not None and px is not None
                and px > _fv(lv.get(k)))
    band = ("Constructive" if above == n_mas and n_mas else
            "Improving" if above >= 2 else
            "Cautious" if above == 1 else "Weak")
    return {"available": True, "band": band, "above_mas": above,
            "mas_available": n_mas,
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
                         % ("is not included in our data plan"
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

_KPI_LABELS = [
    ("subscription_revenue", "Subscription revenue", "money"),
    ("crpo", "cRPO (current RPO, next 12 mo)", "money"),
    ("rpo", "RPO (total remaining performance obligations)", "money"),
    ("ai_acv", "AI annual contract value", "money_floor"),
    ("acv_over_1m_net_new_deals", "Deals over $1M net-new ACV", "count"),
    ("acv_over_5m_customers", "Customers over $5M ACV", "count"),
]


def saas_kpis(snap):
    """The operating KPIs — subscription revenue, cRPO/RPO, ACV milestones,
    deal and customer counts — parsed from the issuer's earnings-release
    exhibit (sec_exhibit). These are filed facts stated in the release, so
    they are OBSERVED, dated to the release. Withheld only when the exhibit
    was not ingested (in which case the event gate has already held the
    report)."""
    ex = snap.get("exhibit") or {}
    kpis = ex.get("kpis") or {}
    if not kpis:
        return _withheld(
            "the earnings-release exhibit was not ingested, so cRPO/RPO, "
            "ACV and customer metrics could not be read")
    rows = []
    for key, label, kind in _KPI_LABELS:
        rec = kpis.get(key)
        if not rec:
            continue
        rows.append({"key": key, "label": label, "kind": kind,
                     "value": rec.get("value"),
                     "growth_yoy_pct": rec.get("growth_yoy_pct"),
                     "basis": rec.get("basis"), "raw": rec.get("raw")})
    if not rows:
        return _withheld("the exhibit was ingested but carried no recognised "
                         "operating KPIs")
    return {"available": True, "rows": rows, "grade": OBSERVED,
            "source": "issuer earnings release (8-K exhibit)",
            "accession": ex.get("accession"), "as_of": ex.get("accepted")}


def financials(snap, estimates):
    """The reported quarter, its margins and cash, the surprise history the
    free tier supplies, and the SaaS operating KPIs when the issuer release
    was ingested. Forward consensus is withheld on a gated tier."""
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
    # An issuer that has never filed a 10-K or 10-Q has no reported
    # quarter — not a quarter of blanks. Seven rows of "n/a" is filler
    # dressed as a table; one line that says why is the honest render.
    no_filings = not any(v is not None for v in reported.values())
    return {
        "reported": reported,
        "no_reported_period": no_filings,
        "surprises": surprises,
        "forward_consensus": (
            {"available": True}
            if (est.get("eps_estimate_next") or est.get("rev_estimate_next"))
            else _withheld(
                "forward consensus %s"
                % ("is not included in our data plan"
                   if (est.get("coverage") or {}).get("eps_estimate")
                   == "premium-gated" else "not available"))),
        "saas_kpis": saas_kpis(snap),
    }


# ── business analysis (page 2) ──────────────────────────────────────────

def business_analysis(snap):
    """Analysis, not description: what the admitted numbers say about the
    business — enterprise motion, revenue visibility, the growth/cash
    balance, and the AI franchise sized against the issuer's own guidance.
    Every figure is derived from filed facts with its formula stated, so
    this reads like research without inventing a single datum. Sections
    that lack inputs are omitted, not filled."""
    fu = snap.get("fundamentals") or {}
    ex = snap.get("exhibit") or {}
    kpis = ex.get("kpis") or {}
    hl = ex.get("guidance_highlights") or {}
    out = {"sections": [], "grade": DERIVED}

    sub = kpis.get("subscription_revenue") or {}
    crpo = kpis.get("crpo") or {}
    rpo = kpis.get("rpo") or {}
    ai = kpis.get("ai_acv") or {}
    d1m = kpis.get("acv_over_1m_net_new_deals") or {}
    c5m = kpis.get("acv_over_5m_customers") or {}
    sub_v = sub.get("value")
    run_rate = sub_v * 4.0 if sub_v else None

    # Enterprise motion: the large-deal and large-customer counts the
    # issuer files are direct evidence of who the customer is and how the
    # motion works.
    if c5m.get("value") or d1m.get("value"):
        bits = []
        if c5m.get("value"):
            floor = c5m["value"] * 5e6
            bits.append("%d customers above $5M in ACV — at least %s of "
                        "annualised contract value from the largest "
                        "accounts alone" % (c5m["value"], _money_s(floor)))
        if d1m.get("value"):
            bits.append("%d transactions above $1M net-new ACV closed in "
                        "the quarter" % d1m["value"])
        out["sections"].append({
            "title": "Enterprise motion",
            "text": "The customer base is concentrated in large enterprise "
                    "and expanding within it: %s. Growth is driven by "
                    "landing and expanding large accounts, not volume of "
                    "small ones." % "; ".join(bits),
            "grade": DERIVED})

    # Revenue visibility: booked backlog against the current run-rate.
    if run_rate and crpo.get("value"):
        cov = 100.0 * crpo["value"] / run_rate
        yrs = (rpo["value"] / run_rate) if rpo.get("value") else None
        text = ("Contracted backlog gives unusual visibility: cRPO of %s "
                "covers roughly %.0f%% of the next twelve months at the "
                "current subscription run-rate (%s annualised)"
                % (_money_s(crpo["value"]), cov, _money_s(run_rate)))
        if yrs:
            text += (", and total RPO of %s is about %.1f years of that "
                     "run-rate already under contract"
                     % (_money_s(rpo["value"]), yrs))
        text += (". The revenue model is subscription-first, so the near "
                 "term is largely booked before the quarter begins.")
        out["sections"].append({"title": "Revenue visibility",
                                "text": text, "grade": DERIVED})

    # Growth against cash generation, on the figures we actually hold.
    fcf = _fv(fu.get("free_cash_flow"))
    rev_q = _fv(fu.get("revenue_q"))
    g = sub.get("growth_yoy_pct")
    if fcf is not None and rev_q and g is not None:
        fm = 100.0 * fcf / rev_q
        text = ("Subscription growth of %.1f%% against a quarterly free-"
                "cash-flow margin of %.1f%% (FCF / total revenue, OCF minus "
                "capex basis) puts the growth-plus-cash balance near %.0f "
                "on this quarter's GAAP-derived figures. Issuer-defined FCF "
                "excludes items this calculation does not, so the company's "
                "own framing will read higher." % (g, fm, g + fm))
        out["sections"].append({"title": "Growth and cash balance",
                                "text": text, "grade": DERIVED})

    # The AI franchise, sized against the issuer's own full-year guide.
    if ai.get("value"):
        fy = hl.get("fy_subscription_revenue") or {}
        mid = ((fy.get("low", 0) + fy.get("high", 0)) / 2.0
               if fy.get("low") else None)
        text = ("The issuer states AI has crossed %s in annual contract "
                "value" % _money_s(ai["value"]))
        if mid:
            text += (" — at least %.0f%% of the full-year subscription-"
                     "revenue guidance midpoint (%s), and the fastest-"
                     "compounding line the release discloses"
                     % (100.0 * ai["value"] / mid, _money_s(mid)))
        text += (". Whether that line re-accelerates bookings is the swing "
                 "factor in the forward-growth debate.")
        out["sections"].append({"title": "AI franchise",
                                "text": text, "grade": DERIVED})

    out["sufficient"] = bool(out["sections"])
    return out


def _money_s(v):
    if v is None:
        return "n/a"
    a = abs(v)
    if a >= 1e9:
        return "$%.2fB" % (v / 1e9)
    if a >= 1e6:
        return "$%.0fM" % (v / 1e6)
    return "$%s" % ("{:,.0f}".format(v))


# ── valuation ───────────────────────────────────────────────────────────

def valuation(snap, estimates, peers, price):
    """Value on inputs that are real, never on a tautology.

    v4.0 divided the 52-week PRICE range by today's EPS and called it a P/E
    band — which made the bear/bull 'implied prices' identical to the price
    low and high by construction. That is circular and is gone. What
    remains is what the filings and this tier actually support: the
    trailing P/E, enterprise value and the multiples that derive from it
    (EV / run-rate revenue, run-rate FCF yield), and a sourced peer table.

    A true historical MULTIPLE band needs point-in-time historical EPS, and
    forward scenarios need issuer guidance — neither is available on this
    tier, so both are WITHHELD with their reason rather than manufactured.
    """
    val = snap.get("valuation") or {}
    fu = snap.get("fundamentals") or {}
    co = snap.get("company") or {}
    pe_t = _fv(val.get("pe_trailing"))
    mcap = _fv(co.get("market_cap"))
    cash = _fv(fu.get("cash"))
    debt = _fv(fu.get("debt"))
    rev_q = _fv(fu.get("revenue_q"))
    fcf_q = _fv(fu.get("free_cash_flow"))

    trailing_pe = ({"available": True, "value": round(pe_t, 1),
                    "basis": "price / trailing-twelve-month EPS",
                    "grade": DERIVED} if pe_t else
                   _withheld("no positive TTM EPS"))

    ev = None
    if mcap is not None and cash is not None and debt is not None:
        ev = mcap + debt - cash
        enterprise = {"available": True, "value": round(ev, 0),
                      "market_cap": mcap, "debt": debt, "cash": cash,
                      "basis": "market cap + total debt - cash & equivalents",
                      "grade": DERIVED}
    else:
        enterprise = _withheld("enterprise value needs market cap, debt and "
                               "cash; one is missing")

    # Run-rate multiples: the latest quarter annualised. Labelled run-rate,
    # never presented as a trailing-twelve-month figure, since only the
    # single quarter is filed for these lines on this tier.
    if ev is not None and rev_q:
        ev_rev = {"available": True, "value": round(ev / (rev_q * 4.0), 1),
                  "basis": "EV / annualised latest-quarter revenue "
                           "(run-rate, not LTM)", "grade": DERIVED}
    else:
        ev_rev = _withheld("no enterprise value or quarterly revenue")

    if mcap and fcf_q is not None:
        fcf_yield = {"available": True,
                     "value": round(100.0 * (fcf_q * 4.0) / mcap, 1),
                     "basis": "annualised latest-quarter free cash flow / "
                              "market cap (run-rate)", "grade": DERIVED}
    else:
        fcf_yield = _withheld("no market cap or quarterly free cash flow")

    peer_block = (peers if (peers and peers.get("rows")) else _withheld(
        "no admitted peer set" if peers is None else
        (peers.get("reason") or "peer multiples unavailable")))

    available = any(b.get("available") for b in
                    (trailing_pe, enterprise, ev_rev, fcf_yield, peer_block))

    return {
        "available": available,
        "pe_trailing": pe_t,                     # kept for masthead/back-compat
        "trailing_pe": trailing_pe,
        "enterprise_value": enterprise,
        "ev_to_revenue": ev_rev,
        "fcf_yield": fcf_yield,
        "peers": peer_block,
        "historical_multiples": _withheld(
            "a point-in-time historical EPS series is not available on this "
            "tier, so a real historical multiple band cannot be built; the "
            "52-week PRICE range divided by current EPS would be circular"),
        "forward_scenarios": _withheld(
            "no admitted forward guidance or consensus estimates on this "
            "tier; bull/base/bear prices are withheld until the issuer "
            "release is ingested rather than derived from the price range"),
        "grade": DERIVED,
    }


# ── risks ───────────────────────────────────────────────────────────────

def risks(snap, event):
    """Three ranked risks, each anchored to a filed fact or the event
    state — never a generic list. Ordered most-concrete first, so the
    company-specific ones the release surfaces lead over the generic ones."""
    out = []
    fu = snap.get("fundamentals") or {}
    lv = snap.get("levels") or {}
    ex = snap.get("exhibit") or {}
    kpis = ex.get("kpis") or {}
    hl = ex.get("guidance_highlights") or {}
    px = M3.spot(snap)

    # Forward bookings decelerating below reported revenue — the specific,
    # data-grounded risk a SaaS release surfaces, not a generic one.
    sub, crpo = kpis.get("subscription_revenue"), kpis.get("crpo")
    if sub and crpo and sub.get("growth_yoy_pct") is not None \
            and crpo.get("growth_yoy_pct") is not None \
            and crpo["growth_yoy_pct"] < sub["growth_yoy_pct"] - 1.5:
        out.append({"text": "Forward bookings are growing below reported "
                            "revenue: cRPO +%.0f%% versus subscription revenue "
                            "+%.0f%%, so current growth may not sustain into "
                            "the next year absent re-acceleration."
                            % (crpo["growth_yoy_pct"], sub["growth_yoy_pct"]),
                    "grade": OBSERVED})
    # FX headwind, when the issuer flags one in guidance.
    if hl.get("fx_commentary"):
        out.append({"text": "Currency headwind flagged in guidance (%s); a "
                            "stronger dollar pressures reported and cRPO "
                            "growth." % str(hl["fx_commentary"]).rstrip(". "),
                    "grade": OBSERVED})

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

    # When the issuer release gives forward bookings, the sharpest variant
    # is grounded in the data: is cRPO (12-month forward revenue) growing
    # with, ahead of, or behind reported revenue — the real growth-durability
    # debate, not a tape-vs-consensus abstraction.
    kpis = (snap.get("exhibit") or {}).get("kpis") or {}
    sub, crpo = kpis.get("subscription_revenue"), kpis.get("crpo")
    ai = kpis.get("ai_acv")
    if sub and crpo and sub.get("growth_yoy_pct") is not None \
            and crpo.get("growth_yoy_pct") is not None:
        sg, cg = sub["growth_yoy_pct"], crpo["growth_yoy_pct"]
        ai_note = (" The AI ACV inflection (now past $%.0fB) is the swing "
                   "factor for whether that gap closes."
                   % (ai["value"] / 1e9) if ai and ai.get("value") else "")
        if cg < sg - 1.5:
            text = ("Forward bookings are decelerating below reported "
                    "revenue: cRPO +%.0f%% versus subscription revenue "
                    "+%.0f%%. The debate the multiple has to resolve is "
                    "whether that gap is a temporary comparison or the start "
                    "of a durable slowdown.%s" % (cg, sg, ai_note))
        elif cg > sg + 1.5:
            text = ("Forward bookings are outrunning reported revenue: cRPO "
                    "+%.0f%% versus subscription +%.0f%%, a leading signal "
                    "that current growth understates demand — the variant is "
                    "that the P/E is looking at the trailing, not the "
                    "forward, curve.%s" % (cg, sg, ai_note))
        else:
            text = ("Forward bookings track reported revenue closely (cRPO "
                    "+%.0f%% versus subscription +%.0f%%); the debate is "
                    "durability of that rate against the multiple the stock "
                    "still carries.%s" % (cg, sg, ai_note))
        return {"available": True, "text": text, "grade": DERIVED}

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

def data_confidence(snap, estimates, view_bits=None):
    """One page-1 line: High / Medium / Low, with the two or three facts
    that set it. Graded mechanically from what actually loaded — filed
    financials, an estimate feed, and trading history — never from how the
    narrative feels. The reasons are the grade; a level with no reasons
    would be an opinion."""
    reasons_neg, reasons_pos = [], []
    fu = snap.get("fundamentals") or {}
    has_filed = any(isinstance(v, dict) and v.get("value") is not None
                    for k, v in fu.items() if k in
                    ("revenue_q", "net_income_q", "operating_cash_flow"))
    if has_filed:
        reasons_pos.append("filed SEC financials ingested")
    else:
        reasons_neg.append("no filed 10-K/10-Q financials")
    est = estimates or {}
    if est.get("recommendation"):
        reasons_pos.append("analyst consensus feed connected")
    else:
        reasons_neg.append("no analyst estimate feed")
    th = snap.get("trading_history") or {}
    if th.get("full_history") is False:
        reasons_neg.append("only %s trading sessions since the %s listing"
                           % (th.get("sessions"), th.get("listing_date")))
    else:
        reasons_pos.append("full-year trading history")
    ex = (snap.get("exhibit") or {})
    if ex.get("disposition") == "ADMITTED":
        reasons_pos.append("issuer earnings release read in")
    n_bad = len(reasons_neg)
    level = "High" if n_bad == 0 else "Medium" if n_bad == 1 else "Low"
    return {"level": level,
            "reasons": (reasons_neg + reasons_pos)[:3]}


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
        "business_analysis": business_analysis(snap),
        "financials": financials(snap, estimates),
        "valuation": valuation(snap, estimates, peers, price),
        "catalysts": fix_catalysts(M3.catalysts(snap), report_time),
        "variant": variant_perception(ratings, snap),
        "monitoring": monitoring(snap),
        "chart": {"earnings_dates": earnings_marker_dates(snap)},
        "trading_history": snap.get("trading_history") or {},
        "insiders": M3.insider_view(snap),
        "ownership": M3.ownership_view(snap),
        "options": M3.options_view(snap),
        "horizon": "12-month fundamental view; swing (2-8 week) tactical "
                   "overlay",
        "data_confidence": data_confidence(snap, estimates),
        "estimates_configured": bool((estimates or {}).get("configured")),
    }
