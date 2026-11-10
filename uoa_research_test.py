"""Accuracy rules for the flow research engine (uoa_research.py).

Each test pins one rule from the 2026-11 research-engine audit: books built
from classified aggressor sides only, the golden-sweep feature, the
walk-forward embargo, verdict strings generated from the numbers, and EVs
that stay in stock-return units.
"""

import json
import random
from datetime import date, timedelta

import pytest

import uoa_research as R

SIDES = ("call_buyer", "put_buyer", "put_seller", "call_seller", "mixed", "unknown")
DIRECTION = {
    "call_buyer": "bullish",
    "put_buyer": "bearish",
    "put_seller": "income",
    "call_seller": "income",
    "mixed": "hedge",
    "unknown": "hedge",
}


def _ret(exc, spy=0.0):
    """A returns cell: stock return = excess + SPY; exc_c == excess keeps the
    price-basis guard happy (ratio 1)."""
    return {"ret": round(exc + spy, 2), "excess": exc, "exc_c": exc}


def _alert(i=0, day="2026-10-05", side="call_buyer", exc=1.0, spy1=0.0, spy5=0.0, hour=15, **kw):
    typ = "put" if side in ("put_buyer", "put_seller") else "call"
    s = {
        "id": f"id{i}_{day}_{hour}",
        "flagged_at": f"{day}T{hour:02d}:00:00+00:00",
        "ticker": kw.pop("ticker", "T%d" % (i % 40)),
        "contract": kw.pop("contract", "O:T%d261120%s%08d" % (i, typ[0].upper(), 100000)),
        "type": typ,
        "flow_side": side,
        "direction": DIRECTION[side],
        "signal_type": "voloi",
        "trade_score": 70,
        "dte": 30,
        "premium": 300e3,
        "liquidity": "B",
        "cap_bucket": "large",
        "sector": "Tech",
        "tags": [],
        "volume": 1000,
        "open_interest": 100,
        "returns": {1: _ret(exc / 3, spy1), 5: _ret(exc, spy5)},
    }
    s.update(kw)
    return s


def _weekdays(start, n):
    d, out = start, []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def synthetic_ledger(days=110, per_day=84, seed=7):
    """A deterministic fake ledger spanning ~22 weeks with every side, some
    golden sweeps, Into-ERN tags, a few re-logs, a split artifact, and
    alerts whose +5d outcome has not matured."""
    rnd = random.Random(seed)
    out = []
    caps = ("mega", "large", "mid", "small")
    for di, day in enumerate(_weekdays(date(2026, 6, 1), days)):
        spy5 = rnd.gauss(0.2, 1.5)
        spy1 = rnd.gauss(0.05, 0.8)
        for j in range(per_day):
            i = di * per_day + j
            side = SIDES[j % len(SIDES)]
            golden = j % 7 == 0
            tags = ["Golden Sweep"] if golden else (["Sweep", "Block"] if j % 5 == 0 else [])
            if j % 4 == 1:
                tags.append("Into ERN")
            a = _alert(
                i,
                day,
                side,
                exc=round(rnd.gauss(-0.2, 6.0), 2),
                spy1=round(spy1, 2),
                spy5=round(spy5, 2),
                ticker="T%d" % (j % 40),
                dte=[3, 12, 30, 45, 90][j % 5],
                premium=[100e3, 500e3, 2e6, 8e6][j % 4],
                liquidity="ABCD"[j % 4],
                cap_bucket=caps[j % 4],
                signal_type="golden_sweep" if golden else "sweep" if "Sweep" in tags else "voloi",
                tags=tags,
                trade_score=55 + (j % 40),
            )
            out.append(a)
            if j % 10 == 0:  # a later scan re-logs the same contract
                r = dict(a, id=a["id"] + "_relog", flagged_at=f"{day}T19:00:00+00:00")
                out.append(r)
    # the newest alerts have no +5d outcome yet
    for s in out[-per_day * 3 :]:
        s["returns"] = {1: s["returns"][1]}
    out[0]["basis_suspect"] = True
    return out


@pytest.fixture(scope="module")
def ledger():
    return synthetic_ledger()


