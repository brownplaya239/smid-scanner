"""The redesigned Daily Brief must actually reach subscribers.

From early October to mid November 2026 the new layout failed its own
pre-send checks on about 31 of 35 mornings, so every subscriber got the old
fallback email. The causes, each reproduced below:

- calendar checks ran on the WHOLE week's file, so a Fed speech or a release
  listed a minute off on another day blocked today's email;
- a release that was already out (the feed has no actuals) was labelled
  COMPLETED, which the gate then rejected;
- an options print the scanner tagged side "unknown" was labelled
  "UNSPECIFIED", which the gate read as a missing field.

Each of those now costs the one line it affects, not the whole email, and a
real conversion bug still blocks.
"""

import json
from datetime import datetime

import pytest

import brief_compose as BC
import brief_model as BM
import daily_brief as DB
import market_layer as ML

NOW = ML.ET.localize(datetime(2026, 11, 16, 8, 45))
TODAY = "2026-11-16"


def _ev(title, hhmm, date=TODAY, **extra):
    h, m = hhmm
    e = {
        "title": title,
        "date": date,
        "time_et": "",
        "starts_at": "%sT%02d:%02d-05:00" % (date, h, m),
        "source_time": "%d:%02d%s" % (((h + 5) % 12) or 12, m, "pm" if h + 5 >= 12 else "am"),
        "source_tz": "UTC",
        "forecast": "",
        "previous": "",
        "actual": "",
        "generic_title": False,
        "title_authority": "vendor",
    }
    e.update(extra)
    return e


@pytest.fixture
def calendar(tmp_path, monkeypatch):
    """Write an economic_calendar.json and point market_layer at it."""
    monkeypatch.setattr(ML, "REPORTS_DIR", str(tmp_path))

    def write(events):
        (tmp_path / "economic_calendar.json").write_text(
            json.dumps({"converted": True, "events": events}), encoding="utf-8"
        )

    return write


# ── calendar ────────────────────────────────────────────────────────────


def test_other_days_never_block_today(calendar):
    calendar(
        [
            _ev("FOMC Member Bowman Speaks", (8, 40), date="2026-11-10", generic_title=True),
            _ev("Bank Holiday", (0, 0), date="2026-11-11", generic_title=True),
            _ev("Crude Oil Inventories", (10, 30), anchor_et="10:30", on_schedule=True),
        ]
    )
    assert ML.calendar_problems(NOW) == []
    assert [e["title"] for e in ML.load_events(NOW)] == ["Crude Oil Inventories"]


def test_unverifiable_events_today_are_left_out_not_blocking(calendar):
    calendar(
        [
            _ev("FOMC Member Waller Speaks", (10, 0), generic_title=True),
            _ev("ADP Weekly Employment Change", (8, 14), anchor_et="08:15", on_schedule=False),
            # out at 7:30, more than 30 minutes ago, and the feed has no actual
            _ev("Challenger Job Cuts y/y", (7, 30), previous="-26.8%"),
            _ev("CPI m/m", (8, 30), forecast="0.3%", anchor_et="08:30", on_schedule=True),
            _ev("Crude Oil Inventories", (10, 30), anchor_et="10:30", on_schedule=True),
        ]
    )
    left_out = []
    shown = ML.load_events(NOW, left_out=left_out)
    assert {e["title"] for e in shown} == {"CPI m/m", "Crude Oil Inventories"}
    assert {x["title"] for x in left_out} == {
        "FOMC Member Waller Speaks",
        "ADP Weekly Employment Change",
        "Challenger Job Cuts y/y",
    }
    assert all(x["why"] for x in left_out)
    assert ML.calendar_problems(NOW) == []
    # and the send gate is happy with what is displayed
    assert BC.check_temporal([], shown, "2026-11-16 08:45 ET") == []


def test_a_finished_non_release_still_shows(calendar):
    calendar([_ev("State Arrival Ceremony", (7, 0), title_authority="override")])
    assert [e["status"] for e in ML.load_events(NOW)] == [ML.COMPLETED]


def test_a_systematic_time_shift_still_blocks(calendar):
    calendar(
        [
            _ev("CPI m/m", (9, 30), anchor_et="08:30", on_schedule=False),
            _ev("Core CPI m/m", (9, 30), anchor_et="08:30", on_schedule=False),
            _ev("Crude Oil Inventories", (10, 30), anchor_et="10:30", on_schedule=True),
        ]
    )
    problems = ML.calendar_problems(NOW)
    assert len(problems) == 1
    assert "2 of 3" in problems[0]


