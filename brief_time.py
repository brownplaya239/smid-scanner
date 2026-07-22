#!/usr/bin/env python3
"""brief_time.py — one place where a timestamp becomes a displayed time.

The bug this module exists to prevent: the economic calendar feed publishes
in UTC, the writer stamped the payload ``"tz": "America/New_York"``, and
nothing converted anything. Every US release rendered four hours late for
as long as the file has existed -- the 10:30 a.m. ET crude inventory print
appeared as "2:30pm". Relabelling is not conversion, and a naive datetime
carries no evidence of which of the two happened.

So: every timestamp is parsed WITH its source zone, converted explicitly,
and rendered with the zone visible. Naive input is rejected rather than
assumed, because assuming is precisely the failure being fixed.

The second half of the module is the audit that would have caught it. US
macro releases are published on fixed clock times set by the issuing
agency -- initial claims at 08:30 ET, EIA petroleum at 10:30 ET -- so a
feed's times can be checked against the agency schedule without trusting
the feed. When most recognised events sit at a constant non-zero offset
from their published time, the feed is in a different zone than it claims
and the brief must not send.

    python brief_time.py --self-test
"""

import re
import sys
from datetime import datetime, timedelta, timezone

try:                                    # 3.9+
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    UTC = timezone.utc
except ImportError:                     # pragma: no cover - old runners
    import pytz
    ET = pytz.timezone("America/New_York")
    UTC = pytz.utc

ET_LABEL = "ET"


class TimeSafetyError(ValueError):
    """Raised when a timestamp cannot be placed on the clock safely."""


# ── parsing ─────────────────────────────────────────────────────────────

_CLOCK = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\s*$", re.I)
_H24 = re.compile(r"^\s*(\d{1,2}):(\d{2})(?::\d{2})?\s*$")


