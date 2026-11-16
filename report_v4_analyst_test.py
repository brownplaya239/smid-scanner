#!/usr/bin/env python3
"""report_v4_analyst_test.py — the AI analyst take, fully offline.

A fake Anthropic client stands in for the API, so every path runs in CI
with no key and no network: a valid take ships and renders, a planted
wrong number is retried once and then omitted, a refusal and an API error
omit the section, and with no key the section is skipped. The report
ships in every case, and the gate re-checks the shipped page.

Uses the committed fixtures_v4/mutation_base.pkl, like the mutation suite.

    uv run pytest report_v4_analyst_test.py
"""

import copy
import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

import estimates_provider as EP
import report_v4 as R4
import report_v4_analyst as AT
import report_v4_mutation as MUT
import report_v4_run as RR
import report_v4_validate as VV


@pytest.fixture(scope="module")
def base():
    snap, view, est = MUT._base()
    if view is None:
        pytest.skip("committed mutation fixture missing")
    return snap, view, est


def _msg(text="", stop="end_turn", model=AT.MODEL, category=None, inp=3000, out=1200):
    content = [SimpleNamespace(type="thinking", thinking="")]
    if text:
        content.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(
        content=content,
        stop_reason=stop,
        stop_details=SimpleNamespace(category=category) if stop == "refusal" else None,
        model=model,
        usage=SimpleNamespace(input_tokens=inp, output_tokens=out),
    )


class _Stream:
    def __init__(self, msg):
        self._msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self._msg