@pytest.fixture(scope="module")
def built(ledger):
    return R.build_rows(ledger)


@pytest.fixture(scope="module")
def payload(built):
    rows, drift_rows, sample, spy, capmap = built
    return R.build_payload(rows, drift_rows, sample, spy, capmap)


# ── side grouping ────────────────────────────────────────────────────────


def test_books_use_classified_aggressor_sides_only():
    alerts = [_alert(i, side=sd, exc=2.0, contract=f"O:X{i}") for i, sd in enumerate(SIDES)]
    rows, drift_rows, sample, _, _ = R.build_rows(alerts)
    by_side = {r["side"]: r for r in rows}
    assert set(by_side) == {"call_buyer", "put_buyer", "put_seller", "call_seller"}
    assert by_side["call_buyer"]["book"] == "call_buy" and by_side["call_buyer"]["exc"] == 2.0
    assert by_side["put_buyer"]["book"] == "put_buy" and by_side["put_buyer"]["exc"] == -2.0
    # sellers: their own book, graded on their lean
    assert by_side["put_seller"]["book"] == "seller" and by_side["put_seller"]["exc"] == 2.0
    assert by_side["call_seller"]["book"] == "seller" and by_side["call_seller"]["exc"] == -2.0
    assert sample["unclassified"] == 2  # mixed + unknown: counted, not graded
    assert len(drift_rows) == 6  # but they are part of the flagged universe


def test_mixed_put_prints_never_enter_the_put_book(built):
    rows = built[0]
    put_book = [r for r in rows if r["book"] == "put_buy"]
    assert put_book and {r["side"] for r in put_book} == {"put_buyer"}
    seller_book = {r["side"] for r in rows if r["book"] == "seller"}
    assert seller_book == {"put_seller", "call_seller"}


def test_attribution_books_report_their_contents(payload, built):
    sample = built[2]
    books = payload["attribution"]["books"]
    assert set(books) == {"call_buy", "put_buy", "seller"}
    assert books["call_buy"]["sides"] == {"call_buyer": sample["by_book"]["call_buy"]}
    assert set(books["seller"]["sides"]) == {"put_seller", "call_seller"}
    assert books["put_buy"]["label"].startswith("Puts bought")
    for b in books.values():
        assert b["selection_component"] == pytest.approx(b["dir_ev"] - b["flow_alpha"], abs=2e-3)


def test_relogs_collapse_to_one_alert_per_contract_per_day():
    a = _alert(1, flagged_at="2026-10-05T14:00:00+00:00", contract="O:SAME")
    b = _alert(1, flagged_at="2026-10-05T19:00:00+00:00", contract="O:SAME", id="other")
    c = _alert(1, flagged_at="2026-10-06T14:00:00+00:00", contract="O:SAME", id="next")
    rows, _, sample, _, _ = R.build_rows([a, b, c])
    assert sample["ledger_rows"] == 3 and sample["alerts"] == 2 and len(rows) == 2


def test_sample_counts_add_up(built, ledger):
    s = built[2]
    assert s["ledger_rows"] == len(ledger)
    assert s["graded"] + s["unclassified"] + s["basis_excluded"] + s["unmatured"] == s["alerts"]
    assert s["basis_excluded"] == 1 and s["unmatured"] > 0
    assert s["span"][0] == "2026-06-01" and s["sessions"] > 100


# ── golden feature ───────────────────────────────────────────────────────


def test_golden_feature_is_the_golden_sweep_tag():
    assert R.is_golden({"tags": ["Golden Sweep", "Block"]})
    assert R.is_golden({"tags": [], "signal_type": "golden_sweep"})
    # a sweep that also printed a block is NOT a golden sweep
    assert not R.is_golden({"tags": ["Sweep", "Block"], "signal_type": "sweep"})
    f = R.features("call_buy", {"tags": ["Golden Sweep"], "dte": 30})
    assert "golden:yes" in f and "dte:22-60" in f


# ── embargo ──────────────────────────────────────────────────────────────


