"""Accuracy rules for the matched-control flow scorecard (uoa_scorecard.py).

Each test pins one rule from the 2026-11 scorecard audit: one row per stock,
ET day and direction; realistic share-trading costs; fallback control tiers
with a full contamination mask; controls stored by symbol and frozen; the
SPY comparison on the same rows; an entry-lag ladder on one common subset;
DST-correct Eastern time; point-in-time VIX regimes; share capacity math.
"""

import gzip
import json
import math
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest

import uoa_scorecard as us

ET = ZoneInfo("America/New_York")


def _ts(day, hh, mm=0):
    """UTC ISO stamp (ledger format) for an ET wall-clock time."""
    dt = datetime.combine(date.fromisoformat(day), time(hh, mm), tzinfo=ET)
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _row(tk, day, hh=10, mm=0, contract=None, **kw):
    base = {
        "flagged_at": _ts(day, hh, mm),
        "ticker": tk,
        "contract": contract or f"O:{tk}261220C00100000",
        "type": "call",
        "flow_side": "call_buyer",
        "direction": "bullish",
        "signal_type": "sweep",
        "trade_score": 70,
        "edge_adj": 0,
        "premium": 100_000,
        "volume": 100,
        "tags": [],
        "underlying_px_at_flag": 100.0,
    }
    base.update(kw)
    base["id"] = f"{base['contract']}_{base['flagged_at']}"
    return base


def _put(tk, day, hh=10, mm=0, **kw):
    kw.setdefault("contract", f"O:{tk}261220P00090000")
    return _row(tk, day, hh, mm, type="put", flow_side="put_buyer", direction="bearish", **kw)


# ── costs ────────────────────────────────────────────────────────────────


def test_liquidity_tier_boundaries():
    assert us.liquidity_tier(2e9) == "adv_1b_plus"
    assert us.liquidity_tier(1e9) == "adv_1b_plus"
    assert us.liquidity_tier(5e8) == "adv_100m_1b"
    assert us.liquidity_tier(5e7) == "adv_20m_100m"
    assert us.liquidity_tier(5e6) == "adv_under_20m"
    assert us.liquidity_tier(None) is None
    assert us.liquidity_tier(float("nan")) is None


def test_half_spread_is_clipped_into_its_tier_band():
    # SNOW/COIN-like: Corwin-Schultz says ~35bp, a $1.5B/day name quotes ~1-3bp
    assert us.half_spread(0.0035, 1.5e9) == pytest.approx(0.0002)
    assert us.half_spread(0.0, 1.5e9) == pytest.approx(0.0001)  # floor
    assert us.half_spread(float("nan"), 1.5e9) == pytest.approx(0.0002)  # missing -> cap
    assert us.half_spread(0.0007, 5e7) == pytest.approx(0.0007)  # inside the mid band
    assert us.half_spread(0.0100, 5e6) == pytest.approx(0.0025)  # small-cap cap
    assert us.half_spread(0.0010, None) is None


def test_cost_model_worked_examples():
    # $1.5B ADV, 3% daily vol, CS 35bp: half-spread 2bp, impact
    # 0.2*0.03*sqrt(1e4/1.5e9) = 1.549e-5 -> round trip 2*(2e-4+1.549e-5) = 0.0431%
    liquid = us.cost_model(0.0035, 0.03, 1.5e9)
    assert liquid == pytest.approx(0.0431, abs=1e-4)
    # the old uncapped Corwin-Schultz charge on the same name was ~16x larger
    old = 100 * 2 * (0.0035 + 0.2 * 0.03 * math.sqrt(1e4 / 1.5e9))
    assert old / liquid > 15
    # $5M ADV small cap, 4% vol, CS 60bp: 25bp half-spread + 3.58bp impact
    small = us.cost_model(0.0060, 0.04, 5e6)
    assert small == pytest.approx(100 * 2 * (0.0025 + 0.2 * 0.04 * math.sqrt(1e4 / 5e6)), abs=1e-4)
    assert us.cost_model(0.001, None, 5e7) is None
    assert us.cost_model(0.001, 0.02, 0) is None
    # every tier's round trip stays below 2 x (cap + impact)
    for _, adv, _, hi in us.LIQ_TIERS:
        a = max(adv, 1e6)
        c = us.cost_model(0.05, 0.02, a)
        assert (
            c is not None and c <= 100 * 2 * (hi + 0.2 * 0.02 * math.sqrt(1e4 / a)) + 1e-4
        )  # 4-dp rounding


