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
