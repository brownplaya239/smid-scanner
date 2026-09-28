"""Social post pipeline: X length counting, caption fitting, and the
publishing gate. Placeholder tickers only (AAA/BBB)."""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "scripts"))
import x_post  # noqa: E402


class XLength(unittest.TestCase):
    def test_links_count_23(self):
        self.assertEqual(x_post.x_len("api.tickerdesk.io/go/flow"), 23)
        self.assertEqual(x_post.x_len("see https://example.com/a?b=c"), 4 + 23)

    def test_plain_text(self):
        self.assertEqual(x_post.x_len("$AAA +12%"), 9)

    def test_fit_drops_items_not_tail(self):
        head, tail = ["H"], ["", x_post.link("flow")]
        items = ["x" * 100] * 5
        out = x_post._fit(head, items, tail)
        self.assertLessEqual(x_post.x_len(out), 280)
        self.assertTrue(out.endswith(x_post.link("flow")))


class Captions(unittest.TestCase):
    def test_flow_interleaves_calls_and_puts(self):
        f = {"day": "2026-09-28", "body": [
            ["AAA Oct 2 $10C", "Calls", "$9.0M", "3", "5x"],
            ["BBB Oct 2 $20C", "Calls", "$8.0M", "3", "4x"],
            ["AAA Oct 2 $9P", "Puts", "$7.0M", "3", "3x"]]}
        text = x_post.flow_caption(f)
        self.assertLess(text.index("$AAA Oct 2 $9P"),
                        text.index("$BBB Oct 2 $20C"))
        self.assertIn("not classified", text)

    def test_preview_lead_is_observation(self):
        rich = [{"implied": 6, "realized_med": 3}] * 3
        self.assertIn("bigger-than-usual", x_post._preview_lead(rich))
        cheap = [{"implied": 2, "realized_med": 4}] * 3
        self.assertIn("smaller", x_post._preview_lead(cheap))


