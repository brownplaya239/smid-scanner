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
    """Source of a `const NAME = ...;` statement at the start of a line (may
    be indented, may span lines, may end in a // comment)."""
    m = re.search(
        r"^[ \t]*const " + name + r" = .*?;[ \t]*(?://[^\n]*)?\r?\n",
        src,
        re.DOTALL | re.MULTILINE,
    )
    assert m, name
    return m.group(0)


def _fn(src, name):
    """_extract, keeping a leading `async` keyword."""
    code = _extract(src, name)
    return ("async " + code) if f"async function {name}(" in src else code


def _node(path, names, body, consts=()):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    # ALL_CAPS entries in `names` are constants too
    js = "\n".join(
        [_const(src, c) for c in consts]
        + [_const(src, n) if n.isupper() else _fn(src, n) for n in names]
    )
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
SITE_FNS = [
    "TD_NEWS_FUTURE_SLACK_MS",
    "tdNewsEtParts",
    "tdNewsNewest",
    "tdNewsFreshness",
    "tdFixMojibake",
    "tdNewsLegacyJunk",
]

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


@needs_node
def test_site_label_goes_stale_while_fetches_fail():
    body = r"""
    let NOW = Date.parse("2026-11-16T18:30:00Z");   // Mon 1:30 PM ET
    Date.now = () => NOW;
    const timers = [];
    globalThis.setInterval = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
    const labels = [];
    const clock = tdNewsClock((f) => labels.push(f), 30000);
    let offline = false;
    // same wiring as pollGeneral: success -> seen(newest), any failure -> failed()
    function poll() {
      return (offline ? Promise.reject(new Error("offline"))
                      : Promise.resolve([{ published_at: "2026-11-16T18:29:00Z" }]))
        .then((rows) => clock.seen(rows[0].published_at))
        .catch(() => clock.failed());
    }
    (async () => {
      const before = labels.length;
      await poll();
      const first = labels[labels.length - 1];
      offline = true;
      for (let i = 0; i < 40; i++) {           // 20 minutes of 30s ticks
        NOW += 30000;
        timers.forEach((t) => t.fn());
        if (i % 2 === 0) await poll();
      }
      const last = labels[labels.length - 1];
      // a page whose very first fetch fails says so instead of "Checking..."
      const l2 = []; const c2 = tdNewsClock((f) => l2.push(f), 30000);
      c2.failed();
      process.stdout.write(JSON.stringify({ before, first, last,
        every: timers.map((t) => t.ms), never: l2[0] }));
    })();
    """
    got = _node(HTML, [*SITE_FNS, "tdNewsClock"], body)
    assert got["before"] == 0  # nothing stamped before the first answer
    assert got["first"] == {"live": True, "label": "Live · 1:29 PM ET"}
    assert got["last"] == {"live": False, "label": "Last headline Mon, Nov 16, 1:29 PM ET"}
    assert got["every"] == [30000, 30000]
    assert got["never"]["live"] is False


@needs_node
def test_site_ignores_a_future_dated_headline():
    # A tomorrow-dated story used to keep the wire "Live" all day (any
    # negative age passed) and, once cached as "newest", it never let go.
    body = r"""
    let NOW = Date.parse("2026-11-16T18:30:00Z");   // Mon 1:30 PM ET
    Date.now = () => NOW;
    globalThis.setInterval = () => 0;
    const TOMORROW = "2026-11-17T18:00:00Z", OLD = "2026-11-16T15:00:00Z";
    const labels = [];
    const clock = tdNewsClock((f) => labels.push(f), 30000);
    clock.seen([TOMORROW, OLD]);
    const withBad = labels[labels.length - 1];
    clock.seen(["2026-11-16T18:28:00Z", OLD]);
    const fresh = labels[labels.length - 1];
    clock.seen([OLD]);                     // bad / newer rows gone: recomputed
    const fixed = labels[labels.length - 1];
    clock.seen(["2026-11-16T18:33:00Z"]);  // 3 min ahead: clock skew, allowed
    const skew = labels[labels.length - 1];
    process.stdout.write(JSON.stringify({ withBad, fixed, fresh, skew,
      direct: tdNewsFreshness(TOMORROW, NOW),
      newest: tdNewsNewest([TOMORROW, OLD, "junk", null], NOW) }));
    """
    got = _node(HTML, [*SITE_FNS, "tdNewsClock"], body)
    stale = {"live": False, "label": "Last headline Mon, Nov 16, 10:00 AM ET"}
    assert got["withBad"] == stale
    assert got["fresh"] == {"live": True, "label": "Live · 1:28 PM ET"}
    assert got["fixed"] == stale  # the cache no longer holds the newer time
    assert got["skew"]["live"] is True
    assert got["direct"]["live"] is False
    assert got["newest"] == "2026-11-16T15:00:00Z"


