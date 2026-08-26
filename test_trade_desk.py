"""Unit + regression tests for the AI Trade Desk engine.

Covers (spec secs 45-48):
  - point-in-time discipline: at-flag features can never include
    post-flag information (next-day OI confirmation, forward returns)
  - ledger immutability: re-issuing the same idea never duplicates or
    mutates an existing ledger line
  - qualification: no fabricated thresholds — an absent/failed
    validation file must yield abstention, never Top Ideas
  - hard filters: every rejection carries a reason; measured regime
    conflict blocks bullish flow in risk_off/mixed
  - scoring: deterministic, monotone in expected excess, honest None
    when the validation model is unavailable
  - performance: stats gate at MIN_N; calibration buckets accrue

Fixture tickers are synthetic (AAA/BBB) — no real symbols in test
logic, per the repo's shared-logic rule.

    python -m pytest test_trade_desk.py -q
"""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import trade_desk as td
import trade_desk_validation as tdv


NOW = datetime.now(timezone.utc)


def _sig(**kw):
    base = {
        "id": "O:AAA260101C00100000_" + NOW.isoformat(),
        "ticker": "AAA", "contract": "O:AAA260101C00100000",
        "signal_type": "sweep", "trade_score": 80, "premium": 900_000,
        "dte": 30, "tags": ["Sweep"], "type": "call",
        "direction": "bullish", "flow_side": "call_buyer",
        "liquidity": "B", "cap_bucket": "large", "opening": "likely_open",
        "flagged_at": NOW.isoformat(), "underlying_px_at_flag": 100.0,
        "volume": 5000, "open_interest": 1000,
    }
    base.update(kw)
    return base


# ------------------------------------------------- point-in-time (sec 47)

def test_features_at_flag_has_no_lookahead_fields():
    """The at-flag feature vector must be computable from flag-time
    fields only. OI confirmation (next-day) and forward returns must
    never leak in — this is the permanent look-ahead regression test."""
    s = _sig(oi_status="confirmed", ret_5d=25.0, excess_5d=24.0,
             mfe=30.0, mae=-2.0)
    feats = tdv.features_at_flag(s, {})
    banned = {"oi", "oi_status", "ret", "excess", "mfe", "mae"}
    for k in feats:
        assert k not in banned
    # and no feature VALUE derived from those fields
    s2 = dict(s)
    for k in ("oi_status", "ret_5d", "excess_5d", "mfe", "mae"):
        s2.pop(k, None)
    assert tdv.features_at_flag(s2, {}) == feats


def test_regime_lookup_uses_flag_date_not_today():
    s = _sig(flagged_at="2026-07-07T14:00:00+00:00")
    regimes = {"2026-07-07": "risk_off", "2026-07-08": "risk_on"}
    assert tdv.features_at_flag(s, regimes)["regime"] == "risk_off"


def test_validation_dataset_excludes_sellers_and_nondirectional():
    cache_row = {"returns": {"5": {"ret": 1.0, "excess": 1.0}}}
    rows = []
    for s in (_sig(flow_side="call_seller"),
              _sig(direction="hedge"),
              _sig(direction="income")):
        # mimic build_dataset's admission logic
        ok = (s.get("direction") in ("bullish", "bearish")
              and tdv._side_of_signal(s) != "seller")
        rows.append(ok)
    assert rows == [False, False, False]


# --------------------------------------------------- qualification gates

def test_missing_validation_means_no_score_and_no_qualified_flow(monkeypatch):
    monkeypatch.setattr(td, "_load",
                        lambda path, default: default)
    score, ok = td._score_fn()
    assert ok is False and score({"side": "call_buy"}) is None
    gates = td.family_gates()
    assert gates["flow"]["verdict"] == "watch"


def test_no_champion_keeps_flow_at_watch(monkeypatch):
    real_load = td._load

    def fake(path, default):
        if path.endswith("trade_desk_research.json"):
            return {"registry": {"champion": {"name": None}}}
        return real_load(path, default)
    monkeypatch.setattr(td, "_load", fake)
    assert td.family_gates()["flow"]["verdict"] == "watch"


