"""Desk accuracy fixes (Nov 16 audit, section C6), pipeline side.

- Funds (ETFs, ETNs, leveraged / inverse products) are flagged in the swing
  grade batch so the Desk keeps them out of stock-only lists.
- The overnight-flow board counts a print as "OI-confirmed" only when the
  morning OI check passed, not merely when OI has settled."""

import carryover_flow
import swing_report
from uoa_scanner import is_fund

# Names exactly as Polygon reported them on the live Desk, Nov 16.
FUNDS = {
    "ZSL": "ProShares UltraShort Silver",
    "NVDQ": "T-Rex 2X Inverse NVIDIA Daily Target ETF",
    "NVD": "GraniteShares ETF Trust GraniteShares 2x Short NVDA Daily ETF",
    "MUU": "Direxion Shares ETF Trust Direxion Daily MU Bull 2X ETF",
    "MULL": "GraniteShares 2x Long MU Daily ETF",
    "TECL": "Direxion Daily Technology Bull 3x ETF",
    "USD": "ProShares Ultra Semiconductors",
    "XSD": "State Street SPDR S&P Semiconductor ETF",
    "FNGU": "MicroSectors FANG+ 3x Leveraged ETNs due February 17, 2045",
    "QTEC": "First Trust NASDAQ-100 Technology  Index Fund",
}
STOCKS = {
    "TENB": "Tenable Holdings, Inc.",
    "UCTT": "Ultra Clean Holdings, Inc.",
    "MMM": "3M Company",
    "TXG": "10x Genomics, Inc. Class A Common Stock",
    "TREX": "Trex Company, Inc.",
    "ET": "ENERGY TRANSFER LP",
    "NTRS": "Northern Trust Corporation",
    "IVZ": "Invesco Ltd.",
}


def test_funds_are_recognised_by_name():
    for t, name in FUNDS.items():
        assert is_fund(t, name), t


def test_stocks_are_not_funds():
    for t, name in STOCKS.items():
        assert not is_fund(t, name), t


def test_curated_list_and_security_type_win():
    assert is_fund("SPY")  # curated EXCLUDE_ETFS, no name needed
    assert is_fund("ABCD", "Plain Name Inc", "ETF")  # Polygon type
    assert is_fund("ABCD", "Plain Name Inc", "etn")
    assert not is_fund("ABCD", "Plain Name Inc", "CS")


def test_stock_type_beats_fund_wording():
    # Polygon says common stock / ADR: the name can't override that
    assert not is_fund("FCI", "Fund Corp Inc.", "CS")
    assert not is_fund("ABCD", "Leveraged Growth Fund Holdings ADR", "ADRC")
    assert is_fund("SPY", "SPDR S&P 500 ETF Trust", "CS")  # curated list still wins


ISSUER_FUNDS = {
    "ASHR": "Xtrackers Harvest CSI 300 China A-Shares",
    "VT": "Vanguard Total World Stock Index",
    "KWEB": "KraneShares CSI China Internet",
    "SMH": "VanEck Semiconductor",
    "URA": "Global X Uranium",
    "QQQ": "Invesco QQQ Trust, Series 1",
    "DBC": "Invesco DB Commodity Index Tracking",
    "DXJ": "WisdomTree Japan Hedged Equity",
    "FTXL": "First Trust Nasdaq Semiconductor ETF",
    "ARKK": "ARK Innovation ETF",
}
ISSUER_COMPANIES = {
    "BLK": "BlackRock, Inc.",
    "IVZ": "Invesco Ltd.",
    "WT": "WisdomTree, Inc.",
    "VNGD": "Vanguard Health Holdings Corp",
    "GLBX": "Global X Holdings Inc",
}


def test_issuer_names_without_etf_wording():
    for t, name in ISSUER_FUNDS.items():
        assert is_fund(t, name), t  # no type from Polygon
    for t, name in ISSUER_COMPANIES.items():
        assert not is_fund(t, name), t


def _metrics(t):
    return {
        "ticker": t,
        "ext": 1.0,
        "price": 10.0,
        "chg_day": 1.234,
        "rvol": 1.0,
        "spark": [1, 2],
    }


def test_grade_cell_flags_funds_only():
    fund = swing_report._grade_cell(_metrics("TECL"), {"name": FUNDS["TECL"]})
    stock = swing_report._grade_cell(_metrics("TENB"), {"name": STOCKS["TENB"]})
    assert fund["etf"] == 1
    assert "etf" not in stock
    assert stock["t"] == "TENB" and stock["chg"] == 1.23 and stock["n"] == STOCKS["TENB"]


def _print(contract, oi, vol, prem=5_000_000):
    return {
        "contract": contract,
        "ticker": contract[2:6].rstrip("0123456789"),
        "premium": prem,
        "open_interest": oi,
        "volume": vol,
        "flow_side": "call_buyer",
    }


