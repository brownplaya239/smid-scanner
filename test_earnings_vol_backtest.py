"""Tests for the earnings options backtest's caching and retry rules.

The backtest saves results forever, so it must never save a skip that a
failed request (or not-yet-available data) caused, must not retry
missing data forever, and must never value an option at $0 because the
expiry-day price is missing. These tests drive reconstruct() through a
fake Polygon (`pg._get`) so request failures enter where they do in
production.

Fixture tickers are synthetic (AAA) and dates are fixed.

    uv run pytest test_earnings_vol_backtest.py -q
"""

import json
from datetime import UTC, date, datetime, timedelta

import pytest

import earnings_vol_backtest as bt
import polygon_data as pg

EVENT = {
    "ticker": "AAA",
    "date": "2026-10-01",
    "session": "AMC",
    "type": "vol_cheap",
    "implied": 5.0,
}
EXPIRY = "2026-10-09"
CALLS = [f"O:AAA261009C{k:08d}" for k in (9000, 10000, 11000)]
PUTS = [f"O:AAA261009P{k:08d}" for k in (9000, 10000, 11000)]


def _bar(d, o, c):
    ts = datetime.fromisoformat(d).replace(tzinfo=UTC).timestamp() * 1000
    return {"t": int(ts), "o": o, "c": c}


def _contracts(tickers, kind):
    return [
        {
            "ticker": t,
            "expiration_date": EXPIRY,
            "contract_type": kind,
            "strike_price": int(t[-8:]) / 1000,
        }
        for t in tickers
    ]


def _underlying(expiry_bar=True):
    rows = [_bar("2026-10-01", 10, 10), _bar("2026-10-02", 10.5, 10.6)]
    if expiry_bar:
        rows.append(_bar(EXPIRY, 10.4, 10.4))
    rows.append(_bar("2026-10-12", 10.2, 10.3))  # a bar AFTER expiry
    return rows


def _option_bars():
    return [_bar("2026-10-01", 1.0, 1.0), _bar("2026-10-02", 0.8, 0.7)]


@pytest.fixture
def polygon(monkeypatch):
    """Fake Polygon. `state["fail"]` names what times out: a ticker, an
    option contract, "options" (every contract), "chain_page2" or
    "chain". Calls (only calls) are on chain page 1, puts on page 2."""
    state = {"fail": set(), "und": _underlying(), "opt": _option_bars(), "calls": 0}

    def fake_get(path, params=None, retries=2, timeout=30):
        state["calls"] += 1
        fail = state["fail"]
        if "/v2/aggs/ticker/" in path:
            tk = path.split("/v2/aggs/ticker/")[1].split("/")[0]
            if tk in fail or (tk.startswith("O:") and "options" in fail):
                return None
            return {"results": state["und"] if tk == "AAA" else state["opt"]}
        if path == "/v3/reference/options/contracts":
            if "chain" in fail:
                return None
            if params["expired"] == "true":
                return {
                    "results": _contracts(CALLS, "call"),
                    "next_url": "https://polygon.test/page2",
                }
            return {"results": []}
        if path == "https://polygon.test/page2":
            return None if "chain_page2" in fail else {"results": _contracts(PUTS, "put")}
        raise AssertionError(path)

    monkeypatch.setattr(pg, "_get", fake_get)
    monkeypatch.setattr(bt, "_BARS_CACHE", {})
    return state


# ------------------------------------------------- request failures


def test_clean_reconstruction(polygon):
    r = bt.reconstruct(EVENT)
    assert bt.outcome(r) == "result", r
    assert r["expiry_spot"] == 10.4
    assert set(r["strategies"]) == {"long_straddle", "long_strangle"}


def test_option_bar_timeouts_are_retryable_not_permanent(polygon):
    """Bars + chain fine, every option-bar request times out: the old code
    returned no_structures_priceable and cached it forever."""
    polygon["fail"] = {"options"}
    r = bt.reconstruct(EVENT)
    assert r["skip"] == "error:option_bars_request"
    assert bt.outcome(r) == "failed"
    cache, retry = {}, {}
    bt.record(cache, retry, EVENT, r, date(2026, 11, 16))
    assert cache == {} and retry[bt._key(EVENT)]["attempts"] == 1


def test_one_failed_option_contract_blocks_a_partial_result(polygon):
    polygon["fail"] = {CALLS[1]}
    r = bt.reconstruct(EVENT)
    assert r["skip"] == "error:option_bars_request"
    assert r["failed_contracts"] == [CALLS[1]]


def test_partial_chain_is_not_cached_as_one_sided(polygon):
    """Page 1 (calls) arrives, page 2 (puts) times out: this used to be a
    permanent one_sided_chain."""
    polygon["fail"] = {"chain_page2"}
    r = bt.reconstruct(EVENT)
    assert r["skip"] == "error:chain_request" and bt.outcome(r) == "failed"


def test_failed_underlying_request_is_not_no_bars(polygon):
    polygon["fail"] = {"AAA"}
    assert bt.reconstruct(EVENT) == {"skip": "error:underlying_bars_request"}