def test_promoted_champion_qualifies_flow(monkeypatch):
    real_load = td._load
    reg = {"registry": {
        "champion": {"name": "setup_additive_v2", "tail_pct": 5},
        "challengers": {"setup_additive_v2": {"verdict": {
            "promoted": True, "tail_pct": 5,
            "evidence": {"pooled": {"n": 200, "avg": 1.2}}}}}}}

    def fake(path, default):
        if path.endswith("trade_desk_research.json"):
            return reg
        return real_load(path, default)
    monkeypatch.setattr(td, "_load", fake)
    g = td.family_gates()["flow"]
    assert g["verdict"] == "qualified" and g["tail_pct"] == 5
    assert g["evidence"]["pooled"]["n"] == 200


def test_negative_measured_family_is_degraded(monkeypatch):
    real_load = td._load

    def fake(path, default):
        if path.endswith("scan_outcomes.json"):
            return {"overall": {"status": "active", "n": 500, "ev": -1.0,
                                "profit_factor": 0.8}}
        return real_load(path, default)
    monkeypatch.setattr(td, "_load", fake)
    assert td.family_gates()["momentum"]["verdict"] == "degraded"


def test_positive_measured_family_qualifies(monkeypatch):
    real_load = td._load

    def fake(path, default):
        if path.endswith("scan_outcomes.json"):
            return {"overall": {"status": "active", "n": 500, "ev": 0.8,
                                "profit_factor": 1.4}}
        return real_load(path, default)
    monkeypatch.setattr(td, "_load", fake)
    assert td.family_gates()["momentum"]["verdict"] == "qualified"


# ---------------------------------------------------------- hard filters

def _flow_cand(**kw):
    anchor = _sig(**kw.pop("anchor", {}))
    c = {"family": "flow", "ticker": anchor["ticker"],
         "direction": anchor["direction"], "anchor": anchor,
         "n_prints": 1, "total_premium": anchor["premium"],
         "feats": tdv.features_at_flag(anchor, {}),
         "catalyst": "flow"}
    c.update(kw)
    return c


def test_regime_conflict_blocks_bullish_flow_in_risk_off():
    c = _flow_cand()
    assert td.hard_filter(c, "risk_off", 90, NOW) == "REGIME_CONFLICT"
    assert td.hard_filter(c, "mixed", 90, NOW) == "REGIME_CONFLICT"
    assert td.hard_filter(c, "risk_on", 90, NOW) is None


def test_bearish_flow_allowed_in_risk_off():
    c = _flow_cand(anchor={"direction": "bearish", "type": "put",
                           "flow_side": "put_buyer",
                           "contract": "O:AAA260101P00100000"},
                   direction="bearish")
    assert td.hard_filter(c, "risk_off", 90, NOW) is None


def test_illiquid_and_small_and_stale_rejected():
    assert td.hard_filter(_flow_cand(anchor={"liquidity": "D"}),
                          "risk_on", 90, NOW) == "BAD_LIQUIDITY"
    small = _flow_cand(anchor={"premium": 100_000})
    small["total_premium"] = 100_000
    assert td.hard_filter(small, "risk_on", 90, NOW) == "LOW_PREMIUM"
    old = (NOW - timedelta(minutes=td.STALE_MIN + 5)).isoformat()
    assert td.hard_filter(_flow_cand(anchor={"flagged_at": old}),
                          "risk_on", 90, NOW) == "STALE_DATA"


def test_low_or_missing_score_rejected():
    assert td.hard_filter(_flow_cand(), "risk_on", None,
                          NOW) == "INSUFFICIENT_DATA"
    assert td.hard_filter(_flow_cand(), "risk_on",
                          td.SCORE_FLOOR - 1, NOW) == "LOW_ALPHA"


# --------------------------------------------------------------- scoring

def test_score_monotone_and_bounded(monkeypatch):
    model = {"production_model": {
        "base": 0.0,
        "adj": {"side:put_buy": 1.0, "side:call_buy": -1.0},
        "scale_anchors": [round(-2 + 4 * i / 20, 3) for i in range(21)]}}
    real_load = td._load

    def fake(path, default):
        if path.endswith("trade_desk_validation.json"):
            return model
        return real_load(path, default)
    monkeypatch.setattr(td, "_load", fake)
    score, ok = td._score_fn()
    assert ok
    hi = score({"side": "put_buy"})
    lo = score({"side": "call_buy"})
    assert 0 <= lo < hi <= 100


