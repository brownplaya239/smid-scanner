#!/usr/bin/env python3
"""scan_slot.py — decide what one scanner.yml run should do, and record
what it finished, so that any number of triggers (the worker's ET-clock
dispatch, GitHub's own cron, a manual run) add up to exactly one scan per
slot.

Why this exists: GitHub's scheduled runs of scanner.yml start 5-7 hours
late (the 10:07 AM ET cron has been starting around 4:20 PM ET). The
workflow routed every run after 4 PM ET to the setup builders, so from
2026-11-10 no breakout scan ran at all, while the setup builders ran four
or five times a night. The worker now dispatches the slots on the
Eastern clock; this gate makes every trigger idempotent.

Slots have explicit ids, ``2026-11-17T10:00ET``: the breakout slots are
10:00, 11:30 and 15:00 ET on trading sessions, and the setup build is
``<date>T16:00ET``. The worker passes the id as the `slot` input; a
GitHub-cron backstop run maps its cron line to the slot it was meant for
(CRON_SLOTS); a run with neither takes the slot from the clock.

The record of what ran is a COMPLETION RECORD, not the archived PDFs: a
scan that finds no candidates archives nothing, and a PDF's timestamp is
when it finished, not which slot started it (a 10:00 run that finished at
11:35 used to make the 11:30 run skip). After a universe's scan succeeds
and publishes, `record` writes it to the R2 object
``scan_runs/<date>.json`` (the worker reads the same object for its
missing-report alert):

    {"date": "2026-11-17",
     "slots": {"2026-11-17T10:00ET": {"smid": {"finished": ..., "reports": [...]},
                                      "iwm":  {...}}}}

scanner.yml runs one job at a time (concurrency group `scanner`), so the
read-modify-write never races.

Rules (all times Eastern):
  * a breakout run scans each universe with no completion record for its
    slot. A slot that has not started yet, before 10:00, at or after the
    close, a holiday, or a slot after a half day's 13:00 close: skip. A
    run that starts after a LATER slot has begun (it sat in the queue)
    serves that later slot instead, so one late run does not double up
    with the next slot's own run.
  * a slot from an earlier day is never run late.
  * the setup builders run once per session, from 16:00, for each universe
    with no completion record (or setup PDF) for today.

    python scan_slot.py plan --mode auto|breakout|setup [--slot ID] [--cron LINE] [--force]
    python scan_slot.py record --slot ID --universe smid|iwm

`plan` writes kind / slot / run_smid / run_iwm / reason to $GITHUB_OUTPUT
when set, and snapshots the archived PDFs so `record` can name the
reports this run added.
"""

import argparse
import glob
import json
import os
import re
import sys
from datetime import UTC, datetime, time, timedelta

import brief_time as BT
import exchange_calendar as XC

BREAKOUT_SLOTS = (time(10, 0), time(11, 30), time(15, 0))
SETUP_FROM = time(16, 0)
_BASE = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(_BASE, "docs", "reports")
SNAPSHOT = os.path.join(_BASE, ".scan_slot_before.json")
UNIVERSES = ("smid", "iwm")
PREFIX = {
    "breakout": {"smid": "smid_scanner_", "iwm": "iwm_scanner_"},
    "setup": {"smid": "smid_setup_", "iwm": "iwm_setup_"},
}
# scanner.yml's backstop cron lines -> the slot each one stands in for.
# Keep in step with the `schedule:` block in .github/workflows/scanner.yml.
CRON_SLOTS = {
    "7 15 * * 1-5": time(10, 0),
    "37 16 * * 1-5": time(11, 30),
    "7 20 * * 1-5": time(15, 0),
    "17 21 * * 1-5": SETUP_FROM,
}
_STAMP = re.compile(r"_(\d{4}-\d{2}-\d{2})_(\d{4})\.pdf$")
_SLOT_ID = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})ET$")


def slot_id(day, t):
    """'2026-11-17T10:00ET' for date `day` and slot time `t`."""
    return "%sT%sET" % (day.isoformat(), t.strftime("%H:%M"))


