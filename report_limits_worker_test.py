"""AI report limits and promo codes in cloudflare-worker/worker.js, run in Node.

Imports the real worker, sends it requests, and answers its outgoing
fetch() calls with a fake Supabase (auth + the database functions
td_reserve_report / td_claim_report / td_redeem_promo), Yahoo and GitHub.
Checks that a report only starts after the sign-in, plan, usage
reservation and exclusive claim all succeed (any failure refuses instead
of letting it through), that a report GitHub accepts is marked
dispatched, that only a clear GitHub refusal (4xx) un-charges it while an
unclear outcome keeps it charged and marks it dispatch_uncertain, that
every write after the claim is scoped to this request's claim token, and
that promo codes go through td_redeem_promo.

The scenario tests at the bottom run several requests in one process
against a fake database that enforces the same UPDATE ... WHERE rules as
the SQL (only a 'reserved' row can be reused or claimed; marks and
refunds only match the caller's own claim token), and check the lost-reply,
concurrent-request and failed-mark cases. The SQL side is in
sql/2026-11-10_lock_plans_and_report_usage.sql. Skipped without Node."""

import json
import os
import re
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
  claim: (c) => json({ ok: true, id: c.body.p_reservation_id }),
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
  if (url.endsWith("/rest/v1/rpc/td_claim_report")) return H.claim(call);
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


CLAIMED = ["auth", "yahoo", "rpc:td_reserve_report", "rpc:td_claim_report", "github"]
DISPATCHED = [*CLAIMED, "PATCH:report_generations"]
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def _token(res):
    """The claim token this request sent to td_claim_report."""
    claims = [c for c in res["calls"] if c["url"].endswith("/rpc/td_claim_report")]
    assert len(claims) == 1, claims
    assert claims[0]["body"]["p_reservation_id"] == RES_ID
    tok = claims[0]["body"]["p_claim_token"]
    assert UUID_RE.match(tok), tok
    return tok


def _scoped(res, call):
    """A write after the claim names this row, this user and this request's token."""
    url = call["url"]
    assert "id=eq." + RES_ID in url and "user_id=eq." + USER in url
    assert "claim_token=eq." + _token(res) in url
    assert call["headers"]["Authorization"] == "Bearer service-key-secret"


def _marked(res, state):
    """The reservation was PATCHed to dispatch_state=state, under our claim."""
    c = res["calls"][-1]
    assert c["method"] == "PATCH", c
    _scoped(res, c)
    assert c["body"] == {"dispatch_state": state}


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
    gh = res["calls"][4]
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
    assert _kinds(res) == [*CLAIMED, "DELETE:report_generations"]
    _scoped(res, res["calls"][-1])
    assert "dispatch_state=eq.dispatching" in res["calls"][-1]["url"]


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
    assert _kinds(res) == DISPATCHED
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


# ── Reports: the exclusive claim ──────────────────────────────────────


def test_each_request_claims_with_a_fresh_token():
    a, b = _run(), _run()
    assert _token(a) != _token(b)


def test_claim_lost_to_another_request_is_409_and_nothing_starts():
    res = _run(
        "{ claim: () => new Response(JSON.stringify({ ok: false, code: 'not_claimable' }),"
        " { status: 200 }) }"
    )
    assert res["status"] == 409, res
    assert res["json"]["code"] == "already_starting"
    assert "already being started" in res["json"]["error"]
    assert _kinds(res) == ["auth", "yahoo", "rpc:td_reserve_report", "rpc:td_claim_report"]


CLAIM_FAILURES = {
    "http-500": "{ claim: () => new Response('boom', { status: 500 }) }",
    "network-down": "{ claim: () => { throw new Error('ECONNRESET'); } }",
    "function-missing": '{ claim: () => new Response(\'{"code":"PGRST202"}\', { status: 404 }) }',
    "wrong-id": "{ claim: () => new Response(JSON.stringify({ ok: true, id: 'other' })) }",
    "unknown-code": "{ claim: () => new Response(JSON.stringify({ ok: false, code: 'x' })) }",
    "not-json": "{ claim: () => new Response('<html>', { status: 200 }) }",
}


@pytest.mark.parametrize("fakes", list(CLAIM_FAILURES.values()), ids=list(CLAIM_FAILURES))
def test_claim_failure_is_503_and_nothing_starts(fakes):
    res = _run(fakes)
    assert res["status"] == 503, res
    assert res["json"]["code"] == "quota_unavailable"
    assert _kinds(res) == ["auth", "yahoo", "rpc:td_reserve_report", "rpc:td_claim_report"]


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


