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


def test_send_gate_verifies_the_signature(key):
    # right shape, wrong signature: the worker would reject it, so the
    # send gate must too
    forged = "https://api.tickerdesk.io/unsubscribe?" + urllib.parse.urlencode(
        {"e": "reader@example.org", "t": "0" * 40}
    )
    assert "unsubscribe signature does not verify" in DB.production_guard(
        "", forged, email="reader@example.org"
    )
    # an account link signed for a different user
    assert DB.production_guard("9f3a", DB.unsub_url("7b21"))
    # a list-style signature can't pass as an account one, or the reverse
    swapped = DB.unsub_url_email("reader@example.org").replace("e=reader%40example.org", "u=9f3a")
    assert DB.production_guard("9f3a", swapped)


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


# ── same-day dedupe across both kinds of recipient ─────────────────────────
class FakeEmailLog:
    """email_log rows plus just enough PostgREST filtering for
    already_sent_today: eq on user_id/kind/brief_date/status, in on email."""

    def __init__(self):
        self.rows: list[dict] = []

    def get(self, path, params=None):
        assert path == "email_log"
        p = {k: urllib.parse.unquote(v) for k, v in (params or {}).items()}
        out = []
        for r in self.rows:
            ok = True
            for col in ("user_id", "kind", "brief_date", "status"):
                if col in p and str(r.get(col)) != p[col].removeprefix("eq."):
                    ok = False
            if "email" in p:
                inner = p["email"].removeprefix("in.(").removesuffix(")")
                wanted = {v.strip('"') for v in inner.split(",")}
                ok = ok and r["email"] in wanted
            if ok:
                out.append(r)
        return out

    def log(self, user_id, email, brief_date="2026-11-17"):
        self.rows.append(
            {
                "user_id": user_id or None,
                "email": (email or "").strip().lower(),
                "kind": "daily_brief",
                "brief_date": brief_date,
                "status": "sent",
            }
        )


@pytest.fixture
def email_log(monkeypatch):
    fake = FakeEmailLog()
    monkeypatch.setattr(DB, "_supabase_get", fake.get)
    monkeypatch.setattr(
        DB,
        "_supabase_post",
        lambda path, body, prefer="": fake.log(body["user_id"], body["email"], body["brief_date"]),
    )
    return fake


def test_dedupe_list_send_then_account_retry(email_log):
    # morning run: the account lookup failed, so the person went out as an
    # email-list subscriber; the retry finds their account
    DB.log_email("", "Reader@Example.org", "2026-11-17", "sent", "s", {})
    assert DB.already_sent_today(UID, "2026-11-17", email="Reader@Example.org")
    assert not DB.already_sent_today(UID, "2026-11-18", email="Reader@Example.org")


def test_dedupe_account_send_then_list_retry(email_log):
    # sent as an account holder; by the retry they turned the account
    # brief off but are on the email list
    DB.log_email(UID, "reader@example.org", "2026-11-17", "sent", "s", {})
    assert DB.already_sent_today("", "2026-11-17", email="reader@example.org")
    # an older account row logged with its original capitals still counts
    email_log.rows = [dict(email_log.rows[0], email="Reader@Example.org")]
    assert DB.already_sent_today("", "2026-11-17", email="Reader@Example.org")
    assert not DB.already_sent_today("", "2026-11-17", email="someone@else.org")


