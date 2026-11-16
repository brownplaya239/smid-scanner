"""Desk wording and the signed-out welcome box in docs/index.html.

TickerDesk's own record says its signals are close to a coin flip, so the
site must describe charts and levels, not tell people what to buy or sell.
These tests:
- run the real welcome-box functions in Node: hidden until the first
  sign-in check finishes, shown only to signed-out visitors, and a
  dismissal sticks for the page even when localStorage throws;
- check the risk-off note on the standout-chart cards states only what the
  outcome record measures (overall average returns, not a risk-off win rate);
- run the 0DTE level-map renderers on fixtures and check what they print;
- guard the visible text of the Desk, the glossary and every landing page
  against advice-sounding phrases coming back.
Node tests are skipped when Node is not installed; the phrase guard always runs."""

import html
import json
import os
import re

import pytest

from flow_site_js_test import HTML, NODE, _run

needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")

ROOT = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(ROOT, "docs")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


WELCOME = ["tdWelcomeSync", "tdWelcomeDismiss"]

# The page-lifetime state the functions share, plus a tiny stand-in for
# window.localStorage with the parts the code uses.
STORE = """
const TD_WELCOME_KEY = "td_welcome_dismissed_v1";
const tdWelcomeState = { dismissed: false };
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
const bad = {getItem: function () { throw new Error('denied'); },
  setItem: function () { throw new Error('denied'); },
  key: function () { throw new Error('denied'); },
  get length() { throw new Error('denied'); }};
"""


def _welcome(body):
    return _run(WELCOME, STORE + body)


@needs_node
def test_welcome_shows_for_signed_out_visitor_once_auth_is_ready():
    r = _welcome(
        "const el = {hidden: true};"
        "const shown = tdWelcomeSync(el, null, mkStore(), true);"
        "console.log(JSON.stringify({shown: shown, hidden: el.hidden}));"
    )
    assert r == {"shown": True, "hidden": False}


@needs_node
def test_welcome_hidden_when_signed_in():
    r = _welcome(
        "const el = {hidden: false};"
        "const shown = tdWelcomeSync(el, {id: 'u1'}, mkStore(), true);"
        "console.log(JSON.stringify({shown: shown, hidden: el.hidden}));"
    )
    assert r == {"shown": False, "hidden": True}


@needs_node
def test_welcome_hidden_while_auth_is_pending_even_with_throwing_storage():
    # A null user before the first sign-in check finishes means "unknown",
    # not "signed out": a returning member must not see the box flash, even
    # in a browser where localStorage throws.
    r = _welcome(
        "const out = {};"
        "for (const [k, st] of [['ok', mkStore()], ['bad', bad], ['none', null]]) {"
        "  const el = {hidden: true};"
        "  out[k] = [tdWelcomeSync(el, null, st, false), el.hidden];"
        "}"
        "console.log(JSON.stringify(out));"
    )
    assert r == {"ok": [False, True], "bad": [False, True], "none": [False, True]}


@needs_node
def test_welcome_dismissal_is_remembered():
    r = _welcome(
        "const el = {hidden: false};"
        "const st = mkStore();"
        "tdWelcomeDismiss(el, st);"
        "const afterDismiss = el.hidden;"
        "tdWelcomeState.dismissed = false;"  # a fresh page load: only storage remains
        "const el2 = {hidden: true};"
        "const again = tdWelcomeSync(el2, null, st, true);"
        "console.log(JSON.stringify({afterDismiss: afterDismiss, again: again,"
        " hidden2: el2.hidden, saved: st.dump()[TD_WELCOME_KEY]}));"
    )
    assert r == {"afterDismiss": True, "again": False, "hidden2": True, "saved": "1"}


@needs_node
def test_welcome_dismissal_survives_resync_with_throwing_storage():
    # Private-mode browsers can throw on every localStorage call. The box
    # still shows once auth is ready, and after the x it stays hidden on
    # every later re-check (auth change, sign-out) for the life of the page.
    r = _welcome(
        "const el = {hidden: true};"
        "const shown = tdWelcomeSync(el, null, bad, true);"
        "tdWelcomeDismiss(el, bad);"
        "const r1 = tdWelcomeSync(el, null, bad, true);"
        "const r2 = tdWelcomeSync(el, null, null, true);"
        "console.log(JSON.stringify({shown: shown, r1: r1, r2: r2, hidden: el.hidden}));"
    )
    assert r == {"shown": True, "r1": False, "r2": False, "hidden": True}


def test_auth_ready_flag_hooks_into_existing_auth_flow():
    src = _read(HTML)
    assert "let TD_AUTH_READY = false;" in src
    assert "window.tdAuthReady = function () { return TD_AUTH_READY; };" in src
    # set right before the first user broadcast, on both init paths: the
    # no-config anon path and the getSession() race's finally
    assert src.count("TD_AUTH_READY = true;\n      _emitUser();") == 2
    welcome = src[src.index("function setupWelcome()") :]
    welcome = welcome[: welcome.index("})();")]
    assert "window.tdAuthReady" in welcome
    assert "setTimeout" not in welcome  # no timer that could show it early


