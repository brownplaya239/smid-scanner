"""Stripe webhook -> public.profiles (cloudflare-worker/worker.js), run in Node.

Imports the real worker, sends it correctly signed Stripe events (one, a
sequence, or several at once per test, in one Node process), and answers its
outgoing fetch() calls with a fake Stripe API, Supabase and Slack.

The plan must come from Stripe's CURRENT subscriptions (read from the API,
across every Stripe customer the account has had), never from the event
payload. Each sync holds the account's lease (td_acquire_reconcile_lease)
from before it reads Stripe until it has saved (td_apply_stripe_state), so
two syncs for one account never interleave. Any error means "retry"
(non-2xx), never "no subscription".

Each sequence test runs twice:
  * "fake":   Supabase is a small JS stand-in (always runs, as in CI);
  * "pglite": Supabase calls run the real SQL from
              sql/2026-11-16_profiles_for_every_account.sql on a throwaway
              in-memory Postgres. Skipped unless PGlite is installed
              (`bun install --cwd sql/tests`).
Skipped entirely when Node is not installed."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
WORKER = ROOT / "cloudflare-worker" / "worker.js"
SQL_TESTS = ROOT / "sql" / "tests"
PGLITE = SQL_TESTS / "node_modules" / "@electric-sql" / "pglite" / "dist" / "index.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

MODES = [
    "fake",
    pytest.param(
        "pglite",
        marks=pytest.mark.skipif(
            not PGLITE.exists(), reason="PGlite not installed (bun install --cwd sql/tests)"
        ),
    ),
]

USER = "3f1c2b8e-1111-4222-8333-444455556666"
OTHER_USER = "9a9a9a9a-1111-4222-8333-444455556666"

# Node harness. Injected above it: MODE, INITIAL, STEPS, ENV_EXTRA, and (pglite
# mode) PGLITE_URL, SCHEMA_URL, PR17_SQL.
#
# A step is {event, stripe?, fail?, stripeDelayMs?, stripePageSize?,
# sigOffset?, sigExtra?, expireLeases?} or {parallel: [step, ...], gapMs}:
# the parallel steps start gapMs apart and run at the same time. A step's
# `stripe` is what Stripe answers to THAT request (so two requests running at
# once can see different Stripe states, as when the state changes between
# their reads); a sequential step's `stripe` also becomes the new default.
HARNESS = r"""
import worker from "./worker.mjs";
import { AsyncLocalStorage } from "node:async_hooks";

const SECRET = "whsec_test";
const env = {
  STRIPE_SECRET_KEY: "sk_test_x",
  STRIPE_WEBHOOK_SECRET: SECRET,
  STRIPE_PRO_PRICE_ID: "price_pro",
  STRIPE_PREMIUM_PRICE_ID: "price_premium",
  SUPABASE_URL: "https://db.example.supabase.co/",
  SUPABASE_SERVICE_KEY: "service-key-secret",
  SLACK_BOT_TOKEN: "xoxb-test",
  SLACK_ALERT_CHANNEL: "C_ALERTS",
  ...ENV_EXTRA,
};
const logs = [];
for (const k of ["log", "error", "warn"]) {
  console[k] = (...a) => logs.push(a.map(String).join(" "));
}
const ctx = new AsyncLocalStorage();