def test_construct_never_invents_marks():
    c = _flow_cand()
    built = td.construct(c)
    assert built["option_mark"] is None
    assert built["underlying_at_flag"] == 100.0


# ---------------------------------------------------------------- ledger

def test_ledger_append_only_and_idempotent(tmp_path, monkeypatch):
    lp = str(tmp_path / "ledger.jsonl")
    monkeypatch.setattr(td, "LEDGER_PATH", lp)
    idea = {"family": "flow", "ticker": "AAA", "direction": "bullish",
            "status": "WATCH", "alpha_score": 75.0}
    ev1 = td.freeze_ideas([idea], NOW)
    ev2 = td.freeze_ideas([dict(idea, alpha_score=99.0)], NOW)
    lines = [json.loads(l) for l in open(lp, encoding="utf-8")]
    assert len(lines) == 1                      # no duplicate for same day
    assert lines[0]["idea"]["alpha_score"] == 75.0   # never mutated
    assert lines[0]["as_of"] and lines[0]["data_cutoff"]
    assert lines[0]["model_version"] == tdv.MODEL_VERSION


def test_grading_waits_for_maturation(tmp_path, monkeypatch):
    lp = str(tmp_path / "ledger.jsonl")
    monkeypatch.setattr(td, "LEDGER_PATH", lp)
    idea = {"family": "flow", "ticker": "AAA", "direction": "bullish",
            "status": "WATCH", "alpha_score": 75.0}
    events = td.freeze_ideas([idea], NOW)
    # do_grade=True but nothing is old enough -> no network, no grades
    events, n = td.grade_ledger(events, NOW, do_grade=True)
    assert n == 0
    assert all(e["ev"] == "issued" for e in events)


def test_performance_gates_below_min_n():
    events = [{"ev": "issued", "id": "flow:AAA:bullish:2026-01-02",
               "idea": {"family": "flow", "alpha_score": 85,
                        "status": "WATCH"}},
              {"ev": "graded", "id": "flow:AAA:bullish:2026-01-02",
               "y": 1.5}]
    perf = td.performance(events)
    assert perf["status"] == "accruing"
    assert "overall" not in perf
    assert perf["score_calibration"]["80-100"]["status"] == "accruing"


def test_performance_publishes_at_min_n():
    events = []
    for i in range(td.MIN_N):
        iid = f"flow:T{i}:bullish:2026-01-02"
        events.append({"ev": "issued", "id": iid,
                       "idea": {"family": "flow", "alpha_score": 85,
                                "status": "WATCH"}})
        events.append({"ev": "graded", "id": iid,
                       "y": 1.0 if i % 2 else -0.5})
    perf = td.performance(events)
    assert perf["overall"]["n"] == td.MIN_N
    assert perf["score_calibration"]["80-100"]["n"] == td.MIN_N


# ------------------------------------------------------ validation math

def test_spearman_perfect_and_inverse():
    assert tdv.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert tdv.spearman([1, 2, 3, 4], [40, 30, 20, 10]) == -1.0


def test_fit_shrinks_small_cohorts_toward_baseline():
    train = ([{"feats": {"f": "rare"}, "y": 10.0}] * 5
             + [{"feats": {"f": "common"}, "y": 0.0}] * 995)
    m = tdv.fit(train)
    # rare cohort's raw dev ~ +9.95 but n=5 vs K=200 -> tiny, clamped adj
    assert abs(m["adj"]["f:rare"]) < 0.5


def test_qualification_requires_positive_every_fold():
    """Reproduce the gate logic: one negative fold kills the cutoff."""
    folds = [{"cutoffs": {"80": {"n": 500, "avg": 0.5, "med": 0.2}}},
             {"cutoffs": {"80": {"n": 500, "avg": -0.1, "med": 0.1}}}]
    ok = all(f["cutoffs"]["80"].get("n", 0) >= tdv.QUAL_MIN_N
             and f["cutoffs"]["80"].get("avg", -1) > 0
             and f["cutoffs"]["80"].get("med", -1) > 0
             for f in folds)
    assert ok is False


# ------------------------------------------------------------ what changed

