"""Callout Results: entry timing, every callout counted, matured vs open,
stale closes, Eastern time."""

from datetime import date, datetime

import callout_tracker as ct

DAY = "2026-11-10"  # a Tuesday


def _ms(iso):
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def _daily(d, h, c):
    # Polygon daily bars start at midnight ET (05:00Z in November)
    return {"t": _ms(f"{d}T05:00:00+00:00"), "h": h, "c": c}


def _entry(**kw):
    e = {
        "date": DAY,
        "occ": "O:TST261218C00100000",
        "basis": ct.BASIS,
        "ticker": "TST",
        "type": "call",
        "strike": 100.0,
        "expiry": "2026-12-18",
        "callout_at": "2026-11-10T16:00:30+00:00",  # 11:00:30 ET
        "rank": 3,
        "entry": None,
        "status": "pending",
    }
    e.update(kw)
    return e


MINUTES = [
    # traded at 1.00 when first scanned, ran to 2.00 before the callout
    {"t": _ms("2026-11-10T14:40:00+00:00"), "o": 1.0, "h": 1.1, "c": 1.0},
    {"t": _ms("2026-11-10T15:59:00+00:00"), "o": 2.0, "h": 2.1, "c": 2.0},
    {"t": _ms("2026-11-10T16:01:00+00:00"), "o": 2.0, "h": 2.4, "c": 2.2},
    {"t": _ms("2026-11-10T20:59:00+00:00"), "o": 1.8, "h": 1.9, "c": 1.8},
]


def _bars(minutes=MINUTES, daily=()):
    def bars(occ, span, frm, to):
        if span == "minute":
            return list(minutes) if minutes is not None else None
        if daily is None:
            return None
        return [b for b in daily if frm <= ct._bar_day(b) <= to]

    return bars


def test_entry_is_first_bar_after_the_top25_callout_not_first_scan():
    e = _entry()
    ct.track(e, date(2026, 11, 10), bars=_bars())
    assert e["entry"] == 2.0  # the 11:01 ET bar, not the 9:40 ET 1.00 print
    assert e["entry_ts"] == _ms("2026-11-10T16:01:00+00:00")
    assert e["peak"] == 2.4
    assert e["peak_pct"] == 20.0
    assert e["last_pct"] == -10.0  # flag-day close 1.80
    assert e["status"] == "tracked"
    assert e["matured"] is False


def test_peak_and_final_over_the_window_with_stale_close():
    daily = [
        _daily("2026-11-11", 3.0, 2.5),
        _daily("2026-11-12", 2.6, 0.5),  # last trade; no trades on 11-13
    ]
    e = _entry(expiry="2026-11-13")  # window = 11-11, 11-12, 11-13
    ct.track(e, date(2026, 11, 13), bars=_bars(daily=daily))
    assert e["peak"] == 3.0 and e["peak_pct"] == 50.0
    assert e["matured"] is True
    assert e["last_pct"] == -75.0
    assert e["last_bar_date"] == "2026-11-12"
    assert e["last_stale"] is True  # no trade on the window's last session


def test_window_counts_sessions_not_bars():
    # an illiquid option with ONE daily bar still matures after 10 sessions
    daily = [_daily("2026-11-11", 2.2, 2.1)]
    e = _entry()
    ct.track(e, date(2026, 11, 24), bars=_bars(daily=daily))
    assert e["sessions"] == 10
    assert e["matured"] is True
    assert e["last_stale"] is True


def test_no_trade_after_callout_is_excluded_not_dropped():
    e = _entry(callout_at="2026-11-10T21:30:00+00:00")  # after the last trade
    ct.track(e, date(2026, 11, 10), bars=_bars())
    assert e["status"] == "no_entry"
    s = ct.stats([e, _done_entry(10.0, matured=True)])
    assert s["n"] == 2 and s["tracked"] == 1 and s["excluded_no_trade"] == 1


def test_api_failure_stays_pending():
    e = _entry()
    ct.track(e, date(2026, 11, 10), bars=_bars(minutes=None))
    assert e["status"] == "pending" and e["entry"] is None
    e = _entry()
    ct.track(e, date(2026, 11, 12), bars=_bars(daily=None))
    assert e["status"] == "pending" and e.get("peak_pct") is None
    assert not e.get("matured")