// ── Fake Stripe: current subscriptions per customer ──────────────────
let STRIPE = INITIAL.stripe || {};
// ── Supabase: fake (JS) or real SQL on PGlite ────────────────────────
let db = null;
const fakeProfiles = JSON.parse(JSON.stringify(INITIAL.profiles || {}));
const fakeEvents = new Set(INITIAL.events || []);
const fakeCustomers = {};  // user -> [customer ids]
for (const [u, ids] of Object.entries(INITIAL.customers || {})) fakeCustomers[u] = ids.slice();
const fakeLease = {};      // user -> {token, until (ms)}
for (const [u, l] of Object.entries(INITIAL.leases || {})) {
  fakeLease[u] = { token: l.token, until: Date.now() + l.ttlMs };
}
if (MODE === "pglite") {
  const { PGlite } = await import(PGLITE_URL);
  const { BASE_SCHEMA } = await import(SCHEMA_URL);
  db = new PGlite();
  await db.exec(BASE_SCHEMA);
  for (const id of [USER, OTHER_USER]) {
    await db.query("insert into auth.users (id) values ($1)", [id]);
  }
  await db.exec(PR17_SQL);   // its trigger did not exist when they signed up
  await db.query("delete from public.profiles");  // ...so they have no profile
  for (const [id, row] of Object.entries(INITIAL.profiles || {})) {
    const cols = Object.keys(row);
    await db.query(
      "insert into public.profiles (id, " + cols.join(", ") + ") values ($1, " +
        cols.map((_, i) => "$" + (i + 2)).join(", ") + ")",
      [id, ...cols.map((c) => row[c])]);
  }
  for (const e of INITIAL.events || []) {
    await db.query("insert into public.stripe_webhook_events (event_id) values ($1)", [e]);
  }
  for (const [u, ids] of Object.entries(INITIAL.customers || {})) {
    for (const c of ids) {
      await db.query("insert into public.stripe_customers (user_id, customer_id) values ($1, $2)", [u, c]);
    }
  }
  for (const [u, l] of Object.entries(INITIAL.leases || {})) {
    await db.query("insert into public.stripe_reconcile_lease (user_id, token, expires_at) " +
      "values ($1, $2, clock_timestamp() + make_interval(secs => $3::double precision / 1000))",
      [u, l.token, l.ttlMs]);
  }
}
// One PostgREST request = one transaction as service_role (requests running
// at the same time take turns, as PGlite has one connection).
async function asService(sql, params) {
  return db.transaction(async (tx) => {
    await tx.exec("set local role service_role");
    return (await tx.query(sql, params)).rows;
  });
}
// Mirror of the SQL functions for the fake mode (the real ones run in
// pglite mode and in sql/tests/profiles_sql_check.mjs).
function fakeAcquire(a) {
  const l = fakeLease[a.p_user];
  if (l && l.until > Date.now()) return false;
  fakeLease[a.p_user] = { token: a.p_token, until: Date.now() + a.p_ttl_seconds * 1000 };
  return true;
}
function fakeRelease(a) {
  const l = fakeLease[a.p_user];
  if (!l || l.token !== a.p_token) return false;
  delete fakeLease[a.p_user];
  return true;
}
function fakeApply(a) {
  const row = fakeProfiles[a.p_user] || (fakeProfiles[a.p_user] = { subscription_tier: "free" });
  const l = fakeLease[a.p_user];
  if (!l || l.token !== a.p_lease_token || l.until <= Date.now()) {
    return { applied: false, tier: row.subscription_tier, reason: "lease_lost" };
  }
  let reason = null;
  if (a.p_tier !== "free") {
    row.subscription_tier = a.p_tier; row.subscription_expires_at = null;
  } else if (["pro", "premium"].includes(row.subscription_tier) && !row.subscription_expires_at) {
    row.subscription_tier = "free";
  } else { reason = "kept_non_stripe_plan"; }
  if (a.p_customer) {
    row.stripe_customer_id = a.p_customer;
    const list = fakeCustomers[a.p_user] || (fakeCustomers[a.p_user] = []);
    if (!list.includes(a.p_customer)) list.push(a.p_customer);
  }
  if (a.p_event_id) fakeEvents.add(a.p_event_id);
  return { applied: true, tier: row.subscription_tier, reason };
}

