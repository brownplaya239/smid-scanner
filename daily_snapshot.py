"""
daily_snapshot.py — Persists today's per-ticker signal state into
public.signal_snapshots so the "What's New Since Last Visit" widget
can compute true day-over-day diffs (not just current-state surfacing).

Schema (from Phase 3 SQL):
  signal_snapshots
    snapshot_date date         — YYYY-MM-DD (one row per ticker per day)
    ticker text                — uppercase
    trend_grade text           — A+ / A / ... / G
    flow_bias text             — bullish / bearish / mixed / null
    flow_score numeric         — top contract premium / dollar score
    theme text                 — primary theme tag (first in themes[])
    earnings_date date         — next earnings date for this ticker
    news_sentiment text        — positive / negative / neutral / null
    appeared_in_modules jsonb  — {top_flow:true, qm:true, ...}
    unique (snapshot_date, ticker)

Runs daily via GH Actions (push to master + cron). Idempotent on
(snapshot_date, ticker) — re-running same-day is a no-op.

Required env vars:
  SUPABASE_URL
  SUPABASE_SERVICE_KEY     bypasses RLS, only present in CI
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

import pytz

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ET = pytz.timezone("America/New_York")
_BASE = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(_BASE, "docs", "reports")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


def _safe_load(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _sb_post(path: str, body: Any, prefer: str = "") -> Any:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    data = json.dumps(body).encode("utf-8")
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            txt = r.read().decode("utf-8")
            return json.loads(txt) if txt else None
    except urllib.error.HTTPError as e:
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8")
        except Exception:
            pass
        raise RuntimeError(f"Supabase POST {path} HTTP {e.code}: {body_txt}") from e


def build_snapshots() -> list[dict]:
    """Synthesize one snapshot row per ticker that has ANY signal today."""
    today = datetime.now(ET).date().isoformat()

    swing = _safe_load(os.path.join(REPORTS_DIR, "swing_report.json"))
    uoa = _safe_load(os.path.join(REPORTS_DIR, "uoa_latest.json"))
    earnings = _safe_load(os.path.join(REPORTS_DIR, "earnings_anticipated.json"))
    qm = _safe_load(os.path.join(REPORTS_DIR, "momentum_qm.json"))
    sb = _safe_load(os.path.join(REPORTS_DIR, "momentum_stockbee.json"))

    rows: dict[str, dict] = {}

    # ── Swing grades ──
    if swing and swing.get("runs"):
        run = swing["runs"][-1]
        for grade, names in (run.get("grades") or {}).items():
            for n in names:
                t = n.get("t")
                if not t:
                    continue
                rows.setdefault(t, {
                    "snapshot_date": today, "ticker": t,
                    "trend_grade": None, "flow_bias": None,
                    "flow_score": None, "theme": None,
                    "earnings_date": None, "news_sentiment": None,
                    "appeared_in_modules": {},
                })
                rows[t]["trend_grade"] = grade
                themes = n.get("th") or []
                if themes:
                    rows[t]["theme"] = themes[0]

    # ── UOA flow (top contract per ticker) ──
    if uoa and uoa.get("rows"):
        by_tk: dict[str, dict] = {}
        for r in uoa["rows"]:
            t = r.get("ticker")
            if not t:
                continue
            prem = r.get("premium") or 0
            if t not in by_tk or prem > (by_tk[t].get("premium") or 0):
                by_tk[t] = r
        for t, r in by_tk.items():
            rows.setdefault(t, {
                "snapshot_date": today, "ticker": t,
                "trend_grade": None, "flow_bias": None,
                "flow_score": None, "theme": None,
                "earnings_date": None, "news_sentiment": None,
                "appeared_in_modules": {},
            })
            rows[t]["flow_bias"] = r.get("direction")
            rows[t]["flow_score"] = r.get("premium")
            rows[t]["appeared_in_modules"]["top_flow"] = True

    # ── Earnings (next earnings date per ticker, this week) ──
    if earnings and earnings.get("days"):
        for day in earnings["days"]:
            d = day.get("date")
            for slot in ("bmo", "amc"):
                for c in (day.get(slot) or []):
                    t = c.get("ticker")
                    if not t:
                        continue
                    rows.setdefault(t, {
                        "snapshot_date": today, "ticker": t,
                        "trend_grade": None, "flow_bias": None,
                        "flow_score": None, "theme": None,
                        "earnings_date": None, "news_sentiment": None,
                        "appeared_in_modules": {},
                    })
                    # First (earliest) earnings date wins
                    if rows[t]["earnings_date"] is None:
                        rows[t]["earnings_date"] = d

    # ── QM Monthly appearance flag ──
    if qm and qm.get("runs") and qm["runs"]:
        last = qm["runs"][-1]
        for r in (last.get("rows") or []):
            t = r.get("ticker")
            if not t:
                continue
            rows.setdefault(t, {
                "snapshot_date": today, "ticker": t,
                "trend_grade": None, "flow_bias": None,
                "flow_score": None, "theme": None,
                "earnings_date": None, "news_sentiment": None,
                "appeared_in_modules": {},
            })
            rows[t]["appeared_in_modules"]["qm"] = True

    # ── Stockbee appearance flag ──
    if sb and sb.get("runs") and sb["runs"]:
        last = sb["runs"][-1]
        for r in (last.get("rows") or []):
            t = r.get("ticker")
            if not t:
                continue
            rows.setdefault(t, {
                "snapshot_date": today, "ticker": t,
                "trend_grade": None, "flow_bias": None,
                "flow_score": None, "theme": None,
                "earnings_date": None, "news_sentiment": None,
                "appeared_in_modules": {},
            })
            rows[t]["appeared_in_modules"]["stockbee"] = True

    return list(rows.values())


def upsert_snapshots(rows: list[dict]) -> None:
    """Push snapshot rows to Supabase. Uses upsert ON CONFLICT for the
    (snapshot_date, ticker) unique index — re-running same-day is safe."""
    if not rows:
        print("  No snapshot rows to write.")
        return
    print(f"  Writing {len(rows)} snapshots to Supabase...")
    # Chunk by 500 rows to keep PostgREST happy with payload size
    chunk = 500
    for i in range(0, len(rows), chunk):
        batch = rows[i:i + chunk]
        try:
            _sb_post(
                "signal_snapshots",
                batch,
                prefer="resolution=merge-duplicates,return=minimal",
            )
            print(f"    chunk {i//chunk + 1}: {len(batch)} rows OK")
        except Exception as e:
            print(f"    chunk {i//chunk + 1}: FAILED {e}")


def main():
    today = datetime.now(ET).date()
    if today.weekday() >= 5:
        print(f"Weekend ({today.isoformat()}) — skipping snapshot run.")
        return
    print(f"daily_snapshot.py · {today.isoformat()}")
    rows = build_snapshots()
    print(f"  Built {len(rows)} ticker snapshots from "
          "swing+uoa+earnings+qm+stockbee")
    upsert_snapshots(rows)
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print(f"Fatal: {type(e).__name__}: {e}")
        traceback.print_exc()
