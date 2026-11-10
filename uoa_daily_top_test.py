"""Top 25 Daily: activity evidence, side-default bonus, callout timing."""

import uoa_daily_top as dt

G1 = "2026-11-10T14:35:00+00:00"
G2 = "2026-11-10T15:05:00+00:00"
G3 = "2026-11-10T15:35:00+00:00"


def _row(**kw):
    base = dt._fixture_row()
    base.update(trade_count=100, side_method="approx", bid_pct=20, expiry="2026-12-18")
    base.update(kw)
    return base


def _acc():
    return {"et_date": None, "batches_seen": [], "contracts": {}}


def _merge(acc, gen, rows):
    acc, _ = dt.merge_batch(acc, {"generated": gen, "rows": rows})
    return acc


def test_repeat_count_change_is_not_new_activity():
    # repeat_count = earlier days flagged; it can change with zero trades
    acc = _merge(_acc(), G1, [_row(repeat_count=0)])
    acc = _merge(acc, G2, [_row(repeat_count=3)])
    c = next(iter(acc["contracts"].values()))
    assert c["seen_scans"] == 2
    assert c["active_batches"] == 1


def test_new_trades_are_new_activity():
    acc = _merge(_acc(), G1, [_row()])
    acc = _merge(acc, G2, [_row(trade_count=140)])
    acc = _merge(acc, G3, [_row(trade_count=140, premium=1_200_000)])
    c = next(iter(acc["contracts"].values()))
    assert c["active_batches"] == 3
    assert c["trade_count"] == 140


def test_no_trade_rows_get_no_side_points():
    no_trades = _row(ticker="NOT", ask_pct=0, bid_pct=0, side_method="none", trade_count=0)
    real = _row(ticker="REAL", ask_pct=80, bid_pct=10)
    acc = _merge(_acc(), G1, [no_trades, real])
    dt.score_all(acc["contracts"], 1)
    by = {c["ticker"]: c for c in acc["contracts"].values()}
    assert by["NOT"]["score_parts"]["side_conviction"] == 0.0
    assert by["REAL"]["score_parts"]["side_conviction"] > 0
    assert dt.side_evidence_pct(by["NOT"]) is None


def test_placeholder_side_earns_no_strong_signal():
    # identical rows: one with the 0/0 placeholder (nothing classified),
    # one with a genuinely balanced tape (50% ask). Neither has side
    # evidence, so both must earn the same strong-signal count; before the
    # fix the placeholder read as "100% at the bid" and earned one more.
    common = {"sweeps": 0, "blocks": 0, "oi_delta": 0, "vol_oi": 1.5, "premium": 300_000}
    placeholder = _row(ticker="PH", ask_pct=0, bid_pct=0, **common)
    balanced = _row(ticker="BAL", ask_pct=50, bid_pct=50, **common)
    acc = _merge(_acc(), G1, [placeholder, balanced])
    dt.score_all(acc["contracts"], 1)
    by = {c["ticker"]: c for c in acc["contracts"].values()}
    assert by["PH"]["score_parts"]["side_conviction"] == 0.0
    assert by["PH"]["strong_signals"] == by["BAL"]["strong_signals"]


def test_payload_publishes_trade_count_not_repeat_days_as_prints():
    acc = _merge(_acc(), G1, [_row(repeat_count=2, trade_count=57, last_trade_price=1.9, vwap=2.1)])
    elig = dt.score_all(acc["contracts"], 1)
    ranked = dt.top_n(elig, 1)
    p = dt.build_payload(acc, ranked, len(elig), len(acc["contracts"]))
    r = p["rows"][0]
    assert r["trade_count"] == 57
    assert r["repeat_days"] == 2
    assert "unique_prints" not in r
    assert r["last_trade_price"] == 1.9 and r["vwap"] == 2.1
    assert "repeat_days" in r["score_parts"] and "prints" not in r["score_parts"]


def _rank_run(acc, gen, rows, shown_at):
    acc = _merge(acc, gen, rows)
    elig = dt.score_all(acc["contracts"], len(acc["batches_seen"]))
    ranked = dt.top_n(elig, len(acc["batches_seen"]))
    dt.mark_callouts(acc, ranked, shown_at)
    return acc, ranked


def test_callout_time_is_first_top25_appearance_not_first_scan(monkeypatch):
    monkeypatch.setattr(dt, "CALLOUT_TOP_N", 2)
    big = [_row(ticker=f"B{i}", premium=5_000_000, trade_score=90) for i in range(2)]
    late = _row(ticker="LATE", premium=300_000, trade_score=40)
    # batch 1: LATE is scanned but ranks below the top 2
    acc, ranked = _rank_run(_acc(), G1, [*big, late], "2026-11-10T14:36:10+00:00")
    assert "LATE" not in [c["ticker"] for c in ranked[:2]]
    # batch 2: LATE surges into the top 2
    surge = _row(ticker="LATE", premium=40_000_000, trade_score=99, trade_count=900)
    acc, ranked = _rank_run(acc, G2, [*big, surge], "2026-11-10T15:06:05+00:00")
    by = {c["ticker"]: c for c in acc["contracts"].values()}
    assert by["LATE"]["first_seen"] == G1
    assert by["LATE"]["first_top_at"] == "2026-11-10T15:06:05+00:00"
    assert by["LATE"]["first_top_rank"] <= 2

    p = dt.build_payload(acc, ranked, 3, 3)
    tickers = [c["ticker"] for c in p["callouts"]]
    # B1 was pushed out in batch 2 but stays a callout (every one shown)
    assert sorted(tickers) == ["B0", "B1", "LATE"]
    assert p["callouts_complete"] is True
    late_c = next(c for c in p["callouts"] if c["ticker"] == "LATE")
    assert late_c["first_top_at"] == "2026-11-10T15:06:05+00:00"


def test_callouts_incomplete_when_marking_starts_mid_day():
    acc = _merge(_acc(), G1, [_row()])  # batch before this code was live
    acc, ranked = _rank_run(acc, G2, [_row()], "2026-11-10T15:06:00+00:00")
    p = dt.build_payload(acc, ranked, 1, 1)
    assert p["callouts_complete"] is False