def test_walk_forward_drops_the_five_sessions_before_each_test_week(built):
    rows = built[0]
    sessions = sorted({r["date"] for r in rows})
    seen = 0
    for _week, train, test in R.walk_forward(rows):
        start = sessions.index(min(r["date"] for r in test))
        train_dates = {r["date"] for r in train}
        assert max(train_dates) == sessions[start - R.EMBARGO_SESSIONS - 1]
        assert not train_dates & set(sessions[start - R.EMBARGO_SESSIONS : start])
        seen += 1
    assert seen >= 15


def test_embargo_split_leaves_a_five_session_gap(built):
    rows = built[0]
    dev, hold, embargoed = R.embargo_split(rows, 0.3)
    sessions = sorted({r["date"] for r in rows})
    gap = sessions.index(min(r["date"] for r in hold)) - sessions.index(max(r["date"] for r in dev))
    assert gap == R.EMBARGO_SESSIONS + 1
    assert embargoed > 0 and len(dev) + len(hold) + embargoed == len(rows)


# ── verdict strings come from the numbers ────────────────────────────────


def test_accruing_models_say_accruing_not_failed():
    lm = R.loser_model([])
    assert lm["status"] == "accruing" and lm["decision"].startswith("ACCRUING")
    s2 = R.score2_eval([])
    assert s2["decision"].startswith("ACCRUING")
    for text in (lm["decision"], s2["decision"]):
        assert "FAIL" not in text.upper() and "REJECT" not in text.upper()


def test_loser_decision_quotes_its_own_numbers():
    good = {
        "status": "active",
        "weeks_improved": 4,
        "weeks_total": 5,
        "ev_base": -0.5,
        "ev_after_exclusion": -0.1,
    }
    assert R.loser_decision(good).startswith("CANDIDATE")
    bad = dict(good, weeks_improved=1, ev_after_exclusion=-0.7)
    text = R.loser_decision(bad)
    assert text.startswith("REJECTED") and "1/5" in text and "-0.50" in text and "-0.70" in text


def test_deployment_rows_match_the_sections(payload):
    dep = {r["finding"]: r for r in payload["deployment"]["rows"]}
    lm, s2 = payload["loser_model"], payload["score2"]
    assert dep["Loser model (categorical odds)"]["evidence"] == lm["decision"]
    assert dep["Loser model (categorical odds)"]["production"] == lm["decision"].split()[0]
    assert dep["score2 ranking"]["evidence"] == s2["decision"]
    ia = payload["interactions"]
    assert dep["Interaction search (depth <= 3)"]["evidence"].startswith("%d tested" % ia["tested"])
    assert dep["score2-quintile sizing table"]["production"] == "RETIRED"
    assert "sizing" not in payload
    assert payload["deployment"]["as_of"] == payload["generated"][:10]


def test_avoidance_verdicts_follow_the_window_numbers():
    base = {"since": R.PREREG_DATE, "n": 500, "weeks": 10, "dates": 40, "adverse_dates": 3}
    gates = {
        "weeks_ge_4": True,
        "dates_ge_15": True,
        "adverse_period": True,
        "clustered_ci_excludes_0": False,
        "pooled_ev_negative": False,
    }
    pos = dict(base, ev=1.123, clustered_ci95=[-0.4, 2.6], gates=gates)
    st, text = R.alpha_verdict(pos)
    assert st == "NOT CONFIRMED" and "+1.12" in text and "did not lose money" in text
    neg = dict(pos, ev=-0.8, gates=dict(gates, pooled_ev_negative=True))
    st, text = R.alpha_verdict(neg)
    assert st == "NOT CONFIRMED" and "spans zero" in text
    ok = dict(
        neg,
        clustered_ci95=[-1.5, -0.2],
        gates=dict(gates, pooled_ev_negative=True, clustered_ci_excludes_0=True),
    )
    assert R.alpha_verdict(ok)[0] == "CONFIRMED"
    thin = dict(neg, weeks=2, gates=dict(gates, weeks_ge_4=False))
    assert R.alpha_verdict(thin)[0] == "ACCRUING"


