"""Stripe webhook -> public.profiles (cloudflare-worker/worker.js), run in Node.

Imports the real worker, sends it correctly signed Stripe events (one or a
sequence per test, in one Node process), and answers its outgoing fetch()
calls with a fake Stripe API, Supabase and Slack.

The plan must come from Stripe's CURRENT subscriptions (read from the API),
never from the event payload, and be saved with td_apply_stripe_state(), so
a late or retried event can never bring back an old plan.

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
HARNESS = r"""
import worker from "./worker.mjs";

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

// ── Fake Stripe: current subscriptions per customer ──────────────────
let STRIPE = INITIAL.stripe || {};
// ── Supabase: fake (JS) or real SQL on PGlite ────────────────────────
let db = null;
const fakeProfiles = JSON.parse(JSON.stringify(INITIAL.profiles || {}));
const fakeEvents = new Set(INITIAL.events || []);
const fakeState = {};  // user -> state_at
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
}
async function asService(sql, params) {
  await db.exec("set role service_role");
  try { return (await db.query(sql, params)).rows; }
  finally { await db.exec("reset role"); }
}
// Mirror of td_apply_stripe_state for the fake mode (the real one runs in
// pglite mode and in sql/tests/profiles_sql_check.mjs).
function fakeApply(a) {
  const row = fakeProfiles[a.p_user] || (fakeProfiles[a.p_user] = { subscription_tier: "free" });
  const prev = fakeState[a.p_user];
  if (prev && prev >= a.p_state_at) {
    fakeEvents.add(a.p_event_id);
    return { applied: false, tier: row.subscription_tier, reason: "stale" };
  }
  let reason = null;
  if (a.p_tier !== "free") {
    row.subscription_tier = a.p_tier; row.subscription_expires_at = null;
  } else if (["pro", "premium"].includes(row.subscription_tier) && !row.subscription_expires_at) {
    row.subscription_tier = "free";
  } else { reason = "kept_non_stripe_plan"; }
  if (a.p_customer) row.stripe_customer_id = a.p_customer;
  fakeState[a.p_user] = a.p_state_at;
  if (a.p_event_id) fakeEvents.add(a.p_event_id);
  return { applied: true, tier: row.subscription_tier, reason };
}

let FAIL = null;
let calls = [];
const json = (o, status = 200) => new Response(JSON.stringify(o), { status });
globalThis.fetch = async (url, init = {}) => {
  url = String(url);
  const call = { url, method: init.method || "GET", headers: init.headers || {},
                 body: init.body ? JSON.parse(init.body) : null };
  calls.push(call);
  await new Promise((r) => setTimeout(r, 2));  // let the clock move
  if (url.startsWith("https://slack.com/api/")) return json({ ok: true });
  if (url.startsWith("https://api.stripe.com/v1/subscriptions")) {
    if (FAIL === "stripe500") return json({ error: { message: "boom" } }, 500);
    if (FAIL === "stripeNetwork") throw new Error("stripe ECONNRESET");
    const cus = new URL(url).searchParams.get("customer");
    // Stripe's default list leaves out canceled subscriptions.
    return json({ object: "list", has_more: false,
      data: (STRIPE[cus] || []).filter((s) => s.status !== "canceled") });
  }
  const u = new URL(url);
  if (u.pathname === "/rest/v1/stripe_webhook_events") {
    const id = u.searchParams.get("event_id").replace(/^eq\./, "");
    if (MODE === "pglite") {
      return json(await asService("select event_id from public.stripe_webhook_events where event_id = $1", [id]));
    }
    return json(fakeEvents.has(id) ? [{ event_id: id }] : []);
  }
  if (u.pathname === "/rest/v1/profiles" && call.method === "GET") {
    const id = u.searchParams.get("id").replace(/^eq\./, "");
    if (MODE === "pglite") {
      return json(await asService("select stripe_customer_id from public.profiles where id = $1", [id]));
    }
    const row = fakeProfiles[id];
    return json(row ? [{ stripe_customer_id: row.stripe_customer_id || null }] : []);
  }
  if (u.pathname === "/rest/v1/rpc/td_apply_stripe_state") {
    if (FAIL === "rpc500") return new Response('{"message":"boom"}', { status: 500 });
    if (FAIL === "rpcEmpty") return new Response("null", { status: 200 });
    if (FAIL === "rpcNetwork") throw new Error("connect ECONNREFUSED");
    const a = call.body;
    if (MODE === "pglite") {
      const rows = await asService(
        "select public.td_apply_stripe_state($1, $2, $3, $4::timestamptz, $5, $6) r",
        [a.p_user, a.p_tier, a.p_customer, a.p_state_at, a.p_event_id, a.p_event_type]);
      return json(rows[0].r);
    }
    return json(fakeApply(a));
  }
  return new Response("unexpected " + url, { status: 599 });
};

async function sign(payload) {
  const t = Math.floor(Date.now() / 1000);
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(SECRET),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(t + "." + payload));
  const hex = Array.from(new Uint8Array(mac)).map(b => b.toString(16).padStart(2, "0")).join("");
  return "t=" + t + ",v1=" + hex;
}

