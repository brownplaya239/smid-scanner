"""earnings_anticipated: Finnhub fallback when Earnings Whispers returns
nobody (its quickcaldata endpoint started 404ing 2026-09-28)."""

import json
import os
import tempfile
import unittest
import urllib.error
from datetime import date
from unittest import mock

import earnings_anticipated as ea


def _row(t, d, hour):
    return {"symbol": t, "date": d, "hour": hour, "quarter": 3}


class FinnhubFallback(unittest.TestCase):
    def _run(self, fh_rows, caps):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "ea.json")

            def ew_404(path):
                raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)

            class Resp:
                def __init__(self, body):
                    self.body = body

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return self.body

            def urlopen(url, timeout=0):
                return Resp(json.dumps({"earningsCalendar": fh_rows}).encode())

            class D(date):
                @classmethod
                def today(cls):
                    return cls(2026, 10, 6)

            with mock.patch.object(ea, "OUT_PATH", out), \
                    mock.patch.object(ea, "_fetch", ew_404), \
                    mock.patch.object(ea, "_market_caps",
                                      lambda ts: {t: caps.get(t) for t in ts}), \
                    mock.patch.dict(os.environ, {"FINNHUB_API_KEY": "k"}), \
                    mock.patch("urllib.request.urlopen", urlopen), \
                    mock.patch.object(ea, "datetime") as dt:
                from datetime import datetime as real
                dt.now.return_value = real(2026, 10, 6, 9, 0, tzinfo=ea.ET)
                dt.strptime = real.strptime
                ea.run()
                with open(out, encoding="utf-8") as f:
                    return json.load(f)

    def test_uses_finnhub_when_ew_empty(self):
        rows = [_row("AAA", "2026-10-06", "bmo"),
                _row("BBB", "2026-10-06", "amc"),
                _row("CCC.L", "2026-10-06", "bmo"),     # foreign line: dropped
                _row("DDD", "2026-10-07", "dmh")]       # no session: dropped
        doc = self._run(rows, {"AAA": 5e9, "BBB": 9e9})
        self.assertEqual(doc["week_of"], "2026-10-05")
        self.assertIn("finnhub", doc["source"])
        tue = next(d for d in doc["days"] if d["date"] == "2026-10-06")
        self.assertEqual([r["ticker"] for r in tue["bmo"]], ["AAA"])
        self.assertEqual([r["ticker"] for r in tue["amc"]], ["BBB"])
        self.assertIsNone(tue["bmo"][0]["score"])
        self.assertEqual(doc["total"], 2)

    def test_keeps_largest_per_session(self):
        rows = [_row(f"A{chr(65 + i // 26)}{chr(65 + i % 26)}", "2026-10-08",
                     "amc") for i in range(40)]
        caps = {r["symbol"]: float(i) for i, r in enumerate(rows)}
        doc = self._run(rows, caps)
        thu = next(d for d in doc["days"] if d["date"] == "2026-10-08")
        self.assertEqual(len(thu["amc"]), ea.FALLBACK_PER_SESSION)
        self.assertEqual(thu["amc"][0]["ticker"], rows[-1]["symbol"])  # largest


if __name__ == "__main__":
    unittest.main()