def test_capacity_solves_round_trip_impact_after_spread():
    # edge 1%, half-spread 2bp, vol 2%, ADV $1B:
    # room = 0.01 - 0.0004 = 0.0096 ; Q = 1e9 * (0.0096 / (4*0.2*0.02))**2 = 3.6e8
    q = us.capacity_q(1.0, 0.0002, 0.02, 1e9)
    assert q == pytest.approx(3.6e8, rel=1e-9)
    # at Q the ROUND-TRIP impact is exactly half the post-spread edge
    assert q is not None
    assert 2 * 0.2 * 0.02 * math.sqrt(q / 1e9) == pytest.approx(0.5 * 0.0096)
    # spread alone eats the edge -> no capacity
    assert us.capacity_q(0.03, 0.0002, 0.02, 1e9) is None
    assert us.capacity_q(1.0, None, 0.02, 1e9) is None


# ── Eastern time ─────────────────────────────────────────────────────────


def test_session_uses_real_eastern_time_after_dst_ends():
    # 2026-11-03 20:30 UTC = 3:30 PM EST (regular). A fixed UTC-4 offset
    # would call it 4:30 PM (after the close).
    assert us._session("2026-11-03T20:30:00+00:00") == "regular"
    assert us._session("2026-11-03T21:30:00+00:00") == "after_close"
    # in summer (EDT) the same UTC hour is after the close
    assert us._session("2026-07-14T20:30:00+00:00") == "after_close"
    assert us._session("2026-11-03T13:00:00+00:00") == "pre_open"  # 8 AM EST


# ── collapse: one row per stock, ET day and direction ────────────────────


def test_build_units_collapses_relogs_contracts_and_counts():
    d1, d2 = "2026-10-05", "2026-10-06"
    c1 = "O:ABC261220C00100000"
    c2 = "O:ABC261220C00110000"
    ledger = [
        # c1 re-logged by three scans (the after-close one too)
        _row("ABC", d1, 10, 0, c1, premium=100_000, trade_score=70),
        _row("ABC", d1, 10, 30, c1, premium=150_000, trade_score=75),
        _row("ABC", d1, 17, 0, c1, premium=180_000, trade_score=72),
        # a second contract on the same stock, day and direction
        _row("ABC", d1, 11, 0, c2, premium=50_000, trade_score=85, signal_type="golden_sweep"),
        # the same stock that day, but puts bought: a separate bearish unit
        _put("ABC", d1, 12, 0, premium=40_000, tags=["Into ERN"]),
        # same contract the next day: a new unit
        _row("ABC", d2, 10, 0, c1),
        # premium selling / hedges have no stock direction: not graded
        _row("ABC", d1, 13, 0, "O:ABC261220P00080000", flow_side="put_seller", direction="income"),
        _row("XYZ", d1, 13, 0, flow_side="mixed", direction="hedge"),
    ]
    units, counts = us.build_units(ledger)
    by = {u["key"]: u for u in units}
    assert set(by) == {f"ABC|{d1}|bullish", f"ABC|{d1}|bearish", f"ABC|{d2}|bullish"}
    bull = by[f"ABC|{d1}|bullish"]
    assert bull["n_alerts"] == 2  # two contracts
    assert bull["n_rows"] == 4  # 3 re-logs + 1
    assert bull["first_ts"] == _ts(d1, 10, 0)  # earliest flag
    assert bull["score"] == 85  # highest score any alert reached
    assert bull["golden"] is True
    assert bull["ern"] is False
    # day premium: each contract's largest cumulative premium, summed
    assert bull["prem"] == 180_000 + 50_000
    assert by[f"ABC|{d1}|bearish"]["ern"] is True
    assert by[f"ABC|{d1}|bearish"]["sign"] == -1.0
    assert counts == {
        "ledger_directional_rows": 6,
        "unique_alerts": 4,
        "ticker_days": 3,
        "distinct_ticker_dates": 2,
        "first_flagged_after_close_pct": 0.0,
    }