def test_site_wires_the_independent_timers():
    with open(HTML, encoding="utf-8") as f:
        src = f.read()
    assert ".catch(function () { wireClock.failed(); });" in src
    assert "wireClock.seen(rows.map(" in src  # every row, not just rows[0]
    assert "setInterval(setLiveStatus, 30000);" in src
    assert "stampFreshness" not in src


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
# Everything the poller and the alert need, for the fake-network runs below.
WIRE_FNS = [
    *WORKER_FNS,
    "newsPollStart",
    "newsKeyCmp",
    "newsCursorAfter",
    "newsFetchRetry",
    "readNewsCursor",
    "writeNewsCursor",
    "newsSupaHeaders",
    "pollAlpacaNews",
    "claimNewsAlert",
    "checkNewsStall",
    "slackApi",
]
WIRE_CONSTS = [
    *WORKER_CONSTS,
    "NEWS_THROTTLE_KEY",
    "NEWS_CURSOR_KEY",
    "NEWS_PAGE_LIMIT",
    "NEWS_MAX_PAGES",
    "NEWS_OVERLAP_MS",
    "NEWS_FIRST_LOOKBACK_MS",
    "NEWS_KEEP_MS",
]


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
        ("fresh row, 5 min old", now, "2026-11-16T18:35:00Z", False),
        ("19 min old", now, "2026-11-16T18:21:00Z", False),
        ("21 min old", now, "2026-11-16T18:19:00Z", True),
        ("silent since Friday", now, "2026-11-13T23:43:00Z", True),
        ("empty table", now, None, True),
        ("Saturday, silent", "2026-11-14T15:00:00Z", "2026-11-13T23:43:00Z", False),
        ("pre-market, silent", "2026-11-16T13:00:00Z", "2026-11-13T23:43:00Z", False),
    ]

    def arg(t):
        return _ms(t) if t else "0"

    got = _node(
        WORKER,
        WORKER_FNS,
        "console.log(JSON.stringify(["
        + ",".join(f"newsStallAlertDue({arg(n)}, {arg(r)})" for _, n, r, _ in cases)
        + "]));",
        WORKER_CONSTS,
    )
    for (case, *_, due), g in zip(cases, got, strict=True):
        assert g is due, case