def parse_clock(s):
    """'2:30pm' / '14:30' / '9am' -> (hour, minute). None when the string
    is a status word ('All Day', 'Tentative') rather than a time."""
    if not s:
        return None
    m = _CLOCK.match(s)
    if m:
        h = int(m.group(1)) % 12
        if m.group(3).lower() == "p":
            h += 12
        return h, int(m.group(2) or 0)
    m = _H24.match(s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return h, mi
    return None


def parse_date(s):
    """MM-DD-YYYY or YYYY-MM-DD -> (y, m, d)."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%m-%d-%Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            d = datetime.strptime(s, fmt)
            return d.year, d.month, d.day
        except ValueError:
            continue
    return None


def aware(date_s, clock_s, source_tz):
    """Build an aware datetime from feed strings and the zone the FEED is
    published in. source_tz is mandatory and must be a tzinfo -- passing
    None is the bug, so it raises instead of defaulting to anything."""
    if source_tz is None:
        raise TimeSafetyError(
            "source timezone is required; a naive timestamp cannot be "
            "converted, only relabelled")
    d, c = parse_date(date_s), parse_clock(clock_s)
    if not d or not c:
        return None
    naive = datetime(d[0], d[1], d[2], c[0], c[1])
    if hasattr(source_tz, "localize"):          # pytz
        return source_tz.localize(naive)
    return naive.replace(tzinfo=source_tz)


def to_et(dt):
    """Convert an aware datetime to Eastern. Rejects naive input."""
    if dt is None:
        return None
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise TimeSafetyError(
            "refusing to convert a naive datetime -- its zone is unknown, "
            "so the result would be a relabel, not a conversion")
    return dt.astimezone(ET)


def parse_iso(s):
    """ISO-8601 -> aware datetime. 'Z' is UTC. An ISO string with no
    offset is naive and is returned as-is for the caller to reject."""
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# ── rendering ───────────────────────────────────────────────────────────

def fmt_time(dt, ampm=True):
    """'10:30 a.m. ET'. The zone is part of the string, never implied."""
    et = to_et(dt)
    if et is None:
        return ""
    h = et.hour % 12 or 12
    if not ampm:
        return "%d:%02d %s" % (h, et.minute, ET_LABEL)
    suffix = "a.m." if et.hour < 12 else "p.m."
    return "%d:%02d %s %s" % (h, et.minute, suffix, ET_LABEL)


def fmt_stamp(dt):
    """'2026-07-22 06:39 ET' for as-of lines."""
    et = to_et(dt)
    return et.strftime("%Y-%m-%d %H:%M ") + ET_LABEL if et else ""


def fmt_day_time(dt):
    """'Wed 10:30 a.m. ET'."""
    et = to_et(dt)
    return (et.strftime("%a ") + fmt_time(et)) if et else ""


# ── primary-source anchors ──────────────────────────────────────────────
# Publication times set by the issuing agency, not by any data vendor.
# BLS/Census/BEA macro releases are 08:30 ET by long-standing policy; EIA
# petroleum status is 10:30 ET Wednesday and natural gas 10:30 ET Thursday;
# S&P Global flash PMIs are 09:45 ET; Conference Board and Census housing
# are 10:00 ET; the FOMC statement is 14:00 ET.
#
# Matched loosely on purpose -- a feed that renames "Unemployment Claims"
# to "Initial Jobless Claims" should still be checked, and an event that
# matches nothing is simply not asserted about.
ANCHORS = (
    # ADP sits above the payrolls pattern on purpose: it is matched first,
    # so "ADP Weekly Employment Change" cannot be claimed by the generic
    # "employment change" rule and mis-audited against the 08:30 anchor.
    (r"adp .*(weekly|employment)", (8, 15)),
    (r"unemployment claims|jobless claims", (8, 30)),
    (r"\bcpi\b|consumer price index", (8, 30)),
    (r"\bppi\b|producer price index", (8, 30)),
    (r"non-?farm|nfp\b|employment change", (8, 30)),
    (r"retail sales", (8, 30)),
    (r"\bgdp\b", (8, 30)),
    (r"durable goods", (8, 30)),
    (r"trade balance", (8, 30)),
    (r"personal (income|spending)|core pce", (8, 30)),
    (r"housing starts|building permits", (8, 30)),
    (r"empire state|philly fed|philadelphia fed", (8, 30)),
    (r"flash .*pmi|s&p global .*pmi", (9, 45)),
    (r"\bism\b", (10, 0)),
    (r"cb (leading index|consumer confidence)", (10, 0)),
    (r"(new|existing) home sales", (10, 0)),
    (r"\bjolts\b|job openings", (10, 0)),
    (r"factory orders|wholesale inventories", (10, 0)),
    (r"consumer sentiment|umich", (10, 0)),
    (r"crude oil inventories|petroleum status", (10, 30)),
    (r"natural gas storage", (10, 30)),
    (r"api weekly", (16, 30)),
    (r"fomc statement|federal funds rate", (14, 0)),
    (r"fomc press conference", (14, 30)),
)

_ANCHORS = tuple((re.compile(p, re.I), t) for p, t in ANCHORS)


def anchor_for(title):
    """(hour, minute) the issuing agency publishes this release, or None
    when the event is not one we hold a schedule for."""
    for rx, t in _ANCHORS:
        if rx.search(title or ""):
            return t
    return None


def audit_offsets(events, source_tz, title_key="title", date_key="date",
                  time_key="time"):
    """Compare each recognised event against its agency publication time.

    Returns (checked, matched, offsets) where offsets maps an offset in
    minutes to how many events showed it. A healthy feed puts everything
    at offset 0. A feed in the wrong zone puts everything at the same
    non-zero offset, which is the signature worth blocking on.
    """
    offsets, checked, matched = {}, 0, 0
    for e in events or []:
        a = anchor_for(e.get(title_key))
        if not a:
            continue
        dt = aware(e.get(date_key), e.get(time_key), source_tz)
        if dt is None:
            continue
        et = to_et(dt)
        checked += 1
        want = et.replace(hour=a[0], minute=a[1], second=0, microsecond=0)
        delta = int(round((et - want).total_seconds() / 60.0))
        if delta == 0:
            matched += 1
        offsets[delta] = offsets.get(delta, 0) + 1
    return checked, matched, offsets


def infer_source_tz(events, title_key="title", date_key="date",
                    time_key="time", min_events=3):
    """Which zone the feed is ACTUALLY published in, from the agency
    schedule. Returns (tzinfo, note) or (None, why-not).

    Only whole-hour offsets are considered: a real zone difference is a
    whole number of hours from Eastern for every US-facing feed, whereas a
    scatter of odd offsets means the events simply moved, not the zone.
    """
    checked, matched, offsets = audit_offsets(
        events, UTC, title_key, date_key, time_key)
    if checked < min_events:
        return None, ("only %d recognised event(s); need %d to infer a zone"
                      % (checked, min_events))
    delta, n = max(offsets.items(), key=lambda kv: kv[1])
    if n * 2 <= checked:
        return None, "no dominant offset across %d events" % checked
    if delta % 60:
        return None, "dominant offset %d min is not a whole hour" % delta
    # events read as UTC land `delta` minutes after their ET anchor, so
    # the feed's own zone is that many minutes ahead of Eastern
    return (UTC if delta == 0 else timezone(timedelta(minutes=-delta)),
            "%d of %d recognised events agree at %+d min from UTC"
            % (n, checked, -delta))


# ── venue timezones and event authority ─────────────────────────────────
# A vendor calendar reports a scheduled time in ITS OWN zone. That is not
# the same as the zone the event happens in, and neither is Eastern. A
# ceremony at 3:00 p.m. local in Ankara (UTC+3) is 8:00 a.m. ET; a feed
# that publishes 19:00 UTC for it is describing a different moment, and
# only the venue can settle which is right.
VENUE_TZ = {
    "ankara": "Europe/Istanbul", "istanbul": "Europe/Istanbul",
    "london": "Europe/London", "frankfurt": "Europe/Berlin",
    "berlin": "Europe/Berlin", "brussels": "Europe/Brussels",
    "tokyo": "Asia/Tokyo", "beijing": "Asia/Shanghai",
    "shanghai": "Asia/Shanghai", "hong kong": "Asia/Hong_Kong",
    "sydney": "Australia/Sydney", "ottawa": "America/Toronto",
    "toronto": "America/Toronto", "washington": "America/New_York",
    "new york": "America/New_York", "chicago": "America/Chicago",
}

# Vendor placeholders. These are not event titles -- they are categories
# the vendor uses when it has not resolved what the event actually is. A
# brief that prints one is asserting something it cannot support.
_GENERIC_TITLE = re.compile(
    r"^\s*(?:[A-Z][\w.'-]+\s+)*"
    r"(speaks|speech|remarks|comments|testifies|testimony|press conference"
    r"|appearance|event|meeting|summit|holiday|bank holiday|tentative"
    r"|all day)\s*$", re.I)


def is_generic_title(title):
    """True when the vendor gave a category rather than an event."""
    return bool(_GENERIC_TITLE.match((title or "").strip()))


def zone(name):
    """tzinfo for an IANA name, or None when it cannot be resolved."""
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        try:
            import pytz
            return pytz.timezone(name)
        except Exception:
            return None


def venue_zone(place):
    """tzinfo for a place name, or None. Never guesses from a country."""
    return zone(VENUE_TZ.get((place or "").strip().lower()))


def check_not_relabeled(events, declared_tz_name, source_tz,
                        title_key="title", date_key="date",
                        time_key="time"):
    """Blocking check: does the feed's declared zone survive contact with
    the agency schedule? Empty list = safe."""
    checked, matched, offsets = audit_offsets(
        events, source_tz, title_key, date_key, time_key)
    if checked == 0:
        return []                       # nothing recognised; assert nothing
    if matched * 2 > checked:
        return []                       # majority land on their anchor
    delta, n = max(offsets.items(), key=lambda kv: kv[1])
    if n * 2 > checked and delta:
        return ["calendar declared %s but %d of %d recognised releases sit "
                "%+d min from their published time -- the feed is being "
                "relabelled, not converted (e.g. a 10:30 ET release "
                "rendering as %02d:%02d)"
                % (declared_tz_name, n, checked, delta,
                   (10 + delta // 60) % 24, 30 + delta % 60)]
    return ["calendar times do not match published release times: only "
            "%d of %d recognised releases are on schedule" % (matched, checked)]


# ── self-test ───────────────────────────────────────────────────────────

def self_test():
    fails, ran = [], [0]

    def chk(n, c, d=""):
        ran[0] += 1
        print(("  PASS  " if c else "  FAIL  ") + n + ("" if c else "  <- %s" % (d,)))
        if not c:
            fails.append(n)

    chk("12-hour clock parsed", parse_clock("2:30pm") == (14, 30))
    chk("midnight hour handled", parse_clock("12:15am") == (0, 15))
    chk("noon hour handled", parse_clock("12:15pm") == (12, 15))
    chk("bare hour parsed", parse_clock("9am") == (9, 0))
    chk("24-hour clock parsed", parse_clock("14:30") == (14, 30))
    chk("'All Day' is not a time", parse_clock("All Day") is None)

    # the regression that started this: EIA petroleum, 14:30 UTC
    dt = aware("07-22-2026", "2:30pm", UTC)
    chk("14:30 UTC renders as 10:30 a.m. ET",
        fmt_time(dt) == "10:30 a.m. ET", fmt_time(dt))
    chk("converted stamp keeps the ET label", "ET" in fmt_stamp(dt))
    chk("day+time render", fmt_day_time(dt) == "Wed 10:30 a.m. ET",
        fmt_day_time(dt))

    try:
        aware("07-22-2026", "2:30pm", None)
        chk("missing source zone rejected", False, "no raise")
    except TimeSafetyError:
        chk("missing source zone rejected", True)
    try:
        to_et(datetime(2026, 7, 22, 14, 30))
        chk("naive datetime rejected by to_et", False, "no raise")
    except TimeSafetyError:
        chk("naive datetime rejected by to_et", True)

    chk("ISO with Z parses as UTC",
        fmt_time(parse_iso("2026-07-22T14:30:00Z")) == "10:30 a.m. ET")
    chk("winter date uses EST not EDT",
        fmt_time(aware("01-14-2026", "2:30pm", UTC)) == "9:30 a.m. ET",
        fmt_time(aware("01-14-2026", "2:30pm", UTC)))

    chk("anchor known for claims", anchor_for("Unemployment Claims") == (8, 30))
    chk("anchor known for EIA crude",
        anchor_for("Crude Oil Inventories") == (10, 30))
    chk("unknown event has no anchor",
        anchor_for("President Trump Speaks") is None)
    chk("weekly ADP not confused with NFP",
        anchor_for("ADP Weekly Employment Change") == (8, 15))

    # the real feed payload, read correctly and incorrectly
    feed = [{"title": "CB Leading Index m/m", "date": "07-20-2026",
             "time": "2:00pm"},
            {"title": "ADP Weekly Employment Change", "date": "07-21-2026",
             "time": "12:15pm"},
            {"title": "Crude Oil Inventories", "date": "07-22-2026",
             "time": "2:30pm"},
            {"title": "Unemployment Claims", "date": "07-23-2026",
             "time": "12:30pm"},
            {"title": "Natural Gas Storage", "date": "07-23-2026",
             "time": "2:30pm"},
            {"title": "Flash Manufacturing PMI", "date": "07-24-2026",
             "time": "1:45pm"},
            {"title": "President Trump Speaks", "date": "07-22-2026",
             "time": "7:00pm"}]

    c, m, _ = audit_offsets(feed, UTC)
    chk("read as UTC, every recognised release is on schedule",
        c == 6 and m == 6, (c, m))
    chk("unrecognised event is not asserted about", c == 6)

    problems = check_not_relabeled(feed, "America/New_York", ET)
    chk("UTC feed relabelled as ET is blocked", len(problems) == 1, problems)
    chk("block message names the real symptom",
        "relabelled, not converted" in (problems[0] if problems else ""),
        problems)
    chk("correctly declared UTC feed passes",
        check_not_relabeled(feed, "UTC", UTC) == [])

    tz, note = infer_source_tz(feed)
    chk("source zone inferred as UTC from the agency schedule",
        tz == UTC or (tz and tz.utcoffset(None) == timedelta(0)), note)

    et_feed = [{"title": t["title"], "date": t["date"],
                "time": {"2:00pm": "10:00am", "12:15pm": "8:15am",
                         "2:30pm": "10:30am", "12:30pm": "8:30am",
                         "1:45pm": "9:45am", "7:00pm": "3:00pm"}[t["time"]]}
               for t in feed]
    chk("a genuinely-ET feed passes unchanged",
        check_not_relabeled(et_feed, "America/New_York", ET) == [])

    print("\n%d/%d checks passed" % (ran[0] - len(fails), ran[0]))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(self_test())
