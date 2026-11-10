"""Latest Flow table numbers: break-even, last trade, price at the print."""

import json
from datetime import UTC, datetime

import uoa_scanner as us


def _ms(iso):
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def test_break_even_uses_the_displayed_average_price_not_polygon():
    # Polygon's break_even_price used another price and disagreed with
    # strike +/- the shown price; it must be ignored now.
    call = {"type": "call", "strike": 100.0, "px": 2.5, "spot": 98.0, "be_snap": 140.0}
    be, bd, _em, _cat = us._trade_plan(call)
    assert be == 102.5
    assert bd == round((102.5 / 98.0 - 1) * 100, 1)  # stock must RISE this much

    put = {"type": "put", "strike": 50.0, "px": 1.2, "spot": 52.0, "be_snap": 10.0}
    be, bd, _em, _cat = us._trade_plan(put)
    assert be == 48.8
    # positive = the stock still has to FALL that far for a put buyer
    assert bd == round((1 - 48.8 / 52.0) * 100, 1)
    assert bd > 0

    past = {"type": "put", "strike": 50.0, "px": 1.0, "spot": 45.0}
    assert us._trade_plan(past)[1] < 0  # already below break-even


def test_analyze_flow_reports_the_true_last_trade(monkeypatch):
    t0 = 1_762_790_400_000_000_000  # ns
    trades = [
        {"price": 2.0, "size": 10, "exchange": 1, "sip_timestamp": t0},
        {"price": 2.6, "size": 500, "exchange": 2, "sip_timestamp": t0 + 60_000_000_000},
        {"price": 2.3, "size": 5, "exchange": 3, "sip_timestamp": t0 + 120_000_000_000},
    ]
    monkeypatch.setattr(us.pg, "option_trades", lambda contract: list(trades))
    monkeypatch.setattr(us.pg, "option_quotes", lambda contract: [])
    flow = us.analyze_flow({"contract": "O:TST261218C00100000", "px": 2.5})
    assert flow["last_trade_price"] == 2.3  # the most recent fill, not the VWAP
    assert flow["trade_count"] == 3
    assert flow["biggest_print_ts"] == datetime.fromtimestamp(
        (t0 + 60_000_000_000) / 1e9, tz=UTC
    ).isoformat(timespec="seconds")


def test_spot_from_minute_bars_uses_the_print_minute():
    bars = [
        {"t": _ms("2026-11-10T15:00:00+00:00"), "c": 100.0},
        {"t": _ms("2026-11-10T15:01:00+00:00"), "c": 101.0},
        {"t": _ms("2026-11-10T15:05:00+00:00"), "c": 105.0},
    ]
    assert us.spot_from_minute_bars(bars, "2026-11-10T15:01:30+00:00") == 101.0
    assert us.spot_from_minute_bars(bars, "2026-11-10T15:05:00+00:00") == 105.0
    # 15:04:30 is 3.5 minutes after the 15:01 bar: no close-enough price
    assert us.spot_from_minute_bars(bars, "2026-11-10T15:04:30+00:00") is None
    assert us.spot_from_minute_bars(bars, "2026-11-10T14:59:00+00:00") is None
    assert us.spot_from_minute_bars([], "2026-11-10T15:01:30+00:00") is None


def test_attach_print_spots_one_fetch_per_underlying_day():
    calls = []
    bars = {"AAA": [{"t": _ms("2026-11-10T15:00:00+00:00"), "c": 50.0}], "SPX": []}

    def fetch(ticker, day):
        calls.append((ticker, day))
        return bars.get(ticker, [])

    rows = [
        {
            "underlying": "AAA",
            "spot": 55.0,
            "flow": {"biggest_print_ts": "2026-11-10T15:00:20+00:00"},
        },
        {
            "underlying": "AAA",
            "spot": 55.0,
            "flow": {"biggest_print_ts": "2026-11-10T15:00:40+00:00"},
        },
        {
            "underlying": "SPX",
            "spot": 6000.0,
            "flow": {"biggest_print_ts": "2026-11-10T15:00:40+00:00"},
        },
        {"underlying": "BBB", "spot": 10.0, "flow": {}},
    ]
    n = us.attach_print_spots(rows, workers=2, fetch=fetch)
    assert sorted(calls) == [("AAA", "2026-11-10"), ("SPX", "2026-11-10")]
    assert [r["spot_at_print"] for r in rows] == [50.0, 50.0, None, None]
    assert n == 2  # the scan-time spot is never copied in as "at print"


def test_emit_latest_publishes_print_spot_last_trade_and_vwap(tmp_path, monkeypatch):
    monkeypatch.setattr(us, "_BASE", str(tmp_path))
    monkeypatch.setattr(us, "LATEST_PATH", str(tmp_path / "docs" / "reports" / "uoa_latest.json"))
    monkeypatch.setattr(us, "OI_HISTORY_PATH", str(tmp_path / "data" / "oi.json"))
    row = {
        "underlying": "TST",
        "contract": "O:TST261218C00100000",
        "type": "call",
        "strike": 100.0,
        "expiry": "2026-12-18",
        "dte": 38,
        "spot": 98.0,
        "spot_at_print": None,
        "volume": 1000,
        "open_interest": 100,
        "vol_oi": 10.0,
        "premium": 250000,
        "px": 2.5,
        "px_basis": "vwap",
        "golden": False,
        "in_universe": False,
        "trade_score": 70,
        "tags": [],
        "flow": {"trade_count": 42, "last_trade_price": 2.7, "biggest_print_ts": None},
    }
    us.emit_latest([row])
    out = json.loads((tmp_path / "docs" / "reports" / "uoa_latest.json").read_text())["rows"][0]
    assert out["spot_at_print"] is None  # not silently the scan-time spot
    assert out["spot"] == 98.0
    assert out["last_price"] == 2.5 and out["vwap"] == 2.5 and out["price_basis"] == "vwap"
    assert out["last_trade_price"] == 2.7
    assert out["trade_count"] == 42
