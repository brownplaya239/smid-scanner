"""test_brief_signups.py — the Daily Brief email list, end to end.

The site's sign-up form used to write straight to Supabase, ignore the
result and always say "You're on the list"; nothing was ever saved, and
nothing read the table anyway. These tests pin the replacement:

  * daily_brief.py picks up confirmed, not-unsubscribed sign-ups, skips
    anyone who already gets the brief through their account, and gives
    each one an unsubscribe link signed for their own address;
  * the send gate accepts those links and still blocks placeholders;
  * the postal address (CAN-SPAM) appears in both email bodies when set;
  * the worker's /brief/subscribe, /brief/confirm and /unsubscribe agree
    with the Python signer (run in Node against a fake Supabase/Resend).

    uv run pytest test_brief_signups.py
"""

import functools
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import urllib.parse

import pytest

import brief_model as BM
import brief_render as BR
import brief_text as BX
import daily_brief as DB

KEY = "test-service-key"
_BASE = os.path.dirname(os.path.abspath(__file__))


def _sig(msg):
    return hmac.new(KEY.encode(), msg.encode(), hashlib.sha256).hexdigest()[:40]


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(DB, "SUPABASE_SERVICE_KEY", KEY)
    monkeypatch.setattr(DB, "WORKER_URL", "https://api.tickerdesk.io")


# ── who gets the list brief ────────────────────────────────────────────────
ROWS = [
    {"email": "Reader@Example.org", "confirmed_at": "2026-11-16T12:00:00Z"},
    {"email": "member@x.io", "confirmed_at": "2026-11-16T12:00:00Z"},
    {"email": "pending@x.io", "confirmed_at": None},
    {
        "email": "left@x.io",
        "confirmed_at": "2026-11-01T12:00:00Z",
        "unsubscribed_at": "2026-11-10T12:00:00Z",
    },
    {"email": "reader@example.org", "confirmed_at": "2026-11-16T12:00:00Z"},
    {"email": "not-an-email", "confirmed_at": "2026-11-16T12:00:00Z"},
]


def test_list_recipients_are_confirmed_deduped_and_market_only():
    out = DB.list_subscriber_recipients(ROWS, {"Member@X.io"})
    assert [u["email"] for u in out] == ["reader@example.org"]
    u = out[0]
    assert u["list_subscriber"] is True
    assert u["user_id"] == "" and u["tickers"] == []


def test_list_recipients_respect_me_filter():
    assert DB.list_subscriber_recipients(ROWS, set(), "nobody@x.io") == []
    out = DB.list_subscriber_recipients(ROWS, set(), "READER@example.org")
    assert [u["email"] for u in out] == ["reader@example.org"]


# ── unsubscribe links ──────────────────────────────────────────────────────
def test_email_unsub_link_is_signed_for_the_address(key):
    url = DB.unsub_url_email(" Reader@Example.org ")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert url.startswith("https://api.tickerdesk.io/unsubscribe?")
    assert q["e"] == ["reader@example.org"]
    assert q["t"] == [_sig("brief-unsub:reader@example.org")]
    # an email token must never equal a user-id token over the same text
    assert q["t"][0] != DB._unsub_sig("reader@example.org")


def test_recipient_unsub_url_routes_by_recipient_kind(key):
    acct = DB.recipient_unsub_url({"user_id": "9f3a", "email": "a@b.co"})
    assert acct == DB.unsub_url("9f3a") and "u=9f3a" in acct
    lst = DB.recipient_unsub_url({"user_id": "", "email": "a@b.co", "list_subscriber": True})
    assert lst == DB.unsub_url_email("a@b.co")
    assert DB.recipient_unsub_url({"user_id": "", "email": "a@b.co"}) == ""


def test_send_gate_accepts_list_subscribers_and_blocks_impostors(key):
    good = DB.unsub_url_email("reader@example.org")
    assert DB.production_guard("", good, email="reader@example.org") == []
    # signed for someone else
    assert DB.production_guard("", good, email="other@example.org")
    # placeholder addresses never go out
    demo = DB.unsub_url_email("preview@tickerdesk.io")
    assert DB.production_guard("", demo, email="preview@tickerdesk.io")
    # no recipient at all
    assert DB.production_guard("", good)
    # unsigned
    assert DB.production_guard(
        "",
        "https://api.tickerdesk.io/unsubscribe?e=reader%40example.org",
        email="reader@example.org",
    )
    # account holders unchanged
    assert DB.production_guard("9f3a", DB.unsub_url("9f3a")) == []
    assert DB.production_guard("demo", DB.unsub_url("demo"))


