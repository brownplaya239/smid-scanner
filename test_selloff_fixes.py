"""Two crashes from the 2026-10-26 week. Placeholder tickers only.

- scanner.generate_pdf: Claude returned changePercent "N/A" -> TypeError.
- earnings_edge: a NaN in the payload made the JSON unparseable in
  browsers (Earnings page implied-move section blank all session)."""

import json
import math
import unittest

import earnings_edge as ee
import scanner as sc


class ScannerNumbers(unittest.TestCase):
    def test_num_coerces_strings_and_rejects_nan(self):
        self.assertEqual(sc._num("N/A"), 0.0)
        self.assertEqual(sc._num("12.5%"), 12.5)
        self.assertEqual(sc._num("$1,234.50"), 1234.5)
        self.assertEqual(sc._num(None), 0.0)
        self.assertEqual(sc._num(float("nan")), 0.0)
        self.assertEqual(sc._num(-3), -3.0)

    def test_pdf_ready_only_touches_number_fields(self):
        r = sc._pdf_ready({"ticker": "AAA", "price": "N/A",
                           "changePercent": "N/A", "theme": "N/A"})
        self.assertEqual((r["price"], r["changePercent"], r["theme"]),
                         (0.0, 0.0, "N/A"))
        f"{r['changePercent']:.1f}"          # the format that crashed
        self.assertTrue(r["changePercent"] >= 0)


class EarningsEdgeJson(unittest.TestCase):
    def test_finite_strips_nan_and_inf_everywhere(self):
        p = {"names": [{"t": "AAA", "implied": float("nan"),
                        "ratio": float("inf"), "ok": 1.5}],
             "x": (float("-inf"), 2.0)}
        out = ee._finite(p)
        s = json.dumps(out, allow_nan=False)        # must not raise
        self.assertNotIn("NaN", s)
        self.assertIsNone(out["names"][0]["implied"])
        self.assertEqual(out["names"][0]["ok"], 1.5)
        self.assertEqual(out["x"], [None, 2.0])
        self.assertFalse(any(isinstance(v, float) and math.isnan(v)
                             for v in out["names"][0].values()))


if __name__ == "__main__":
    unittest.main()
