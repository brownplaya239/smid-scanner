"""Stripe webhook analytics (subscribed / cancelled) in cloudflare-worker/worker.js.

Imports the real worker in Node, sends it correctly signed Stripe webhook
events, and answers its outgoing fetch() calls with a fake Supabase and a
fake PostHog. Checks that the webhook's answer is identical whether PostHog
works, throws, or never answers; that nothing goes to PostHog without
POSTHOG_KEY; that the event is keyed by the user id with no email; and
that the send is handed to ctx.waitUntil instead of being awaited.
Skipped without Node."""

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

USER = "3f1c2b8e-1111-4222-8333-444455556666"
SECRET = "whsec_test_secret"

HARNESS = r"""
import worker from "./worker.mjs";
import crypto from "node:crypto";

const env = {
  STRIPE_SECRET_KEY: "sk_test",
  STRIPE_WEBHOOK_SECRET: SECRET,
  SUPABASE_URL: "https://db.example.supabase.co",
  SUPABASE_SERVICE_KEY: "service-key",
  STRIPE_PRO_PRICE_ID: "price_pro",
  STRIPE_PREMIUM_PRICE_ID: "price_premium",
  ...(OPT.env || {}),
};
const logs = [];
for (const k of ["log", "error", "warn"]) console[k] = (...a) => logs.push(a.map(String).join(" "));
const calls = [];
globalThis.fetch = (url, init = {}) => {
  url = String(url);
  calls.push({ url, method: init.method || "GET", body: init.body ? JSON.parse(init.body) : null,
               hasSignal: !!init.signal });
  if (url.includes("posthog")) {
    if (OPT.posthog === "throw") throw new Error("posthog exploded");
    if (OPT.posthog === "reject") return Promise.reject(new Error("network down"));
    if (OPT.posthog === "hang") return new Promise(() => {});
    return Promise.resolve(new Response("{}", { status: 200 }));
  }
  if (url.includes("/rest/v1/profiles")) return Promise.resolve(new Response(null, { status: 204 }));
  return Promise.resolve(new Response("unexpected " + url, { status: 599 }));
};

const body = JSON.stringify(OPT.event);
const t = Math.floor(Date.now() / 1000);
const v1 = crypto.createHmac("sha256", OPT.signWith).update(t + "." + body).digest("hex");
const req = new Request("https://api.tickerdesk.io/stripe/webhook", {
  method: "POST", headers: { "stripe-signature": "t=" + t + ",v1=" + v1 }, body,
});
const waited = [];
const ctx = { waitUntil(p) { waited.push(p); } };
const resp = await Promise.race([
  worker.fetch(req, env, ctx),
  new Promise((r) => setTimeout(() => r(null), 3000)),
]);
const out = resp
  ? { status: resp.status, body: await resp.text(), ctype: resp.headers.get("content-type") }
  : { timedOut: true };
// Let a non-hanging send settle, then report whether waitUntil's promise
// ever rejects (it must not: an unhandled rejection would be logged as an
// error in the worker).
let waitRejected = false;
if (OPT.posthog !== "hang") {
  for (const p of waited) { try { await p; } catch (_) { waitRejected = true; } }
}
process.stdout.write(JSON.stringify({ ...out, calls, logs, waitedCount: waited.length, waitRejected }));
process.exit(0);
"""

CHECKOUT = {
    "id": "evt_1",
    "created": 1763300000,
    "type": "checkout.session.completed",
    "data": {
        "object": {
            "id": "cs_1",
            "mode": "subscription",
            "client_reference_id": USER,
            "customer": "cus_1",
            "customer_email": "buyer@example.com",
            "metadata": {"user_id": USER, "plan": "pro"},
        }
    },
}
DELETED = {
    "id": "evt_2",
    "created": 1763300000,
    "type": "customer.subscription.deleted",
    "data": {
        "object": {
            "id": "sub_1",
            "customer": "cus_1",
            "metadata": {"user_id": USER},
            "items": {"data": [{"price": {"id": "price_premium"}}]},
        }
    },
}


def _run(event, posthog="ok", key="phc_test", sign_with=SECRET):
    opt = {
        "event": event,
        "posthog": posthog,
        "env": {"POSTHOG_KEY": key} if key else {},
        "signWith": sign_with,
    }
    with tempfile.TemporaryDirectory() as d:
        shutil.copy(WORKER, os.path.join(d, "worker.mjs"))
        for name in os.listdir(os.path.dirname(WORKER)):
            if name.endswith(".mjs"):
                shutil.copy(os.path.join(os.path.dirname(WORKER), name), d)
        p = os.path.join(d, "run.mjs")
        with open(p, "w", encoding="utf-8") as f:
            f.write(
                f"const SECRET = {json.dumps(SECRET)};\nconst OPT = {json.dumps(opt)};\n" + HARNESS
            )
        out = subprocess.run(
            [NODE, p], check=False, capture_output=True, encoding="utf-8", timeout=60, cwd=d
        )
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout)


def _posthog(res):
    return [c for c in res["calls"] if "posthog" in c["url"]]


def _answer(res):
    return (res.get("timedOut"), res["status"], res["body"], res["ctype"])


@pytest.mark.parametrize("event", [CHECKOUT, DELETED], ids=["checkout", "deleted"])
@pytest.mark.parametrize("mode", ["throw", "reject", "hang"])
def test_webhook_answer_is_unchanged_when_posthog_fails(event, mode):
    baseline = _run(event, key=None)
    res = _run(event, posthog=mode)
    assert baseline["status"] == 200 and json.loads(baseline["body"]) == {"received": True}
    assert _answer(res) == _answer(baseline)
    assert len(_posthog(res)) == 1
    # The plan was still applied to the profile.
    assert [c["method"] for c in res["calls"] if "/profiles" in c["url"]] == ["PATCH"]
    assert res["waitRejected"] is False


def test_no_key_sends_nothing():
    res = _run(CHECKOUT, key=None)
    assert _posthog(res) == []
    assert res["waitedCount"] == 0


def test_subscribed_is_keyed_by_user_id_without_email():
    res = _run(CHECKOUT)
    (c,) = _posthog(res)
    assert c["url"] == "https://us.i.posthog.com/i/v0/e/"
    assert c["method"] == "POST" and c["hasSignal"] is True  # gives up after 5 s
    assert c["body"]["api_key"] == "phc_test"
    assert c["body"]["event"] == "subscribed"
    assert c["body"]["distinct_id"] == USER
    assert c["body"]["properties"] == {
        "plan": "pro",
        "source": "stripe_webhook",
        "stripe_event": "checkout.session.completed",
    }
    assert "example.com" not in json.dumps(c["body"])
    assert res["waitedCount"] == 1  # handed to ctx.waitUntil, not awaited


def test_cancelled_reads_the_plan_from_the_price():
    res = _run(DELETED)
    (c,) = _posthog(res)
    assert c["body"]["event"] == "cancelled"
    assert c["body"]["distinct_id"] == USER
    assert c["body"]["properties"]["plan"] == "premium"


def test_other_events_send_nothing():
    upd = json.loads(json.dumps(DELETED))
    upd["type"] = "customer.subscription.updated"
    upd["data"]["object"]["status"] = "active"
    assert _posthog(_run(upd)) == []
    one_off = json.loads(json.dumps(CHECKOUT))
    one_off["data"]["object"]["mode"] = "payment"
    assert _posthog(_run(one_off)) == []


def test_bad_signature_never_reaches_posthog():
    res = _run(CHECKOUT, sign_with="whsec_wrong")
    assert res["status"] == 400
    assert _posthog(res) == []