# ── Scenarios: several requests against one fake database ─────────────
#
# The fake database keeps report_generations rows and applies the same
# rules as the SQL: td_reserve_report reuses only an unclaimed 'reserved'
# row for the same user + ticker + type, else inserts one under the cap;
# td_claim_report is UPDATE ... WHERE dispatch_state = 'reserved'; PATCH and
# DELETE apply the PostgREST eq. filters in the URL, so a write scoped to a
# different claim token matches nothing. HOOKS (set per scenario) can make a
# reply get lost AFTER the database committed, hold replies back to force an
# interleaving, or fail writes. SCENARIO is an async function given
# `send(ticker)`, which runs one request through the real worker, and
# `gate(n)`, which holds callers until n of them are waiting.

SCENARIO_HARNESS = r"""
import worker from "./worker.mjs";

const env = {
  PAT: "ghp_test",
  SUPABASE_URL: "https://db.example.supabase.co",
  SUPABASE_ANON_KEY: "anon-key",
  SUPABASE_SERVICE_KEY: "service-key-secret",
};
const logs = [];
for (const k of ["log", "error", "warn"]) {
  console[k] = (...a) => logs.push(a.map(String).join(" "));
}
const json = (o, s = 200) => new Response(JSON.stringify(o), { status: s });
const rows = [];
const events = [];
let seq = 0;
const H = HOOKS;
const CAP = 3;

function reserve(p) {
  const mine = rows.filter((r) => r.user_id === p.p_user);
  const reuse = mine.filter((r) => r.ticker === p.p_ticker &&
    r.report_type === p.p_report_type && r.dispatch_state === "reserved" &&
    r.claim_token === null).pop();
  if (reuse) {
    return { ok: true, id: reuse.id, tier: "free", used: mine.length, cap: CAP, reused: true };
  }
  if (mine.length >= CAP) {
    return { ok: false, code: "monthly_cap", tier: "free", used: mine.length, cap: CAP };
  }
  const id = "00000000-0000-4000-8000-" + String(++seq).padStart(12, "0");
  rows.push({ id, user_id: p.p_user, ticker: p.p_ticker, report_type: p.p_report_type,
              dispatch_state: "reserved", claim_token: null });
  return { ok: true, id, tier: "free", used: mine.length + 1, cap: CAP };
}
function claim(p) {
  const r = rows.find((x) => x.id === p.p_reservation_id &&
    x.dispatch_state === "reserved" && x.claim_token === null);
  if (!r) return { ok: false, code: "not_claimable" };
  r.dispatch_state = "dispatching";
  r.claim_token = p.p_claim_token;
  return { ok: true, id: r.id };
}
function matching(url) {
  const f = {};
  for (const [k, v] of new URL(url).searchParams) {
    if (!v.startsWith("eq.")) throw new Error("unsupported filter " + k + "=" + v);
    f[k] = v.slice(3);
  }
  if (!f.id || !f.claim_token) throw new Error("write not scoped to a claim: " + url);
  return rows.filter((r) => Object.entries(f).every(([k, v]) => String(r[k]) === v));
}

globalThis.fetch = async (url, init = {}) => {
  url = String(url);
  const body = init.body ? JSON.parse(init.body) : null;
  const method = init.method || "GET";
  if (url.endsWith("/auth/v1/user")) return json({ id: USER });
  if (url.includes("finance.yahoo.com")) return json({ chart: { result: [{ timestamp: [1] }] } });
  if (url.endsWith("/rpc/td_reserve_report")) {
    const out = reserve(body);
    events.push(["reserve", out.id || out.code]);
    if (H.afterReserve) await H.afterReserve(out);   // may throw: reply lost
    return json(out);
  }
  if (url.endsWith("/rpc/td_claim_report")) {
    const out = claim(body);
    events.push(["claim", body.p_reservation_id, out.ok]);
    if (H.afterClaim) await H.afterClaim(out);       // may throw: reply lost
    return json(out);
  }
  if (url.startsWith("https://api.github.com/")) {
    events.push(["github", body.inputs.ticker]);
    return H.github ? H.github(body) : new Response(null, { status: 204 });
  }
  if (url.includes("/rest/v1/report_generations")) {
    if (H.writeFails) throw new Error("supabase down");
    const hit = matching(url);
    if (method === "PATCH") for (const r of hit) Object.assign(r, body);
    if (method === "DELETE") for (const r of hit) rows.splice(rows.indexOf(r), 1);
    events.push([method, hit.length]);
    return new Response(null, { status: 204 });
  }
  return new Response("unexpected " + url, { status: 599 });
};

async function send(ticker) {
  const req = new Request("https://api.tickerdesk.io/", {
    method: "POST",
    headers: { "Content-Type": "application/json", "CF-Connecting-IP": "203.0.113.9",
               "Authorization": "Bearer user-jwt" },
    body: JSON.stringify({ ticker, report: "adhoc" }),
  });
  const resp = await worker.fetch(req, env, { waitUntil() {} });
  return { status: resp.status, body: JSON.parse(await resp.text()) };
}
function gate(n) {
  let waiting = 0;
  let open;
  const p = new Promise((r) => { open = r; });
  return () => { if (++waiting >= n) open(); return p; };
}

const results = await SCENARIO(send, gate);
process.stdout.write(JSON.stringify({
  results, events, logs,
  rows: rows.map(({ claim_token, ...r }) => ({ ...r, claimed: claim_token !== null })),
}));
"""


