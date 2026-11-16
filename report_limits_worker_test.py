"""AI report limits and promo codes in cloudflare-worker/worker.js, run in Node.

Imports the real worker, sends it requests, and answers its outgoing
fetch() calls with a fake Supabase (auth + the two database functions
td_reserve_report / td_redeem_promo), Yahoo and GitHub. Checks that a
report only starts after the sign-in, plan and usage reservation all
succeed (any failure refuses instead of letting it through), that a
report GitHub accepts is marked dispatched, that only a clear GitHub
refusal (4xx) un-charges it while an unclear outcome keeps it charged
and marks it dispatch_uncertain, and that promo codes go
through td_redeem_promo. The SQL side is in
sql/2026-11-10_lock_plans_and_report_usage.sql. Skipped without Node."""

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
RES_ID = "9a9a9a9a-0000-4000-8000-000000000001"

# FAKES (set per test) is a JS object of handlers: auth, rpc, yahoo, github,
# del, patch. Each gets the recorded call and returns a Response (or throws).
HARNESS = r"""
import worker from "./worker.mjs";

const env = {
  PAT: "ghp_test",
  SUPABASE_URL: "https://db.example.supabase.co",
  SUPABASE_ANON_KEY: "anon-key",
  SUPABASE_SERVICE_KEY: "service-key-secret",
  PROMO_CODES: JSON.stringify({ SPRING30: { tier: "premium", days: 30, label: "30-day Premium" } }),
  ...(globalThis.ENV_EXTRA || {}),
};
for (const k of Object.keys(env)) if (env[k] === null) delete env[k];
const calls = [];
const logs = [];
for (const k of ["log", "error", "warn"]) {
  console[k] = (...a) => logs.push(a.map(String).join(" "));
}
const json = (o, s = 200) => new Response(JSON.stringify(o), { status: s });
const DEFAULTS = {
  auth: () => json({ id: USER }),
  rpc: (c) => json({ ok: true, id: RES_ID, tier: "free", used: 1, cap: 3 }),
  yahoo: () => json({ chart: { result: [{ timestamp: [1, 2, 3] }] } }),
  github: () => new Response(null, { status: 204 }),
  del: () => new Response(null, { status: 204 }),
  patch: () => new Response(null, { status: 204 }),
};
const H = { ...DEFAULTS, ...FAKES };
globalThis.fetch = async (url, init = {}) => {
  url = String(url);
  let body = null;
  try { body = init.body ? JSON.parse(init.body) : null; } catch (_) { body = init.body; }
  const call = { url, method: init.method || "GET", headers: init.headers || {}, body,
                 hasSignal: !!init.signal };
  calls.push(call);
  if (url.endsWith("/auth/v1/user")) return H.auth(call);
  if (url.includes("/rest/v1/rpc/")) return H.rpc(call);
  if (url.includes("finance.yahoo.com")) return H.yahoo(call);
  if (url.startsWith("https://api.github.com/")) return H.github(call);
  if (url.includes("/rest/v1/report_generations") && call.method === "DELETE") return H.del(call);
  if (url.includes("/rest/v1/report_generations") && call.method === "PATCH") return H.patch(call);
  return new Response("unexpected " + url, { status: 599 });
};

const headers = { "Content-Type": "application/json", "CF-Connecting-IP": "203.0.113.9" };
if (REQ.auth !== false) headers["Authorization"] = "Bearer user-jwt";
const req = new Request("https://api.tickerdesk.io" + REQ.path, {
  method: "POST", headers, body: JSON.stringify(REQ.body),
});
const resp = await worker.fetch(req, env, { waitUntil() {} });
process.stdout.write(JSON.stringify({ status: resp.status, body: await resp.text(), calls, logs }));
"""


