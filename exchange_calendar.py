#!/usr/bin/env python3
"""exchange_calendar.py — which days are sessions, and when they end.

"Yesterday" is not "today minus one day". Friday's flow is evaluated on
Monday; the Wednesday before Thanksgiving is followed by a half day; a
holiday that falls on a Saturday is observed on the preceding Friday. Any
code that reaches for timedelta(days=1) gets all of those wrong, and gets
them wrong silently — the OI it fetches is simply from the wrong session.

Close times are per PRODUCT, not per market. Standard equity options stop
at 16:00 ET; cash-settled index options (SPX, NDX, RUT, VIX and their
minis) trade until 16:15. Freezing an SPX contract's daily aggregate at
16:00 throws away fifteen minutes of prints.

Dates outside the known range raise rather than guess. A calendar that
invents a holiday table for 2031 is worse than one that says it does not
know, because the invented answer looks like a fact.

    python exchange_calendar.py --self-test
"""

import sys
from datetime import date, datetime, time, timedelta

import brief_time as BT

# Full-day closures, as OBSERVED (a Saturday holiday is taken on the
# preceding Friday, a Sunday holiday on the following Monday). Verified
# against the NYSE/Cboe published schedules for these years only.
HOLIDAYS = {
    2025: ["2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17",
           "2025-04-18", "2025-05-26", "2025-06-19", "2025-07-04",
           "2025-09-01", "2025-11-27", "2025-12-25"],
    2026: ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
           "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
           "2026-11-26", "2026-12-25"],
    2027: ["2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
           "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06",
           "2027-11-25", "2027-12-24"],
}
# 13:00 ET equity close (options follow, index options 13:15).
HALF_DAYS = {
    2025: ["2025-07-03", "2025-11-28", "2025-12-24"],
    2026: ["2026-11-27", "2026-12-24"],
    2027: ["2027-11-26"],
}
KNOWN_YEARS = tuple(sorted(HOLIDAYS))

# Products whose options run past the equity close.
INDEX_ROOTS = ("SPX", "SPXW", "XSP", "NDX", "NDXP", "XND", "RUT", "RUTW",
               "MRUT", "VIX", "VIXW", "DJX")
EQUITY_CLOSE = time(16, 0)
INDEX_CLOSE = time(16, 15)
HALF_EQUITY_CLOSE = time(13, 0)
HALF_INDEX_CLOSE = time(13, 15)


class CalendarRangeError(ValueError):
    """Asked about a year the table does not cover."""


def _d(x):
    if isinstance(x, date) and not isinstance(x, datetime):
        return x
    if isinstance(x, datetime):
        return x.date()
    return date.fromisoformat(str(x)[:10])


def _require_year(y):
    if y not in HOLIDAYS:
        raise CalendarRangeError(
            "no exchange calendar for %d; known years are %s — extend the "
            "table rather than inferring" % (y, list(KNOWN_YEARS)))


def is_holiday(d):
    d = _d(d)
    _require_year(d.year)
    return d.isoformat() in HOLIDAYS[d.year]


def is_half_day(d):
    d = _d(d)
    _require_year(d.year)
    return d.isoformat() in HALF_DAYS.get(d.year, [])


def is_session(d):
    d = _d(d)
    _require_year(d.year)
    return d.weekday() < 5 and not is_holiday(d)


def previous_session(d):
    """The session strictly before `d`. Friday for a Monday, and the day
    before a holiday for the day after it."""
    d = _d(d)
    cur = d - timedelta(days=1)
    for _ in range(12):
        _require_year(cur.year)
        if is_session(cur):
            return cur
        cur -= timedelta(days=1)
    raise CalendarRangeError("no session found within 12 days before %s" % d)


def next_session(d):
    d = _d(d)
    cur = d + timedelta(days=1)
    for _ in range(12):
        _require_year(cur.year)
        if is_session(cur):
            return cur
        cur += timedelta(days=1)
    raise CalendarRangeError("no session found within 12 days after %s" % d)


def sessions_between(a, b):
    """Sessions in [a, b], inclusive."""
    a, b = _d(a), _d(b)
    out, cur = [], a
    while cur <= b:
        _require_year(cur.year)
        if is_session(cur):
            out.append(cur)
        cur += timedelta(days=1)
    return out


_OCC_TAIL = __import__("re").compile(r"\d{6}[CP]\d{8}$")


def root_of(occ_or_ticker):
    """Underlying root from an OCC symbol or a bare ticker.

    Split from the RIGHT. An OCC tail is always six date digits, C or P,
    and eight strike digits — fifteen characters — so the root is whatever
    precedes them. Scanning left-to-right for letters instead reads
    "AAPL1" as "AAPL", which silently erases the digit that marks an
    ADJUSTED series: exactly the contract whose open interest must not be
    differenced across the adjustment.
    """
    s = str(occ_or_ticker or "").upper()
    if s.startswith("O:"):
        s = s[2:]
    if len(s) > 15 and _OCC_TAIL.search(s[-15:]):
        return s[:-15]
    i = 0
    while i < len(s) and (s[i].isalnum() or s[i] == "."):
        i += 1
    return s[:i]


def is_index_product(occ_or_ticker):
    return root_of(occ_or_ticker) in INDEX_ROOTS


