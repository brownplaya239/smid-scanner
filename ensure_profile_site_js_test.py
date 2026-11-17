"""ensureProfile() from the site (docs/index.html), run in Node.

Extracts the real function and runs it against a fake tdSupaRestFetch, to
check the guarantees the profile fix relies on: one request per user even
when several callers ask at once, never a write to an existing row's plan,
and failures that resolve false (and can be retried) instead of throwing.
Skipped when Node is not installed."""

import json
import os
import shutil
import subprocess
import tempfile

import pytest

from flow_site_js_test import HTML, _extract

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

# The page-level state ensureProfile() uses, then the function itself, then
# a scenario that drives it. RESP is a queue of answers ("ok", "500",
# "throw", or "hang" / "hang500" = answer only when release() is called).
PRELUDE = r"""
let CURRENT_USER = { id: "user-a" };
let _tdProfileEnsuredFor = null;
let _tdProfileEnsuring = null;
const calls = [];
const RESP = [];
const pending = [];
const window = {};
console.warn = () => {};
function tdSupaRestFetch(path, init) {
  calls.push({ path, method: init.method, headers: init.headers, body: init.body });
  const kind = RESP.length ? RESP.shift() : "ok";
  if (kind === "throw") return Promise.reject(new Error("offline"));
  const r = kind.endsWith("500") ? { ok: false, status: 500 } : { ok: true, status: 201 };
  if (kind.startsWith("hang")) return new Promise((res) => pending.push(() => res(r)));
  return Promise.resolve(r);
}
const release = () => { while (pending.length) pending.shift()(); };
const tick = () => new Promise((r) => setTimeout(r, 0));
"""


def _run(scenario):
    with open(HTML, encoding="utf-8") as f:
        src = f.read()
    js = PRELUDE + _extract(src, "ensureProfile") + "\n(async () => {\n" + scenario + "\n})();"
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as f:
        f.write(js)
    try:
        out = subprocess.run(
            [NODE, f.name], capture_output=True, encoding="utf-8", check=False, timeout=30
        )
    finally:
        os.unlink(f.name)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout)


def _emit(expr):
    return f"process.stdout.write(JSON.stringify({expr}));"


def test_request_inserts_only_id_and_never_overwrites():
    res = _run("const ok = await ensureProfile();" + _emit("{ ok, calls }"))
    assert res["ok"] is True
    (c,) = res["calls"]
    assert c["method"] == "POST"
    assert c["path"] == "/rest/v1/profiles?on_conflict=id"
    # ON CONFLICT DO NOTHING: an existing row (and its plan) is never touched
    assert "resolution=ignore-duplicates" in c["headers"]["Prefer"]
    assert sorted(c["body"]) == ["id", "last_seen"]
    assert c["body"]["id"] == "user-a"


def test_concurrent_callers_share_one_request():
    res = _run(
        'RESP.push("hang");'
        "const ps = [ensureProfile(), ensureProfile(), ensureProfile()];"
        "await tick(); const during = calls.length; release();"
        "const oks = await Promise.all(ps);"
        "const again = await ensureProfile();" + _emit("{ during, oks, again, n: calls.length }")
    )
    assert res["during"] == 1
    assert res["oks"] == [True, True, True]
    assert res["again"] is True
    assert res["n"] == 1  # cached after success: no second request


@pytest.mark.parametrize("kind", ["500", "throw"])
def test_failure_resolves_false_and_can_retry(kind):
    res = _run(
        f'RESP.push("{kind}");'
        "const first = await ensureProfile();"
        "const second = await ensureProfile();" + _emit("{ first, second, n: calls.length }")
    )
    assert res["first"] is False  # never throws
    assert res["second"] is True  # not cached as done, and not stuck in flight
    assert res["n"] == 2


def test_concurrent_callers_all_see_the_failure():
    res = _run(
        'RESP.push("hang500");'
        "const ps = [ensureProfile(), ensureProfile()];"
        "await tick(); release();"
        "const oks = await Promise.all(ps);"
        "const retry = await ensureProfile();" + _emit("{ oks, retry, n: calls.length }")
    )
    assert res["oks"] == [False, False]
    assert res["retry"] is True
    assert res["n"] == 2


def test_no_user_makes_no_request():
    res = _run("CURRENT_USER = null; const ok = await ensureProfile();" + _emit("{ ok, calls }"))
    assert res == {"ok": False, "calls": []}


def test_switching_user_mid_request_starts_a_new_one():
    res = _run(
        'RESP.push("hang");'
        "const a = ensureProfile();"
        'CURRENT_USER = { id: "user-b" };'
        "const b = ensureProfile();"
        "release();"
        "const both = await Promise.all([a, b]);"
        "const ids = calls.map((c) => c.body.id);" + _emit("{ both, ids }")
    )
    assert res["both"] == [True, True]
    assert res["ids"] == ["user-a", "user-b"]