def _run(fakes_js="{}", path="/", body=None, auth=True, env_extra=None):
    req = {"path": path, "body": body or {"ticker": "NVDA", "report": "adhoc"}, "auth": auth}
    with tempfile.TemporaryDirectory() as d:
        shutil.copy(WORKER, os.path.join(d, "worker.mjs"))
        # worker.js imports its sibling modules (e.g. ./claude.mjs).
        for name in os.listdir(os.path.dirname(WORKER)):
            if name.endswith(".mjs"):
                shutil.copy(os.path.join(os.path.dirname(WORKER), name), d)
        script = (
            f"const USER = {json.dumps(USER)}; const RES_ID = {json.dumps(RES_ID)};\n"
            f"globalThis.ENV_EXTRA = {json.dumps(env_extra or {})};\n"
            f"const REQ = {json.dumps(req)};\n"
            f"const FAKES = {fakes_js};\n" + HARNESS
        )
        p = os.path.join(d, "run.mjs")
        with open(p, "w", encoding="utf-8") as f:
            f.write(script)
        out = subprocess.run(
            [NODE, p], check=False, capture_output=True, encoding="utf-8", timeout=60, cwd=d
        )
    assert out.returncode == 0, out.stderr[-2000:]
    res = json.loads(out.stdout)
    res["json"] = json.loads(res["body"]) if res["body"] else None
    return res


def _kinds(res):
    """The outgoing calls, in order, as short labels."""
    out = []
    for c in res["calls"]:
        u = c["url"]
        if u.endswith("/auth/v1/user"):
            out.append("auth")
        elif "/rest/v1/rpc/" in u:
            out.append("rpc:" + u.rsplit("/", 1)[1])
        elif "finance.yahoo.com" in u:
            out.append("yahoo")
        elif u.startswith("https://api.github.com/"):
            out.append("github")
        elif "/rest/v1/report_generations" in u:
            out.append(c["method"] + ":report_generations")
        else:
            out.append(c["method"] + ":" + u)
    return out


# ── Reports: the happy path ───────────────────────────────────────────


DISPATCHED = ["auth", "yahoo", "rpc:td_reserve_report", "github", "PATCH:report_generations"]


def _marked(res, state):
    """The reservation was PATCHed to dispatch_state=state, scoped to the user."""
    c = res["calls"][-1]
    assert c["method"] == "PATCH", c
    assert "id=eq." + RES_ID in c["url"] and "user_id=eq." + USER in c["url"]
    assert c["body"] == {"dispatch_state": state}
    assert c["headers"]["Authorization"] == "Bearer service-key-secret"


def test_report_reserves_then_dispatches():
    res = _run()
    assert res["status"] == 200, res
    assert res["json"] == {"ok": True, "ticker": "NVDA"}
    assert _kinds(res) == DISPATCHED
    _marked(res, "dispatched")
    rpc = res["calls"][2]
    assert rpc["method"] == "POST"
    assert rpc["body"] == {"p_user": USER, "p_ticker": "NVDA", "p_report_type": "adhoc"}
    assert rpc["headers"]["Authorization"] == "Bearer service-key-secret"
    gh = res["calls"][3]
    assert gh["body"]["inputs"] == {"ticker": "NVDA", "user_id": USER}


def test_altdata_report_type_is_passed_through():
    res = _run(body={"ticker": "AMD", "report": "altdata"})
    assert res["status"] == 200, res
    assert res["calls"][2]["body"]["p_report_type"] == "altdata"


# ── Reports: at the cap ───────────────────────────────────────────────


@pytest.mark.parametrize("code", ["monthly_cap", "lifetime_cap"])
def test_over_cap_is_429_and_nothing_starts(code):
    fakes = (
        "{ rpc: () => new Response(JSON.stringify({ ok: false, code: '"
        + code
        + "', tier: 'free', used: 3, cap: 3 }), { status: 200 }) }"
    )
    res = _run(fakes)
    assert res["status"] == 429, res
    assert res["json"]["code"] == code
    assert res["json"]["used"] == 3 and res["json"]["cap"] == 3
    assert "github" not in _kinds(res)


# ── Reports: fail closed ──────────────────────────────────────────────

RPC_FAILURES = {
    "http-500": '{ rpc: () => new Response(\'{"message":"boom"}\', { status: 500 }) }',
    "network-down": "{ rpc: () => { throw new Error('connect ECONNREFUSED'); } }",
    "no-id": "{ rpc: () => new Response(JSON.stringify({ ok: true, tier: 'pro' }), { status: 200 }) }",
    "empty-id": "{ rpc: () => new Response(JSON.stringify({ ok: true, id: '' }), { status: 200 }) }",
    "null-body": "{ rpc: () => new Response('null', { status: 200 }) }",
    "array-body": "{ rpc: () => new Response('[]', { status: 200 }) }",
    "not-json": "{ rpc: () => new Response('<html>', { status: 200 }) }",
    "unknown-code": "{ rpc: () => new Response(JSON.stringify({ ok: false, code: 'weird' }), { status: 200 }) }",
    "function-missing": '{ rpc: () => new Response(\'{"code":"PGRST202"}\', { status: 404 }) }',
}