def close_time(d, occ_or_ticker=""):
    """The ET time this PRODUCT stops trading on this date."""
    d = _d(d)
    _require_year(d.year)
    if not is_session(d):
        raise CalendarRangeError("%s is not a trading session" % d)
    idx = is_index_product(occ_or_ticker)
    if is_half_day(d):
        return HALF_INDEX_CLOSE if idx else HALF_EQUITY_CLOSE
    return INDEX_CLOSE if idx else EQUITY_CLOSE


def session_close_dt(d, occ_or_ticker=""):
    """Aware ET datetime at which this contract's session ends."""
    d = _d(d)
    t = close_time(d, occ_or_ticker)
    return datetime(d.year, d.month, d.day, t.hour, t.minute, tzinfo=BT.ET)


def session_of(dt, occ_or_ticker=""):
    """The trading date an aware timestamp belongs to.

    A print at 16:07 ET on an SPX contract belongs to that day's session;
    the same timestamp on an equity option is after the close and belongs
    to the next session's tape.
    """
    et = BT.to_et(dt)
    d = et.date()
    _require_year(d.year)
    if is_session(d) and et <= session_close_dt(d, occ_or_ticker):
        return d
    return next_session(d) if not is_session(d) or \
        et > session_close_dt(d, occ_or_ticker) else d


def is_frozen(session_date, now_et, occ_or_ticker=""):
    """True once this contract's session has closed and its daily
    aggregate can no longer change."""
    return BT.to_et(now_et) > session_close_dt(session_date, occ_or_ticker)


def self_test():
    fails, ran = [], [0]

    def chk(n, c, d=""):
        ran[0] += 1
        print(("  PASS  " if c else "  FAIL  ") + n
              + ("" if c else "  <- %s" % (d,)))
        if not c:
            fails.append(n)

    chk("a normal weekday is a session", is_session("2026-07-22"))
    chk("Saturday is not a session", not is_session("2026-07-25"))
    chk("Christmas is a holiday", is_holiday("2026-12-25"))
    chk("Good Friday is a holiday", is_holiday("2026-04-03"))
    chk("July 4 on a Saturday is observed on the Friday",
        is_holiday("2026-07-03") and not is_session("2026-07-03"))

    chk("Monday's previous session is Friday",
        previous_session("2026-07-20").isoformat() == "2026-07-17",
        previous_session("2026-07-20"))
    chk("the day after Thanksgiving looks back to Wednesday",
        previous_session("2026-11-27").isoformat() == "2026-11-25",
        previous_session("2026-11-27"))
    chk("the day after Christmas looks back over the holiday",
        previous_session("2026-12-28").isoformat() == "2026-12-24",
        previous_session("2026-12-28"))
    chk("previous session never returns a weekend",
        all(previous_session(d).weekday() < 5
            for d in ("2026-07-20", "2026-07-21", "2026-01-04")))
    chk("next session skips the holiday",
        next_session("2026-07-02").isoformat() == "2026-07-06",
        next_session("2026-07-02"))
    chk("sessions_between excludes holidays and weekends",
        [d.isoformat() for d in sessions_between("2026-07-17", "2026-07-21")]
        == ["2026-07-17", "2026-07-20", "2026-07-21"],
        sessions_between("2026-07-17", "2026-07-21"))

    chk("equity options close at 16:00",
        close_time("2026-07-22", "O:TSLA260724C00380000") == time(16, 0))
    chk("index options close at 16:15",
        close_time("2026-07-22", "O:SPXW260722P06000000") == time(16, 15),
        close_time("2026-07-22", "O:SPXW260722P06000000"))
    chk("half day closes equities at 13:00",
        close_time("2026-11-27", "O:AAPL261218C00300000") == time(13, 0))
    chk("half day closes index options at 13:15",
        close_time("2026-11-27", "O:SPX261218C06000000") == time(13, 15))
    chk("root parsed from an OCC symbol",
        root_of("O:BRK.B260724C00500000") == "BRK.B",
        root_of("O:BRK.B260724C00500000"))
    chk("index product detected", is_index_product("O:SPXW260722P06000000"))
    chk("equity product not treated as index",
        not is_index_product("O:TSLA260724C00380000"))

    late = datetime(2026, 7, 22, 16, 7, tzinfo=BT.ET)
    chk("16:07 belongs to the same session for SPX",
        session_of(late, "O:SPXW260722P06000000").isoformat() == "2026-07-22")
    chk("16:07 rolls to the next session for an equity option",
        session_of(late, "O:TSLA260724C00380000").isoformat() == "2026-07-23",
        session_of(late, "O:TSLA260724C00380000"))
    chk("an equity aggregate is frozen after 16:00",
        is_frozen("2026-07-22", datetime(2026, 7, 22, 16, 1, tzinfo=BT.ET),
                  "O:TSLA260724C00380000"))
    chk("an index aggregate is NOT frozen at 16:01",
        not is_frozen("2026-07-22",
                      datetime(2026, 7, 22, 16, 1, tzinfo=BT.ET),
                      "O:SPXW260722P06000000"))

    try:
        is_session("2031-03-04")
        chk("an unknown year raises rather than guessing", False, "no raise")
    except CalendarRangeError:
        chk("an unknown year raises rather than guessing", True)

    print("\n%d/%d checks passed" % (ran[0] - len(fails), ran[0]))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(self_test())