class Gate(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(x_post, "_log_rows", return_value=[])
        p.start()
        self.addCleanup(p.stop)

    def _mom(self, chg="+40%", date=None):
        return {"which": "stockbee", "count": 1, "new": [],
                "date": date or x_post._now_et().date().isoformat(),
                "body": [["AAA", "$10.00", chg, "6.0%", "$120M", "—"]]}

    def test_empty_card_blocks(self):
        blocks, _ = x_post.gate("stockbee", {"body": [], "date": "x"},
                                "t", False)
        self.assertIn("card has no rows", blocks)

    def test_stale_blocks_only_when_scheduled(self):
        f = self._mom(date="2000-01-03")
        blocks, review = x_post.gate("stockbee", f, "t", False)
        self.assertTrue(any("run is 2000-01-03" in r for r in review))
        blocks, _ = x_post.gate("stockbee", f, "t", True)
        self.assertTrue(any("run is 2000-01-03" in b for b in blocks))

    def test_extreme_momentum_needs_review(self):
        _, review = x_post.gate("stockbee", self._mom("+450%"), "t", False)
        self.assertTrue(any("split" in r for r in review))

    def test_duplicate_caption_blocks(self):
        import hashlib
        h = hashlib.sha1(b"same").hexdigest()
        with mock.patch.object(x_post, "_log_rows",
                               return_value=[{"text_sha1": h}]):
            blocks, _ = x_post.gate("stockbee", self._mom(), "same", False)
        self.assertIn("identical caption already posted", blocks)

    def test_big_print_needs_review(self):
        f = {"day": x_post._now_et().date().isoformat(), "generated": None,
             "body": [["AAA Oct 2 $9P", "Puts", "$210.0M", "2", "3x"]]}
        _, review = x_post.gate("flow", f, "t", False)
        self.assertTrue(any("hedge" in r for r in review))


class SlackPreview(unittest.TestCase):
    def _send(self, b, env, card=None):
        sent = {}

        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"ok"

        def fake(req, timeout=0):
            sent["body"] = json.loads(req.data)
            return R()
        env = {"SLACK_WEBHOOK_URL": "https://hooks.example/x",
               "GITHUB_RUN_ID": "7", **env}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch("urllib.request.urlopen", fake):
            x_post.slack_preview(b, card)
        return sent.get("body")

    def _b(self, **kw):
        b = {"kind": "flow", "date": "2026-09-28", "text": "$AAA <x> & y",
             "alt": "alt", "blocks": [], "review": []}
        b.update(kw)
        return b

    def test_waiting_preview_has_caption_image_and_link(self):
        body = self._send(self._b(), {"SOCIAL_DRY_RUN": "false"},
                          "https://api.tickerdesk.io/cards/7_a.png")
        types = [bl["type"] for bl in body["blocks"]]
        self.assertIn("image", types)
        self.assertIn("waiting for your approval", body["text"])
        dump = json.dumps(body)
        self.assertIn("&lt;x&gt; &amp; y", dump)          # escaped
        self.assertIn("actions/runs/7", dump)

    def test_review_flags_listed(self):
        body = self._send(self._b(review=["AAA $210M premium"]),
                          {"SOCIAL_DRY_RUN": "false"})
        self.assertIn("Check before approving", json.dumps(body))

    def test_blocked_says_not_posting_without_image(self):
        body = self._send(self._b(blocks=["stale data"]),
                          {"SOCIAL_DRY_RUN": "false"})
        self.assertIn("Not posting", body["text"])
        self.assertNotIn("image", [bl["type"] for bl in body["blocks"]])

    def test_dry_run_labelled(self):
        body = self._send(self._b(), {"SOCIAL_DRY_RUN": "true"})
        self.assertIn("DRY RUN", body["text"])

    def test_no_webhook_no_call(self):
        with mock.patch.dict(os.environ, {"SLACK_WEBHOOK_URL": ""}), \
                mock.patch("urllib.request.urlopen") as u:
            x_post.slack_preview(self._b(), None)
        u.assert_not_called()


class SlackReview(unittest.TestCase):
    def _b(self, **kw):
        b = {"kind": "flow", "date": "2026-09-28", "text": "$AAA draft",
             "alt": "alt", "blocks": [], "review": []}
        b.update(kw)
        return b

    def _run(self, b, env):
        posted, saved = [], []

        def api(method, body):
            posted.append((method, body))
            return {"ok": True, "channel": "C1", "ts": "1.2"}
        env = {"SLACK_BOT_TOKEN": "x", "SLACK_REVIEW_CHANNEL": "C1",
               "GITHUB_RUN_ID": "7", **env}
        import r2_sync
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(x_post, "_slack_api", api), \
                mock.patch.object(r2_sync, "put_json",
                                  lambda k, o: saved.append((k, o))):
            ok = x_post.slack_review(b, "https://api.tickerdesk.io/cards/7.png")
        return ok, posted, saved

    def test_pending_draft_has_three_buttons_and_record(self):
        ok, posted, saved = self._run(self._b(), {"SOCIAL_DRY_RUN": "false"})
        self.assertTrue(ok)
        acts = [bl for bl in posted[0][1]["blocks"] if bl["type"] == "actions"]
        self.assertEqual([e["action_id"] for e in acts[0]["elements"]],
                         ["td_approve", "td_edit", "td_reject"])
        key, rec = saved[0]
        self.assertEqual(key, "social_reviews/7.json")
        self.assertEqual((rec["status"], rec["ts"]), ("pending", "1.2"))

    def test_dry_run_and_auto_have_no_buttons(self):
        for env in ({"SOCIAL_DRY_RUN": "true"},
                    {"SOCIAL_DRY_RUN": "false", "SOCIAL_AUTO": "1"}):
            _, posted, _ = self._run(self._b(), env)
            self.assertFalse(any(bl["type"] == "actions"
                                 for bl in posted[0][1]["blocks"]), env)

    def test_blocked_posts_note_without_record(self):
        _, posted, saved = self._run(self._b(blocks=["stale"]),
                                     {"SOCIAL_DRY_RUN": "false"})
        self.assertIn("Not posting", posted[0][1]["text"])
        self.assertEqual(saved, [])

    def test_without_bot_falls_back(self):
        with mock.patch.dict(os.environ, {"SLACK_BOT_TOKEN": ""}):
            self.assertFalse(x_post.slack_review(self._b(), None))

    def test_edited_draft_shows_editor_and_length(self):
        rec = {"run_id": "7", "kind": "flow", "date": "d", "text": "$AAA new",
               "status_line": "waiting", "run_url": "u", "edited_by": "U1"}
        txt = json.dumps(x_post.review_blocks(rec, True))
        self.assertIn("Edited by <@U1>", txt)
        self.assertIn("/280", txt)


if __name__ == "__main__":
    unittest.main()