@pytest.mark.parametrize("fakes", list(RPC_FAILURES.values()), ids=list(RPC_FAILURES))
def test_reservation_failure_is_503_and_nothing_starts(fakes):
    res = _run(fakes)
    assert res["status"] == 503, res
    assert res["json"]["code"] == "quota_unavailable"
    assert "github" not in _kinds(res)
    assert "service-key-secret" not in json.dumps(res["logs"])


AUTH_FAILURES = {
    "supabase-500": ("{ auth: () => new Response('oops', { status: 500 }) }", 503),
    "supabase-429": ("{ auth: () => new Response('slow down', { status: 429 }) }", 503),
    "network-down": ("{ auth: () => { throw new Error('ECONNRESET'); } }", 503),
    "not-json": ("{ auth: () => new Response('<html>', { status: 200 }) }", 503),
    "no-id": ("{ auth: () => new Response('{}', { status: 200 }) }", 401),
    "bad-id": ("{ auth: () => new Response(JSON.stringify({ id: 'x' }), { status: 200 }) }", 401),
    "expired-token": ("{ auth: () => new Response('{}', { status: 401 }) }", 401),
    "bad-jwt-403": ("{ auth: () => new Response('{}', { status: 403 }) }", 401),
}


@pytest.mark.parametrize(("fakes", "status"), list(AUTH_FAILURES.values()), ids=list(AUTH_FAILURES))
def test_auth_failure_refuses_before_reserving(fakes, status):
    res = _run(fakes)
    assert res["status"] == status, res
    assert _kinds(res) == ["auth"]


def test_no_token_is_401_with_no_calls():
    res = _run(auth=False)
    assert res["status"] == 401
    assert res["json"]["code"] == "auth_required"
    assert res["calls"] == []


def test_missing_service_key_is_503_with_no_calls():
    res = _run(env_extra={"SUPABASE_SERVICE_KEY": None})
    assert res["status"] == 503
    assert res["calls"] == []


def test_invalid_ticker_from_yahoo_is_not_reserved():
    res = _run(
        "{ yahoo: () => new Response(JSON.stringify({ chart: { result: [] } }), { status: 200 }) }"
    )
    assert res["status"] == 400
    assert _kinds(res) == ["auth", "yahoo"]


def test_yahoo_down_still_reserves_then_dispatches():
    res = _run("{ yahoo: () => { throw new Error('blocked'); } }")
    assert res["status"] == 200
    assert _kinds(res) == DISPATCHED


# ── Reports: only a clear GitHub refusal is un-charged ────────────────


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_github_refusal_releases_the_reservation(status):
    res = _run("{ github: () => new Response('nope', { status: %d }) }" % status)
    assert res["status"] == 502, res
    assert _kinds(res) == [
        "auth",
        "yahoo",
        "rpc:td_reserve_report",
        "github",
        "DELETE:report_generations",
    ]
    url = res["calls"][-1]["url"]
    assert "id=eq." + RES_ID in url and "user_id=eq." + USER in url


@pytest.mark.parametrize(
    "github",
    [
        "() => new Response('bad', { status: 500 })",
        "() => new Response('bad gateway', { status: 502 })",
        "() => new Response('unavailable', { status: 503 })",
        "() => { throw new Error('ETIMEDOUT'); }",
        "() => { throw new DOMException('The operation timed out.', 'TimeoutError'); }",
        "() => new Response(null, { status: 304 })",
    ],
    ids=[
        "github-500",
        "github-502",
        "github-503",
        "github-network",
        "github-timeout",
        "github-3xx",
    ],
)
def test_unclear_dispatch_keeps_the_reservation_charged(github):
    res = _run("{ github: " + github + " }")
    assert res["status"] == 202, res
    assert res["json"]["ok"] is False
    assert res["json"]["code"] == "dispatch_uncertain"
    assert "My Reports" in res["json"]["error"]
    assert _kinds(res) == [
        "auth",
        "yahoo",
        "rpc:td_reserve_report",
        "github",
        "PATCH:report_generations",
    ]
    _marked(res, "dispatch_uncertain")


