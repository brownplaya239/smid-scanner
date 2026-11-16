"""Breakout-scan scheduling, the stale economic-calendar check and the
earnings-idea timing (2026-11-16 audit, A4-A6).

Python: scan_slot.plan (which scanner.yml run scans what, by slot id and
completion record), the backstop cron -> slot map, scan_slot record, and
economic_calendar.calendar_stale. Node (skipped without node): the
worker's breakout slot clock, completion-record alert, the scheduled
handler end to end (real worker module, fetch / R2 / clock mocked), the
Nasdaq date-offset vote (incl. holiday weeks), and the site's
econCalStatus / earnTiming, extracted from the real files."""

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, date, datetime

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
# `records` = completion records by slot id, as scan_slot.record writes.

S1000 = "2026-11-16T10:00ET"
S1130 = "2026-11-16T11:30ET"
S1500 = "2026-11-16T15:00ET"
SETUP = "2026-11-16T16:00ET"


def done(*pairs):
    """{slot: {universe: record}} for (slot, universe) pairs, each a clean
    run that archived nothing (no candidates)."""
    out = {}
    for sid, u in pairs:
        out.setdefault(sid, {})[u] = {"reports": []}
    return out


def test_breakout_waits_for_the_first_slot():
    p = scan_slot.plan(et(2026, 11, 16, 9, 45), "breakout")
    assert p["kind"] is None
    assert "before the first" in p["reason"]


def test_breakout_slot_runs_both_universes_when_nothing_recorded():
    p = scan_slot.plan(et(2026, 11, 16, 10, 2), "breakout", slot=S1000)
    assert p["kind"] == "breakout"
    assert p["slot"] == S1000
    assert p["run"] == {"smid": True, "iwm": True}


def test_breakout_slot_fills_only_the_missing_universe():
    p = scan_slot.plan(et(2026, 11, 16, 10, 20), "breakout", done((S1000, "smid")), slot=S1000)
    assert p["run"] == {"smid": False, "iwm": True}


def test_empty_successful_scans_count_as_done():
    # both scans finished cleanly with no candidates: no PDF, but recorded
    recs = done((S1000, "smid"), (S1000, "iwm"))
    p = scan_slot.plan(et(2026, 11, 16, 11, 0), "breakout", recs, files=[], slot=S1000)
    assert p["kind"] is None
    assert "already ran" in p["reason"]
    # a late backstop for the same slot (no slot input, by the clock) too
    assert scan_slot.plan(et(2026, 11, 16, 11, 5), "auto", recs)["kind"] is None
    # an empty setup build counts as today's build
    recs = done((SETUP, "smid"), (SETUP, "iwm"))
    assert scan_slot.plan(et(2026, 11, 16, 21, 5), "auto", recs)["kind"] is None
    assert scan_slot.plan(et(2026, 11, 16, 16, 50), "setup", recs, slot=SETUP)["kind"] is None


def test_report_from_a_delayed_earlier_slot_does_not_cover_the_next_slot():
    # the 10:00 run started 10:05 and finished at 11:35: its PDFs are
    # stamped 1135, but its record is the 10:00 slot's. The queued 11:30
    # run still scans both universes.
    files = ["smid_scanner_2026-11-16_1135.pdf", "iwm_scanner_2026-11-16_1135.pdf"]
    recs = done((S1000, "smid"), (S1000, "iwm"))
    p = scan_slot.plan(et(2026, 11, 16, 11, 36), "breakout", recs, files, slot=S1130)
    assert p["slot"] == S1130
    assert p["run"] == {"smid": True, "iwm": True}


def test_run_that_starts_after_the_next_slot_serves_the_next_slot():
    # the 10:00 dispatch sat in the queue until 11:40: it scans for 11:30
    p = scan_slot.plan(et(2026, 11, 16, 11, 40), "breakout", slot=S1000)
    assert p["slot"] == S1130
    assert p["run"] == {"smid": True, "iwm": True}
    assert "replaced" in p["reason"]
    # ...so the 11:30 dispatch queued behind it skips
    recs = done((S1130, "smid"), (S1130, "iwm"))
    assert scan_slot.plan(et(2026, 11, 16, 11, 55), "breakout", recs, slot=S1130)["kind"] is None


