"""Regression tests for the 2026-11 Trade Desk accuracy audit.

Each test pins one statistic the page publishes to the definition it
claims:
  - Research Rank: prior-session regime (no same-day look-ahead),
    close-anchored outcomes only, date-boundary purged folds, measured
    regime-conflict cells, score clamped to 0-100;
  - setup research: promotion checks the SAME tail's folds and a cost
    haircut; tail cuts report the share they really selected;
  - momentum family: candidates from the LATEST swing run, gate on the
    same cohort's de-duplicated, date-clustered record;
  - directional ledger: one position per earnings event and per open
    5-session window, drift graded from the reaction close by the
    earnings loop's own grader, neutral ideas not "tracked", sub-floor
    flow kept as a paper-only shadow for calibration;
  - desk counts: a directional paper edge never lights "Trade
    Qualified"; print-level skips are not candidate rejections.

Synthetic tickers only.
"""

import json
from datetime import UTC, date, datetime, timedelta, timezone

import earnings_ideas as ei
import trade_desk as td
import trade_desk_context as tdc
import trade_desk_research as tdr
import trade_desk_validation as tdv

# ------------------------------------------------ Research Rank inputs


def test_prior_regime_skips_same_day_and_stale_labels():
    regimes = {"2026-07-10": "risk_on", "2026-07-13": "mixed"}  # Fri, Mon
    assert tdv.prior_regime(regimes, "2026-07-13") == "risk_on"  # Monday sees Friday
    assert tdv.prior_regime(regimes, "2026-07-14") == "mixed"
    assert tdv.prior_regime(regimes, "2026-07-10") == "unknown"  # nothing before
    assert tdv.prior_regime(regimes, "2026-07-25") == "unknown"  # 12 days stale
    assert tdv.prior_regime({}, "2026-07-14") == "unknown"


def _ledger_sig(i, day, direction="bullish", ticker=None):
    call = direction == "bullish"
    tk = ticker or f"T{i}"
    return {
        "id": f"s{i}",
        "ticker": tk,
        "contract": f"O:{tk}260101{'C' if call else 'P'}00100000",
        "direction": direction,
        "flow_side": "call_buyer" if call else "put_buyer",
        "type": "call" if call else "put",
        "flagged_at": f"{day}T15:00:00+00:00",
        "dte": 30,
        "liquidity": "B",
        "premium": 500_000,
        "signal_type": "sweep",
        "tags": [],
        "trade_score": 70,
    }


REGIME_DOC = {
    "days": [
        {"date": "2026-07-07", "label": "risk_off"},
        {"date": "2026-07-08", "label": "risk_on"},
    ]
}


def test_validation_dataset_uses_close_anchored_excess_only(monkeypatch):
    sigs = [
        _ledger_sig(0, "2026-07-08"),
        _ledger_sig(1, "2026-07-08"),
        _ledger_sig(2, "2026-07-09", "bearish"),
    ]
    cache = {
        "s0": {"returns": {"5": {"excess": 9.0, "exc_c": 1.0}}},
        "s1": {"returns": {"5": {"excess": 4.0}}},  # frozen before exc_c existed
        "s2": {"returns": {"5": {"excess": -3.0, "exc_c": -2.0}}},
    }
    monkeypatch.setattr(tdv, "load_ledger", lambda: sigs)
    monkeypatch.setattr(tdv, "_load", lambda p, d: cache if p == tdv.CACHE_PATH else REGIME_DOC)
    rows, meta = tdv.build_dataset()
    assert [r["y"] for r in rows] == [1.0, 2.0]  # exc_c, direction-signed
    assert meta["dropped_no_close_anchor"] == 1
    assert rows[0]["feats"]["regime"] == "risk_off"  # a 07-08 flag sees 07-07
    assert rows[1]["feats"]["regime"] == "risk_on"


