"""Social post pipeline: X length counting, caption fitting, and the
publishing gate. Placeholder tickers only (AAA/BBB)."""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import x_post


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


class Cashtags(unittest.TestCase):
    """X rejects posts with more than one cashtag (HTTP 403)."""

    def test_keeps_first_cashtag_only(self):
        t = "$AAA Oct 2 $1070C: $14.6M\n$BBB Dec 18 $720P: $54.2M\n\n$CCC $DD.B"
        out = x_post.limit_cashtags(t)
        self.assertEqual(x_post.cashtags(out), ["AAA"])
        self.assertIn("BBB Dec 18 $720P: $54.2M", out)  # $ amounts kept
        self.assertIn("CCC DD.B", out)

    def test_every_caption_kind_has_at_most_one(self):
        f = {
            "day": "2026-09-28",
            "body": [
                ["AAA Oct 2 $10C", "Calls", "$9.0M", "3", "5x"],
                ["BBB Oct 2 $9P", "Puts", "$7.0M", "3", "3x"],
            ],
        }
        self.assertLessEqual(len(x_post.cashtags(x_post.limit_cashtags(x_post.flow_caption(f)))), 1)


class ApprovalWindow(unittest.TestCase):
    def test_evening_draft_can_be_approved_next_morning(self):
        # 8:15 PM ET render (00:15Z next day in EDT) -> deadline 10:15 AM
        self.assertEqual(x_post.approve_by("2026-10-08T00:15:00+00:00"), "10:15 AM Thu ET")
        self.assertGreaterEqual(x_post.BUNDLE_MAX_H, 14)

    def test_bad_timestamp_still_gives_text(self):
        self.assertIn("after render", x_post.approve_by("garbage"))


class Captions(unittest.TestCase):
    def test_flow_interleaves_calls_and_puts(self):
        f = {
            "day": "2026-09-28",
            "body": [
                ["AAA Oct 2 $10C", "Calls", "$9.0M", "3", "5x"],
                ["BBB Oct 2 $20C", "Calls", "$8.0M", "3", "4x"],
                ["AAA Oct 2 $9P", "Puts", "$7.0M", "3", "3x"],
            ],
        }
        text = x_post.flow_caption(f)
        self.assertLess(text.index("$AAA Oct 2 $9P"), text.index("$BBB Oct 2 $20C"))
        self.assertIn("not classified", text)

    def test_preview_lead_is_observation(self):
        rich = [{"implied_event": 6, "fair_move": 3}] * 3
        self.assertIn("bigger-than-usual", x_post._preview_lead(rich))
        cheap = [{"implied_event": 2, "fair_move": 4}] * 3
        self.assertIn("smaller", x_post._preview_lead(cheap))

    def test_preview_compares_event_move_with_mean_not_raw_with_median(self):
        # Raw straddle 8% vs a 4% median looks 2x rich; the report-day
        # move (6%) vs the 5.5% average reaction is fairly priced.
        n = {
            "t": "AAA",
            "date": "2026-11-18",
            "session": "AMC",
            "implied": 8.0,
            "realized_med": 4.0,
            "implied_event": 6.0,
            "fair_move": 5.5,
        }
        self.assertNotIn("bigger-than-usual", x_post._preview_lead([n] * 3))
        text = x_post.earnings_preview_caption({"names": [n]})
        self.assertIn("±6.0% priced vs ±5.5% typical", text)
        self.assertNotIn("8.0", text)
        f = {"names": [n], "date": "2026-11-18", "body": [["AAA"]]}
        self.assertEqual(x_post._log_extra("earnings_preview", f)["names"][0]["implied_event"], 6.0)

    def test_preview_review_flag_uses_event_move(self):
        n = {
            "t": "AAA",
            "date": "2099-01-02",
            "implied": 25.0,
            "implied_event": 9.0,
            "fair_move": 8.0,
        }
        f = {"names": [n], "date": x_post._now_et().date().isoformat(), "body": [["AAA"]]}
        with mock.patch.object(x_post, "_log_rows", return_value=[]):
            _, review = x_post.gate("earnings_preview", f, "t", False)
            self.assertFalse([r for r in review if "AAA" in r], review)
            n2 = dict(n, implied_event=24.0)
            _, review = x_post.gate("earnings_preview", dict(f, names=[n2]), "t", False)
            self.assertTrue([r for r in review if "AAA priced move" in r], review)


