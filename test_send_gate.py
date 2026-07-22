#!/usr/bin/env python3
"""test_send_gate.py — prove the gate blocks, not just that it runs.

A validator that returns [] on good input has demonstrated nothing. Each
case below injects exactly one defect from the send-time list into an
otherwise-valid payload and asserts that validate_send() reports it. The
control case at the end asserts the same payload passes untouched, so a
gate that blocked everything would fail too.

    python test_send_gate.py
"""

import sys

import brief_compose as BC

GOOD_DOC = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8"></head>'
    '<body><p>2 of your 4 watch list names changed materially.</p>'
    '<p>55% of the 700-name universe is above its 20-day average.</p>'
    '<p>SPY 0.00% QQQ +1.20%</p>'
    '<a href="https://api.tickerdesk.io/unsubscribe?u=u1&amp;t=sig">'
    'Unsubscribe</a></body></html>')
GOOD_TEXT = ("Desk: https://tickerdesk.io/#desk\n"
             "Settings: https://tickerdesk.io/#settings\n"
             "Unsubscribe: https://api.tickerdesk.io/unsubscribe?u=u1&t=sig\n")
UNSUB = "https://api.tickerdesk.io/unsubscribe?u=u1&t=sig"
URLS = ["https://tickerdesk.io/#desk", "https://tickerdesk.io/#settings",
        UNSUB]
SECTIONS = ("market", "watchlist", "flow", "calendar")

WL = BC.rank_watchlist([{"ticker": "A", "grade_delta": 2, "has_flow": True,
                         "grade_from": "B", "grade_to": "A"},
                        {"ticker": "B", "grade_delta": 2, "has_flow": True,
                         "grade_from": "C", "grade_to": "B"},
                        {"ticker": "C"}, {"ticker": "D"}])
MARKET = {"indices": {"SPY": {"above_ma20": True}, "QQQ": {"above_ma20": True},
                      "IWM": {"above_ma20": False}, "DIA": {"above_ma20": True}},
          "breadth": {"breadth_pct": 55, "universe": 700},
          "regime": {"label": "BALANCED",
                     "why": "3 of 4 major indices above their 20-day"}}


def base(**over):
    kw = dict(market=MARKET, wl=WL, news=[], events=[],
              watch_flow={"A": [{"side": "call_buy", "oi_state": BC.CONF_YES,
                                 "oi_delta": 10}]},
              market_flow=[], html_doc=GOOD_DOC, text_doc=GOOD_TEXT,
              as_of="2026-07-22 06:39 ET", eligible_total=4,
              subject="Balanced tape · 2 watchlist changes",
              preheader="SPY 0.00% QQQ +1.20% · A grade B to A.",
              unsub=UNSUB, sections=SECTIONS, urls=URLS,
              calendar_problems=())
    kw.update(over)
    return kw


CASES = [
    ("malformed document nesting",
     dict(html_doc=GOOD_DOC.replace("<body>", "<body><!doctype html>"
                                    '<html lang="en"><head></head><body>', 1)
          + "</body></html>")),
    ("incorrect timezone conversion",
     dict(calendar_problems=["calendar declared America/New_York but 6 of 6 "
                             "recognised releases sit +240 min from their "
                             "published time"])),
    ("mismatched unsubscribe URL",
     dict(html_doc=GOOD_DOC.replace(
         "https://api.tickerdesk.io/unsubscribe?u=u1&amp;t=sig",
         "https://tickerdesk.io/#watchlist"))),
    ("unsubscribe pointing at the watch list",
     dict(unsub="https://tickerdesk.io/#watchlist")),
    ("unsupported subject claim",
     dict(subject="Balanced tape · Semis leads · 2 watchlist changes")),
    ("over-long subject", dict(subject="Balanced tape · " + "x" * 60)),
    ("watch-list count mismatch", dict(eligible_total=9)),
    ("changed name hidden with no overflow line",
     dict(wl={**WL, "overflow": [{"ticker": "Z", "reasons": ["moved"]}],
              "overflow_line": ""})),
    ("displayed row with no reason",
     dict(wl={**WL, "shown": [{"ticker": "Z", "reasons": []}],
              "overflow": [], "quiet": [], "n_changed": 1},
          eligible_total=1)),
    ("premature UNCONFIRMED",
     dict(watch_flow={"A": [{"side": "call_buy", "oi_state": BC.CONF_NO}]})),
    ("negative zero",
     dict(html_doc=GOOD_DOC.replace("SPY 0.00%", "SPY -0.00%"))),
    ("missing plain-text URL",
     dict(text_doc="Open your desk on the site.\n")),
    ("empty plain-text part", dict(text_doc=" ")),
    ("incomplete preheader",
     dict(preheader="SPY 0.00% QQQ +1.20% · A grade B to A ·")),
    ("over-long preheader", dict(preheader="x" * 200)),
    ('missing lang="en"',
     dict(html_doc=GOOD_DOC.replace('<html lang="en">', "<html>"))),
]


def main():
    fails, ran = [], [0]

    def chk(n, c, d=""):
        ran[0] += 1
        print(("  PASS  " if c else "  FAIL  ") + n
              + ("" if c else "  <- %s" % (d,)))
        if not c:
            fails.append(n)

    # control first: if this does not pass, every block below is meaningless
    clean = BC.validate_send(**base())
    chk("CONTROL: a clean payload passes", clean == [], clean)

    for name, mut in CASES:
        got = BC.validate_send(**base(**mut))
        chk("blocks: %s" % name, bool(got),
            "gate returned no problems")

    print("\n%d/%d checks passed" % (ran[0] - len(fails), ran[0]))
    if fails:
        print("FAILED: " + "; ".join(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