# A fake network for the worker: Alpaca (paged, oldest first, honours
# start/limit/page_token), Supabase news_live (upsert ignoring duplicates;
# newest-row query honouring select/order/lte), the ops_alert_throttle row
# (a conditional update that, like Postgres, checks and sets in one step)
# and Slack. Each knob makes one thing fail on purpose.
WIRE_HARNESS = r"""
const NOW = Date.parse("2026-11-16T18:40:00Z");   // Mon 1:40 PM ET
const iso = (ms) => new Date(ms).toISOString();
const J = (x, st) => new Response(JSON.stringify(x),
  { status: st || 200, headers: { "content-type": "application/json" } });
let W;
function world(o) {
  o = o || {};
  W = { items: o.items || [], table: new Map(), posts: [], r2: new Map(),
        throttle: { key: "news_stall", last_alert_at: "1970-01-01T00:00:00.000Z" },
        throttleMode: o.throttleMode || "ok", patchDelay: o.patchDelay || 0,
        alpacaFails: o.alpacaFails || 0, upsertFails: o.upsertFails || 0,
        upsertStatus: o.upsertStatus || 500, alpacaCalls: 0, patches: 0,
        readFails: o.readFails || 0, tokenFails: o.tokenFails || 0,
        insertAt: NOW };
  if (o.cursor) W.r2.set(NEWS_CURSOR_KEY, JSON.stringify(o.cursor));
  (o.rows || []).forEach((r) => W.table.set(r.id, r));
  W.env = Object.assign({
    SUPABASE_URL: "https://supa.test", SUPABASE_SERVICE_KEY: "svc",
    SUPABASE_ANON_KEY: "anon", ALPACA_KEY: "k", ALPACA_SECRET: "s",
    SLACK_BOT_TOKEN: "xoxb", SLACK_ALERT_CHANNEL: "C1",
    DATA: { get: async (k) => {
              if (W.readFails > 0) { W.readFails--; throw new Error("R2 503"); }
              return W.r2.has(k)
                ? { json: async () => JSON.parse(W.r2.get(k)) } : null; },
            put: async (k, v) => { W.r2.set(k, v); } },
  }, o.env || {});
  return W;
}
function items(n, firstMs, stepMs) {
  return Array.from({ length: n }, (_, i) => ({ id: 1000 + i,
    headline: "Story " + i, created_at: iso(firstMs + i * stepMs) }));
}
globalThis.fetch = async function (url, init) {
  init = init || {};
  const u = new URL(url), m = init.method || "GET";
  if (u.host === "data.alpaca.markets") {
    W.alpacaCalls++;
    if (W.alpacaFails > 0) { W.alpacaFails--; return J({ message: "slow" }, 429); }
    if (u.searchParams.get("page_token") && W.tokenFails > 0) {
      W.tokenFails--; return J({ message: "invalid page token" }, 422);
    }
    const start = Date.parse(u.searchParams.get("start"));
    const lim = +u.searchParams.get("limit"), off = +(u.searchParams.get("page_token") || 0);
    const all = W.items.filter((i) => Date.parse(i.created_at) >= start)
      .sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at));
    return J({ news: all.slice(off, off + lim),
               next_page_token: off + lim < all.length ? String(off + lim) : null });
  }
  if (u.pathname === "/rest/v1/news_live" && m === "POST") {
    if (W.upsertFails > 0) { W.upsertFails--; return J({}, W.upsertStatus); }
    JSON.parse(init.body).forEach((r) => {
      if (!W.table.has(r.id)) W.table.set(r.id, Object.assign({ created_at: iso(W.insertAt) }, r));
    });
    return new Response(null, { status: 201 });
  }
  if (u.pathname === "/rest/v1/news_live" && m === "GET") {
    const field = u.searchParams.get("order").split(".")[0], sel = u.searchParams.get("select");
    let rows = [...W.table.values()];
    const f = u.searchParams.get(field);
    if (f && f.startsWith("lte.")) rows = rows.filter((r) => Date.parse(r[field]) <= Date.parse(f.slice(4)));
    rows.sort((a, b) => Date.parse(b[field]) - Date.parse(a[field]));
    return J(rows.slice(0, 1).map((r) => ({ [sel]: r[sel] })));
  }
  if (u.pathname === "/rest/v1/ops_alert_throttle" && m === "PATCH") {
    W.patches++;
    if (W.throttleMode === "missing") return J({ code: "PGRST205" }, 404);
    if (W.throttleMode === "error") return J({ message: "boom" }, 500);
    if (W.throttleMode === "throw") throw new TypeError("network down");
    await new Promise((r) => setTimeout(r, W.patchDelay));   // both in flight
    const cutoff = Date.parse(u.searchParams.get("last_alert_at").slice(3));
    const row = W.throttle;
    if (u.searchParams.get("key") !== "eq." + row.key ||
        !(Date.parse(row.last_alert_at) < cutoff)) return J([]);
    row.last_alert_at = JSON.parse(init.body).last_alert_at;
    return J([Object.assign({}, row)]);
  }
  if (u.host === "slack.com") { W.posts.push(JSON.parse(init.body)); return J({ ok: true }); }
  throw new Error("unexpected " + m + " " + url);
};
console.log = () => {};   // keep stdout for the JSON result
const out = (x) => process.stdout.write(JSON.stringify(x));
const cursorOf = () => JSON.parse(W.r2.get(NEWS_CURSOR_KEY) || "null");
"""