# ── the worker, run in Node against a fake Supabase + Resend ───────────────
# The fake database below implements the four SQL functions in
# sql/2026-11-16_brief_signups.sql with the same rules (each call is
# atomic, like the row and advisory locks make it in Postgres). The SQL
# itself was checked against a real Postgres when it was written.
HARNESS = r"""
const mod = await import(process.argv[2]);
const worker = mod.default;
const KEY = process.argv[3];
const UID = process.argv[4], UID2 = process.argv[5];
const env = { SUPABASE_URL: "https://sb.test", SUPABASE_SERVICE_KEY: KEY,
              RESEND_API_KEY: "re_test", FROM_EMAIL: "TickerDesk <brief@tickerdesk.io>" };
const MIN = 60 * 1000, DAY = 24 * 60 * MIN;
let now = Date.parse("2026-11-16T15:00:00Z");
const db = { signups: new Map(), attempts: [], users: new Map(), profiles: new Map() };
let fail = {};
let calls = [];
async function sha256(s) {
  const b = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(b)].map(function (x) { return x.toString(16).padStart(2, "0"); }).join("");
}
const rpc = {
  brief_signup_claim(a) {
    const e = String(a.p_email || "").trim().toLowerCase();
    if (!/^[0-9a-f]{64}$/.test(a.p_token_hash || "")) return "invalid";
    const ip = a.p_ip_hash || "?";
    db.attempts = db.attempts.filter(function (x) { return x.at > now - DAY; });
    if (db.attempts.filter(function (x) { return x.ip === ip && x.at > now - 10 * MIN; }).length >= 5) return "rate_limited";
    if (db.attempts.filter(function (x) { return x.at > now - 60 * MIN; }).length >= 300) return "rate_limited";
    db.attempts.push({ ip: ip, at: now });
    if (!db.signups.has(e)) db.signups.set(e, { source: a.p_source });
    const r = db.signups.get(e);
    if (r.confirmed_at && !r.unsubscribed_at) return "already_subscribed";
    if (r.confirm_sent_at && r.confirm_sent_at > now - 15 * MIN) return "pending";
    Object.assign(r, { confirm_sent_at: now, token_hash: a.p_token_hash, expires: now + 7 * DAY });
    return "send";
  },
  brief_signup_release(a) {
    const r = db.signups.get(String(a.p_email).toLowerCase());
    if (r && r.token_hash === a.p_token_hash) {
      Object.assign(r, { confirm_sent_at: null, token_hash: null, expires: null });
    }
    return null;
  },
  brief_confirm(a) {
    for (const [e, r] of db.signups) {
      if (r.token_hash && r.token_hash === a.p_token_hash && r.expires > now) {
        Object.assign(r, { confirmed_at: now, unsubscribed_at: null, token_hash: null, expires: null });
        return e;
      }
    }
    return null;
  },
  brief_unsubscribe(a) {
    let e;
    if (a.p_user_id) {
      if (db.profiles.has(a.p_user_id)) db.profiles.set(a.p_user_id, false);
      e = (db.users.get(a.p_user_id) || "").toLowerCase();
    } else {
      e = String(a.p_email || "").trim().toLowerCase();
    }
    if (!e) return "ok";
    const r = db.signups.get(e);
    if (r) Object.assign(r, { unsubscribed_at: r.unsubscribed_at || now, token_hash: null, expires: null });
    for (const [id, em] of db.users) if (em.toLowerCase() === e) db.profiles.set(id, false);
    return "ok";
  },
};
globalThis.fetch = async function (url, init) {
  init = init || {};
  url = String(url);
  const c = { url: url, method: init.method || "GET", headers: init.headers || {},
              body: init.body || null };
  calls.push(c);
  // yield so simultaneous requests really interleave
  await new Promise(function (r) { setTimeout(r, 0); });
  const m = url.match(/\/rest\/v1\/rpc\/(\w+)$/);
  if (m) {
    if (fail[m[1]]) return new Response('{"message":"boom"}', { status: fail[m[1]] });
    const res = rpc[m[1]](JSON.parse(init.body));
    return new Response(JSON.stringify(res), { status: 200 });
  }
  if (url.startsWith("https://api.resend.com/")) {
    if (fail.resend) return new Response("{}", { status: fail.resend });
    return new Response('{"id":"x"}', { status: 200 });
  }
  return new Response('{"message":"unexpected call"}', { status: 599 });
};
const out = {};
async function call(req) {
  const before = calls.length;
  const r = await worker.fetch(req, env, { waitUntil() {} });
  const text = await r.text();
  return { status: r.status, type: r.headers.get("Content-Type") || "", text,
           calls: calls.slice(before) };
}
async function run(name, req, opts) {
  fail = (opts && opts.fail) || {};
  out[name] = await call(req);
  fail = {};
}
function post(body, ip, type) {
  return new Request("https://api.tickerdesk.io/brief/subscribe", {
    method: "POST",
    headers: { "Content-Type": type || "application/json",
               "Origin": "https://tickerdesk.io", "CF-Connecting-IP": ip || "1.1.1.1" },
    body: typeof body === "string" ? body : JSON.stringify(body) });
}
function sentLink(r) {
  const c = r.calls.find(function (x) { return x.url.startsWith("https://api.resend.com/"); });
  return c ? JSON.parse(c.body).text.match(/https:\S+/)[0] : "";
}
function row(e) { return Object.assign({}, db.signups.get(e) || null); }

await run("bad_email", post({ email: "nope", source: "footer" }));
await run("honeypot", post({ email: "a@b.co", website: "http://spam" }, "2.2.2.2"));
await run("new", post({ email: " New@Example.org ", source: "footer" }, "3.3.3.3"));
const link1 = sentLink(out["new"]);
out.link1 = link1;
out.row_after_new = row("new@example.org");
out.token_hash_matches = out.row_after_new.token_hash === await sha256(new URL(link1).searchParams.get("token"));
await run("again_soon", post({ email: "new@example.org" }, "3.3.3.4"));

// five simultaneous requests for one address from one network
// (their call logs interleave, so count over the whole burst)
const burstFrom = calls.length;
const burst = await Promise.all([0, 1, 2, 3, 4].map(function () {
  return call(post({ email: "victim@example.org" }, "8.8.8.8")); }));
const burstMail = calls.slice(burstFrom)
  .filter(function (c) { return c.url.startsWith("https://api.resend.com/"); });
out.burst_status = burst.map(function (r) { return r.status; });
out.burst_claims = calls.slice(burstFrom)
  .filter(function (c) { return c.url.endsWith("/rpc/brief_signup_claim"); }).length;
out.burst_sends = burstMail.length;
out.burst_keys = burstMail.map(function (c) { return c.headers["Idempotency-Key"] || ""; });

// per-network limit, shared through the database
for (let i = 0; i < 6; i++) {
  await run("rl" + i, post({ email: "rl" + i + "@example.org" }, "7.7.7.7"));
}
// failures
await run("claim_fails", post({ email: "x@example.org" }, "5.5.5.5"),
  { fail: { brief_signup_claim: 503 } });
await run("resend_fails", post({ email: "y@example.org" }, "5.5.5.6"), { fail: { resend: 422 } });
out.row_after_resend_fail = row("y@example.org");
await run("retry_after_fail", post({ email: "y@example.org" }, "5.5.5.6"));
await run("form_post", post("email=z%40example.org&source=lp_vcp", "6.6.6.6",
  "application/x-www-form-urlencoded"));

// confirm: once, unexpired, untampered
await run("confirm", new Request(link1));
await run("confirm_reuse", new Request(link1));
await run("confirm_tampered", new Request(link1.replace(/.$/, function (c) {
  return c === "0" ? "1" : "0"; })));
await run("confirm_malformed", new Request("https://api.tickerdesk.io/brief/confirm?token=abc"));
await run("dup_confirmed", post({ email: "new@example.org" }, "4.4.4.4"));
const zlink = sentLink(out["form_post"]);
now += 8 * DAY;
await run("confirm_expired", new Request(zlink));
out.z_confirmed = !!row("z@example.org").confirmed_at;

// unsubscribe, both directions
db.users.set(UID, "Reader@Example.org"); db.profiles.set(UID, true);
db.signups.set("reader@example.org", { confirmed_at: now, unsubscribed_at: null });
db.users.set(UID2, "both@example.org"); db.profiles.set(UID2, true);
db.signups.set("both@example.org", { confirmed_at: now, unsubscribed_at: null });
const unsubEmail = process.argv[6], unsubUser = process.argv[7];
// account link (with a forged ?e= bolted on, which must be ignored)
await run("unsub_user", new Request(unsubUser + "&e=victim%40example.org"));
out.after_user_unsub = { profile: db.profiles.get(UID), list: row("reader@example.org"),
                         victim: row("victim@example.org") };
await run("unsub_email_post", new Request(unsubEmail, { method: "POST" }));
out.after_email_unsub = { profile: db.profiles.get(UID2), list: row("both@example.org") };
await run("unsub_email_bad", new Request(unsubEmail.replace("t=", "t=0")));
await run("unsub_fails", new Request(unsubEmail), { fail: { brief_unsubscribe: 500 } });

// an unsubscribe kills a pending link; signing up again needs a fresh one
await run("pending_signup", post({ email: "late@example.org" }, "9.9.9.1"));
const lateLink = sentLink(out["pending_signup"]);
await rpc.brief_unsubscribe({ p_user_id: null, p_email: "late@example.org" });
await run("confirm_after_unsub", new Request(lateLink));
now += 20 * MIN;
await run("resubscribe", post({ email: "late@example.org" }, "9.9.9.2"));
const freshLink = sentLink(out["resubscribe"]);
await run("confirm_old_after_resub", new Request(lateLink));
await run("confirm_fresh", new Request(freshLink));
out.late_row = row("late@example.org");
console.log(JSON.stringify(out));
"""

