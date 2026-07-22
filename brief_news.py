#!/usr/bin/env python3
"""brief_news.py — headlines for the brief, with their provenance.

The brief previously reused the pre-market overnight pull, which reads
Supabase with a service key. Outside CI that key is absent, so the list
was always empty and the Top News section silently disappeared — the
reader could not tell "nothing happened" from "we did not look".

This adapter reads the public worker endpoint instead (no secret), and
returns records that state where each headline came from, when it was
published in Eastern, whether the outlet is a primary source, and one
sentence on why it matters. When there is genuinely nothing, it says so
rather than vanishing.

    python brief_news.py --self-test
    python brief_news.py AAPL MSFT      # live pull
"""

import json
import sys
import urllib.parse
import urllib.request

import brief_time as BT

WORKER = "https://api.tickerdesk.io"
PRIMARY, SECONDARY = "PRIMARY", "SECONDARY"

# Outlets that publish the company's or regulator's own words. Everything
# else is commentary about those words, which can be right, but is a
# second-hand account and is labelled as one.
_PRIMARY_HOSTS = (
    "globenewswire.com", "prnewswire.com", "businesswire.com",
    "accesswire.com", "newsfilecorp.com", "sec.gov", "federalreserve.gov",
    "bls.gov", "eia.gov", "treasury.gov", "sec.report", "ir.",
)
# Outlets whose output is overwhelmingly listicles and screeners. They are
# not evidence that anything happened today.
_LOW_VALUE = (
    "fool.com", "zacks.com", "investorplace.com", "24/7 wall st",
    "simply wall st", "insider monkey", "benzinga.com/general/",
)
_LOW_VALUE_TITLE = (
    "best stocks", "stocks to buy", "top stocks", "poised to outperform",
    "should you buy", "is it time to buy", "3 reasons", "5 stocks",
    "7 stocks", "10 stocks", "billionaire", "motley", "prediction:",
)


def _host(url):
    try:
        return (urllib.parse.urlparse(url).netloc or "").lower()
    except Exception:
        return ""


def source_tier(article):
    """PRIMARY when the outlet carries the issuer's own statement."""
    h = _host(article.get("url"))
    pub = (article.get("publisher") or "").lower()
    if any(p in h or p in pub for p in _PRIMARY_HOSTS):
        return PRIMARY
    return SECONDARY


def is_low_value(article):
    """Screener filler, judged on the HEADLINE.

    Judging by outlet threw away real market recaps because the same site
    also publishes listicles. What disqualifies an item is being a
    listicle, not who ran it — so the title decides, and the outlet only
    affects ranking.
    """
    return any(x in (article.get("title") or "").lower()
               for x in _LOW_VALUE_TITLE)


def is_filler_source(article):
    """Outlets whose median output is commentary rather than reporting.
    Ranked last, not excluded."""
    h = _host(article.get("url"))
    pub = (article.get("publisher") or "").lower()
    return any(x in h or x in pub for x in _LOW_VALUE)


def fetch(query, limit=25, timeout=20):
    """Raw articles from the public worker. [] on any failure — a news
    section that fails to load must not take the brief down with it."""
    url = "%s/?news=%s&limit=%d" % (WORKER, urllib.parse.quote(query), limit)
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "TickerDesk-Brief/1.0",
                          "Origin": "https://tickerdesk.io"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (json.loads(r.read().decode("utf-8")) or {}
                    ).get("articles") or []
    except Exception as e:
        print("  news fetch failed (non-fatal): %s: %s"
              % (type(e).__name__, e))
        return []


WHY_MAX = 140


def clip(s, limit=WHY_MAX):
    """Trim on a word boundary and say that you did.

    Slicing a string mid-token produced things like "insiders hav" — the
    reader cannot tell a truncation from a typo, and an ellipsis welded to
    a half-word is no better. Cut at the last space and append a spaced
    ellipsis so the elision is unmistakable.
    """
    s = " ".join((s or "").split())
    if len(s) <= limit:
        return s
    cut = s[:limit - 2]
    sp = cut.rfind(" ")
    if sp > limit * 0.5:
        cut = cut[:sp]
    return cut.rstrip(" ,;:-—") + " …"