def test_walk_forward_splits_never_share_a_date_and_purge():
    rows = []
    d = date(2026, 5, 18)
    for _ in range(120):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        # 37 rows a day: index-based edges would land mid-day
        rows += [{"date": d.isoformat(), "feats": {}, "y": 0.0}] * 37
        d += timedelta(days=1)
    dev, hold, folds = tdv.walk_forward_splits(rows)
    assert dev[-1]["date"] < hold[0]["date"]
    assert len(folds) == tdv.N_FOLDS
    for _k, train, n_purged, val in folds:
        assert train[-1]["date"] < val[0]["date"]
        gap = date.fromisoformat(val[0]["date"]) - date.fromisoformat(train[-1]["date"])
        assert gap.days >= tdv.EMBARGO_DAYS
        assert n_purged > 0


def _conflict_rows(n_dates):
    rows = []
    for i in range(n_dates):
        day = (date(2026, 8, 1) + timedelta(days=i)).isoformat()
        wobble = 0.5 * ((i % 5) - 2)
        for _ in range(10):
            rows.append(
                {
                    "date": day,
                    "direction": "bullish",
                    "feats": {"regime": "risk_on"},
                    "y": -3.0 + wobble,
                }
            )
            rows.append(
                {
                    "date": day,
                    "direction": "bearish",
                    "feats": {"regime": "risk_off"},
                    "y": 1.0 + wobble,
                }
            )
        for _ in range(5):
            rows.append(
                {"date": day, "direction": "bullish", "feats": {"regime": "unknown"}, "y": 0.0}
            )
    return rows


def test_regime_conflict_flags_only_measured_worse_cells():
    out = tdv.regime_conflict(_conflict_rows(60))
    assert out["conflicts"] == ["risk_on|bullish"]
    assert "unknown|bullish" not in out["cells"]
    assert out["cells"]["risk_on|bullish"]["dates"] == 60
    # a thin record blocks nothing
    assert tdv.regime_conflict(_conflict_rows(10))["conflicts"] == []


def test_research_rank_is_clamped_to_0_100(monkeypatch):
    model = {
        "production_model": {
            "base": 0.0,
            "adj": {"side:put_buy": -5.0, "side:call_buy": 5.0},
            "scale_anchors": [round(-1 + 2 * i / 20, 3) for i in range(21)],
        }
    }
    monkeypatch.setattr(td, "_load", lambda p, d: model if p == td.VALIDATION_PATH else d)
    score, ok = td._score_fn()
    assert ok
    assert score({"side": "put_buy"}) == 0.0  # used to read -200
    assert score({"side": "call_buy"}) == 100.0


# ------------------------------------------------ setup-level research


GOOD_TAIL = {"n": 150, "avg": 1.0, "med": 0.5, "bootstrap": {"ci95": [0.5, 1.5]}}


def test_promotion_checks_the_folds_of_the_same_tail():
    pooled = {
        "top5": {"n": 150, "avg": -0.2, "med": -0.1, "bootstrap": {"ci95": [-1.0, 0.5]}},
        "top2": GOOD_TAIL,
    }
    hold = {"top2": {"avg": 0.4}}
    # top-5% folds positive, top-2% folds negative -> top-2% must NOT promote
    bad = {"top5": [1.0, 1.0, 1.0], "top2": [-0.5, -0.3, 0.2]}
    assert tdr.promoted(pooled, hold, bad)["promoted"] is False
    ok = {"top5": [-1.0, -1.0, -1.0], "top2": [0.3, 0.4, -0.1]}
    v = tdr.promoted(pooled, hold, ok)
    assert v["promoted"] is True and v["tail_pct"] == 2


def test_promotion_ci_must_clear_round_trip_cost():
    tail = dict(GOOD_TAIL, bootstrap={"ci95": [0.1, 1.5]})  # above 0, below cost
    assert tdr.PROMO["ci_low_above"] == tdv.STOCK_RT_COST_PP
    assert tdr.promoted({"top5": tail}, {"top5": {"avg": 0.4}}, {"top5": [1, 1, 1]}) == {
        "promoted": False
    }