RO = ["_sgn", "_nfmt", "deskMomentumRead", "opcRiskOffNote"]


def _ro(stats):
    return _run(RO, "console.log(JSON.stringify(opcRiskOffNote(" + json.dumps(stats) + ")));")


@needs_node
def test_risk_off_note_states_only_what_the_record_measures():
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
    b = r["banner"]
    assert "negative average returns" in b
    assert "overall outcome record (all market conditions)" in b
    assert "A+ setups win 44% (−0.9% avg)" in b
    assert "not entries" in b
    assert r["tag"] == "Risk-off; overall record: negative average returns"
    # the record is not a risk-off cohort and "loses" is not a win rate
    for text in (b, r["tag"]):
        low = text.lower()
        assert "more often than not" not in low
        assert "kind of market" not in low
        assert "historically" not in low

    # positive average but a sub-50% win rate: no "negative" claim either way
    r = _ro({"scans": {"overall": {"status": "active", "n": 900, "win_rate": 45, "ev": 0.4}}})
    assert "negative" not in r["banner"] and "negative" not in r["tag"]

    r = _ro({})
    assert r["banner"] == (
        "Risk-off tape: most stocks are falling today. "
        "Treat these as a research watchlist, not entries."
    )
    assert r["tag"] == "Risk-off: most stocks are falling today"


# ── 0DTE level map: run the real renderers on a fixture ──────────────────


def _block(src, marker):
    """`marker` through the bracket that closes the first { or [ after it,
    skipping string literals."""
    start = src.index(marker)
    i = start + len(marker)
    while src[i] not in "{[":
        i += 1
    depth, q = 0, None
    while True:
        ch = src[i]
        if q:
            if ch == "\\":
                i += 2
                continue
            if ch == q:
                q = None
        elif ch in "\"'`":
            q = ch
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return src[start : i + 1] + ";"
        i += 1


LEVELS_FIXTURE = """
const hosts = {};
const $id = function (x) { return hosts[x] || (hosts[x] = {innerHTML: ""}); };
const fmt2 = function (v) { return v == null || !isFinite(v) ? "-" : "$" + v.toFixed(2); };
let sym = "SPY";
let _zStats = {total_graded: 120, by_type: {
  or_break_long: {status: "active", win_rate: 52, n: 41},
  vwap_loss_short: {status: "accruing", n: 12}}};
const VALS = {VWAP: 500, ORH: 503, ORL: 497, VAH: 504, VAL: 496, PDH: 507,
  PDL: 492, "EM+": 509, "EM−": 491};
function levelRows(ctx) {
  return Object.keys(VALS).concat(["CALL WALL", "PUT WALL"]).map(function (n) {
    return {n: n, v: n === "CALL WALL" ? 510 : n === "PUT WALL" ? 490 : VALS[n]};
  });
}
function lvStatsFor(s) {
  return {levels: {VWAP: {touch: 81, held: 62}, PDH: {held: 40, brk_ext: 0.5},
                   PDL: {held: 45, brk_ext: 0.4}}};
}
const out = {};
for (const last of [501, 499]) {
  renderPlaybook({sym: "SPY", last: last});
  out["playbook_" + last] = hosts["zdte-playbook"].innerHTML;
}
const vals = Object.assign({}, VALS);
renderIdeas(vals, 501, {call_wall: 510, put_wall: 490, flip: 495}, 501, null);
out.ideas_pos = hosts["zdte-ideas"].innerHTML;
renderIdeas(vals, 499, {call_wall: 510, put_wall: 490, flip: 505}, 499, null);
out.ideas_neg = hosts["zdte-ideas"].innerHTML;
out.zdef = JSON.stringify(ZDEF);
out.lines = JSON.stringify(LINE_DEFS);
console.log(JSON.stringify(out));
"""


def _level_map():
    src = _read(HTML)
    consts = "\n".join(
        _block(src, m) for m in ("const IDEA_LABEL = ", "const ZDEF = ", "const LINE_DEFS = ")
    )
    return _run(["renderPlaybook", "renderIdeas"], consts + LEVELS_FIXTURE)


# Words that make a level map read as a trade ticket.
TRADE_TICKET = [
    r"\bLONG\b",
    r"\bSHORT\b",
    r"\bbuy\b",
    r"\bsell\b",
    r"\btarget\b",
    r"\btrigger\b",
    r"R:R",
    r"\bfade\b",
    r"trade ideas?",
    r"trail the move",
    r"\bentry\b",
    r"\bstop\b",
]