def test_slot_not_started_or_from_another_day_is_skipped():
    p = scan_slot.plan(et(2026, 11, 16, 11, 0), "breakout", slot=S1130)
    assert p["kind"] is None and "not started" in p["reason"]
    p = scan_slot.plan(et(2026, 11, 17, 10, 5), "breakout", slot=S1000)
    assert p["kind"] is None and "not today" in p["reason"]
    p = scan_slot.plan(et(2026, 11, 16, 10, 5), "breakout", slot="2026-11-16T10:15ET")
    assert p["kind"] is None and "not a breakout slot" in p["reason"]


def test_next_slot_needs_its_own_record():
    recs = done((S1000, "smid"), (S1000, "iwm"))
    p = scan_slot.plan(et(2026, 11, 16, 11, 31), "breakout", recs, slot=S1130)
    assert p["run"] == {"smid": True, "iwm": True}
    # ...and yesterday's 15:00 record does not cover today's
    old = done(("2026-11-13T15:00ET", "smid"), ("2026-11-13T15:00ET", "iwm"))
    p = scan_slot.plan(et(2026, 11, 16, 15, 1), "breakout", old, slot=S1500)
    assert p["run"] == {"smid": True, "iwm": True}


def test_no_breakout_scan_after_the_close():
    p = scan_slot.plan(et(2026, 11, 16, 16, 20), "breakout")
    assert p["kind"] is None
    # a backstop for the 3:00 slot that GitHub starts at 4:20 PM
    assert scan_slot.plan(et(2026, 11, 16, 16, 20), "auto", slot=S1500)["kind"] is None


def test_holiday_and_weekend_skip():
    assert scan_slot.plan(et(2026, 11, 26, 10, 1), "breakout")["kind"] is None
    assert scan_slot.plan(et(2026, 11, 21, 10, 1), "breakout")["kind"] is None
    assert scan_slot.plan(et(2026, 11, 26, 16, 50), "auto")["kind"] is None
    p = scan_slot.plan(et(2026, 11, 26, 16, 50), "setup", slot="2026-11-26T16:00ET")
    assert p["kind"] is None


def test_half_day_skips_the_afternoon_slot():
    assert scan_slot.plan(et(2026, 11, 27, 10, 1), "breakout")["kind"] == "breakout"
    assert scan_slot.plan(et(2026, 11, 27, 15, 1), "breakout")["kind"] is None


def test_auto_mode_follows_the_clock_and_builds_setups_once():
    assert scan_slot.plan(et(2026, 11, 16, 11, 40), "auto")["kind"] == "breakout"
    p = scan_slot.plan(et(2026, 11, 16, 16, 47), "setup", slot=SETUP)
    assert p["kind"] == "setup" and p["slot"] == SETUP
    assert p["run"] == {"smid": True, "iwm": True}
    # the late GitHub backstop at ~9 PM finds today's record and skips
    recs = done((SETUP, "smid"), (SETUP, "iwm"))
    assert scan_slot.plan(et(2026, 11, 16, 21, 5), "auto", recs)["kind"] is None
    # ...or today's setup PDFs, for builds from before records existed
    pdfs = ["smid_setup_2026-11-16_1647.pdf", "iwm_setup_2026-11-16_1655.pdf"]
    assert scan_slot.plan(et(2026, 11, 16, 21, 5), "auto", None, pdfs)["kind"] is None
    # a setup run asked for before the close waits
    assert scan_slot.plan(et(2026, 11, 16, 15, 30), "setup")["kind"] is None


def test_force_runs_anyway():
    recs = done((S1000, "smid"), (S1000, "iwm"))
    p = scan_slot.plan(et(2026, 11, 16, 10, 30), "breakout", recs, force=True, slot=S1000)
    assert p["run"] == {"smid": True, "iwm": True}
    assert p["slot"] == S1000


def test_backstop_crons_map_to_their_slots():
    def utc(*a):
        return datetime(*a, tzinfo=UTC)

    # the "10:07 AM" cron that GitHub started at 4:20 PM ET Monday
    assert scan_slot.slot_for_cron("7 15 * * 1-5", utc(2026, 11, 16, 21, 20)) == S1000
    assert scan_slot.slot_for_cron("37 16 * * 1-5", utc(2026, 11, 16, 16, 40)) == S1130
    assert scan_slot.slot_for_cron("7 20 * * 1-5", utc(2026, 11, 16, 23, 0)) == S1500
    # the setup cron started after midnight UTC still means Monday's build
    assert scan_slot.slot_for_cron("17 21 * * 1-5", utc(2026, 11, 17, 4, 20)) == SETUP
    # Friday's setup cron started on Monday at 03:00 UTC: Friday's slot
    got = scan_slot.slot_for_cron("17 21 * * 1-5", utc(2026, 11, 16, 3, 0))
    assert got == "2026-11-13T16:00ET"
    assert scan_slot.slot_for_cron("0 9 * * *", utc(2026, 11, 16, 9, 0)) is None
    # every cron line in scanner.yml has a slot
    with open(os.path.join(_BASE, ".github", "workflows", "scanner.yml"), encoding="utf-8") as f:
        crons = re.findall(r"cron: '([^']+)'", f.read())
    assert sorted(crons) == sorted(scan_slot.CRON_SLOTS)