def test_chain_request_failure(polygon):
    polygon["fail"] = {"chain"}
    assert bt.reconstruct(EVENT)["skip"] == "error:chain_request"


def test_confirmed_empty_option_bars_are_permanent(polygon):
    """Every request succeeded and the contracts never traded: that is
    a real, permanent skip."""
    polygon["opt"] = []
    r = bt.reconstruct(EVENT)
    assert r == {"skip": "no_structures_priceable"} and bt.outcome(r) == "permanent"


def test_empty_underlying_answer_is_a_coverage_gap(polygon):
    polygon["und"] = []
    r = bt.reconstruct(EVENT)
    assert r == {"skip": "no_underlying_bars"} and bt.outcome(r) == "coverage"


def test_failed_option_bars_are_not_memoized(polygon):
    polygon["fail"] = {"options"}
    assert bt.outcome(bt.reconstruct(EVENT)) == "failed"
    polygon["fail"] = set()
    assert bt.outcome(bt.reconstruct(EVENT)) == "result"


# ------------------------------------------------- expiry settlement


def test_missing_expiry_bar_with_later_bars_is_not_zero(polygon):
    """Bars exist on both sides of expiry but not on expiry day. The old
    guard only looked at the latest bar, so this valued every leg at $0."""
    polygon["und"] = _underlying(expiry_bar=False)
    r = bt.reconstruct(EVENT)
    assert r == {"skip": "no_expiry_settlement", "expiry": EXPIRY}
    assert bt.outcome(r) == "coverage"


def test_unexpired_option_waits(polygon):
    polygon["und"] = _underlying()[:2]  # latest bar is before expiry
    r = bt.reconstruct(EVENT)
    assert r == {"skip": "expiry_pending", "expiry": EXPIRY}
    assert bt.outcome(r) == "pending"


@pytest.mark.parametrize("bad", [0, -1.0, float("nan"), None, "10"])
def test_invalid_expiry_price_is_not_a_settlement(polygon, bad):
    polygon["und"] = [_bar("2026-10-01", 10, 10), _bar("2026-10-02", 10.5, 10.6)]
    polygon["und"] += [dict(_bar(EXPIRY, 10, 10), c=bad), _bar("2026-10-12", 10, 10)]
    assert bt.reconstruct(EVENT)["skip"] == "no_expiry_settlement"


def test_structure_pnl_never_values_missing_settlement_at_zero():
    legs = [
        {
            "side": +1,
            "kind": "call",
            "strike": 100,
            "marks": {"entry_close": 2.0, "exit_open": 1.0, "exit_close": 1.0},
        }
    ]
    assert bt._structure_pnl(legs, "expiry_hold", expiry_spot=None) is None
    assert bt._structure_pnl(legs, "expiry_hold", expiry_spot=float("nan")) is None
    assert bt._structure_pnl(legs, "expiry_hold", expiry_spot=110.0) is not None


def test_legacy_rows_without_settlement_are_left_out_of_expiry_hold():
    def row(spot, pnl):
        exits = {xm: {"pnl": pnl, "risk": 100, "ror": pnl / 100} for xm in bt.EXITS}
        return {
            "night_id": "2026-09-01",
            "expiry_spot": spot,
            "strategies": {"s": {"exits": exits}},
        }

    rows = [row(10.0, 50), row(None, -100)]
    assert bt.strategy_table(rows, "s", "expiry_hold")["n"] == 1
    assert bt.strategy_table(rows, "s", "next_close")["n"] == 2


# ------------------------------------------------- planning + retries

TODAY = date(2026, 11, 16)
OLD = {"ticker": "AAA", "date": "2026-10-01", "type": "vol_rich"}


def _ev(i):
    return {"ticker": f"T{i}", "date": "2026-10-01", "type": "vol_rich"}


def test_unsettled_events_wait():
    new = {"ticker": "BBB", "date": "2026-11-12", "type": "vol_rich"}
    p = bt.plan({}, {}, [OLD, new], TODAY)
    assert p["todo"] == [OLD] and p["unsettled"] == 1


def test_permanent_skip_is_cached_dated_and_not_retried():
    cache, retry = {}, {}
    assert bt.record(cache, retry, OLD, {"skip": "one_sided_chain"}, TODAY) == "permanent"
    (row,) = cache.values()
    assert row["attempted"] == "2026-11-16" and row["bt_version"] == bt.BT_VERSION
    assert bt.plan(cache, retry, [OLD], TODAY)["todo"] == []


def test_legacy_and_transient_cached_skips_are_retried():
    k = bt._key(OLD)
    keep = "Y|2026-09-01|vol_rich|" + bt.BT_VERSION
    cache = {
        k: {"skip": "no_session_bracket", "bt_version": bt.BT_VERSION},  # undated
        "X|2026-09-01|vol_rich|" + bt.BT_VERSION: {
            "skip": "no_chain",  # coverage, not confirmed over N tries
            "bt_version": bt.BT_VERSION,
            "attempted": "2026-09-20",
        },
        keep: {"bt_version": bt.BT_VERSION, "spot": 10},
    }
    assert bt.plan(cache, {}, [OLD], TODAY)["todo"] == [OLD]
    assert list(cache) == [keep]