UID = "11111111-1111-4111-8111-111111111111"
UID2 = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(scope="module")
def worker_run(tmp_path_factory):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    d = tmp_path_factory.mktemp("worker")
    # .mjs so Node loads it as an ES module whatever its version
    shutil.copy(os.path.join(_BASE, "cloudflare-worker", "worker.js"), d / "worker.mjs")
    (d / "harness.mjs").write_text(HARNESS, encoding="utf-8")
    em = "both@example.org"
    unsub = "https://api.tickerdesk.io/unsubscribe?" + urllib.parse.urlencode(
        {"e": em, "t": _sig("brief-unsub:" + em)}
    )
    unsub_user = f"https://api.tickerdesk.io/unsubscribe?u={UID}&t=" + _sig(UID)
    p = subprocess.run(
        [
            node,
            str(d / "harness.mjs"),
            (d / "worker.mjs").as_uri(),
            KEY,
            UID,
            UID2,
            unsub,
            unsub_user,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


def _calls(r, method=None, url_part=""):
    return [c for c in r["calls"] if (not method or c["method"] == method) and url_part in c["url"]]


def _rpc_args(r, fn):
    return [json.loads(c["body"]) for c in _calls(r, "POST", "/rest/v1/rpc/" + fn)]


def _sends(r):
    return _calls(r, "POST", "api.resend.com")


def test_subscribe_rejects_bad_email(worker_run):
    r = worker_run["bad_email"]
    assert r["status"] == 400 and "valid email" in json.loads(r["text"])["error"]
    assert r["calls"] == []


def test_subscribe_honeypot_saves_nothing(worker_run):
    r = worker_run["honeypot"]
    assert r["status"] == 200 and r["calls"] == []


def test_subscribe_claims_then_sends_one_expiring_link(worker_run):
    r = worker_run["new"]
    assert r["status"] == 200, r["text"]
    assert json.loads(r["text"])["message"] == "Check your inbox to confirm your subscription."
    (claim,) = _rpc_args(r, "brief_signup_claim")
    assert claim["p_email"] == "new@example.org" and claim["p_source"] == "footer"
    # the network is stored only as a keyed hash, never the raw IP
    assert claim["p_ip_hash"] == _sig("brief-ip:3.3.3.3")
    (mail,) = _sends(r)
    sent = json.loads(mail["body"])
    assert sent["to"] == ["new@example.org"] and "/brief/confirm?token=" in sent["html"]
    assert sent["from"] == "TickerDesk <brief@tickerdesk.io>"
    assert "expires in 7 days" in sent["text"]
    # the claim happens BEFORE the send, and the send is keyed to the claim
    assert r["calls"].index(mail) > r["calls"].index(_calls(r, url_part="brief_signup_claim")[0])
    assert mail["headers"]["Idempotency-Key"] == "brief-confirm/" + claim["p_token_hash"][:48]
    # the database keeps only the token's hash, not the token
    assert worker_run["token_hash_matches"] is True
    assert claim["p_token_hash"] not in worker_run["link1"]


def test_simultaneous_requests_send_one_email(worker_run):
    assert worker_run["burst_status"] == [200] * 5
    assert worker_run["burst_claims"] == 5  # every request asked the database
    assert worker_run["burst_sends"] == 1
    assert len(set(worker_run["burst_keys"])) == 1


def test_repeat_and_confirmed_signups_send_nothing(worker_run):
    r = worker_run["again_soon"]
    assert r["status"] == 200 and not _sends(r)
    r = worker_run["dup_confirmed"]
    assert r["status"] == 200 and json.loads(r["text"])["status"] == "already_subscribed"
    assert not _sends(r)


def test_subscribe_is_rate_limited_per_network_in_the_database(worker_run):
    assert [worker_run["rl%d" % i]["status"] for i in range(6)] == [200] * 5 + [429]
    assert not _sends(worker_run["rl5"])


def test_no_claim_means_no_send(worker_run):
    r = worker_run["claim_fails"]
    assert r["status"] == 502 and json.loads(r["text"])["ok"] is False
    assert not _sends(r)


def test_failed_send_releases_the_claim(worker_run):
    r = worker_run["resend_fails"]
    assert r["status"] == 502 and json.loads(r["text"])["ok"] is False
    (claim,) = _rpc_args(r, "brief_signup_claim")
    (rel,) = _rpc_args(r, "brief_signup_release")
    assert rel == {"p_email": "y@example.org", "p_token_hash": claim["p_token_hash"]}
    assert worker_run["row_after_resend_fail"]["confirm_sent_at"] is None
    # so an immediate retry can send
    assert len(_sends(worker_run["retry_after_fail"])) == 1


def test_plain_form_post_gets_an_html_page(worker_run):
    r = worker_run["form_post"]
    assert r["status"] == 200 and r["type"].startswith("text/html")
    assert "Check your inbox" in r["text"]
    (claim,) = _rpc_args(r, "brief_signup_claim")
    assert claim["p_email"] == "z@example.org" and claim["p_source"] == "lp_vcp"


def test_confirm_link_works_once_and_expires(worker_run):
    r = worker_run["confirm"]
    assert r["status"] == 200 and "subscribed" in r["text"]
    for name in ("confirm_reuse", "confirm_tampered", "confirm_expired"):
        bad = worker_run[name]
        assert bad["status"] == 410 and "expired" in bad["text"], name
    assert worker_run["z_confirmed"] is False
    bad = worker_run["confirm_malformed"]
    assert bad["status"] == 400 and bad["calls"] == []


def test_account_unsubscribe_also_stops_the_list_brief(worker_run):
    r = worker_run["unsub_user"]
    assert r["status"] == 200, r["text"]
    # the address is never taken from the link: only the signed user id
    assert _rpc_args(r, "brief_unsubscribe") == [{"p_user_id": UID, "p_email": None}]
    after = worker_run["after_user_unsub"]
    assert after["profile"] is False
    assert after["list"]["unsubscribed_at"]
    assert not after["victim"].get("unsubscribed_at")


def test_list_unsubscribe_also_stops_the_account_brief(worker_run):
    r = worker_run["unsub_email_post"]
    assert r["status"] == 200, r["text"]
    assert _rpc_args(r, "brief_unsubscribe") == [{"p_user_id": None, "p_email": "both@example.org"}]
    after = worker_run["after_email_unsub"]
    assert after["profile"] is False and after["list"]["unsubscribed_at"]


def test_unsubscribe_rejects_bad_signatures_and_reports_failures(worker_run):
    bad = worker_run["unsub_email_bad"]
    assert "invalid or has expired" in bad["text"] and bad["calls"] == []
    assert worker_run["unsub_fails"]["status"] == 500


def test_unsubscribe_kills_pending_links_and_resubscribe_needs_a_fresh_one(worker_run):
    assert worker_run["confirm_after_unsub"]["status"] == 410
    assert len(_sends(worker_run["resubscribe"])) == 1
    assert worker_run["confirm_old_after_resub"]["status"] == 410
    assert worker_run["confirm_fresh"]["status"] == 200
    late = worker_run["late_row"]
    assert late["confirmed_at"] and late["unsubscribed_at"] is None