def test_late_backstop_for_a_previous_day_does_not_run():
    # Friday's setup cron arriving Monday 10:30 ET
    p = scan_slot.plan(et(2026, 11, 16, 10, 30), "auto", slot="2026-11-13T16:00ET")
    assert p["kind"] is None and "not today" in p["reason"]


def test_record_marks_an_empty_scan_done(monkeypatch, tmp_path):
    store = {}

    class FakeR2:
        @staticmethod
        def get_json(key):
            return store.get(key)

        @staticmethod
        def put_json(key, obj):
            store[key] = json.loads(json.dumps(obj))

    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "smid_scanner_2026-11-16_0944.pdf").write_bytes(b"old")
    snap = tmp_path / "before.json"
    monkeypatch.setattr(scan_slot, "REPORTS_DIR", str(reports))
    monkeypatch.setattr(scan_slot, "SNAPSHOT", str(snap))
    monkeypatch.setattr(scan_slot, "_r2", lambda: FakeR2)
    snap.write_text(json.dumps(["smid_scanner_2026-11-16_0944.pdf"]))

    # SMID found nothing (no new PDF); IWM archived one
    scan_slot.main(["record", "--slot", S1000, "--universe", "smid"])
    (reports / "iwm_scanner_2026-11-16_1012.pdf").write_bytes(b"new")
    scan_slot.main(["record", "--slot", S1000, "--universe", "iwm"])

    doc = store["scan_runs/2026-11-16.json"]
    assert doc["date"] == "2026-11-16"
    assert doc["slots"][S1000]["smid"]["reports"] == []
    assert doc["slots"][S1000]["iwm"]["reports"] == ["iwm_scanner_2026-11-16_1012.pdf"]
    assert doc["slots"][S1000]["smid"]["kind"] == "breakout"
    # and the next trigger for that slot skips
    p = scan_slot.plan(et(2026, 11, 16, 10, 40), "breakout", doc["slots"], slot=S1000)
    assert p["kind"] is None


