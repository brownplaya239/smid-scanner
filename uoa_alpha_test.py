"""Accuracy rules for the Options Flow track record (uoa_alpha.py).

Each test pins one measurement rule from the 2026-11 accuracy audit:
one alert per contract per ET day, direction-adjusted grading, split-safe
price basis, next-day-only OI confirmation, clustered CIs.
"""

import json
from datetime import date, datetime, timedelta

import pytest

import uoa_alpha as ua


def _sig(**kw):
    base = {
        "id": "x",
        "flagged_at": "2026-10-05T15:00:00+00:00",
        "ticker": "ABC",
        "contract": "O:ABC261120C00100000",
        "type": "call",
        "flow_side": "call_buyer",
        "direction": "bullish",
        "trade_score": 70,
        "volume": 1000,
        "open_interest": 500,
        "underlying_px_at_flag": 100.0,
    }
    base.update(kw)
    return base


def _ret(r, exc=None, exc_c=None):
    return {"ret": r, "excess": r if exc is None else exc, "exc_c": exc if exc_c is None else exc_c}


# ── direction ────────────────────────────────────────────────────────────


def test_dir_sign_by_direction_and_fallback():
    assert ua._dir_sign({"direction": "bullish"}) == 1
    assert ua._dir_sign({"direction": "bearish"}) == -1
    assert ua._dir_sign({"direction": "income", "flow_side": "put_seller"}) is None
    assert ua._dir_sign({"direction": "hedge", "flow_side": "mixed"}) is None
    # old rows with no `direction` fall back to the aggressor side
    assert ua._dir_sign({"flow_side": "put_buyer"}) == -1
    assert ua._dir_sign({"flow_side": "mixed"}) is None


def test_put_buyer_is_graded_on_the_stock_falling():
    put = _sig(direction="bearish", flow_side="put_buyer", returns={5: _ret(-5.0)})
    call = _sig(returns={5: _ret(3.0)})
    seller = _sig(direction="income", flow_side="put_seller", returns={5: _ret(-9.0)})
    g = ua._group([put, call, seller])["5"]
    assert g["n"] == 2  # the seller has no stock direction to grade
    assert g["hit_rate"] == 100  # both directional calls were right
    assert g["avg"] == 4.0  # (+5 put, +3 call) / 2


def test_excursion_flips_for_bearish_and_needs_full_window():
    full = {5: _ret(-2.0), 20: _ret(-8.0)}
    put = _sig(direction="bearish", returns=full, excursion={"mfe": 4.0, "mae": -12.0})
    assert ua._signed_excursion(put) == (12.0, -4.0)  # favourable = the drop
    young = _sig(returns={5: _ret(1.0)}, excursion={"mfe": 3.0, "mae": -1.0})
    assert ua._signed_excursion(young) is None  # 20 sessions not matured
    assert ua._signed_excursion(young, require_full=False) == (3.0, -1.0)
    avg = ua._excursion_avg([put, young])
    assert avg == {"avg_mfe": 12.0, "avg_mae": -4.0, "n": 1}


# ── one alert per contract per ET day ────────────────────────────────────


def test_unique_alerts_collapses_relogs_keeps_first_and_day_volume():
    rows = [
        _sig(id="a", flagged_at="2026-10-05T15:00:00+00:00", volume=800, trade_score=66),
        _sig(id="b", flagged_at="2026-10-05T19:00:00+00:00", volume=3000, trade_score=82),
        # 23:30 UTC is still Oct 5 in New York: same alert
        _sig(id="c", flagged_at="2026-10-05T23:30:00+00:00", volume=3100),
        # 00:30 UTC Oct 7 is Oct 6 in New York: a new alert
        _sig(id="d", flagged_at="2026-10-07T00:30:00+00:00", volume=50),
    ]
    out = ua.unique_alerts(rows)
    assert [r["id"] for r in out] == ["a", "d"]
    assert out[0]["trade_score"] == 66  # the score users first saw
    assert out[0]["day_volume"] == 3100
    assert out[0]["relogs"] == 3


def test_unique_alerts_regrades_oi_against_full_day_volume():
    oi = {"status": "confirmed", "oi_change": 900, "retained_pct": 113, "basis": "next_day"}
    rows = [
        _sig(id="a", volume=800, oi=oi),
        _sig(id="b", flagged_at="2026-10-05T19:00:00+00:00", volume=3000, oi=oi),
    ]
    rep = ua.unique_alerts(rows)[0]
    assert rep["oi"]["status"] == "weak"  # 900 / 3000 = 30%: >15%, <50%
    assert rep["oi"]["retained_pct"] == 30
    assert rep["oi"]["basis"] == "next_day"


# ── price basis / splits ─────────────────────────────────────────────────


def _bars(closes, start=date(2026, 10, 5)):
    out, d = {}, start
    for c in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out[d.isoformat()] = {"c": c, "h": c * 1.01, "l": c * 0.99}
        d += timedelta(days=1)
    return out


