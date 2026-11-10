"""
callout_tracker.py — how did each day's published options callouts do?

Tracks every contract the site published inside its daily Top 25, at the
OPTION level, from the moment the site showed it. uoa_daily_top.py records
when each contract FIRST appeared in a published Top 25 list and publishes
all of them (even ones that later dropped out) under "callouts".

  entry        open of the first minute bar at/after the time the contract
               first appeared in a published Top 25 list
  peak         highest price reached afterwards (minute highs on the
               callout day, daily highs on following sessions) through
               expiry or MAX_SESSIONS sessions, whichever comes first
  final        close on the last session of that window (matured callouts)
  latest       most recent close so far (callouts still inside the window)
  peak_pct     peak / entry - 1   (best exit available, not a typical one)

Honesty rules:
  * Every contract that reached the Top 25 is logged. One with no trade
    after its callout moment that day has no entry price: it is counted
    and published as EXCLUDED, never silently dropped from n.
  * Matured and still-open callouts are reported separately — a final
    result and a mid-flight mark are different numbers.
  * A close is STALE when the option did not trade on the latest session
    of its window (the last trade was earlier); stale closes are flagged.
  * Days whose callout times are incomplete (tracking began mid-session)
    are not logged. Entries recorded under the old method (entry at the
    contract's first scan, not its Top 25 appearance) stay in the log for
    audit but are never published.

Entries are frozen once set; peaks only ever update from real bars.

History: --backfill rebuilds the days still in git history of
uoa_top25_daily.json (through the 2026-09-28 move to R2): every commit is
one published list, so the first commit listing a contract at rank <= 25
is its callout moment.

    python callout_tracker.py              # update + publish
    python callout_tracker.py --backfill   # also rebuild git-history days
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import UTC, date, datetime, time
from statistics import median
from zoneinfo import ZoneInfo

import exchange_calendar as EC

_BASE = os.path.dirname(os.path.abspath(__file__))
R = lambda *p: os.path.join(_BASE, *p)
TOP_PATH = R("docs", "reports", "uoa_top25_daily.json")
ARCHIVE_DIR = R("docs", "reports", "top_daily")
LOG_PATH = R("data", "callouts_log.json")
OUT_PATH = R("docs", "reports", "callouts.json")

TOP_N = 25
MAX_SESSIONS = 10
ROLL_DAYS = 20
ARCHIVE_LOOKBACK = 5  # archived sessions re-read each night (missed-run recovery)
BASIS = "top25"  # entry timed at the first published Top 25 appearance
ET = ZoneInfo("America/New_York")
DAY_DONE = time(16, 20)  # all option sessions (incl. index 16:15) are over


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


def _ms(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _bar_day(b):
    """ET calendar date of a daily bar (its `t` is the window start)."""
    return datetime.fromtimestamp(b["t"] / 1000, ET).date().isoformat()


def last_complete_session(now_et):
    """Most recent session whose option trading is over."""
    d = now_et.date()
    if EC.is_session(d) and now_et.time() >= DAY_DONE:
        return d
    return EC.previous_session(d)


def window_sessions(day, expiry):
    """Sessions after the callout day that count toward the result: the
    next MAX_SESSIONS sessions, cut at expiry."""
    out, cur = [], day
    for _ in range(MAX_SESSIONS):
        cur = EC.next_session(cur)
        if cur > expiry:
            break
        out.append(cur)
    return out


# ── where the callouts come from ────────────────────────────────────────


def callouts_of_payload(doc):
    """(day, callouts) from a published Top list, or None when the list
    cannot say when each contract was first shown in the Top 25."""
    if not doc or not doc.get("et_date") or not doc.get("callouts_complete"):
        return None
    return doc["et_date"], doc.get("callouts") or []


def recent_payloads():
    """Live list + the last few archived finals (recovers a missed night)."""
    docs = [_load(TOP_PATH, None)]
    idx = _load(os.path.join(ARCHIVE_DIR, "index.json"), {}) or {}
    for d in (idx.get("days") or [])[:ARCHIVE_LOOKBACK]:
        docs.append(_load(os.path.join(ARCHIVE_DIR, d.get("date", "") + ".json"), None))
    return [d for d in docs if d]


def callouts_from_lists(lists):
    """Rebuild callouts from a time-ordered sequence of published lists.

    lists: [(published_at_iso, doc)], oldest first. Each doc is one
    published Top list; the first list showing a contract at rank <= TOP_N
    is that contract's callout moment. Returns {et_date: [callout]}."""
    out = {}
    for published_at, doc in lists:
        day = doc.get("et_date")
        if not day or _et_date(published_at) != day:
            continue  # a list published after midnight belongs to no live day
        seen = out.setdefault(day, {})
        for r in doc.get("rows") or []:
            rank = r.get("rank")
            if not rank or rank > TOP_N:
                continue
            o = occ(r)
            if not o:
                continue
            if o not in seen:
                seen[o] = {
                    "ticker": r["ticker"],
                    "type": r["type"],
                    "strike": r["strike"],
                    "expiry": r["expiry"],
                    "first_top_at": published_at,
                    "first_top_rank": rank,
                    "best_rank": rank,
                    "score": r.get("score"),
                    "premium": r.get("premium"),
                }
            else:
                seen[o]["best_rank"] = min(seen[o]["best_rank"], rank)
    return {d: list(v.values()) for d, v in out.items()}


