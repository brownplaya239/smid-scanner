#!/usr/bin/env python3
"""brief_adapters.py — repo data -> the shapes brief_compose expects.

Deliberately thin and deliberately conservative. Where the repo does not
hold the state a field needs, the field is left ABSENT rather than
approximated: `trigger_hit` and `crossed_level` both require yesterday's
level relative to today's, and nothing on disk records a per-user
trigger, so they stay unset. A brief that invents "entry trigger
reached" is worse than one that says a name is quiet.

    python brief_adapters.py --self-test
"""

import re
import sys

import brief_compose as BC

GRADE_ORDER = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-",
               "D+", "D", "D-", "F"]
GRADE_RANK = {g: i for i, g in enumerate(GRADE_ORDER)}


def _grade_delta(cur, prev):
    """Positive = improved. None when either side is unknown, so an
    unrated name never reads as a downgrade."""
    if not cur or not prev:
        return None
    a, b = GRADE_RANK.get(cur), GRADE_RANK.get(prev)
    if a is None or b is None:
        return None
    return b - a


def technical_line(f):
    """One clause from the fact table, in evidence-bounded language."""
    if not f:
        return ""
    bits = []
    t = f.get("trend")
    if t:
        bits.append(t.replace("-", " "))
    for span in ("ema20", "ema50", "ema200"):
        side = f.get(span)
        dist = f.get(span + "_dist")
        if side and dist is not None:
            bits.append("%s the %s-day by %.1f%%"
                        % (side, span.replace("ema", ""), abs(dist)))
            break
    rsi = f.get("rsi14")
    if rsi is not None:
        bits.append("RSI %.0f" % rsi)
    return "; ".join(bits)


def flow_line(rows):
    """Summarise a ticker's flow without asserting institutional intent."""
    if not rows:
        return ""
    r = rows[0]
    side = (r.get("flow_side") or r.get("direction") or "").replace("_", " ")
    prem = r.get("premium")
    p = ("$%.1fM" % (prem / 1e6)) if isinstance(prem, (int, float)) and prem \
        else ""
    bits = [b for b in (side, p) if b]
    if r.get("is_sweep"):
        bits.append("sweep")
    if r.get("golden") or r.get("is_golden"):
        bits.append("golden")
    return ", ".join(bits)


def _contract(r):
    """One displayable contract, carrying the evidence its confirmation
    state is derived from rather than a bare boolean."""
    oid = r.get("oi_delta")
    confirmed = None
    if oid is not None:
        confirmed = oid > 0
    prem = r.get("premium")
    c = {
        "ticker": r.get("ticker"),
        "right": "call" if (r.get("type") or "").lower().startswith("c")
                 else "put" if (r.get("type") or "").lower().startswith("p")
                 else (r.get("type") or ""),
        "strike": r.get("strike"),
        "expiry": r.get("expiry"),
        "side": r.get("flow_side") or r.get("direction"),
        "premium": ("$%.1fM" % (prem / 1e6)) if isinstance(
            prem, (int, float)) and prem else None,
        "premium_raw": prem if isinstance(prem, (int, float)) else None,
        "spot": r.get("spot"),
        "oi_delta": oid,
        "oi_confirmed": confirmed,
        "oi_as_of": r.get("oi_as_of"),
        "printed_at": r.get("last_print_ts"),
        "status": ("confirmed" if confirmed else
                   "failed" if confirmed is False else "unknown"),
        "session_date": (r.get("last_print_ts") or "")[:10] or None,
        "break_even": r.get("break_even"),
        "is_sweep": bool(r.get("is_sweep")),
        "golden": bool(r.get("golden") or r.get("is_golden")),
        "tier": r.get("tier"),
    }
    # the state machine decides PENDING vs UNCONFIRMED from the clock, so
    # the renderer never has to infer it from a missing field
    c["oi_state"] = BC.oi_state(c)["state"]
    return c


