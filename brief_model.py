#!/usr/bin/env python3
"""brief_model.py — the canonical view of one email.

Both bodies are rendered from this. That is the whole point: the HTML and
the plain text previously each decided for themselves how many contracts
to show, and drifted — six market-wide contracts in text against three in
HTML, describing the same email. Any rule applied here applies to both by
construction, and the parity test compares these records rather than the
prose either renderer produced from them.

A section is {id, title, sub, kind, records, empty_line}. A record is a
plain dict whose `key` uniquely identifies it inside its section, so two
renderings can be compared without parsing sentences.

    python brief_model.py --self-test
"""

import sys

import brief_compose as BC
import brief_schema as BS
import brief_time as BT

# Flow's share of the email is a budget, enforced here rather than in each
# renderer. Market-wide entries point at a name; the user's own names get
# the contract detail, because there it is the point.
MAX_MARKET_TICKERS = 3
MAX_MARKET_CONTRACTS = 1
MAX_DRIVING_TICKERS = 5
MAX_OTHER_TICKERS = 2
MAX_CONTRACTS_PER_TICKER = 2

ACTION_BUY, ACTION_SELL, ACTION_TWO_SIDED, ACTION_UNKNOWN = (
    "BUY", "SELL", "TWO-SIDED", "UNSPECIFIED")
DIR_BULL, DIR_BEAR, DIR_NONE = "bullish", "bearish", "unresolved"

_ACTION = {
    "call_buy": ACTION_BUY, "call_buyer": ACTION_BUY,
    "put_buy": ACTION_BUY, "put_buyer": ACTION_BUY,
    "call_sell": ACTION_SELL, "call_seller": ACTION_SELL,
    "put_sell": ACTION_SELL, "put_seller": ACTION_SELL,
}


def contract_record(c, site="https://tickerdesk.io"):
    """One contract with everything needed to judge it.

    A row that says only "CALL 380 · $42.8M" leaves the reader to guess
    whether somebody bought or sold it — which inverts the meaning.
    """
    side = (c.get("side") or c.get("flow_side") or "").strip().lower()
    action = _ACTION.get(side, ACTION_TWO_SIDED if side in BC._AMBIG_SIDES
                         else ACTION_UNKNOWN)
    d = BC.contract_direction(c)
    direction = {BC.BULLISH: DIR_BULL, BC.BEARISH: DIR_BEAR}.get(d, DIR_NONE)
    right = (c.get("right") or "").upper() or "?"
    ts = BT.parse_iso(c.get("printed_at") or "")
    oi_ts = BT.parse_iso(c.get("oi_as_of") or "")
    return {
        "key": "%s|%s|%s|%s" % (c.get("ticker"), right, c.get("strike"),
                                c.get("expiry")),
        "ticker": c.get("ticker"),
        "right": right,
        "strike": c.get("strike"),
        "expiry": c.get("expiry"),
        "action": action,
        "direction": direction,
        "side_label": "%s %s · %s" % (right, action, direction),
        "premium": c.get("premium") or "",
        "spot": c.get("spot"),
        "flow_at": BT.fmt_stamp(ts) if ts else "",
        "oi_state": c.get("oi_state") or BC.CONF_PENDING,
        "oi_as_of": BT.fmt_stamp(oi_ts) if oi_ts else "",
        "sweep": bool(c.get("is_sweep")),
        "url": "%s/#ticker=%s" % (site, c.get("ticker")),
    }


def _rank(rows):
    def key(c):
        p = c.get("premium_raw")
        score = float(p) if isinstance(p, (int, float)) else 0.0
        if c.get("is_sweep"):
            score *= 1.4
        if c.get("golden"):
            score *= 1.3
        if str(c.get("tier") or "").startswith("A"):
            score *= 1.25
        if c.get("oi_state") == BC.CONF_YES:
            score *= 1.5
        elif c.get("oi_state") == BC.CONF_NO:
            score *= 0.6
        return -score
    return sorted(rows or [], key=key)


def _group(rows, max_tickers, max_each, site):
    """Cap by TICKER, then by contract. Six rows of one name is one idea
    wearing six hats, and it crowds out five other names."""
    order, groups = [], {}
    for c in _rank(rows):
        tk = c.get("ticker")
        if tk not in groups:
            if len(order) >= max_tickers:
                continue
            order.append(tk)
            groups[tk] = []
        if len(groups[tk]) < max_each:
            groups[tk].append(contract_record(c, site))
    return order, groups