class Gate(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(x_post, "_log_rows", return_value=[])
        p.start()
        self.addCleanup(p.stop)

    def _mom(self, chg="+40%", date=None):
        return {
            "which": "stockbee",
            "count": 1,
            "new": [],
            "date": date or x_post._now_et().date().isoformat(),
            "body": [["AAA", "$10.00", chg, "6.0%", "$120M", "—"]],
        }

    def test_empty_card_blocks(self):
        blocks, _ = x_post.gate("stockbee", {"body": [], "date": "x"}, "t", False)
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
        with mock.patch.object(x_post, "_log_rows", return_value=[{"text_sha1": h}]):
            blocks, _ = x_post.gate("stockbee", self._mom(), "same", False)
        self.assertIn("identical caption already posted", blocks)

    def test_big_print_needs_review(self):
        f = {
            "day": x_post._now_et().date().isoformat(),
            "generated": None,
            "body": [["AAA Oct 2 $9P", "Puts", "$210.0M", "2", "3x"]],
        }
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

        env = {"SLACK_WEBHOOK_URL": "https://hooks.example/x", "GITHUB_RUN_ID": "7", **env}
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch("urllib.request.urlopen", fake),
        ):
            x_post.slack_preview(b, card)
        return sent.get("body")

    def _b(self, **kw):
        b = {
            "kind": "flow",
            "date": "2026-09-28",
            "text": "$AAA <x> & y",
            "alt": "alt",
            "blocks": [],
            "review": [],
        }
        b.update(kw)
        return b

    def test_waiting_preview_has_caption_image_and_link(self):
        body = self._send(
            self._b(), {"SOCIAL_DRY_RUN": "false"}, "https://api.tickerdesk.io/cards/7_a.png"
        )
        types = [bl["type"] for bl in body["blocks"]]
        self.assertIn("image", types)
        self.assertIn("waiting for your approval", body["text"])
        dump = json.dumps(body)
        self.assertIn("&lt;x&gt; &amp; y", dump)  # escaped
        self.assertIn("actions/runs/7", dump)

    def test_review_flags_listed(self):
        body = self._send(self._b(review=["AAA $210M premium"]), {"SOCIAL_DRY_RUN": "false"})
        self.assertIn("Check before approving", json.dumps(body))

    def test_blocked_says_not_posting_without_image(self):
        body = self._send(self._b(blocks=["stale data"]), {"SOCIAL_DRY_RUN": "false"})
        self.assertIn("Not posting", body["text"])
        self.assertNotIn("image", [bl["type"] for bl in body["blocks"]])

    def test_dry_run_labelled(self):
        body = self._send(self._b(), {"SOCIAL_DRY_RUN": "true"})
        self.assertIn("DRY RUN", body["text"])

    def test_no_webhook_no_call(self):
        with (
            mock.patch.dict(os.environ, {"SLACK_WEBHOOK_URL": ""}),
            mock.patch("urllib.request.urlopen") as u,
        ):
            x_post.slack_preview(self._b(), None)
        u.assert_not_called()