def test_units_use_the_eastern_date_not_utc():
    # 8:30 PM EST on Nov 3 is Nov 4 in UTC — it belongs to Nov 3
    late = _row("ABC", "2026-11-03", 20, 30)
    assert late["flagged_at"].startswith("2026-11-04")
    units, counts = us.build_units([late])
    assert units[0]["et_day"] == "2026-11-03"
    assert units[0]["session"] == "after_close"
    assert counts["first_flagged_after_close_pct"] == 100.0


def test_bad_price_basis_rows_are_dropped():
    units, _ = us.build_units([_row("ABC", "2026-10-05", basis_suspect=True)])
    assert units == []


def test_flagged_by_day_includes_every_flow_side():
    d = "2026-10-05"
    ledger = [
        _row("BUY", d),
        _row("SELL", d, flow_side="put_seller", direction="income"),
        _row("HEDGE", d, flow_side="mixed", direction="hedge"),
        _row("LATE", "2026-11-03", 20, 30),  # UTC Nov 4, ET Nov 3
    ]
    f = us.flagged_by_day(ledger)
    assert f[d] == {"BUY", "SELL", "HEDGE"}
    assert f["2026-11-03"] == {"LATE"}


# ── matching ─────────────────────────────────────────────────────────────


def _days(n, end="2026-10-30"):
    out, d = [], date.fromisoformat(end)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return sorted(out)


def _matrix(tickers, n_days, seed=1, drift=None):
    rng = np.random.default_rng(seed)
    n_t = len(tickers)
    rets = rng.normal(0, 0.02, (n_days, n_t))
    if drift:
        for j, g in drift.items():
            rets[:, j] += g
    C = 100 * np.cumprod(1 + rets, axis=0)
    V = np.full_like(C, 1e6)
    return C, V, C * 1.01, C * 0.99, C * 1.0


def _setup(caps_sectors, n_days=30, seed=1):
    tickers = sorted([*caps_sectors, "SPY"])
    ti = {t: i for i, t in enumerate(tickers)}
    days = _days(n_days)
    C, V, H, L, O = _matrix(tickers, n_days, seed)
    meta = {t: {"mkt_cap": c, "sector": s} for t, (c, s) in caps_sectors.items()}
    return days, tickers, ti, C, V, H, L, O, meta


def _assign(units, setup, matched, flagged, today="2027-01-01"):
    days, tickers, ti, C, V, H, L, _, meta = setup
    return us.assign_controls(units, days, tickers, ti, C, V, H, L, meta, matched, flagged, today)


def test_fallback_tiers_and_full_contamination_mask():
    # NVDA-like: one same-sector peer, a handful of cross-sector peers
    world = {
        "SIG": (1000e9, "Semis"),
        "S1": (800e9, "Semis"),  # the only same-sector peer: sector tiers fail
        "X0": (500e9, "Soft"),
        "X1": (2000e9, "Soft"),
        "X2": (2500e9, "Soft"),
        "X3": (1500e9, "Soft"),
        "FAR1": (6000e9, "Soft"),  # 6x: only inside the 10x band
        "FAR2": (150e9, "Banks"),  # 6.7x smaller: only inside the 10x band
    }
    setup = _setup(world)
    day = setup[0][-1]
    units, _ = us.build_units([_row("SIG", day)])
    # X2 has only premium-selling flow and X3 only a hedge print that day:
    # neither may be a control
    ledger = [
        _row("SIG", day),
        _row("X2", day, flow_side="put_seller", direction="income"),
        _row("X3", day, flow_side="mixed", direction="hedge"),
    ]
    matched = {}
    n = _assign(units, setup, matched, us.flagged_by_day(ledger))
    rec = matched[f"SIG|{day}|bullish"]
    assert n == 1
    # only 3 clean peers within 3x (S1, X0, X1) -> the 10x tier
    assert rec["t"] == "any_sector_10x"
    assert set(rec["c"]) == {"S1", "X0", "X1", "FAR1", "FAR2"}
    assert all(isinstance(c, str) for c in rec["c"])  # symbols, not matrix positions

    # a buyers-only mask (the v1 bug) would have let the seller and the
    # hedge in as controls, landing in the 3x cross-sector tier
    matched2 = {}
    _assign(units, setup, matched2, us.flagged_by_day(ledger[:1]))
    rec2 = matched2[f"SIG|{day}|bullish"]
    assert rec2["t"] == "any_sector_3x"
    assert set(rec2["c"]) == {"S1", "X0", "X1", "X2", "X3"}