def _why(article, tickers=()):
    """One sentence on why the headline matters, drawn from the article's
    own sentiment insights where the feed supplies them. Never invented:
    with no insight and no description we say what the record is, not what
    it means."""
    ins = article.get("insights") or []
    mine = [i for i in ins
            if not tickers or (i.get("ticker") or "").upper() in tickers]
    pick = (mine or ins or [None])[0]
    if pick and pick.get("sentiment_reasoning"):
        s = pick["sentiment_reasoning"].strip()
        s = s.split(". ")[0].rstrip(".")
        senti = (pick.get("sentiment") or "").lower()
        tag = ("%s on %s" % (senti, pick.get("ticker"))
               if senti and pick.get("ticker") else "")
        out = clip("%s%s" % (s, (" — %s" % tag) if tag else ""))
    else:
        d = (article.get("description") or "").strip()
        if not d:
            return "Headline recorded; the wire carried no summary."
        out = clip(d.split(". ")[0].rstrip("."))
    # every summary ends on a terminator, so a full sentence and an
    # abridged one are visibly different
    return out if out.endswith("…") else out.rstrip(".") + "."


def to_record(article, tickers=()):
    import brief_schema as BS
    dt = BT.parse_iso(article.get("published") or "")
    return {
        # content-addressed: a headline PREFIX collided whenever two
        # stories opened the same way, and changed identity whenever an
        # outlet edited its own headline
        "key": BS.record_key(article.get("url"), article.get("title")),
        "headline": (article.get("title") or "").strip(),
        "url": article.get("url") or "",
        "source": article.get("publisher") or _host(article.get("url"))
                  or "unknown",
        "published_et": BT.fmt_stamp(dt) if dt else "",
        "published_sort": dt.isoformat() if dt else "",
        "tier": source_tier(article),
        "tickers": [t.upper() for t in (article.get("tickers") or [])],
        "why": _why(article, tickers),
    }


def _dedupe(records):
    seen, out = set(), []
    for r in records:
        k = "".join(ch for ch in r["headline"].lower() if ch.isalnum())[:80]
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


# A "market-moving" headline is one about the market, not merely one that
# arrived on the wire. The general firehose is mostly corporate notices, so
# an item earns the market section by naming a macro driver or a broad
# instrument -- otherwise it is somebody's press release.
MACRO_TERMS = (
    "federal reserve", "fed ", "fomc", "interest rate", "rate cut",
    "rate hike", "inflation", "cpi", "ppi", "jobs report", "payroll",
    "unemployment", "gdp", "recession", "tariff", "treasury yield",
    "treasury yields", "bond yields", "s&p 500", "nasdaq composite",
    "nasdaq 100", "dow jones", "russell 2000", "stock market",
    "wall street", "crude oil", "oil prices", "opec", "dollar index",
    "earnings season", "jobless claims", "consumer confidence",
    "market rebound", "market selloff", "market rally", "risk-off",
    "risk-on", "volatility", "vix",
)
INDEX_TICKERS = ("SPY", "QQQ", "IWM", "DIA", "VOO", "IVV")
MAX_AGE_HOURS = 26          # one session plus the overnight tape


_MACRO_RX = None


def is_market_moving(record):
    """Matched on word boundaries: substring matching made "fed " miss
    "Fed holds" at the start of a line and let "oil" match "spoiled"."""
    global _MACRO_RX
    if _MACRO_RX is None:
        import re
        _MACRO_RX = re.compile(
            r"\b(?:%s)\b" % "|".join(re.escape(t.strip())
                                     for t in MACRO_TERMS), re.I)
    if _MACRO_RX.search(record.get("headline") or ""):
        return True
    return bool(set(record.get("tickers") or []) & set(INDEX_TICKERS))