def _wire(body):
    return _node(
        WORKER,
        WIRE_FNS,
        WIRE_HARNESS
        + "(async () => {\n"
        + body
        + "\n})().catch((e) => { process.stderr.write(String((e && e.stack) || e));"
        + " process.exit(1); });",
        WIRE_CONSTS,
    )


@needs_node
def test_worker_poll_pages_through_a_burst():
    got = _wire("""
      world({ items: items(120, NOW - 3600000, 30000) });   // 120 in the last hour
      const r = await pollAlpacaNews(W.env, NOW, 0);
      out({ r, stored: W.table.size, cursor: cursorOf(),
            newest: W.items[119].created_at });
    """)
    assert got["r"]["pages"] == 3 and got["r"]["wrote"] == 120
    assert got["stored"] == 120
    assert got["cursor"]["watermark_ts"] == got["newest"]
    assert got["cursor"]["watermark_id"] == "1119"
    assert got["cursor"]["next_page_token"] is None and got["cursor"]["pass_start"] is None


@needs_node
def test_worker_poll_recovers_after_downtime_without_losing_stories():
    got = _wire("""
      const cur = { watermark_ts: iso(NOW - 10 * 3600000), watermark_id: "1" };
      world({ items: items(400, NOW - 10 * 3600000 + 60000, 90000), cursor: cur });
      const r1 = await pollAlpacaNews(W.env, NOW, 0);
      const after1 = W.table.size, c1 = cursorOf();
      const r2 = await pollAlpacaNews(W.env, NOW + 60000, 0);
      out({ r1, after1, c1, r2, after2: W.table.size, c2: cursorOf(),
            item299: W.items[299].created_at, item399: W.items[399].created_at });
    """)
    # bounded: one run reads at most 6 pages x 50, then the next run resumes
    assert got["r1"]["pages"] == 6 and got["after1"] == 300
    assert got["c1"]["watermark_ts"] == got["item299"] and got["c1"]["next_page_token"]
    assert got["after2"] == 400 and got["r2"]["pages"] == 2
    assert got["c2"]["watermark_ts"] == got["item399"] and got["c2"]["next_page_token"] is None


@needs_node
def test_worker_poll_drains_a_dense_backlog_in_two_runs():
    # 600 stories one second apart: the old "restart 5 min before the
    # bookmark" logic re-read the same 300 every run and never got further.
    got = _wire("""
      world({ items: items(600, NOW - 1800000, 1000) });
      await pollAlpacaNews(W.env, NOW, 0);
      const after1 = W.table.size;
      await pollAlpacaNews(W.env, NOW + 60000, 0);
      out({ after1, after2: W.table.size, c: cursorOf(),
            last: W.items[599].created_at });
    """)
    assert got["after1"] == 300 and got["after2"] == 600
    assert got["c"]["watermark_ts"] == got["last"] and got["c"]["watermark_id"] == "1599"
    assert got["c"]["next_page_token"] is None


@needs_node
def test_worker_poll_drains_stories_that_share_one_timestamp():
    got = _wire("""
      world({ items: items(400, NOW - 600000, 0) });   // all at the same second
      await pollAlpacaNews(W.env, NOW, 0);
      const after1 = W.table.size, c1 = cursorOf();
      await pollAlpacaNews(W.env, NOW + 60000, 0);
      const after2 = W.table.size;
      // a later story still arrives on the following (fresh) pass
      W.items.push({ id: 5000, headline: "Later", created_at: iso(NOW + 60000) });
      await pollAlpacaNews(W.env, NOW + 120000, 0);
      await pollAlpacaNews(W.env, NOW + 180000, 0);
      out({ after1, c1, after2, after4: W.table.size, c4: cursorOf() });
    """)
    assert got["after1"] == 300 and got["c1"]["watermark_id"] == "1299"
    assert got["after2"] == 400
    assert got["after4"] == 401 and got["c4"]["watermark_id"] == "5000"


