#!/usr/bin/env python3
"""scan_slot.py — decide what one scanner.yml run should do, so that any
number of triggers (the worker's ET-clock dispatch, GitHub's own cron, the
4:45 PM EOD dispatch, a manual run) add up to exactly one report per slot.

Why this exists: GitHub's scheduled runs of scanner.yml start 5-7 hours
late (the 10:07 AM ET cron has been starting around 4:20 PM ET). The
workflow routed every run after 4 PM ET to the setup builders, so from
2026-11-10 no breakout scan ran at all, while the setup builders ran four
or five times a night. The worker now dispatches the breakout slots on the
Eastern clock; this gate makes every trigger idempotent.

Rules (all times Eastern):
  * breakout slots are 10:00, 11:30 and 15:00 on trading sessions. A run
    belongs to the latest slot that has started; it scans each universe
    (SMID, IWM) that has no breakout PDF stamped at or after that slot's
    start today. Before 10:00, at or after the close, on a holiday, or for
    a slot after a half day's 13:00 close, it skips.
  * the setup builders run once per session, from 16:00 on, for each
    universe that has no setup PDF dated today.
  * mode "auto" (cron and EOD dispatches) picks by the clock: before 16:00
    breakouts, from 16:00 the setup builders.

The archived PDFs in docs/reports/ (committed by publish_reports.sh) are
the record of what already ran; their names carry the ET date and time:
smid_scanner_2026-11-16_1003.pdf.

    python scan_slot.py --mode auto|breakout|setup [--force]

writes run_smid / run_iwm / kind / reason to $GITHUB_OUTPUT when set.
"""

import argparse
import glob
import os
import re
from datetime import datetime, time

import brief_time as BT
import exchange_calendar as XC

BREAKOUT_SLOTS = (time(10, 0), time(11, 30), time(15, 0))
SETUP_FROM = time(16, 0)
REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "reports")
PREFIX = {
    "breakout": {"smid": "smid_scanner_", "iwm": "iwm_scanner_"},
    "setup": {"smid": "smid_setup_", "iwm": "iwm_setup_"},
}
_STAMP = re.compile(r"_(\d{4}-\d{2}-\d{2})_(\d{4})\.pdf$")


def _stamps(files, prefix):
    """(date, HHMM) of every archived PDF with this prefix."""
    out = []
    for fn in files:
        fn = os.path.basename(fn)
        if not fn.startswith(prefix):
            continue
        m = _STAMP.search(fn)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def current_slot(now_et):
    """The latest breakout slot that has started today, or None."""
    t = now_et.time()
    started = [s for s in BREAKOUT_SLOTS if s <= t]
    return started[-1] if started else None


def plan(now_et, mode, files, force=False):
    """What this run should do. Pure: `files` is the list of archived PDF
    names, `now_et` an aware or naive datetime in Eastern time."""
    skip = {"kind": None, "run": {"smid": False, "iwm": False}}
    day = now_et.date()
    try:
        session = XC.is_session(day)
        close = XC.close_time(day) if session else None
    except XC.CalendarRangeError:
        # a year the holiday table does not know: weekdays only, rather
        # than silently never scanning again
        session = day.weekday() < 5
        close = time(16, 0)
    kind = mode
    if mode == "auto":
        kind = "setup" if now_et.time() >= SETUP_FROM else "breakout"
    if kind not in PREFIX:
        return dict(skip, reason="unknown mode %r" % mode)
    if force:
        return {"kind": kind, "run": {"smid": True, "iwm": True}, "reason": "forced run"}
    if not session:
        return dict(skip, reason="%s is not a trading session" % day.isoformat())

    today = day.isoformat()
    if kind == "breakout":
        slot = current_slot(now_et)
        if slot is None:
            return dict(skip, reason="before the first breakout slot (10:00 ET)")
        if now_et.time() >= close:
            return dict(
                skip, reason="market closed at %s ET; no breakout scan" % close.strftime("%H:%M")
            )
        since = slot.strftime("%H%M")
        run = {
            u: not any(d == today and hm >= since for d, hm in _stamps(files, p))
            for u, p in PREFIX["breakout"].items()
        }
        label = "%s ET slot" % slot.strftime("%H:%M")
    else:
        if now_et.time() < SETUP_FROM:
            return dict(skip, reason="setup builders wait for 16:00 ET")
        run = {
            u: not any(d == today for d, _ in _stamps(files, p)) for u, p in PREFIX["setup"].items()
        }
        label = "today's setup build"
    if not any(run.values()):
        return dict(skip, reason="%s already ran (report archived)" % label)
    todo = " + ".join(u.upper() for u, r in run.items() if r)
    return {"kind": kind, "run": run, "reason": "%s: run %s" % (label, todo)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="auto", choices=("auto", "breakout", "setup"))
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    now = datetime.now(BT.ET)
    files = glob.glob(os.path.join(REPORTS_DIR, "*.pdf"))
    p = plan(now, a.mode, files, force=a.force)
    print(
        "[scan_slot] %s ET, mode=%s -> %s" % (now.strftime("%Y-%m-%d %H:%M"), a.mode, p["reason"])
    )
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write("kind=%s\n" % (p["kind"] or "none"))
            fh.write("run_smid=%s\n" % str(p["run"]["smid"]).lower())
            fh.write("run_iwm=%s\n" % str(p["run"]["iwm"]).lower())
            fh.write("reason=%s\n" % p["reason"])


if __name__ == "__main__":
    main()