def day_callouts_from_git():
    """{et_date: callouts} from every commit of uoa_top25_daily.json."""
    r = subprocess.run(
        ["git", "log", "--reverse", "--format=%H %cI", "--", "docs/reports/uoa_top25_daily.json"],
        check=False,
        cwd=_BASE,
        capture_output=True,
        text=True,
    )
    lists = []
    for line in r.stdout.splitlines():  # oldest first
        sha, iso = line.split(" ", 1)
        b = subprocess.run(
            ["git", "show", f"{sha}:docs/reports/uoa_top25_daily.json"],
            check=False,
            cwd=_BASE,
            capture_output=True,
        )
        try:
            doc = json.loads(b.stdout.decode("utf-8"))
        except Exception:
            continue
        lists.append((iso, doc))
    return callouts_from_lists(lists)


def log_callouts(log, day, callouts):
    """Add each callout once. A legacy entry (old first-scan basis) for the
    same contract-day is replaced, since its entry time was wrong."""
    added = 0
    for c in callouts:
        o = occ(c)
        if not o or not c.get("first_top_at"):
            continue
        key = f"{day}|{o}"
        old = log.get(key) or {}
        if old.get("basis") == BASIS:
            if c.get("best_rank"):
                old["best_rank"] = min(old.get("best_rank") or 999, c["best_rank"])
            continue
        log[key] = {
            "date": day,
            "occ": o,
            "basis": BASIS,
            "ticker": c["ticker"],
            "type": c["type"],
            "strike": c["strike"],
            "expiry": c["expiry"],
            "callout_at": c["first_top_at"],
            "rank": c.get("first_top_rank"),
            "best_rank": c.get("best_rank"),
            "score": c.get("score"),
            "premium": c.get("premium"),
            "entry": None,
            "status": "pending",
        }
        added += 1
    return added


# ── option bars ─────────────────────────────────────────────────────────


def _bars(ticker, span, frm, to):
    """Aggregates, [] when the market has none, None when the API failed
    (so the caller retries instead of recording a false 'no trade')."""
    import polygon_data as pg

    data = pg._get(
        f"/v2/aggs/ticker/{ticker}/range/1/{span}/{frm}/{to}",
        {"limit": 50000, "adjusted": "true", "sort": "asc"},
    )
    if data is None:
        return None
    return data.get("results") or []


