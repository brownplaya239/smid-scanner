"""News wire health (2026-11-16): the honest "Live" label, the garbled-text
repair, the Google News filter, and the worker's poll/alert timing.

The site and worker functions are pulled out of docs/index.html and
cloudflare-worker/worker.js and run in Node (skipped when Node is missing);
the relay filter is plain Python."""

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

import macro_news_relay as relay
from flow_site_js_test import _extract

ROOT = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(ROOT, "docs", "index.html")
WORKER = os.path.join(ROOT, "cloudflare-worker", "worker.js")
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")

GARBLED = "Fedâ€™s Warsh says â€œpatienceâ€\u009d"
CLEAN = "Fed’s Warsh says “patience”"


def _const(src, name):
    """Source of a top-level `const NAME = ...;` statement (may span lines,
    may end in a // comment)."""
    m = re.search(
        r"^const " + name + r" = .*?;[ \t]*(?://[^\n]*)?\r?\n", src, re.DOTALL | re.MULTILINE
    )
    assert m, name
    return m.group(0)


def _node(path, names, body, consts=()):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    js = "\n".join([_const(src, c) for c in consts] + [_extract(src, n) for n in names])
    # a UTF-8 file, not `node -e`: the Windows command line mangles non-ASCII
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as f:
        f.write(js + "\n" + body)
    try:
        out = subprocess.run(
            [NODE, f.name], capture_output=True, encoding="utf-8", check=True, timeout=30
        )
    finally:
        os.unlink(f.name)
    return json.loads(out.stdout)


def _ms(iso):
    return f'Date.parse("{iso}")'


# ── Site: "Live" only when the newest headline is recent ──────────────
SITE_FNS = ["tdNewsEtParts", "tdNewsFreshness", "tdFixMojibake", "tdNewsLegacyJunk"]

# (case, newest headline, now, expected live?) — Nov dates are EST (UTC-5),
# the July one EDT (UTC-4).
FRESH_CASES = [
    ("market hours, 10 min old", "2026-11-16T18:30:00Z", "2026-11-16T18:40:00Z", True),
    ("market hours, 16 min old", "2026-11-16T18:24:00Z", "2026-11-16T18:40:00Z", False),
    ("Monday, newest from Friday", "2026-11-13T23:43:00Z", "2026-11-16T18:40:00Z", False),
    ("EDT market hours, 14 min old", "2026-07-15T13:21:00Z", "2026-07-15T13:35:00Z", True),
    ("pre-market, 60 min old", "2026-11-16T12:00:00Z", "2026-11-16T13:00:00Z", True),
    ("pre-market, 3 h old", "2026-11-16T10:00:00Z", "2026-11-16T13:00:00Z", False),
    ("Saturday, 90 min old", "2026-11-14T13:30:00Z", "2026-11-14T15:00:00Z", True),
    ("Saturday, 2.5 h old", "2026-11-14T12:30:00Z", "2026-11-14T15:00:00Z", False),
    ("after the close, 40 min old", "2026-11-16T21:20:00Z", "2026-11-16T22:00:00Z", True),
]


@needs_node
def test_site_freshness_rule():
    body = (
        "console.log(JSON.stringify(["
        + ",".join(f"tdNewsFreshness({json.dumps(n)}, {_ms(now)})" for _, n, now, _ in FRESH_CASES)
        + "]));"
    )
    got = _node(HTML, SITE_FNS, body)
    for (case, _, _, live), f in zip(FRESH_CASES, got, strict=True):
        assert f["live"] is live, (case, f)
        if live:
            assert f["label"].startswith("Live · ") and f["label"].endswith(" ET"), f
        else:
            assert f["label"].startswith("Last headline "), f


@needs_node
def test_site_stale_label_names_the_day_and_time_in_et():
    got = _node(
        HTML,
        SITE_FNS,
        "console.log(JSON.stringify([tdNewsFreshness('2026-11-13T23:43:00Z', "
        + _ms("2026-11-16T18:40:00Z")
        + "), tdNewsFreshness(null, Date.now()), tdNewsFreshness('2026-11-16T18:30:00Z', "
        + _ms("2026-11-16T18:40:00Z")
        + ")]));",
    )
    assert got[0] == {"live": False, "label": "Last headline Fri, Nov 13, 6:43 PM ET"}
    assert got[1]["live"] is False
    assert got[2] == {"live": True, "label": "Live · 1:30 PM ET"}