def test_hedge_verdict_never_contradicts_payoff_per_carry():
    eps = {"count": 3}
    no_carry = {
        "carry_ev": 0.22,
        "payoff_ev": 6.06,
        "payoff_per_unit_carry": None,
        "adverse_episodes": eps,
    }
    st, text = R.hedge_verdict(no_carry)
    assert st == "NOT A HEDGE PROFILE" and "< 1" not in text
    hedge = {
        "carry_ev": -0.5,
        "payoff_ev": 2.0,
        "payoff_per_unit_carry": 4.0,
        "adverse_episodes": eps,
    }
    st, text = R.hedge_verdict(hedge)
    assert st == "PASSES IN STOCK TERMS" and "4.00 >= 1.0 before option costs" in text
    weak = dict(hedge, payoff_ev=0.25, payoff_per_unit_carry=0.5)
    assert R.hedge_verdict(weak)[0] == "FAILS"
    one = dict(hedge, adverse_episodes={"count": 1})
    assert R.hedge_verdict(one)[0] == "ACCRUING"


def test_payoff_per_unit_carry_only_when_the_pocket_costs_carry(payload):
    hu = payload["avoidance_candidate"]["hedge_utility"]
    carry, payoff, ratio = hu["carry_ev"], hu["payoff_ev"], hu["payoff_per_unit_carry"]
    if carry is not None and payoff is not None and carry < 0 < payoff:
        assert ratio == pytest.approx(payoff / -carry, abs=0.01)
    else:
        assert ratio is None


def test_multiple_testing_text_uses_tonights_count(payload):
    av, ia = payload["avoidance_candidate"], payload["interactions"]
    assert "tested %d combinations" % ia["tested"] in av["multiple_testing"]
    assert "489" not in json.dumps(payload)


def test_adverse_episodes_merge_within_two_sessions():
    days = _weekdays(date(2026, 9, 1), 12)
    spy1 = dict.fromkeys(days, 0.3)
    # SPY falls on sessions 2 and 4 (one episode) and on session 9
    for k in (1, 3, 8):
        spy1[days[k]] = -1.5
    eps = R.adverse_episodes(spy1)
    assert [e[0] for e in eps] == [days[2], days[9]]


# ── units ────────────────────────────────────────────────────────────────


def test_pocket_stays_in_stock_return_units(payload, built):
    av = payload["avoidance_candidate"]
    assert av["status"] == "shadow"
    assert "by_liquidity_net_costs" not in av and "haircut" not in json.dumps(av)
    rows = built[0]
    sel = [r for r in rows if r["book"] == "put_buy" and 22 <= r["dte"] <= 60]
    for lg, cell in av["by_liquidity"].items():
        v = [r["exc"] for r in sel if r["liq"] == lg]
        assert cell["n"] == len(v)
        assert cell["ev"] == pytest.approx(sum(v) / len(v), abs=1e-3)


def test_into_earnings_share_reads_the_tag(payload, built):
    rows = built[0]
    sel = [r for r in rows if r["book"] == "put_buy" and 22 <= r["dte"] <= 60]
    share = sum(1 for r in sel if r["ern"]) / len(sel)
    assert share > 0
    assert payload["avoidance_candidate"]["concentration"]["into_earnings_share"] == round(share, 3)


def test_payload_is_json_and_self_consistent(payload):
    json.dumps(payload)
    s = payload["sample"]
    assert payload["version"] == 2
    assert sum(s["by_book"].values()) == s["graded"]
    assert all(b["n"] == s["by_book"][k] for k, b in payload["attribution"]["books"].items())


def test_nightly_run_scores_the_full_ledger_via_compute_edge(tmp_path, monkeypatch, ledger):
    calls = []

    def fake_compute_edge(rebuild=False, persist=True):
        assert persist is False  # research must score read-only
        calls.append(rebuild)
        return {}, ledger

    monkeypatch.setattr(R.ua, "compute_edge", fake_compute_edge)
    monkeypatch.setattr(R, "OUT", str(tmp_path / "uoa_research.json"))
    R.run()
    assert calls == [False]
    out = json.loads((tmp_path / "uoa_research.json").read_text(encoding="utf-8"))
    assert out["sample"]["ledger_rows"] == len(ledger)
    assert out["sample"]["span"][0] == "2026-06-01"  # archives included, not a 7-day slice