def test_coverage_gap_backs_off_then_becomes_permanent():
    """no_chain every night: retried on a growing schedule, then saved as
    a permanent skip after MAX_COVERAGE_ATTEMPTS tries."""
    cache, retry, day, tried = {}, {}, TODAY, []
    for _ in range(60):
        p = bt.plan(cache, retry, [OLD], day)
        if p["todo"]:
            tried.append((day - TODAY).days)
            bt.record(cache, retry, OLD, {"skip": "no_chain"}, day)
        day += timedelta(days=1)
    assert tried == [0, 1, 3, 7]
    (row,) = cache.values()
    assert row["skip"] == "no_chain" and row["attempts"] == bt.MAX_COVERAGE_ATTEMPTS
    assert retry == {} and bt.is_permanent_skip(row)


def test_request_failures_back_off_and_give_up_outside_the_results():
    cache, retry, day, tried = {}, {}, TODAY, []
    for _ in range(120):
        if bt.plan(cache, retry, [OLD], day)["todo"]:
            tried.append((day - TODAY).days)
            bt.record(cache, retry, OLD, {"skip": "error:option_bars_request"}, day)
        day += timedelta(days=1)
    assert tried == [0, 1, 3, 7, 15, 31]
    assert cache == {}  # never a permanent skip in the results
    meta = retry[bt._key(OLD)]
    assert meta["gave_up"] and meta["attempts"] == bt.MAX_FAIL_ATTEMPTS
    assert bt.plan(cache, retry, [OLD], day)["gave_up"] == 1


def test_success_after_failures_clears_the_retry_entry():
    cache, retry = {}, {}
    bt.record(cache, retry, OLD, {"skip": "error:chain_request"}, TODAY)
    bt.record(cache, retry, OLD, {"spot": 10, "strategies": {}}, TODAY + timedelta(days=1))
    assert retry == {} and "skip" not in cache[bt._key(OLD)]


def test_pending_retries_after_expiry_without_using_attempts():
    cache, retry = {}, {}
    r = {"skip": "expiry_pending", "expiry": "2026-11-20"}
    assert bt.record(cache, retry, OLD, r, TODAY) == "pending"
    meta = retry[bt._key(OLD)]
    assert meta["next_retry"] == "2026-11-21" and meta["attempts"] == 0
    assert bt.plan(cache, retry, [OLD], date(2026, 11, 20))["todo"] == []
    assert bt.plan(cache, retry, [OLD], date(2026, 11, 21))["todo"] == [OLD]


def test_retry_budget_puts_new_events_first():
    retry = {}
    stale = [_ev(i) for i in range(10)]
    for e in stale:
        bt.record({}, retry, e, {"skip": "error:x"}, TODAY - timedelta(days=5))
    fresh = [_ev(100 + i) for i in range(3)]
    p = bt.plan({}, retry, stale + fresh, TODAY, budget=4)
    assert p["todo"][:3] == fresh and len(p["todo"]) == 7
    assert p["new"] == 3 and p["retries"] == 4 and p["over_budget"] == 6


def test_repeated_runs_end_to_end(tmp_path, monkeypatch):
    """run() night after night: a good event is reconstructed once, a
    chain that never exists stops being requested, and nothing transient
    reaches the immutable results file."""
    good = dict(EVENT, ticker="GOOD")
    gone = dict(EVENT, ticker="GONE")
    flaky = dict(EVENT, ticker="FLAKY")
    calls = []

    def fake_reconstruct(e):
        calls.append(e["ticker"])
        if e["ticker"] == "GOOD":
            return {"spot": 10, "night_id": e["date"], "strategies": {}}
        if e["ticker"] == "GONE":
            return {"skip": "no_chain"}
        return {"skip": "error:option_bars_request"}

    monkeypatch.setattr(bt, "CACHE_PATH", str(tmp_path / "pnl.json"))
    monkeypatch.setattr(bt, "RETRY_PATH", str(tmp_path / "retry.json"))
    monkeypatch.setattr(bt, "OUT_PATH", str(tmp_path / "out.json"))
    monkeypatch.setattr(bt, "_events", lambda: [good, gone, flaky])
    monkeypatch.setattr(bt, "reconstruct", fake_reconstruct)
    monkeypatch.setattr(pg, "available", lambda: True)
    for n in range(40):
        bt.run(today=TODAY + timedelta(days=n))
    assert calls.count("GOOD") == 1
    assert calls.count("GONE") == bt.MAX_COVERAGE_ATTEMPTS
    assert calls.count("FLAKY") == bt.MAX_FAIL_ATTEMPTS
    pnl = json.loads((tmp_path / "pnl.json").read_text())
    assert sorted(r["event"]["ticker"] for r in pnl.values()) == ["GONE", "GOOD"]
    retry = json.loads((tmp_path / "retry.json").read_text())
    assert list(retry) == [bt._key(flaky)] and retry[bt._key(flaky)]["gave_up"]
    out = json.loads((tmp_path / "out.json").read_text())
    assert out["retry_queue"]["gave_up_after_failures"] == 1