const results = [];
for (const step of STEPS) {
  if (step.stripe) STRIPE = step.stripe;
  FAIL = step.fail || null;
  calls = [];
  const payload = JSON.stringify(step.event);
  const req = new Request("https://api.tickerdesk.io/stripe/webhook", {
    method: "POST", body: payload,
    headers: { "stripe-signature": await sign(payload), "CF-Connecting-IP": "203.0.113.9" },
  });
  const resp = await worker.fetch(req, env, { waitUntil() {} });
  const text = await resp.text();
  let body = text;
  try { body = JSON.parse(text); } catch (_) { /* plain-text reply */ }
  results.push({ status: resp.status, body, calls });
}
let profile = null;
if (MODE === "pglite") {
  profile = (await db.query(
    "select subscription_tier, subscription_expires_at, stripe_customer_id, promo_code_redeemed " +
    "from public.profiles where id = $1", [USER])).rows[0] || null;
  if (profile && profile.subscription_expires_at) {
    profile.subscription_expires_at = new Date(profile.subscription_expires_at).toISOString();
  }
} else {
  profile = fakeProfiles[USER] || null;
}
process.stdout.write(JSON.stringify({ results, profile, logs }));
"""


def _run(steps, *, mode="fake", initial=None, env_extra=None):
    with tempfile.TemporaryDirectory() as d:
        shutil.copy(WORKER, os.path.join(d, "worker.mjs"))
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
        # line on top, then the injected constants, then the rest.
        imp, rest = HARNESS.lstrip().split("\n", 1)
        path = os.path.join(d, "run.mjs")
        with open(path, "w", encoding="utf-8") as f:
            f.write(imp + "\n" + "\n".join(head) + "\n" + rest)
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


def _sub_event(kind, evt_id="evt_sub_1", status="active", price="price_premium"):
    return {
        "id": evt_id,
        "type": "customer.subscription." + kind,
        "data": {
            "object": {
                "id": "sub_1",
                "customer": "cus_123",
                "status": status,
                "metadata": {"user_id": USER},
                "items": {"data": [{"price": {"id": price}}]},
            }
        },
    }


def _rpc_calls(step):
    return [c for c in step["calls"] if c["url"].endswith("/rest/v1/rpc/td_apply_stripe_state")]


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
    assert a["p_state_at"].endswith("Z")
    assert rpc["headers"]["Authorization"] == "Bearer service-key-secret"
    (stripe,) = _stripe_calls(step)
    assert "customer=cus_123" in stripe["url"]
    assert stripe["headers"]["Authorization"] == "Bearer sk_test_x"
    assert res["profile"]["subscription_tier"] == "pro"
    assert res["profile"]["stripe_customer_id"] == "cus_123"
    assert not _slack_posts(step)


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


# ── Finding 1: a retried old event cannot overwrite a newer plan ─────────


@pytest.mark.parametrize("mode", MODES)
def test_retried_old_event_cannot_bring_back_old_plan(mode):
    # Reproduces the review: Pro update fails (500) -> Premium update succeeds
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
    # stamps only move forward
    t2 = _rpc_calls(s2)[0]["body"]["p_state_at"]
    t3 = _rpc_calls(s3)[0]["body"]["p_state_at"]
    assert t3 > t2


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


# ── Existing rows ──────────────────────────────────────────────────────


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


# ── Failures: alert + 500 so Stripe retries ──────────────────────────────


@pytest.mark.parametrize(
    ("fail", "why"),
    [
        ("rpc500", "HTTP 500"),
        ("rpcEmpty", "no result"),
        ("rpcNetwork", "network"),
        ("stripe500", "Stripe HTTP 500"),
        ("stripeNetwork", "Stripe network"),
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
    (slack,) = _slack_posts(step)
    assert slack["url"].endswith("/chat.postMessage")
    assert slack["body"]["channel"] == "C_ALERTS"
    text = slack["body"]["text"]
    assert "evt_checkout_1" in text and USER in text and why in text
    blob = json.dumps(res["logs"]) + text
    assert "service-key-secret" not in blob and "xoxb-test" not in blob
    assert "sk_test_x" not in blob
    assert any("[ops-alert]" in line for line in res["logs"])


def test_unknown_price_on_active_subscription_never_downgrades():
    res = _run(
        [{"event": _checkout()}],
        initial={"stripe": {"cus_123": [_sub(price="price_from_elsewhere")]}},
    )
    step = res["results"][0]
    assert step["status"] == 500
    assert not _rpc_calls(step)
    assert "unknown price" in _slack_posts(step)[0]["body"]["text"]


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
    assert not _rpc_calls(res["results"][0]) and not _stripe_calls(res["results"][0])


def test_ignored_event_type_does_nothing():
    res = _run([{"event": {"id": "evt_x", "type": "invoice.paid", "data": {"object": {}}}}])
    assert res["results"][0]["status"] == 200
    assert res["results"][0]["calls"] == []


def test_bad_signature_is_rejected_before_any_call():
    res = _run([{"event": _checkout()}], env_extra={"STRIPE_WEBHOOK_SECRET": "whsec_other"})
    assert res["results"][0]["status"] == 400
    assert res["results"][0]["calls"] == []
