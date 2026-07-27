#!/usr/bin/env python3
"""feed_check.py — prove every data feed the v4 report depends on is
reachable, authenticated, and returning what the report claims it does.

    python feed_check.py [TICKER]

Prints one line per feed: whether it is connected, what tier/coverage it
resolved to, and a sample value so a wrong-but-reachable feed cannot pass.
It NEVER prints an API key, and it never fails the process on a missing
optional feed — the point is a truthful register, not a gate.

Feeds are graded by provenance, because that is what "golden source"
means here:

  PRIMARY   the issuer or the regulator said it (SEC XBRL, EDGAR filings,
            the 8-K earnings exhibit). Nothing sits between us and the
            filing; these are the numbers a reader can audit.
  VENDOR    a data provider's aggregate, carrying its own as-of date
            (Finnhub consensus, peers). Admissible when dated and labelled
            as a vendor observation.
  UNOFFICIAL a scraped or undocumented endpoint with no contract, no SLA
            and no redistribution licence (yfinance). Works until it does
            not, and can change shape without notice.
"""

import os
import sys

PRIMARY, VENDOR, UNOFFICIAL = "PRIMARY", "VENDOR", "UNOFFICIAL"

_ROWS = []


def row(feed, grade, ok, detail):
    _ROWS.append((feed, grade, ok, detail))
    print("  %-26s %-10s %-14s %s"
          % (feed, grade, "connected" if ok else "NOT CONNECTED", detail))


def check_sec_xbrl(ticker):
    """SEC XBRL company concepts — the filed financials."""
    try:
        import research_live as RL
        cik = RL.cik_for(ticker)
        rows = (RL.concept(
            cik, "RevenueFromContractWithCustomerExcludingAssessedTax")
            or RL.concept(cik, "Revenues"))
        n = len(rows or [])
        row("SEC XBRL (financials)", PRIMARY, n > 0,
            "CIK %s, %d revenue facts" % (cik, n))
        return cik
    except Exception as e:
        row("SEC XBRL (financials)", PRIMARY, False, "error: %s" % e)
        return None


def check_edgar_exhibit(ticker, cik):
    """The 8-K earnings exhibit — guidance and the operating KPIs."""
    try:
        import research_live as RL
        import sec_exhibit as SX
        acc, _subs = RL.acceptance_map(cik)   # returns (map, submissions)
        ex = SX.ingest(cik, acc, RL.sec_text)
        k = len(ex.get("kpis") or {})
        g = len(ex.get("guidance_highlights") or {})
        row("EDGAR 8-K exhibit", PRIMARY,
            ex.get("disposition") == "ADMITTED",
            "%s, %d KPIs, %d guidance items" % (ex.get("disposition"), k, g))
    except Exception as e:
        row("EDGAR 8-K exhibit", PRIMARY, False, "error: %s" % e)


def check_finnhub(ticker):
    """Consensus, surprises, peers. The only keyed feed in the report."""
    import estimates_provider as EP
    configured = bool(os.environ.get(EP.ENV_KEY))
    if not configured:
        row("Finnhub (estimates)", VENDOR, False,
            "%s not set — add it to .env or the environment" % EP.ENV_KEY)
        return
    try:
        est = EP.fetch_estimates(ticker)
        cov = est.get("coverage") or {}
        rec = est.get("recommendation")
        pt = est.get("price_target")
        tier = "premium" if pt else "free"
        detail = "tier=%s" % tier
        if rec:
            detail += ", consensus=%s (%s, %d analysts)" % (
                rec.get("band"), rec.get("as_of"),
                sum(rec.get(k, 0) for k in ("strong_buy", "buy", "hold",
                                            "sell", "strong_sell")))
        else:
            detail += ", no consensus returned"
        detail += ", surprises=%d" % len(est.get("surprises") or [])
        gated = [k for k, v in cov.items() if v == "premium-gated"]
        if gated:
            detail += ", gated: %s" % ",".join(sorted(gated))
        row("Finnhub (estimates)", VENDOR, bool(rec), detail)
        peers = EP.fetch_peers(ticker)
        row("Finnhub (peers)", VENDOR, bool(peers and peers.get("rows")),
            "%d peers" % len((peers or {}).get("rows") or []))
    except Exception as e:
        row("Finnhub (estimates)", VENDOR, False, "error: %s" % e)


def check_market(ticker):
    """Price and volume bars — every level, average, RSI and chart. Reports
    the feed that ACTUALLY served, so a silent fallback to the unofficial
    source shows up here instead of being assumed away."""
    try:
        import research_live as RL
        mk = RL.fetch_market(ticker)
        n = len(mk.get("closes") or [])
        src = mk.get("bar_source") or "unknown"
        grade = VENDOR if mk.get("bar_source_licensed") else UNOFFICIAL
        row("Market bars (%s)" % src, grade, n > 200,
            "%d daily bars, last close %.2f, asof %s%s"
            % (n, (mk.get("closes") or [0])[-1], mk.get("bar_time"),
               "" if mk.get("bar_source_licensed")
               else "  [FELL BACK to unofficial feed]"))
    except Exception as e:
        row("Market bars", UNOFFICIAL, False, "error: %s" % e)


def check_news(ticker):
    """Reports the feed that actually served the headlines."""
    try:
        import research_live as RL
        items, src = RL._news_items(ticker, 8)
        grade = VENDOR if src == "polygon" else UNOFFICIAL
        row("News (%s)" % src, grade, len(items) > 0,
            "%d headlines%s" % (len(items),
                                "" if src == "polygon"
                                else "  [FELL BACK to unofficial feed]"))
    except Exception as e:
        row("News", UNOFFICIAL, False, "error: %s" % e)


def check_social(ticker):
    try:
        import urllib.request
        import json as _j
        u = ("https://api.stocktwits.com/api/2/streams/symbol/%s.json"
             % ticker.upper())
        req = urllib.request.Request(u, headers={"User-Agent": "research/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            j = _j.loads(r.read().decode("utf-8"))
        row("Social (StockTwits)", UNOFFICIAL, True,
            "%d messages" % len(j.get("messages") or []))
    except Exception as e:
        row("Social (StockTwits)", UNOFFICIAL, False, "error: %s" % e)


def main():
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "NOW").upper()
    print("\nFeed check for %s — no key or secret is ever printed\n" % ticker)
    print("  %-26s %-10s %-14s %s" % ("FEED", "GRADE", "STATUS", "DETAIL"))
    print("  " + "-" * 88)
    cik = check_sec_xbrl(ticker)
    if cik:
        check_edgar_exhibit(ticker, cik)
    check_finnhub(ticker)
    check_market(ticker)
    check_news(ticker)
    check_social(ticker)

    n_ok = sum(1 for _, _, ok, _ in _ROWS if ok)
    print("\n  %d/%d feeds connected" % (n_ok, len(_ROWS)))
    prim = [r for r in _ROWS if r[1] == PRIMARY]
    print("  primary (issuer/regulator): %d/%d connected"
          % (sum(1 for r in prim if r[2]), len(prim)))
    unoff = [r for r in _ROWS if r[1] == UNOFFICIAL]
    if unoff:
        print("  unofficial (no contract/SLA): %s"
              % ", ".join(r[0] for r in unoff))
    return 0


if __name__ == "__main__":
    sys.exit(main())