def _scenario(hooks_js, scenario_js):
    with tempfile.TemporaryDirectory() as d:
        shutil.copy(WORKER, os.path.join(d, "worker.mjs"))
        for name in os.listdir(os.path.dirname(WORKER)):
            if name.endswith(".mjs"):
                shutil.copy(os.path.join(os.path.dirname(WORKER), name), d)
        script = (
            f"const USER = {json.dumps(USER)};\n"
            f"const HOOKS = {hooks_js};\n"
            f"const SCENARIO = {scenario_js};\n" + SCENARIO_HARNESS
        )
        p = os.path.join(d, "run.mjs")
        with open(p, "w", encoding="utf-8") as f:
            f.write(script)
        out = subprocess.run(
            [NODE, p], check=False, capture_output=True, encoding="utf-8", timeout=60, cwd=d
        )
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout)


def _github_calls(res):
    return [e for e in res["events"] if e[0] == "github"]


def test_concurrent_requests_for_one_reservation_only_one_starts():
    # Both requests reach td_reserve_report before either claims, so both
    # are handed the same 'reserved' row. Only one can claim it.
    res = _scenario(
        "{}",
        """async (send, gate) => {
            HOOKS.afterReserve = gate(2);
            return Promise.all([send("NVDA"), send("NVDA")]);
        }""",
    )
    reserved = [e[1] for e in res["events"] if e[0] == "reserve"]
    assert len(reserved) == 2 and reserved[0] == reserved[1]  # same row handed out twice
    assert sorted(r["status"] for r in res["results"]) == [200, 409], res
    loser = next(r for r in res["results"] if r["status"] == 409)
    assert loser["body"]["code"] == "already_starting"
    assert len(_github_calls(res)) == 1
    assert len(res["rows"]) == 1
    assert res["rows"][0]["dispatch_state"] == "dispatched"


def test_many_concurrent_requests_one_dispatch_per_reservation():
    res = _scenario(
        "{}",
        """async (send, gate) => {
            HOOKS.afterReserve = gate(5);
            return Promise.all([1, 2, 3, 4, 5].map(() => send("NVDA")));
        }""",
    )
    assert sorted(r["status"] for r in res["results"]) == [200, 409, 409, 409, 409], res
    assert len(_github_calls(res)) == 1
    assert len(res["rows"]) == 1


def test_lost_reserve_reply_retry_reuses_the_same_row():
    res = _scenario(
        "{ lose: 1 }",
        """async (send) => {
            HOOKS.afterReserve = () => { if (HOOKS.lose-- > 0) throw new Error("reply lost"); };
            return [await send("NVDA"), await send("NVDA")];
        }""",
    )
    first, retry = res["results"]
    assert first["status"] == 503 and first["body"]["code"] == "quota_unavailable"
    assert retry["status"] == 200, res
    assert len(res["rows"]) == 1, res["rows"]  # charged once
    assert res["rows"][0]["dispatch_state"] == "dispatched"
    assert len(_github_calls(res)) == 1