def test_split_factor_only_counts_executed_splits_after_the_flag():
    splits = [
        {"execution_date": "2026-10-10", "split_from": 1, "split_to": 4},  # after flag
        {"execution_date": "2026-01-10", "split_from": 1, "split_to": 2},  # before flag
        {"execution_date": "2027-01-10", "split_from": 1, "split_to": 3},  # not yet
    ]
    assert ua.split_factor(splits, "2026-10-05", "2026-11-10") == 0.25


def test_forward_returns_split_adjusts_the_flag_price():
    # Bars are split-adjusted (4-for-1 after the flag): flag-day close 100.
    bars = _bars([100, 101, 102, 103, 104, 106, 107])
    spy = {d: 500.0 for d in bars}
    s = _sig(underlying_px_at_flag=400.0)  # raw pre-split spot
    assert ua.forward_returns(s, bars, spy) == {}  # basis mismatch -> excluded
    out = ua.forward_returns(s, bars, spy, sfac=0.25)
    assert out[5]["ret"] == 6.0
    assert out[5]["excess"] == 6.0  # flat SPY
    assert ua.excursions(s, bars)["mfe"] is None
    assert ua.excursions(s, bars, sfac=0.25)["mfe"] is not None


def test_forward_returns_aligns_spy_by_date_not_index():
    bars = _bars([100, 101, 102, 103, 104, 105])
    days = sorted(bars)
    spy = {d: 500.0 for d in days}
    del bars[days[2]]  # the stock is missing a session
    spy[days[4]] = 510.0  # SPY +2% on the stock's 3rd bar date
    out = ua.forward_returns(_sig(), bars, spy)
    # stock's +3 bar is days[4] (one skipped) -> SPY leg must use days[4]
    assert out[3]["ret"] == 4.0
    assert out[3]["excess"] == 2.0


def test_basis_ok_flags_frozen_split_artifacts():
    good = _sig(returns={1: _ret(2.0, exc=1.5, exc_c=1.0)})
    assert ua._basis_ok(good)
    # ret +300% with a close-anchored excess of ~0 => flag price was 1/4 basis
    bad = _sig(returns={1: {"ret": 300.0, "excess": 300.0, "exc_c": 0.0}})
    assert not ua._basis_ok(bad)
    assert ua._signed_returns(bad) is None
    assert not ua._basis_ok(_sig(basis_suspect=True))


# ── next-day OI ──────────────────────────────────────────────────────────


def test_oi_is_frozen_at_the_first_next_weekday_read():
    s = _sig(flagged_at="2026-10-09T18:00:00+00:00")  # Friday
    store = {}
    mon = date(2026, 10, 12)
    st = ua.oi_status(s, {s["contract"]: 1200}, store, mon)
    assert st["basis"] == "next_day"
    assert st["oi_change"] == 700 and st["status"] == "confirmed"
    # a later re-read must not overwrite the next-day read
    st2 = ua.oi_status(s, {s["contract"]: 100}, store, mon + timedelta(days=9))
    assert st2["oi_change"] == 700 and st2["basis"] == "next_day"


def test_late_first_read_is_kept_out_of_the_next_day_stat():
    s = _sig(flagged_at="2026-10-05T15:00:00+00:00")  # Monday
    st = ua.oi_status(s, {s["contract"]: 1200}, {}, date(2026, 10, 9))
    assert st["basis"] == "late"
    assert ua._nextday_statuses([dict(s, oi=st)]) == []
    assert ua.oi_status(s, {}, {}, date(2026, 10, 5))["status"] == "pending"


# ── statistics ───────────────────────────────────────────────────────────


def test_cluster_se_counts_days_not_rows():
    # 50 identical alerts per day on 4 days: really 4 observations
    pairs = [
        (d, v) for d, v in (("d1", 1.0), ("d2", -1.0), ("d3", 2.0), ("d4", -2.0)) for _ in range(50)
    ]
    se = ua._cluster_se(pairs)
    from statistics import mean, pstdev

    day_means = [1.0, -1.0, 2.0, -2.0]
    naive_rows = pstdev([v for _, v in pairs]) / len(pairs) ** 0.5
    # CR1 on equal clusters == sample SE of the 4 day means
    expected = (sum((m - mean(day_means)) ** 2 for m in day_means) / 3 / 4) ** 0.5
    assert se == pytest.approx(expected)
    assert se > 5 * naive_rows


def test_rich_is_direction_adjusted_with_clustered_ci():
    rows = []
    for i in range(30):
        day = f"2026-09-{i + 1:02d}T15:00:00+00:00"
        rows.append(_sig(id=f"c{i}", flagged_at=day, returns={5: _ret(2.0)}))
        rows.append(
            _sig(
                id=f"p{i}",
                flagged_at=day,
                direction="bearish",
                flow_side="put_buyer",
                returns={5: _ret(-2.0)},
            )
        )
    r = ua._rich(rows)
    assert r["ev"] == 2.0 and r["win_rate"] == 100
    assert r["days"] == 30


# ── lifecycle ────────────────────────────────────────────────────────────