def test_tail_share_reports_what_the_cut_really_selected():
    train_preds = list(range(100))  # train top-5% cut = 95
    val = [(96, 1.0)] + [(10, 0.0)] * 39  # the cut takes 1 of 40
    t = tdr.tail_table(train_preds, val)
    assert t["top5"]["n"] == 1
    assert t["top5"]["share_pct"] == 2.5


def test_research_setups_use_prior_regime_and_close_anchor(monkeypatch):
    sigs = [_ledger_sig(0, "2026-07-08", ticker="AAA"), _ledger_sig(1, "2026-07-08", ticker="BBB")]
    cache = {
        "s0": {"returns": {"5": {"excess": 9.0, "exc_c": 1.5}}},
        "s1": {"returns": {"5": {"excess": 4.0}}},
    }
    monkeypatch.setattr(tdr, "load_ledger", lambda: sigs)
    monkeypatch.setattr(tdr, "_load", lambda p, d: cache)
    monkeypatch.setattr(tdr, "_regime_map", lambda: {"2026-07-07": "risk_off", "2026-07-08": "x"})
    setups, meta = tdr.build_setups()
    graded = [s for s in setups if s["y5"] is not None]
    assert [s["ticker"] for s in graded] == ["AAA"]
    assert graded[0]["y5"] == 1.5
    assert graded[0]["feats"]["regime"] == "risk_off"
    assert meta["dropped_no_close_anchor"] == 1


# ------------------------------------------------------- momentum family


def _card(t):
    return {"t": t, "chg": 1.0, "ext": 1.0, "rvol": 1.0, "sec": "Tech", "spark": [1, 2]}


def _swing_summary(now):
    prior = {"date": "2026-11-06", "grades": {"A+": [_card("OLD")]}}
    latest = {
        "date": "2026-11-09",
        "grades": {
            "A+": [_card(f"P{i}") for i in range(7)],
            "A": [_card("Q1")],
            "B": [_card("NOPE")],
        },
    }
    return {"updated": now.isoformat(), "runs": [prior, latest]}


def test_momentum_candidates_read_latest_run_grades(monkeypatch):
    now = datetime.now(UTC)
    monkeypatch.setattr(
        td,
        "_load",
        lambda p, d: _swing_summary(now) if p.endswith("swing_latest_summary.json") else d,
    )
    out = td._momentum_candidates(now)
    assert [c["ticker"] for c in out] == ["P0", "P1", "P2", "P3", "P4", "Q1"]
    assert all(c["direction"] == "bullish" and c["family"] == "momentum" for c in out)
    assert "spark" not in out[0]["row"] and out[0]["row"]["swing_date"] == "2026-11-09"


def test_context_logs_latest_swing_grade(monkeypatch):
    monkeypatch.setattr(tdc, "_load", lambda p, d: _swing_summary(datetime.now(UTC)))
    g = tdc._swing_grades()
    assert g.get("P0") == "A+" and "OLD" not in g


def test_context_logs_prior_session_regime(monkeypatch):
    now = datetime(2026, 7, 8, 15, tzinfo=UTC)
    scored = {"signals": [_ledger_sig(0, "2026-07-08", ticker="AAA")]}
    monkeypatch.setattr(
        tdc, "_load", lambda p, d: scored if p.endswith("uoa_signals_scored.json") else d
    )
    monkeypatch.setattr(tdc, "_regime_map", lambda: {"2026-07-07": "risk_off", "2026-07-08": "x"})
    rows = tdc.build_rows(now)
    assert rows and rows[0]["market_regime"] == "risk_off"


def test_momentum_record_dedups_repeat_picks():
    days = []
    d = date(2026, 10, 5)  # Monday
    for _ in range(5):
        days.append(
            {"date": d.isoformat(), "rows": [{"t": "AAA", "grade": "A", "dir": "bull", "ret": 1.0}]}
        )
        d += timedelta(days=1)
    rec = td._momentum_record({"days": days})
    assert rec["n"] == 1 and rec["duplicates_excluded"] == 4