def test_lost_claim_reply_retry_is_charged_but_starts_once():
    res = _scenario(
        "{ lose: 1 }",
        """async (send) => {
            HOOKS.afterClaim = () => { if (HOOKS.lose-- > 0) throw new Error("reply lost"); };
            return [await send("NVDA"), await send("NVDA")];
        }""",
    )
    first, retry = res["results"]
    assert first["status"] == 503
    assert retry["status"] == 200, res
    assert [r["dispatch_state"] for r in res["rows"]] == ["dispatching", "dispatched"]
    assert len(_github_calls(res)) == 1


def test_github_refusal_only_refunds_its_own_claim():
    # An earlier NVDA report already started; a new NVDA request is refused
    # by GitHub (422). Its refund deletes only its own row.
    res = _scenario(
        "{}",
        """async (send) => {
            const first = await send("NVDA");
            HOOKS.github = () => new Response("nope", { status: 422 });
            return [first, await send("NVDA")];
        }""",
    )
    first, refused = res["results"]
    assert first["status"] == 200 and refused["status"] == 502
    assert len(res["rows"]) == 1 and res["rows"][0]["dispatch_state"] == "dispatched"
    assert ["DELETE", 1] in res["events"]


def test_losing_request_never_writes_to_the_winners_row():
    # Request A wins the claim and waits on GitHub; request B got the same
    # reservation, loses the claim (409) and writes nothing. A then succeeds.
    res = _scenario(
        "{}",
        """async (send, gate) => {
            HOOKS.afterReserve = gate(2);
            let release;
            const held = new Promise((r) => { release = r; });
            HOOKS.github = async () => { await held; return new Response(null, { status: 204 }); };
            const a = send("NVDA");
            const b = send("NVDA");
            const first = await Promise.race([a, b]);
            release();
            return [first, ...(await Promise.all([a, b]))];
        }""",
    )
    first, a, b = res["results"]
    assert first["status"] == 409, res  # the loser answers while the winner waits
    assert sorted([a["status"], b["status"]]) == [200, 409]
    assert not [e for e in res["events"] if e[0] == "DELETE"]
    assert [e for e in res["events"] if e[0] == "PATCH"] == [["PATCH", 1]]
    assert res["rows"][0]["dispatch_state"] == "dispatched"


def test_refusal_after_a_lost_claim_reply_cannot_delete_the_claimed_row():
    # The first request's claim committed but its reply was lost, so it
    # stops (503). A stray refund aimed at that row with any other token
    # matches nothing: the fake applies the same id + claim_token filter.
    res = _scenario(
        "{ lose: 1 }",
        """async (send) => {
            HOOKS.afterClaim = () => { if (HOOKS.lose-- > 0) throw new Error("reply lost"); };
            const first = await send("NVDA");
            HOOKS.github = () => new Response("nope", { status: 422 });
            return [first, await send("NVDA")];
        }""",
    )
    first, refused = res["results"]
    assert first["status"] == 503 and refused["status"] == 502
    # the refused retry deleted only its own row; the first stays charged
    assert [r["dispatch_state"] for r in res["rows"]] == ["dispatching"]
    assert ["DELETE", 1] in res["events"]
    assert len(_github_calls(res)) == 1  # the first request never reached GitHub


@pytest.mark.parametrize("github", ["204", "500"], ids=["accepted", "unclear"])
def test_persistent_mark_failure_gives_no_free_repeats(github):
    # Every write after the claim fails, so rows stay 'dispatching'. They
    # are still counted and never reused: a free user gets exactly 3.
    res = _scenario(
        "{ writeFails: true, github: () => new Response(null, { status: %s }) }" % github,
        """async (send) => {
            const out = [];
            for (let i = 0; i < 5; i++) out.push(await send("NVDA"));
            return out;
        }""",
    )
    ok = 200 if github == "204" else 202
    assert [r["status"] for r in res["results"]] == [ok, ok, ok, 429, 429], res
    assert len(_github_calls(res)) == 3
    assert [r["dispatch_state"] for r in res["rows"]] == ["dispatching"] * 3


def test_refund_failure_keeps_the_report_charged():
    res = _scenario(
        "{ writeFails: true, github: () => new Response('nope', { status: 404 }) }",
        """async (send) => [await send("NVDA"), await send("NVDA")]""",
    )
    assert [r["status"] for r in res["results"]] == [502, 502]
    assert [r["dispatch_state"] for r in res["rows"]] == ["dispatching", "dispatching"]
    assert len(_github_calls(res)) == 2