def test_lifecycle_keys_per_alert_and_ages_out_by_date(tmp_path, monkeypatch):
    monkeypatch.setattr(ua, "LIFECYCLE_PATH", str(tmp_path / "life.json"))
    (tmp_path / "life.json").write_text('{"legacy-id": {"5": 1}, "O:OLD|2026-01-02": {"1": 1}}')
    s = _sig(flagged_at="2026-10-05T15:00:00+00:00", day_volume=1000)
    out = ua._lifecycle([s], {"ABC": {s["contract"]: 900}}, date(2026, 10, 6))
    assert out["1"]["n"] == 1
    import json

    log = json.loads((tmp_path / "life.json").read_text())
    assert list(log) == [ua._oi_key(s)]  # legacy + stale keys pruned
    assert log[ua._oi_key(s)] == {"1": 1}  # +400 OI > 15% of 1000


# ── end to end ───────────────────────────────────────────────────────────


def test_compute_edge_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(ua, "ALPHA_CACHE_PATH", str(tmp_path / "cache.json"))
    monkeypatch.setattr(ua, "OI_NEXTDAY_PATH", str(tmp_path / "nextday.json"))
    monkeypatch.setattr(ua, "LIFECYCLE_PATH", str(tmp_path / "life.json"))
    today = datetime.now(ua.UTC).date()
    start = today - timedelta(days=20)
    while start.weekday() >= 5:
        start += timedelta(days=1)
    bars = _bars([100 + i for i in range(30)], start)
    flag = f"{start.isoformat()}T15:00:00+00:00"
    ledger = [
        # same call contract flagged on three scans -> one alert
        _sig(id="c1", flagged_at=flag, volume=500),
        _sig(id="c2", flagged_at=f"{start.isoformat()}T18:00:00+00:00", volume=900),
        _sig(id="c3", flagged_at=f"{start.isoformat()}T23:40:00+00:00", volume=1000),
        # a put buyer on the same rising stock -> a directional loss
        _sig(
            id="p1",
            contract="O:ABC261120P00090000",
            type="put",
            flow_side="put_buyer",
            direction="bearish",
            flagged_at=flag,
        ),
        # a premium seller -> counted, but not graded
        _sig(
            id="s1",
            contract="O:ABC261120P00080000",
            type="put",
            flow_side="put_seller",
            direction="income",
            flagged_at=flag,
        ),
    ]
    monkeypatch.setattr(ua, "load_ledger", lambda: [dict(r) for r in ledger])
    monkeypatch.setattr(ua, "_bars", lambda t, days=160: bars)
    monkeypatch.setattr(ua.pg, "splits", lambda t, limit=50: [])
    monkeypatch.setattr(ua, "_oi_now", lambda t: {})
    edge, rows = ua.compute_edge()
    assert len(rows) == 5  # research consumers still get every ledger row
    assert edge["ledger_rows"] == 5
    assert edge["total_signals"] == 3  # call alert + put alert + seller alert
    assert edge["graded_signals"] == 2
    h5 = edge["overall"]["5"]
    assert h5["n"] == 2 and h5["hit_rate"] == 50  # call won, put lost
    assert edge["direction_split"]["unclassified_n"] == 0
    assert "STOCK" in edge["method"]


def _old_ledger(today):
    old = today - timedelta(days=40)
    while old.weekday() >= 5:
        old -= timedelta(days=1)
    return [_sig(id="old1", flagged_at=f"{old.isoformat()}T15:00:00+00:00")]


def test_no_freeze_when_bars_fail_and_read_only_writes_nothing(tmp_path, monkeypatch):
    cache = tmp_path / "cache.json"
    monkeypatch.setattr(ua, "ALPHA_CACHE_PATH", str(cache))
    monkeypatch.setattr(ua, "OI_NEXTDAY_PATH", str(tmp_path / "nextday.json"))
    monkeypatch.setattr(ua, "LIFECYCLE_PATH", str(tmp_path / "life.json"))
    today = datetime.now(ua.UTC).date()
    monkeypatch.setattr(ua, "load_ledger", lambda: _old_ledger(today))
    monkeypatch.setattr(ua.pg, "splits", lambda t, limit=50: [])
    monkeypatch.setattr(ua, "_oi_now", lambda t: {})
    # outage: no bars -> the final signal must NOT be frozen empty
    monkeypatch.setattr(ua, "_bars", lambda t, days=160: {})
    ua.compute_edge()
    assert not cache.exists()
    for f in ("nextday.json", "life.json"):
        (tmp_path / f).unlink(missing_ok=True)
    # read-only scoring writes no shared state even when bars load
    start = today - timedelta(days=45)
    monkeypatch.setattr(ua, "_bars", lambda t, days=160: _bars([100 + i for i in range(40)], start))
    ua.compute_edge(persist=False)
    assert not cache.exists()
    assert not (tmp_path / "nextday.json").exists()
    assert not (tmp_path / "life.json").exists()
    # the flow job (persist=True) does freeze it once bars are there
    ua.compute_edge()
    assert "old1" in json.loads(cache.read_text())
