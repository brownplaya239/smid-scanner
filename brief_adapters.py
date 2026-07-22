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
    """One displayable contract. `status` is confirmed only when open
    interest actually rose — the scanner's own OI delta, not a guess."""
    oid = r.get("oi_delta")
    confirmed = None
    if oid is not None:
        confirmed = oid > 0
    prem = r.get("premium")
    return {
        "ticker": r.get("ticker"),
        "right": "call" if (r.get("type") or "").lower().startswith("c")
                 else "put" if (r.get("type") or "").lower().startswith("p")
                 else (r.get("type") or ""),
        "strike": r.get("strike"),
        "expiry": r.get("expiry"),
        "side": r.get("flow_side") or r.get("direction"),
        "premium": ("$%.1fM" % (prem / 1e6)) if isinstance(
            prem, (int, float)) and prem else None,
        "spot": r.get("spot"),
        "oi_confirmed": confirmed,
        "status": ("confirmed" if confirmed else
                   "failed" if confirmed is False else "unknown"),
        "session_date": (r.get("last_print_ts") or "")[:10] or None,
        "break_even": r.get("break_even"),
    }


def build_changes(tickers, swing, uoa_by_ticker, facts, earnings,
                  edge_stats=None):
    """One change record per watchlist name."""
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
        edge = None
        if edge_stats:
            edge = BC.translate_edge(edge_stats.get(tk))["label"]
        rec = {
            "ticker": tk,
            "price": sw.get("price") or f.get("close"),
            "price_change_pct": sw.get("chg"),
            "grade_delta": gd,
            "has_flow": bool(rows),
            "catalyst": cat,
            "earnings_in_days": e.get("days") if e else (
                rows[0].get("earnings_days") if rows else None),
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


def split_flow(market_top, uoa_by_ticker, watch_tickers, max_market=3,
               max_per_ticker=2):
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
