"""Desk wording and the signed-out welcome box in docs/index.html.

TickerDesk's own record says its signals are close to a coin flip, so the
Desk must describe charts, not tell people what to buy or sell. These tests:
- run the real welcome-box functions in Node: shown only when nobody is
  signed in, hidden for a saved session, and dismissal is remembered;
- check the risk-off note on the standout-chart cards only says "lose more
  often than not" when the outcome record says so;
- guard against the old advice-sounding phrases coming back.
Node tests are skipped when Node is not installed; the phrase guard always runs."""

import json
import re

import pytest

from flow_site_js_test import HTML, NODE, _run

needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")

WELCOME = ["tdWelcomeSync", "tdWelcomeDismiss"]

# A tiny stand-in for window.localStorage with the parts the code uses.
STORE = """
const TD_WELCOME_KEY = "td_welcome_dismissed_v1";
function mkStore(init) {
  const d = Object.assign({}, init || {});
  return {
    getItem: function (k) { return k in d ? d[k] : null; },
    setItem: function (k, v) { d[k] = String(v); },
    key: function (i) { return Object.keys(d)[i] || null; },
    get length() { return Object.keys(d).length; },
    dump: function () { return d; },
  };
}
"""


def _welcome(body):
    return _run(WELCOME, STORE + body)


@needs_node
def test_welcome_shows_for_signed_out_visitor():
    r = _welcome(
        "const el = {hidden: true};"
        "const shown = tdWelcomeSync(el, null, mkStore());"
        "console.log(JSON.stringify({shown: shown, hidden: el.hidden}));"
    )
    assert r == {"shown": True, "hidden": False}


@needs_node
def test_welcome_hidden_when_signed_in():
    r = _welcome(
        "const el = {hidden: false};"
        "const shown = tdWelcomeSync(el, {id: 'u1'}, mkStore());"
        "console.log(JSON.stringify({shown: shown, hidden: el.hidden}));"
    )
    assert r == {"shown": False, "hidden": True}


@needs_node
def test_welcome_hidden_while_saved_session_loads():
    # A returning member's Supabase session key is in storage before auth
    # resolves: the box must not flash for them.
    r = _welcome(
        "const el = {hidden: true};"
        "const st = mkStore({'sb-abc123-auth-token': '{}'});"
        "console.log(JSON.stringify(tdWelcomeSync(el, null, st)));"
    )
    assert r is False


@needs_node
def test_welcome_dismissal_is_remembered():
    r = _welcome(
        "const el = {hidden: false};"
        "const st = mkStore();"
        "tdWelcomeDismiss(el, st);"
        "const afterDismiss = el.hidden;"
        "const el2 = {hidden: true};"
        "const again = tdWelcomeSync(el2, null, st);"
        "console.log(JSON.stringify({afterDismiss: afterDismiss, again: again,"
        " hidden2: el2.hidden, saved: st.dump()[TD_WELCOME_KEY]}));"
    )
    assert r == {"afterDismiss": True, "again": False, "hidden2": True, "saved": "1"}


@needs_node
def test_welcome_survives_broken_storage():
    # Private-mode browsers can throw on localStorage access.
    r = _welcome(
        "const bad = {getItem: function () { throw new Error('denied'); },"
        " setItem: function () { throw new Error('denied'); }, length: 0,"
        " key: function () { return null; }};"
        "const el = {hidden: true};"
        "const shown = tdWelcomeSync(el, null, bad);"
        "tdWelcomeDismiss(el, bad);"
        "console.log(JSON.stringify({shown: shown, hidden: el.hidden}));"
    )
    assert r == {"shown": True, "hidden": True}


RO = ["_sgn", "_nfmt", "deskMomentumRead", "opcRiskOffNote"]


def _ro(stats):
    return _run(RO, "console.log(JSON.stringify(opcRiskOffNote(" + json.dumps(stats) + ")));")


@needs_node
def test_risk_off_note_says_lose_only_when_record_says_so():
    losing = {
        "scans": {
            "overall": {
                "status": "active",
                "n": 1484,
                "win_rate": 43,
                "ev": -1.81,
                "profit_factor": 0.66,
                "hold_d": 5,
            }
        },
        "setups": {"grades": {"A+": {"status": "active", "n": 266, "win_rate": 44, "avg": -0.9}}},
    }
    r = _ro(losing)
    assert "lost more often than not" in r["banner"]
    assert "A+ setups win 44%" in r["banner"]
    assert "not entries" in r["banner"]
    assert r["tag"] == "Risk-off: historically these lose more often than not"

    r = _ro({})
    assert "lost" not in r["banner"] and "lose" not in r["tag"]
    assert r["banner"].startswith("Risk-off tape:")


# Phrases that read like personal trade instructions or overclaim an edge.
# Matched case-insensitively against the whole page, comments included, so
# they can't creep back in through a copy-paste.
BANNED = [
    "what to trade",
    "buy pullback",
    "watch breakout",
    "avoid longs",
    "best long setup",
    "best short / avoid",
    "personal alpha record",
    "favor longs",
    "respect stops",
    "smart-money options flow",
    "validated market edges",
    ">LONG<",
    ">SHORT<",
    '"LONG" : "SHORT"',
]


def test_no_advice_phrases_on_the_site():
    with open(HTML, encoding="utf-8") as f:
        src = f.read().lower()
    found = [p for p in BANNED if p.lower() in src]
    assert not found, f"advice-sounding wording is back in docs/index.html: {found}"


def test_welcome_box_and_legend_are_wired():
    with open(HTML, encoding="utf-8") as f:
        src = f.read()
    box = re.search(
        r'<div class="tdw" id="td-welcome" hidden>(.*?)<h2 class="tab-title">', src, re.DOTALL
    )
    assert box, "welcome box must sit at the top of the Desk, hidden until JS decides"
    html = box.group(1)
    for need in (
        'href="/sample-report"',
        'data-go-tab="adhoc"',
        'href="/transparency"',
        'id="tdw-form"',
        'id="tdw-x"',
    ):
        assert need in html, need
    assert "beat the market" not in html.lower()
    # one shared sign-up function for both boxes, no second copy of the fetch
    assert src.count('"/brief/subscribe"') == 1
    assert 'tdWireBriefForm("ec", "footer")' in src
    assert 'tdWireBriefForm("tdw", "welcome")' in src
    assert '<details class="desk-legend"' in src
    assert 'href="/glossary#tickerdesk-labels"' in src