@needs_node
def test_worker_poll_recovers_from_an_expired_page_token():
    got = _wire("""
      world({ items: items(400, NOW - 3600000, 5000) });
      await pollAlpacaNews(W.env, NOW, 0);
      const c1 = cursorOf();
      W.tokenFails = 1;      // Alpaca no longer accepts the saved token
      const r2 = await pollAlpacaNews(W.env, NOW + 60000, 0);
      await pollAlpacaNews(W.env, NOW + 120000, 0);
      out({ token1: c1.next_page_token, r2, stored: W.table.size, c: cursorOf(),
            last: W.items[399].created_at });
    """)
    assert got["token1"]
    assert got["r2"]["wrote"] > 0  # restarted from the watermark in the same run
    assert got["stored"] == 400
    assert got["c"]["watermark_ts"] == got["last"] and got["c"]["next_page_token"] is None


@needs_node
def test_worker_poll_skips_the_run_when_the_bookmark_cannot_be_read():
    # A failed R2 read used to look like "no bookmark": the poll restarted 2 h
    # back and overwrote the bookmark, losing the older backlog for good.
    got = _wire("""
      const cur = { watermark_ts: iso(NOW - 10 * 3600000), watermark_id: "1" };
      world({ items: items(400, NOW - 10 * 3600000 + 60000, 90000), cursor: cur,
              readFails: 1 });
      const r1 = await pollAlpacaNews(W.env, NOW, 0);
      const s1 = W.table.size, c1 = cursorOf(), calls1 = W.alpacaCalls;
      await pollAlpacaNews(W.env, NOW + 60000, 0);
      await pollAlpacaNews(W.env, NOW + 120000, 0);
      out({ r1, s1, c1, calls1, stored: W.table.size, start: cur.watermark_ts });
    """)
    assert got["r1"]["aborted"] is True
    assert got["s1"] == 0 and got["calls1"] == 0
    assert got["c1"]["watermark_ts"] == got["start"]  # untouched
    assert got["stored"] == 400  # the whole 10-hour backlog


@needs_node
def test_worker_poll_starts_two_hours_back_only_when_no_bookmark_exists():
    got = _wire("""
      const old = items(5, NOW - 3 * 3600000, 60000);
      const recent = items(5, NOW - 3600000, 60000).map((x) => ({ ...x, id: x.id + 100 }));
      world({ items: [...old, ...recent] });
      await pollAlpacaNews(W.env, NOW, 0);
      out({ stored: W.table.size, saved: !!cursorOf() });
    """)
    assert got == {"stored": 5, "saved": True}


@needs_node
def test_worker_poll_keeps_the_bookmark_when_the_save_fails():
    got = _wire("""
      const cur = { watermark_ts: iso(NOW - 3600000), watermark_id: "1" };
      // non-retryable 400: the run stops, the bookmark stays put
      world({ items: items(30, NOW - 3000000, 60000), cursor: cur,
              upsertFails: 1, upsertStatus: 400 });
      const r1 = await pollAlpacaNews(W.env, NOW, 0);
      const c1 = cursorOf(), s1 = W.table.size;
      // 500 three times in a row: retried, then the run fails, bookmark kept
      W.upsertFails = 3; W.upsertStatus = 500;
      let threw = false;
      try { await pollAlpacaNews(W.env, NOW, 0); } catch (_) { threw = true; }
      const c2 = cursorOf();
      // storage back: everything lands
      const r3 = await pollAlpacaNews(W.env, NOW + 60000, 0);
      out({ r1, c1, s1, threw, c2, r3, s3: W.table.size, start: cur.watermark_ts });
    """)
    assert got["r1"]["wrote"] == 0 and got["s1"] == 0
    assert got["c1"]["watermark_ts"] == got["start"]
    assert got["threw"] is True and got["c2"]["watermark_ts"] == got["start"]
    assert got["s3"] == 30


@needs_node
def test_worker_poll_retries_alpaca_rate_limits():
    got = _wire("""
      world({ items: items(10, NOW - 600000, 60000), alpacaFails: 2 });
      const r = await pollAlpacaNews(W.env, NOW, 0);
      out({ r, calls: W.alpacaCalls, stored: W.table.size });
    """)
    assert got["calls"] == 3 and got["stored"] == 10


