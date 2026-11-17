"""Visitor analytics (docs/analytics.js), run in Node.

Loads the real file into a fake browser (window, document, navigator) and
checks that tdTrack does nothing while TD_POSTHOG_KEY is empty, that with
a key it calls PostHog with the right event and properties (window.posthog
is a stub that records calls), that Do Not Track switches it off, that the
loader asks for the privacy settings, and that no email reaches an event.
Also checks every page loads the helper and the CSP allows the PostHog
hosts. Skipped without Node."""

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(ROOT, "docs")
ANALYTICS = os.path.join(DOCS, "analytics.js")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

KEY_LINE = re.compile(r'^var TD_POSTHOG_KEY = "[^"]*";$', re.MULTILINE)
TEST_KEY = "phc_testKey123"

HARNESS = r"""
const vm = require("vm");
const fs = require("fs");
const SRC = fs.readFileSync(process.argv[2], "utf8");
const OPT = JSON.parse(process.argv[3]);
const captured = [], identified = [], resets = [], appended = [];
const win = {
  location: { pathname: "/vcp-screener", hostname: "tickerdesk.io",
              search: "?utm_source=x&utm_medium=social&utm_campaign=launch&email=a%40b.co" },
  navigator: OPT.dnt ? { doNotTrack: "1" } : {},
};
if (OPT.stub) {
  win.posthog = {
    __loaded: true,
    capture: (...a) => captured.push(a),
    identify: (...a) => identified.push(a),
    reset: (...a) => resets.push(a),
  };
}
const doc = {
  referrer: OPT.referrer === undefined ? "https://www.google.com/search?q=vcp" : OPT.referrer,
  createElement: (tag) => ({ tag }),
  head: { appendChild: (el) => appended.push(el) },
};
const ctx = { window: win, document: doc, URL, URLSearchParams, console };
vm.createContext(ctx);
vm.runInContext(SRC, ctx);
const before = captured.length;
win.tdTrack("brief_signup", { source: "footer", email: "x@y.com", note: "me@x.io", n: null });
win.tdTrack("checkout_started", { plan: "pro" }, { transport: "sendBeacon" });
win.tdIdentify("user-123");
win.tdIdentify("user-123");
win.tdIdentify(null);
let threw = false;
try { win.tdTrack(); win.tdTrack("x", "not-an-object"); } catch (e) { threw = true; }
const ph = win.posthog;
const queued = ph && Array.isArray(ph) ? ph.slice() : null;
const init = ph && ph._i ? ph._i : null;
let scrubbed = null;
if (init && init[0] && init[0][1] && init[0][1].before_send) {
  scrubbed = init[0][1].before_send({ event: "x", properties: {
    $current_url: "https://tickerdesk.io/?utm_source=x&session_id=cs_1#access_token=SECRET",
    $referrer: "https://t.co/abc?ref=1", other: "kept" } });
}
process.stdout.write(JSON.stringify({
  on: win.tdAnalyticsOn(), captured, before, identified, resets, threw,
  appended, queued, init: init ? JSON.parse(JSON.stringify(init)) : null, scrubbed,
  clean: win.tdAnalyticsCleanUrl("https://tickerdesk.io/a?utm_source=x&code=1&utm_medium=m#tok"),
}));
"""


def _run(key="", **opt):
    with open(ANALYTICS, encoding="utf-8") as f:
        src = f.read()
    assert len(KEY_LINE.findall(src)) == 1, "TD_POSTHOG_KEY line missing or duplicated"
    src = KEY_LINE.sub('var TD_POSTHOG_KEY = "' + key + '";', src)
    with tempfile.TemporaryDirectory() as d:
        js, h = os.path.join(d, "analytics.js"), os.path.join(d, "run.js")
        with open(js, "w", encoding="utf-8") as f:
            f.write(src)
        with open(h, "w", encoding="utf-8") as f:
            f.write(HARNESS)
        out = subprocess.run(
            [NODE, h, js, json.dumps(opt)],
            check=False,
            capture_output=True,
            encoding="utf-8",
            timeout=60,
        )
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout)


def test_no_key_does_nothing():
    r = _run("", stub=True)
    assert r["on"] is False
    assert r["captured"] == [] and r["identified"] == [] and r["resets"] == []
    assert r["appended"] == []  # PostHog's script is never requested
    assert r["threw"] is False


def test_no_key_and_no_posthog_does_not_create_one():
    r = _run("")
    assert r["queued"] is None and r["appended"] == []


