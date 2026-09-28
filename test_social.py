"""Social post pipeline: X length counting, caption fitting, and the
publishing gate. Placeholder tickers only (AAA/BBB)."""

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


if __name__ == "__main__":
    unittest.main()