@needs_node
def test_worker_poll_start_and_bookmark_bounds():
    got = _wire("""
      const after = (prev, rows) => newsCursorAfter(prev, rows, NOW);
      out({
        none: newsPollStart(NOW, null) === NOW - 7200000,
        overlap: newsPollStart(NOW, { watermark_ts: iso(NOW - 600000) }) === NOW - 900000,
        capped: newsPollStart(NOW, { watermark_ts: iso(NOW - 10 * 86400000) })
          === NOW - 3 * 86400000,
        future: after(null, [{ id: "9", published_at: iso(NOW + 86400000) }])
          .watermark_ts === iso(NOW),
        keeps: after({ watermark_ts: iso(NOW - 60000), watermark_id: "5" },
          [{ id: "7", published_at: iso(NOW - 600000) }]).watermark_id === "5",
        tie: after({ watermark_ts: iso(NOW - 60000), watermark_id: "5" },
          [{ id: "12", published_at: iso(NOW - 60000) }]).watermark_id === "12",
      });
    """)
    assert got == {
        "none": True,
        "overlap": True,
        "capped": True,
        "future": True,
        "keeps": True,
        "tie": True,
    }


@needs_node
def test_worker_alert_posts_once_when_runs_overlap():
    got = _wire("""
      world({ rows: [{ id: "a", published_at: iso(NOW - 3600000),
                       created_at: iso(NOW - 3600000) }], patchDelay: 5 });
      const both = await Promise.all([checkNewsStall(W.env, NOW), checkNewsStall(W.env, NOW)]);
      const later = await checkNewsStall(W.env, NOW + 30 * 60000);
      const hourLater = await checkNewsStall(W.env, NOW + 61 * 60000);
      out({ both: both.sort(), later, hourLater, posts: W.posts.length,
            channel: W.posts[0] && W.posts[0].channel });
    """)
    assert got["both"] == ["posted", "throttled"]
    assert got["later"] == "throttled"
    assert got["hourLater"] == "posted"
    assert got["posts"] == 2 and got["channel"] == "C1"


@needs_node
@pytest.mark.parametrize("mode", ["missing", "error", "throw", "no-service-key"])
def test_worker_alert_does_not_post_when_throttle_storage_fails(mode):
    env = "{ SUPABASE_SERVICE_KEY: '' }" if mode == "no-service-key" else "{}"
    tmode = "ok" if mode == "no-service-key" else mode
    got = _wire(f"""
      world({{ throttleMode: "{tmode}", env: {env} }});   // empty table: silent
      const res = await checkNewsStall(W.env, NOW);
      out({{ res, posts: W.posts.length }});
    """)
    assert got == {"res": "throttled", "posts": 0}


@needs_node
def test_worker_alert_only_logs_without_a_channel():
    got = _wire("""
      world({ env: { SLACK_ALERT_CHANNEL: "" } });
      const res = await checkNewsStall(W.env, NOW);
      out({ res, posts: W.posts.length, patches: W.patches });
    """)
    assert got == {"res": "log-only", "posts": 0, "patches": 0}


@needs_node
def test_worker_stall_judged_by_publish_time_not_insert_time():
    got = _wire("""
      // inserted a minute ago, but the story itself is 3 hours old
      world({ env: { SLACK_ALERT_CHANNEL: "" },
              rows: [{ id: "old", published_at: iso(NOW - 3 * 3600000),
                       created_at: iso(NOW - 60000) }] });
      const oldInsert = await checkNewsStall(W.env, NOW);
      // a mis-dated story from tomorrow doesn't count as fresh either
      W.table.set("fut", { id: "fut", published_at: iso(NOW + 86400000), created_at: iso(NOW) });
      const future = await checkNewsStall(W.env, NOW);
      W.table.set("new", { id: "new", published_at: iso(NOW - 5 * 60000),
                           created_at: iso(NOW - 5 * 60000) });
      const fresh = await checkNewsStall(W.env, NOW);
      const closed = await checkNewsStall(W.env, Date.parse("2026-11-14T15:00:00Z"));
      out({ oldInsert, future, fresh, closed });
    """)
    assert got == {
        "oldInsert": "log-only",
        "future": "log-only",
        "fresh": "fresh",
        "closed": "closed",
    }


def test_wrangler_alert_channel_defaults_to_empty():
    with open(os.path.join(ROOT, "cloudflare-worker", "wrangler.toml"), encoding="utf-8") as f:
        toml = f.read()
    assert re.search(r'^SLACK_ALERT_CHANNEL = ""', toml, re.MULTILINE)


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
