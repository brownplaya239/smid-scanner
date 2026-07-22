"""
economic_calendar.py — fetch the weekly US economic calendar from the
ForexFactory feed and emit JSON for the Today's Desk dashboard card.

The feed publishes the upcoming week's events with title, country, date,
time, impact (Low/Medium/High/Holiday), forecast and previous. "Actual"
populates as releases come out, so re-running through the day refreshes
those values without changing the schedule.

Output: docs/reports/economic_calendar.json
"""
import os
import sys
import json
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

import brief_time as BT

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ET_TZ = BT.ET
_BASE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(_BASE, "docs", "reports", "economic_calendar.json")
FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

# The feed publishes in UTC. This was previously copied through verbatim
# under a "tz": "America/New_York" label, which put every US release four
# hours late -- the 10:30 ET crude inventory print read as "2:30pm". The
# zone is now declared here, converted explicitly, and then re-derived
# from the agency release schedule so a silent upstream zone change is
# caught rather than shipped.
SOURCE_TZ = BT.UTC
SOURCE_TZ_NAME = "UTC"


OVERRIDE_PATH = os.path.join(_BASE, "docs", "reports", "event_overrides.json")


def _load_overrides():
    """Corrections, but only attributed ones. An override with no
    `authority` is another guess wearing a lab coat, so it is dropped."""
    try:
        with open(OVERRIDE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    good, dropped = [], 0
    for o in data.get("overrides") or []:
        if not (o.get("authority") and o.get("match")):
            dropped += 1
            continue
        good.append(o)
    if dropped:
        print(f"  ! {dropped} unattributed override(s) ignored")
    return good


def _match_override(overrides, raw, out):
    want_dates = {raw.get("date"), out.get("date")}
    for o in overrides:
        if o.get("date") and o["date"] not in want_dates:
            # the vendor date may still be in MM-DD-YYYY at this point
            try:
                y, m, d = o["date"].split("-")
                if "%s-%s-%s" % (m, d, y) not in want_dates:
                    continue
            except ValueError:
                continue
        if (o.get("match") or "").lower() in (raw.get("title") or "").lower():
            return o
    return None


def _text(el, tag):
    node = el.find(tag)
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def run():
    print(f"Fetching {FEED_URL} ...")
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    root = ET.fromstring(data)

    raw = []
    for e in root.findall("event"):
        if _text(e, "country") != "USD":
            continue                                         # US events only
        raw.append({
            "date":     _text(e, "date"),                    # MM-DD-YYYY
            "time":     _text(e, "time"),                    # feed-zone clock
            "title":    _text(e, "title"),
            "impact":   _text(e, "impact"),                  # Low/Medium/High/Holiday
            "forecast": _text(e, "forecast"),
            "previous": _text(e, "previous"),
            "actual":   _text(e, "actual"),
            "url":      _text(e, "url"),
        })
    print(f"  {len(raw)} US events this week")

    # Re-derive the feed's zone from agency publication times rather than
    # trusting the constant above. If the feed ever switches to ET this
    # notices instead of shifting every release by four hours.
    inferred, note = BT.infer_source_tz(raw)
    src = SOURCE_TZ
    src_name = SOURCE_TZ_NAME
    if inferred is not None:
        off = inferred.utcoffset(None)
        if off != src.utcoffset(None):
            src = inferred
            src_name = "UTC%+d" % (off.total_seconds() // 3600)
            print(f"  ! feed zone is {src_name}, not {SOURCE_TZ_NAME}: {note}")
        else:
            print(f"  zone confirmed against release schedule: {note}")
    else:
        print(f"  zone not re-derivable ({note}); using declared {src_name}")

    overrides = _load_overrides()
    now_iso = datetime.now(ET_TZ).isoformat(timespec='seconds')
    events, unconverted = [], 0
    for r in raw:
        dt = BT.aware(r["date"], r["time"], src)
        out = dict(r)
        # provenance travels with every event: what the source said, in
        # which zone, where the event happens, and what we converted it to
        out["source_time"] = r["time"]
        out["source_tz"] = src_name
        out["source_url"] = r.get("url") or ""
        out["title_authority"] = "vendor"
        out["venue_tz"] = None
        out["venue"] = None

        # the vendor's record, preserved exactly as published, so a
        # correction can always be audited against what it corrected
        out["vendor_title"] = r["title"]
        out["vendor_time"] = r["time"]
        out["vendor_tz"] = src_name
        out["vendor_url"] = r.get("url") or ""

        ov = _match_override(overrides, r, out)
        if ov:
            vz = BT.zone(ov.get("venue_tz"))
            corrected_start = ""
            if vz is not None and ov.get("local_time"):
                # the venue's own clock is the authority on when it happens
                vdt = BT.aware(ov.get("date") or r["date"],
                               ov["local_time"], vz)
                if vdt is not None:
                    dt = vdt
                    corrected_start = BT.to_et(vdt).isoformat(
                        timespec="minutes")
                    out["source_time"] = ov["local_time"]
                    out["source_tz"] = ov["venue_tz"]
            out["title"] = ov.get("title") or out["title"]
            out["title_authority"] = ov.get("authority") or "override"
            out["venue"] = ov.get("venue")
            out["venue_tz"] = ov.get("venue_tz")
            # the correction is a SEPARATE record, not an edit of the
            # vendor's: both travel so the reader (and an auditor) can see
            # what was claimed, what replaced it, and on whose authority
            out["correction"] = {
                "corrected_title": ov.get("title") or "",
                "corrected_start": corrected_start,
                "correction_source_url": ov.get("source_url") or "",
                "correction_authority": ov.get("authority") or "",
                "correction_timestamp": now_iso,
                "correction_reason": ov.get("note") or "",
            }
            # the visible link must support the CORRECTED title and time;
            # the vendor page still carries the label we replaced
            if ov.get("source_url"):
                out["source_url"] = ov["source_url"]

        if dt is None:                       # "All Day" / "Tentative"
            unconverted += 1
            out["time_et"] = ""
            out["scheduled"] = False
        else:
            et = BT.to_et(dt)
            out["date"] = et.strftime("%Y-%m-%d")
            out["time_et"] = BT.fmt_time(dt)
            out["time_utc"] = dt.astimezone(BT.UTC).strftime("%H:%MZ")
            out["starts_at"] = et.isoformat(timespec="minutes")
            out["scheduled"] = True
            a = BT.anchor_for(out["title"])
            if a:
                out["anchor_et"] = "%02d:%02d" % a
                out["on_schedule"] = (et.hour, et.minute) == a
        # a vendor category is not an event title. Flag it so nothing
        # downstream prints "President Trump Speaks" as if that were a
        # description of what is scheduled.
        out["generic_title"] = (BT.is_generic_title(out["title"])
                                and out["title_authority"] == "vendor")
        # the feed-zone clock is not a displayable value; drop it so no
        # renderer can pick it up and print it as if it were Eastern
        out.pop("time", None)
        events.append(out)

    checked, matched, _ = BT.audit_offsets(raw, src)
    print(f"  {matched}/{checked} recognised releases match their published "
          f"time; {unconverted} unscheduled")
    if checked and matched * 2 <= checked:
        raise SystemExit(
            "  ABORT: converted times still disagree with the agency "
            "schedule; refusing to write a calendar the brief would "
            "render incorrectly")

    now = datetime.now(ET_TZ)
    payload = {
        "updated":     now.isoformat(timespec="seconds"),
        "tz":          "America/New_York",   # the zone of time_et/date
        "source_tz":   src_name,             # the zone the feed publishes in
        "converted":   True,
        "schedule_check": {"checked": checked, "matched": matched},
        "events":      events,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    print(f"  Wrote economic_calendar.json")


if __name__ == "__main__":
    run()