def test_same_sector_tier_is_preferred_and_share_classes_excluded():
    world = {
        "GOOGL": (3500e9, "Soft"),
        "GOOG": (3490e9, "Soft"),  # same company, other share class
        **{f"S{i}": (c, "Soft") for i, c in enumerate([1500e9, 2000e9, 2500e9, 4000e9, 5000e9])},
        "X0": (3400e9, "Semis"),
    }
    setup = _setup(world)
    day = setup[0][-1]
    units, _ = us.build_units([_row("GOOGL", day)])
    matched = {}
    _assign(units, setup, matched, {})
    rec = matched[f"GOOGL|{day}|bullish"]
    assert rec["t"] == "sector_3x"
    assert "GOOG" not in rec["c"]
    assert set(rec["c"]) == {"S0", "S1", "S2", "S3", "S4"}


def test_share_class_twins_by_symbol():
    tk = ["BRK.A", "BRK.B", "FOX", "FOXA", "NWSA", "NWS", "AAPL"]
    tw = us.share_class_twins(tk)
    ix = {t: i for i, t in enumerate(tk)}
    assert ix["BRK.B"] in tw[ix["BRK.A"]]
    assert ix["FOX"] in tw[ix["FOXA"]] and ix["FOXA"] in tw[ix["FOX"]]
    assert ix["NWS"] in tw[ix["NWSA"]]
    assert ix["AAPL"] not in tw


def test_unmatchable_units_record_a_reason():
    setup = _setup({"SIG": (1000e9, "Semis"), "S1": (900e9, "Semis")})
    day = setup[0][-1]
    units, _ = us.build_units([_row("SIG", day), _row("NOMETA", day)])
    matched = {}
    assert _assign(units, setup, matched, {}) == 0
    assert matched[f"SIG|{day}|bullish"]["why"] == "too_few_clean_peers"
    assert matched[f"NOMETA|{day}|bullish"]["why"] == "no_price_history"
    cov = us.match_coverage(units, matched)
    assert cov["units_attempted"] == 2 and cov["assigned"] == 0 and cov["coverage_pct"] == 0.0


def test_market_cap_is_point_in_time():
    # PEER is 6x the signal's cap today (outside both sector bands), but its
    # price has since risen ~2.4x: on the signal date it was ~2.5x (inside 3x)
    world = {"SIG": (100e9, "Semis"), "PEER": (600e9, "Semis")}
    world |= {f"S{i}": (100e9 * (1 + (i + 1) / 10), "Semis") for i in range(4)}
    setup = _setup(world, seed=2)
    days, tickers, ti, C, *_ = setup
    n = len(days)
    for t in tickers:
        C[:, ti[t]] = 100.0 * (1 + 0.01 * np.cos(np.arange(n) + ti[t]))  # flat, finite vol
    C[:, ti["PEER"]] = np.linspace(30, 100, n) * (1 + 0.01 * np.sin(np.arange(n)))
    di = 5
    day = days[di]
    units, _ = us.build_units([_row("SIG", day)])
    matched = {}
    _assign(units, setup, matched, {})
    rec = matched[f"SIG|{day}|bullish"]
    assert rec["t"] == "sector_3x"  # today's caps would have needed the 10x tier
    assert "PEER" in rec["c"]


def test_assignments_freeze_and_unsettled_days_wait():
    world = {f"S{i}": (100e9 * (1 + i / 10), "Semis") for i in range(8)}
    setup = _setup(world)
    days = setup[0]
    frozen = {"d": days[-2], "t": "sector_3x", "c": ["Z1", "Z2", "Z3", "Z4", "Z5"], "e": 0}
    key = f"S0|{days[-2]}|bullish"
    matched = {key: dict(frozen)}
    future = (date.fromisoformat(days[-1]) + timedelta(days=3)).isoformat()
    units, _ = us.build_units([_row("S0", days[-2]), _row("S1", future)])
    assert _assign(units, setup, matched, {}) == 0
    assert matched[key] == frozen  # never reassigned
    assert f"S1|{future}|bullish" not in matched  # waits for its bars