@needs_node
def test_site_mojibake_repair_and_legacy_filter():
    cases = [GARBLED, "Google News Â· Markets", "café ’ok \U0001f680", "", None]
    got = _node(
        HTML,
        SITE_FNS,
        "console.log(JSON.stringify({fix: "
        + json.dumps(cases)
        + ".map(tdFixMojibake), junk: "
        + json.dumps(
            ["Google News · Markets", "Google News Â· Markets", "CNBC", "Reuters", "", None]
        )
        + ".map(tdNewsLegacyJunk)}));",
    )
    assert got["fix"] == [CLEAN, "Google News · Markets", cases[2], "", ""]
    assert got["junk"] == [True, True, False, False, False, False]


# ── Worker: poll schedule, stall alert, row mapping, same repair ──────
WORKER_FNS = [
    "newsEtParts",
    "newsPollDue",
    "newsMarketHours",
    "newsStallAlertDue",
    "fixMojibake",
    "decodeNewsText",
    "alpacaToNewsRow",
]
WORKER_CONSTS = ["NEWS_STALL_MIN", "NEWS_ALERT_EVERY_MS", "CP1252_BYTE"]


@needs_node
def test_worker_poll_schedule():
    cases = [
        ("Mon 4:00 AM ET", "2026-11-16T09:00:00Z", True),
        ("Mon 3:59 AM ET", "2026-11-16T08:59:00Z", False),
        ("Mon 1:41 PM ET", "2026-11-16T18:41:00Z", True),
        ("Mon 7:59 PM ET", "2026-11-17T00:59:00Z", True),
        ("Mon 8:01 PM ET", "2026-11-17T01:01:00Z", False),
        ("Mon 8:15 PM ET", "2026-11-17T01:15:00Z", True),
        ("Sat 10:07 AM ET", "2026-11-14T15:07:00Z", False),
        ("Sat 10:15 AM ET", "2026-11-14T15:15:00Z", True),
    ]
    got = _node(
        WORKER,
        WORKER_FNS,
        "console.log(JSON.stringify(["
        + ",".join(f"newsPollDue({_ms(t)})" for _, t, _ in cases)
        + "]));",
        WORKER_CONSTS,
    )
    assert got == [due for _, _, due in cases], list(
        zip([c for c, _, _ in cases], got, strict=True)
    )


@needs_node
def test_worker_stall_alert_rule():
    now = "2026-11-16T18:40:00Z"  # Mon 1:40 PM ET
    cases = [
        ("fresh row, 5 min old", now, "2026-11-16T18:35:00Z", None, False),
        ("19 min old", now, "2026-11-16T18:21:00Z", None, False),
        ("21 min old, never alerted", now, "2026-11-16T18:19:00Z", None, True),
        ("silent since Friday", now, "2026-11-13T23:43:00Z", None, True),
        ("empty table", now, None, None, True),
        ("alerted 30 min ago", now, "2026-11-13T23:43:00Z", "2026-11-16T18:10:00Z", False),
        ("alerted 61 min ago", now, "2026-11-13T23:43:00Z", "2026-11-16T17:39:00Z", True),
        ("Saturday, silent", "2026-11-14T15:00:00Z", "2026-11-13T23:43:00Z", None, False),
        ("pre-market, silent", "2026-11-16T13:00:00Z", "2026-11-13T23:43:00Z", None, False),
    ]

    def arg(t):
        return _ms(t) if t else "0"

    got = _node(
        WORKER,
        WORKER_FNS,
        "console.log(JSON.stringify(["
        + ",".join(f"newsStallAlertDue({arg(n)}, {arg(r)}, {arg(a)})" for _, n, r, a, _ in cases)
        + "]));",
        WORKER_CONSTS,
    )
    for (case, *_, due), g in zip(cases, got, strict=True):
        assert g is due, case


