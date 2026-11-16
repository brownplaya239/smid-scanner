"""Desk accuracy fixes (Nov 16 audit, section C6), page + worker side.

Extracts the real functions from docs/index.html and
cloudflare-worker/worker.js and runs them in Node with payloads shaped like
the live reports. Skipped when Node is not installed."""

import json
import re

from flow_site_js_test import HTML, _run, pytestmark  # noqa: F401  (skip marker)
from news_wire_test import WORKER, _node


def _js(names, expr, prelude=""):
    return _run(names, prelude + "\nconsole.log(JSON.stringify(" + expr + "));")


def _html():
    with open(HTML, encoding="utf-8") as f:
        return f.read()


# 1. one chip per ticker -------------------------------------------------
def test_lists_show_each_ticker_once():
    r = _js(
        ["deskUniqByTicker"],
        'deskUniqByTicker([{tk:"TENB",n:1},{tk:"AAPL"},{tk:"tenb ",n:2},{tk:""},null,{tk:"AAPL"}])',
    )
    assert r == [{"tk": "TENB", "n": 1}, {"tk": "AAPL"}]
    assert _js(["deskUniqByTicker"], 'deskUniqByTicker([{t:"X"},{t:"X"}], "t")') == [{"t": "X"}]


def test_every_action_list_segment_is_deduped():
    src = _html()
    seg = src[src.index("      function segment(title, items, maxShow") :]
    seg = seg[: seg.index("      const segments = [")]
    assert "items = deskUniqByTicker(items);" in seg


# 2. money format --------------------------------------------------------
def test_heavy_flow_uses_the_money_formatter():
    src = _html()
    assert "Math.round(f.premium / 1000)" not in src
    assert (
        'why: "Heavy bullish flow (" +\n                            fmtPrem(f.premium) + ")"' in src
    )


# 3. funds out of stock lists --------------------------------------------
def test_fund_flag_reader():
    assert _js(
        ["deskIsFund"], "[deskIsFund({etf:1}), deskIsFund({t:'X'}), deskIsFund(undefined)]"
    ) == [
        True,
        False,
        False,
    ]


def test_stock_lists_skip_funds():
    src = _html()
    # grade leaderboards, confluence ctx (Top Bullish/Bearish, Opportunities),
    # action list grades, fade candidates
    assert 'return !deskIsFund(c) && typeof eff(c) === "number";' in src
    assert "funds.forEach(function (t) { delete ctx[t]; });" in src
    assert "if (!deskIsFund(c)) grades[c.t] = g;" in src
    assert "if (!t || seen[t] || deskIsFund(DESK_TKMAP[t])) return;" in src


# 4. themes: one timeframe per column -------------------------------------
SWING = {
    "runs": [
        {
            "grades": {
                "A": [
                    {"t": "XOM", "chg": 2.0, "th": ["Oil & Gas"]},
                    {"t": "CVX", "chg": 1.0, "th": ["Oil & Gas"]},
                    {"t": "COP", "chg": 1.5, "th": ["Oil & Gas"]},
                    {"t": "NVDA", "chg": -1.0, "th": ["Semis"]},
                    {"t": "AMD", "chg": -2.0, "th": ["Semis"]},
                    {"t": "AVGO", "chg": 0.0, "th": ["Semis"]},
                ]
            }
        }
    ]
}


def test_theme_rollup_keeps_prior_close_and_today_apart():
    now = "Date.now()"
    prelude = (
        "var window = {LIVE_QUOTE_CACHE: {"
        f"XOM: {{change: -3, ts: {now}}}, CVX: {{change: -2, ts: {now}}}, "
        f"COP: {{change: -2.8, ts: {now}}}, NVDA: {{change: 2, ts: {now}}}"
        "}};\nlet THEME_ROLLUP_META = {};"
    )
    r = _js(
        ["buildThemeRollup", "themeRankPrior"],
        "themeRankPrior(buildThemeRollup(" + json.dumps(SWING) + ")).map(function (t) {"
        " return [t.theme, +t.chg_prior.toFixed(2), +t.chg_live.toFixed(2)]; })",
        prelude,
    )
    # Ranked by the prior close (Oil & Gas +1.5% first) even though today
    # Oil & Gas is down 2.6% and Semis are up; each figure keeps its own day.
    assert r == [["Oil & Gas", 1.5, -2.6], ["Semis", -1.0, 2.0]]


def test_themes_without_a_prior_close_are_not_ranked():
    themes = [
        {"theme": "A", "chg_prior": 1.0},
        {"theme": "NoPrior", "chg_prior": None},
        {"theme": "B", "chg_prior": -2.0},
        {"theme": "C", "chg_prior": 0.5},
    ]
    r = _js(
        ["themeRankPrior"],
        "themeRankPrior(T).map(function (t) { return t.theme; })",
        "const T = " + json.dumps(themes) + ";",
    )
    # the card reverses the bottom half for "Weakest": B must lead it
    assert r == ["A", "C", "B"]


