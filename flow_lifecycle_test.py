"""LIFE column: OI read only on sessions, an un-updated vendor OI is never a
CLOSED verdict, volume frozen to the flag day, flag date = the list's date."""

import flow_lifecycle as fl

OCC = "O:TST261218C00100000"
KEY = "TST|2026-12-18|call|100"


def _top(et_date, rank=1):
    return {
        "et_date": et_date,
        "rows": [
            {
                "ticker": "TST",
                "type": "call",
                "strike": 100.0,
                "expiry": "2026-12-18",
                "rank": rank,
                "side": "CALL BUY",
            }
        ],
    }


def _snap(generated, oi=1000, vol=10000):
    return {
        "generated": generated,
        "rows": [
            {
                "contract": OCC,
                "ticker": "TST",
                "type": "call",
                "strike": 100.0,
                "expiry": "2026-12-18",
                "open_interest": oi,
                "volume": vol,
            }
        ],
    }


def _state(**kw):
    c = {
        "occ": OCC,
        "ticker": "TST",
        "type": "call",
        "strike": 100.0,
        "expiry": "2026-12-18",
        "flag_date": "2026-10-09",  # Friday before Columbus Day
        "baseline_oi": 1000,
        "flow_vol": 10000,
        "peak_oi": 1000,
        "series": [],
        "status": "PENDING",
    }
    c.update(kw)
    return {"contracts": {KEY: c}, "schema": fl.SCHEMA}


def _reader(value):
    calls = []

    def read(c):
        calls.append(c["occ"])
        return value

    read.calls = calls
    return read


def test_flag_date_is_the_lists_session_not_the_run_date():
    st = {"contracts": {}}
    # Friday 2026-11-06 list, batch generated 15:55 ET that Friday
    n = fl.register(st, _top("2026-11-06"), _snap("2026-11-06T20:55:00+00:00"))
    assert n == 1
    assert st["contracts"][KEY]["flag_date"] == "2026-11-06"


def test_snapshot_from_another_session_registers_nothing():
    st = {"contracts": {}}
    assert fl.register(st, _top("2026-11-09"), _snap("2026-11-06T20:55:00+00:00")) == 0
    assert st["contracts"] == {}


def test_flow_volume_is_frozen_to_the_flag_day():
    st = {"contracts": {}}
    fl.register(st, _top("2026-11-06"), _snap("2026-11-06T20:55:00+00:00", vol=10000))
    # same-day re-run sees more volume: allowed
    fl.register(st, _top("2026-11-06"), _snap("2026-11-06T20:59:00+00:00", vol=12000))
    assert st["contracts"][KEY]["flow_vol"] == 12000
    # re-flagged Monday with a bigger day: Friday's verdict keeps Friday's volume
    fl.register(st, _top("2026-11-09", rank=1), _snap("2026-11-09T20:55:00+00:00", vol=50000))
    c = st["contracts"][KEY]
    assert c["flow_vol"] == 12000
    assert c["flag_date"] == "2026-11-06"


def test_no_oi_reads_on_a_non_session():
    st = _state(flag_date="2026-11-06")
    read = _reader(7000)
    assert fl.fetch_oi(st, "2026-11-08", read=read) == (0, 0, 0)  # Sunday
    assert read.calls == []
    fl.classify(st, "2026-11-08")
    assert st["contracts"][KEY]["status"] == "PENDING"


def test_unchanged_oi_is_retried_not_closed():
    # Columbus Day: Friday's flow, Monday's vendor OI still the old number
    st = _state()
    assert fl.fetch_oi(st, "2026-10-12", read=_reader(1000)) == (0, 1, 0)
    fl.classify(st, "2026-10-12")
    c = st["contracts"][KEY]
    assert c["status"] == "PENDING"
    assert c["stale_reads"] == ["2026-10-12"]
    # Tuesday the vendor catches up: a real verdict, marked one session late
    fl.fetch_oi(st, "2026-10-13", read=_reader(7000))
    fl.classify(st, "2026-10-13")
    assert c["status"] == "OPEN" and c["retained_pct"] == 60
    assert c["first_read_lag"] == 1
    assert c["series"][0]["asof"] == "2026-10-12"


def test_never_updated_oi_gives_no_verdict():
    st = _state()
    for day in ("2026-10-12", "2026-10-13", "2026-10-14"):
        fl.fetch_oi(st, day, read=_reader(1000))
    fl.classify(st, "2026-10-14")
    c = st["contracts"][KEY]
    assert c["status"] == "NO_DATA"
    assert "first_verdict" not in c
    # and it is never read again
    read = _reader(5000)
    fl.fetch_oi(st, "2026-10-15", read=read)
    assert read.calls == []


def test_on_time_read_and_stats_use_on_time_reads_only():
    st = _state(flag_date="2026-11-06")
    fl.fetch_oi(st, "2026-11-09", read=_reader(7000))  # Monday reads Friday's settlement
    fl.classify(st, "2026-11-09")
    c = st["contracts"][KEY]
    assert c["first_read_lag"] == 0 and c["status"] == "OPEN"
    late = dict(c, first_read_lag=1, occ="O:X")
    st["contracts"]["X|2026-12-18|call|100"] = late
    p = fl.publish(st, "2026-11-09")
    assert p["stats"]["n"] == 1 and p["stats"]["late_excluded"] == 1
    assert p["contracts"][KEY][6] == 0  # first_read_lag published
    assert p["encoding"][6:] == ["first_read_lag", "stale_reads"]


def test_flagged_today_is_not_read():
    st = _state(flag_date="2026-11-10")
    read = _reader(7000)
    fl.fetch_oi(st, "2026-11-10", read=read)
    assert read.calls == []


def test_migrate_cleans_old_state():
    st = {
        "contracts": {
            # registered by a Sunday manual run; Sunday read = old number
            "a": dict(
                _state(flag_date="2026-09-27")["contracts"][KEY],
                series=[{"date": "2026-09-27", "oi": 1000}, {"date": "2026-09-29", "oi": 6000}],
            ),
            # first read equal to the baseline, then a real one 1 session later
            "b": dict(
                _state(flag_date="2026-10-09")["contracts"][KEY],
                series=[{"date": "2026-10-12", "oi": 1000}, {"date": "2026-10-13", "oi": 4000}],
                status="CLOSED",
            ),
            # first read weeks late (API budget starvation): no verdict
            "c": dict(
                _state(flag_date="2026-10-01")["contracts"][KEY],
                series=[{"date": "2026-10-27", "oi": 9000}],
            ),
        }
    }
    fl.migrate(st)
    a, b, c = (st["contracts"][k] for k in "abc")
    assert a["flag_date"] == "2026-09-25"  # the Friday list that run held
    assert [p["date"] for p in a["series"]] == ["2026-09-29"]
    assert a["first_read_lag"] == 1  # 09-29 read shows 09-28's settlement
    assert [p["date"] for p in b["series"]] == ["2026-10-13"] and b["first_read_lag"] == 1
    assert c["series"] == [] and c["no_data"] is True
    fl.classify(st, "2026-11-10")
    assert b["status"] == "PARTIAL"  # was a permanent CLOSED 0%
    assert c["status"] == "NO_DATA"
    assert st["schema"] == fl.SCHEMA
    assert fl.migrate(st) == 0  # runs once


def test_expired_without_verdict_is_unresolved():
    st = _state(expiry="2026-10-16", no_data=True)
    fl.classify(st, "2026-11-10")
    assert st["contracts"][KEY]["status"] == "EXPIRED_UNRESOLVED"