def test_grading_reads_controls_by_symbol_whatever_the_ticker_order():
    """v1 stored controls as positions in a ticker list that changed every
    night; v2 stores symbols, so adding a ticker cannot change a grade."""
    world = {f"S{i}": (100e9 * (1 + i / 10), "Semis") for i in range(8)}
    setup = _setup(world)
    days, tickers, ti, C, *_ = setup
    di = 3
    units, _ = us.build_units([_row("S0", days[di])])
    matched = {}
    _assign(units, setup, matched, {})
    g1 = us.grade_units(units, matched, days, ti, C)[0]["x"]

    tickers2 = sorted([*tickers, "AAA"])  # a new ticker sorts first
    ti2 = {t: i for i, t in enumerate(tickers2)}
    C2 = np.full((len(days), len(tickers2)), 50.0)
    for t, j in ti.items():
        C2[:, ti2[t]] = C[:, j]
    g2 = us.grade_units(units, matched, days, ti2, C2)[0]["x"]
    assert g1 == g2 and 5 in g1


def test_excess_is_direction_signed_and_spy_uses_the_same_row():
    days = _days(8)
    tickers = ["C1", "C2", "C3", "C4", "C5", "SIG", "SPY"]
    ti = {t: i for i, t in enumerate(tickers)}
    C = np.full((8, 7), 100.0)
    C[6, ti["SIG"]] = 110.0  # +10% over 5 sessions from di=1
    for c in ("C1", "C2", "C3", "C4", "C5"):
        C[6, ti[c]] = 102.0  # controls +2%
    C[6, ti["SPY"]] = 101.0  # SPY +1%
    rec = {"d": days[1], "t": "sector_3x", "c": ["C1", "C2", "C3", "C4", "C5"]}
    bull = {"key": "b", "tk": "SIG", "sign": 1.0}
    bear = {"key": "s", "tk": "SIG", "sign": -1.0}
    rows = us.grade_units([bull, bear], {"b": rec, "s": rec}, days, ti, C)
    assert rows[0]["x"][5] == 8.0 and rows[0]["spy"][5] == 9.0
    assert rows[1]["x"][5] == -8.0 and rows[1]["spy"][5] == -9.0
    assert 10 not in rows[0]["x"]  # not matured


# ── entry-lag ladder ─────────────────────────────────────────────────────


def _bars(day, until=(16, 30), price=None):
    """Minute bars 9:30 ET -> `until`, close = price(minute index)."""
    start = datetime.combine(date.fromisoformat(day), time(9, 30), tzinfo=ET)
    end = datetime.combine(date.fromisoformat(day), time(*until), tzinfo=ET)
    out, t, i = {}, start, 0
    while t <= end:
        out[str(int(t.timestamp() * 1000))] = price(i) if price else 100.0 + i / 100
        t += timedelta(minutes=1)
        i += 1
    return out