def test_momentum_gate_without_log_never_qualifies(monkeypatch):
    agg = {"grades": {"A+": {"status": "active", "n": 200, "avg": 2.0}}}

    def fake(p, d):
        if p.endswith("setup_outcomes_log.json"):
            return None
        if p.endswith("setup_outcomes.json"):
            return agg
        return d

    monkeypatch.setattr(td, "_load", fake)
    g = td._momentum_gate()
    assert g["verdict"] == "watch" and "cannot qualify" in g["why"]


# ------------------------------------------------- earnings gates


def _gate_for(monkeypatch, st):
    monkeypatch.setattr(
        td,
        "_load",
        lambda p, d: (
            {"by_type": {"post_report_drift": st}} if p.endswith("earnings_ideas.json") else d
        ),
    )
    return td.family_gates()["earnings"]["types"]["post_report_drift"]


def test_earnings_directional_gate_needs_ci_above_cost(monkeypatch):
    base = {"status": "active", "n": 120, "win_rate": 55}
    assert _gate_for(monkeypatch, dict(base, ev=0.5, ci95=[0.05, 0.95]))["verdict"] == "watch"
    assert _gate_for(monkeypatch, dict(base, ev=0.6, ci95=[0.3, 0.9]))["verdict"] == "qualified"
    assert _gate_for(monkeypatch, dict(base, ev=-0.45, ci95=[-1.2, 0.3]))["verdict"] == "degraded"
    # positive but not past the round trip -> degraded net of cost
    assert _gate_for(monkeypatch, dict(base, ev=0.1, ci95=[-0.5, 0.7]))["verdict"] == "degraded"
    g = _gate_for(monkeypatch, dict(base, ev=0.5, ci95=None))
    assert g["verdict"] == "watch" and "not vs SPY" in g["why"]


# --------------------------------------------------- directional ledger


def _drift_idea(tk="AAA", ev="2026-10-14", session="AMC", status="EXPERIMENTAL"):
    return {
        "family": "earnings",
        "ticker": tk,
        "direction": "bullish",
        "status": status,
        "alpha_score": None,
        "construct": {"event_date": ev, "session": session},
        "evidence": {"etype": "post_report_drift"},
    }