def _short_dated(rows, days=30):
    """True when the nearest expiry sits inside the window — positioning
    that expires next week is a different claim from positioning that
    expires next year, and the synthesis needs to know which."""
    import brief_time as BT
    from datetime import datetime
    now = datetime.now(BT.ET)
    for r in rows or []:
        d = BT.parse_date(r.get("expiry") or "")
        if not d:
            continue
        try:
            exp = datetime(d[0], d[1], d[2], tzinfo=BT.ET)
        except ValueError:
            continue
        if 0 <= (exp - now).days <= days:
            return True
    return False


def build_discovery(market_top, watch_tickers, limit=3, exclude=()):
    """Names off the watch list, with the evidence that surfaced them.

    `exclude` drops tickers the market-wide flow section already showed.
    Printing IBM identically in both sections spends the reader's
    attention twice for one idea; a name earns a second appearance only by
    carrying information the first one did not.
    """
    import brief_model as BM
    watch = set(watch_tickers or [])
    skip = {t for t in (exclude or []) if t}
    out, seen = [], set()
    for r in market_top or []:
        tk = r.get("ticker")
        if not tk or tk in watch or tk in seen or tk in skip:
            continue
        seen.add(tk)
        c = _contract(r)
        rec = BM.contract_record(c)
        why = []
        if c["is_sweep"]:
            why.append("swept across exchanges")
        if c["golden"]:
            why.append("golden sweep")
        if r.get("tier"):
            why.append("tier %s" % r["tier"])
        if r.get("sector"):
            why.append(r["sector"])
        if r.get("earnings_days") is not None:
            why.append("earnings in %sd" % r["earnings_days"])
        out.append({
            "ticker": tk,
            "contract": "%s %s %s" % (rec["right"], c["strike"], c["expiry"]),
            "side_label": "%s %s · %s" % (rec["right"], rec["action"],
                                          rec["direction"]),
            "premium": c["premium"],
            "oi_state": c["oi_state"],
            "why": ", ".join(why) or "largest unusual print outside your list",
        })
        if len(out) >= limit:
            break
    return out


def build_weekly(market):
    """The five-session lens, from the breadth series the regime file
    already keeps. Returns None when the history is too short to say
    anything — a 'weekly' line built from two days is not a weekly line.
    """
    b = (market or {}).get("breadth") or {}
    cur, wow = b.get("breadth_pct"), b.get("breadth_wow")
    if cur is None or wow is None:
        return None
    direction = ("widened" if wow >= 2 else "narrowed" if wow <= -2
                 else "held roughly flat")
    line = ("Participation %s over the week: %d%% of the %s-name universe is "
            "above its 20-day average, against %d%% five sessions ago."
            % (direction, round(cur), "{:,}".format(b.get("universe") or 0),
               round(cur - wow)))
    prior, now = b.get("label_prior"), b.get("label_current")
    if prior and now and prior != now:
        line += " The regime label moved from %s to %s." % (prior, now)
    return {"line": line, "sub": "Five-session view", "changed": True,
            "breadth_pct": cur, "breadth_wow": wow}


