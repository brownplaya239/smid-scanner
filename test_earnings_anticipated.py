"""earnings_anticipated: Nasdaq's public calendar arbitrates the report
session and stands in for the whole week when Earnings Whispers returns
nobody (its quickcaldata endpoint started 404ing 2026-09-28; Finnhub's
calendar, the previous fallback, is 401 on the free tier)."""

import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime as real_datetime
from unittest import mock

import earnings_anticipated as ea


def _nrow(sym, time, mcap, name=None, ests="7"):
    """A row as api.nasdaq.com/api/calendar/earnings returns it."""
    return {"symbol": sym, "name": name or sym + " Inc", "time": time,
            "marketCap": mcap, "noOfEsts": ests, "epsForecast": "$1.00",
            "fiscalQuarterEnding": "Sep/2026", "lastYearEPS": "$0.90",
            "lastYearRptDt": "10/14/2025"}


def _ew(ticker, release_time, total=50):
    return {"ticker": ticker, "company": ticker + " Corp", "total": total,
            "analysts": 4, "releaseTime": release_time,
            "confirmDate": "2026-09-30", "nextEPSDate": None, "qDate": None}


class _Harness(unittest.TestCase):
    """Runs ea.run() as of Tue 2026-10-06 09:00 ET with both feeds
    stubbed. `nasdaq_by_date` maps YYYY-MM-DD -> raw rows; a date that is
    missing means both the direct fetch and the worker proxy failed.
    `ew_by_path` maps EW quickcaldata paths -> rows; None means EW 404s."""

    def _run(self, nasdaq_by_date, ew_by_path=None, prior=None, caps=None):
        caps = caps or {}
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "ea.json")
            if prior is not None:
                with open(out, "w", encoding="utf-8") as f:
                    json.dump(prior, f)

            def ew(path):
                if ew_by_path is None:
                    raise urllib.error.HTTPError(path, 404, "Not Found",
                                                 {}, None)
                return ew_by_path.get(path, [])

            def nasdaq_day(date_iso):
                if date_iso not in nasdaq_by_date:
                    raise RuntimeError("nasdaq: HTTP Error 403; "
                                       "worker: HTTP Error 502")
                return nasdaq_by_date[date_iso], "nasdaq"

            with mock.patch.object(ea, "OUT_PATH", out), \
                    mock.patch.object(ea, "_fetch", ew), \
                    mock.patch.object(ea, "_nasdaq_day", nasdaq_day), \
                    mock.patch.object(ea, "_market_caps",
                                      lambda ts: {t: caps.get(t) for t in ts}), \
                    mock.patch.object(ea, "datetime") as dt:
                dt.now.return_value = real_datetime(2026, 10, 6, 9, 0,
                                                    tzinfo=ea.ET)
                ea.run()
                with open(out, encoding="utf-8") as f:
                    return json.load(f)

    @staticmethod
    def _day(doc, date):
        return next(d for d in doc["days"] if d["date"] == date)


class NasdaqFallback(_Harness):
    WEEK = {
        "2026-10-05": [],
        "2026-10-06": [
            _nrow("AAA", "time-pre-market", "$5,000,000,000"),
            _nrow("BBB", "time-after-hours", "$9,000,000,000"),
            _nrow("CCC.L", "time-pre-market", "$1,000,000,000"),   # ADR line
            _nrow("BRK/A", "time-pre-market", "$1,000,000,000"),   # share class
            _nrow("DDD", "time-not-supplied", "$8,000,000,000"),   # no session
        ],
        "2026-10-07": [_nrow("EEE", "time-after-hours", "N/A", ests="")],
        "2026-10-08": [],
        "2026-10-09": [],
    }

    def test_uses_nasdaq_when_ew_empty(self):
        doc = self._run(self.WEEK)
        self.assertEqual(doc["week_of"], "2026-10-05")
        self.assertIn("nasdaq", doc["source"])
        self.assertNotIn("finnhub", doc["source"])
        tue = self._day(doc, "2026-10-06")
        self.assertEqual([r["ticker"] for r in tue["bmo"]], ["AAA"])
        self.assertEqual([r["ticker"] for r in tue["amc"]], ["BBB"])
        row = tue["bmo"][0]
        self.assertEqual(row["source"], "nasdaq")
        self.assertEqual(row["company"], "AAA Inc")
        self.assertEqual(row["mcap"], 5e9)
        self.assertEqual(row["analysts"], 7)
        self.assertEqual(row["eps_forecast"], "$1.00")
        self.assertEqual(row["q"], "Sep/2026")
        self.assertEqual(row["next_eps"], "2026-10-06")
        self.assertIsNone(row["score"])
        self.assertTrue(row["confirmed"])
        self.assertEqual(row["release_time"], 1)
        self.assertEqual(tue["amc"][0]["release_time"], 3)
        wed = self._day(doc, "2026-10-07")
        self.assertIsNone(wed["amc"][0]["mcap"])        # "N/A" cap
        self.assertIsNone(wed["amc"][0]["analysts"])    # blank count
        self.assertEqual(doc["total"], 3)

    def test_keeps_largest_per_session(self):
        rows = [_nrow(f"A{chr(65 + i // 26)}{chr(65 + i % 26)}",
                      "time-after-hours", f"${(i + 1) * 1_000_000:,}")
                for i in range(40)]
        doc = self._run({"2026-10-08": rows})
        thu = self._day(doc, "2026-10-08")
        self.assertEqual(len(thu["amc"]), ea.FALLBACK_PER_SESSION)
        self.assertEqual(thu["amc"][0]["ticker"], rows[-1]["symbol"])  # largest
        self.assertEqual(thu["amc"][0]["mcap"], 40_000_000.0)
        self.assertEqual(thu["amc"][-1]["mcap"], 16_000_000.0)

    def test_keeps_previous_file_when_both_feeds_empty(self):
        prior = {"week_of": "2026-09-28",
                 "generated": "2026-10-02T20:43:15-04:00",
                 "source": "earningswhispers.com (Most Anticipated calendar)",
                 "total": 20, "days": []}
        self.assertEqual(self._run({}, prior=prior), prior)


