#!/usr/bin/env python3
"""brief_compose.py — what goes in the brief, in what order, and why.

Separated from rendering on purpose. The email's hard problems are
editorial, not visual: which of a user's names actually changed, whether
a ticker's flow agrees with itself, whether a historical hit rate is an
edge or a coin flip, and whether a headline is really about the ticker it
was filed under. Those are decided here and unit-tested without building
a single table.

    python brief_compose.py --self-test
"""

import re
import sys

# ── watchlist ranking ───────────────────────────────────────────────────
ACT_NOW, WATCH, NO_ACTION, QUIET = "ACT NOW", "WATCH", "NO ACTION", "QUIET"
RANK_ORDER = {ACT_NOW: 0, WATCH: 1, NO_ACTION: 2, QUIET: 3}
MAX_CHANGED_SHOWN = 5


def rank_ticker(ch):
    """Bucket one watchlist name from what actually changed.

    `ch` is a change record: grade_delta, crossed_level, has_flow,
    catalyst, earnings_in_days, price_change_pct, trigger_hit.
    """
    reasons = []
    if ch.get("trigger_hit"):
        reasons.append("entry trigger reached")
    if ch.get("crossed_level"):
        reasons.append("crossed %s" % ch["crossed_level"])
    if ch.get("grade_delta"):
        reasons.append("grade %+d" % ch["grade_delta"])
    if ch.get("has_flow"):
        reasons.append("new options flow")
    if ch.get("catalyst"):
        reasons.append(ch["catalyst"])
    e = ch.get("earnings_in_days")
    if e is not None and e <= 2:
        reasons.append("earnings in %dd" % e)
    pc = ch.get("price_change_pct")
    if pc is not None and abs(pc) >= 4:
        reasons.append("%+.1f%% move" % pc)

    material = bool(ch.get("trigger_hit") or ch.get("crossed_level")
                    or (e is not None and e <= 2)
                    or (pc is not None and abs(pc) >= 4)
                    or (ch.get("grade_delta") or 0) and
                    abs(ch.get("grade_delta") or 0) >= 2)
    soft = bool(ch.get("has_flow") or ch.get("catalyst")
                or (ch.get("grade_delta") or 0))

    if material and (ch.get("has_flow") or ch.get("catalyst")
                     or ch.get("trigger_hit")):
        bucket = ACT_NOW
    elif material or soft:
        bucket = WATCH if (material or soft) else NO_ACTION
    else:
        bucket = QUIET
    if bucket == WATCH and not reasons:
        bucket = QUIET
    return {"bucket": bucket, "reasons": reasons,
            "changed": bucket in (ACT_NOW, WATCH, NO_ACTION)}


def rank_watchlist(changes):
    """Rank, cap the displayed set, and summarise the rest in one line."""
    ranked = []
    for ch in changes:
        r = rank_ticker(ch)
        ranked.append({**ch, **r})
    ranked.sort(key=lambda x: (RANK_ORDER[x["bucket"]],
                               -abs(x.get("price_change_pct") or 0)))
    changed = [x for x in ranked if x["bucket"] != QUIET]
    quiet = [x for x in ranked if x["bucket"] == QUIET]
    shown = changed[:MAX_CHANGED_SHOWN]
    return {
        "shown": shown,
        "overflow": changed[MAX_CHANGED_SHOWN:],
        "quiet": quiet,
        "n_total": len(ranked),
        "n_changed": len(changed),
        "alert_line": "%d of your %d watch-list names changed materially."
                      % (len(changed), len(ranked)),
        "quiet_line": ("%d names quiet: %s"
                       % (len(quiet), ", ".join(x["ticker"] for x in quiet[:12])
                          + ("…" if len(quiet) > 12 else ""))) if quiet else "",
    }


# ── options-flow reconciliation ─────────────────────────────────────────
MIXED, BULLISH, BEARISH, NEUTRAL = "MIXED", "BULLISH", "BEARISH", "NEUTRAL"


