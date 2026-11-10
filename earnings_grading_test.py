"""Earnings idea grading must measure the actual earnings reaction.

v1 graded every idea on prior close -> event-day close, which for an
after-close (AMC) report is the quiet day BEFORE the print.
"""

import earnings_ideas as ei

# Mon..Fri closes around a Wednesday 2026-10-14 report
ROWS = [
    ("2026-10-12", 100.0),
    ("2026-10-13", 100.0),
    ("2026-10-14", 101.0),  # report day: +1% (pre-report drift)
    ("2026-10-15", 111.1),  # day after: +10% (the AMC reaction)
    ("2026-10-16", 112.0),
    ("2026-10-19", 113.0),
    ("2026-10-20", 114.0),
    ("2026-10-21", 115.0),
    ("2026-10-22", 116.0),
    ("2026-10-23", 117.0),
]


def test_reaction_frame_by_session():
    assert ei._reaction_frame(ROWS, "2026-10-14", "AMC") == (2, 3)
    assert ei._reaction_frame(ROWS, "2026-10-14", "BMO") == (1, 2)
    assert ei._reaction_frame(ROWS, "2026-10-14", None) == (1, 3)  # brackets both
    # an event dated on a weekend reacts on the next session
    assert ei._reaction_frame(ROWS, "2026-10-17", "AMC") == (4, 5)


def _idea(**kw):
    base = {"t": "ABC", "date": "2026-10-14", "type": "vol_rich", "implied": 6.0, "bias": "neutral"}
    base.update(kw)
    return base


def test_amc_vol_rich_is_graded_on_the_next_day(monkeypatch):
    monkeypatch.setattr(ei, "_closes_around", lambda t, ev: ROWS)
    res, mv = ei._grade(_idea(feat={"session": "AMC"}))
    # implied 6% vs the real +10% reaction: the premium seller LOST
    assert res == "loss"
    assert mv == -4.0


def test_bmo_frame_unchanged(monkeypatch):
    monkeypatch.setattr(ei, "_closes_around", lambda t, ev: ROWS)
    res, mv = ei._grade(_idea(feat={"session": "BMO"}))
    assert res == "win" and mv == 5.0  # implied 6 - |+1%|


def test_drift_runs_five_sessions_from_the_reaction_close(monkeypatch):
    monkeypatch.setattr(ei, "_closes_around", lambda t, ev: ROWS)
    res, mv = ei._grade(_idea(type="post_report_drift", bias="bull", feat={"session": "AMC"}))
    # reaction close 111.1 (idx 3) -> +5 sessions = idx 8 (116.0)
    assert res == "win"
    assert mv == round((116.0 / 111.1 - 1) * 100, 2)