def build(market, wl, *, news=None, market_flow=None, watch_flow=None,
          discovery=None, weekly=None, earnings=None,
          followthrough=None,
          site="https://tickerdesk.io", unsub="", as_of="",
          summary_lines=None, preview=True):
    sections = []
    reg_in = market.get("regime") or {}
    # ONE vocabulary. The headline said TRANSITION while changed_from said
    # "mixed" — the same regime under two names, in one email.
    regime = dict(reg_in)
    regime["label"] = BS.regime_display(reg_in.get("label"))
    regime["display"] = regime["label"]
    if reg_in.get("changed_from"):
        regime["changed_from"] = BS.regime_display(reg_in["changed_from"])
        regime["prior_display"] = regime["changed_from"]

    # ── market
    idx = market.get("indices") or {}
    rows = []
    for t in ("SPY", "QQQ", "IWM", "DIA"):
        d = idx.get(t)
        if not d:
            continue
        dist = d.get("dist_ma20_pct")
        rows.append({
            "key": t, "ticker": t, "last": d.get("last"),
            "d1": d.get("chg_1d_pct"), "w1": d.get("chg_1w_pct"),
            "ytd": d.get("chg_ytd_pct"), "vs20": dist,
            "ma_state": BC.ma_state(dist),
            "url": "%s/#ticker=%s" % (site, t),
        })
    sections.append({
        "id": "market", "title": "Market in 30 seconds", "kind": "index",
        "sub": "%s · as of %s" % (market.get("session_label") or "",
                                  market.get("as_of_et") or ""),
        "records": rows,
        "regime": regime,
        "ma_summary": BC.ma_state_summary([r["vs20"] for r in rows]),
        "event": market.get("top_event") or {},
        "alert_line": wl.get("alert_line") or "",
        "mixed_sessions": bool(market.get("mixed_sessions")),
    })

    # ── watch list
    wrecords = []
    for x in wl.get("shown") or []:
        wrecords.append({
            "key": x["ticker"], "ticker": x["ticker"],
            "status": x["bucket"],
            "status_basis": x.get("status_basis") or "",
            "reason_codes": x.get("reason_codes") or [],
            "reasons": x.get("reasons") or [],
            "price": x.get("price_record") or BC.price_record(
                x.get("price"), BC.BASIS_CLOSE, x.get("price_as_of") or ""),
            "flow_quality": x.get("signal_strength"),
            "evidence": x.get("evidence"),
            "edge": x.get("edge"),
            "technical": x.get("technical") or "",
            "next_confirmation": x.get("next_confirmation") or "",
            "invalidation": x.get("invalidation") or "",
            "url": "%s/#ticker=%s" % (site, x["ticker"]),
        })
    sections.append({
        "id": "watchlist", "title": "Your watch list", "kind": "watch",
        "sub": "Ranked by what changed since the last brief",
        "records": wrecords,
        "overflow_line": wl.get("overflow_line") or "",
        "notable_line": wl.get("notable_line") or "",
        "quiet_line": wl.get("quiet_line") or "",
        "overflow_url": "%s/#watchlist" % site,
    })

    # ── flow, split so every ranking claim is traceable
    watch_flow = dict(watch_flow or {})
    driving_names = [x["ticker"] for x in (wl.get("shown") or [])
                     if x["ticker"] in watch_flow]
    other_names = [t for t in watch_flow if t not in set(driving_names)]

    def flow_section(sid, title, names, cap, sub):
        recs = []
        for tk in names[:cap]:
            group = watch_flow.get(tk) or []
            # the aggregate verdict reads EVERY contract; the rendered rows
            # are a subset. Conflating the two let a ticker announce
            # "three prints" above two rows.
            v = BC.classify_flow(group)
            shown = _rank(group)[:MAX_CONTRACTS_PER_TICKER]
            total = len(group)
            disp = len(shown)
            states = [c.get("oi_state") or BC.CONF_PENDING for c in group]
            recs.append({
                "key": tk, "ticker": tk, "verdict": v["label"],
                "direction": v["direction"], "score": v["score"],
                "explain": v["explain"],
                "contract_count_total": total,
                "contract_count_displayed": disp,
                "contract_count_omitted": total - disp,
                "confirmed_count": states.count(BC.CONF_YES),
                "pending_count": states.count(BC.CONF_PENDING),
                "unresolved_count": states.count(BC.CONF_NO),
                "omitted_line": ("showing %d of %d contracts"
                                 % (disp, total)) if total > disp else "",
                "url": "%s/#ticker=%s" % (site, tk),
                "contracts": [contract_record(c, site) for c in shown],
            })
        return {"id": sid, "title": title, "kind": "flow_group",
                "sub": sub, "records": recs}

    mkt_order, mkt_groups = _group(market_flow or [], MAX_MARKET_TICKERS,
                                   MAX_MARKET_CONTRACTS, site)
    flow_sections = []
    if driving_names:
        flow_sections.append(flow_section(
            "flow_driving", "Driving today's watch-list changes",
            driving_names, MAX_DRIVING_TICKERS,
            "The contracts behind the statuses above"))
    if other_names:
        flow_sections.append(flow_section(
            "flow_other", "Other notable watch-list flow",
            other_names, MAX_OTHER_TICKERS,
            "Prints on your names that did not move a status"))
    if mkt_order:
        flow_sections.append({
            "id": "flow_market", "title": "Market-wide flow",
            "kind": "flow_flat",
            "sub": "Largest unusual prints outside your list",
            "records": [c for tk in mkt_order for c in mkt_groups[tk]],
        })
    sections.extend(flow_sections)
    if flow_sections:
        sections.append({"id": "flow_link", "title": "", "kind": "link",
                         "sub": "", "records": [
                             {"key": "all_flow", "text": "View all flow",
                              "url": "%s/#flow" % site}]})

    # ── calendar
    evs = []
    for e in (market.get("events") or [])[:5]:
        corr = e.get("correction") or {}
        evs.append({
            "key": BS.record_key(e.get("vendor_title") or e.get("title"),
                                 e.get("starts_at")),
            "time_et": e.get("time_et") or "--",
            "title": e.get("title") or "",
            "status": e.get("status") or "",
            "source_time": e.get("source_time") or "",
            "source_tz": e.get("source_tz") or "",
            "venue": e.get("venue") or "",
            "venue_tz": e.get("venue_tz") or "",
            # the vendor's own record, kept verbatim so the correction can
            # be audited against what was actually published
            "vendor_title": e.get("vendor_title") or "",
            "vendor_time": e.get("vendor_time") or "",
            "vendor_tz": e.get("vendor_tz") or "",
            "vendor_url": e.get("vendor_url") or "",
            "corrected": bool(corr),
            "corrected_title": corr.get("corrected_title") or "",
            "corrected_start": corr.get("corrected_start") or "",
            "correction_source_url": corr.get("correction_source_url") or "",
            "correction_authority": corr.get("correction_authority") or "",
            "correction_timestamp": corr.get("correction_timestamp") or "",
            "correction_reason": corr.get("correction_reason") or "",
            # The visible link must support what is DISPLAYED. A corrected
            # row therefore links to the correction's source or to NOTHING:
            # falling back to the vendor page would send the reader to a
            # page still headed "President Trump Speaks" at 3:00pm, which
            # contradicts the row they clicked.
            "url": (corr.get("correction_source_url") or "") if corr
                   else (e.get("source_url") or ""),
            "authority": e.get("title_authority") or "vendor",
        })
    if evs:
        sections.append({"id": "calendar", "title": "Macro calendar",
                         "kind": "event", "records": evs,
                         "sub": "Times converted to Eastern from each "
                                "source's own zone."})

    # ── news, with an explicit empty state
    sel = news or {}
    nrecs = []
    for scope, items in (("market", sel.get("market") or []),
                         ("watchlist", sel.get("watchlist") or [])):
        for it in items:
            nrecs.append({
                # the adapter's content hash, not a headline slice: two
                # stories opening the same way collided, and an edited
                # headline became a different record overnight
                "key": it.get("key") or BS.record_key(it.get("url"),
                                                      it["headline"]),
                "scope": scope,
                "headline": it["headline"], "source": it["source"],
                "published_et": it["published_et"], "tier": it["tier"],
                "why": it["why"], "url": it.get("url") or "",
                "tickers": it.get("watch_tickers") or [],
            })
    sections.append({
        "id": "news", "title": "Top news", "kind": "news", "records": nrecs,
        "sub": "Market first, then your names. PRIMARY marks the issuer's "
               "own statement.",
        "empty_line": sel.get("empty_line")
        or "No high-relevance headlines since the previous brief.",
    })

    # ── yesterday's OI follow-through
    ft = followthrough or {}
    frecs = []
    for r in (ft.get("rows") or [])[:5]:
        frecs.append({
            "key": r["contract"], "rank": r.get("rank"),
            "ticker": r.get("ticker"), "contract": r.get("contract"),
            "right": r.get("right"), "strike": r.get("strike"),
            "expiry": r.get("expiry"),
            # direction and OI are separate claims and never merged
            "action": r.get("action"), "direction": r.get("direction"),
            "flow_at": r.get("flow_at"),
            "observed_contracts": r.get("observed_contracts"),
            "premium": r.get("premium"),
            "oi_before": r.get("oi_before"), "oi_after": r.get("oi_after"),
            "delta_oi": r.get("delta_oi"),
            "follow_through_ratio": r.get("follow_through_ratio"),
            "structure": r.get("structure"),
            "structure_confidence": r.get("structure_confidence"),
            "oi_state": r.get("state"),
            "oi_data_date": r.get("oi_after_trade_date"),
            "oi_verified_at": r.get("oi_verified_at"),
            "url": "%s/#ticker=%s" % (site, r.get("ticker")),
        })
    if frecs or ft.get("pending_line"):
        sections.append({
            "id": "oi_followthrough",
            "title": "Yesterday's OI Follow-Through",
            "kind": "followthrough", "records": frecs,
            "sub": ft.get("sub") or "",
            # the not-yet-posted case is stated, and states plainly that
            # this email is a snapshot rather than something that updates
            "empty_line": ft.get("pending_line") or "",
            "desk_url": "%s/#flow" % site,
            "desk_line": ft.get("desk_line") or "",
        })

    # ── earnings: who reports today, what the options pay for, what the
    # stock usually does
    erecs = []
    for r in (earnings or {}).get("records") or []:
        erecs.append({
            "key": "%s|%s" % (r["ticker"], r["session"]),
            "ticker": r["ticker"], "company": r.get("company") or "",
            "session": r["session"], "on_watchlist": bool(r.get("on_watchlist")),
            "implied_move_pct": r.get("implied_move_pct"),
            "iv_pct": r.get("iv_pct"), "iv_level": r.get("iv_level"),
            "realized_med_pct": r.get("realized_med_pct"),
            "n_reports": r.get("n_reports"),
            "dte": r.get("dte"), "expiry": r.get("expiry"),
            "verdict": r.get("verdict") or "", "why": r.get("why") or "",
            "url": "%s/#ticker=%s" % (site, r["ticker"]),
        })
    if erecs:
        c = (earnings or {}).get("counts") or {}
        sections.append({
            "id": "earnings", "title": "Earnings today", "kind": "earnings",
            "records": erecs,
            "sub": "%d before the open · %d after the close · implied move "
                   "from the front expiry" % (c.get("bmo") or 0,
                                              c.get("amc") or 0),
            "note": (earnings or {}).get("note") or "",
        })

    # ── weekly + discovery
    if weekly and weekly.get("line"):
        # the weekly line quotes regime labels too, and must use the same
        # display vocabulary as the headline above it
        line = weekly["line"]
        for internal in ("risk_off", "risk_on", "mixed", "transition",
                         "balanced"):
            line = line.replace(internal, BS.regime_display(internal))
        sections.append({"id": "weekly", "title": "Weekly lens",
                         "kind": "prose", "sub": weekly.get("sub") or "",
                         "weekly_regime": {
                             "label": regime["label"],
                             "prior_display": regime.get("prior_display", "")},
                         "records": [{"key": "weekly", "text": line}]})
    drecs = []
    for i, x in enumerate(discovery or [], 1):
        drecs.append({
            "key": x["ticker"], "rank": i, "ticker": x["ticker"],
            "contract": x.get("contract") or "",
            "side_label": x.get("side_label") or "",
            "premium": x.get("premium") or "",
            "oi_state": x.get("oi_state") or "",
            "why": x.get("why") or "",
            "url": "%s/#ticker=%s" % (site, x["ticker"]),
        })
    if drecs:
        sections.append({"id": "discovery", "title": "Market discovery",
                         "kind": "discovery", "records": drecs,
                         "sub": "Not on your watch list · ranked by signal "
                                "strength"})

    # Subject and preheader are BUILT HERE, from the sections that exist,
    # and stored on the envelope. Computing them outside the model let the
    # subject promise a section the body did not contain.
    ids = {s["id"] for s in sections if s.get("records") or s.get("empty_line")}
    flow_head = None
    for sid in ("flow_driving", "flow_other"):
        sec = next((s for s in sections if s["id"] == sid), None)
        if sec and sec["records"]:
            flow_head = "%s flow" % sec["records"][0]["ticker"]
            break
    subject = BC.build_subject(dict(market, regime=regime), wl,
                               flow_headline=flow_head, sections=ids)
    preheader = BC.build_preheader(dict(market, regime=regime), wl,
                                   summary_lines or {}, sections=ids)

    model = {
        "schema": BS.SCHEMA,
        "meta": {"subject": subject,
                 "preheader": preheader,
                 "as_of": as_of or market.get("as_of_et") or "",
                 "session": market.get("session_label") or "",
                 "preview": bool(preview),
                 "regime": regime["label"],
                 "site": site, "unsub": unsub},
        "sections": sections,
        "validation": {},
        "artifact_hashes": {},
    }
    model["validation"] = BS.summarise(BS.validate_model(model)
                                       + [BS._r("model.internal_consistency",
                                                not BC.check_model(model),
                                                BC.check_model(model)[:3]
                                                or "clean")])
    return model