def test_callout_day_still_trading_is_left_alone():
    e = _entry()
    ct.track(e, date(2026, 11, 9), bars=_bars())
    assert e["entry"] is None and e["status"] == "pending"


def _done_entry(last_pct, matured, stale=False, peak_pct=60.0):
    return _entry(
        status="tracked",
        entry=1.0,
        peak=1.0 + peak_pct / 100,
        peak_pct=peak_pct,
        last_pct=last_pct,
        matured=matured,
        last_stale=stale,
    )


def test_matured_and_open_medians_are_separate():
    ents = [
        _done_entry(-90.0, True),
        _done_entry(-80.0, True, stale=True),
        _done_entry(40.0, False),
        _done_entry(20.0, False),
        _done_entry(0.0, False, stale=True),
    ]
    s = ct.stats(ents)
    assert s["matured"] == 2 and s["median_final_pct"] == -85.0 and s["final_stale"] == 1
    assert s["open"] == 3 and s["median_latest_pct"] == 20.0 and s["latest_stale"] == 1
    assert "median_last_pct" not in s


def test_legacy_first_scan_entries_are_never_published():
    log = {
        "a": {**_done_entry(5.0, True), "basis": None},
        "b": _done_entry(5.0, True),
    }
    out = ct.publish(log)
    assert out["legacy_unpublished"] == 1
    assert out["rolling"]["n"] == 1


def test_log_callouts_replaces_legacy_and_keeps_new():
    c = {
        "ticker": "TST",
        "type": "call",
        "strike": 100.0,
        "expiry": "2026-12-18",
        "first_top_at": "2026-11-10T16:00:30+00:00",
        "first_top_rank": 7,
        "best_rank": 3,
    }
    key = f"{DAY}|O:TST261218C00100000"
    log = {key: {"date": DAY, "entry": 1.0, "first_seen": "2026-11-10T14:35:00+00:00"}}
    assert ct.log_callouts(log, DAY, [c]) == 1
    assert log[key]["basis"] == ct.BASIS and log[key]["callout_at"] == c["first_top_at"]
    assert log[key]["entry"] is None
    assert ct.log_callouts(log, DAY, [{**c, "best_rank": 1}]) == 0
    assert log[key]["best_rank"] == 1


def test_eastern_time_follows_daylight_saving():
    # 00:30 UTC on Nov 10 is 19:30 EST on Nov 9 (a fixed UTC-4 said 20:30)
    assert ct._et_date("2026-11-10T00:30:00Z") == "2026-11-09"
    # 04:30 UTC: still Nov 9 in EST; a fixed UTC-4 put it on Nov 10
    assert ct._et_date("2026-11-10T04:30:00Z") == "2026-11-09"
    assert ct.last_complete_session(datetime(2026, 11, 10, 15, 0, tzinfo=ct.ET)) == date(
        2026, 11, 9
    )
    assert ct.last_complete_session(datetime(2026, 11, 10, 16, 30, tzinfo=ct.ET)) == date(
        2026, 11, 10
    )


def test_callouts_from_lists_uses_first_list_with_rank_25_or_better():
    def doc(rank):
        return {
            "et_date": DAY,
            "rows": [
                {
                    "ticker": "TST",
                    "type": "call",
                    "strike": 100.0,
                    "expiry": "2026-12-18",
                    "rank": rank,
                }
            ],
        }

    lists = [
        ("2026-11-10T14:40:00+00:00", doc(40)),
        ("2026-11-10T15:10:00+00:00", doc(12)),
        ("2026-11-10T15:40:00+00:00", doc(5)),
        ("2026-11-11T05:10:00+00:00", doc(1)),  # after midnight ET: ignored
    ]
    got = ct.callouts_from_lists(lists)[DAY]
    assert len(got) == 1
    assert got[0]["first_top_at"] == "2026-11-10T15:10:00+00:00"
    assert got[0]["first_top_rank"] == 12 and got[0]["best_rank"] == 5


def test_payload_without_callout_times_is_not_used():
    assert ct.callouts_of_payload({"et_date": DAY, "rows": [], "callouts": []}) is None
    assert ct.callouts_of_payload(
        {"et_date": DAY, "callouts_complete": True, "callouts": [{"x": 1}]}
    ) == (DAY, [{"x": 1}])