@needs_node
def test_worker_alpaca_row_and_repair():
    item = {
        "id": 60848607,
        "headline": "What&#39;s next for Fedâ€™s &amp; banks",
        "summary": "&quot;Big&quot; week",
        "source": "benzinga",
        "url": "https://example.com/a",
        "symbols": ["GIL", "SPY"],
        "created_at": "2026-11-16T18:30:00Z",
    }
    got = _node(
        WORKER,
        WORKER_FNS,
        "console.log(JSON.stringify({row: alpacaToNewsRow("
        + json.dumps(item)
        + "), none: [alpacaToNewsRow(null), alpacaToNewsRow({id: 1, headline: ''})],"
        + " fix: fixMojibake("
        + json.dumps(GARBLED)
        + ")}));",
        WORKER_CONSTS,
    )
    assert got["row"] == {
        "id": "60848607",
        "headline": "What's next for Fed’s & banks",
        "summary": '"Big" week',
        "source": "benzinga",
        "url": "https://example.com/a",
        "symbols": ["GIL", "SPY"],
        "published_at": "2026-11-16T18:30:00Z",
    }
    assert got["none"] == [None, None]
    assert got["fix"] == CLEAN


def test_worker_macro_error_hides_key_details():
    with open(WORKER, encoding="utf-8") as f:
        src = _extract(f.read(), "fetchFinnhubMacroNews")
    assert "keylen" not in src
    assert "key.length" not in src
    assert src.count('error: "macro feed unavailable"') == 3


# ── Relay (GitHub Action): Google News filter + the same repair ───────
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Your guide to winter farmers markets around Milwaukee - WISN", None),
        ("Caught on camera: Used cooking oil stolen from Naples businesses - 13WHAM-TV", None),
        ("105 housing markets to enter 2027 above pre-pandemic inventory levels - ResiClub", None),
        (
            "Oil falls 5% as OPEC talks stall - Reuters",
            ("Oil falls 5% as OPEC talks stall", "Reuters"),
        ),
        (
            "Fed holds rates steady - The Wall Street Journal",
            ("Fed holds rates steady", "The Wall Street Journal"),
        ),
        (
            "Acme (NASDAQ:ACME) jumps on guidance - Small Paper",
            ("Acme (NASDAQ:ACME) jumps on guidance", "Small Paper"),
        ),
        ("$TSLA slides after recall - Local Blog", ("$TSLA slides after recall", "Local Blog")),
        ("No publisher suffix here", None),
        ("", None),
    ],
)
def test_relay_google_news_filter(title, expected):
    assert relay.google_news_item(title) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (GARBLED, CLEAN),
        ("Google News Â· Markets", "Google News · Markets"),
        ("café ’ok \U0001f680", "café ’ok \U0001f680"),
        ("Plain ASCII headline", "Plain ASCII headline"),
        ("", ""),
    ],
)
def test_relay_fix_mojibake(text, expected):
    assert relay.fix_mojibake(text) == expected


def test_relay_filters_google_items_and_keeps_others(monkeypatch):
    rss = (
        b'<?xml version="1.0" encoding="UTF-8"?><rss><channel>'
        b"<item><title>Your guide to winter farmers markets - WISN</title>"
        b"<link>https://a</link></item>"
        b"<item><title>Brent drops 4% - Reuters</title><link>https://b</link></item>"
        b"</channel></rss>"
    )

    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    monkeypatch.setattr(
        relay, "RSS_FEEDS", ((relay.GOOGLE_NEWS, "https://g"), ("CNBC", "https://c"))
    )
    monkeypatch.setattr(relay.urllib.request, "urlopen", lambda req, timeout: Resp(rss))
    got = sorted((a["source"], a["headline"]) for a in relay.fetch_rss())
    # CNBC items pass through unfiltered; Google News keeps only Reuters,
    # re-labelled with the real publisher and the " - Reuters" suffix gone.
    assert ("Reuters", "Brent drops 4%") in got
    assert ("CNBC", "Your guide to winter farmers markets - WISN") in got
    assert all(src != relay.GOOGLE_NEWS for src, _ in got)
    assert not any(src == "Reuters" and "farmers" in h for src, h in got)