def build_changes(tickers, swing, uoa_by_ticker, facts, earnings,
                  edge_stats=None):
    """One change record per watchlist name."""
    import brief_time as BT
    from datetime import datetime, timedelta
    now = datetime.now(BT.ET)
    session_as_of = BT.fmt_stamp(now)
    # a pre-market quote is good until the opening auction resolves it; a
    # prior close is good until the next one prints
    stale_premarket = BT.fmt_stamp(now.replace(hour=9, minute=30, second=0,
                                               microsecond=0))
    stale_close = BT.fmt_stamp((now + timedelta(days=1)).replace(
        hour=16, minute=0, second=0, microsecond=0))
    facts_as_of = (facts or {}).get("_as_of") or ""
    out = []
    for tk in tickers:
        sw = (swing or {}).get(tk) or {}
        f = (facts or {}).get(tk) or {}
        rows = (uoa_by_ticker or {}).get(tk) or []
        e = (earnings or {}).get(tk) or {}
        gd = _grade_delta(sw.get("grade"), sw.get("prev_grade"))
        cat = None
        if rows and rows[0].get("catalyst"):
            cat = rows[0]["catalyst"]
        # the scanner sometimes carries the earnings horizon only as prose
        # in the catalyst ("Earnings in 1d"). Without parsing it back out,
        # synthesize() cannot fold the event into its sentence and the row
        # prints the catalyst a second time alongside it.
        days = (e.get("days") if e else None)
        if days is None and rows:
            days = rows[0].get("earnings_days")
        if days is None and cat:
            m = re.search(r"earnings in (\d+)\s*d", cat, re.I)
            if m:
                days = int(m.group(1))
        edge = None
        if edge_stats:
            edge = BC.translate_edge(edge_stats.get(tk))["label"]
        # the synthesis needs the flow's DIRECTION, not merely that flow
        # exists: "grade up + bearish flow" is the case that must not be
        # reported as "strengthening"
        cons = [_contract(r) for r in rows]
        flow = BC.classify_flow(cons) if cons else None
        # High-quality flow is what makes a name MATERIAL on flow alone:
        # institutional size, top tier, or a completed OI confirmation.
        # Without this bar every stray print counted, which is how 13 of
        # 16 names came to be "materially changed".
        flow_hq = any(
            (c.get("premium_raw") or 0) >= BC.FLOW_HQ_PREMIUM
            or str(c.get("tier") or "") in BC.FLOW_HQ_TIERS
            or c.get("oi_state") == BC.CONF_YES for c in cons)
        flow_confirmed = any(c.get("oi_state") == BC.CONF_YES for c in cons)
        # Which price this is, when it was true, where it came from, and
        # when it stops being usable. A bare number is the one thing a
        # brief must not print, because it looks current whatever its age.
        if sw.get("price") is not None:
            price = sw["price"]
            basis, src = BC.BASIS_PREMARKET, "swing scan"
            as_of = sw.get("as_of") or sw.get("session_date") or session_as_of
            stale = stale_premarket
        else:
            price = f.get("close")
            basis, src = BC.BASIS_CLOSE, "technical_facts nightly"
            as_of = f.get("date") or f.get("as_of") or facts_as_of
            stale = stale_close
        rec = {
            "ticker": tk,
            "price": price,
            "price_record": BC.price_record(
                price, basis, as_of, reason="no quote in this run",
                source=src, stale_after=stale),
            "price_change_pct": sw.get("chg"),
            "flow_hq": flow_hq,
            "flow_confirmed": flow_confirmed,
            # the earnings date is only "confirmed" when it came from the
            # earnings calendar, not from a scanner's prose catalyst
            "earnings_confirmed": bool(e.get("days") is not None
                                       or (rows and rows[0].get(
                                           "earnings_days") is not None)),
            "tech_transition": bool(f.get("ma_state_changed")),
            "tech_deterioration": f.get("ma_state_changed") == "down",
            "tech_improvement": f.get("ma_state_changed") == "up",
            "grade_delta": gd,
            "grade_from": sw.get("prev_grade"),
            "grade_to": sw.get("grade"),
            "has_flow": bool(rows),
            "flow_direction": (flow or {}).get("direction"),
            "flow_short_dated": _short_dated(rows),
            "catalyst": cat,
            "earnings_in_days": days,
            "technical": technical_line(f),
            "flow_line": flow_line(rows),
            "edge": edge,
            "signal_strength": (rows[0].get("tier") if rows else None),
            "evidence": ("moderate" if rows else "limited"),
            # NOT inferred: nothing on disk records yesterday's level or a
            # per-user trigger, so these stay absent rather than guessed
            "crossed_level": None,
            "trigger_hit": None,
        }
        if sw.get("change") == "new":
            rec["catalyst"] = rec["catalyst"] or "new to the scan"
        out.append(rec)
    return out


