"""r2_sync: key mapping and push --changed (only files this run changed)."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import r2_sync


class Mapping(unittest.TestCase):
    def test_keys(self):
        b = r2_sync._BASE
        self.assertEqual(
            r2_sync.repo_to_key(os.path.join(b, "docs", "reports", "aaa.json")), "reports/aaa.json"
        )
        self.assertEqual(
            r2_sync.repo_to_key(os.path.join(b, "data", "bbb.jsonl")), "data/bbb.jsonl"
        )
        self.assertIsNone(r2_sync.repo_to_key(os.path.join(b, "docs", "index.html")))

    def test_manifest_json_stays_in_git(self):
        b = r2_sync._BASE
        self.assertIsNone(r2_sync.repo_to_key(os.path.join(b, "docs", "reports", "manifest.json")))
        self.assertIsNone(r2_sync.key_to_repo("reports/manifest.json"))


class Changed(unittest.TestCase):
    def test_only_changed_and_new_files(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "data"))
            os.makedirs(os.path.join(d, "docs", "reports"))
            paths = {}
            for rel in ("data/same.json", "data/edited.json", "docs/reports/r.json"):
                p = os.path.join(d, *rel.split("/"))
                with open(p, "w") as f:
                    f.write("v1")
                paths[rel] = p
            with (
                mock.patch.object(r2_sync, "_BASE", d),
                mock.patch.object(r2_sync, "MANIFEST", os.path.join(d, ".m.json")),
            ):
                r2_sync.write_manifest(["data/same.json", "data/edited.json", "reports/r.json"])
                with open(paths["data/edited.json"], "w") as f:
                    f.write("v2")
                new = os.path.join(d, "data", "new.jsonl")
                with open(new, "w") as f:
                    f.write("x")
                got = sorted(
                    os.path.relpath(p, d).replace("\\", "/") for p in r2_sync.changed_paths()
                )
        self.assertEqual(got, ["data/edited.json", "data/new.jsonl"])


class DeletesAndGitOnly(unittest.TestCase):
    def test_pruned_file_is_deleted_key(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "data"))
            p = os.path.join(d, "data", "old.json")
            with open(p, "w") as f:
                f.write("x")
            with (
                mock.patch.object(r2_sync, "_BASE", d),
                mock.patch.object(r2_sync, "MANIFEST", os.path.join(d, ".m.json")),
            ):
                r2_sync.write_manifest(["data/old.json"])
                os.remove(p)
                self.assertEqual(r2_sync.deleted_keys(), ["data/old.json"])

    def test_fixtures_and_config_never_sync(self):
        b = r2_sync._BASE
        for rel in (
            "data/fixtures_expected.json",
            "data/mutation_proofs/x.json",
            "data/hedge_monitor_exclusions.jsonl",
            "docs/reports/event_overrides.json",
        ):
            self.assertIsNone(r2_sync.repo_to_key(os.path.join(b, *rel.split("/"))), rel)
        self.assertIsNone(r2_sync.key_to_repo("reports/event_overrides.json"))
        self.assertIsNone(r2_sync.key_to_repo("data/research_state/a.jsonl"))


class PullPublic(unittest.TestCase):
    def test_index_names_skip_git_only_and_paths(self):
        got = r2_sync.public_names(
            ["a.json", "manifest.json", "event_overrides.json", "../x.json", "sub/b.json", "", 3]
        )
        self.assertEqual(got, ["a.json"])

    def test_pull_public_never_overwrites_manifest(self):
        bodies = {
            "index.json": json.dumps(["a.json", "manifest.json"]).encode(),
            "a.json": b'{"r2": 1}',
            "manifest.json": b'{"stale": true}',
        }
        fetched = []

        class Resp:
            def __init__(self, body):
                self.body = body

            def read(self):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_open(req, timeout=0):
            name = req.full_url.rsplit("/", 1)[-1]
            fetched.append(name)
            return Resp(bodies[name])

        with tempfile.TemporaryDirectory() as d:
            rep = os.path.join(d, "docs", "reports")
            os.makedirs(rep)
            with open(os.path.join(rep, "manifest.json"), "w") as f:
                f.write('{"git": true}')
            with (
                mock.patch.object(r2_sync, "_BASE", d),
                mock.patch.object(r2_sync.urllib.request, "urlopen", side_effect=fake_open),
            ):
                names, fails = r2_sync.pull_public()
            with open(os.path.join(rep, "manifest.json")) as f:
                self.assertEqual(json.load(f), {"git": True})
            with open(os.path.join(rep, "a.json")) as f:
                self.assertEqual(json.load(f), {"r2": 1})
        self.assertEqual(names, ["a.json"])
        self.assertFalse(fails)
        self.assertNotIn("manifest.json", fetched)


class Commands(unittest.TestCase):
    def test_init_writes_empty_manifest_and_exits_cleanly(self):
        with tempfile.TemporaryDirectory() as d:
            m = os.path.join(d, ".m.json")
            with (
                mock.patch.object(r2_sync, "MANIFEST", m),
                mock.patch.object(sys, "argv", ["r2_sync.py", "init"]),
            ):
                r2_sync.main()  # must not raise
            with open(m) as f:
                self.assertEqual(json.load(f), {})


if __name__ == "__main__":
    unittest.main()