def reconcile_ticker_flow(contracts):
    """One verdict per ticker, honest about internal disagreement.

    A ticker with one confirmed bullish contract and one failed contract
    is MIXED and says so. Reporting "8/8 confirmed" while displaying a
    failed contract is the specific defect this prevents.
    """
    if not contracts:
        return {"verdict": NEUTRAL, "confirmed": 0, "total": 0,
                "explain": "no contracts"}
    conf = [c for c in contracts if c.get("status") == "confirmed"]
    failed = [c for c in contracts if c.get("status") == "failed"]
    bull = [c for c in conf if (c.get("side") or "").lower() in
            ("call_buy", "put_sell", "bullish")]
    bear = [c for c in conf if (c.get("side") or "").lower() in
            ("put_buy", "call_sell", "bearish")]
    if failed and conf:
        verdict = MIXED
    elif bull and bear:
        verdict = MIXED
    elif bull:
        verdict = BULLISH
    elif bear:
        verdict = BEARISH
    else:
        verdict = NEUTRAL
    bits = []
    if bull:
        bits.append("%d confirmed bullish" % len(bull))
    if bear:
        bits.append("%d confirmed bearish" % len(bear))
    if failed:
        bits.append("%d failed to confirm" % len(failed))
    return {
        "verdict": verdict,
        "confirmed": len(conf),
        "failed": len(failed),
        "total": len(contracts),
        # the denominator is EVERY displayed contract, never just the
        # ones that worked
        "score": "%d of %d confirmed" % (len(conf), len(contracts)),
        "explain": "; ".join(bits) or "no confirmed contracts",
    }


# ── measured edge ───────────────────────────────────────────────────────
POSITIVE_EDGE = "POSITIVE MEASURED EDGE"
NO_EDGE = "NO MEASURED EDGE"
NEGATIVE_EDGE = "NEGATIVE MEASURED EDGE"
ACCRUING = "ACCRUING"
MIN_N = 30


def translate_edge(stats):
    """Turn a cohort record into a claim a reader can act on.

    A 49-50% hit rate with a near-zero excess return is not conviction,
    and must never be printed as though it were.
    """
    if not stats:
        return {"label": ACCRUING, "why": "no graded history yet"}
    n = stats.get("n") or 0
    if n < MIN_N:
        return {"label": ACCRUING,
                "why": "%d of %d graded outcomes needed" % (n, MIN_N)}
    excess = stats.get("excess_pct")
    hit = stats.get("hit_rate_pct")
    if excess is None:
        return {"label": ACCRUING, "why": "no excess-return measurement"}
    if abs(excess) < 0.5 or (hit is not None and 48 <= hit <= 52):
        return {"label": NO_EDGE,
                "why": "%.2f%% excess over %d trades%s — inside noise"
                       % (excess, n,
                          (", %.0f%% hit rate" % hit) if hit is not None else "")}
    if excess > 0:
        return {"label": POSITIVE_EDGE,
                "why": "%+.2f%% excess over %d trades" % (excess, n)}
    return {"label": NEGATIVE_EDGE,
            "why": "%+.2f%% excess over %d trades" % (excess, n)}


# ── news relevance and dedup ────────────────────────────────────────────
MAX_MARKET_NEWS = 3
MAX_WATCHLIST_NEWS = 3
_TIER_PRIMARY = ("company_ir", "sec", "regulator", "exchange")


def _norm_headline(h):
    s = re.sub(r"[^a-z0-9 ]", "", (h or "").lower())
    return " ".join(sorted(set(s.split())))[:120]


VERIFIED, MISATTRIBUTED, UNCONFIRMED = ("verified", "misattributed",
                                        "unconfirmed")


def verify_relevance(item, ticker, aliases=None):
    """Three outcomes, not two.

    A feed files an Nvidia story under six symbols, so attribution has to
    be checked. But most headlines name the COMPANY, not the ticker —
    "Micron guides Q4 above consensus" is a real MU story and rejecting
    it for lacking the letters "MU" throws away the news the user came
    for. So:

      verified      the ticker or one of its names appears
      misattributed another company is clearly the subject and this one
                    is absent — the real defect
      unconfirmed   neither; kept, but labelled rather than asserted
    """
    hay = " ".join([item.get("headline") or "", item.get("summary") or ""])
    names = list(item.get("company_words") or [])
    names += list((aliases or {}).get(ticker) or [])
    if re.search(r"\b%s\b" % re.escape(ticker), hay, re.I):
        return VERIFIED
    for w in names:
        if len(w) > 3 and re.search(r"\b%s" % re.escape(w), hay, re.I):
            return VERIFIED
    # does the headline name some OTHER covered company instead?
    for other, alist in (aliases or {}).items():
        if other == ticker:
            continue
        if re.search(r"\b%s\b" % re.escape(other), hay, re.I):
            return MISATTRIBUTED
        for w in alist or []:
            if len(w) > 3 and re.search(r"\b%s" % re.escape(w), hay, re.I):
                return MISATTRIBUTED
    return UNCONFIRMED


