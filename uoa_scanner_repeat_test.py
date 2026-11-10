"""Repeat flow counts prior trading DAYS, not our own scans."""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import uoa_scanner as us


def test_repeat_counts_distinct_prior_days(tmp_path, monkeypatch):
    et = ZoneInfo("America/New_York")
    today = datetime.now(et).replace(hour=11, minute=0, second=0, microsecond=0)
    rows = []
    # flagged on 10 scans today -> not a repeat by itself
    for k in range(10):
        rows.append(
            {"contract": "A", "flagged_at": (today + timedelta(minutes=30 * k)).isoformat()}
        )
    # B: 6 scans yesterday + 3 scans two days ago -> 2 prior days
    for d, n in ((1, 6), (2, 3)):
        for k in range(n):
            ts = today - timedelta(days=d) + timedelta(minutes=30 * k)
            rows.append({"contract": "B", "flagged_at": ts.isoformat()})
    # C: outside the 5-day lookback
    rows.append({"contract": "C", "flagged_at": (today - timedelta(days=9)).isoformat()})
    path = tmp_path / "ledger.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows))
    monkeypatch.setattr(us, "LEDGER_PATH", str(path))
    m = us._load_repeat_map(lookback_days=5)
    assert m.get("A", 0) == 0
    assert m["B"] == 2
    assert "C" not in m