def test_confirmed_count_counts_passed_checks_not_settled_oi(monkeypatch):
    # META held 99.8% of its print; NVDA kept 13%; UPS saw OI fall; one
    # contract has no settled OI yet.
    prints = [
        _print("O:META261218C00700000", 1185, 2503),
        _print("O:NVDA261218C00200000", 10963, 103445),
        _print("O:UPSS261218P00090000", 13676, 80875),
        _print("O:COPP261218C00100000", 100, 1000),
    ]
    today_oi = {
        "O:META261218C00700000": 3684,
        "O:NVDA261218C00200000": 23946,
        "O:UPSS261218P00090000": 66,
        "O:COPP261218C00100000": None,
    }
    monkeypatch.setattr(carryover_flow, "_load_session_prints", lambda d: ("2026-11-13", prints))
    monkeypatch.setattr(
        carryover_flow.pg,
        "option_contract",
        lambda tk, c: None if today_oi[c] is None else {"open_interest": today_oi[c]},
    )
    out = carryover_flow.build()
    assert out["total"] == 4
    assert out["settled_count"] == 3
    assert out["confirmed_count"] == 1 == out["held_count"]
    meta = next(c for c in out["contracts"] if c["ticker"] == "META")
    # held ~100% and "beyond the print" is about zero: nobody else joined
    assert meta["held"] is True and meta["net_new_oi"] == -4


# Review fixes, round 2 -----------------------------------------------------
def test_llc_and_lp_names_are_stocks_but_commodity_pools_are_funds():
    for name in (
        "Vanguard Natural Resources LLC",
        "Global X Partners, L.P.",
        "Invesco Mortgage Holdings L.L.C.",
        "VanEck Resources LP",
    ):
        assert not is_fund("ABCD", name), name
    assert is_fund("USO", "United States Oil Fund, LP")
    assert is_fund("UNG", "United States Natural Gas Fund LP")


def test_meta_cache_without_security_type_is_refetched():
    import datetime as dt

    today = dt.date(2026, 11, 16)
    old = {"fetched": "2026-11-10", "market_cap": 5e9, "name": "X"}
    assert not swing_report._meta_is_fresh(old, today)  # pre-dates "type"
    assert swing_report._meta_is_fresh(dict(old, type="CS"), today)
    assert swing_report._meta_is_fresh(dict(old, type=""), today)  # Polygon had none


def test_oi_confirmed_rule_is_half_the_print():
    f = carryover_flow.oi_confirmed
    assert f(500, 1000) is True  # exactly half
    assert f(499, 1000) is False
    assert f(0, 1000) is False and f(-50, 1000) is False
    assert f(-50, None) is False  # OI fell: fails whatever the size
    assert f(900, None) is None  # rose, but the print size is unknown
    assert f(None, 1000) is None  # no reading yet


def test_daily_brief_uses_the_same_oi_rule():
    import datetime as dt

    import brief_adapters
    import brief_compose
    import brief_time

    # OI rose, but by only 1% of the print: the site says not confirmed, so
    # the brief must too (it used to say CONFIRMED for any rise)
    thin = brief_adapters._contract({"ticker": "X", "type": "call", "oi_delta": 10, "volume": 1000})
    assert thin["oi_confirmed"] is False and thin["status"] == "failed"
    held = brief_adapters._contract(
        {"ticker": "X", "type": "call", "oi_delta": 600, "volume": 1000}
    )
    assert held["oi_confirmed"] is True and held["status"] == "confirmed"

    now = dt.datetime(2026, 7, 22, 9, 0, tzinfo=brief_time.ET)
    base = {"printed_at": "2026-07-20T19:44:00Z", "oi_as_of": "2026-07-21T14:00:00Z"}
    st = brief_compose.oi_state(dict(base, oi_delta=10, volume=1000), now)
    assert st["state"] == brief_compose.CONF_NO
    st = brief_compose.oi_state(dict(base, oi_delta=600, volume=1000), now)
    assert st["state"] == brief_compose.CONF_YES
    assert "at least half the print" in st["reason"]

    flow = brief_compose.classify_flow(
        [
            {"side": "call_buy", "status": "confirmed"},
            {"side": "call_buy", "status": "confirmed"},
        ]
    )
    assert (
        "at least half the print showed up as new open interest next morning" in (flow["explain"])
    )
    assert "rise in open interest" not in flow["explain"]


def test_failed_details_lookup_keeps_the_old_cache_entry(monkeypatch):
    # A rate-limited or empty Polygon answer must not overwrite good cached
    # details with a placeholder that would count as fresh for 30 days.
    monkeypatch.setattr(swing_report.polygon_data, "ticker_details", lambda t: None)
    assert swing_report._fetch_one_meta("MSFT") is None

    def boom(t):
        raise RuntimeError("429")

    monkeypatch.setattr(swing_report.polygon_data, "ticker_details", boom)
    assert swing_report._fetch_one_meta("MSFT") is None

    monkeypatch.setattr(
        swing_report.polygon_data,
        "ticker_details",
        lambda t: {"name": "Microsoft Corp", "type": "CS", "market_cap": 3.8e12},
    )
    info = swing_report._fetch_one_meta("MSFT")
    assert info["name"] == "Microsoft Corp" and info["type"] == "CS"