def test_github_dispatch_has_a_timeout():
    res = _run()
    gh = [c for c in res["calls"] if c["url"].startswith("https://api.github.com/")]
    assert gh and gh[0]["hasSignal"] is True


@pytest.mark.parametrize(
    "patch",
    ["() => new Response('x', { status: 500 })", "() => { throw new Error('down'); }"],
    ids=["http-500", "network-down"],
)
def test_marking_failure_does_not_change_the_answer(patch):
    ok = _run("{ patch: " + patch + " }")
    assert ok["status"] == 200, ok
    assert ok["json"] == {"ok": True, "ticker": "NVDA"}
    unclear = _run("{ patch: " + patch + ", github: () => { throw new Error('reset'); } }")
    assert unclear["status"] == 202, unclear
    assert unclear["json"]["code"] == "dispatch_uncertain"
    assert "DELETE:report_generations" not in _kinds(unclear)


def test_reused_reservation_is_dispatched_normally():
    fakes = (
        "{ rpc: () => new Response(JSON.stringify({ ok: true, id: RES_ID, tier: 'free',"
        " used: 3, cap: 3, reused: true }), { status: 200 }) }"
    )
    res = _run(fakes)
    assert res["status"] == 200, res
    assert _kinds(res) == DISPATCHED
    _marked(res, "dispatched")


# ── Promo codes ────────────────────────────────────────────────────────


def _promo(fakes="{}", code="spring30", env_extra=None):
    return _run(fakes, path="/promo/redeem", body={"code": code}, env_extra=env_extra)


def test_promo_goes_through_td_redeem_promo():
    fakes = (
        "{ rpc: () => new Response(JSON.stringify({ ok: true, tier: 'premium', days: 30,"
        " expires: '2026-12-16T12:00:00.000+00:00' }), { status: 200 }) }"
    )
    res = _promo(fakes)
    assert res["status"] == 200, res
    assert res["json"]["ok"] is True
    assert res["json"]["tier"] == "premium" and res["json"]["days"] == 30
    assert res["json"]["expires"] == "2026-12-16T12:00:00.000Z"
    assert _kinds(res) == ["auth", "rpc:td_redeem_promo"]
    assert res["calls"][1]["body"] == {
        "p_user": USER,
        "p_code": "SPRING30",
        "p_tier": "premium",
        "p_days": 30,
    }
    # never a direct profile write from the worker
    assert not [c for c in res["calls"] if "/rest/v1/profiles" in c["url"]]


@pytest.mark.parametrize("code", ["already_redeemed", "plan_active", "higher_tier"])
def test_promo_refusals_are_409(code):
    fakes = (
        "{ rpc: () => new Response(JSON.stringify({ ok: false, code: '"
        + code
        + "', tier: 'pro' }), { status: 200 }) }"
    )
    res = _promo(fakes)
    assert res["status"] == 409, res
    assert res["json"]["code"] == code


@pytest.mark.parametrize(
    "fakes",
    [
        "{ rpc: () => new Response('boom', { status: 500 }) }",
        "{ rpc: () => { throw new Error('down'); } }",
        "{ rpc: () => new Response(JSON.stringify({ ok: true }), { status: 200 }) }",
        "{ rpc: () => new Response('[]', { status: 200 }) }",
    ],
    ids=["http-500", "network-down", "no-expires", "array-body"],
)
def test_promo_failures_are_503(fakes):
    res = _promo(fakes)
    assert res["status"] == 503, res
    assert res["json"]["ok"] is False


def test_unknown_promo_code_never_reaches_the_database():
    res = _promo(code="TEST69")
    assert res["status"] == 400
    assert _kinds(res) == ["auth"]


def test_no_promo_secret_rejects_every_code():
    res = _promo(env_extra={"PROMO_CODES": None})
    assert res["status"] == 400
    assert "rpc:td_redeem_promo" not in _kinds(res)