def track(e, end_session, bars=_bars):
    """Fill/refresh one callout from real bars. end_session = the most
    recent session whose trading is over (a date)."""
    day = date.fromisoformat(e["date"])
    if end_session < day:
        return  # callout day still trading
    if e.get("entry") is None:
        mins = bars(e["occ"], "minute", e["date"], e["date"])
        if mins is None:
            e["status"] = "pending"
            return
        t0 = _ms(e["callout_at"])
        after = sorted((b for b in mins if (b.get("t") or 0) >= t0), key=lambda b: b["t"])
        if not after:
            # the day is over and nothing traded after the callout:
            # no price anyone could have entered at — excluded, counted
            e["status"] = "no_entry"
            return
        first = after[0]
        e["entry"] = first.get("o") or first.get("c")
        e["entry_ts"] = first["t"]
        top = max(after, key=lambda b: b.get("h") or 0)
        e["day_peak"], e["day_peak_ts"] = top.get("h") or 0, top["t"]
        e["day_close"] = after[-1].get("c")
    if e.get("matured") or not e.get("entry"):
        return
    try:
        expiry = date.fromisoformat(e["expiry"])
    except Exception:
        expiry = day
    win = window_sessions(day, expiry)
    done = [s for s in win if s <= end_session]
    peak, peak_ts = e.get("day_peak") or 0, e.get("day_peak_ts")
    last_close, last_day = e.get("day_close"), e["date"]
    if done:
        daily = bars(e["occ"], "day", done[0].isoformat(), done[-1].isoformat())
        if daily is None:
            # keep the last good numbers; a first pass stays pending
            if e.get("peak_pct") is None:
                e["status"] = "pending"
            return
        daily = sorted(daily, key=lambda b: b["t"])
        for b in daily:
            if (b.get("h") or 0) > peak:
                peak, peak_ts = b["h"], b["t"]
        if daily:
            last_close, last_day = daily[-1].get("c"), _bar_day(daily[-1])
    e["peak"], e["peak_ts"] = peak, peak_ts
    e["last_close"], e["last_bar_date"] = last_close, last_day
    # the latest session that counts so far; a close from before it is stale
    through = (done[-1] if done else day).isoformat()
    e["through"] = through
    e["last_stale"] = last_day < through
    e["sessions"] = len(done)
    e["matured"] = len(done) == len(win)
    e["peak_pct"] = round(100 * (peak / e["entry"] - 1), 1)
    if last_close is not None:
        e["last_pct"] = round(100 * (last_close / e["entry"] - 1), 1)
    e["status"] = "tracked"


def build(backfill=False, now_et=None):
    log = _load(LOG_PATH, {})
    now_et = now_et or datetime.now(ET)
    end = last_complete_session(now_et)

    days = day_callouts_from_git() if backfill else {}
    for doc in recent_payloads():
        got = callouts_of_payload(doc)
        if got:
            days[got[0]] = got[1]
    added = sum(log_callouts(log, day, cos) for day, cos in days.items())

    pending = [
        e
        for e in log.values()
        if e.get("basis") == BASIS and not e.get("matured") and e.get("status") != "no_entry"
    ]
    print(f"callouts: {len(log)} logged (+{added}), refreshing {len(pending)}")
    for e in pending:
        try:
            track(e, end)
        except Exception as ex:
            e["error"] = str(ex)[:80]

    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, separators=(",", ":"))
    out = publish(log)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    _print_summary(out)


# ── publish ─────────────────────────────────────────────────────────────


def _label(e):
    k = e["strike"]
    k = int(k) if float(k).is_integer() else k
    exp = datetime.strptime(e["expiry"], "%Y-%m-%d")
    return f"{e['ticker']} {exp:%b} {exp.day} ${k}{'C' if e['type'] == 'call' else 'P'}"


def _med(vals):
    return round(median(vals), 1) if vals else None