class SlackReview(unittest.TestCase):
    def _b(self, **kw):
        b = {
            "kind": "flow",
            "date": "2026-09-28",
            "text": "$AAA draft",
            "alt": "alt",
            "blocks": [],
            "review": [],
        }
        b.update(kw)
        return b

    def _run(self, b, env):
        posted, saved = [], []

        def api(method, body):
            posted.append((method, body))
            return {"ok": True, "channel": "C1", "ts": "1.2"}

        env = {"SLACK_BOT_TOKEN": "x", "SLACK_REVIEW_CHANNEL": "C1", "GITHUB_RUN_ID": "7", **env}
        import r2_sync

        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(x_post, "_slack_api", api),
            mock.patch.object(r2_sync, "put_json", lambda k, o: saved.append((k, o))),
        ):
            ok = x_post.slack_review(b, "https://api.tickerdesk.io/cards/7.png")
        return ok, posted, saved

    def test_pending_draft_has_three_buttons_and_record(self):
        ok, posted, saved = self._run(self._b(), {"SOCIAL_DRY_RUN": "false"})
        self.assertTrue(ok)
        acts = [bl for bl in posted[0][1]["blocks"] if bl["type"] == "actions"]
        self.assertEqual(
            [e["action_id"] for e in acts[0]["elements"]], ["td_approve", "td_edit", "td_reject"]
        )
        key, rec = saved[0]
        self.assertEqual(key, "social_reviews/7.json")
        self.assertEqual((rec["status"], rec["ts"]), ("pending", "1.2"))

    def test_dry_run_and_auto_have_no_buttons(self):
        for env in ({"SOCIAL_DRY_RUN": "true"}, {"SOCIAL_DRY_RUN": "false", "SOCIAL_AUTO": "1"}):
            _, posted, _ = self._run(self._b(), env)
            self.assertFalse(any(bl["type"] == "actions" for bl in posted[0][1]["blocks"]), env)

    def test_blocked_posts_note_without_record(self):
        _, posted, saved = self._run(self._b(blocks=["stale"]), {"SOCIAL_DRY_RUN": "false"})
        self.assertIn("Not posting", posted[0][1]["text"])
        self.assertEqual(saved, [])

    def test_without_bot_falls_back(self):
        with mock.patch.dict(os.environ, {"SLACK_BOT_TOKEN": ""}):
            self.assertFalse(x_post.slack_review(self._b(), None))

    def test_newer_draft_supersedes_older_same_kind_only(self):
        import r2_sync

        recs = {
            "social_reviews/1.json": {
                "run_id": "1",
                "kind": "flow",
                "status": "pending",
                "channel": "C1",
                "ts": "1.1",
                "date": "d",
                "text": "t",
                "status_line": "w",
                "run_url": "u",
            },
            "social_reviews/2.json": {"run_id": "2", "kind": "levels", "status": "pending"},
            "social_reviews/3.json": {"run_id": "3", "kind": "flow", "status": "rejected"},
            "social_reviews/9.json": {"run_id": "9", "kind": "flow", "status": "pending"},
        }
        cancelled, updated = [], []

        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b""

        def urlopen(req, timeout=0):
            cancelled.append(req.full_url)
            return R()

        env = {"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "o/r", "CLOUDFLARE_API_TOKEN": "c"}
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(r2_sync, "list_keys", lambda p: list(recs)),
            mock.patch.object(r2_sync, "get_json", lambda k: dict(recs[k])),
            mock.patch.object(r2_sync, "put_json", lambda k, o: updated.append((k, o["status"]))),
            mock.patch("urllib.request.urlopen", urlopen),
            mock.patch.object(x_post, "_slack_api", lambda m, b: {"ok": True}),
        ):
            x_post.supersede_older("flow", "9")
        self.assertEqual(cancelled, ["https://api.github.com/repos/o/r/actions/runs/1/cancel"])
        self.assertEqual(updated, [("social_reviews/1.json", "superseded")])

    def test_edited_draft_shows_editor_and_length(self):
        rec = {
            "run_id": "7",
            "kind": "flow",
            "date": "d",
            "text": "$AAA new",
            "status_line": "waiting",
            "run_url": "u",
            "edited_by": "U1",
        }
        txt = json.dumps(x_post.review_blocks(rec, True))
        self.assertIn("Edited by <@U1>", txt)
        self.assertIn("/280", txt)


