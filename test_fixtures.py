#!/usr/bin/env python3
"""test_fixtures.py — the brief under three market regimes.

The live pull only ever exercises whatever the tape is doing today. These
fixtures drive the whole chain — model, HTML, plain text, send gate — for
a risk-on, a neutral and a risk-off session, plus the two degenerate cases
that have historically been rendered badly: a watch list where nothing
changed, and a session with no qualifying news.

    python test_fixtures.py
    python test_fixtures.py --write   # dump each rendering for eyeballing
"""

import os
import sys

import brief_compose as BC
import brief_model as BM
import brief_news as BN
import brief_render as BR
import brief_text as BX

SITE = "https://tickerdesk.io"
UNSUB = "https://api.tickerdesk.io/unsubscribe?u=fixture&t=deadbeef"
AS_OF = "2026-07-22 07:20 ET"


def _idx(last, d1, w1, ytd, vs20):
    return {"last": last, "chg_1d_pct": d1, "chg_1w_pct": w1,
            "chg_ytd_pct": ytd, "dist_ma20_pct": vs20,
            "above_ma20": vs20 > 0}


def _con(tk, right, strike, side, prem_m, **kw):
    c = {"ticker": tk, "right": right, "strike": strike,
         "expiry": "2026-08-21", "side": side,
         "premium": "$%.1fM" % prem_m, "premium_raw": prem_m * 1e6,
         "spot": 100.0, "printed_at": "2026-07-21T19:44:00Z",
         "oi_state": BC.CONF_PENDING}
    c.update(kw)
    return c


def _market(label, why, indices, events=None):
    return {"indices": indices,
            "regime": {"label": label, "why": why},
            "session_label": "Pre-Market Brief", "as_of_et": AS_OF,
            "events": events or [
                {"title": "Crude Oil Inventories", "time_et": "10:30 a.m. ET",
                 "status": "UPCOMING", "source_time": "2:30pm",
                 "source_tz": "UTC", "starts_at": "2026-07-22T10:30:00-04:00",
                 "source_url": "https://www.eia.gov/petroleum/supply/weekly/"}]}


RISK_ON = _market(
    "RISK-ON",
    "72% of the 750-name universe is above its 20-day average; 4 of 4 major "
    "indices above their 20-day; VIX 12.8 (-6.1%); 10-year yield 4.21% "
    "(-4 bp).",
    {"SPY": _idx(770.10, 1.24, 2.10, 12.90, 2.6),
     "QQQ": _idx(731.44, 1.61, 2.88, 19.40, 3.1),
     "IWM": _idx(305.02, 1.905, 2.44, 22.60, 2.2),
     "DIA": _idx(534.80, 0.98, 1.72, 10.10, 1.8)})

NEUTRAL = _market(
    "BALANCED",
    "51% of the 750-name universe is above its 20-day average; 2 of 4 major "
    "indices above their 20-day; VIX 15.9 (+0.4%); 10-year yield 4.47% "
    "(+1 bp).",
    {"SPY": _idx(748.28, 0.11, -0.20, 9.53, 0.02),
     "QQQ": _idx(708.97, -0.06, -0.44, 15.63, -0.01),
     "IWM": _idx(296.54, 0.31, 0.18, 19.20, 0.9),
     "DIA": _idx(521.51, -0.12, -0.31, 7.83, -0.7)})

RISK_OFF = _market(
    "RISK-OFF",
    "28% of the 750-name universe is above its 20-day average; 0 of 4 major "
    "indices above their 20-day; VIX 27.4 (+18.2%); 10-year yield 4.88% "
    "(+11 bp).",
    {"SPY": _idx(709.66, -2.41, -4.10, 3.90, -4.2),
     "QQQ": _idx(660.15, -3.02, -5.55, 7.70, -5.6),
     "IWM": _idx(272.30, -2.88, -6.02, 9.10, -6.4),
     "DIA": _idx(498.44, -1.96, -3.40, 3.10, -3.5)})


