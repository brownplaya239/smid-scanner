"""Stripe webhook -> public.profiles writes (cloudflare-worker/worker.js), run in Node.

Imports the real worker, sends it correctly signed Stripe events, and
answers its outgoing fetch() calls with a fake Supabase + Slack. Checks
that every plan change is an UPSERT (so an account with no profile row
still gets its paid plan), and that a failed write alerts Slack and
answers non-2xx so Stripe retries. Skipped when Node is not installed."""

import json
import os
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(ROOT, "cloudflare-worker", "worker.js")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

# Shared harness. SCENARIO (set per test) decides how the fake Supabase
# answers; the script prints {status, body, calls} as JSON.
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
  ...(globalThis.ENV_EXTRA || {}),
};
const calls = [];
const logs = [];
for (const k of ["log", "error", "warn"]) {
  console[k] = (...a) => logs.push(a.map(String).join(" "));
}
globalThis.fetch = async (url, init = {}) => {
  url = String(url);
  const call = { url, method: init.method || "GET", headers: init.headers || {},
                 body: init.body ? JSON.parse(init.body) : null };
  calls.push(call);
  if (url.startsWith("https://slack.com/api/")) {
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  }
  if (url.includes("/rest/v1/profiles")) return SUPABASE(call);
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

const payload = JSON.stringify(EVENT);
const req = new Request("https://api.tickerdesk.io/stripe/webhook", {
  method: "POST", body: payload,
  headers: { "stripe-signature": await sign(payload), "CF-Connecting-IP": "203.0.113.9" },
});
const resp = await worker.fetch(req, env, { waitUntil() {} });
process.stdout.write(JSON.stringify({ status: resp.status, body: await resp.text(),
                                      calls, logs }));
"""

USER = "3f1c2b8e-1111-4222-8333-444455556666"

# Fake Supabase: a profiles table with no row for USER (the bug's
# precondition). An upsert creates it and returns it.
EMPTY_TABLE = r"""
const TABLE = {};
const SUPABASE = (call) => {
  if (call.method === "PATCH") {  // old behaviour: matches nothing, 204
    return new Response(null, { status: 204 });
  }
  if (call.method === "POST") {
    const row = { ...(TABLE[call.body.id] || {}), ...call.body };
    TABLE[row.id] = row;
    return new Response(JSON.stringify([{ id: row.id,
      subscription_tier: row.subscription_tier || "free" }]), { status: 201 });
  }
  return new Response("[]", { status: 200 });
};
"""


def _run(event, supabase_js, env_extra=None):
    with tempfile.TemporaryDirectory() as d:
        shutil.copy(WORKER, os.path.join(d, "worker.mjs"))
        script = (
            f"globalThis.ENV_EXTRA = {json.dumps(env_extra or {})};\n"
            + supabase_js
            + f"\nconst EVENT = {json.dumps(event)};\n"
            + HARNESS
        )
        path = os.path.join(d, "run.mjs")
        with open(path, "w", encoding="utf-8") as f:
            f.write(script)
        out = subprocess.run(
            [NODE, path], check=False, capture_output=True, encoding="utf-8", timeout=60, cwd=d
        )
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout)


def _checkout_event():
    return {
        "id": "evt_checkout_1",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_1",
                "client_reference_id": USER,
                "customer": "cus_123",
                "metadata": {"plan": "pro", "user_id": USER},
            }
        },
    }


def _subscription_event(kind, status="active", price="price_premium"):
    return {
        "id": "evt_sub_1",
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


def _profile_writes(res):
    return [c for c in res["calls"] if "/rest/v1/profiles" in c["url"]]


def _slack_posts(res):
    return [c for c in res["calls"] if c["url"].startswith("https://slack.com/api/")]


def test_checkout_creates_missing_profile_with_paid_plan():
    res = _run(_checkout_event(), EMPTY_TABLE)
    assert res["status"] == 200, res
    writes = _profile_writes(res)
    assert len(writes) == 1
    w = writes[0]
    assert w["method"] == "POST"  # an upsert, never a bare PATCH
    assert "on_conflict=id" in w["url"]
    assert "resolution=merge-duplicates" in w["headers"]["Prefer"]
    assert "return=representation" in w["headers"]["Prefer"]
    assert w["body"] == {
        "id": USER,
        "subscription_tier": "pro",
        "subscription_expires_at": None,
        "stripe_customer_id": "cus_123",
    }
    assert w["headers"]["Authorization"] == "Bearer service-key-secret"
    assert not _slack_posts(res)


@pytest.mark.parametrize(
    ("kind", "status", "tier"),
    [
        ("created", "active", "premium"),
        ("updated", "incomplete", "premium"),
        ("updated", "canceled", "free"),
        ("deleted", "canceled", "free"),
    ],
)
def test_subscription_events_upsert(kind, status, tier):
    res = _run(_subscription_event(kind, status), EMPTY_TABLE)
    assert res["status"] == 200, res
    (w,) = _profile_writes(res)
    assert w["method"] == "POST" and "on_conflict=id" in w["url"]
    assert w["body"]["id"] == USER
    assert w["body"]["subscription_tier"] == tier
    assert w["body"]["subscription_expires_at"] is None
    # deleted keeps the Stripe customer id (no field sent); others refresh it
    assert ("stripe_customer_id" in w["body"]) == (kind != "deleted")


ERROR_500 = r"""
const SUPABASE = () => new Response('{"message":"boom"}', { status: 500 });
"""
ZERO_ROWS = r"""
const SUPABASE = () => new Response("[]", { status: 201 });
"""
NETWORK_DOWN = r"""
const SUPABASE = () => { throw new Error("connect ECONNREFUSED"); };
"""


@pytest.mark.parametrize(
    ("supabase_js", "why"),
    [(ERROR_500, "HTTP 500"), (ZERO_ROWS, "no row"), (NETWORK_DOWN, "network")],
    ids=["http-500", "zero-rows", "network-down"],
)
def test_failed_write_alerts_and_asks_stripe_to_retry(supabase_js, why):
    res = _run(_checkout_event(), supabase_js)
    assert res["status"] == 500, res  # non-2xx -> Stripe retries
    assert json.loads(res["body"])["received"] is False
    (slack,) = _slack_posts(res)
    assert slack["url"].endswith("/chat.postMessage")
    assert slack["body"]["channel"] == "C_ALERTS"
    text = slack["body"]["text"]
    assert "evt_checkout_1" in text and USER in text and "pro" in text
    assert why in text
    # never leak the service key or the Slack token
    blob = json.dumps(res["logs"]) + text
    assert "service-key-secret" not in blob and "xoxb-test" not in blob
    assert any("[ops-alert]" in line for line in res["logs"])


def test_failure_without_alert_channel_still_returns_500():
    res = _run(_checkout_event(), ERROR_500, env_extra={"SLACK_ALERT_CHANNEL": ""})
    assert res["status"] == 500
    assert not _slack_posts(res)
    assert any("[ops-alert]" in line for line in res["logs"])


def test_event_without_user_does_not_write():
    ev = _checkout_event()
    obj = ev["data"]["object"]
    obj["client_reference_id"] = None
    obj["metadata"] = {"plan": "pro"}
    res = _run(ev, EMPTY_TABLE)
    assert res["status"] == 200
    assert not _profile_writes(res)


def test_bad_signature_is_rejected_before_any_write():
    # Re-sign with the wrong secret by changing the worker's secret.
    res = _run(_checkout_event(), EMPTY_TABLE, env_extra={"STRIPE_WEBHOOK_SECRET": "whsec_other"})
    assert res["status"] == 400
    assert not _profile_writes(res)