def select(market_articles, watch_articles, watch_tickers,
           max_market=3, max_watch=3, as_of=None,
           max_age_hours=MAX_AGE_HOURS, priority=()):
    """Two populations, ranked, capped, and never pooled.

    A watch-list name appearing in the market feed must not be presented
    as market news, and vice versa: the reader treats the two sections
    differently and the separation is the whole point.
    """
    watch = {t.upper() for t in (watch_tickers or [])}
    cutoff = BT.parse_iso(as_of) if as_of else None
    oldest = None
    if cutoff is not None and max_age_hours:
        from datetime import timedelta
        oldest = BT.to_et(cutoff) - timedelta(hours=max_age_hours)

    def prep(arts, want_watch):
        out = []
        for a in arts or []:
            r = to_record(a, watch)
            if not r["headline"] or is_low_value(a):
                continue
            d = BT.parse_iso(r["published_sort"]) if r["published_sort"] \
                else None
            # nothing published after the brief's as-of time may appear:
            # the reader would be told about a story the brief could not
            # have read
            if cutoff and d and BT.to_et(d) > BT.to_et(cutoff):
                continue
            # ...and nothing from last week may be called news "since the
            # previous brief"
            if oldest and d and BT.to_et(d) < oldest:
                continue
            hits = watch & set(r["tickers"])
            if want_watch and not hits:
                continue
            if not want_watch:
                if hits:
                    continue
                if not is_market_moving(r):
                    continue
            r["watch_tickers"] = sorted(hits)
            r["filler"] = is_filler_source(a)
            out.append(r)
        # primary sources first, then most recent
        out.sort(key=lambda r: (r.get("filler", False),
                                r["tier"] != PRIMARY,
                                -_ts(r["published_sort"])))
        return _dedupe(out)

    w_all = prep(watch_articles, True)
    # a story about the name that moved most is worth more than a story
    # about the name that merely appears on the list
    prio = {t.upper(): i for i, t in enumerate(priority or [])}
    if prio:
        w_all.sort(key=lambda r: (
            min((prio.get(t, 999) for t in r.get("watch_tickers") or []),
                default=999),
            r["tier"] != PRIMARY, -_ts(r["published_sort"])))
    # one shared quota across both sections: the validator counts the
    # union, so capping each section independently let a single outlet
    # supply four of five displayed stories
    quota = {}
    w = _cap_publisher(w_all, max_watch, used=quota)
    # the reader's own names win the story: showing it again under Market
    # would present one event as two
    claimed = {r["key"] for r in w} | {
        "".join(ch for ch in r["headline"].lower() if ch.isalnum())[:80]
        for r in w}
    m_all = [r for r in prep(market_articles, False)
             if r["key"] not in claimed
             and "".join(ch for ch in r["headline"].lower()
                         if ch.isalnum())[:80] not in claimed]
    m = _cap_publisher(m_all, max_market, used=quota)

    shown = m + w
    notes = []
    if shown:
        pubs = {}
        for r in shown:
            pubs[r["source"]] = pubs.get(r["source"], 0) + 1
        if len(pubs) == 1:
            notes.append("All headlines below come from %s; no second "
                         "outlet covered these stories in the window."
                         % list(pubs)[0])
        if not any(r["tier"] == PRIMARY for r in shown):
            notes.append("No primary-source (issuer or regulator) coverage "
                         "was available in the window; every item below is "
                         "second-hand reporting.")
    return {
        "market": m,
        "watchlist": w,
        "empty": not shown,
        "coverage_note": " ".join(notes),
        "empty_line": "No high-relevance headlines since the previous brief.",
    }


def _cap_publisher(records, cap, share=0.5, used=None):
    """No outlet may supply more than `share` of the DISPLAYED news while
    another outlet has something to say.

    The first version deferred over-quota items and then backfilled them
    when the section came up short, which defeated the cap entirely: four
    of five displayed stories came from one outlet and the schema check
    caught it in production. Shipping fewer, more varied stories is the
    point — a short section is a smaller problem than one outlet's
    editorial line presented as the day's news.

    `used` is shared across sections so the quota applies to the union,
    which is what the validator measures.
    """
    if not records:
        return []
    counts = used if used is not None else {}
    sources = {r["source"] for r in records}
    limit = max(1, int(cap * share))
    out = []
    for r in records:
        if len(out) >= cap:
            break
        # a single available outlet cannot be diversified; the coverage
        # note discloses that rather than the section going empty
        if len(sources) > 1 and counts.get(r["source"], 0) >= limit:
            continue
        counts[r["source"]] = counts.get(r["source"], 0) + 1
        out.append(r)
    return out


def _ts(iso):
    d = BT.parse_iso(iso)
    try:
        return d.timestamp() if d else 0.0
    except Exception:
        return 0.0