def _watch(kind):
    if kind == "quiet":
        return [{"ticker": t} for t in ("AAA", "BBB", "CCC", "DDD")]
    base = [
        {"ticker": "GEV", "grade_delta": 1, "grade_from": "B",
         "grade_to": "A-", "has_flow": True, "flow_hq": True,
         "flow_direction": BC.BEARISH, "flow_short_dated": True,
         "earnings_in_days": 1, "earnings_confirmed": True,
         "price": 1078.81,
         "price_record": BC.price_record(1078.81, BC.BASIS_CLOSE, "Jul 21"),
         "evidence": "moderate", "signal_strength": "A-"},
        {"ticker": "MU", "grade_delta": 2, "grade_from": "B-",
         "grade_to": "A-", "has_flow": True, "flow_hq": True,
         "flow_confirmed": True, "flow_direction": BC.BULLISH,
         "price_change_pct": 5.4, "price": 118.20,
         "price_record": BC.price_record(118.20, BC.BASIS_PREMARKET,
                                         "07:20 ET"),
         "evidence": "moderate", "signal_strength": "A+"},
        {"ticker": "T", "has_flow": True, "flow_hq": True,
         "flow_direction": BC.BEARISH, "price_change_pct": -0.4,
         "price": 22.26,
         "price_record": BC.price_record(22.26, BC.BASIS_CLOSE, "Jul 21"),
         "evidence": "limited", "signal_strength": "C"},
        {"ticker": "NOPX", "has_flow": True, "flow_hq": True,
         "flow_direction": BC.BULLISH, "price": None,
         "price_record": BC.price_record(None, BC.BASIS_CLOSE, "",
                                         reason="source timeout"),
         "evidence": "limited"},
        {"ticker": "ZZZ"}, {"ticker": "YYY"},
    ]
    if kind == "risk_off":
        base[1]["flow_direction"] = BC.BEARISH
        base[1]["grade_delta"] = -2
        base[1]["grade_from"], base[1]["grade_to"] = "A-", "B-"
        base[1]["price_change_pct"] = -6.1
    return base


def _flow():
    return ({"GEV": [_con("GEV", "put", 930, "put_buyer", 0.2)],
             "MU": [_con("MU", "call", 120, "call_buyer", 2.4, is_sweep=True,
                         oi_state=BC.CONF_YES, oi_delta=900,
                         oi_as_of="2026-07-22T13:00:00Z")],
             "T": [_con("T", "call", 22.5, "call_seller", 0.6)],
             "NOPX": [_con("NOPX", "call", 40, "call_buyer", 1.2)],
             "TEL": [_con("TEL", "call", 240, "mixed", 4.6, is_sweep=True)]},
            [_con("IBM", "call", 250, "call_buyer", 4.9),
             _con("META", "put", 625, "put_buyer", 3.0, is_sweep=True),
             _con("COIN", "call", 310, "call_buyer", 3.3)])


def _news(kind):
    if kind == "empty":
        return BN.select([], [], [])
    mkt = [{"title": "Fed officials split on the pace of rate cuts",
            "url": "https://www.federalreserve.gov/x",
            "publisher": "Federal Reserve",
            "published": "2026-07-22T10:40:00Z",
            "insights": [{"ticker": "SPY", "sentiment": "neutral",
                          "sentiment_reasoning":
                          "Minutes show a divided committee"}]},
           {"title": "Crude oil slips ahead of the weekly inventory report",
            "url": "https://www.reuters.com/y", "publisher": "Reuters",
            "published": "2026-07-22T10:05:00Z"}]
    wl = [{"title": "Micron guides memory pricing higher for the fourth "
                    "quarter", "url": "https://www.globenewswire.com/z",
           "publisher": "GlobeNewswire", "published": "2026-07-22T09:30:00Z",
           "tickers": ["MU"],
           "insights": [{"ticker": "MU", "sentiment": "positive",
                         "sentiment_reasoning":
                         "Guidance above consensus on DRAM pricing"}]}]
    return BN.select(mkt, wl, ["GEV", "MU", "T", "NOPX"],
                     as_of="2026-07-22T11:20:00Z")


def build(regime, market, watch_kind="normal", news_kind="normal"):
    watch_flow, market_flow = _flow()
    if watch_kind == "quiet":
        watch_flow = {}
        market_flow = market_flow[:1]
    wl = BC.rank_watchlist(_watch(watch_kind))
    weekly = {"line": "Participation narrowed over the week: 51% of the "
                      "750-name universe is above its 20-day average, "
                      "against 63% five sessions ago.",
              "sub": "Five-session view", "changed": True}
    return BM.build(
        market, wl, news=_news(news_kind), market_flow=market_flow,
        watch_flow=watch_flow,
        discovery=[{"ticker": "CLF", "contract": "PUT 8 2027-03-19",
                    "side_label": "PUT BUY · bearish", "premium": "$2.0M",
                    "oi_state": BC.CONF_PENDING,
                    "why": "largest print outside your list"}],
        weekly=weekly, site=SITE, unsub=UNSUB, as_of=AS_OF)