def test_records_are_read_from_r2(monkeypatch):
    class FakeR2:
        @staticmethod
        def get_json(key):
            assert key == "scan_runs/2026-11-16.json"
            return {"slots": done((S1000, "smid"))}

    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test")
    monkeypatch.setattr(scan_slot, "_r2", lambda: FakeR2)
    assert scan_slot.load_records(date(2026, 11, 16)) == done((S1000, "smid"))

    class Broken:
        @staticmethod
        def get_json(key):
            raise OSError("R2 down")

    monkeypatch.setattr(scan_slot, "_r2", lambda: Broken)
    assert scan_slot.load_records(date(2026, 11, 16)) == {}


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
    src = _worker()
    js = _between(src, "const MARKET_HOLIDAYS", "function _hhmmToMin") + _between(
        src, "const ECON_WEEKDAY_ANCHORS", "function _econUsTitles"
    )
    r = _node(
        js
        + """
console.log(JSON.stringify({
  // 2026-11-16 as observed: date=11-17 held Monday's bill auctions,
  // date=11-16 held no US releases at all
  lagging: econDateShift([
    { query: "2026-11-16", titles: [] },
    { query: "2026-11-17", titles: ["NY Empire State Manufacturing Index",
                                    "3-Month Bill Auction",
                                    "6-Month Bill Auction"] },
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
    { query: "2026-11-17", titles: ["NY Empire State Manufacturing Index"] },
  ]),
  // a correctly dated Tuesday: Redbook alone is enough to stay at 0
  correct_tue: econDateShift([
    { query: "2026-11-17", titles: ["Redbook"] },
  ]),
  // one anchor alone is not enough to shift
  single_anchor: econDateShift([
    { query: "2026-11-17", titles: ["3-Month Bill Auction"] },
  ]),
  // anchors that disagree: no shift
  disagree: econDateShift([
    { query: "2026-11-19", titles: ["MBA Mortgage Applications",
                                    "Crude Oil Inventories"] },
    { query: "2026-11-18", titles: ["MBA Mortgage Applications"] },
  ]),
  // Columbus Day Mon 2026-10-12: the CORRECT feed lists both bill
  // auctions on Tuesday 10-13. Must not read as a one-day lag.
  columbus_correct: econDateShift([
    { query: "2026-10-13", titles: ["3-Month Bill Auction",
                                    "6-Month Bill Auction"] },
    { query: "2026-10-14", titles: ["MBA Mortgage Applications"] },
  ]),
  // Thanksgiving week 2026: claims move to Wednesday 11-25
  thanksgiving_correct: econDateShift([
    { query: "2026-11-25", titles: ["Initial Jobless Claims",
                                    "Natural Gas Storage",
                                    "MBA Mortgage Applications"] },
  ]),
  // Veterans Day Wed 2026-11-11 (bond market shut, stocks open)
  veterans_week: econDateShift([
    { query: "2026-11-12", titles: ["MBA Mortgage Applications",
                                    "Crude Oil Inventories"] },
  ]),
  // the Monday after a holiday week is judged on its own week
  monday_after_veterans: econDateShift([
    { query: "2026-11-17", titles: ["3-Month Bill Auction",
                                    "6-Month Bill Auction"] },
  ]),
  plus1: _isoAddDays("2026-11-30", 1),
  hw_columbus: _econHolidayWeek("2026-10-15"),
  hw_plain: _econHolidayWeek("2026-11-18"),
  hw_unknown_year: _econHolidayWeek("2031-03-04"),
}));
"""
    )
    assert r["lagging"] == 1
    assert r["lagging_thu"] == 1
    assert r["correct"] == 0
    assert r["no_evidence"] is None
    assert r["correct_tue"] == 0
    assert r["single_anchor"] is None
    assert r["disagree"] is None
    assert r["columbus_correct"] is None
    assert r["thanksgiving_correct"] is None
    assert r["veterans_week"] is None
    assert r["monday_after_veterans"] == 1
    assert r["plus1"] == "2026-12-01"
    assert r["hw_columbus"] is True
    assert r["hw_plain"] is False
    assert r["hw_unknown_year"] is True


@needs_node
def test_worker_live_calendar_in_a_holiday_week():
    """_econFromNasdaq with Nasdaq's feed mocked. In Thanksgiving week the
    week's own anchors are not trusted, so the offset is learned from the
    week before; a correctly dated Columbus-week feed stays unshifted."""
    src = _worker()
    js = (
        _between(src, "const MARKET_HOLIDAYS", "function _hhmmToMin")
        + _between(src, "const _EC_HI", "function _ffTag")
        + """
let MEM = {};
function memGet(k) { return k in MEM ? MEM[k] : null; }
function memPut(k, v) { MEM[k] = v; }
// what really happens on each day
const REAL = {
  "2026-11-16": ["3-Month Bill Auction", "6-Month Bill Auction"],
  "2026-11-17": ["Redbook"],
  "2026-11-23": ["3-Month Bill Auction", "6-Month Bill Auction"],
  "2026-11-24": ["Redbook", "CB Consumer Confidence"],
  "2026-10-06": ["Redbook"],
  "2026-10-07": ["MBA Mortgage Applications", "Crude Oil Inventories"],
  "2026-10-13": ["3-Month Bill Auction", "6-Month Bill Auction", "Redbook"],
};
let LAG = 0;
const asked = [];
globalThis.fetch = async function (url) {
  const q = String(url).split("date=")[1].slice(0, 10);
  asked.push(q);
  const d = new Date(q + "T12:00:00Z");
  d.setUTCDate(d.getUTCDate() - LAG);
  const rows = (REAL[d.toISOString().slice(0, 10)] || []).map(function (t) {
    return { country: "United States", eventName: t, gmt: "15:00" };
  });
  return new Response(JSON.stringify({ data: { rows: rows } }));
};
(async function () {
  LAG = 1;
  const tg = await _econFromNasdaq("2026-11-24");
  const tgShift = MEM.econshift, tgAsked = asked.splice(0);
  MEM = {}; LAG = 0;
  const co = await _econFromNasdaq("2026-10-13");
  console.log(JSON.stringify({
    tg: tg.map(function (e) { return [e.date, e.title]; }), tgShift, tgAsked,
    co: co.map(function (e) { return e.title; }), coShift: MEM.econshift,
  }));
})();
"""
    )
    r = _node(js)
    # Thanksgiving week, lagging feed: Tuesday's releases, learned from 11/17-18
    assert r["tgShift"] == 1
    assert sorted(r["tgAsked"][:4]) == ["2026-11-17", "2026-11-18", "2026-11-24", "2026-11-25"]
    assert [t for _, t in r["tg"]] == ["Redbook", "CB Consumer Confidence"]
    assert {d for d, _ in r["tg"]} == {"11-24-2026"}
    # Columbus week, correct feed: the Tuesday bill auctions stay on Tuesday
    assert r["coShift"] == 0
    assert "3-Month Bill Auction" in r["co"]