def parse_slot(sid):
    """(date, time) of a slot id; ValueError if it is not one."""
    m = _SLOT_ID.match(sid or "")
    if not m:
        raise ValueError("bad slot id %r (want YYYY-MM-DDTHH:MMET)" % sid)
    t = time(int(m.group(2)), int(m.group(3)))
    if t not in BREAKOUT_SLOTS and t != SETUP_FROM:
        raise ValueError("slot %r is not a breakout slot or the 16:00 setup build" % sid)
    return datetime.strptime(m.group(1), "%Y-%m-%d").date(), t


def slot_kind(t):
    return "setup" if t == SETUP_FROM else "breakout"


def slot_for_cron(cron, now_utc):
    """The slot id a GitHub backstop cron run stands in for. GitHub starts
    these runs hours late, so find when the cron line last fired (a
    weekday in UTC, at or before now) and take that moment's ET date."""
    t = CRON_SLOTS.get((cron or "").strip())
    if t is None:
        return None
    mi, hr = (int(x) for x in cron.split()[:2])
    fired = now_utc.astimezone(UTC).replace(hour=hr, minute=mi, second=0, microsecond=0)
    if fired > now_utc:
        fired -= timedelta(days=1)
    while fired.weekday() >= 5:  # "1-5": the cron never fires Sat/Sun UTC
        fired -= timedelta(days=1)
    return slot_id(fired.astimezone(BT.ET).date(), t)


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


def _session(day):
    try:
        session = XC.is_session(day)
        return session, (XC.close_time(day) if session else None)
    except XC.CalendarRangeError:
        # a year the holiday table does not know: weekdays only, rather
        # than silently never scanning again
        return day.weekday() < 5, time(16, 0)


def plan(now_et, mode, records=None, files=(), force=False, slot=None):
    """What this run should do. Pure.

    now_et   aware datetime in Eastern time
    mode     auto | breakout | setup (ignored when `slot` names one)
    records  {slot_id: {universe: record}} completion records for today
    files    archived PDF names (setup fallback only)
    slot     the slot id this run was started for, or None for the clock
    Returns {kind, slot, run: {smid, iwm}, reason}."""
    records = records or {}
    skip = {"kind": None, "slot": None, "run": {u: False for u in UNIVERSES}}
    day = now_et.date()
    today = day.isoformat()

    want = None
    if slot:
        try:
            sday, want = parse_slot(slot)
        except ValueError as e:
            return dict(skip, reason=str(e))
        if sday != day:
            return dict(skip, reason="slot %s is not today (%s); not run late" % (slot, today))
        kind = slot_kind(want)
    elif mode == "auto":
        kind = "setup" if now_et.time() >= SETUP_FROM else "breakout"
    else:
        kind = mode
    if kind not in PREFIX:
        return dict(skip, reason="unknown mode %r" % mode)

    session, close = _session(day)
    note = ""
    if kind == "breakout":
        cur = current_slot(now_et)
        if want is not None and (cur is None or want > cur):
            return dict(skip, reason="slot %s has not started yet" % slot)
        if cur is None:
            return dict(skip, reason="before the first breakout slot (10:00 ET)")
        if want is not None and want < cur:
            note = " (started for %s, which a later slot has replaced)" % slot
        sid = slot_id(day, cur)
    else:
        sid = slot_id(day, SETUP_FROM)
    if force:
        return {
            "kind": kind,
            "slot": sid,
            "run": {u: True for u in UNIVERSES},
            "reason": "forced run (%s)" % sid,
        }
    if not session:
        return dict(skip, reason="%s is not a trading session" % today)

    done = records.get(sid) or {}
    if kind == "breakout":
        if now_et.time() >= close:
            return dict(
                skip, reason="market closed at %s ET; no breakout scan" % close.strftime("%H:%M")
            )
        run = {u: u not in done for u in UNIVERSES}
    else:
        if now_et.time() < SETUP_FROM:
            return dict(skip, reason="setup builders wait for 16:00 ET")
        # a setup PDF dated today also counts (it can only be today's build)
        run = {
            u: u not in done and not any(d == today for d, _ in _stamps(files, PREFIX["setup"][u]))
            for u in UNIVERSES
        }
    if not any(run.values()):
        return dict(skip, slot=sid, reason="%s already ran (completion recorded)%s" % (sid, note))
    todo = " + ".join(u.upper() for u in UNIVERSES if run[u])
    return {"kind": kind, "slot": sid, "run": run, "reason": "%s: run %s%s" % (sid, todo, note)}