CASES = [
    ("risk_on", RISK_ON, "normal", "normal"),
    ("neutral", NEUTRAL, "normal", "normal"),
    ("risk_off", RISK_OFF, "risk_off", "normal"),
    ("quiet_watchlist", NEUTRAL, "quiet", "normal"),
    ("no_news", NEUTRAL, "normal", "empty"),
]


def main():
    write = "--write" in sys.argv
    fails, ran = [], [0]

    def chk(n, c, d=""):
        ran[0] += 1
        print(("  PASS  " if c else "  FAIL  ") + n
              + ("" if c else "  <- %s" % (d,)))
        if not c:
            fails.append(n)

    for name, market, wk, nk in CASES:
        print("\n== %s ==" % name)
        model = build(name, market, wk, nk)
        subject = BC.build_subject(market, {"n_changed": len(
            BM.section(model, "watchlist")["records"])},
            sections=BM.section_ids(model))
        pre = BC.build_preheader(
            market, {"shown": BM.section(model, "watchlist")["records"]},
            {"indices": "SPY %+.2f%%" % market["indices"]["SPY"]["chg_1d_pct"],
             "vix": "", "ten_year": ""},
            sections=BM.section_ids(model))
        doc = BR.render(model, preheader=pre)
        txt = BX.render_text(model, subject=subject)
        body = BC.visible_text(doc)

        chk("%s: one valid document" % name,
            BC.check_document(doc) == [], BC.check_document(doc))
        chk("%s: no negative zero" % name,
            BC.check_no_negative_zero(body) == [],
            BC.check_no_negative_zero(body))
        chk("%s: unsubscribe matches" % name,
            BC.check_unsubscribe(doc, UNSUB) == [],
            BC.check_unsubscribe(doc, UNSUB))
        chk("%s: plain text carries the URLs" % name,
            BC.check_plain_text(txt, BX.urls_in(SITE, UNSUB)) == [],
            BC.check_plain_text(txt, BX.urls_in(SITE, UNSUB)))
        chk("%s: model is internally consistent" % name,
            BC.check_model(model) == [], BC.check_model(model))
        chk("%s: regime label rendered" % name,
            market["regime"]["label"] in body)
        chk("%s: 20-day summary present" % name,
            "their 20-day averages" in body, body[:0])

        # populations identical across bodies
        for sec in model["sections"]:
            for r in sec.get("records") or []:
                tk = r.get("ticker")
                if tk:
                    chk("%s: %s/%s in both bodies" % (name, sec["id"], tk),
                        tk in body and tk in txt, tk)

        if nk == "empty":
            chk("%s: empty news says so" % name,
                "No high-relevance headlines" in body
                and "No high-relevance headlines" in txt)
        else:
            chk("%s: news headline rendered" % name,
                "Micron guides memory pricing" in body)
            chk("%s: primary tier shown" % name, "PRIMARY" in body)
        if wk == "quiet":
            chk("%s: quiet list does not claim changes" % name,
                "0 of your" in body or "No material changes" in body,
                body[:0])
        else:
            chk("%s: a missing price says so rather than blank" % name,
                "Price unavailable" in body and "Price unavailable" in txt)
            chk("%s: OI-confirmed name is not called merely pending" % name,
                "CONFIRMED BULLISH" in body or "CONFIRMED BEARISH" in body,
                body[:0])

        if write:
            d = os.path.join("docs", "email-previews", "fixtures")
            os.makedirs(d, exist_ok=True)
            open(os.path.join(d, "%s.html" % name), "w",
                 encoding="utf-8").write(doc)
            open(os.path.join(d, "%s.txt" % name), "w",
                 encoding="utf-8").write(txt)
            print("  wrote fixtures/%s.{html,txt}" % name)

    print("\n%d/%d checks passed" % (ran[0] - len(fails), ran[0]))
    if fails:
        print("FAILED: " + "; ".join(fails[:8]))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