def load(watch_tickers, as_of=None, max_market=3, max_watch=3, live=True,
         priority=()):
    """The adapter the brief calls. `live=False` returns the empty state
    without touching the network, for tests and offline runs."""
    if not live:
        return select([], [], watch_tickers, max_market, max_watch, as_of,
                      priority=priority)
    general = fetch("general", limit=60)
    for ix in ("SPY", "QQQ"):
        general.extend(fetch(ix, limit=10))
    per = []
    for tk in sorted({t.upper() for t in (watch_tickers or [])})[:12]:
        per.extend(fetch(tk, limit=6))
    return select(general, per, watch_tickers, max_market, max_watch, as_of,
                  priority=priority)


def self_test():
    fails, ran = [], [0]

    def chk(n, c, d=""):
        ran[0] += 1
        print(("  PASS  " if c else "  FAIL  ") + n
              + ("" if c else "  <- %s" % (d,)))
        if not c:
            fails.append(n)

    chk("wire service is PRIMARY",
        source_tier({"url": "https://www.globenewswire.com/x",
                     "publisher": "GlobeNewswire Inc."}) == PRIMARY)
    chk("regulator is PRIMARY",
        source_tier({"url": "https://www.sec.gov/x"}) == PRIMARY)
    chk("commentary is SECONDARY",
        source_tier({"url": "https://www.reuters.com/x",
                     "publisher": "Reuters"}) == SECONDARY)
    chk("screener filler is rejected on its headline",
        is_low_value({"url": "https://www.fool.com/investing/x",
                      "title": "5 Stocks Poised to Outperform"}))
    chk("a real recap from a filler outlet is kept, only demoted",
        not is_low_value({"url": "https://www.fool.com/x",
                          "title": "Stock Market Today: Micron surges 12%"})
        and is_filler_source({"url": "https://www.fool.com/x"}))
    chk("a bell-ringing ceremony is not market-moving",
        not is_market_moving({"headline":
                              "Coalition rings the Nasdaq opening bell",
                              "tickers": []}))
    chk("a real headline is kept",
        not is_low_value({"url": "https://reuters.com/x",
                          "title": "GE Vernova raises full-year guidance"}))

    mkt = [{"title": "Treasury yields ease after auction",
            "url": "https://treasury.gov/z", "publisher": "US Treasury",
            "published": "2026-07-22T12:30:00Z",
            "insights": [{"ticker": "SPY", "sentiment": "neutral",
                          "sentiment_reasoning": "Policy unchanged. Second."}]},
           {"title": "Fed holds rates steady", "url": "https://sec.gov/a",
            "publisher": "Federal Reserve",
            "published": "2026-07-22T12:00:00Z"},
           {"title": "5 Stocks Poised to Outperform",
            "url": "https://www.fool.com/x", "publisher": "The Motley Fool",
            "published": "2026-07-22T11:00:00Z"},
           {"title": "Oil slips on inventory build",
            "url": "https://reuters.com/b", "publisher": "Reuters",
            "published": "2026-07-22T10:00:00Z"}]
    wl = [{"title": "GE Vernova wins turbine order",
           "url": "https://globenewswire.com/c", "publisher": "GlobeNewswire",
           "published": "2026-07-22T09:00:00Z", "tickers": ["GEV"]},
          {"title": "Fed holds rates steady", "url": "https://sec.gov/a",
           "publisher": "Federal Reserve",
           "published": "2026-07-22T12:00:00Z", "tickers": ["GEV"]}]

    sel = select(mkt, wl, ["GEV"], as_of="2026-07-22T13:00:00Z")
    chk("screener filler never reaches the brief",
        all("Poised" not in r["headline"] for r in sel["market"]), sel)
    chk("market and watch-list populations stay separate",
        all("GEV" not in r.get("watch_tickers", []) for r in sel["market"]))
    chk("watch-list headline is attributed to its name",
        sel["watchlist"] and sel["watchlist"][0]["watch_tickers"] == ["GEV"])
    chk("primary sources rank first",
        sel["market"][0]["tier"] == PRIMARY, sel["market"][0])
    chk("publication time is rendered in Eastern",
        sel["market"][0]["published_et"].endswith("ET"),
        sel["market"][0]["published_et"])
    chk("12:30 UTC publishes as 08:30 ET",
        "08:30" in sel["market"][0]["published_et"],
        sel["market"][0]["published_et"])
    chk("why-it-matters comes from the article, not invention",
        sel["market"][0]["why"].startswith("Policy unchanged"),
        sel["market"][0]["why"])
    chk("a corporate press release never enters the market section",
        all("Kalmar" not in r["headline"] for r in select(
            [{"title": "Kalmar extends service agreement",
              "url": "https://globenewswire.com/k",
              "publisher": "GlobeNewswire",
              "published": "2026-07-22T12:00:00Z"}], [], [],
            as_of="2026-07-22T13:00:00Z")["market"]))
    # the same story carried by both feeds belongs to exactly one section:
    # a headline tagged with a user's ticker is watch-list news, and
    # showing it again under Market would double-count it
    in_mkt = ["Fed holds" in r["headline"] for r in sel["market"]]
    in_wl = ["Fed holds" in r["headline"] for r in sel["watchlist"]]
    chk("a story tagged to a watch-list name appears only there",
        any(in_wl) and not any(in_mkt), (sel["market"], sel["watchlist"]))
    chk("identical headlines are deduplicated within a section",
        len({r["headline"] for r in sel["watchlist"]})
        == len(sel["watchlist"]))
    chk("market cap respected", len(sel["market"]) <= 3)
    # the production failure: one outlet supplied 4 of 5 displayed stories
    # because over-quota items were deferred and then backfilled
    flood = [{"title": "Fed signals patience on rates %d" % i,
              "url": "https://www.fool.com/a%d" % i,
              "publisher": "The Motley Fool",
              "published": "2026-07-22T11:0%d:00Z" % i} for i in range(6)]
    flood.append({"title": "Treasury yields ease after auction",
                  "url": "https://reuters.com/t", "publisher": "Reuters",
                  "published": "2026-07-22T11:30:00Z"})
    fl = select(flood, [], [], as_of="2026-07-22T12:00:00Z")
    shown = fl["market"] + fl["watchlist"]
    pubs = {}
    for r in shown:
        pubs[r["source"]] = pubs.get(r["source"], 0) + 1
    top = max(pubs.values())
    chk("no outlet exceeds half of the displayed stories",
        len(pubs) == 1 or top * 2 <= len(shown), pubs)
    chk("the cap ships fewer stories rather than violating itself",
        len(shown) < 3 or len(pubs) > 1, (len(shown), pubs))
    solo = select(flood[:3], [], [], as_of="2026-07-22T12:00:00Z")
    chk("a single available outlet still fills the section",
        len(solo["market"]) == 3, len(solo["market"]))
    chk("...and that lack of diversity is disclosed",
        "no second outlet" in (solo.get("coverage_note") or ""),
        solo.get("coverage_note"))
    chk("a corporate notice is not called market-moving",
        not is_market_moving({"headline": "Kalmar extends service agreement",
                              "tickers": ["KALM"]}))
    chk("a macro headline is market-moving",
        is_market_moving({"headline": "Fed holds rates steady",
                          "tickers": []}))
    chk("an index-tagged headline is market-moving",
        is_market_moving({"headline": "Stocks drift", "tickers": ["SPY"]}))
    stale = select([{"title": "Fed holds rates steady",
                     "url": "https://sec.gov/a", "publisher": "Fed",
                     "published": "2026-07-16T12:00:00Z"}], [], [],
                   as_of="2026-07-22T13:00:00Z")
    chk("a week-old story is not news since the previous brief",
        stale["empty"], stale["market"])

    late = select(mkt, wl, ["GEV"], as_of="2026-07-22T09:30:00Z")
    chk("nothing published after the as-of time is shown",
        all("Fed holds" not in r["headline"] for r in late["market"]),
        late["market"])

    none = select([], [], ["GEV"])
    chk("an empty result is stated, not omitted",
        none["empty"] and none["empty_line"].startswith("No high-relevance"))
    chk("offline load returns the empty state without network",
        load(["GEV"], live=False)["empty"])

    print("\n%d/%d checks passed" % (ran[0] - len(fails), ran[0]))
    return 1 if fails else 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--self-test" in sys.argv or not args:
        raise SystemExit(self_test())
    sel = load(args)
    print(json.dumps(sel, indent=1)[:4000])