def add_record(doc, sid, universe, reports, finished, run_id=""):
    """`doc` (the day's scan_runs object, or None) with one universe of one
    slot marked done. Pure; empty `reports` is a valid, successful run."""
    day = parse_slot(sid)[0].isoformat()
    doc = dict(doc or {})
    doc.setdefault("date", day)
    slots = dict(doc.get("slots") or {})
    entry = dict(slots.get(sid) or {})
    entry[universe] = {
        "kind": slot_kind(parse_slot(sid)[1]),
        "finished": finished,
        "reports": sorted(reports),
        "run_id": run_id,
    }
    slots[sid] = entry
    doc["slots"] = slots
    return doc


def records_key(day):
    return "scan_runs/%s.json" % day.isoformat()


def _r2():
    sys.path.insert(0, os.path.join(_BASE, "scripts"))
    import r2_sync

    return r2_sync


def load_records(day):
    """Today's completion records from R2, {} when none (or unreadable:
    a duplicate scan beats a missing one)."""
    if not os.environ.get("CLOUDFLARE_API_TOKEN"):
        print("[scan_slot] no CLOUDFLARE_API_TOKEN: no completion records read")
        return {}
    try:
        doc = _r2().get_json(records_key(day)) or {}
    except Exception as e:
        print("::warning::scan_slot: could not read completion records (%s)" % e)
        return {}
    return doc.get("slots") or {}


def _pdfs():
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(REPORTS_DIR, "*.pdf")))


def cmd_plan(a):
    now = datetime.now(BT.ET)
    sid = a.slot or (slot_for_cron(a.cron, datetime.now(UTC)) if a.cron else None)
    files = _pdfs()
    p = plan(now, a.mode, load_records(now.date()), files, force=a.force, slot=sid)
    print(
        "[scan_slot] %s ET, mode=%s, slot=%s -> %s"
        % (now.strftime("%Y-%m-%d %H:%M"), a.mode, sid or "(clock)", p["reason"])
    )
    with open(SNAPSHOT, "w", encoding="utf-8") as fh:
        json.dump(files, fh)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write("kind=%s\n" % (p["kind"] or "none"))
            fh.write("slot=%s\n" % (p["slot"] or ""))
            fh.write("run_smid=%s\n" % str(p["run"]["smid"]).lower())
            fh.write("run_iwm=%s\n" % str(p["run"]["iwm"]).lower())
            fh.write("reason=%s\n" % p["reason"])


def cmd_record(a):
    _, t = parse_slot(a.slot)
    try:
        with open(SNAPSHOT, encoding="utf-8") as fh:
            before = set(json.load(fh))
    except FileNotFoundError:
        before = set()
    prefix = PREFIX[slot_kind(t)][a.universe]
    new = [f for f in _pdfs() if f.startswith(prefix) and f not in before]
    r2 = _r2()
    key = records_key(parse_slot(a.slot)[0])
    doc = add_record(
        r2.get_json(key),
        a.slot,
        a.universe,
        new,
        datetime.now(BT.ET).isoformat(timespec="seconds"),
        os.environ.get("GITHUB_RUN_ID", ""),
    )
    r2.put_json(key, doc)
    print(
        "[scan_slot] recorded %s %s done (%s)"
        % (a.slot, a.universe.upper(), ", ".join(new) or "no candidates, no report")
    )


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--mode", default="auto", choices=("auto", "breakout", "setup"))
    p.add_argument("--slot", default="", help="slot id, e.g. 2026-11-17T10:00ET")
    p.add_argument("--cron", default="", help="github.event.schedule of a backstop run")
    p.add_argument("--force", action="store_true")
    r = sub.add_parser("record")
    r.add_argument("--slot", required=True)
    r.add_argument("--universe", required=True, choices=UNIVERSES)
    a = ap.parse_args(argv)
    (cmd_plan if a.cmd == "plan" else cmd_record)(a)


if __name__ == "__main__":
    main()