def test_theme_card_labels_both_columns():
    src = _html()
    assert "'Prior close</span>'" in src and "'Today</span></div>'" in src
    assert "At the prior close &mdash; Strongest:" in src
    upd = src[src.index("function updateThemeCardLive()") :]
    upd = upd[: upd.index("\n    }\n")]
    assert ".tr-live" in upd and "chg_avg" not in upd


# 5. conviction board -----------------------------------------------------
CARRY = {
    "held_threshold": 0.5,
    "confirmed_count": 8,  # old producer: counted settled prints
    "total": 8,
    "contracts": [
        {"ticker": "META", "held": True, "oi_confirmed": 0.998, "net_new_oi": -4},
        {"ticker": "NVDA", "held": True, "oi_confirmed": 0.918, "net_new_oi": -824},
        {"ticker": "MRVL", "held": True, "oi_confirmed": 0.846, "net_new_oi": -2134},
        {"ticker": "NVDA", "held": False, "oi_confirmed": 0.126, "net_new_oi": -90462},
        {"ticker": "UPS", "held": False, "oi_confirmed": -0.168, "net_new_oi": -94485},
        {"ticker": "X", "held": None, "oi_confirmed": None, "net_new_oi": None},
        {"ticker": "Y", "held": False, "oi_confirmed": 0.6, "net_new_oi": 250},
    ],
}


def test_board_counts_only_passed_oi_checks():
    r = _js(["ovfCounts"], "ovfCounts(" + json.dumps(CARRY) + ")")
    assert r == {"total": 7, "confirmed": 3, "settled": 6}


def test_beyond_print_shows_extra_oi_not_negatives():
    cells = _js(
        ["ovfBeyondPrintHTML"],
        "CO.contracts.map(ovfBeyondPrintHTML)",
        "const CO = " + json.dumps(CARRY) + ";",
    )
    text = [re.sub(r"<[^>]+>", "", c) for c in cells]
    assert text == ["none", "none", "none", "none", "none", "—", "+250"]
    assert "-824" in cells[1]  # the raw figure stays in the tooltip
    assert "didn&#39;t grow past the print size" in cells[1]
    assert "OI fell" not in cells[1]
    src = _html()
    assert "not a sign that OI fell" not in src
    assert "'none' means open interest didn't grow past the print size" in src
    assert "% of the print was still open " in src and "print held overnight" not in src


# 6. Market Buzz ----------------------------------------------------------
def test_routine_filings_are_not_catalysts_on_the_page():
    names = ["pmbRoutineFiling"]
    routine = [
        "13G/A — passive holder position update",
        "13G filed — passive 5%+ holder",
        "144",
        "144 · FORM 144",
        "Insider transaction",
        "S-8 · S-8",
        "Shelf registration (S-3)",
        "S-3/A · S-3/A",
        "Quarterly report (10-Q)",
        "8-K · Financial Statements / Exhibits",
        "8-K · Submission of Matters to Holder Vote",
    ]
    real = [
        "8-K · Earnings Release",
        "8-K · Material Agreement + Financial Statements / Exhibits",
        "13D filed — activist / >5% holder",
        "13D/A — activist position update",
        "Prospectus supplement / shelf takedown",
        "Prospectus supplement",
        "424B3 · 424B3",
        "Late filing notice (NT 10-K)",
        "Late filing notice (NT 10-Q)",
        "IPO registration (S-1)",
        "S-1/A · S-1/A",
        "F-1 · F-1",
        "8-K · Bylaw/Charter Amendment",
    ]
    rows = [{"catalyst": c, "catalyst_src": "edgar"} for c in routine + real]
    rows.append({"catalyst": "144 analysts upgrade the stock", "catalyst_src": None})
    r = _js(names, "R.map(pmbRoutineFiling)", "const R = " + json.dumps(rows) + ";")
    assert r == [True] * len(routine) + [False] * len(real) + [False]


def test_no_catalyst_wording_follows_the_session():
    # Mon Nov 16 2026: 8:00 AM, 1:41 PM, 5:00 PM ET (EST = UTC-5)
    r = _js(
        ["pmbHalfDay", "phaseLabel", "pmbNoCatalyst"],
        "[13, 18.683, 22].map(function (h) {"
        " var ms = Date.UTC(2026, 10, 16, 0) + h * 3600000;"
        " return [phaseLabel(ms), pmbNoCatalyst(phaseLabel(ms))]; })"
        ".concat([[phaseLabel(Date.UTC(2026, 10, 16, 14, 30)), '']])",
    )
    assert r[0] == ["Pre-market", "No clear catalyst found (pre-market)"]
    assert r[1] == ["Intraday", "No clear catalyst found (today)"]
    assert r[2] == ["After-hours", "No clear catalyst found (after hours)"]
    assert r[3][0] == "Intraday"  # the regular session opens at 9:30
    assert (
        _js(["pmbHalfDay", "phaseLabel"], "phaseLabel(Date.UTC(2026, 10, 16, 14, 29))")
        == "Pre-market"
    )
    assert "pre-market mover" not in _html()