class EarningsCards(unittest.TestCase):
    """The earnings cards use the site's comparison: the report-day priced
    move (implied_event) vs the average past move (fair_move)."""

    EDGE = {
        "names": [
            # raw 8 vs median 4 looks rich; event 6 vs mean 5.5 is fair
            {
                "t": "AAA",
                "date": "2099-01-05",
                "session": "AMC",
                "implied": 8.0,
                "realized_med": 4.0,
                "implied_event": 6.0,
                "fair_move": 5.5,
                "n_reports": 8,
            },
            {
                "t": "BBB",
                "date": "2099-01-05",
                "session": "BMO",
                "implied": 5.0,
                "realized_med": 4.5,
                "implied_event": 4.0,
                "fair_move": 2.0,
                "n_reports": 8,
            },
            # one report dominates the average: no rich/cheap call
            {
                "t": "CCC",
                "date": "2099-01-05",
                "session": "BMO",
                "implied_event": 9.0,
                "fair_move": 3.0,
                "fair_move_dominated": True,
            },
            # no event-only read: left off
            {"t": "DDD", "date": "2099-01-05", "implied": 7.0, "realized_med": 2.0},
        ]
    }

    def _cards(self):
        import social_cards

        return social_cards

    def test_preview_card_ranks_by_event_vs_mean(self):
        sc = self._cards()
        seen = {}

        def fake_table(fname, kicker, title, subtitle, cols, rows, footer, colors=None):
            seen.update(title=title, subtitle=subtitle, rows=rows, cols=cols)
            return "x.png"

        with (
            mock.patch.object(sc, "_load", return_value=self.EDGE),
            mock.patch.object(sc, "table_card", side_effect=fake_table),
        ):
            _, alt, meta = sc.earnings_preview_card("2099-01-01")
        self.assertEqual([r[0] for r in seen["rows"]], ["BBB", "AAA"])
        self.assertEqual(seen["rows"][0][2:5], ["±4.0%", "±2.0%", "2.0x"])
        self.assertEqual(seen["rows"][1][2:5], ["±6.0%", "±5.5%", "1.1x"])
        self.assertIn("average move", seen["subtitle"])
        self.assertIn("typical past move", alt)
        self.assertNotIn("_cmp", meta["names"][0])

    def test_receipt_grades_against_event_move_and_skips_legacy(self):
        import pandas as pd

        sc = self._cards()
        posted = [
            {
                "t": "AAA",
                "date": "2099-01-05",
                "session": "BMO",
                "implied_event": 6.0,
                "fair_move": 5.5,
                "implied": 8.0,
                "realized_med": 4.0,
            },
            # legacy preview: raw read only, not graded
            {
                "t": "BBB",
                "date": "2099-01-05",
                "session": "BMO",
                "implied": 8.0,
                "realized_med": 4.0,
            },
        ]
        idx = pd.to_datetime(["2099-01-02", "2099-01-05"])
        px = pd.DataFrame(
            {("Close", "AAA"): [100.0, 107.0], ("Close", "BBB"): [50.0, 60.0]}, index=idx
        )
        seen = {}

        def fake_table(fname, kicker, title, subtitle, cols, rows, footer, colors=None):
            seen["rows"] = rows
            return "x.png"

        fake_yf = mock.MagicMock()
        fake_yf.download.return_value = px
        with (
            mock.patch.object(sc, "_social_log", return_value=[]),
            mock.patch.object(sc, "_posted_previews", return_value=posted),
            mock.patch.object(sc, "_load", return_value={"names": []}),
            mock.patch.object(sc, "table_card", side_effect=fake_table),
            mock.patch.dict(sys.modules, {"yfinance": fake_yf}),
        ):
            _, _, meta = sc.receipt_card()
        self.assertEqual([r[0] for r in seen["rows"]], ["AAA"])
        # +7% vs a ±6% report-day priced move (the raw ±8% would say "inside")
        self.assertEqual(seen["rows"][0][2:6], ["±6.0%", "±5.5%", "+7.0%", "Bigger than priced"])
        self.assertEqual(meta["beat"], 1)


if __name__ == "__main__":
    unittest.main()