def stats(ents):
    """Context line for a set of callouts. n = every contract that reached
    the Top 25; results are over those with an entry price."""
    tracked = [e for e in ents if e.get("status") == "tracked" and e.get("peak_pct") is not None]
    matured = [e for e in tracked if e.get("matured")]
    still = [e for e in tracked if not e.get("matured")]
    pk = [e["peak_pct"] for e in tracked]
    fin = [e["last_pct"] for e in matured if e.get("last_pct") is not None]
    lat = [e["last_pct"] for e in still if e.get("last_pct") is not None]
    return {
        "n": len(ents),
        "tracked": len(tracked),
        "excluded_no_trade": sum(1 for e in ents if e.get("status") == "no_entry"),
        "pending": sum(1 for e in ents if e.get("status") not in ("tracked", "no_entry")),
        "hit_50": sum(1 for p in pk if p >= 50),
        "hit_100": sum(1 for p in pk if p >= 100),
        "median_peak_pct": _med(pk),
        "matured": len(matured),
        "median_final_pct": _med(fin),
        "final_stale": sum(1 for e in matured if e.get("last_stale")),
        "open": len(still),
        "median_latest_pct": _med(lat),
        "latest_stale": sum(1 for e in still if e.get("last_stale")),
    }


def _iso_ms(ms):
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat() if ms else None


def publish(log):
    by_day = {}
    legacy = 0
    for e in log.values():
        if e.get("basis") != BASIS:
            legacy += 1
            continue
        by_day.setdefault(e["date"], []).append(e)
    days = []
    for day in sorted(by_day, reverse=True):
        ents = by_day[day]
        ranked = [e for e in ents if e.get("status") == "tracked" and e.get("peak_pct") is not None]
        best = sorted(ranked, key=lambda e: -e["peak_pct"])[:5]
        days.append(
            {
                "date": day,
                **stats(ents),
                "top": [
                    {
                        "label": _label(e),
                        "ticker": e["ticker"],
                        "rank": e.get("rank"),
                        "best_rank": e.get("best_rank"),
                        "entry": e["entry"],
                        "peak": e["peak"],
                        "peak_pct": e["peak_pct"],
                        "last_pct": e.get("last_pct"),
                        "last_stale": bool(e.get("last_stale")),
                        "last_bar_date": e.get("last_bar_date"),
                        "flagged": e["callout_at"],
                        "entry_at": _iso_ms(e.get("entry_ts")),
                        "peak_at": _iso_ms(e.get("peak_ts")),
                        "matured": bool(e.get("matured")),
                    }
                    for e in best
                ],
            }
        )
    roll_days = [d["date"] for d in days[:ROLL_DAYS]]
    roll = [e for d in roll_days for e in by_day[d]]
    return {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "universe": f"every contract that appeared in the site's published Top {TOP_N} "
        "during the session, tracked at the option level from that moment",
        "method": "entry = open of the first minute bar after the contract first "
        f"appeared in a published Top {TOP_N}; peak = highest price reached "
        f"afterwards (through expiry or {MAX_SESSIONS} sessions) — the best exit "
        "available, not a typical one; final = close on the last session of that "
        "window; latest = most recent close for callouts still inside it",
        "tracking_since": days[-1]["date"] if days else None,
        "legacy_unpublished": legacy,
        "rolling": {"days": len(roll_days), **stats(roll)},
        "days": days[:60],
    }


def _print_summary(out):
    r = out["rolling"]
    print(
        f"published {len(out['days'])} days · rolling {r['days']}d: "
        f"n={r['n']} tracked={r['tracked']} excluded={r['excluded_no_trade']} "
        f"hit+50={r['hit_50']} hit+100={r['hit_100']} median peak {r['median_peak_pct']}% · "
        f"matured {r['matured']} median final {r['median_final_pct']}% · "
        f"open {r['open']} median latest {r['median_latest_pct']}%"
    )
    if out["days"]:
        d0 = out["days"][0]
        print(
            f"latest {d0['date']}: "
            + " | ".join(f"{t['label']} +{t['peak_pct']}%" for t in d0["top"][:3])
        )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    build(backfill=ap.parse_args().backfill)