@needs_node
def test_site_econ_calendar_status():
    js = _func(_html(), "econCalStatus")
    r = _node(
        js
        + """
const last = { events: [{ date: "2026-11-09" }, { date: "2026-11-13" }] };
const live = { events: [{ date: "11-16-2026", title: "Empire" }] };
// Mon 2026-11-16 10:00 AM ET
const now = Date.parse("2026-11-16T15:00:00Z");
const liveDay = function (extra) {
  return Object.assign({ source: "nasdaq-live", scope: "day", date: "2026-11-16",
    updated: "2026-11-16T14:58:00Z", events: [] }, extra || {});
};
console.log(JSON.stringify({
  monday_last_week: econCalStatus(last, "2026-11-16", now),
  friday_same_week: econCalStatus(last, "2026-11-13", now),
  sunday_after: econCalStatus(last, "2026-11-15", now),
  live_today: econCalStatus(live, "2026-11-16", now),
  quiet_day: econCalStatus({ events: [{ date: "2026-11-17" }] }, "2026-11-16", now),
  // a fresh, empty live day: a quiet day, not an old calendar
  live_empty: econCalStatus(liveDay(), "2026-11-16", now),
  // weekend + holiday: Nasdaq lists nothing, still not "out of date"
  live_empty_sat: econCalStatus(liveDay({ date: "2026-11-21",
    updated: "2026-11-21T15:00:00Z" }), "2026-11-21",
    Date.parse("2026-11-21T15:05:00Z")),
  live_empty_thanksgiving: econCalStatus(liveDay({ date: "2026-11-26",
    updated: "2026-11-26T15:00:00Z" }), "2026-11-26",
    Date.parse("2026-11-26T15:05:00Z")),
  // worker deployed before date/scope: Nasdaq-live fetched today
  live_empty_old_worker: econCalStatus({ source: "nasdaq-live",
    updated: "2026-11-16T14:58:00Z", events: [] }, "2026-11-16", now),
  // a live day fetched yesterday or 7h ago is not today's
  live_empty_yesterday: econCalStatus(liveDay({ date: "2026-11-13",
    updated: "2026-11-13T15:00:00Z" }), "2026-11-16", now),
  live_empty_old: econCalStatus(liveDay({ updated: "2026-11-16T07:59:00Z" }),
    "2026-11-16", now),
  // the weekly file is never day-only
  archive_empty: econCalStatus({ updated: "2026-11-16T14:58:00Z", events: [] },
    "2026-11-16", now),
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
    assert r["quiet_day"] == {"todays": [], "newest": "2026-11-17", "live": False, "stale": False}
    for k in (
        "live_empty",
        "live_empty_sat",
        "live_empty_thanksgiving",
        "live_empty_old_worker",
    ):
        assert r[k]["live"] is True, k
        assert r[k]["stale"] is False, k
    for k in ("live_empty_yesterday", "live_empty_old", "archive_empty"):
        assert r[k]["live"] is False, k
        assert r[k]["stale"] is True, k


def test_site_keeps_a_fresh_empty_live_calendar():
    # the desk used to swap any empty live response for the weekly file
    src = _html()
    i = src.index('fetch(WORKER_URL + "/?econ-calendar=1")')
    assert "econCalStatus(d, window.tdTodayET()).live" in src[i : i + 600]


# The scheduled handler end to end: the real worker module, with fetch,
# the R2 binding and the clock replaced. Returns what it sent out.
_SCHEDULED_HARNESS = """
import { pathToFileURL } from "node:url";
const NOW = Date.parse(process.argv[3]);
const RealDate = Date;
globalThis.Date = class extends RealDate {
  constructor(...a) { if (a.length) super(...a); else super(NOW); }
  static now() { return NOW; }
};
const calls = [];
globalThis.fetch = async function (url, init) {
  const body = init && init.body ? JSON.parse(init.body) : null;
  calls.push({ url: String(url), method: (init && init.method) || "GET", body });
  if (String(url).indexOf("https://slack.com/") === 0) {
    return new Response(JSON.stringify({ ok: true }));
  }
  if (String(url).indexOf("api.github.com") >= 0) {
    return new Response(null, { status: 204 });
  }
  return new Response("{}", { status: 404 });
};
const record = JSON.parse(process.argv[4]);
const reads = [];
const env = {
  PAT: "test-pat", SLACK_BOT_TOKEN: "xoxb-test", SLACK_ALERT_CHANNEL: "C0TEST",
  DATA: { get: async function (k) {
    reads.push(k);
    return record === null ? null
      : { json: async function () { return record; } };
  } },
};
const waits = [];
const mod = await import(pathToFileURL(process.argv[2]).href);
await mod.default.scheduled({ cron: "*/15 * * * *" }, env,
  { waitUntil: function (p) { waits.push(p); } });
await Promise.all(waits);
console.log(JSON.stringify({ calls, reads }));
"""


def _scheduled(now_utc, record=None):
    d = tempfile.mkdtemp()
    try:
        worker = os.path.join(d, "worker.mjs")
        shutil.copyfile(WORKER, worker)
        # worker.js imports its sibling claude.mjs (#24)
        shutil.copyfile(
            os.path.join(os.path.dirname(WORKER), "claude.mjs"), os.path.join(d, "claude.mjs")
        )
        harness = os.path.join(d, "harness.mjs")
        with open(harness, "w", encoding="utf-8") as f:
            f.write(_SCHEDULED_HARNESS)
        out = subprocess.run(
            [NODE, harness, worker, now_utc, json.dumps(record)],
            capture_output=True,
            encoding="utf-8",
            check=True,
            timeout=60,
        )
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def _dispatches(r, workflow):
    return [
        c["body"]
        for c in r["calls"]
        if c["url"].endswith("/actions/workflows/%s/dispatches" % workflow)
    ]


def _slack(r):
    return [c["body"] for c in r["calls"] if c["url"].startswith("https://slack.com/")]


@needs_node
def test_scheduled_handler_dispatches_the_breakout_slot():
    # Tue 2026-11-17 10:00 AM ET (EST)
    r = _scheduled("2026-11-17T15:00:20Z")
    assert _dispatches(r, "scanner.yml") == [
        {"ref": "master", "inputs": {"mode": "breakout", "slot": "2026-11-17T10:00ET"}}
    ]
    assert _slack(r) == []
    # 11:30 and 3:00 PM name their own slots
    r = _scheduled("2026-11-17T16:30:05Z")
    assert _dispatches(r, "scanner.yml")[0]["inputs"]["slot"] == "2026-11-17T11:30ET"
    # 4:45 PM: the setup build, slot <date>T16:00ET
    r = _scheduled("2026-11-17T21:45:05Z")
    assert _dispatches(r, "scanner.yml") == [
        {"ref": "master", "inputs": {"mode": "setup", "slot": "2026-11-17T16:00ET"}}
    ]
    # Thanksgiving: no breakout dispatch
    assert _dispatches(_scheduled("2026-11-26T15:00:20Z"), "scanner.yml") == []


@needs_node
def test_scheduled_handler_alerts_only_for_unrecorded_universes():
    sid = "2026-11-17T10:00ET"
    at = "2026-11-17T15:45:10Z"  # 10:45 AM ET, 45 min after the slot
    # both universes finished cleanly with no candidates: no alert
    rec = {"slots": {sid: {"smid": {"reports": []}, "iwm": {"reports": []}}}}
    r = _scheduled(at, rec)
    assert r["reads"] == ["scan_runs/2026-11-17.json"]
    assert _slack(r) == []
    # IWM never finished: alert names IWM only
    r = _scheduled(at, {"slots": {sid: {"smid": {"reports": []}}}})
    (msg,) = _slack(r)
    assert msg["channel"] == "C0TEST"
    assert "IWM did not finish the 10:00 AM ET slot (2026-11-17)" in msg["text"]
    assert "SMID" not in msg["text"]
    # a record only for the 11:30 slot does not cover 10:00
    r = _scheduled(at, {"slots": {"2026-11-17T11:30ET": {"smid": {}, "iwm": {}}}})
    assert "SMID and IWM" in _slack(r)[0]["text"]


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
