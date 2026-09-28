"""
callout_tracker.py — how did each day's published options callouts do?

Takes the site's own daily Top 25 (uoa_top25_daily.json — the list users
actually see) and tracks every callout at the OPTION level, from the
moment the site first flagged it:

  entry        contract price at the first minute bar at/after first_seen
  peak         highest price reached afterwards (minute highs on the flag
               day, daily highs on following sessions) through expiry or
               MAX_SESSIONS sessions, whichever comes first
  close        flag-day close, and the latest close while tracking
  peak_pct     peak / entry - 1   (best exit available, not a typical one)

Publishes docs/reports/callouts.json: per day, the top winners by peak
gain WITH the day's context line (n tracked, how many hit +50% / +100%
at peak, median peak, median latest), plus rolling aggregates. The
context line is what makes a "+400% peak" credible when shared.

Entries are frozen once set; peaks only ever update forward from real
bars. Missing data is skipped and counted, never filled.

History: past days are rebuilt from git history of uoa_top25_daily.json
(last commit of each ET date = that day's final list).

    python callout_tracker.py              # update + publish
    python callout_tracker.py --backfill   # also rebuild past days
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from statistics import median

import polygon_data as pg

_BASE = os.path.dirname(os.path.abspath(__file__))
R = lambda *p: os.path.join(_BASE, *p)
TOP_PATH = R("docs", "reports", "uoa_top25_daily.json")
LOG_PATH = R("data", "callouts_log.json")
OUT_PATH = R("docs", "reports", "callouts.json")

TOP_N = 25
MAX_SESSIONS = 10
ROLL_DAYS = 20
ET = timezone(timedelta(hours=-4))


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def occ(row):
    try:
        exp = datetime.strptime(row["expiry"], "%Y-%m-%d")
        cp = "C" if row["type"] == "call" else "P"
        k = int(round(float(row["strike"]) * 1000))
        return f"O:{row['ticker']}{exp:%y%m%d}{cp}{k:08d}"
    except Exception:
        return None


def _et_date(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(ET).date().isoformat()
    except Exception:
        return None


def day_lists_from_git():
    """{et_date: rows} — final Top list of each past day."""
    r = subprocess.run(["git", "log", "--format=%H %cI", "--",
                        "docs/reports/uoa_top25_daily.json"],
                       cwd=_BASE, capture_output=True, text=True)
    last_of_day = {}
    for line in r.stdout.splitlines():          # newest first
        sha, iso = line.split(" ", 1)
        d = _et_date(iso)
        if d and d not in last_of_day:
            last_of_day[d] = sha
    out = {}
    for d, sha in last_of_day.items():
        b = subprocess.run(["git", "show",
                            f"{sha}:docs/reports/uoa_top25_daily.json"],
                           cwd=_BASE, capture_output=True)
        try:
            doc = json.loads(b.stdout.decode("utf-8"))
        except Exception:
            continue
        day = doc.get("et_date") or d
        out[day] = doc.get("rows") or []
    return out


def _bars(ticker, span, frm, to):
    data = pg._get(f"/v2/aggs/ticker/{ticker}/range/1/{span}/{frm}/{to}",
                   {"limit": 50000, "adjusted": "true"})
    return (data or {}).get("results") or []


def track(entry, today):
    """Fill/refresh one callout record from real bars."""
    first_ms = int(datetime.fromisoformat(
        entry["first_seen"].replace("Z", "+00:00")).timestamp() * 1000)
    day = entry["date"]
    if entry.get("entry") is None:
        mins = _bars(entry["occ"], "minute", day, day)
        after = [b for b in mins if b.get("t", 0) >= first_ms]
        if not after:
            entry["status"] = "no_entry_bar"
            return
        e = after[0]
        entry["entry"] = e.get("o") or e.get("c")
        entry["entry_ts"] = e["t"]
        hi = max((b.get("h") or 0) for b in after)
        entry["peak"] = hi
        entry["peak_ts"] = max(after, key=lambda b: b.get("h") or 0)["t"]
        entry["day_close"] = after[-1].get("c")
    if entry.get("matured"):
        return
    try:
        exp = datetime.strptime(entry["expiry"], "%Y-%m-%d").date()
    except Exception:
        exp = today
    end = min(exp, today)
    frm = (datetime.strptime(day, "%Y-%m-%d").date()
           + timedelta(days=1))
    if frm <= end:
        daily = _bars(entry["occ"], "day", frm.isoformat(),
                      end.isoformat())[:MAX_SESSIONS]
        for b in daily:
            h = b.get("h") or 0
            if h > (entry.get("peak") or 0):
                entry["peak"], entry["peak_ts"] = h, b["t"]
        if daily:
            entry["last_close"] = daily[-1].get("c")
            entry["sessions"] = len(daily)
        if exp < today or len(daily) >= MAX_SESSIONS:
            entry["matured"] = True
    if entry.get("entry"):
        entry["peak_pct"] = round(100 * (entry["peak"] / entry["entry"] - 1), 1)
        lc = entry.get("last_close") or entry.get("day_close")
        if lc is not None:
            entry["last_pct"] = round(100 * (lc / entry["entry"] - 1), 1)
        entry["status"] = "tracked"


def build(backfill=False):
    log = _load(LOG_PATH, {})
    today = datetime.now(ET).date()
    days = day_lists_from_git() if backfill else {}
    cur = _load(TOP_PATH, {})
    if cur.get("et_date"):
        days[cur["et_date"]] = cur.get("rows") or []

    for day, rows in days.items():
        top = sorted((r for r in rows if r.get("rank")),
                     key=lambda r: r["rank"])[:TOP_N]
        for r in top:
            o = occ(r)
            if not o or not r.get("first_seen"):
                continue
            key = f"{day}|{o}"
            if key not in log:
                log[key] = {"date": day, "occ": o, "ticker": r["ticker"],
                            "type": r["type"], "strike": r["strike"],
                            "expiry": r["expiry"], "rank": r["rank"],
                            "first_seen": r["first_seen"],
                            "score": r.get("score"),
                            "premium": r.get("premium"),
                            "entry": None}

    pending = [e for e in log.values() if not e.get("matured")
               and e.get("status") != "no_entry_bar"]
    print(f"callouts: {len(log)} logged, refreshing {len(pending)}")
    for e in pending:
        try:
            track(e, today)
        except Exception as ex:
            e["error"] = str(ex)[:80]

    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, separators=(",", ":"))
    publish(log)


def _label(e):
    k = e["strike"]
    k = int(k) if float(k).is_integer() else k
    exp = datetime.strptime(e["expiry"], "%Y-%m-%d")
    return (f"{e['ticker']} {exp:%b} {exp.day} "
            f"${k}{'C' if e['type'] == 'call' else 'P'}")


def _day_stats(ents):
    pk = [e["peak_pct"] for e in ents]
    lp = [e["last_pct"] for e in ents if e.get("last_pct") is not None]
    return {
        "n": len(ents),
        "hit_50": sum(1 for p in pk if p >= 50),
        "hit_100": sum(1 for p in pk if p >= 100),
        "median_peak_pct": round(median(pk), 1) if pk else None,
        "median_last_pct": round(median(lp), 1) if lp else None,
    }


def publish(log):
    by_day = {}
    for e in log.values():
        if e.get("status") == "tracked" and e.get("peak_pct") is not None:
            by_day.setdefault(e["date"], []).append(e)
    days = []
    for day in sorted(by_day, reverse=True):
        ents = by_day[day]
        best = sorted(ents, key=lambda e: -e["peak_pct"])[:5]
        days.append({
            "date": day,
            **_day_stats(ents),
            "top": [{"label": _label(e), "ticker": e["ticker"],
                     "rank": e["rank"], "entry": e["entry"],
                     "peak": e["peak"], "peak_pct": e["peak_pct"],
                     "last_pct": e.get("last_pct"),
                     "flagged": e["first_seen"],
                     "peak_at": datetime.fromtimestamp(
                         e["peak_ts"] / 1000, timezone.utc).isoformat()
                     if e.get("peak_ts") else None,
                     "matured": bool(e.get("matured"))}
                    for e in best],
        })
    roll = [e for d in days[:ROLL_DAYS] for e in by_day[d["date"]]]
    out = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "universe": f"site's daily Top {TOP_N} callouts, tracked at the "
                    "option level from first flag",
        "method": "entry = first minute bar at/after the flag; peak = "
                  "highest price reached afterwards (through expiry or "
                  f"{MAX_SESSIONS} sessions); peak is the best exit "
                  "available, not a typical one",
        "rolling": {"days": min(ROLL_DAYS, len(days)), **_day_stats(roll)},
        "days": days[:60],
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    r = out["rolling"]
    print(f"published {len(days)} days · rolling {r['days']}d: "
          f"n={r['n']} hit+50={r['hit_50']} hit+100={r['hit_100']} "
          f"median peak {r['median_peak_pct']}% "
          f"median last {r['median_last_pct']}%")
    if days:
        d0 = days[0]
        print(f"latest {d0['date']}: " + " | ".join(
            f"{t['label']} +{t['peak_pct']}%" for t in d0["top"][:3]))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    build(backfill=ap.parse_args().backfill)