class FakeClient:
    """Mimics client.beta.messages.stream(...) -> context manager with
    get_final_message(). Responses are consumed in order; an exception in
    the list is raised instead."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kw):
        self.calls.append(kw)
        r = self.responses.pop(0)
        if isinstance(r, BaseException):
            raise r
        return _Stream(r)


def _good_json(view, snap):
    t = MUT.grounded_take(view, snap)
    assert t is not None
    return json.dumps({k: t[k] for k in ("summary", "bull_points", "bear_points", "watch_next")})


def _bad_number_json(view, snap):
    d = json.loads(_good_json(view, snap))
    d["bull_points"][0]["text"] = "The figure on record is 987654.3."
    return json.dumps(d)


def _render_and_gate(snap, view, est):
    core = R4.build_core(snap, view)
    apx = R4.build_appendix(snap, view, estimates=est)
    return core, VV.report(view, snap, core, apx, estimates=est, run_mutation=False)


def _status(res, cid):
    return {c["check_id"]: c["status"] for c in res["checks"]}.get(cid)


def test_valid_response_ships_and_renders(base):
    snap, view, est = base
    view = copy.deepcopy(view)
    fake = FakeClient(_msg(_good_json(view, snap)))
    at = AT.generate(view, snap, client=fake)

    assert at["available"] is True
    assert at["meta"]["status"] == "ok"
    assert len(at["meta"]["attempts"]) == 1
    # $4/M in + $20/M out
    assert at["meta"]["estimated_cost_usd"] == pytest.approx(3000 * 4e-6 + 1200 * 20e-6)

    kw = fake.calls[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"]["effort"] == "medium"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["betas"] == ["server-side-fallback-2026-07-01"] and kw["fallbacks"] == "default"
    assert "temperature" not in kw and "budget_tokens" not in json.dumps(kw["thinking"])
    # the prompt is built from the evidence pack only
    assert "[FIN-" in kw["messages"][0]["content"]

    view["analyst_take"] = at
    core, res = _render_and_gate(snap, view, est)
    _, text = VV._pdf_pages_text(core)
    assert "written by Claude Opus 5.5 from the data in this report" in text
    for cid in VV.ANALYST_CHECKS + ("ANALYST_TAKE_RENDERED", "CORE_PAGE_COUNT"):
        assert _status(res, cid) == "PASS", cid
    assert res["ok"] is True or res["blocking_failures"] == []
    assert res["analyst_take"]["status"] == "ok"


def test_invalid_number_retries_then_omits(base):
    snap, view, est = base
    view = copy.deepcopy(view)
    fake = FakeClient(_msg(_bad_number_json(view, snap)), _msg(_bad_number_json(view, snap)))
    at = AT.generate(view, snap, client=fake)

    assert at["available"] is False
    assert at["note"] == AT.UNAVAILABLE_NOTE
    assert at["meta"]["status"] == "invalid"
    assert len(fake.calls) == 2
    # the retry tells Claude what was wrong
    assert "987654.3" in fake.calls[1]["messages"][0]["content"]
    assert "rejected by the checker" in fake.calls[1]["messages"][0]["content"]
    assert at["meta"]["input_tokens"] == 6000

    view["analyst_take"] = at
    core, res = _render_and_gate(snap, view, est)
    _, text = VV._pdf_pages_text(core)
    assert "Analyst take unavailable for this report" in text
    assert AT.LABEL_MARK not in text
    assert _status(res, "ANALYST_TAKE_RENDERED") == "PASS"
    assert _status(res, "ANALYST_NUMBERS_GROUNDED") == "SKIP"
    assert _status(res, "CORE_PAGE_COUNT") == "PASS"
    assert not res["blocking_failures"]


def test_invalid_then_valid_ships_on_retry(base):
    snap, view, _ = base
    fake = FakeClient(_msg(_bad_number_json(view, snap)), _msg(_good_json(view, snap)))
    at = AT.generate(copy.deepcopy(view), snap, client=fake)
    assert at["available"] is True
    assert len(at["meta"]["attempts"]) == 2


@pytest.mark.parametrize("bad", ["", "not json at all"])
def test_empty_or_unparseable_output_omits(base, bad):
    snap, view, _ = base
    fake = FakeClient(_msg(bad), _msg(bad))
    at = AT.generate(copy.deepcopy(view), snap, client=fake)
    assert at["available"] is False
    assert at["meta"]["status"] in ("empty", "bad_json")


def test_refusal_omits(base):
    snap, view, _ = base
    fake = FakeClient(_msg("", stop="refusal", category="cyber"))
    at = AT.generate(copy.deepcopy(view), snap, client=fake)
    assert at["available"] is False
    assert at["meta"]["status"] == "refused"
    assert len(fake.calls) == 1  # a refusal is not retried by us


def test_api_error_omits(base):
    snap, view, _ = base
    err = anthropic.APIConnectionError(
        request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    at = AT.generate(copy.deepcopy(view), snap, client=FakeClient(err))
    assert at["available"] is False
    assert at["meta"]["status"] == "api_error"


def test_no_key_skips_cleanly(base, monkeypatch):
    snap, view, _ = base
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    def boom():
        raise AssertionError("no client may be built without a key")

    monkeypatch.setattr(AT, "_make_client", boom)
    at = AT.generate(copy.deepcopy(view), snap)
    assert at["available"] is False
    assert at["meta"]["status"] == "no_key"


def test_fallback_model_is_named_on_the_page(base):
    snap, view, _ = base
    fake = FakeClient(_msg(_good_json(view, snap), model="claude-opus-4-8"))
    at = AT.generate(copy.deepcopy(view), snap, client=fake)
    assert at["available"] is True
    assert AT.label_for(at).startswith("Analyst take, written by Claude Opus 4.8 ")


def test_checker_details():
    pack = [
        {"id": "FIN-revenue_q", "text": "Quarterly revenue: $3.99B (3987000000)"},
        {"id": "CAT-next-1", "text": "Upcoming: next earnings on 2026-10-21"},
        {"id": "PEER-CRM", "text": "Peer CRM trailing P/E: 40.0"},
    ]

    def take(text, ids):
        p = {"text": text, "evidence_ids": ids}
        return {
            "summary": {"text": "Revenue was $4.0B. It grew.", "evidence_ids": ["FIN-revenue_q"]},
            "bull_points": [p],
            "bear_points": [
                {"text": "Revenue was 3,987 million.", "evidence_ids": ["FIN-revenue_q"]}
            ],
            "watch_next": [{"text": "Earnings on 2026-10-21.", "evidence_ids": ["CAT-next-1"]}],
        }

    def ids_of(t):
        return {c for c, _ in AT.take_problems(t, pack, "NOW")}

    assert (
        ids_of(take("Revenue reached $3.99B, compared with peer CRM.", ["FIN-revenue_q"])) == set()
    )
    # rounding is fine, a different number is not
    assert "ANALYST_NUMBERS_GROUNDED" in ids_of(take("Revenue reached $4.5B.", ["FIN-revenue_q"]))
    # a number must be in the item the point cites, not just anywhere
    assert "ANALYST_NUMBERS_GROUNDED" in ids_of(take("Peer P/E is 40.", ["FIN-revenue_q"]))
    # a date not in the cited evidence
    assert "ANALYST_NUMBERS_GROUNDED" in ids_of(take("Earnings on 2026-11-02.", ["CAT-next-1"]))
    assert "ANALYST_NO_FOREIGN_TICKERS" in ids_of(
        take("Unlike AAPL, revenue was $3.99B.", ["FIN-revenue_q"])
    )
    for adv in ("We recommend the shares.", "The stock is undervalued.", "A price target is set."):
        assert "ANALYST_NO_ADVICE" in ids_of(take(adv, ["FIN-revenue_q"])), adv
    # a buyback is not a recommendation
    assert "ANALYST_NO_ADVICE" not in ids_of(
        take("A share buyback was announced.", ["FIN-revenue_q"])
    )
    assert "ANALYST_EVIDENCE_IDS" in ids_of(take("Revenue was $3.99B.", []))
    assert "ANALYST_LENGTH_CAPS" in ids_of(take("x " * 200, ["FIN-revenue_q"]))


def test_markup_characters_are_spelled_out():
    assert AT.plain_ai("margin < 5% & > 2% — ok") == "margin less than 5% and more than 2% - ok"


def test_mutation_proofs_cover_the_ai_checks():
    m = MUT.summary()
    assert m["all_checks_proven"] is True, m.get("unproven")
    for cid in VV.ANALYST_CHECKS + ("ANALYST_TAKE_RENDERED",):
        assert cid in m["proven"], cid


def test_run_ships_without_a_take_the_gate_rejects(base, tmp_path, monkeypatch):
    """A take that somehow bypassed the generator's checks (here: advice
    text marked available) is caught by the gate; run() then rebuilds the
    report without it and ships clean."""
    snap, view, est = base
    bad = MUT.grounded_take(view, snap)
    bad["bull_points"][0]["text"] = "Investors should buy the shares."
    monkeypatch.setattr(RR, "_snapshot", lambda t: (copy.deepcopy(snap), {}))
    monkeypatch.setattr(EP, "fetch_estimates", lambda *a, **k: est)
    monkeypatch.setattr(EP, "fetch_peers", lambda *a, **k: {"rows": [], "source": "test"})
    monkeypatch.setattr(AT, "generate", lambda v, s=None, client=None: copy.deepcopy(bad))

    res = RR.run("NOW", str(tmp_path), want_spy=False)
    r = res["result"]
    assert r["ok"] is True, r["blocking_failures"]
    assert r["analyst_take"]["status"] == "gate_rejected"
    assert "ANALYST_NO_ADVICE" in r["analyst_take"]["reason"]
    _, text = VV._pdf_pages_text(open(res["core"], "rb").read())
    assert "Analyst take unavailable for this report" in text
    assert "should buy" not in text