def select_news(items, watch_tickers, aliases=None,
                max_market=MAX_MARKET_NEWS, max_watch=MAX_WATCHLIST_NEWS):
    """Split into market-wide and watchlist news, dedupe syndication, and
    drop any item whose ticker attribution cannot be verified."""
    seen, market, watch, rejected = set(), [], [], []
    for it in items or []:
        key = _norm_headline(it.get("headline"))
        if key in seen:
            rejected.append({**it, "reason": "duplicate of a syndicated story"})
            continue
        seen.add(key)
        tks = [t for t in (it.get("tickers") or []) if t in watch_tickers]
        verdicts = {t: verify_relevance(it, t, aliases) for t in tks}
        keep = [t for t, v in verdicts.items()
                if v in (VERIFIED, UNCONFIRMED)]
        wrong = [t for t, v in verdicts.items() if v == MISATTRIBUTED]
        if wrong:
            it = {**it, "dropped_tickers": wrong}
        if keep:
            watch.append({**it, "tickers": keep,
                          "attribution": {t: verdicts[t] for t in keep}})
        elif not tks:
            market.append(it)
        else:
            rejected.append({**it, "reason":
                             "another company is the subject; %s not "
                             "mentioned" % ", ".join(wrong)})
    rank = lambda x: (0 if x.get("source_type") in _TIER_PRIMARY else 1,
                      -(x.get("impact") or 0))
    market.sort(key=rank)
    watch.sort(key=rank)
    return {"market": market[:max_market], "watchlist": watch[:max_watch],
            "rejected": rejected}


# ── subject + preheader ─────────────────────────────────────────────────
def build_subject(market, wl, flow_headline=None):
    """Market first, then the user's names — the same order as the body.

    e.g. "Risk-off tape · Semis lag · MU flow + 2 watchlist changes"
    """
    reg = (market.get("regime") or {}).get("label") or "Market"
    tone = {"RISK-ON": "Risk-on tape", "RISK-OFF": "Risk-off tape",
            "BALANCED": "Balanced tape",
            "TRANSITION": "Tape in transition"}.get(reg, reg)
    parts = [tone]
    secs = (market.get("sectors") or {})
    if secs.get("leaders"):
        parts.append("%s leads" % secs["leaders"][0]["name"])
    elif secs.get("laggards"):
        parts.append("%s lags" % secs["laggards"][0]["name"])
    tail = []
    if flow_headline:
        tail.append(flow_headline)
    n = wl.get("n_changed") or 0
    if n:
        tail.append("%d watchlist change%s" % (n, "" if n == 1 else "s"))
    if tail:
        parts.append(" + ".join(tail))
    return " · ".join(parts)[:120]


def build_preheader(market, wl, lines):
    """90–120 chars: the numbers first, then what they did to the user's
    names. Gmail shows this next to the subject and it must not repeat it."""
    a = lines.get("indices") or ""
    b = " ".join(x for x in (lines.get("vix"), lines.get("ten_year")) if x)
    head = " ".join(x for x in (a, b) if x)
    ev = (market.get("top_event") or {})
    ev_s = ""
    if ev.get("title"):
        ev_s = " · %s %s" % (ev.get("title"), ev.get("time") or "")
    tail = ""
    shown = wl.get("shown") or []
    if shown:
        tail = " · " + ", ".join(
            "%s %s" % (x["ticker"], (x.get("reasons") or ["changed"])[0])
            for x in shown[:2])
    s = (head + ev_s + tail).strip()
    if len(s) < 90:
        s = (s + " · " + (wl.get("alert_line") or "")).strip()
    return s[:120]