const json = (o, status = 200) => new Response(JSON.stringify(o), { status });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function fakeFetch(url, init = {}) {
  url = String(url);
  const c = ctx.getStore();
  const call = { url, method: init.method || "GET", headers: init.headers || {},
                 body: init.body ? JSON.parse(init.body) : null, at: Date.now() };
  c.calls.push(call);
  const FAIL = c.fail;
  await sleep(1);
  if (url.startsWith("https://slack.com/api/")) return json({ ok: true });
  if (url.startsWith("https://api.stripe.com/v1/subscriptions")) {
    if (c.stripeDelayMs) await sleep(c.stripeDelayMs);
    if (FAIL === "stripe500") return json({ error: { message: "boom" } }, 500);
    if (FAIL === "stripeNetwork") throw new Error("stripe ECONNRESET");
    const q = new URL(url).searchParams;
    const cus = q.get("customer");
    // Stripe's default list leaves out canceled subscriptions.
    const all = ((c.stripe || STRIPE)[cus] || []).filter((s) => s.status !== "canceled");
    const size = c.stripePageSize || 100;
    const after = q.get("starting_after");
    const start = after ? all.findIndex((s) => s.id === after) + 1 : 0;
    const data = all.slice(start, start + size);
    return json({ object: "list", has_more: start + size < all.length, data });
  }
  const u = new URL(url);
  const p = u.pathname;
  const eq = (k) => (u.searchParams.get(k) || "").replace(/^eq\./, "");
  if (p === "/rest/v1/stripe_webhook_events") {
    if (FAIL === "eventsLookup500") return json({ message: "boom" }, 500);
    const id = eq("event_id");
    if (MODE === "pglite") {
      return json(await asService("select event_id from public.stripe_webhook_events where event_id = $1", [id]));
    }
    return json(fakeEvents.has(id) ? [{ event_id: id }] : []);
  }
  if (p === "/rest/v1/stripe_customers" && call.method === "POST") {
    if (FAIL === "recordCustomer500") return json({ message: "boom" }, 500);
    const b = call.body;
    if (MODE === "pglite") {
      await asService("insert into public.stripe_customers (user_id, customer_id) values ($1, $2) on conflict do nothing",
        [b.user_id, b.customer_id]);
    } else {
      const list = fakeCustomers[b.user_id] || (fakeCustomers[b.user_id] = []);
      if (!list.includes(b.customer_id)) list.push(b.customer_id);
    }
    return new Response("", { status: 201 });
  }
  if (p === "/rest/v1/stripe_customers") {
    if (FAIL === "customersLookup500") return json({ message: "boom" }, 500);
    if (FAIL === "customersLookupNetwork") throw new Error("connect ECONNREFUSED");
    const uid = eq("user_id");
    if (MODE === "pglite") {
      return json(await asService("select customer_id from public.stripe_customers where user_id = $1", [uid]));
    }
    return json((fakeCustomers[uid] || []).map((customer_id) => ({ customer_id })));
  }
  if (p === "/rest/v1/profiles" && call.method === "GET") {
    if (FAIL === "profileLookup500") return json({ message: "boom" }, 500);
    if (FAIL === "profileLookupNetwork") throw new Error("connect ECONNREFUSED");
    const id = eq("id");
    if (MODE === "pglite") {
      return json(await asService("select stripe_customer_id from public.profiles where id = $1", [id]));
    }
    const row = fakeProfiles[id];
    return json(row ? [{ stripe_customer_id: row.stripe_customer_id || null }] : []);
  }
  if (p === "/rest/v1/rpc/td_acquire_reconcile_lease") {
    if (FAIL === "acquire500") return json({ message: "boom" }, 500);
    const a = call.body;
    if (MODE === "pglite") {
      const rows = await asService("select public.td_acquire_reconcile_lease($1, $2, $3) r",
        [a.p_user, a.p_token, a.p_ttl_seconds]);
      return json(rows[0].r);
    }
    return json(fakeAcquire(a));
  }
  if (p === "/rest/v1/rpc/td_release_reconcile_lease") {
    if (FAIL === "releaseNetwork") throw new Error("connect ECONNRESET");
    const a = call.body;
    if (MODE === "pglite") {
      const rows = await asService("select public.td_release_reconcile_lease($1, $2) r", [a.p_user, a.p_token]);
      return json(rows[0].r);
    }
    return json(fakeRelease(a));
  }
  if (p === "/rest/v1/rpc/td_apply_stripe_state") {
    if (FAIL === "rpc500") return new Response('{"message":"boom"}', { status: 500 });
    if (FAIL === "rpcEmpty") return new Response("null", { status: 200 });
    if (FAIL === "rpcNetwork") throw new Error("connect ECONNREFUSED");
    const a = call.body;
    if (MODE === "pglite") {
      const rows = await asService(
        "select public.td_apply_stripe_state($1, $2, $3, $4, $5, $6) r",
        [a.p_user, a.p_tier, a.p_customer, a.p_lease_token, a.p_event_id, a.p_event_type]);
      return json(rows[0].r);
    }
    return json(fakeApply(a));
  }
  return new Response("unexpected " + url, { status: 599 });
}
globalThis.fetch = (url, init) => fakeFetch(url, init);

async function hmac(t, payload, secret) {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(t + "." + payload));
  return Array.from(new Uint8Array(mac)).map(b => b.toString(16).padStart(2, "0")).join("");
}

async function runStep(step) {
  if (step.expireLeases) {
    if (MODE === "pglite") await db.exec("update public.stripe_reconcile_lease set expires_at = now() - interval '1 second'");
    else for (const l of Object.values(fakeLease)) l.until = 0;
  }
  const c = { calls: [], fail: step.fail || null, stripe: step.stripe || null,
              stripeDelayMs: step.stripeDelayMs || 0, stripePageSize: step.stripePageSize || 0 };
  return ctx.run(c, async () => {
    const payload = JSON.stringify(step.event);
    const t = Math.floor(Date.now() / 1000) + (step.sigOffset || 0);
    const sig = "t=" + t + (step.sigExtra || "") + ",v1=" + (await hmac(t, payload, SECRET));
    const req = new Request("https://api.tickerdesk.io/stripe/webhook", {
      method: "POST", body: payload,
      headers: { "stripe-signature": sig, "CF-Connecting-IP": "203.0.113.9" },
    });
    const started = Date.now();
    const resp = await worker.fetch(req, env, { waitUntil() {} });
    const text = await resp.text();
    let body = text;
    try { body = JSON.parse(text); } catch (_) { /* plain-text reply */ }
    return { status: resp.status, body, calls: c.calls, started, ended: Date.now() };
  });
}