@needs_node
def test_level_map_renders_neutral_scenarios_not_trade_tickets():
    out = _level_map()
    for key, raw in out.items():
        text = _visible(raw)
        hits = [p for p in TRADE_TICKET if re.search(p, text, re.IGNORECASE if p.islower() else 0)]
        assert not hits, f"{key} reads like a trade ticket: {hits}\n{text[:400]}"
    pb = _visible(out["playbook_501"])
    assert "Holds above VWAP $500.00" in pb
    assert "next reference above: ORH $503.00" in pb
    assert "Stays below VWAP $500.00" in pb
    assert "hypothetical scenarios" in pb
    ideas = _visible(out["ideas_pos"]) + " " + _visible(out["ideas_neg"])
    assert "above level" in ideas and "below level" in ideas
    assert "Level $" in ideas and "Next reference $" in ideas
    assert "52% · n=41" in ideas  # the track record still shows
    assert "not advice or trade instructions" in ideas


# ── phrase guard over the visible text of every public page ──────────────

# Phrases that read like personal trade instructions, overclaim an edge,
# or claim more than the data measures. Case-insensitive.
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
    "buy dips",
    "sell rallies",
    "dip-buy",
    "→ target",
    "trade idea",
    "earnings edge",
    "mispricing",
    "fade the extremes",
    "play momentum",
    "short trigger",
    "size down",
    "plan the exit",
    "inside the trade",
    "opened and held",
    "held overnight",
    "stayed in the trade",
    "more often than not in this kind of market",
    "lose more often than not",
    "lost more often than not",
    "a name near a breakout",
    "looks like protection",
    "highest-edge",
    "historically struggle",
    "late entry",
    "toward alpha",
    "edge log",
    "your edge by setup",
    "modest edge",
    "too tight for these names",
    "buy point",
    "tail-risk hedging",
]
# Raw-markup patterns for the old LONG/SHORT trade badges.
BANNED_RAW = [">LONG<", ">SHORT<", '"LONG" : "SHORT"', "▲ LONG", "▼ SHORT"]


def _visible(src):
    """What a reader sees: tags stripped, entities decoded (&nbsp;, &rsquo;),
    whitespace collapsed."""
    text = re.sub(r"<[^>]*>", "", src)
    text = html.unescape(text).replace(" ", " ")
    return re.sub(r"\s+", " ", text).strip()


def _strip_comments(src):
    """Drop HTML, CSS/JS block and JS line comments: readers never see them
    (a comment explaining what the code avoids may quote a banned phrase).
    `//` after a colon or a quote is a URL, not a comment."""
    src = re.sub(r"<!--.*?-->", "", src, flags=re.DOTALL)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return re.sub(r"(?m)(^|[^:'\"\\])//[^\n]*", r"\1", src)


def _variants(src):
    """The page several ways, so a phrase can't hide behind markup, entities,
    line breaks or a JS string split across `'...' + '...'`."""
    src = _strip_comments(src)
    raw = html.unescape(src).replace("\u00a0", " ")
    joined = re.sub(r"(['\"])\s*\+\s*\1", "", raw)
    for v in (raw, joined, _visible(src), _visible(joined)):
        yield re.sub(r"\s+", " ", v).lower()


def _public_pages():
    slugs = re.findall(r'"slug": "([a-z0-9-]+)"', _read(os.path.join(ROOT, "landing_pages.py")))
    assert len(slugs) >= 8, slugs
    names = ["index.html", "glossary.html", "transparency.html"] + [s + ".html" for s in slugs]
    return [os.path.join(DOCS, n) for n in names] + [os.path.join(ROOT, "landing_pages.py")]


def test_visible_text_normalizer():
    assert _visible("buy&nbsp;<b>dips</b>\n  now") == "buy dips now"
    assert "buy dips against" in list(_variants("'buy dips ' +\n  'against it'"))[1]
    assert "buy dips" not in " ".join(_variants("// buy dips\n<!-- buy dips -->x"))
    assert "https://x.io" in list(_variants("'https://x.io'"))[0]


@pytest.mark.parametrize("path", _public_pages(), ids=os.path.basename)
def test_no_advice_phrases_on_public_pages(path):
    src = _read(path)
    found = {p for v in _variants(src) for p in BANNED if p.lower() in v}
    found |= {p for p in BANNED_RAW if p in src}
    assert not found, (
        f"advice-sounding wording is back in {os.path.basename(path)}: {sorted(found)}"
    )


def test_welcome_box_and_legend_are_wired():
    src = _read(HTML)
    box = re.search(
        r'<div class="tdw" id="td-welcome" hidden>(.*?)<h2 class="tab-title">', src, re.DOTALL
    )
    assert box, "welcome box must sit at the top of the Desk, hidden until JS decides"
    markup = box.group(1)
    for need in (
        'href="/sample-report"',
        'data-go-tab="adhoc"',
        'href="/transparency"',
        'id="tdw-form"',
        'id="tdw-x"',
    ):
        assert need in markup, need
    assert "beat the market" not in markup.lower()
    # one shared sign-up function for both boxes, no second copy of the fetch
    assert src.count('"/brief/subscribe"') == 1
    assert 'tdWireBriefForm("ec", "footer")' in src
    assert 'tdWireBriefForm("tdw", "welcome")' in src
    assert '<details class="desk-legend"' in src
    assert 'href="/glossary#tickerdesk-labels"' in src