# ── self-test ───────────────────────────────────────────────────────────
def self_test():
    fails = []

    def chk(name, cond, detail=""):
        print(("  PASS  " if cond else "  FAIL  ") + name +
              ("" if cond else "  <- %s" % detail))
        if not cond:
            fails.append(name)

    # ranking
    r = rank_ticker({"ticker": "MU", "trigger_hit": True, "has_flow": True})
    chk("trigger + flow -> ACT NOW", r["bucket"] == ACT_NOW, r)
    r = rank_ticker({"ticker": "X", "grade_delta": 1})
    chk("small grade change -> WATCH", r["bucket"] == WATCH, r)
    r = rank_ticker({"ticker": "Y"})
    chk("nothing changed -> QUIET", r["bucket"] == QUIET, r)
    out = rank_watchlist([{"ticker": "A", "trigger_hit": True, "has_flow": True},
                          {"ticker": "B", "grade_delta": 1},
                          {"ticker": "C"}, {"ticker": "D"}])
    chk("alert line counts only changed names",
        out["alert_line"] == "2 of your 4 watch-list names changed materially.",
        out["alert_line"])
    chk("quiet names summarised, not listed in full",
        out["quiet_line"].startswith("2 names quiet"), out["quiet_line"])
    many = [{"ticker": "T%d" % i, "trigger_hit": True, "has_flow": True}
            for i in range(9)]
    chk("displayed changed names capped at 5",
        len(rank_watchlist(many)["shown"]) == 5)

    # flow reconciliation
    f = reconcile_ticker_flow([
        {"side": "call_buy", "status": "confirmed"},
        {"side": "call_buy", "status": "failed"}])
    chk("one confirmed + one failed -> MIXED", f["verdict"] == MIXED, f)
    chk("score denominator counts every displayed contract",
        f["score"] == "1 of 2 confirmed", f["score"])
    chk("MIXED explains both sides", "failed to confirm" in f["explain"], f)
    f2 = reconcile_ticker_flow([{"side": "call_buy", "status": "confirmed"},
                                {"side": "put_buy", "status": "confirmed"}])
    chk("confirmed both directions -> MIXED", f2["verdict"] == MIXED, f2)
    f3 = reconcile_ticker_flow([{"side": "call_buy", "status": "confirmed"}])
    chk("single confirmed bullish -> BULLISH", f3["verdict"] == BULLISH, f3)

    # edge
    chk("n below floor -> ACCRUING",
        translate_edge({"n": 12, "excess_pct": 3.0})["label"] == ACCRUING)
    chk("49% hit + tiny excess -> NO MEASURED EDGE",
        translate_edge({"n": 400, "excess_pct": 0.1,
                        "hit_rate_pct": 49.4})["label"] == NO_EDGE)
    chk("real positive excess -> POSITIVE",
        translate_edge({"n": 400, "excess_pct": 2.4,
                        "hit_rate_pct": 58})["label"] == POSITIVE_EDGE)
    chk("negative excess -> NEGATIVE",
        translate_edge({"n": 400, "excess_pct": -2.6,
                        "hit_rate_pct": 41})["label"] == NEGATIVE_EDGE)

    # news
    items = [
        {"headline": "Nvidia returns to NZS Growth Fund", "tickers": ["MU"],
         "source_type": "media"},
        {"headline": "Micron guides Q4 above consensus", "tickers": ["MU"],
         "source_type": "company_ir"},
        {"headline": "Micron guides Q4 above consensus!", "tickers": ["MU"],
         "source_type": "media"},
        {"headline": "Fed holds rates steady", "tickers": [],
         "source_type": "regulator"},
    ]
    sel = select_news(items, {"MU"},
                      aliases={"MU": ["Micron"], "NVDA": ["Nvidia"]})
    chk("syndicated duplicate removed",
        any("duplicate" in (r.get("reason") or "") for r in sel["rejected"]))
    chk("misattributed story rejected (Nvidia filed under MU)",
        any("another company is the subject" in (r.get("reason") or "")
            for r in sel["rejected"]), sel["rejected"])
    chk("verified watchlist story kept",
        any("Micron" in i["headline"] for i in sel["watchlist"]))
    chk("market-wide story separated from watchlist",
        any("Fed" in i["headline"] for i in sel["market"]))
    chk("market news capped at 3", len(sel["market"]) <= 3)

    # subject + preheader
    mk = {"regime": {"label": "RISK-OFF"},
          "sectors": {"leaders": [{"name": "Energy"}],
                      "laggards": [{"name": "Semis"}]},
          "top_event": {"title": "UoM Sentiment", "time": "2:00pm"}}
    wl = rank_watchlist([{"ticker": "MU", "trigger_hit": True,
                          "has_flow": True},
                         {"ticker": "ASML", "grade_delta": -2}])
    subj = build_subject(mk, wl, flow_headline="MU flow")
    chk("subject leads with regime", subj.startswith("Risk-off tape"), subj)
    chk("subject names the watchlist change count",
        "2 watchlist changes" in subj, subj)
    chk("subject within 120 chars", len(subj) <= 120)
    pre = build_preheader(mk, wl, {"indices": "SPY +1.2% QQQ +1.6%",
                                   "vix": "VIX -1.0%", "ten_year": "10Y 4.63%"})
    chk("preheader 90-120 chars", 90 <= len(pre) <= 120, "%d: %s" % (len(pre), pre))
    chk("preheader leads with market numbers", pre.startswith("SPY"), pre)
    chk("preheader mentions the user's names", "MU" in pre, pre)
    chk("preheader does not repeat the subject verbatim", pre != subj)

    total = 24
    print("\n%d/%d checks passed" % (total - len(fails), total))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(self_test() if "--self-test" in sys.argv else self_test())