# ── postal address in the footer ───────────────────────────────────────────
ADDR = "TickerDesk, 123 Main St #4, Austin, TX 78701"


def test_postal_address_in_both_bodies(monkeypatch):
    monkeypatch.setattr(BM, "build", functools.partial(BM.build, postal_address=ADDR))
    m = BR._demo_model()
    assert m["meta"]["postal_address"] == ADDR
    assert ADDR in BR.render(m)
    assert "123 Main St #4" in BX.render_text(m)


def test_no_postal_address_when_unset():
    m = BR._demo_model()
    assert m["meta"]["postal_address"] == ""
    assert "123 Main St" not in BR.render(m)
    assert "123 Main St" not in BX.render_text(m)


# ── the worker, run in Node against fake Supabase + Resend ─────────────────
HARNESS = r"""
const mod = await import(process.argv[2]);
const worker = mod.default;
const KEY = process.argv[3];
const env = { SUPABASE_URL: "https://sb.test", SUPABASE_SERVICE_KEY: KEY,
              RESEND_API_KEY: "re_test", FROM_EMAIL: "TickerDesk <brief@tickerdesk.io>" };
let calls = [];
let script = [];   // queued fake responses, consumed in order
globalThis.fetch = async function (url, init) {
  init = init || {};
  calls.push({ url: String(url), method: init.method || "GET", body: init.body || null });
  const next = script.shift() || { status: 200, body: [] };
  return new Response(typeof next.body === "string" ? next.body : JSON.stringify(next.body),
                      { status: next.status });
};
async function run(name, req, fakes) {
  calls = []; script = fakes || [];
  const r = await worker.fetch(req, env, { waitUntil() {} });
  const text = await r.text();
  out[name] = { status: r.status, type: r.headers.get("Content-Type") || "", text, calls };
}
function post(body, ip, type) {
  return new Request("https://api.tickerdesk.io/brief/subscribe", {
    method: "POST",
    headers: { "Content-Type": type || "application/json",
               "Origin": "https://tickerdesk.io", "CF-Connecting-IP": ip || "1.1.1.1" },
    body: typeof body === "string" ? body : JSON.stringify(body) });
}
const out = {};
await run("bad_email", post({ email: "nope", source: "footer" }));
await run("honeypot", post({ email: "a@b.co", website: "http://spam" }, "2.2.2.2"));
await run("new", post({ email: " New@Example.org ", source: "footer" }, "3.3.3.3"), [
  { status: 201, body: "" }, { status: 200, body: [{}] },
  { status: 200, body: { id: "x" } }, { status: 200, body: "" }]);
await run("dup_confirmed", post({ email: "new@example.org" }, "4.4.4.4"), [
  { status: 409, body: { code: "23505" } },
  { status: 200, body: [{ confirmed_at: "2026-11-16T12:00:00Z", unsubscribed_at: null }] }]);
await run("recently_sent", post({ email: "new@example.org" }, "4.4.4.5"), [
  { status: 409, body: {} },
  { status: 200, body: [{ confirm_sent_at: new Date().toISOString() }] }]);
await run("insert_fails", post({ email: "x@example.org" }, "5.5.5.5"), [
  { status: 401, body: { message: "nope" } }]);
await run("resend_fails", post({ email: "y@example.org" }, "5.5.5.6"), [
  { status: 201, body: "" }, { status: 200, body: [{}] }, { status: 422, body: {} }]);
await run("form_post", post("email=z%40example.org&source=lp_vcp", "6.6.6.6",
  "application/x-www-form-urlencoded"), [
  { status: 201, body: "" }, { status: 200, body: [{}] },
  { status: 200, body: {} }, { status: 200, body: "" }]);
for (let i = 0; i < 6; i++) {
  await run("rl" + i, post({ email: "rl" + i + "@example.org" }, "7.7.7.7"), [
    { status: 201, body: "" }, { status: 200, body: [{ confirmed_at: "x" }] }]);
}
const link = JSON.parse(out["new"].calls[2].body).text.match(/https:\S+/)[0];
out.link = link;
await run("confirm", new Request(link), [{ status: 200, body: [{ email: "new@example.org" }] }]);
await run("confirm_missing", new Request(link), [{ status: 200, body: [] }]);
await run("confirm_tampered", new Request(link.replace(/.$/, function (c) {
  return c === "0" ? "1" : "0"; })));
const unsub = process.argv[4];
await run("unsub_email_get", new Request(unsub), [{ status: 200, body: "" }]);
await run("unsub_email_post", new Request(unsub, { method: "POST" }), [{ status: 200, body: "" }]);
await run("unsub_email_bad", new Request(unsub.replace("t=", "t=0")));
await run("unsub_user", new Request(process.argv[5]), [{ status: 200, body: "" }]);
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def worker_run(tmp_path_factory):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    d = tmp_path_factory.mktemp("worker")
    # .mjs so Node loads it as an ES module whatever its version
    shutil.copy(os.path.join(_BASE, "cloudflare-worker", "worker.js"), d / "worker.mjs")
    (d / "harness.mjs").write_text(HARNESS, encoding="utf-8")
    em = "reader@example.org"
    unsub = "https://api.tickerdesk.io/unsubscribe?" + urllib.parse.urlencode(
        {"e": em, "t": _sig("brief-unsub:" + em)}
    )
    unsub_user = "https://api.tickerdesk.io/unsubscribe?u=9f3a&t=" + _sig("9f3a")
    p = subprocess.run(
        [node, str(d / "harness.mjs"), (d / "worker.mjs").as_uri(), KEY, unsub, unsub_user],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


def _calls(r, method=None, url_part=""):
    return [c for c in r["calls"] if (not method or c["method"] == method) and url_part in c["url"]]


def test_subscribe_rejects_bad_email(worker_run):
    r = worker_run["bad_email"]
    assert r["status"] == 400 and "valid email" in json.loads(r["text"])["error"]
    assert r["calls"] == []


def test_subscribe_honeypot_saves_nothing(worker_run):
    r = worker_run["honeypot"]
    assert r["status"] == 200 and r["calls"] == []


def test_subscribe_new_address_inserts_and_sends_confirmation(worker_run):
    r = worker_run["new"]
    assert r["status"] == 200, r["text"]
    assert json.loads(r["text"])["message"] == "Check your inbox to confirm your subscription."
    ins = _calls(r, "POST", "/rest/v1/email_signups")
    assert json.loads(ins[0]["body"]) == {"email": "new@example.org", "source": "footer"}
    mail = _calls(r, "POST", "api.resend.com")
    sent = json.loads(mail[0]["body"])
    assert sent["to"] == ["new@example.org"] and "/brief/confirm?token=" in sent["html"]
    assert sent["from"] == "TickerDesk <brief@tickerdesk.io>"
    assert _calls(r, "PATCH", "email_signups?email=eq.new%40example.org")


def test_subscribe_duplicate_is_not_an_error(worker_run):
    r = worker_run["dup_confirmed"]
    assert r["status"] == 200
    assert json.loads(r["text"])["status"] == "already_subscribed"
    assert not _calls(r, url_part="resend")
    r = worker_run["recently_sent"]
    assert r["status"] == 200 and not _calls(r, url_part="resend")


def test_subscribe_reports_real_failures(worker_run):
    for name in ("insert_fails", "resend_fails"):
        r = worker_run[name]
        assert r["status"] >= 500 and json.loads(r["text"])["ok"] is False, (name, r)


def test_plain_form_post_gets_an_html_page(worker_run):
    r = worker_run["form_post"]
    assert r["status"] == 200 and r["type"].startswith("text/html")
    assert "Check your inbox" in r["text"]
    ins = json.loads(_calls(r, "POST", "/rest/v1/email_signups")[0]["body"])
    assert ins == {"email": "z@example.org", "source": "lp_vcp"}


def test_subscribe_is_rate_limited_per_ip(worker_run):
    assert [worker_run["rl%d" % i]["status"] for i in range(6)] == [200] * 5 + [429]


def test_confirm_link_marks_the_row_confirmed(worker_run):
    r = worker_run["confirm"]
    assert r["status"] == 200 and "subscribed" in r["text"]
    (patch,) = _calls(r, "PATCH", "email_signups?email=eq.new%40example.org")
    body = json.loads(patch["body"])
    assert body["confirmed_at"] and body["unsubscribed_at"] is None
    assert worker_run["confirm_missing"]["status"] == 404
    bad = worker_run["confirm_tampered"]
    assert bad["status"] == 400 and bad["calls"] == []


def test_worker_accepts_python_signed_unsubscribe(worker_run):
    for name in ("unsub_email_get", "unsub_email_post"):
        r = worker_run[name]
        assert r["status"] == 200, (name, r["text"])
        (patch,) = _calls(r, "PATCH", "email_signups?email=eq.reader%40example.org")
        assert json.loads(patch["body"])["unsubscribed_at"]
    bad = worker_run["unsub_email_bad"]
    assert "invalid or has expired" in bad["text"] and bad["calls"] == []
    r = worker_run["unsub_user"]
    (patch,) = _calls(r, "PATCH", "profiles?id=eq.9f3a")
    assert json.loads(patch["body"]) == {"daily_brief_enabled": False}
