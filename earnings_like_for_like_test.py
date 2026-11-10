"""Like-for-like earnings move comparisons (2026-11 accuracy audit).

The options market's priced move and TickerDesk Fair Move must measure
the same thing: the mean |move| of the report session alone. These tests
pin the arithmetic with worked numbers.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

import earnings_edge as ee
import earnings_ideas as ei
import earnings_move_math as EM
import earnings_vol_engine as eve
import fair_move_lab as fml
import trade_desk as td

# ------------------------------------------------------------- constants


def test_normal_and_fat_tail_constants():
    # E|X| = sqrt(2/pi) sigma for a normal move
    assert EM.MEAN_ABS_PER_SIGMA == pytest.approx(0.797885, abs=1e-6)
    # a FAIRLY priced straddle still beats the realized move this often
    assert EM.NORMAL_FAIR_WIN == pytest.approx(0.5751, abs=1e-4)
    assert EM.LAPLACE_FAIR_WIN == pytest.approx(0.6321, abs=1e-4)


# ------------------------------------------------------ event-only move


def test_straddle_stripped_of_normal_sessions():
    # 10% straddle, 3 normal sessions to expiry, 2% daily vol:
    # sigma_total = 10/0.7979 = 12.533; var 157.08 - 3*4 = 145.08
    # sigma_event = 12.045; mean |move| = 0.7979 * 12.045 = 9.61
    r = EM.event_move(10.0, "straddle", 3, 2.0)
    assert r["implied_event"] == 9.61
    assert r["sigma_event"] == 12.045
    assert r["nonevent_var_share"] == 0.076  # 12 / 157.08


def test_one_sigma_fallback_lands_on_the_same_basis():
    # a 1-sigma IV read of 12.533% IS a 10% straddle: same event move
    one_sigma = 10.0 / EM.MEAN_ABS_PER_SIGMA
    r = EM.event_move(one_sigma, "iv_1sigma", 3, 2.0)
    assert r["straddle_equiv"] == 10.0
    assert r["implied_event"] == 9.61
    # stored raw, the 1-sigma number would have read ~25% richer
    assert one_sigma / 10.0 == pytest.approx(1.2533, abs=1e-4)


def test_no_normal_sessions_means_no_strip():
    assert EM.event_move(10.0, "straddle", 0, None)["implied_event"] == 10.0


def test_normal_days_explaining_the_whole_price_gives_no_event_move():
    r = EM.event_move(3.0, "straddle", 4, 2.0)
    assert r["implied_event"] is None and "no measurable earnings premium" in r["why"]


def test_missing_daily_vol_cannot_strip():
    assert EM.event_move(10.0, "straddle", 2, None)["implied_event"] is None


def test_two_session_frame_adds_one_normal_session():
    # unknown report time is graded close D-1 -> close D+1
    v = EM.frame_move(6.0, 1.0, 1)
    assert v == pytest.approx(
        EM.MEAN_ABS_PER_SIGMA * ((6.0 / EM.MEAN_ABS_PER_SIGMA) ** 2 + 1) ** 0.5
    )
    assert round(v, 2) == 6.05
    assert EM.frame_move(6.0, None, 0) == 6.0


# ----------------------------------------------------- daily volatility


def test_daily_vol_is_rms_and_skips_earnings_days():
    import math

    closes = [100.0]
    for i in range(60):
        closes.append(closes[-1] * math.exp(0.01 if i % 2 == 0 else -0.01))
    assert EM.daily_vol_pct(closes) == 1.0
    jumped = [*closes, closes[-1] * 1.25]  # a 25% earnings gap on the last bar
    assert EM.daily_vol_pct(jumped, exclude_idx={len(jumped) - 1}) == 1.0
    assert EM.daily_vol_pct(closes[:10]) is None  # too few returns
    # a one-off catalyst gap is clipped at 3 robust SDs (3 x 1.4826 x 1% =
    # 4.45%), not counted in full: sqrt((39 x 1 + 4.45^2) / 40) = 1.21%,
    # where the raw RMS would read 3.6%
    assert EM.daily_vol_pct(jumped) == pytest.approx(1.21, abs=0.005)


def test_one_outsized_report_is_flagged():
    assert EM.dominated_by_one([3.0, 2.0, 4.0, 350.0])  # bigger than the rest combined
    assert not EM.dominated_by_one([3.0, 2.0, 4.0, 8.0])
    assert not EM.dominated_by_one([5.0])


# ------------------------------------------------------------- calendar


def test_nonevent_sessions_counts_from_read_to_expiry():
    # read Mon close, BMO Tue, expiry Fri: Tue..Fri = 4 sessions, 1 is the event
    assert EM.nonevent_sessions("2026-11-09", "2026-11-13", "2026-11-10", "BMO") == 3
    # AMC Tue read at Tue's close: Wed (reaction), Thu, Fri
    assert EM.nonevent_sessions("2026-11-10", "2026-11-13", "2026-11-10", "AMC") == 2
    # Thanksgiving: Thu 11-26 is closed, Fri 11-27 is a half-day session
    assert EM.nonevent_sessions("2026-11-24", "2026-11-27", "2026-11-25", "BMO") == 1
    # unknown time: the read must precede D (close D-1 frame)
    assert EM.nonevent_sessions("2026-11-09", "2026-11-13", "2026-11-10", "TBD") == 3
    assert EM.nonevent_sessions("2026-11-10", "2026-11-13", "2026-11-10", "TBD") is None
    # an expiry that does not reach the reaction session is no read at all
    assert EM.nonevent_sessions("2026-11-09", "2026-11-10", "2026-11-10", "AMC") is None


def test_pre_event_rule():
    assert EM.is_pre_event("2026-11-09", "2026-11-10", "BMO")
    assert not EM.is_pre_event("2026-11-10", "2026-11-10", "BMO")
    assert EM.is_pre_event("2026-11-10", "2026-11-10", "AMC")  # 4pm quotes, report after
    assert not EM.is_pre_event("2026-11-11", "2026-11-10", "AMC")
    assert not EM.is_pre_event("2026-11-10", "2026-11-10", "TBD")


def test_assumed_weekly_expiry_for_legacy_reads():
    assert EM.assumed_weekly_expiry("2026-11-10", "BMO") == date(2026, 11, 13)
    # Friday AMC: the next Friday is 6 days after the effective date
    assert EM.assumed_weekly_expiry("2026-11-13", "AMC") is None
    assert EM.assumed_weekly_expiry("2026-11-25", "AMC") == date(2026, 11, 27)
    # Good Friday 2026-04-03 is closed: the weekly expires Thursday
    assert EM.assumed_weekly_expiry("2026-03-31", "BMO") == date(2026, 4, 2)


def test_read_session_date_follows_the_et_session():
    # post-close job: today's close
    assert EM.read_session_date(datetime(2026, 11, 10, 21, 33, tzinfo=UTC)) == date(2026, 11, 10)
    # an intraday read already reflects today: a BMO report today is behind it
    assert EM.read_session_date(datetime(2026, 11, 10, 15, 0, tzinfo=UTC)) == date(2026, 11, 10)
    assert not EM.is_pre_event(
        EM.read_session_date(datetime(2026, 11, 10, 15, 0, tzinfo=UTC)), "2026-11-10", "BMO"
    )
    # before the open: yesterday's close
    assert EM.read_session_date(datetime(2026, 11, 10, 13, 0, tzinfo=UTC)) == date(2026, 11, 9)
    # a late run after midnight UTC still reads the previous ET evening
    assert EM.read_session_date(datetime(2026, 11, 11, 3, 47, tzinfo=UTC)) == date(2026, 11, 10)
    # Saturday: Friday's close
    assert EM.read_session_date(datetime(2026, 11, 14, 15, 0, tzinfo=UTC)) == date(2026, 11, 13)


def test_fair_win_baseline_uses_the_sample_dispersion():
    # ratios scaled to mean 1.0: 2 of 4 fall below
    assert EM.fair_win_baseline([0.5, 0.5, 1.0, 2.0]) == 50.0
    # one blow-up drags the mean up: a fair price "wins" 4 of 5
    assert EM.fair_win_baseline([0.2, 0.4, 0.6, 0.8, 3.0]) == 80.0
    assert EM.fair_win_baseline([]) is None


# ------------------------------------------------------- earnings_edge


def test_fair_move_is_the_mean_and_the_median_is_context():
    s = ee.summarize_moves([1.0, -1.0, 1.0, 9.0], [0.5])
    assert s["fair_move"] == 3.0 and s["realized_avg"] == 3.0
    assert s["realized_med"] == 1.0
    assert ee.summarize_moves([1.0, 2.0, 3.0], []) is None  # < MIN_REPORTS


def test_reaction_moves_are_session_aware():
    dates = [date(2026, 10, d) for d in (12, 13, 14, 15, 16, 19, 20, 21, 22, 23)]
    closes = [100.0, 100.0, 104.0, 99.84, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0]
    bmo = datetime(2026, 10, 14, 7, 0)  # before the open: 13 -> 14 close
    amc = datetime(2026, 10, 14, 16, 30)  # after the close: 14 -> 15 close
    moves, _, idx = ee.reaction_moves(dates, closes, [bmo, amc])
    assert [round(m, 2) for m in moves] == [4.0, -4.0]
    assert idx == [2, 3]


def test_edge_read_strips_normal_days_and_refuses_post_event_reads():
    r = ee.edge_read(10.0, "straddle", "2026-11-13", date(2026, 11, 9), "2026-11-10", "BMO", 2.0)
    assert r["implied"] == 10.0 and r["implied_src"] == "straddle"
    assert r["nonevent_sessions"] == 3 and r["implied_event"] == 9.61
    assert r["implied_asof"] == "2026-11-09"
    late = ee.edge_read(4.0, "straddle", "2026-11-13", date(2026, 11, 10), "2026-11-10", "BMO", 2.0)
    assert late["implied_event"] is None and "after the report" in late["implied_why"]


def test_verdict_compares_like_with_like():
    # raw straddle 10 vs median 6 used to read 1.67x "rich";
    # event-only 9.61 vs mean 8.0 is 1.20x: fairly priced
    v, why = ee.verdict_for(9.61, {"fair_move": 8.0, "realized_med": 6.0}, None)
    assert v == "Watch" and "fairly priced" in why and "median ±6.0%" in why
    v, why = ee.verdict_for(12.0, {"fair_move": 8.0, "realized_med": 6.0}, None)
    assert v == "Avoid" and "(1.5×)" in why
    # an average that is one outsized report makes no rich/cheap call
    s = ee.summarize_moves([3.0, -2.0, 4.0, 350.0], [])
    assert s is not None
    assert s["fair_move_dominated"] and s["max_reaction"] == 350.0
    v, why = ee.verdict_for(16.8, s, None)
    assert v == "Watch" and "dominated by one ±350.0% reaction" in why
    # 9.61 / 89.8 would otherwise read 0.11x "cheap"
    assert ei.generate([_name(**s)], {}, {}) == []


# ------------------------------------------------------- earnings_ideas

READ = {
    "implied": 10.0,
    "implied_src": "straddle",
    "implied_expiry": "2026-11-13",
    "implied_asof": "2026-11-09",
    "nonevent_sessions": 3,
    "daily_vol": 2.0,
    "implied_event": 9.61,
    "fair_move": 8.0,
    "realized_med": 6.0,
    "n_reports": 8,
}


def _name(**kw):
    n = {
        "t": "XYZ",
        "date": "2026-11-10",
        "days": 1,
        "session": "BMO",
        "mcap": 5e9,
        "drift_5d": 0.2,
        **READ,
    }
    n.update(kw)
    return n


def test_rich_only_when_like_for_like_ratio_says_so():
    # old basis: 10 / 6 = 1.67x -> vol_rich. New basis: 9.61 / 8.0 = 1.20x
    assert ei.generate([_name()], {}, {}) == []
    ideas = ei.generate([_name(fair_move=6.5)], {}, {})  # 9.61 / 6.5 = 1.48x
    assert [i["type"] for i in ideas] == ["vol_rich"]
    th = ideas[0]["thesis"]
    assert "±9.6% move for the report itself" in th
    assert "(the ±10.0% straddle to 11/13, less 3 normal sessions)" in th
    assert "±6.5% average reaction" in th and "1.48× rich" in th
    assert ideas[0]["implied_event"] == 9.61 and ideas[0]["implied"] == 10.0


def test_pending_vol_idea_is_graded_on_the_last_pre_event_read():
    e = {"t": "XYZ", "date": "2026-11-12", "type": "vol_rich", "result": "pending"}
    early = dict(READ, implied_asof="2026-11-06", implied_event=9.0, session="AMC")
    assert ei.apply_read(e, early) and e["implied_event"] == 9.0
    last = dict(READ, implied_asof="2026-11-12", implied_event=8.2, session="AMC")
    assert ei.apply_read(e, last) and e["implied_event"] == 8.2  # AMC: D's close is pre-report
    after = dict(READ, implied_asof="2026-11-13", implied_event=3.0, session="AMC")
    assert not ei.apply_read(e, after) and e["implied_event"] == 8.2
    assert e["reads"] == 2 and e["implied_asof"] == "2026-11-12"


def test_vol_idea_without_a_pre_event_read_is_not_logged():
    log = {"ideas": []}
    good = ei.generate([_name(fair_move=6.5)], {}, {})
    late = ei.generate([_name(t="LATE", fair_move=6.5, implied_asof="2026-11-10")], {}, {})
    assert ei.log_new_ideas(log, good + late, "2026-11-09") == 1
    (e,) = log["ideas"]
    assert e["t"] == "XYZ" and e["implied_event"] == 9.61 and e["implied"] == 10.0
    assert e["fair_move"] == 6.5 and e["realized_med"] == 6.0 and e["reads"] == 1


ROWS = [
    ("2026-10-12", 100.0),
    ("2026-10-13", 100.0),
    ("2026-10-14", 101.0),
    ("2026-10-15", 111.1),
    ("2026-10-16", 112.0),
    ("2026-10-19", 113.0),
    ("2026-10-20", 114.0),
    ("2026-10-21", 115.0),
]


def test_unknown_session_is_graded_over_two_sessions(monkeypatch):
    monkeypatch.setattr(ei, "_closes_around", lambda t, ev: ROWS)
    idea = {
        "t": "ABC",
        "date": "2026-10-14",
        "type": "vol_rich",
        "bias": "neutral",
        "implied_event": 6.0,
        "daily_vol": 1.0,
    }
    g = ei._grade_full(idea)
    # close 10-13 (100) -> close 10-15 (111.1): |move| 11.1 over 2 sessions
    assert g["frame_sessions"] == 2 and g["abs_move"] == 11.1
    assert g["implied_graded"] == 6.05  # 6.0 widened by one 1% session
    assert g["mv"] == -5.05 and g["result"] == "loss" and g["basis"] == "exact"


def _legacy_rows(jump_on="2026-10-14", jump=1.05):
    import math

    out, px, d, i = [], 100.0, date(2026, 7, 27), 0
    while d <= date(2026, 11, 6):
        if EM._is_session(d):
            if d.isoformat() == jump_on:
                px *= jump
            elif i:
                px *= math.exp(0.01 if i % 2 else -0.01)
            out.append((d.isoformat(), px))
            i += 1
        d += timedelta(days=1)
    return out


def test_legacy_vol_idea_is_reconstructed_from_its_first_read(monkeypatch):
    rows = _legacy_rows()
    monkeypatch.setattr(ei, "_closes_around", lambda t, ev: rows)
    idea = {
        "t": "OLD",
        "date": "2026-10-14",  # Wednesday, before the open
        "type": "vol_rich",
        "bias": "neutral",
        "implied": 10.0,  # raw straddle, frozen at first logging
        "published": "2026-10-12",  # logged Monday evening
        "feat": {"session": "BMO"},
        "result": "win",
        "gv": 2,
        "mv": 5.0,
    }
    g = ei._grade_full(idea)
    # assumed expiry Fri 10-16; sessions 13,14,15,16 -> k = 3; daily vol 1.0
    exp_ev = EM.event_move(10.0, "straddle", 3, 1.0)["implied_event"]
    assert g["basis"] == "reconstructed" and g["nonevent_sessions"] == 3
    assert g["daily_vol"] == 1.0 and g["implied_expiry"] == "2026-10-16"
    assert g["implied_graded"] == pytest.approx(exp_ev) and round(exp_ev, 1) == 9.9
    assert g["abs_move"] == pytest.approx(5.0, abs=0.01)  # the +5% BMO reaction
    assert g["result"] == "win" and g["mv"] == pytest.approx(exp_ev - 5.0, abs=0.01)


def test_legacy_read_logged_on_report_day_is_void(monkeypatch):
    rows = _legacy_rows()
    monkeypatch.setattr(ei, "_closes_around", lambda t, ev: rows)
    idea = {
        "t": "OLD",
        "date": "2026-10-14",
        "type": "vol_cheap",
        "bias": "neutral",
        "implied": 2.0,  # a tiny post-report straddle that looked "cheap"
        "published": "2026-10-14",
        "feat": {"session": "BMO"},
        "result": "win",
        "gv": 2,
        "mv": 3.0,
    }
    g = ei._grade_full(idea)
    assert g == {"result": "void", "void_why": "first read not provably before the report"}
    log = {"ideas": [idea]}
    assert ei.grade_log(log, "2026-11-01") == (0, 1, 1)
    assert idea["result"] == "void" and idea["gv"] == ei.VOL_GRADE_VERSION
    assert idea["prev_grade"] == {"result": "win", "mv": 3.0, "gv": 2}
    assert "mv" not in idea


def test_vol_stats_carry_the_fair_baseline():
    entries = []
    for k in range(5):
        for a in (2, 4, 6, 8, 10, 12):
            entries.append(
                {
                    "type": "vol_rich",
                    "date": "2026-10-%02d" % (k + 1),
                    "result": "win" if a < 10 else "loss",
                    "gv": ei.VOL_GRADE_VERSION,
                    "mv": 10.0 - a,
                    "abs_move": float(a),
                    "implied_graded": 10.0,
                    "basis": "exact",
                }
            )
    entries.append({"type": "vol_rich", "result": "win", "gv": 2, "mv": 9.0})  # stale v2
    entries.append({"type": "vol_rich", "result": "void", "gv": 3})
    st = ei.type_stats(entries)["vol_rich"]
    assert st["n"] == 30 and st["win_rate"] == 67  # 4 of 6 below 10
    # ratios 0.2..1.2, mean 0.7 -> a fair price would win 3 of 6
    assert st["fair_win_rate"] == 50.0 and st["win_vs_fair_pp"] == 17.0
    assert st["realized_over_implied"] == 0.7
    assert st["n_exact"] == 30 and st["n_void"] == 1
    assert st["fair_win_rate_normal"] == 57.5


# ----------------------------------------------------------- vol engine


def test_engine_reads_the_graded_basis(monkeypatch):
    log = {
        "ideas": [
            {
                "t": "AAA",
                "date": "2026-10-14",
                "type": "vol_rich",
                "result": "win",
                "gv": ei.VOL_GRADE_VERSION,
                "implied": 10.0,
                "implied_graded": 8.0,
                "abs_move": 2.0,
                "mv": 6.0,
                "basis": "exact",
            },
            {  # pre-v3 grade: excluded
                "t": "BBB",
                "date": "2026-10-14",
                "type": "vol_rich",
                "result": "win",
                "gv": 2,
                "implied": 10.0,
                "mv": 8.0,
            },
        ]
    }
    monkeypatch.setattr(eve, "_load", lambda p, d: log if p.endswith("ideas_log.json") else {})
    monkeypatch.setattr(eve, "_regime_map", dict)
    ev = eve._load_events()
    (e,) = ev["vol_rich"]
    assert e["ratio"] == 0.25 and e["implied"] == 8.0 and e["implied_raw"] == 10.0


# ------------------------------------------------------- fair move lab


def test_lab_scores_rmse_mae_bias_and_edge_sign():
    pairs = [
        ({"implied": 10.0, "realized": 5.0}, {"x": 6.0}),
        ({"implied": 10.0, "realized": 9.0}, {"x": 8.0}),
    ]
    s = fml.score_one(pairs, "x")
    assert s == {"n": 2, "rmse": 1.0, "mae": 1.0, "bias": 0.0, "edge_sign_pct": 100}


def test_lab_uses_point_in_time_displayed_figures():
    ctx = {"OLD": [("2026-09-01", 9.9), ("2026-10-08", 4.0), ("2026-10-20", 7.7)]}
    # latest row in [D-10, D]; a row after the report is never used
    assert fml.context_median(ctx, "OLD", "2026-10-14") == 4.0
    assert fml.context_median(ctx, "OLD", "2026-11-30") is None
    events = {
        "vol_rich": [
            {
                "ticker": "NEW",
                "date": "2026-11-12",
                "implied": 9.0,
                "realized": 6.0,
                "basis": "exact",
                "fair_move": 7.0,
                "realized_med": 5.0,
                "n_reports": 8,
                "frame_sessions": 1,
            },
            {
                "ticker": "OLD",
                "date": "2026-10-14",
                "implied": 9.0,
                "realized": 3.0,
                "basis": "reconstructed",
                "frame_sessions": 1,
            },
            {
                "ticker": "TBD",
                "date": "2026-10-15",
                "implied": 9.0,
                "realized": 3.0,
                "frame_sessions": 2,
            },
        ],
        "vol_cheap": [],
    }
    rows, excluded = fml.build_rows(events, ctx)
    assert excluded == 1
    by = {r["ticker"]: r for r in rows}
    assert by["NEW"]["fair_mean"] == 7.0 and by["NEW"]["median_src"] == "logged"
    assert by["OLD"]["fair_mean"] is None and by["OLD"]["fair_median"] == 4.0
    assert by["OLD"]["median_src"] == "setup_context"
    out = fml.run(dry=True, events=events, ctx=ctx)
    assert out["display_definition"] == "fair_mean"
    assert out["active"]["n"] == 1 and out["active"]["status"] == "accruing"
    assert out["previous_display"]["n"] == 2
    assert out["previous_display"]["sources"] == {"logged": 1, "setup_context": 1}
    assert out["status"] == "insufficient_history"


# ------------------------------------------------------------ trade desk


def test_construct_uses_the_like_for_like_pair(monkeypatch):
    monkeypatch.setattr(td, "_load", lambda p, d: {})
    idea = {**READ, "implied_event": 6.0, "fair_move": 8.0, "t": "XYZ", "date": "2026-11-12"}
    c = {"family": "earnings", "ticker": "XYZ", "etype": "vol_cheap", "idea": idea}
    out = td.construct(c)
    assert out["implied_move"] == 6.0 and out["implied_raw"] == 10.0
    assert out["fair_move"] == 8.0 and out["realized_median"] == 6.0
    assert out["vol_edge_pp"] == -2.0 and out["market_fair_ratio"] == 0.75
    assert out["vol_stance"] == "BUY VOL" and out["analogues"] == 8