class NasdaqArbitratesSession(_Harness):
    def test_moves_ew_row_to_nasdaq_hour(self):
        ew = {"/api/quickcaldata/20261006/1": [_ew("NE", 1)],
              "/api/quickcaldata/20261006/3": [_ew("AAA", 3)]}
        nq = {"2026-10-06": [_nrow("NE", "time-after-hours", "$4,000,000,000")]}
        doc = self._run(nq, ew_by_path=ew, caps={"NE": 4e9, "AAA": 1e9})
        self.assertIn("earningswhispers", doc["source"])    # EW still primary
        tue = self._day(doc, "2026-10-06")
        self.assertEqual(tue["bmo"], [])
        self.assertEqual([r["ticker"] for r in tue["amc"]], ["NE", "AAA"])
        ne = tue["amc"][0]
        self.assertEqual(ne["session_corrected"], "EW listed BMO; Nasdaq AMC")
        self.assertEqual(ne["mcap"], 4e9)          # Polygon cap for EW rows
        self.assertNotIn("source", ne)             # EW row, not a Nasdaq one
        self.assertEqual(doc["total"], 2)


class NasdaqFetch(unittest.TestCase):
    class _Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    def test_direct_then_worker_proxy(self):
        body = json.dumps({"data": {"rows": [_nrow("JPM", "time-pre-market",
                                                   "$880,976,070,000")]},
                           "status": {"rCode": 200}}).encode()
        calls = []

        def urlopen(req, timeout=0):
            calls.append(req.full_url)
            if "api.nasdaq.com" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 403, "Forbidden",
                                             {}, None)
            return self._Resp(body)

        with mock.patch("urllib.request.urlopen", urlopen):
            rows, via = ea._nasdaq_day("2026-10-13")
        self.assertEqual(via, "worker")
        self.assertEqual(rows[0]["symbol"], "JPM")
        self.assertEqual(calls, [
            ea.NASDAQ_URL + "2026-10-13",
            ea.WORKER + "/?earnings-calendar=2026-10-13"])

    def test_direct_success_skips_proxy(self):
        body = json.dumps({"data": {"rows": []},
                           "status": {"rCode": 200}}).encode()
        calls = []

        def urlopen(req, timeout=0):
            calls.append(req.full_url)
            return self._Resp(body)

        with mock.patch("urllib.request.urlopen", urlopen):
            rows, via = ea._nasdaq_day("2026-10-17")
        self.assertEqual((rows, via), ([], "nasdaq"))
        self.assertEqual(len(calls), 1)

    def test_in_body_error_counts_as_failure(self):
        body = json.dumps({"data": None,
                           "status": {"rCode": 400,
                                      "bCodeMessage": [{"errorMessage": "x"}]}}
                          ).encode()

        def urlopen(req, timeout=0):
            return self._Resp(body)

        with mock.patch("urllib.request.urlopen", urlopen):
            with self.assertRaises(RuntimeError):
                ea._nasdaq_day("2026-10-13")

    def test_raises_when_both_fail(self):
        def urlopen(req, timeout=0):
            raise urllib.error.URLError("blocked")

        with mock.patch("urllib.request.urlopen", urlopen):
            with self.assertRaises(RuntimeError) as cm:
                ea._nasdaq_day("2026-10-13")
        self.assertIn("nasdaq:", str(cm.exception))
        self.assertIn("worker:", str(cm.exception))

    def test_parse_mcap(self):
        self.assertEqual(ea._parse_mcap("$880,976,070,000"), 880976070000.0)
        self.assertIsNone(ea._parse_mcap("N/A"))
        self.assertIsNone(ea._parse_mcap(""))
        self.assertIsNone(ea._parse_mcap(None))
        self.assertIsNone(ea._parse_mcap("$0"))


if __name__ == "__main__":
    unittest.main()