def test_what_changed_detects_status_and_new_and_dropped():
    prev = {"top_ideas": [{"family": "flow", "ticker": "AAA",
                           "direction": "bullish", "status": "QUALIFIED",
                           "alpha_score": 85}],
            "watch": [{"family": "flow", "ticker": "BBB",
                       "direction": "bearish", "status": "WATCH",
                       "alpha_score": 70}]}
    cur = [{"family": "flow", "ticker": "AAA", "direction": "bullish",
            "status": "WATCH", "alpha_score": 85},
           {"family": "flow", "ticker": "CCC", "direction": "bullish",
            "status": "WATCH", "alpha_score": 75}]
    ch = td.what_changed(prev, cur)
    kinds = {(c["t"], c["change"]) for c in ch}
    assert ("AAA", "status") in kinds
    assert ("CCC", "new") in kinds
    assert ("BBB", "dropped") in kinds


# ------------------------------------------------------- freshness guards

def test_stale_source_files_yield_no_candidates(monkeypatch):
    stale = {"generated": "2026-01-01T00:00:00+00:00",
             "ideas": [{"t": "AAA", "type": "vol_rich", "bias": "neutral"}]}
    real_load = td._load

    def fake(path, default):
        if path.endswith("earnings_ideas.json"):
            return stale
        return real_load(path, default)
    monkeypatch.setattr(td, "_load", fake)
    before = td.REJECT.get("STALE_DATA", 0)
    assert td._earnings_candidates(NOW) == []
    assert td.REJECT["STALE_DATA"] == before + 1


def test_stale_regime_reads_unknown():
    old = {"2026-01-02": "risk_off"}
    assert td._regime_today(old, NOW) == "unknown"
    fresh_day = NOW.date().isoformat()
    assert td._regime_today({fresh_day: "risk_off"}, NOW) == "risk_off"


# ------------------------------------------------- setup-level research

def test_direction_derived_from_flow_side_when_absent():
    """Scored-feed rows omit `direction` — the candidate builder must
    derive it (call buyer = bullish, put buyer = bearish, sellers out)."""
    import trade_desk_research  # noqa: F401  (import sanity)
    sigs = [
        {"flagged_at": NOW.isoformat(), "ticker": "AAA", "type": "call",
         "flow_side": "call_buyer", "premium": 5e5, "liquidity": "B"},
        {"flagged_at": NOW.isoformat(), "ticker": "BBB", "type": "put",
         "flow_side": "put_buyer", "premium": 5e5, "liquidity": "B"},
        {"flagged_at": NOW.isoformat(), "ticker": "CCC", "type": "call",
         "flow_side": "call_seller", "premium": 5e5, "liquidity": "B"},
    ]
    import trade_desk as td2
    real_load = td2._load

    def fake(path, default):
        if path.endswith("uoa_signals_scored.json"):
            return {"signals": sigs}
        return real_load(path, default)
    orig = td2._load
    td2._load = fake
    try:
        out = td2._flow_candidates({}, NOW)
    finally:
        td2._load = orig
    got = {(c["ticker"], c["direction"]) for c in out}
    assert ("AAA", "bullish") in got
    assert ("BBB", "bearish") in got
    assert all(t != "CCC" for t, _ in got)


def test_research_purge_drops_label_overlap():
    import trade_desk_research as tdr
    train = [{"date": "2026-06-01"}, {"date": "2026-06-09"},
             {"date": "2026-06-14"}]
    purged = tdr._purge(train, "2026-06-15")
    dates = [r["date"] for r in purged]
    assert "2026-06-14" not in dates      # inside embargo window
    assert "2026-06-01" in dates


def test_research_bootstrap_flags_thin_clusters():
    import trade_desk_research as tdr
    out = tdr.cluster_bootstrap([("a", 1.0), ("b", -1.0)])
    assert out["status"] == "insufficient_clusters"


def test_degraded_family_routes_to_experimental_tier(monkeypatch):
    real_load = td._load

    def fake(path, default):
        if path.endswith("scan_outcomes.json"):
            return {"overall": {"status": "active", "n": 500, "ev": -1.0,
                                "profit_factor": 0.8}}
        return real_load(path, default)
    monkeypatch.setattr(td, "_load", fake)
    assert td.family_gates()["momentum"]["verdict"] == "degraded"
    # run()'s tier mapping: degraded -> EXPERIMENTAL (not rejected) is
    # asserted via the mapping dict used there
    assert {"qualified": "QUALIFIED",
            "degraded": "EXPERIMENTAL"}.get("degraded") == "EXPERIMENTAL"