const results = [];
for (const step of STEPS) {
  if (step.parallel) {
    const runs = [];
    for (const [i, s] of step.parallel.entries()) {
      if (i) await sleep(step.gapMs || 0);
      runs.push(runStep(s));
    }
    results.push(await Promise.all(runs));
  } else {
    if (step.stripe) STRIPE = step.stripe;
    results.push(await runStep(step));
  }
}
let profile = null;
let customers = [];
let lease = null;
if (MODE === "pglite") {
  profile = (await db.query(
    "select subscription_tier, subscription_expires_at, stripe_customer_id, promo_code_redeemed " +
    "from public.profiles where id = $1", [USER])).rows[0] || null;
  if (profile && profile.subscription_expires_at) {
    profile.subscription_expires_at = new Date(profile.subscription_expires_at).toISOString();
  }
  customers = (await db.query("select customer_id from public.stripe_customers where user_id = $1 order by 1",
    [USER])).rows.map((r) => r.customer_id);
  lease = (await db.query("select token from public.stripe_reconcile_lease where user_id = $1", [USER])).rows[0] || null;
} else {
  profile = fakeProfiles[USER] || null;
  customers = (fakeCustomers[USER] || []).slice().sort();
  lease = fakeLease[USER] ? { token: fakeLease[USER].token } : null;
}
process.stdout.write(JSON.stringify({ results, profile, customers, lease, logs }));
"""


def _run(steps, *, mode="fake", initial=None, env_extra=None):
    with tempfile.TemporaryDirectory() as d:
        shutil.copy(WORKER, os.path.join(d, "worker.mjs"))
        for mod in WORKER.parent.glob("*.mjs"):  # modules worker.js imports
            shutil.copy(mod, os.path.join(d, mod.name))
        head = [
            f"const MODE = {json.dumps(mode)};",
            f"const USER = {json.dumps(USER)};",
            f"const OTHER_USER = {json.dumps(OTHER_USER)};",
            f"const INITIAL = {json.dumps(initial or {})};",
            f"const STEPS = {json.dumps(steps)};",
            f"const ENV_EXTRA = {json.dumps(env_extra or {})};",
        ]
        if mode == "pglite":
            sql = (ROOT / "sql" / "2026-11-16_profiles_for_every_account.sql").read_text("utf-8")
            head += [
                f"const PGLITE_URL = {json.dumps(PGLITE.as_uri())};",
                f"const SCHEMA_URL = {json.dumps((SQL_TESTS / 'supabase_schema.mjs').as_uri())};",
                f"const PR17_SQL = {json.dumps(sql)};",
            ]
        # imports must come first in an ES module: put the harness's import
        # lines on top, then the injected constants, then the rest.
        lines = HARNESS.lstrip().split("\n")
        n_imp = next(i for i, ln in enumerate(lines) if not ln.startswith("import "))
        path = os.path.join(d, "run.mjs")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines[:n_imp] + head + lines[n_imp:]))
        out = subprocess.run(
            [NODE, path], check=False, capture_output=True, encoding="utf-8", timeout=120, cwd=d
        )
    assert out.returncode == 0, out.stderr[-3000:]
    return json.loads(out.stdout)


def _sub(status="active", price="price_premium", owner=USER, sid="sub_1"):
    return {
        "id": sid,
        "status": status,
        "metadata": {"user_id": owner} if owner else {},
        "items": {"data": [{"price": {"id": price}}]},
    }


def _checkout(evt_id="evt_checkout_1", plan="pro", customer="cus_123"):
    return {
        "id": evt_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_1",
                "client_reference_id": USER,
                "customer": customer,
                "metadata": {"plan": plan, "user_id": USER},
            }
        },
    }


def _sub_event(
    kind, evt_id="evt_sub_1", status="active", price="price_premium", customer="cus_123"
):
    return {
        "id": evt_id,
        "type": "customer.subscription." + kind,
        "data": {
            "object": {
                "id": "sub_1",
                "customer": customer,
                "status": status,
                "metadata": {"user_id": USER},
                "items": {"data": [{"price": {"id": price}}]},
            }
        },
    }


def _calls(step, suffix):
    return [c for c in step["calls"] if c["url"].split("?")[0].endswith(suffix)]


def _rpc_calls(step):
    return _calls(step, "/rest/v1/rpc/td_apply_stripe_state")


def _stripe_calls(step):
    return [c for c in step["calls"] if c["url"].startswith("https://api.stripe.com/")]


def _slack_posts(step):
    return [c for c in step["calls"] if c["url"].startswith("https://slack.com/api/")]


# ── The plan comes from Stripe's current state ─────────────────────────


@pytest.mark.parametrize("mode", MODES)
def test_checkout_creates_missing_profile_with_paid_plan(mode):
    res = _run(
        [{"event": _checkout(plan="pro")}],
        mode=mode,
        initial={"stripe": {"cus_123": [_sub(price="price_pro")]}},
    )
    (step,) = res["results"]
    assert step["status"] == 200, res
    (rpc,) = _rpc_calls(step)
    a = rpc["body"]
    assert a["p_user"] == USER and a["p_tier"] == "pro" and a["p_customer"] == "cus_123"
    assert a["p_event_id"] == "evt_checkout_1"
    assert "p_state_at" not in a  # no ordering by the worker's own clock
    assert rpc["headers"]["Authorization"] == "Bearer service-key-secret"
    (stripe,) = _stripe_calls(step)
    assert "customer=cus_123" in stripe["url"]
    assert stripe["headers"]["Authorization"] == "Bearer sk_test_x"
    assert res["profile"]["subscription_tier"] == "pro"
    assert res["profile"]["stripe_customer_id"] == "cus_123"
    assert res["customers"] == ["cus_123"]  # remembered for later syncs
    assert not _slack_posts(step)


@pytest.mark.parametrize("mode", MODES)
def test_lease_wraps_the_stripe_read_and_the_save(mode):
    res = _run(
        [{"event": _checkout(plan="pro")}],
        mode=mode,
        initial={"stripe": {"cus_123": [_sub(price="price_pro")]}},
    )
    step = res["results"][0]
    order = [c["url"].split("?")[0].rsplit("/", 1)[-1] for c in step["calls"]]
    acq = order.index("td_acquire_reconcile_lease")
    assert order.index("stripe_customers") < acq  # the event's customer is saved first
    assert acq < order.index("subscriptions") < order.index("td_apply_stripe_state")
    assert order[-1] == "td_release_reconcile_lease"
    (acquire,) = _calls(step, "td_acquire_reconcile_lease")
    (apply_,) = _rpc_calls(step)
    (release,) = _calls(step, "td_release_reconcile_lease")
    token = acquire["body"]["p_token"]
    assert acquire["body"]["p_ttl_seconds"] == 60
    assert apply_["body"]["p_lease_token"] == token == release["body"]["p_token"]
    assert res["lease"] is None  # given back


@pytest.mark.parametrize("mode", MODES)
def test_event_payload_plan_is_ignored(mode):
    # The event says Premium; Stripe says the customer pays for Pro.
    res = _run(
        [{"event": _sub_event("updated", price="price_premium")}],
        mode=mode,
        initial={"stripe": {"cus_123": [_sub(price="price_pro")]}},
    )
    assert res["results"][0]["status"] == 200
    assert _rpc_calls(res["results"][0])[0]["body"]["p_tier"] == "pro"
    assert res["profile"]["subscription_tier"] == "pro"


@pytest.mark.parametrize(
    ("status", "tier"),
    [
        ("active", "premium"),
        ("trialing", "premium"),
        ("past_due", "premium"),  # Stripe is still retrying the card
        ("incomplete", "free"),  # checkout not paid yet
        ("unpaid", "free"),
        ("canceled", "free"),
    ],
)
def test_subscription_status_decides_paid(status, tier):
    res = _run(
        [{"event": _sub_event("updated", status="active")}],
        initial={"stripe": {"cus_123": [_sub(status=status)]}},
    )
    assert res["results"][0]["status"] == 200
    assert _rpc_calls(res["results"][0])[0]["body"]["p_tier"] == tier


# ── Ordering: one sync per account at a time ─────────────────────────────


@pytest.mark.parametrize("mode", MODES)
def test_concurrent_syncs_cannot_end_on_the_older_state(mode):
    # Round-2 reproduction: A starts and its Stripe read is slow; B (whose
    # read would see the older Pro state) arrives meanwhile. Before, B saved
    # after A and the account ended on Pro. Now B can't take the lease, so
    # it saves nothing and answers 409; A saves Premium. Stripe's later
    # retry of B reads the current state (Premium).
    premium = {"cus_123": [_sub(price="price_premium")]}
    pro = {"cus_123": [_sub(price="price_pro")]}
    a = {"event": _sub_event("updated", evt_id="evt_A"), "stripe": premium, "stripeDelayMs": 2500}
    b = {"event": _sub_event("updated", evt_id="evt_B", price="price_pro"), "stripe": pro}
    res = _run(
        [
            {"parallel": [a, b], "gapMs": 100},
            {"event": b["event"], "stripe": premium},  # Stripe's retry of B
        ],
        mode=mode,
    )
    (ra, rb), retry = res["results"]
    assert ra["status"] == 200, res
    assert rb["status"] == 409 and rb["body"]["received"] is False
    assert not _stripe_calls(rb) and not _rpc_calls(rb)  # B never read nor saved
    assert len(_calls(rb, "td_acquire_reconcile_lease")) == 4  # waited, then gave up
    assert not _slack_posts(rb)  # busy is normal: no alert
    assert retry["status"] == 200
    assert _rpc_calls(retry)[0]["body"]["p_tier"] == "premium"
    assert res["profile"]["subscription_tier"] == "premium"
    assert res["lease"] is None


@pytest.mark.parametrize("mode", MODES)
def test_second_sync_waits_briefly_for_the_lease(mode):
    # Checkout and "subscription created" usually arrive together: the
    # second one waits for the first to finish, then reads Stripe itself.
    premium = {"cus_123": [_sub(price="price_premium")]}
    a = {"event": _checkout(), "stripe": premium, "stripeDelayMs": 150}
    b = {"event": _sub_event("created", evt_id="evt_created"), "stripe": premium}
    res = _run([{"parallel": [a, b], "gapMs": 20}], mode=mode)
    ra, rb = res["results"][0]
    assert ra["status"] == 200 and rb["status"] == 200, res
    a_save = _rpc_calls(ra)[0]["at"]
    assert all(c["at"] > a_save for c in _stripe_calls(rb))  # B read after A saved
    assert res["profile"]["subscription_tier"] == "premium"


@pytest.mark.parametrize("mode", MODES)
def test_expired_lease_is_taken_over(mode):
    # A sync that crashed left its lease behind; once expired, the next
    # event takes it over.
    res = _run(
        [{"event": _checkout()}],
        mode=mode,
        initial={
            "stripe": {"cus_123": [_sub(price="price_pro")]},
            "leases": {USER: {"token": "11111111-1111-4111-8111-111111111111", "ttlMs": -1000}},
        },
    )
    assert res["results"][0]["status"] == 200, res
    assert res["profile"]["subscription_tier"] == "pro"
    assert res["lease"] is None


@pytest.mark.parametrize("mode", MODES)
def test_lost_release_expires(mode):
    stripe = {"cus_123": [_sub(price="price_pro")]}
    res = _run(
        [
            {"event": _checkout(evt_id="evt_1"), "stripe": stripe, "fail": "releaseNetwork"},
            {"event": _checkout(evt_id="evt_2")},  # lease still held: 409
            {"event": _checkout(evt_id="evt_2"), "expireLeases": True},  # ...until it expires
        ],
        mode=mode,
    )
    s1, s2, s3 = res["results"]
    assert s1["status"] == 200, res  # the save itself worked
    assert any("lease release failed" in ln for ln in res["logs"])
    assert s2["status"] == 409 and not _stripe_calls(s2)
    assert s3["status"] == 200
    assert res["lease"] is None


@pytest.mark.parametrize("mode", MODES)
def test_retried_old_event_cannot_bring_back_old_plan(mode):
    # Round-1 reproduction: Pro update fails (500) -> Premium update succeeds
    # -> Stripe retries the old Pro event. The account must stay Premium.
    pro_evt = _sub_event("updated", evt_id="evt_pro", price="price_pro")
    premium_evt = _sub_event("updated", evt_id="evt_premium", price="price_premium")
    res = _run(
        [
            {"event": pro_evt, "stripe": {"cus_123": [_sub(price="price_pro")]}, "fail": "rpc500"},
            {"event": premium_evt, "stripe": {"cus_123": [_sub(price="price_premium")]}},
            {"event": pro_evt},  # Stripe's retry of the first event
        ],
        mode=mode,
    )
    s1, s2, s3 = res["results"]
    assert [s1["status"], s2["status"], s3["status"]] == [500, 200, 200], res
    assert _slack_posts(s1) and not _slack_posts(s2) and not _slack_posts(s3)
    assert _rpc_calls(s3)[0]["body"]["p_tier"] == "premium"
    assert res["profile"]["subscription_tier"] == "premium"
    assert res["lease"] is None  # released even after the failed save


@pytest.mark.parametrize("mode", MODES)
def test_duplicate_event_is_skipped(mode):
    ev = _checkout()
    res = _run(
        [{"event": ev}, {"event": ev}],
        mode=mode,
        initial={"stripe": {"cus_123": [_sub(price="price_pro")]}},
    )
    s1, s2 = res["results"]
    assert s1["status"] == 200 and s2["status"] == 200
    assert s2["body"].get("duplicate") is True
    assert not _stripe_calls(s2) and not _rpc_calls(s2)


# ── Every Stripe customer, every page ──────────────────────────────────


@pytest.mark.parametrize("mode", MODES)
def test_existing_paid_row_not_clobbered_by_customer_id_only_event(mode):
    # An event that carries only a customer id (no plan in its metadata) for
    # an account already paying for Premium: the plan stays Premium.
    ev = _checkout(plan=None, customer="cus_new")
    ev["data"]["object"]["metadata"] = {"user_id": USER}
    res = _run(
        [{"event": ev}],
        mode=mode,
        initial={
            "profiles": {USER: {"subscription_tier": "premium", "stripe_customer_id": "cus_old"}},
            "stripe": {"cus_new": [], "cus_old": [_sub(price="price_premium")]},
        },
    )
    step = res["results"][0]
    assert step["status"] == 200
    # both the event's customer and the cached one were checked
    urls = " ".join(c["url"] for c in _stripe_calls(step))
    assert "customer=cus_new" in urls and "customer=cus_old" in urls
    assert res["profile"]["subscription_tier"] == "premium"
    assert res["profile"]["stripe_customer_id"] == "cus_old"
    assert res["customers"] == ["cus_new", "cus_old"]


@pytest.mark.parametrize("mode", MODES)
def test_every_customer_the_account_ever_had_is_checked(mode):
    # Premium lives on the oldest of three customers; the event is about a
    # newer one with nothing active, and the profile caches a third.
    ev = _sub_event("deleted", status="canceled", customer="cus_b")
    res = _run(
        [{"event": ev}],
        mode=mode,
        initial={
            "profiles": {USER: {"subscription_tier": "premium", "stripe_customer_id": "cus_c"}},
            "customers": {USER: ["cus_a", "cus_b"]},
            "stripe": {"cus_a": [_sub(price="price_premium")], "cus_b": [], "cus_c": []},
        },
    )
    step = res["results"][0]
    assert step["status"] == 200, res
    urls = " ".join(c["url"] for c in _stripe_calls(step))
    for cus in ("cus_a", "cus_b", "cus_c"):
        assert "customer=" + cus in urls
    assert _rpc_calls(step)[0]["body"]["p_tier"] == "premium"
    assert res["profile"]["subscription_tier"] == "premium"


@pytest.mark.parametrize("mode", MODES)
def test_subscription_list_is_paginated(mode):
    # The paying subscription is on page 3 (page size 1 here).
    subs = [
        _sub(status="incomplete", sid="sub_a"),
        _sub(status="unpaid", sid="sub_b"),
        _sub(price="price_premium", sid="sub_c"),
    ]
    res = _run(
        [{"event": _checkout(), "stripePageSize": 1}],
        mode=mode,
        initial={"stripe": {"cus_123": subs}},
    )
    step = res["results"][0]
    assert step["status"] == 200, res
    urls = [c["url"] for c in _stripe_calls(step)]
    assert len(urls) == 3
    assert "starting_after" not in urls[0]
    assert "starting_after=sub_a" in urls[1] and "starting_after=sub_b" in urls[2]
    assert res["profile"]["subscription_tier"] == "premium"


# ── Existing rows ──────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", MODES)
def test_live_promo_kept_when_stripe_says_not_paying(mode):
    # Late "deleted" event for an old subscription while a promo is running.
    res = _run(
        [{"event": _sub_event("deleted", status="canceled")}],
        mode=mode,
        initial={
            "profiles": {
                USER: {
                    "subscription_tier": "premium",
                    "subscription_expires_at": "2099-01-01T00:00:00.000Z",
                    "promo_code_redeemed": "SPRING",
                }
            },
            "stripe": {"cus_123": [_sub(status="canceled")]},
        },
    )
    assert res["results"][0]["status"] == 200
    p = res["profile"]
    assert p["subscription_tier"] == "premium"
    assert p["subscription_expires_at"].startswith("2099-01-01")


@pytest.mark.parametrize("mode", MODES)
def test_promo_user_who_pays_gets_stripe_plan_and_no_end_date(mode):
    res = _run(
        [{"event": _checkout(plan="pro")}],
        mode=mode,
        initial={
            "profiles": {
                USER: {
                    "subscription_tier": "premium",
                    "subscription_expires_at": "2099-01-01T00:00:00.000Z",
                    "promo_code_redeemed": "SPRING",
                }
            },
            "stripe": {"cus_123": [_sub(price="price_pro")]},
        },
    )
    p = res["profile"]
    assert p["subscription_tier"] == "pro" and p["subscription_expires_at"] is None
    assert p["promo_code_redeemed"] == "SPRING"  # history kept


@pytest.mark.parametrize("mode", MODES)
def test_cancelled_paid_plan_goes_free(mode):
    res = _run(
        [{"event": _sub_event("deleted", status="canceled")}],
        mode=mode,
        initial={
            "profiles": {USER: {"subscription_tier": "premium", "stripe_customer_id": "cus_123"}},
            "stripe": {"cus_123": [_sub(status="canceled")]},
        },
    )
    assert res["results"][0]["status"] == 200
    assert res["profile"]["subscription_tier"] == "free"


def test_subscription_for_another_account_is_ignored():
    res = _run(
        [{"event": _checkout()}],
        initial={"stripe": {"cus_123": [_sub(owner=OTHER_USER)]}},
    )
    assert _rpc_calls(res["results"][0])[0]["body"]["p_tier"] == "free"


# ── Errors are never "no subscription" ───────────────────────────────────


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "fail",
    ["customersLookup500", "customersLookupNetwork", "profileLookup500", "profileLookupNetwork"],
)
def test_failed_customer_lookup_never_downgrades(mode, fail):
    # Round-2 reproduction: paid through a customer the database remembers,
    # the event is about an old inactive customer, and reading the
    # remembered customers fails. Before: saved Free, 200. Now: nothing
    # saved, alert, 500 so Stripe retries.
    ev = _sub_event("deleted", status="canceled", customer="cus_old")
    res = _run(
        [{"event": ev, "fail": fail}],
        mode=mode,
        initial={
            "profiles": {USER: {"subscription_tier": "premium", "stripe_customer_id": "cus_paid"}},
            "customers": {USER: ["cus_paid"]},
            "stripe": {"cus_old": [], "cus_paid": [_sub(price="price_premium")]},
        },
    )
    step = res["results"][0]
    assert step["status"] == 500, res
    assert not _rpc_calls(step) and not _stripe_calls(step)
    assert _slack_posts(step)
    assert res["profile"]["subscription_tier"] == "premium"
    assert res["lease"] is None  # released on the error path too


@pytest.mark.parametrize(
    ("fail", "why"),
    [
        ("rpc500", "HTTP 500"),
        ("rpcEmpty", "no result"),
        ("rpcNetwork", "network"),
        ("stripe500", "Stripe HTTP 500"),
        ("stripeNetwork", "Stripe network"),
        ("eventsLookup500", "duplicate"),
        ("recordCustomer500", "Stripe customer"),
        ("acquire500", "lease"),
    ],
)
def test_failed_sync_alerts_and_asks_stripe_to_retry(fail, why):
    res = _run(
        [{"event": _checkout(), "fail": fail}],
        initial={"stripe": {"cus_123": [_sub(price="price_pro")]}},
    )
    step = res["results"][0]
    assert step["status"] == 500, res  # non-2xx -> Stripe retries
    assert step["body"]["received"] is False
    assert not _rpc_calls(step) or fail.startswith("rpc")
    (slack,) = _slack_posts(step)
    assert slack["url"].endswith("/chat.postMessage")
    assert slack["body"]["channel"] == "C_ALERTS"
    text = slack["body"]["text"]
    assert "evt_checkout_1" in text and USER in text and why in text
    blob = json.dumps(res["logs"]) + text
    assert "service-key-secret" not in blob and "xoxb-test" not in blob
    assert "sk_test_x" not in blob
    assert any("[ops-alert]" in line for line in res["logs"])


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("alongside", [None, "price_pro", "price_premium"])
def test_unknown_price_aborts_even_alongside_known_plans(mode, alongside):
    # Round-2 reproduction: an unknown-price Premium plus a recognised Pro
    # used to save Pro. Any paying subscription with a price the worker
    # doesn't know now stops the sync: nothing saved, alert, 500.
    subs = [_sub(price="price_from_elsewhere", sid="sub_unknown")]
    if alongside:
        subs.append(_sub(price=alongside, sid="sub_known"))
    res = _run(
        [{"event": _checkout()}],
        mode=mode,
        initial={
            "profiles": {USER: {"subscription_tier": "premium", "stripe_customer_id": "cus_123"}},
            "stripe": {"cus_123": subs},
        },
    )
    step = res["results"][0]
    assert step["status"] == 500, res
    assert not _rpc_calls(step)
    text = _slack_posts(step)[0]["body"]["text"]
    assert "unknown price" in text and "sub_unknown" in text
    assert res["profile"]["subscription_tier"] == "premium"


def test_unknown_extra_item_on_a_known_subscription_aborts():
    sub = _sub(price="price_pro")
    sub["items"]["data"].append({"price": {"id": "price_addon"}})
    res = _run([{"event": _checkout()}], initial={"stripe": {"cus_123": [sub]}})
    assert res["results"][0]["status"] == 500
    assert not _rpc_calls(res["results"][0])


def test_failure_without_alert_channel_still_returns_500():
    res = _run(
        [{"event": _checkout(), "fail": "rpc500"}],
        initial={"stripe": {"cus_123": [_sub(price="price_pro")]}},
        env_extra={"SLACK_ALERT_CHANNEL": ""},
    )
    step = res["results"][0]
    assert step["status"] == 500
    assert not _slack_posts(step)
    assert any("[ops-alert]" in line for line in res["logs"])


def test_event_without_user_does_not_write():
    ev = _checkout()
    obj = ev["data"]["object"]
    obj["client_reference_id"] = None
    obj["metadata"] = {"plan": "pro"}
    res = _run([{"event": ev}])
    assert res["results"][0]["status"] == 200
    assert res["results"][0]["calls"] == []


def test_ignored_event_type_does_nothing():
    res = _run([{"event": {"id": "evt_x", "type": "invoice.paid", "data": {"object": {}}}}])
    assert res["results"][0]["status"] == 200
    assert res["results"][0]["calls"] == []


# ── Signature ──────────────────────────────────────────────────────────


def test_bad_signature_is_rejected_before_any_call():
    res = _run([{"event": _checkout()}], env_extra={"STRIPE_WEBHOOK_SECRET": "whsec_other"})
    assert res["results"][0]["status"] == 400
    assert res["results"][0]["calls"] == []


@pytest.mark.parametrize(
    ("offset", "status"),
    [(-301, 400), (301, 400), (-3600, 400), (-290, 200), (290, 200)],
)
def test_signature_timestamp_must_be_within_five_minutes(offset, status):
    # Correctly signed, but signed `offset` seconds from now: a replayed old
    # request is refused (Stripe's standard 300 s tolerance).
    res = _run(
        [{"event": _checkout(), "sigOffset": offset}],
        initial={"stripe": {"cus_123": [_sub(price="price_pro")]}},
    )
    step = res["results"][0]
    assert step["status"] == status, res
    if status == 400:
        assert step["calls"] == []


def test_any_matching_v1_signature_is_accepted():
    # While a signing secret is being rolled, Stripe sends two v1 values.
    res = _run(
        [{"event": _checkout(), "sigExtra": ",v1=" + "0" * 64}],
        initial={"stripe": {"cus_123": [_sub(price="price_pro")]}},
    )
    assert res["results"][0]["status"] == 200