def test_an_unconverted_calendar_still_blocks(calendar, tmp_path):
    (tmp_path / "economic_calendar.json").write_text(
        json.dumps({"events": [_ev("CPI m/m", (8, 30))]}), encoding="utf-8"
    )
    assert ML.calendar_problems(NOW)


# ── options-flow sides ──────────────────────────────────────────────────


def _row(side, strike=100):
    return {
        "ticker": "BE",
        "right": "call",
        "strike": strike,
        "expiry": "2026-11-20",
        "side": side,
        "premium": "$1.0M",
        "printed_at": "2026-11-13T20:00:00Z",
    }


def test_unknown_side_is_stated_not_blocking(check):
    rec = BM.contract_record(_row("unknown"))
    check("unknown side is labelled", rec["action"] == "SIDE UNKNOWN", rec["action"])
    check("unknown side has no direction", rec["direction"] == BM.DIR_NONE, rec["direction"])
    model = {
        "sections": [
            {"id": "watchlist", "records": []},
            {"id": "flow_driving", "records": [{"ticker": "BE", "contracts": [rec]}]},
        ]
    }
    check(
        "the gate accepts a row that says its side is unknown",
        not [p for p in BC.check_model(model) if "buy/sell side" in p],
        BC.check_model(model),
    )
    rec["action"] = ""
    check(
        "a row with no side at all is still blocked",
        any("buy/sell side" in p for p in BC.check_model(model)),
    )


def test_known_sides_are_unchanged(check):
    for side, action in (
        ("call_buyer", "BUY"),
        ("put_seller", "SELL"),
        ("mixed", "TWO-SIDED"),
    ):
        got = BM.contract_record(_row(side))["action"]
        check(side, got == action, got)


# ── flow sentences ──────────────────────────────────────────────────────


def test_no_vote_sentences_read_as_english(check):
    one_amb = BC.classify_flow([_row("mixed")])["explain"]
    check(
        "one unresolved print",
        one_amb == "The tape could not resolve the one print to a buyer or seller.",
        one_amb,
    )
    three_unknown = BC.classify_flow([_row("unknown", s) for s in (1, 2, 3)])["explain"]
    check(
        "three side-less prints",
        three_unknown == "None of the three prints arrived with a side.",
        three_unknown,
    )
    both = BC.classify_flow([_row("mixed", 1), _row("unknown", 2)])["explain"]
    check("mixed reasons", both.startswith("No print carries a direction, "), both)
    for s in (one_amb, three_unknown, both):
        check("no 'Only no of'", "Only no" not in s, s)


# ── OI follow-through date ──────────────────────────────────────────────


@pytest.mark.parametrize("day,shown", [("2026-11-13", "Nov 13"), ("2026-07-21", "Jul 21")])
def test_oi_pending_line_names_the_right_session(day, shown):
    import oi_pipeline as OP

    line = OP.email_section({}, datetime.strptime(day, "%Y-%m-%d").date())["pending_line"]
    assert line.startswith(shown + " end-of-day OI")


# ── the alert ───────────────────────────────────────────────────────────


def test_fallback_alert_counts_and_dedupes():
    subject, body = DB.fallback_alert(["reason A", "reason A", "reason B"], 3, TODAY)
    assert "3 of 3" in subject
    assert body.count("reason A") == 1
    assert "reason B" in body


def test_report_fallbacks_without_alert_email_only_warns(monkeypatch, capsys):
    monkeypatch.setattr(DB, "ALERT_EMAIL", "")
    sent = []
    monkeypatch.setattr(DB, "send_email", lambda *a, **k: sent.append(a) or (True, ""))
    DB.report_fallbacks(["x"], 1, TODAY)
    out = capsys.readouterr().out
    assert out.startswith("::warning title=Daily Brief used the old layout::")
    assert sent == []


def test_report_fallbacks_emails_the_operator(monkeypatch):
    monkeypatch.setattr(DB, "ALERT_EMAIL", "ops@example.com")
    sent = []
    monkeypatch.setattr(DB, "send_email", lambda *a, **k: sent.append(a) or (True, ""))
    DB.report_fallbacks(["x"], 2, TODAY)
    assert sent and sent[0][0] == "ops@example.com"
    assert "1 of 2" in sent[0][1]