def test_ladder_counts_ticker_days_on_one_common_subset():
    days = _days(12, end="2026-11-06")
    ti = {"ABC": 0, "XYZ": 1}
    C = np.full((12, 2), 100.0)
    O = np.full((12, 2), 103.0)
    C[5, 0] = 110.0  # exit for di=0

    def unit(tk, di, hh, mm, sign=1.0, x5=8.0, dirn="bullish"):
        day = days[di]
        return {
            "key": f"{tk}|{day}|{dirn}",
            "tk": tk,
            "et_day": day,
            "day": day,
            "di": di,
            "si": ti[tk],
            "sign": sign,
            "dir": dirn,
            "first_ts": _ts(day, hh, mm),
            "first_px": 99.0,
            "x": {5: x5},
            "ern": False,
        }

    a = unit("ABC", 0, 10, 0)
    a_bear = unit("ABC", 0, 10, 30, sign=-1.0, x5=-8.0, dirn="bearish")
    late = unit("XYZ", 1, 15, 20)  # after 3:00 PM: no +60m bar before the close
    after = unit("XYZ", 2, 17, 0)  # first flagged after the close
    nobars = unit("XYZ", 3, 10, 0)
    short = unit("ABC", 4, 14, 59)  # its bars stop at 15:30 -> no +60m bar
    old = unit("ABC", 0, 10, 0)
    old["et_day"] = old["day"] = "2026-01-02"
    cands = us.lag_candidates([a, a_bear, late, after, nobars, short, old], cutoff=days[0])
    assert {c["key"] for c in cands} == {a["key"], a_bear["key"], nobars["key"], short["key"]}

    cache = {
        f"ABC|{days[0]}": _bars(days[0]),
        f"ABC|{days[4]}": _bars(days[4], until=(15, 30)),
    }
    out = us.compute_ladder(cands, cache, days, C, O)
    assert out["units"] == 2
    assert out["ticker_days"] == 1  # both directions on one stock-day
    assert {v["n"] for v in out["ladder"].values()} == {2}  # same n on every rung
    assert out["excluded"] == {"no minute bars": 1, "missing an entry price": 1}

    # Worked example. Bullish: control leg = 100*(110/100-1) - 8 = +2, so
    # EV(entry) = 100*(110/entry-1) - 2. Bearish: control leg = -10+8 = -2,
    # EV(entry) = -100*(110/entry-1) + 2. Minute bar i closes at 100+i/100:
    # the 10:00 bar is i=30 (100.30), 10:30 is i=60 (100.60).
    def bull(e):
        return 100 * (110 / e - 1) - 2

    def bear(e):
        return -100 * (110 / e - 1) + 2

    lad = out["ladder"]
    assert lad["+0m"]["avg"] == pytest.approx((bull(100.30) + bear(100.60)) / 2, abs=1e-3)
    assert lad["+15m"]["avg"] == pytest.approx((bull(100.45) + bear(100.75)) / 2, abs=1e-3)
    assert lad["+60m"]["avg"] == pytest.approx((bull(100.90) + bear(101.20)) / 2, abs=1e-3)
    assert lad["logged_snapshot"]["avg"] == pytest.approx((bull(99.0) + bear(99.0)) / 2, abs=1e-3)
    assert lad["next_open"]["avg"] == pytest.approx((bull(103.0) + bear(103.0)) / 2, abs=1e-3)


def test_bar_at_ignores_after_hours_bars():
    day = "2026-11-06"
    bars = _bars(day, until=(17, 0))
    close = us._close_ms(day)
    t1530 = int(
        datetime.combine(date.fromisoformat(day), time(15, 30), tzinfo=ET).timestamp() * 1000
    )
    assert us._bar_at(bars, t1530, close) is not None
    assert us._bar_at(bars, t1530 + 60 * 60_000, close) is None  # 16:30 is after the close


# ── regimes ──────────────────────────────────────────────────────────────


def test_vix_regimes_are_point_in_time():
    rng = np.random.default_rng(5)
    vals = list(15 + 5 * rng.random(200))
    dates = [f"d{i:03d}" for i in range(200)]
    early = us.vix_regimes_pit(dates[:150], vals[:150])
    # a later VIX spike must not relabel any earlier day
    spiked = vals[:150] + [80.0] * 50
    full = us.vix_regimes_pit(dates, spiked)
    assert all(full[d] == early[d] for d in early)
    assert "d000" not in full  # not enough history yet
    assert full["d199"] == "high"


# ── stats helpers kept from v1 ───────────────────────────────────────────


def test_scorecard_spearman_and_bootstrap():
    assert us._spearman([1, 2, 3, 4], [2, 4, 6, 8]) == 1.0
    assert us._spearman([1, 2, 3, 4], [8, 6, 4, 2]) == -1.0
    rows = [("c" + str(i % 10), float(i % 7) - 3) for i in range(400)]
    b = us.cluster_boot(rows, iters=200)
    assert b.get("clusters") == 10 and "ci95" in b
    assert b.get("n_eff") is not None


def test_scorecard_ern_bucket():
    assert us._ern_bucket("2026-09-10", "2026-09-08") == "0-5"
    assert us._ern_bucket("2026-09-25", "2026-09-08") == "6-20"
    assert us._ern_bucket(None, "2026-09-08") == "unk"