def _closed_reason_src():
    src = _html()
    start = src.index("window.tdMarketClosedReason = function")
    end = src.index("\n  };", start) + len("\n  };")
    return src[start:end]


def test_session_label_knows_holidays_and_half_days():
    prelude = "var window = {};\n" + _closed_reason_src() + "\n"
    # Thanksgiving Thu Nov 26 2026, 11:00 AM ET: no session at all
    # Fri Nov 27 2026 (1:00 PM close): 12:30 PM, 2:00 PM, 8:30 PM ET; Mon 2 PM
    r = _js(
        ["pmbHalfDay", "phaseLabel"],
        "[Date.UTC(2026, 10, 26, 16), Date.UTC(2026, 10, 27, 17, 30),"
        " Date.UTC(2026, 10, 27, 19), Date.UTC(2026, 10, 28, 1, 30),"
        " Date.UTC(2026, 10, 30, 19)].map(phaseLabel)",
        prelude,
    )
    assert r == ["Prior session", "Intraday", "After-hours", "Prior close", "Intraday"]


def test_site_half_days_match_exchange_calendar():
    import exchange_calendar

    src = _html()
    body = src[src.index("function pmbHalfDay(") :]
    body = body[: body.index("}")]
    got = sorted(re.findall(r'"(\d{4}-\d{2}-\d{2})"', body))
    assert got == sorted(d for ds in exchange_calendar.HALF_DAYS.values() for d in ds)


def test_worker_only_uses_event_filings_as_catalysts():
    cases = [
        ("8-K", "2.02,9.01", True),
        ("8-K", "1.01", True),
        ("8-K", "9.01", False),
        ("8-K", "5.07", False),
        ("8-K", "", True),
        ("6-K", "", True),
        ("SC 13D", "", True),
        ("SCHEDULE 13D", "", True),
        ("SC 13D/A", "", True),
        ("8-K", "5.03", True),
        ("8-K", "5.03,9.01", True),
        ("424B5", "", True),
        ("424B4", "", True),
        ("424B3", "", True),
        ("NT 10-K", "", True),
        ("NT 10-Q", "", True),
        ("S-1", "", True),
        ("S-1/A", "", True),
        ("F-1", "", True),
        ("S-3", "", False),
        ("SC 13G", "", False),
        ("3", "", False),
        ("5", "", False),
        ("SC 13G/A", "", False),
        ("SCHEDULE 13G", "", False),
        ("144", "", False),
        ("4", "", False),
        ("S-8", "", False),
        ("10-Q", "", False),
        ("SC TO-T", "", True),
        ("DEFM14A", "", True),
    ]
    body = (
        "console.log(JSON.stringify(C.map(function (c) { return isCatalystFiling(c[0], c[1]); })));"
    )
    r = _node(
        WORKER,
        ["is13D", "isCatalystFiling"],
        "const C = " + json.dumps([[f, i] for f, i, _ in cases]) + ";\n" + body,
        consts=("ROUTINE_8K_ITEMS",),
    )
    assert r == [want for _, _, want in cases]


# 7. earnings today --------------------------------------------------------
def test_action_list_earnings_use_todays_eastern_date():
    src = _html()
    assert "cardActionList(ctx, swing, uoa, uoaMeta, todayET)" in src
    assert "cardEarnings(uoaMeta, todayET, true)" in src


# 8. engine picks label ----------------------------------------------------
def test_picks_label_names_the_day():
    r = _js(
        ["deskPicksLabel"],
        "[deskPicksLabel('2026-11-12', Date.UTC(2026, 10, 16, 14)),"
        " deskPicksLabel('2026-11-13', Date.UTC(2026, 10, 16, 14)),"
        " deskPicksLabel('2026-10-30', Date.UTC(2026, 10, 16, 14)),"
        " deskPicksLabel(null)]",
    )
    assert r == ["picks from Thu", "picks from Fri", "picks from Oct 30", "last graded picks"]
    assert "yesterday’s picks " not in _html()


