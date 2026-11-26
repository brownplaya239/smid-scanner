"""The UOA freshness monitor and the data-quality audit know market holidays.

Thanksgiving 2026 (2026-11-26) paged twice: the freshness check sent an
"upstream empty" email because the worker ran flow scans on a closed market,
and dq_audit failed the job because it measured freshness against a session
that did not exist (and read the winter clock an hour fast).
"""

import os
import sys
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))

import dq_audit
import uoa_freshness_check as fc

ET = ZoneInfo("America/New_York")


def et(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


# --- uoa_freshness_check -------------------------------------------------


@pytest.mark.parametrize(
    ("now", "want"),
    [
        (et(2026, 11, 25, 12, 15), (585, 990)),  # normal day: 9:45-16:30
        (et(2026, 11, 26, 12, 15), None),  # Thanksgiving
        (et(2026, 11, 27, 12, 15), (585, 810)),  # half day: 9:45-13:30
        (et(2026, 11, 28, 12, 15), None),  # Saturday
        (et(2031, 3, 5, 12, 15), (585, 990)),  # year the calendar lacks: weekday rules
        (et(2031, 3, 8, 12, 15), None),
    ],
)
def test_session_window(now, want):
    assert fc.session_window(now) == want


def test_monitor_skips_thanksgiving_without_fetching_or_emailing(monkeypatch):
    monkeypatch.setattr(fc, "et_now", lambda: et(2026, 11, 26, 15, 45))

    def boom(*a, **k):
        raise AssertionError("should not run on a holiday")

    monkeypatch.setattr(fc.urllib.request, "urlopen", boom)
    monkeypatch.setattr(fc, "send_alert", boom)
    assert fc.main() == 0


def test_monitor_skips_after_a_half_day_close(monkeypatch):
    monkeypatch.setattr(fc, "et_now", lambda: et(2026, 11, 27, 15, 45))

    def boom(*a, **k):
        raise AssertionError("should not run after the 13:00 close")

    monkeypatch.setattr(fc.urllib.request, "urlopen", boom)
    monkeypatch.setattr(fc, "send_alert", boom)
    assert fc.main() == 0


# --- dq_audit ------------------------------------------------------------


def at(monkeypatch, utc):
    monkeypatch.setattr(dq_audit, "NOW", utc)
    monkeypatch.setattr(dq_audit, "RESULTS", [])


def test_audit_reads_winter_time(monkeypatch):
    at(monkeypatch, datetime(2026, 11, 26, 17, 15, tzinfo=UTC))
    n = dq_audit.NOW.astimezone(dq_audit.ET)
    assert (n.hour, n.minute) == (12, 15)  # EST, not EDT


def test_thanksgiving_measures_against_wednesday(monkeypatch):
    at(monkeypatch, datetime(2026, 11, 26, 17, 15, tzinfo=UTC))
    assert dq_audit.last_rth_batch() == et(2026, 11, 25, 15, 55)
    assert dq_audit.freshness_limit("intraday") == et(2026, 11, 25, 15, 25)
    assert dq_audit.freshness_limit("daily") == et(2026, 11, 25, 9, 0)
    # Wednesday 18:45 ET: the last real batch before the holiday
    assert dq_audit.ts_of({"generated": "2026-11-25T23:45:00Z"}) >= dq_audit.freshness_limit(
        "intraday"
    )


def test_thanksgiving_signals_check_uses_wednesday(monkeypatch):
    at(monkeypatch, datetime(2026, 11, 26, 17, 15, tzinfo=UTC))
    sig = [{"flagged_at": "2026-11-25T15:00:00+00:00"}] * 30
    dq_audit.audit_xref({"uoa_signals_scored": {"signals": sig}})
    got = [r for r in dq_audit.RESULTS if r[2] == "signals last session"]
    assert got == [("PASS", "XREF", "signals last session", "2026-11-25: 30 rows")]


def test_half_day_last_batch_is_before_the_1pm_close(monkeypatch):
    at(monkeypatch, datetime(2026, 11, 27, 20, 0, tzinfo=UTC))  # Fri 15:00 ET
    assert dq_audit.last_rth_batch() == et(2026, 11, 27, 12, 55)


def test_normal_day_inside_rth_is_unchanged(monkeypatch):
    at(monkeypatch, datetime(2026, 11, 25, 17, 15, tzinfo=UTC))  # Wed 12:15 ET
    assert dq_audit.last_rth_batch() == et(2026, 11, 25, 11, 0)


def test_monday_before_open_looks_back_to_friday(monkeypatch):
    at(monkeypatch, datetime(2026, 11, 30, 13, 0, tzinfo=UTC))  # Mon 8:00 ET
    assert dq_audit.last_rth_batch() == et(2026, 11, 27, 12, 55)  # Friday half day