# ── the whole nightly build, offline ─────────────────────────────────────


def _world(n_days=60, n_tk=60, per_day=22, seed=3):
    rng = np.random.default_rng(seed)
    end = datetime.now(ET).date() - timedelta(days=1)
    days = _days(n_days, end=end.isoformat())
    names = [f"T{i:02d}" for i in range(n_tk)]
    tickers = sorted([*names, "SPY"])
    ti = {t: i for i, t in enumerate(tickers)}
    C, V, H, L, O = _matrix(tickers, n_days, seed)
    V[:] = rng.uniform(2e5, 5e6, V.shape)
    sectors = ["Semis", "Soft", "Banks"]
    meta = {
        t: {
            "mkt_cap": float(np.exp(rng.normal(np.log(2e10), 0.6))),
            "sector": sectors[i % 3],
            "earnings_date": None,
        }
        for i, t in enumerate(names)
    }
    ledger = []
    for di, day in enumerate(days):
        pick = rng.choice(names, per_day + 5, replace=False)
        for k, tk in enumerate(pick):
            px = float(C[di, ti[tk]])
            if k >= per_day:  # sellers: contaminate controls, never graded
                ledger.append(_row(tk, day, 11, 0, flow_side="put_seller", direction="income"))
                continue
            mk = _put if rng.random() < 0.35 else _row
            hh = int(rng.choice([10, 11, 13, 14, 16, 17]))
            kw = {
                "trade_score": int(rng.integers(55, 100)),
                "edge_adj": int(rng.integers(-3, 4)),
                "underlying_px_at_flag": round(px * 0.999, 2),
                "tags": ["Into ERN"] if rng.random() < 0.3 else [],
                "signal_type": "golden_sweep" if rng.random() < 0.1 else "sweep",
            }
            ledger.append(mk(tk, day, hh, 5, **kw))
            ledger.append(mk(tk, day, hh, 35, **kw))  # scan re-log
            ledger.append(mk(tk, day, 19, 0, **kw))  # after-close re-log
    vd = _days(330, end=end.isoformat())
    vv = list(15 + 8 * rng.random(len(vd)))
    return days, tickers, ti, (C, V, H, L, O), meta, ledger, (vd, vv)


def _minute_get(days, ti, C):
    def get(path, params=None):
        parts = path.split("/")  # /v2/aggs/ticker/{tk}/range/1/minute/{day}/{day}
        tk, day = parts[4], parts[-1]
        di = days.index(day)
        base = float(C[di, ti[tk]])
        return {
            "results": [
                {"t": int(t), "c": round(base * (1 + 0.0005 * (k % 7 - 3)), 4)}
                for k, t in enumerate(_bars(day, until=(16, 30)))
            ]
        }

    return get


@pytest.fixture
def offline(tmp_path, monkeypatch):
    days, tickers, ti, mats, meta, ledger, vix = _world()
    C, V, H, L, O = mats
    real_load = us._load
    monkeypatch.setattr(us, "MATCH_PATH", str(tmp_path / "uoa_matched_v2.json.gz"))
    monkeypatch.setattr(us, "OUT_PATH", str(tmp_path / "uoa_scorecard.json"))
    monkeypatch.setattr(us, "LAG_CACHE", str(tmp_path / "lag.json"))
    monkeypatch.setattr(us.pg, "available", lambda: True)
    monkeypatch.setattr(us.pg, "_get", _minute_get(days, ti, C))
    monkeypatch.setattr(us, "load_ledger", lambda: ledger)
    monkeypatch.setattr(
        us, "_load", lambda p, d=None: meta if "uoa_meta_cache" in p else real_load(p, d)
    )
    monkeypatch.setattr(us, "_fetch_vix", lambda: vix)
    monkeypatch.setattr(
        us,
        "build_matrix",
        lambda pool, today: (days, tickers, ti, C.copy(), V, H, L, O),
    )
    return tmp_path, meta