def test_key_set_sends_page_view_then_events():
    r = _run(TEST_KEY, stub=True)
    assert r["on"] is True
    page_view = r["captured"][0]
    assert page_view == [
        "page_view",
        {
            "path": "/vcp-screener",
            "referrer_domain": "www.google.com",
            "utm_source": "x",
            "utm_medium": "social",
            "utm_campaign": "launch",
        },
    ]
    assert r["before"] == 1
    # Emails are dropped, by key name and by value; nulls are dropped.
    assert r["captured"][1] == ["brief_signup", {"source": "footer"}]
    assert r["captured"][2] == ["checkout_started", {"plan": "pro"}, {"transport": "sendBeacon"}]
    # tdTrack() with no event sends nothing; non-object props become {}.
    assert r["captured"][3:] == [["x", {}]]
    assert r["identified"] == [["user-123"]]  # second identical call skipped
    assert r["resets"] == [[]]
    assert r["threw"] is False


def test_own_site_referrer_is_blank():
    r = _run(TEST_KEY, stub=True, referrer="https://tickerdesk.io/pricing")
    assert r["captured"][0][1]["referrer_domain"] == ""


def test_do_not_track_switches_it_off():
    r = _run(TEST_KEY, stub=True, dnt=True)
    assert r["on"] is False and r["captured"] == [] and r["appended"] == []


def test_bad_key_switches_it_off():
    r = _run("not-a-key", stub=True)
    assert r["on"] is False and r["captured"] == []


def test_loader_uses_privacy_settings():
    r = _run(TEST_KEY)
    assert [a.get("src") for a in r["appended"]] == [
        "https://us-assets.i.posthog.com/static/array.js"
    ]
    (token, cfg, _name) = r["init"][0]
    assert token == TEST_KEY
    assert cfg["api_host"] == "https://us.i.posthog.com"
    assert cfg["persistence"] == "sessionStorage"  # no cookies
    assert cfg["autocapture"] is False  # nothing typed into a form is read
    assert cfg["capture_pageview"] is False
    assert cfg["disable_session_recording"] is True
    assert cfg["respect_dnt"] is True
    assert cfg["person_profiles"] == "identified_only"
    # Calls before PostHog's script arrives are queued, then replayed.
    assert ["capture", "page_view"] == r["queued"][0][:2]


def test_urls_lose_tokens_before_sending():
    r = _run(TEST_KEY)
    p = r["scrubbed"]["properties"]
    assert p["$current_url"] == "https://tickerdesk.io/?utm_source=x"
    assert p["$referrer"] == "https://t.co/abc"
    assert p["other"] == "kept"
    assert r["clean"] == "https://tickerdesk.io/a?utm_source=x&utm_medium=m"


PAGES = [
    "index.html",
    "privacy.html",
    "terms.html",
    "glossary.html",
    "sample-report.html",
    "transparency.html",
    "vcp-screener.html",
    "options-flow-scanner.html",
    "stock-market-morning-brief.html",
]


@pytest.mark.parametrize("page", PAGES)
def test_every_page_loads_the_helper(page):
    with open(os.path.join(DOCS, page), encoding="utf-8") as f:
        html = f.read()
    assert '<script src="/analytics.js" defer></script>' in html


def test_landing_page_generator_includes_helper_and_signup_event():
    with open(os.path.join(ROOT, "landing_pages.py"), encoding="utf-8") as f:
        src = f.read()
    assert src.count('<script src="/analytics.js" defer></script>') == 2
    assert 'window.tdTrack("brief_signup"' in src


def test_index_csp_allows_only_the_posthog_hosts_it_needs():
    with open(os.path.join(DOCS, "index.html"), encoding="utf-8") as f:
        html = f.read()
    csp = html[html.index('http-equiv="Content-Security-Policy"') :]
    csp = csp[: csp.index('" />')]
    script = re.search(r"script-src ([^;]+);", csp).group(1)
    connect = re.search(r"connect-src ([^;]+);", csp).group(1)
    assert "https://us-assets.i.posthog.com" in script.split()
    assert "https://us.i.posthog.com" not in script.split()
    assert "https://us.i.posthog.com" in connect.split()
    assert "*" not in csp  # no wildcards


def test_index_fires_the_funnel_events():
    with open(os.path.join(DOCS, "index.html"), encoding="utf-8") as f:
        html = f.read()
    for ev in [
        "brief_signup",
        "account_created",
        "report_requested",
        "upgrade_clicked",
        "checkout_started",
    ]:
        assert 'window.tdTrack("' + ev + '"' in html, ev
    assert html.count('window.tdTrack("checkout_started"') == 2  # modal + pricing tab
    assert html.count('window.tdTrack("upgrade_clicked"') == 2
    assert "window.tdIdentify(CURRENT_USER ? CURRENT_USER.id : null)" in html
    # The checkout event goes out before the page leaves for Stripe.
    for m in re.finditer(r'window\.tdTrack\("checkout_started"', html):
        assert "window.location.href = data.url" in html[m.end() : m.end() + 300]