def split_flow(market_top, uoa_by_ticker, watch_tickers, max_market=6,
               max_per_ticker=3):
    """Market-wide flow and watchlist flow are different populations and
    must not be pooled — a user's name appearing in the market-wide list
    would read as a personalised alert."""
    watch = set(watch_tickers or [])
    mkt = [_contract(r) for r in (market_top or [])
           if r.get("ticker") not in watch][:max_market]
    per = {}
    for tk in watch:
        rows = (uoa_by_ticker or {}).get(tk) or []
        if rows:
            per[tk] = [_contract(r) for r in rows[:max_per_ticker]]
    return mkt, per


def self_test():
    fails = []

    def chk(n, c, d=""):
        print(("  PASS  " if c else "  FAIL  ") + n + ("" if c else "  <- %s" % d))
        if not c:
            fails.append(n)

    chk("upgrade is a positive delta", _grade_delta("A", "B") > 0)
    chk("downgrade is negative", _grade_delta("C", "B") < 0)
    chk("unknown grade yields None, not a downgrade",
        _grade_delta("A", None) is None)

    swing = {"MU": {"grade": "A", "prev_grade": "B", "price": 118.2,
                    "chg": 5.1, "change": "upgrade"},
             "AAPL": {"grade": "B", "prev_grade": "B", "price": 210.0,
                      "chg": 0.2, "change": "same"}}
    facts = {"MU": {"close": 118.2, "trend": "strong-up", "ema20": "above",
                    "ema20_dist": 3.9, "rsi14": 61.0}}
    uoa = {"MU": [{"ticker": "MU", "type": "call", "strike": 120,
                   "expiry": "2026-08-15", "flow_side": "call_buyer",
                   "premium": 2.4e6, "spot": 118.2, "oi_delta": 900,
                   "tier": "A+", "catalyst": "Earnings in 2d",
                   "last_print_ts": "2026-07-21T15:44:00Z",
                   "is_sweep": True}]}
    ch = build_changes(["MU", "AAPL"], swing, uoa, facts, {})
    mu = [c for c in ch if c["ticker"] == "MU"][0]
    chk("flow detected on MU", mu["has_flow"])
    chk("technical line is evidence-bounded",
        "above the 20-day" in mu["technical"], mu["technical"])
    chk("trigger_hit not invented", mu["trigger_hit"] is None)
    chk("crossed_level not invented", mu["crossed_level"] is None)
    ranked = BC.rank_watchlist(ch)
    chk("MU ranks above quiet AAPL",
        ranked["shown"] and ranked["shown"][0]["ticker"] == "MU",
        [x["ticker"] for x in ranked["shown"]])

    mkt, per = split_flow([{"ticker": "NVDA", "type": "call", "strike": 200,
                            "expiry": "2026-08-15", "premium": 9e6,
                            "oi_delta": 100},
                           {"ticker": "MU", "type": "call", "strike": 120,
                            "expiry": "2026-08-15", "premium": 5e6,
                            "oi_delta": 10}],
                          uoa, ["MU"])
    chk("watchlist name excluded from market-wide flow",
        all(c["ticker"] != "MU" for c in mkt), mkt)
    chk("watchlist flow keyed per ticker", "MU" in per)
    chk("per-ticker contracts capped at 2", len(per["MU"]) <= 2)
    c = per["MU"][0]
    chk("OI rise marks the contract confirmed", c["status"] == "confirmed", c)
    chk("contract carries a session date", bool(c["session_date"]), c)
    neg = _contract({"ticker": "X", "type": "put", "oi_delta": -50})
    chk("OI fall marks the contract failed", neg["status"] == "failed", neg)
    unk = _contract({"ticker": "X", "type": "put"})
    chk("absent OI is 'unknown', never 'confirmed'",
        unk["status"] == "unknown", unk)

    total = 15
    print("\n%d/%d checks passed" % (total - len(fails), total))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(self_test() if "--self-test" in sys.argv else self_test())