def test_offline_nightly_build_is_internally_consistent(offline):
    tmp_path, meta = offline
    res = us.run(lag_budget=10_000)
    cnt = res["counts"]
    assert cnt["unique_alerts"] < cnt["ledger_directional_rows"]  # re-logs collapsed
    assert cnt["ticker_days"] <= cnt["unique_alerts"]
    m = res["matched"]
    assert m["graded_5d"] == res["pockets"]["overall"]["n"] == m["n_unique"]  # dq_audit rule
    assert m["coverage_pct"] > 90
    assert sum(m["by_tier"].values()) == m["assigned"]
    hb = res["headline"]
    assert hb["n"] == res["pockets"]["overall"]["n"]  # same rows
    assert hb["benchmark_gap_5d"] == pytest.approx(hb["vs_spy_5d"] - hb["matched_5d"], abs=2e-3)
    for b in res["calibration"]["reliability_rolling90"]:
        lo, hi = b["ci95_pct"]
        assert 0 <= lo <= hi <= 100 and lo - 10 <= b["hit_pct"] <= hi + 10  # all in percent
    lag = res["entry_lag"]
    assert lag["units"] > 0
    assert {v["n"] for v in lag["ladder"].values()} == {lag["units"]}
    assert lag["ticker_days"] <= lag["units"]
    assert lag["window"]["from"] >= (datetime.now(ET).date() - timedelta(days=46)).isoformat()
    assert "stock, not option" in res["book_sim"]["overall"]["note"]
    assert "stock, not option" in res["cost_model"]["label"]
    ov = res["pockets"]["overall"]
    # each ticker-day stands for 3 logged rows here (two scan re-logs); the
    # hypothesis lab divides n_eff by n_rows because it grades raw rows
    assert ov["n_rows"] == 3 * ov["n_alerts"] and ov["n_alerts"] >= ov["n"]
    assert set(ov["by_liquidity"]) == {t[0] for t in us.LIQ_TIERS}
    assert "by_spread" not in ov
    # modelled costs are now a small fraction of a percent per round trip
    assert ov["avg"] - ov["net"]["avg"] < 0.6
    for cap in res["capacity"].values():
        ok = cap.get("edge_nonpositive") or cap.get("spread_eats_edge")
        assert ok or "SHARE capacity" in cap["note"]
    pub = json.loads((tmp_path / "uoa_scorecard.json").read_text(encoding="utf-8"))
    assert pub["version"] == "uoa_scorecard_v2"


def test_offline_rerun_never_reassigns(offline):
    tmp_path, meta = offline
    us.run(no_lag=True)
    with gzip.open(tmp_path / "uoa_matched_v2.json.gz", "rt", encoding="utf-8") as f:
        first = json.load(f)
    assert first["v"] == 2
    # meta drifts overnight (sector relabel, caps move): frozen records must not
    for t in meta:
        meta[t]["sector"] = "Other"
        meta[t]["mkt_cap"] *= 3
    us.run(no_lag=True)
    with gzip.open(tmp_path / "uoa_matched_v2.json.gz", "rt", encoding="utf-8") as f:
        second = json.load(f)
    assert second["units"] == first["units"]


def test_dry_run_writes_nothing(offline):
    tmp_path, _ = offline
    us.run(dry=True)
    assert not (tmp_path / "uoa_matched_v2.json.gz").exists()
    assert not (tmp_path / "uoa_scorecard.json").exists()
    assert not (tmp_path / "lag.json").exists()


def test_units_use_the_days_first_flag_like_the_track_record():
    base = {"ticker": "ABC", "contract": "O:ABC261120C00100000", "type": "call"}
    ledger = [
        # first flag of the day: mixed prints (no direction) ...
        dict(
            base,
            id="a",
            flagged_at="2026-10-05T15:00:00+00:00",
            flow_side="mixed",
            direction="hedge",
        ),
        # ... a later re-log reads as a buyer: still the SAME alert, ungraded
        dict(
            base,
            id="b",
            flagged_at="2026-10-05T18:00:00+00:00",
            flow_side="call_buyer",
            direction="bullish",
        ),
        # a different contract whose first flag is a buyer counts
        dict(
            base,
            id="c",
            contract="O:ABC261120C00110000",
            flagged_at="2026-10-05T16:00:00+00:00",
            flow_side="call_buyer",
            direction="bullish",
        ),
    ]
    units, counts = us.build_units(ledger)
    assert counts["unique_alerts"] == 1
    assert len(units) == 1 and units[0]["n_alerts"] == 1
