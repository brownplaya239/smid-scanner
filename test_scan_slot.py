"""Breakout-scan scheduling, the stale economic-calendar check and the
earnings-idea timing (2026-11-16 audit, A4-A6).

Python: scan_slot.plan (which scanner.yml run scans what) and
economic_calendar.calendar_stale. Node (skipped without node): the
worker's breakout slot clock + missing-report check + Nasdaq date-offset
vote, and the site's econCalStatus / earnTiming, extracted from the real
files."""

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import date, datetime

import pytest

import brief_render
import brief_text
import economic_calendar
import exchange_calendar
import scan_slot
from brief_time import ET

_BASE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(_BASE, "cloudflare-worker", "worker.js")
HTML = os.path.join(_BASE, "docs", "index.html")
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")


def et(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


# ── scan_slot.plan ─────────────────────────────────────────────────────
# Mon 2026-11-16 is a session; Thu 11-26 Thanksgiving; Fri 11-27 half day.


def test_breakout_waits_for_the_first_slot():
    p = scan_slot.plan(et(2026, 11, 16, 9, 45), "breakout", [])
    assert p["kind"] is None
    assert "before the first" in p["reason"]


def test_breakout_slot_runs_both_universes_when_nothing_archived():
    p = scan_slot.plan(et(2026, 11, 16, 10, 2), "breakout", [])
    assert p["kind"] == "breakout"
    assert p["run"] == {"smid": True, "iwm": True}


def test_breakout_slot_fills_only_the_missing_universe():
    files = ["docs/reports/smid_scanner_2026-11-16_1004.pdf"]
    p = scan_slot.plan(et(2026, 11, 16, 10, 20), "breakout", files)
    assert p["run"] == {"smid": False, "iwm": True}


def test_breakout_slot_that_already_ran_is_skipped():
    files = ["smid_scanner_2026-11-16_1004.pdf", "iwm_scanner_2026-11-16_1009.pdf"]
    p = scan_slot.plan(et(2026, 11, 16, 11, 0), "breakout", files)
    assert p["kind"] is None
    assert "already ran" in p["reason"]


def test_next_slot_needs_its_own_report():
    # the 10:00 reports do not cover the 11:30 slot
    files = ["smid_scanner_2026-11-16_1004.pdf", "iwm_scanner_2026-11-16_1009.pdf"]
    p = scan_slot.plan(et(2026, 11, 16, 11, 31), "breakout", files)
    assert p["run"] == {"smid": True, "iwm": True}
    # ...and a report from yesterday's 15:00 slot does not cover today's
    old = ["smid_scanner_2026-11-13_1504.pdf", "iwm_scanner_2026-11-13_1509.pdf"]
    p = scan_slot.plan(et(2026, 11, 16, 15, 1), "breakout", old)
    assert p["run"] == {"smid": True, "iwm": True}


def test_no_breakout_scan_after_the_close():
    p = scan_slot.plan(et(2026, 11, 16, 16, 20), "breakout", [])
    assert p["kind"] is None


def test_holiday_and_weekend_skip():
    assert scan_slot.plan(et(2026, 11, 26, 10, 1), "breakout", [])["kind"] is None
    assert scan_slot.plan(et(2026, 11, 21, 10, 1), "breakout", [])["kind"] is None
    assert scan_slot.plan(et(2026, 11, 26, 16, 50), "auto", [])["kind"] is None


def test_half_day_skips_the_afternoon_slot():
    assert scan_slot.plan(et(2026, 11, 27, 10, 1), "breakout", [])["kind"] == "breakout"
    assert scan_slot.plan(et(2026, 11, 27, 15, 1), "breakout", [])["kind"] is None


def test_auto_mode_follows_the_clock_and_builds_setups_once():
    assert scan_slot.plan(et(2026, 11, 16, 11, 40), "auto", [])["kind"] == "breakout"
    p = scan_slot.plan(et(2026, 11, 16, 16, 47), "auto", [])
    assert p["kind"] == "setup"
    assert p["run"] == {"smid": True, "iwm": True}
    # the late GitHub backstop at ~9 PM finds today's setups and skips
    done = ["smid_setup_2026-11-16_1647.pdf", "iwm_setup_2026-11-16_1655.pdf"]
    assert scan_slot.plan(et(2026, 11, 16, 21, 5), "auto", done)["kind"] is None
    # a setup run asked for before the close waits
    assert scan_slot.plan(et(2026, 11, 16, 15, 30), "setup", [])["kind"] is None


def test_force_runs_anyway():
    files = ["smid_scanner_2026-11-16_1004.pdf", "iwm_scanner_2026-11-16_1009.pdf"]
    p = scan_slot.plan(et(2026, 11, 16, 10, 30), "breakout", files, force=True)
    assert p["run"] == {"smid": True, "iwm": True}


# ── economic_calendar.calendar_stale ───────────────────────────────────


def _cal(*dates):
    return {"events": [{"date": d, "title": "x"} for d in dates]}


def test_calendar_from_last_week_is_stale_on_monday():
    cal = _cal("2026-11-09", "2026-11-13")
    assert economic_calendar.calendar_stale(cal, date(2026, 11, 16))
    # still current on the Friday it covers, and on that weekend
    assert not economic_calendar.calendar_stale(cal, date(2026, 11, 13))
    assert not economic_calendar.calendar_stale(cal, date(2026, 11, 15))


def test_calendar_stale_reads_both_date_formats_and_absent_is_not_stale():
    assert not economic_calendar.calendar_stale(_cal("11-16-2026"), date(2026, 11, 18))
    assert economic_calendar.calendar_stale(_cal("11-13-2026"), date(2026, 11, 18))
    assert not economic_calendar.calendar_stale({"events": []}, date(2026, 11, 18))
    assert not economic_calendar.calendar_stale(None, date(2026, 11, 18))


def test_brief_says_the_calendar_is_out_of_date():
    sec = {
        "id": "calendar",
        "title": "Macro calendar",
        "kind": "event",
        "records": [],
        "sub": "",
        "empty_line": "Economic calendar not updated for this week.",
    }
    assert "not updated for this week" in brief_render.render_event(sec, "")
    out = []
    brief_text.t_event(sec, "", out)
    assert any("not updated for this week" in line for line in out)


# ── Node helpers ───────────────────────────────────────────────────────


def _between(src, start, end):
    i = src.index(start)
    return src[i : src.index(end, i)]


def _func(src, name):
    """`function name(...) {...}` by brace matching (regex literals in
    these functions contain no braces outside quantifiers)."""
    start = src.index(f"function {name}(")
    i = src.index("{", start)
    depth = 0
    while True:
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1


def _node(js):
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as f:
        f.write(js)
    try:
        out = subprocess.run(
            [NODE, f.name], capture_output=True, encoding="utf-8", check=True, timeout=30
        )
    finally:
        os.unlink(f.name)
    return json.loads(out.stdout)


def _worker():
    with open(WORKER, encoding="utf-8") as f:
        return f.read()


def _html():
    with open(HTML, encoding="utf-8") as f:
        return f.read()


def test_worker_holiday_table_matches_exchange_calendar():
    src = _worker()

    def arr(name):
        body = _between(src, f"const {name} = [", "];")
        return sorted(re.findall(r'"(\d{4}-\d{2}-\d{2})"', body))

    want_h = sorted(d for ds in exchange_calendar.HOLIDAYS.values() for d in ds)
    want_half = sorted(d for ds in exchange_calendar.HALF_DAYS.values() for d in ds)
    assert arr("MARKET_HOLIDAYS") == want_h
    assert arr("MARKET_HALF_DAYS") == want_half


@needs_node
def test_worker_breakout_clock():
    js = _between(_worker(), "const BREAKOUT_SLOTS", "function etDateISO")
    r = _node(
        js
        + """
const cases = {
  mon_1000: breakoutDue(1, "2026-11-16", "10:00"),
  mon_1130: breakoutDue(1, "2026-11-16", "11:30"),
  mon_1500: breakoutDue(1, "2026-11-16", "15:00"),
  mon_1015: breakoutDue(1, "2026-11-16", "10:15"),
  mon_1045: breakoutDue(1, "2026-11-16", "10:45"),
  mon_1215: breakoutDue(1, "2026-11-16", "12:15"),
  mon_1545: breakoutDue(1, "2026-11-16", "15:45"),
  sat_1000: breakoutDue(6, "2026-11-21", "10:00"),
  thanksgiving: breakoutDue(4, "2026-11-26", "10:00"),
  half_1000: breakoutDue(5, "2026-11-27", "10:00"),
  half_1500: breakoutDue(5, "2026-11-27", "15:00"),
  half_1545: breakoutDue(5, "2026-11-27", "15:45"),
};
const man = { reports: {
  "smid-scanner": [{ file: "smid_scanner_2026-11-16_1004.pdf" }],
  "iwm-scanner": [{ file: "iwm_scanner_2026-11-13_1509.pdf" }],
} };
cases.missing_1000 = missingBreakoutReports(man, "2026-11-16", "10:00");
cases.missing_1130 = missingBreakoutReports(man, "2026-11-16", "11:30");
cases.missing_empty = missingBreakoutReports({}, "2026-11-16", "10:00");
console.log(JSON.stringify(cases));
"""
    )
    on = {"dispatch": True, "check": None}
    off = {"dispatch": False, "check": None}
    assert r["mon_1000"] == on and r["mon_1130"] == on and r["mon_1500"] == on
    assert r["mon_1015"] == off
    assert r["mon_1045"] == {"dispatch": False, "check": "10:00"}
    assert r["mon_1215"] == {"dispatch": False, "check": "11:30"}
    assert r["mon_1545"] == {"dispatch": False, "check": "15:00"}
    assert r["sat_1000"] == off and r["thanksgiving"] == off
    assert r["half_1000"] == on
    assert r["half_1500"] == off and r["half_1545"] == off
    assert r["missing_1000"] == ["iwm-scanner"]
    assert r["missing_1130"] == ["smid-scanner", "iwm-scanner"]
    assert r["missing_empty"] == ["smid-scanner", "iwm-scanner"]


@needs_node
def test_worker_learns_nasdaq_date_offset():
    js = _between(_worker(), "const ECON_WEEKDAY_ANCHORS", "function _econUsTitles")
    r = _node(
        js
        + """
console.log(JSON.stringify({
  // 2026-11-16 as observed: date=11-17 held Monday's bill auctions,
  // date=11-16 held no US releases at all
  lagging: econDateShift([
    { query: "2026-11-16", titles: [] },
    { query: "2026-11-17", titles: ["NY Empire State Manufacturing Index",
                                    "3-Month Bill Auction"] },
  ]),
  lagging_thu: econDateShift([
    { query: "2026-11-19", titles: ["MBA Mortgage Applications",
                                    "Crude Oil Inventories"] },
    { query: "2026-11-20", titles: ["Initial Jobless Claims"] },
  ]),
  correct: econDateShift([
    { query: "2026-11-18", titles: ["MBA Mortgage Applications"] },
    { query: "2026-11-19", titles: ["Initial Jobless Claims"] },
  ]),
  no_evidence: econDateShift([
    { query: "2026-11-17", titles: ["Redbook"] },
  ]),
  plus1: _isoAddDays("2026-11-30", 1),
}));
"""
    )
    assert r["lagging"] == 1
    assert r["lagging_thu"] == 1
    assert r["correct"] == 0
    assert r["no_evidence"] is None
    assert r["plus1"] == "2026-12-01"


@needs_node
def test_site_econ_calendar_status():
    js = _func(_html(), "econCalStatus")
    r = _node(
        js
        + """
const last = { events: [{ date: "2026-11-09" }, { date: "2026-11-13" }] };
const live = { events: [{ date: "11-16-2026", title: "Empire" }] };
console.log(JSON.stringify({
  monday_last_week: econCalStatus(last, "2026-11-16"),
  friday_same_week: econCalStatus(last, "2026-11-13"),
  sunday_after: econCalStatus(last, "2026-11-15"),
  live_today: econCalStatus(live, "2026-11-16"),
  quiet_day: econCalStatus({ events: [{ date: "2026-11-17" }] }, "2026-11-16"),
}));
"""
    )
    assert r["monday_last_week"]["stale"] is True
    assert r["monday_last_week"]["newest"] == "2026-11-13"
    assert r["friday_same_week"]["stale"] is False
    assert len(r["friday_same_week"]["todays"]) == 1
    assert r["sunday_after"]["stale"] is False
    assert r["live_today"]["stale"] is False
    assert len(r["live_today"]["todays"]) == 1
    assert r["quiet_day"] == {"todays": [], "newest": "2026-11-17", "stale": False}


@needs_node
def test_site_earnings_timing():
    js = _func(_html(), "earnTiming")
    r = _node(
        js
        + """
const at = function (iso) { return Date.parse(iso); };
const mon11 = at("2026-11-16T16:00:00Z");      // Mon 11:00 AM EST
console.log(JSON.stringify({
  // Monday 2026-11-16 (the audit's examples)
  tsn_bmo_reported: earnTiming("2026-11-16", "BMO", mon11),
  pags_amc_later: earnTiming("2026-11-16", "AMC", mon11),
  tbd_today: earnTiming("2026-11-16", "TBD", mon11),
  as_tomorrow: earnTiming("2026-11-17", "BMO", mon11),
  cgem_saturday: earnTiming("2026-11-14", "AMC", mon11),
  amc_after_close: earnTiming("2026-11-16", "AMC", at("2026-11-16T21:05:00Z")),
  bmo_before_open: earnTiming("2026-11-16", "BMO", at("2026-11-16T14:29:00Z")),
  // Friday 8:40 PM ET (Saturday in UTC): Monday is 3 days out
  fri_night_monday: earnTiming("2026-11-16", "BMO", at("2026-11-14T01:40:00Z")),
  // Monday 10 PM ET is Tuesday in UTC: Tuesday is still "tomorrow"
  mon_night_tuesday: earnTiming("2026-11-17", "BMO", at("2026-11-17T03:00:00Z")),
  // weekend: never "today"
  sat_same_day: earnTiming("2026-11-14", "AMC", at("2026-11-14T15:00:00Z")),
  sat_for_monday: earnTiming("2026-11-16", "BMO", at("2026-11-14T15:00:00Z")),
  // DST: 13:45 UTC is 8:45 AM EST (Fri 3/6) but 9:45 AM EDT (Mon 3/9)
  dst_before: earnTiming("2026-03-06", "BMO", at("2026-03-06T13:45:00Z")),
  dst_after: earnTiming("2026-03-09", "BMO", at("2026-03-09T13:45:00Z")),
  // fall back: 14:15 UTC is 10:15 AM EDT (Fri 10/30), 9:15 AM EST (Mon 11/2)
  fall_before: earnTiming("2026-10-30", "BMO", at("2026-10-30T14:15:00Z")),
  fall_after: earnTiming("2026-11-02", "BMO", at("2026-11-02T14:15:00Z")),
  bad_date: earnTiming("", "BMO", mon11),
}));
"""
    )
    assert r["tsn_bmo_reported"] == {"past": True}
    assert r["pags_amc_later"] == {"past": False, "label": "today"}
    assert r["tbd_today"] == {"past": False, "label": "today"}
    assert r["as_tomorrow"] == {"past": False, "label": "tomorrow"}
    assert r["cgem_saturday"] == {"past": True}
    assert r["amc_after_close"] == {"past": True}
    assert r["bmo_before_open"] == {"past": False, "label": "today"}
    assert r["fri_night_monday"] == {"past": False, "label": "in 3d"}
    assert r["mon_night_tuesday"] == {"past": False, "label": "tomorrow"}
    assert r["sat_same_day"]["past"] is False
    assert "today" not in r["sat_same_day"]["label"]
    assert r["sat_same_day"]["label"] == "Sat 11/14"
    assert r["sat_for_monday"] == {"past": False, "label": "in 2d"}
    assert r["dst_before"] == {"past": False, "label": "today"}
    assert r["dst_after"] == {"past": True}
    assert r["fall_before"] == {"past": True}
    assert r["fall_after"] == {"past": False, "label": "today"}
    assert r["bad_date"] is None