def _lines(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def test_drift_idea_frozen_once_per_event(tmp_path, monkeypatch):
    lp = str(tmp_path / "ledger.jsonl")
    monkeypatch.setattr(td, "LEDGER_PATH", lp)
    d1 = datetime(2026, 10, 12, 15, tzinfo=UTC)
    for k in range(3):  # shown three days running before the print
        td.freeze_ideas([_drift_idea()], d1 + timedelta(days=k))
    lines = _lines(lp)
    assert len(lines) == 1
    assert lines[0]["id"] == "earnings:post_report_drift:AAA:bullish:2026-10-14"


def _flow(tk="AAA", status="WATCH", score=85):
    return {
        "family": "flow",
        "ticker": tk,
        "direction": "bullish",
        "status": status,
        "alpha_score": score,
    }


def test_flow_repeat_inside_open_window_not_refrozen(tmp_path, monkeypatch):
    lp = str(tmp_path / "ledger.jsonl")
    monkeypatch.setattr(td, "LEDGER_PATH", lp)
    mon = datetime(2026, 10, 5, 15, tzinfo=UTC)
    for k in range(5):  # Mon..Fri: the Monday position is still open
        td.freeze_ideas([_flow()], mon + timedelta(days=k))
    assert len(_lines(lp)) == 1
    td.freeze_ideas([_flow()], mon + timedelta(days=7))  # 5 sessions later
    assert len(_lines(lp)) == 2


def test_shadow_never_double_counts_a_displayed_position(tmp_path, monkeypatch):
    lp = str(tmp_path / "ledger.jsonl")
    monkeypatch.setattr(td, "LEDGER_PATH", lp)
    mon = datetime(2026, 10, 5, 15, tzinfo=UTC)
    td.freeze_ideas([_flow(status="SHADOW", score=60)], mon)
    td.freeze_ideas([_flow()], mon + timedelta(days=1))  # displayed: still frozen
    td.freeze_ideas([_flow(status="SHADOW", score=60)], mon + timedelta(days=2))  # blocked
    assert [x["idea"]["status"] for x in _lines(lp)] == ["SHADOW", "WATCH"]


def _issued(iid, idea, day):
    return {"ev": "issued", "id": iid, "as_of": f"{day}T15:00:00+00:00", "idea": idea}


def test_performance_dedups_legacy_repeats_and_excludes_neutral():
    vol = dict(_drift_idea(), direction="neutral", evidence={"etype": "vol_rich"})
    events = []
    for day in ("2026-10-05", "2026-10-06", "2026-10-07"):  # legacy per-day ids
        events.append(_issued(f"flow:AAA:bullish:{day}", _flow(), day))
        events.append(_issued(f"earnings:AAA:bullish:{day}", _drift_idea(), day))
        events.append(_issued(f"earnings:AAA:neutral:{day}", vol, day))
    events.append(_issued("flow:BBB:bullish:2026-10-05", _flow("BBB"), "2026-10-05"))
    perf = td.performance(events)
    assert perf["tracked"] == 3  # AAA flow, BBB flow, one drift event
    assert perf["duplicates_excluded"] == 4
    assert perf["neutral_frozen"] == 1
    assert perf["by_family"]["earnings:post_report_drift"]["tracked"] == 1


ROWS = [  # Mon..Fri closes around a Wednesday 2026-10-14 AMC report
    ("2026-10-12", 100.0),
    ("2026-10-13", 100.0),
    ("2026-10-14", 101.0),
    ("2026-10-15", 111.1),  # the AMC reaction
    ("2026-10-16", 112.0),
    ("2026-10-19", 113.0),
    ("2026-10-20", 114.0),
    ("2026-10-21", 115.0),
    ("2026-10-22", 116.0),
    ("2026-10-23", 117.0),
]


def test_drift_graded_from_reaction_close_by_the_gate_grader(tmp_path, monkeypatch):
    lp = str(tmp_path / "ledger.jsonl")
    monkeypatch.setattr(td, "LEDGER_PATH", lp)
    monkeypatch.setattr(ei, "_closes_around", lambda t, ev: ROWS)
    iid = "earnings:AAA:bullish:2026-10-12"  # legacy id, issued before the print
    td._append_ledger(
        [
            _issued(iid, _drift_idea(), "2026-10-12"),
            # old grade: issue-day close -> +5 sessions, includes the reaction
            {"ev": "graded", "id": iid, "y": 13.0},
        ]
    )
    events = td._read_ledger()
    assert td.performance(events)["graded"] == 0  # superseded definition
    events, n = td.grade_ledger(events, datetime(2026, 10, 30, 15, tzinfo=UTC))
    assert n == 1
    expect = round((116.0 / 111.1 - 1) * 100, 2)  # reaction close -> +5 sessions
    _, mv = ei._grade(
        {
            "t": "AAA",
            "date": "2026-10-14",
            "type": "post_report_drift",
            "bias": "bull",
            "session": "AMC",
        }
    )
    assert mv == expect  # one definition: the earnings gate's own grader
    perf = td.performance(events)
    assert perf["graded"] == 1
    fam = perf["by_family"]["earnings:post_report_drift"]
    assert fam["status"] == "accruing" and fam["n"] == 1
    assert [e["y"] for e in events if e.get("gv") == td.GRADE_VERSION] == [expect]


def test_calibration_bands_are_reachable_and_shadow_fills_the_low_band():
    events = []
    for i in range(td.MIN_N):
        day = f"2026-09-{1 + i % 20:02d}"
        for tk, score, status in ((f"S{i}", 55, "SHADOW"), (f"W{i}", 75, "WATCH")):
            iid = f"flow:{tk}:bullish:{day}"
            events.append(_issued(iid, _flow(tk, status, score), day))
            events.append({"ev": "graded", "id": iid, "y": 0.5 if i % 2 else -0.2})
    perf = td.performance(events)
    cal = perf["score_calibration"]
    assert list(cal) == ["90-100", "80-89", "70-79", "<70 (shadow)"]
    assert cal["<70 (shadow)"]["n"] == td.MIN_N and cal["70-79"]["n"] == td.MIN_N
    assert perf["tracked"] == td.MIN_N  # shadow never enters the displayed record
    assert perf["shadow_tracked"] == td.MIN_N


# --------------------------------------------------------- full run


def test_run_counts_directional_apart_and_skips_prints(tmp_path, monkeypatch):
    now = datetime.now(UTC)
    flagged = now.astimezone(timezone(timedelta(hours=-4))).isoformat()

    def sig(tk, typ, side):
        return {
            "ticker": tk,
            "type": typ,
            "flow_side": side,
            "flagged_at": flagged,
            "premium": 600_000,
            "liquidity": "B",
            "contract": f"O:{tk}260101{typ[0].upper()}00100000",
        }

    files = {
        "uoa_signals_scored.json": {
            "signals": [
                sig("AAA", "call", "call_buyer"),
                sig("BBB", "put", "put_buyer"),
                sig("CCC", "call", "call_seller"),
            ]
        },
        "trade_desk_validation.json": {
            "production_model": {
                "base": 0.0,
                "adj": {"side:call_buy": 1.0, "side:put_buy": -1.0},
                "scale_anchors": [round(-1 + 2 * i / 20, 3) for i in range(21)],
            },
            "regime_conflict": {"conflicts": []},
        },
        "trade_desk_research.json": {
            "registry": {"champion": {"name": "setup_additive_v2", "tail_pct": 5}}
        },
        "earnings_ideas.json": {"generated": now.isoformat(), "ideas": [], "by_type": {}},
    }

    def fake(path, default):
        for k, v in files.items():
            if path.endswith(k):
                return v
        return default

    monkeypatch.setattr(td, "_load", fake)
    monkeypatch.setattr(td, "_regime_map", dict)
    lp = str(tmp_path / "ledger.jsonl")
    monkeypatch.setattr(td, "LEDGER_PATH", lp)
    out = td.run(dry=True, do_grade=False)
    c = out["desk_counts"]
    assert c["directional_qualified"] == 1 and c["trade_qualified"] == 0
    assert out["abstention"] is True  # a paper edge is not an executable trade
    assert out["skipped_prints"] == {"NOT_DIRECTIONAL": 1}
    assert "NOT_DIRECTIONAL" not in out["rejections"]
    assert out["rejections"] == {"LOW_ALPHA": 1}
    assert {(x["idea"]["ticker"], x["idea"]["status"]) for x in _lines(lp)} == {
        ("AAA", "QUALIFIED"),
        ("BBB", "SHADOW"),
    }
    assert out["performance"]["tracked"] == 1 and out["performance"]["shadow_tracked"] == 1
    assert out["ledgers"]["executable"]["trade_qualified_strategies"] == 0
    assert "directional setups qualified" in out["abstention_note"]


def test_research_setups_count_each_contract_once_at_its_day_premium(monkeypatch):
    # one contract re-logged by three scans with its CUMULATIVE premium
    a = _ledger_sig(0, "2026-07-08", ticker="AAA")
    b = dict(a, id="s0b", flagged_at="2026-07-08T17:00:00+00:00", premium=700_000)
    c = dict(a, id="s0c", flagged_at="2026-07-08T19:00:00+00:00", premium=900_000)
    monkeypatch.setattr(tdr, "load_ledger", lambda: [a, b, c])
    monkeypatch.setattr(tdr, "_load", lambda p, d: {})
    monkeypatch.setattr(tdr, "_regime_map", dict)
    setups, _ = tdr.build_setups()
    assert len(setups) == 1
    f = setups[0]["feats"]
    assert f["prem"] == ">=250k"  # $900k day total, not $2.1M summed re-logs
    assert f["nprints"] == "1"  # one contract, not three scan rows
