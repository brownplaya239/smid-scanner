"""Scanner rules from the 2026-11 accuracy audit: Golden Sweep definition,
size-weighted side estimate, regular-session ledger guard, and the
learner/scanner side-feature mirror."""

from datetime import UTC, datetime

import uoa_alpha as ua
import uoa_scanner as us

BUY = {"method": "approx", "ask_pct": 70, "bid_pct": 30}
SELL = {"method": "approx", "ask_pct": 20, "bid_pct": 80}


def _flow(**kw):
    base = {"sweeps": 3, "sweep_premium": 1_500_000, "max_sweep_premium": 1_200_000, "side": BUY}
    base.update(kw)
    return base


def test_golden_needs_one_big_buy_side_sweep():
    row = {"dte": 10}
    assert us.is_golden_sweep(row, _flow())
    # many small sweeps that only SUM past $1M are not golden
    assert not us.is_golden_sweep(row, _flow(max_sweep_premium=300_000))
    # seller-leaning tape is not golden
    assert not us.is_golden_sweep(row, _flow(side=SELL))
    # unknown side is not golden
    assert not us.is_golden_sweep(row, _flow(side={"method": "none"}))
    assert not us.is_golden_sweep({"dte": 45}, _flow())


def test_side_estimate_is_weighted_by_contracts():
    # one 500-lot above the VWAP vs ten 1-lots below it
    trades = [{"price": 2.10, "size": 500}] + [{"price": 1.90, "size": 1}] * 10
    side = us._classify_approx(trades, {"px": 2.00})
    assert side["ask_pct"] == 98  # 500 / 510 contracts
    assert side["method"] == "approx"


def test_ledger_only_appends_in_the_regular_session():
    def at(s):
        return datetime.fromisoformat(s).astimezone(UTC)

    assert us.in_regular_session(at("2026-11-10T10:00:00-05:00"))
    assert us.in_regular_session(at("2026-11-10T16:05:00-05:00"))  # grace
    assert not us.in_regular_session(at("2026-11-10T09:00:00-05:00"))  # pre-open
    assert not us.in_regular_session(at("2026-11-10T18:30:00-05:00"))  # after close
    assert not us.in_regular_session(at("2026-11-14T11:00:00-05:00"))  # Saturday
    assert not us.in_regular_session(at("2026-11-26T11:00:00-05:00"))  # Thanksgiving


def test_learner_side_feature_matches_the_scanner_mirror(monkeypatch):
    monkeypatch.setattr(
        us,
        "_edge_weights",
        lambda: {
            "features": {
                f"side:{k}": {"adj": 1.0} for k in ("seller", "call_buy", "put_buy", "unclassified")
            }
        },
    )
    for fs, want in (
        ("call_buyer", "call_buy"),
        ("put_buyer", "put_buy"),
        ("put_seller", "seller"),
        ("call_seller", "seller"),
        ("mixed", "unclassified"),
        ("unknown", "unclassified"),
    ):
        assert ua._ew_side({"flow_side": fs}) == want
        row = {"flow_side": fs, "type": "put", "dte": 10, "tags": []}
        live, _ = us.edge_adjust(row)
        # side is shadow-only: recorded as the challenger term, never live
        assert row["challenger_why"] == [f"{want} +1.0"]
        assert live == 0.0
        assert f"side:{want}" in ua._ew_features({"flow_side": fs, "dte": 10})


def test_learner_grades_like_the_track_record():
    put = {
        "direction": "bearish",
        "flow_side": "put_buyer",
        "returns": {5: {"ret": -4.0, "excess": -3.0}},
    }
    assert ua._ew_win(put) is True
    seller = {"direction": "income", "flow_side": "put_seller", "returns": {5: {"excess": 2.0}}}
    assert ua._ew_win(seller) is None