def section(model, sid):
    for s in model["sections"]:
        if s["id"] == sid:
            return s
    return None


def section_ids(model):
    return {s["id"] for s in model["sections"] if s.get("records")
            or s.get("empty_line")}


def parity_fingerprint(model):
    """What both renderings must agree on: which sections exist, in what
    order, and which records they contain, in what order."""
    return [(s["id"], [r["key"] for r in s["records"]])
            for s in model["sections"]]


def self_test():
    fails, ran = [], [0]

    def chk(n, c, d=""):
        ran[0] += 1
        print(("  PASS  " if c else "  FAIL  ") + n
              + ("" if c else "  <- %s" % (d,)))
        if not c:
            fails.append(n)

    site = "https://tickerdesk.io"
    market = {"indices": {
        "SPY": {"last": 748.28, "chg_1d_pct": 0.83, "chg_1w_pct": -0.47,
                "chg_ytd_pct": 9.53, "dist_ma20_pct": 0.4},
        "QQQ": {"last": 708.97, "chg_1d_pct": 1.85, "chg_1w_pct": -1.49,
                "chg_ytd_pct": 15.63, "dist_ma20_pct": -0.8},
        "IWM": {"last": 296.54, "chg_1d_pct": 1.45, "chg_1w_pct": 0.69,
                "chg_ytd_pct": 19.2, "dist_ma20_pct": -0.02},
        "DIA": {"last": 521.51, "chg_1d_pct": 0.69, "chg_1w_pct": -0.61,
                "chg_ytd_pct": 7.83, "dist_ma20_pct": -0.3}},
        "regime": {"label": "TRANSITION", "why": "breadth 55%"},
        "session_label": "Pre-Market Brief", "as_of_et": "2026-07-22 07:20 ET",
        "events": [{"title": "Crude Oil Inventories",
                    "time_et": "10:30 a.m. ET", "status": "UPCOMING",
                    "source_time": "2:30pm", "source_tz": "UTC"}]}

    def con(tk, right, strike, side, prem, sweep=False):
        return {"ticker": tk, "right": right, "strike": strike,
                "expiry": "2026-08-21", "side": side, "premium": prem,
                "premium_raw": float(prem.strip("$M")) * 1e6,
                "spot": 100.0, "printed_at": "2026-07-21T19:44:00Z",
                "is_sweep": sweep, "oi_state": BC.CONF_PENDING}

    wl = BC.rank_watchlist([
        {"ticker": "GEV", "grade_delta": 1, "grade_from": "B",
         "grade_to": "A-", "has_flow": True, "flow_direction": BC.BEARISH,
         "flow_short_dated": True, "earnings_in_days": 1,
         "earnings_confirmed": True, "price": 1078.81},
        {"ticker": "PM", "grade_delta": -1, "grade_from": "B+",
         "grade_to": "B", "has_flow": True, "flow_direction": BC.BEARISH,
         "price": 188.04},
        {"ticker": "QUIET1"}])
    watch_flow = {"GEV": [con("GEV", "put", 930, "put_buyer", "$0.2M")],
                  "PM": [con("PM", "put", 175, "put_buyer", "$0.4M")],
                  "TSLA": [con("TSLA", "call", 380, "mixed", "$42.8M", True),
                           con("TSLA", "put", 380, "put_seller", "$38.4M")]}
    mflow = [con("IBM", "call", 250, "call_buyer", "$4.9M"),
             con("IBM", "call", 260, "call_buyer", "$1.1M"),
             con("META", "put", 625, "put_buyer", "$3.0M", True),
             con("COIN", "call", 310, "call_buyer", "$3.3M")]

    m = build(market, wl, market_flow=mflow, watch_flow=watch_flow,
              news={"market": [], "watchlist": [], "empty": True,
                    "empty_line": "No high-relevance headlines since the "
                                  "previous brief."},
              discovery=[{"ticker": "CLF", "contract": "PUT 8",
                          "premium": "$2.0M", "why": "largest print"}],
              site=site, unsub="https://api.tickerdesk.io/unsubscribe?u=1")

    ix = section(m, "market")
    chk("index rows carry an explicit 20-day state",
        [r["ma_state"] for r in ix["records"]]
        == [BC.ABOVE, BC.BELOW, BC.AT, BC.BELOW],
        [(r["ticker"], r["vs20"], r["ma_state"]) for r in ix["records"]])
    chk("the summary uses the same tolerance as the table",
        ix["ma_summary"] == "1 above · 1 at · 2 below their 20-day averages",
        ix["ma_summary"])

    drv = section(m, "flow_driving")
    chk("every ranked-for-flow name appears in the driving section",
        {r["ticker"] for r in drv["records"]} >= {"GEV", "PM"},
        [r["ticker"] for r in drv["records"]])
    oth = section(m, "flow_other")
    chk("flow that did not move a status is filed separately",
        [r["ticker"] for r in oth["records"]] == ["TSLA"],
        [r["ticker"] for r in oth["records"]])
    chk("a name never appears in both flow subsections",
        not ({r["ticker"] for r in drv["records"]}
             & {r["ticker"] for r in oth["records"]}))

    mk = section(m, "flow_market")
    chk("market-wide flow is capped by ticker, not by row",
        len(mk["records"]) == 3, [r["key"] for r in mk["records"]])
    chk("the same market ticker never repeats",
        len({r["ticker"] for r in mk["records"]}) == 3)

    c = mk["records"][0]
    chk("contract states side and direction",
        c["action"] in (ACTION_BUY, ACTION_SELL, ACTION_TWO_SIDED)
        and c["direction"] in (DIR_BULL, DIR_BEAR, DIR_NONE), c)
    chk("contract carries its flow timestamp", c["flow_at"].endswith("ET"), c)
    chk("contract carries an OI state", bool(c["oi_state"]), c)
    two = [r for r in oth["records"][0]["contracts"]
           if r["action"] == ACTION_TWO_SIDED]
    chk("an unresolved print is labelled TWO-SIDED · unresolved",
        two and two[0]["direction"] == DIR_NONE, two)

    nw = section(m, "news")
    chk("news section exists even when empty", nw is not None)
    chk("the empty state is stated, not omitted",
        nw["empty_line"].startswith("No high-relevance"), nw)

    chk("parity fingerprint is stable and ordered",
        parity_fingerprint(m)[0][0] == "market"
        and parity_fingerprint(m)[1][0] == "watchlist")
    chk("discovery is ranked", section(m, "discovery")["records"][0]["rank"]
        == 1)

    print("\n%d/%d checks passed" % (ran[0] - len(fails), ran[0]))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(self_test())