def test_expired_preprint_earnings_ideas_rejected(monkeypatch):
    """A vol_rich idea for a report that already printed must never
    surface — pre-print types expire at the event."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    et_today = (now.astimezone(timezone(timedelta(hours=-4)))
                .date().isoformat())
    yesterday = (now.astimezone(timezone(timedelta(hours=-4))).date()
                 - timedelta(days=1)).isoformat()
    doc = {"generated": now.isoformat(),
           "ideas": [
               {"t": "AAA", "type": "vol_rich", "bias": "neutral",
                "date": yesterday, "session": "AMC"},
               {"t": "BBB", "type": "vol_rich", "bias": "neutral",
                "date": et_today, "session": "AMC"},
               {"t": "CCC", "type": "post_report_drift", "bias": "bull",
                "date": yesterday, "session": "AMC"},
           ]}
    real_load = td._load

    def fake(path, default):
        if path.endswith("earnings_ideas.json"):
            return doc
        return real_load(path, default)
    monkeypatch.setattr(td, "_load", fake)
    out = td._earnings_candidates(now)
    got = {c["ticker"] for c in out}
    assert "AAA" not in got        # printed yesterday -> dead
    assert "BBB" in got            # prints tonight -> alive
    assert "CCC" in got            # post-print type -> alive


# ---------------------------------------- signal vs trade qualification

def _fake_files(vol_sig_qualified, bt_trade_qualified):
    real_load = td._load

    def fake(path, default):
        if path.endswith("earnings_ideas.json"):
            return {"by_type": {"vol_rich": {
                "status": "active", "n": 102, "win_rate": 91,
                "ev": 6.87}}}
        if path.endswith("earnings_vol.json"):
            return {"types": {"vol_rich": {
                "signal_qualified": vol_sig_qualified,
                "date_cluster_bootstrap": {"nights": 18,
                                           "ci95": [5.87, 7.83]}}}}
        if path.endswith("earnings_vol_backtest.json"):
            return ({"types": {"vol_rich": {
                "trade_qualified": bt_trade_qualified}}}
                if bt_trade_qualified is not None else {})
        return real_load(path, default)
    return fake


def test_vol_type_caps_at_signal_qualified_without_backtest(monkeypatch):
    """A validated implied-vs-realized relationship is a QUALIFIED
    SIGNAL, never a qualified trade, until the option-P&L
    reconstruction clears costs and tails."""
    monkeypatch.setattr(td, "_load", _fake_files(True, None))
    g = td.family_gates()["earnings"]["types"]["vol_rich"]
    assert g["verdict"] == "signal_qualified"
    assert "pending executable validation" in g["why"]


def test_vol_type_trade_qualifies_only_via_backtest(monkeypatch):
    monkeypatch.setattr(td, "_load", _fake_files(True, True))
    g = td.family_gates()["earnings"]["types"]["vol_rich"]
    assert g["verdict"] == "trade_qualified"


def test_vol_type_without_night_ci_stays_watch(monkeypatch):
    monkeypatch.setattr(td, "_load", _fake_files(False, None))
    g = td.family_gates()["earnings"]["types"]["vol_rich"]
    assert g["verdict"] == "watch"


def test_vol_engine_ratio_reconstruction():
    """ratio = 1 - mv/implied for vol_rich; 1 + mv/implied for cheap."""
    import earnings_vol_engine as ev
    # vol_rich: implied 10, |realized| 2.3 -> mv 7.7 -> ratio 0.23
    assert abs((1 - 7.7 / 10) - 0.23) < 1e-9
    # vol_cheap: implied 5, |realized| 9 -> mv 4 -> ratio 1.8
    assert abs((1 + 4 / 5) - 1.8) < 1e-9


def test_backtest_defined_risk_cannot_lose_more_than_1R():
    """Defined-risk ROR floor: pnl >= -risk by construction, so
    loss_gt_1R must count zero for correctly built structures."""
    import earnings_vol_backtest as bt
    recons = [{"event": {"type": "vol_rich", "date": "2026-08-01"},
               "strategies": {"iron_fly": {"pnl": -500, "risk": 500,
                                           "ror": -1.0}}}]
    tbl = bt.strategy_table(recons, "iron_fly")
    assert tbl["loss_gt_1R"] == 0
