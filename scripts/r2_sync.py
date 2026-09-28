#!/usr/bin/env python3
"""r2_sync.py — move TickerDesk's published data between the runner and
Cloudflare R2 (bucket `tickerdesk-data`), replacing git as the data store.

Uses Cloudflare's R2 object REST API with CLOUDFLARE_API_TOKEN (the CI
secret the worker deploy already uses). Keys mirror repo paths:
  docs/reports/foo.json  ->  reports/foo.json   (served publicly by the
                                                  worker at /reports/)
  data/bar.jsonl         ->  data/bar.jsonl     (internal, never served)

    python scripts/r2_sync.py push FILE [FILE ...]   # upload these paths
    python scripts/r2_sync.py push --git-staged      # upload data files
                                                    # staged in git index
    python scripts/r2_sync.py push --all             # seed everything
    python scripts/r2_sync.py pull [--prefix data/]  # download all objects
                                                    # into their repo paths
                                                    # + record a manifest
    python scripts/r2_sync.py push --changed         # upload only files that
                                                    # differ from the pull
                                                    # manifest (or are new)
    python scripts/r2_sync.py pull-public            # reports/*.json via the
                                                    # public worker, no token
                                                    # (local dev / dry runs)
    python scripts/r2_sync.py list [--prefix ...]
    python scripts/r2_sync.py verify                 # local == R2, byte-wise

Exit code is non-zero if any transfer failed; failures are listed.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ACCOUNT = os.environ.get("CF_ACCOUNT_ID",
                         "06ea98cb3e05c64a52517aeb802d96a4")
BUCKET = os.environ.get("R2_BUCKET", "tickerdesk-data")
API = (f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}"
       f"/r2/buckets/{BUCKET}/objects")
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _token():
    t = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if not t:
        sys.exit("CLOUDFLARE_API_TOKEN not set")
    return t


# Files that stay in git and are never synced: manifest.json is derived
# from the report PDFs (which stay in git); the rest are hand-edited
# config or test fixtures. An R2 pull must never overwrite them.
GIT_ONLY = {"docs/reports/manifest.json", "docs/reports/event_overrides.json",
            "data/hedge_monitor_exclusions.jsonl",
            "data/fixtures_expected.json"}
GIT_ONLY_DIRS = ("data/mutation_proofs/", "data/research_state/",
                 "data/listing_facts/")


def _git_only(rel):
    return rel in GIT_ONLY or rel.startswith(GIT_ONLY_DIRS)


def repo_to_key(path):
    rel = os.path.relpath(os.path.abspath(path), _BASE).replace("\\", "/")
    if _git_only(rel):
        return None
    if rel.startswith("docs/reports/"):
        return "reports/" + rel[len("docs/reports/"):]
    if rel.startswith("data/"):
        return rel
    return None


def key_to_repo(key):
    if _git_only("docs/" + key) or _git_only(key):
        return None
    if key.startswith("reports/"):
        return os.path.join(_BASE, "docs", "reports", key[len("reports/"):])
    if key.startswith("data/"):
        return os.path.join(_BASE, *key.split("/"))
    return None


def _ctype(key):
    if key.endswith(".json"):
        return "application/json; charset=utf-8"
    if key.endswith(".jsonl"):
        return "application/x-ndjson"
    if key.endswith(".gz"):
        return "application/gzip"
    if key.endswith(".png"):
        return "image/png"
    return "application/octet-stream"


def _req(method, url, data=None, headers=None, timeout=120):
    h = {"Authorization": "Bearer " + _token()}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    return urllib.request.urlopen(req, timeout=timeout)


MANIFEST = os.path.join(_BASE, ".r2_manifest.json")


def _md5(path):
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_manifest(keys):
    """What this runner pulled: {key: md5}. push --changed compares
    against it so a run only uploads files IT changed — never a stale
    copy of a file another workflow updated meanwhile."""
    man = {}
    for k in keys:
        p = key_to_repo(k)
        if p and os.path.isfile(p):
            man[k] = _md5(p)
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(man, f)
    return man


def _manifest():
    try:
        with open(MANIFEST, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        # Refuse rather than guess: without a manifest every local file
        # looks new, and a job that never pulled would overwrite R2 with
        # whatever partial copies it happens to have.
        sys.exit("no .r2_manifest.json — run `pull` (or `init` for a job "
                 "that reads no data) at the start of the job")


def changed_paths():
    man = _manifest()
    out = []
    for p in data_paths():
        k = repo_to_key(p)
        if k and man.get(k) != _md5(p):
            out.append(p)
    return out


def deleted_keys():
    """Keys this runner pulled whose local file is now gone (pruned)."""
    return [k for k in _manifest()
            if key_to_repo(k) and not os.path.exists(key_to_repo(k))]


def delete(key):
    url = API + "/" + urllib.parse.quote(key, safe="")
    for attempt in range(3):
        try:
            with _req("DELETE", url) as r:
                r.read()
            return key, None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return key, None
            err = f"HTTP {e.code}"
            if e.code < 500 and e.code != 429:
                break
        except Exception as e:
            err = str(e)[:120]
    return key, err


def list_keys_local():
    return [repo_to_key(p) for p in data_paths() if repo_to_key(p)]


def pull_public():
    """reports/*.json through the worker's public /reports/ route."""
    base = "https://api.tickerdesk.io/reports/"
    with urllib.request.urlopen(urllib.request.Request(
            base.rstrip("/") + "/index.json",
            headers={"User-Agent": "Mozilla/5.0"}), timeout=60) as r:
        names = json.loads(r.read())

    def one(name):
        try:
            req = urllib.request.Request(base + name,
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
            dest = os.path.join(_BASE, "docs", "reports", name)
            with open(dest + ".r2tmp", "wb") as f:
                f.write(body)
            os.replace(dest + ".r2tmp", dest)
            return name, None
        except Exception as e:
            return name, str(e)[:120]
    return names, _run(one, names)


def put(path):
    key = repo_to_key(path)
    if not key:
        return path, "skipped (not a data path)"
    return put_key(path, key)


def get_json(key):
    """Small JSON object by key (None if missing) — e.g. review records."""
    url = API + "/" + urllib.parse.quote(key, safe="")
    try:
        with _req("GET", url) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def put_json(key, obj):
    url = API + "/" + urllib.parse.quote(key, safe="")
    with _req("PUT", url, data=json.dumps(obj).encode("utf-8"),
              headers={"Content-Type": "application/json"}) as r:
        r.read()


def put_key(path, key):
    """Upload a local file to an explicit key (e.g. cards/<run>_x.png)."""
    with open(path, "rb") as f:
        body = f.read()
    url = API + "/" + urllib.parse.quote(key, safe="")
    for attempt in range(3):
        try:
            with _req("PUT", url, data=body,
                      headers={"Content-Type": _ctype(key)}) as r:
                r.read()
            return path, None
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}"
            if e.code < 500 and e.code != 429:
                break
        except Exception as e:  # network blips
            err = str(e)[:120]
    return path, err


def get(key):
    dest = key_to_repo(key)
    if not dest:
        return key, "skipped"
    url = API + "/" + urllib.parse.quote(key, safe="")
    for attempt in range(3):
        try:
            with _req("GET", url) as r:
                body = r.read()
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".r2tmp"
            with open(tmp, "wb") as f:
                f.write(body)
            os.replace(tmp, dest)
            return key, None
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}"
            if e.code < 500 and e.code != 429:
                break
        except Exception as e:
            err = str(e)[:120]
    return key, err


def list_keys(prefix=""):
    keys, cursor = [], None
    while True:
        q = {"per_page": "1000"}
        if prefix:
            q["prefix"] = prefix
        if cursor:
            q["cursor"] = cursor
        with _req("GET", API + "?" + urllib.parse.urlencode(q)) as r:
            d = json.loads(r.read())
        for o in d.get("result") or []:
            keys.append(o["key"])
        cursor = (d.get("result_info") or {}).get("cursor")
        if not cursor or not (d.get("result_info") or {}).get("is_truncated"):
            break
    return keys


def data_paths():
    out = sorted(glob.glob(os.path.join(_BASE, "docs", "reports", "*.json")))
    for root, _, files in os.walk(os.path.join(_BASE, "data")):
        for fn in files:
            out.append(os.path.join(root, fn))
    return out


def git_staged():
    r = subprocess.run(["git", "diff", "--cached", "--name-only",
                        "--diff-filter=AM"], cwd=_BASE,
                       capture_output=True, text=True)
    return [os.path.join(_BASE, p) for p in r.stdout.split()
            if repo_to_key(os.path.join(_BASE, p))]


def _run(fn, items):
    fails = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for item, err in ex.map(fn, items):
            if err and not err.startswith("skipped"):
                fails.append((item, err))
    return fails


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd, args = sys.argv[1], sys.argv[2:]
    prefix = ""
    if "--prefix" in args:
        prefix = args[args.index("--prefix") + 1]
    if cmd == "push":
        if "--all" in args:
            paths = data_paths()
        elif "--changed" in args:
            paths = changed_paths()
        elif "--git-staged" in args:
            paths = git_staged()
        else:
            paths = [a for a in args if not a.startswith("--")]
        paths = [p for p in paths if os.path.isfile(p) and repo_to_key(p)]
        fails = _run(put, paths)
        print(f"[r2] pushed {len(paths) - len(fails)}/{len(paths)} objects")
        if "--changed" in args:
            gone = deleted_keys()
            dfails = _run(delete, gone)
            fails += dfails
            if gone:
                print(f"[r2] deleted {len(gone) - len(dfails)}/{len(gone)} "
                      "pruned objects")
            if not fails:
                # This run's writes are now R2's state: a second push in
                # the same job only sends what changed after this one.
                write_manifest(list_keys_local())
    elif cmd == "init":
        # For jobs that read no data but may create new files.
        with open(MANIFEST, "w", encoding="utf-8") as f:
            json.dump({}, f)
        print("[r2] empty manifest (job reads no data)")
        return
    elif cmd == "pull":
        keys = list_keys(prefix)
        fails = _run(get, keys)
        write_manifest(keys)
        print(f"[r2] pulled {len(keys) - len(fails)}/{len(keys)} objects")
    elif cmd == "pull-public":
        names, fails = pull_public()
        print(f"[r2] pulled {len(names) - len(fails)}/{len(names)} "
              "public reports")
    elif cmd == "verify":
        # Parity check for the migration: every local data file must be
        # byte-identical in R2 (compares MD5 of local bytes vs object).
        import hashlib
        paths = [p for p in data_paths() if repo_to_key(p)]
        def chk(path):
            key = repo_to_key(path)
            url = API + "/" + urllib.parse.quote(key, safe="")
            try:
                with _req("GET", url) as r:
                    remote = hashlib.md5(r.read()).hexdigest()
            except urllib.error.HTTPError as e:
                return path, f"missing in R2 (HTTP {e.code})"
            with open(path, "rb") as f:
                local = hashlib.md5(f.read()).hexdigest()
            return path, None if local == remote else "content differs"
        fails = _run(chk, paths)
        print(f"[r2] verified {len(paths) - len(fails)}/{len(paths)} "
              "identical")
    elif cmd == "list":
        for k in list_keys(prefix):
            print(k)
        return
    else:
        sys.exit(__doc__)
    for item, err in fails:
        print(f"[r2] FAILED {item}: {err}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