# 9. freshness stamp -------------------------------------------------------
def test_eod_batch_from_last_session_is_neutral():
    fri = "2026-11-13T21:45:00Z"  # Fri 4:45 PM ET
    mon = "Date.UTC(2026, 10, 16, 14)"  # Mon 9 AM ET
    r = _js(
        ["postCloseState"],
        "[postCloseState(F, function () { return 0; }, " + mon + "),"
        " postCloseState(F, function () { return 1; }, " + mon + "),"
        " postCloseState(F, function () { return 3; }, " + mon + "),"
        " postCloseState('2026-11-16T21:45:00Z', function () { return 0; },"
        " Date.UTC(2026, 10, 16, 23))]",
        "const F = " + json.dumps(fri) + ";",
    )
    assert r == [
        {"cls": "prior", "label": "last close (Fri)"},
        {"cls": "recent", "label": None},
        {"cls": "stale", "label": None},
        {"cls": "fresh", "label": None},
    ]
    src = _html()
    assert ".freshness.prior" in src and "window.tdSessionsBehind = sessionsBehind;" in src


# 10. Trade Desk tile vs badge --------------------------------------------
def test_family_badge_says_setups_today():
    r = _js(
        ["familyBadgeText"],
        "[familyBadgeText('SIGNAL_QUALIFIED', 0), familyBadgeText('SIGNAL_QUALIFIED', 1),"
        " familyBadgeText('WATCH', 0), familyBadgeText('TRADE_QUALIFIED', 2)]",
    )
    assert r == [
        "FAMILY SIGNAL QUALIFIED · 0 SETUPS TODAY",
        "FAMILY SIGNAL QUALIFIED · 1 SETUP TODAY",
        "WATCH",
        "FAMILY TRADE QUALIFIED · 2 SETUPS TODAY",
    ]
    assert 'tile("Signal Qualified · setups today"' in _html()


def test_action_list_shows_a_long_setup_once_and_money_formatted():
    swing = {
        "runs": [
            {
                "grades": {
                    "A-": [{"t": "TENB"}],
                    "B": [{"t": "MSFT"}],
                    "A+": [{"t": "TECL", "etf": 1}],
                }
            }
        ]
    }
    uoa = {
        "rows": [
            {"ticker": "TENB", "direction": "bullish", "premium": 472502},
            {"ticker": "TENB", "direction": "bullish", "premium": 100000},
            {"ticker": "MSFT", "direction": "bullish", "premium": 37235000},
            {"ticker": "TECL", "direction": "bullish", "premium": 9000000},
        ]
    }
    meta = {"ABUS": {"earnings_date": "2026-11-16"}, "INBX": {"earnings_date": "2026-11-13"}}
    prelude = (
        "var window = {tdWatchlist: function () { return new Set(['MSFT']); },"
        " tdUser: function () { return {}; }};\n"
        "function desk(t, b) { return b; }\nfunction freshness() { return {}; }\n"
        "const SW = "
        + json.dumps(swing)
        + ", U = "
        + json.dumps(uoa)
        + ", M = "
        + json.dumps(meta)
        + ";"
    )
    body = (
        "(function () { var a = cardActionList({}, SW, U, M, '2026-11-16');"
        " return a.split('class=\"al-seg\" ').slice(1).map(function (x) {"
        '  return [x.match(/al-seg-h">([^<]*)/)[1],'
        '   (x.match(/al-tk">[A-Z]+/g) || []).map(function (m) { return m.slice(7); }),'
        '   (x.match(/al-why">[^<]*/g) || []).map(function (m) { return m.slice(8); })]; }); })()'
    )
    segs = {
        name: (tks, whys)
        for name, tks, whys in _js(
            ["deskUniqByTicker", "deskIsFund", "cardActionList"], body, prelude
        )
    }
    # segment 1 is "Best Long Setups" (renamed "Strongest Setups" by PR #28)
    long_seg = segs.get("Strongest Setups") or segs["Best Long Setups"]
    assert long_seg[0] == ["TENB"]
    assert segs["Flow + Trend Confirmed"][0] == ["MSFT"]  # TENB not repeated; TECL is a fund
    assert segs["Earnings Today"][0] == ["ABUS"]
    urg = segs["⭐ Watchlist Urgencies"]
    assert urg == (["MSFT"], ["Heavy bullish flow ($37.2M)"])


def test_oi_confirmed_means_one_thing_everywhere():
    """Footer, brief, chip, Action List line and glossary all use the same
    test: at least half the print showed up as new open interest."""
    src = _html()
    assert "Whale positions held overnight" not in src
    assert "whale held overnight" not in src
    assert "at least half the print showed up as new open interest" in src
    with open(HTML.replace("index.html", "glossary.html"), encoding="utf-8") as f:
        gl = f.read()
    assert 'id="oi-confirmed-board"' in gl and 'id="oi-beyond-print"' in gl
    assert "at least half of the\n      print&rsquo;s size showed up as new open interest" in gl
    assert "open interest didn&rsquo;t grow past\n      the print size" in gl
