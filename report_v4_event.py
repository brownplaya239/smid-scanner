#!/usr/bin/env python3
"""report_v4_event.py — the Equity Research v4 event gate (v4.1).

The v4 spec makes one thing non-negotiable: before any conclusion, the
report reconciles the clock against the earnings calendar, the issuer's
release, the SEC filings and the call, and collapses all of it into ONE
event state. A directional rating is only permitted in states where the
facts it rests on are actually verified.

Six states (v4.1 expands the machine so a call held the evening of a
release is not still 'not concluded' the next morning):

  PRE-RELEASE          no results out for this period yet. A rating is
                       allowed; it is a pre-print view and says so.
  RELEASED PRE-CALL    the 8-K item 2.02 is out and reads as results, but
                       the earnings call has not yet started. Rating
                       allowed, flagged that call guidance is still to come.
  CALL IN PROGRESS     the call is (or, by the clock, is likely) live. No
                       rating — guidance colour changes on the call.
  POST-CALL NOT        the call has concluded by the clock, but no
  TRANSCRIPT-VERIFIED  transcript feed confirms what was said. Rating
                       allowed, flagged that call nuances are unverified.
                       This is the honest default the morning after.
  POST-CALL VERIFIED   results out and the call explicitly verified
                       (transcript/feed). The strongest state.
  DATA HOLD            results are out but the primary release or its
                       guidance could not be parsed. Fail-closed: NO
                       rating, a 'pending verification' flash instead.

Timing without a call feed: a post-market release is followed by a call
within the first couple of hours; by any later ET calendar day the call
is over. The gate infers RELEASED PRE-CALL / CALL IN PROGRESS / POST-CALL
from the release timestamp and the report clock (both in ET), and errs
toward withholding (CALL IN PROGRESS) inside the likely-live window rather
than asserting a rating over a call that may still move guidance. It never
claims POST-CALL VERIFIED from the clock alone — that needs a feed.
"""

import datetime as dt

# ── the six states ──────────────────────────────────────────────────────
PRE_RELEASE = "PRE-RELEASE"
RELEASED_PRE_CALL = "RELEASED PRE-CALL"
CALL_IN_PROGRESS = "CALL IN PROGRESS"
POST_CALL_UNVERIFIED = "POST-CALL NOT TRANSCRIPT-VERIFIED"
POST_CALL_VERIFIED = "POST-CALL VERIFIED"
DATA_HOLD = "DATA HOLD"

# Back-compat aliases for the two v4.0 names still referenced elsewhere.
PRE_EVENT = PRE_RELEASE
RESULTS_RELEASED = RELEASED_PRE_CALL

STATES = (PRE_RELEASE, RELEASED_PRE_CALL, CALL_IN_PROGRESS,
          POST_CALL_UNVERIFIED, POST_CALL_VERIFIED, DATA_HOLD)

# A rating is only meaningful where its inputs are settled. CALL IN
# PROGRESS and DATA HOLD withhold it.
RATING_ALLOWED = {PRE_RELEASE, RELEASED_PRE_CALL, POST_CALL_UNVERIFIED,
                  POST_CALL_VERIFIED}

# Post-market earnings calls typically begin ~30 min after the release and
# run ~90 min. Used only to distinguish the post-release phases from the
# clock — never to claim POST-CALL VERIFIED, which needs a feed.
CALL_DELAY_MIN = 30
CALL_DURATION_MIN = 90
ET_OFFSET_HOURS = -4                     # EDT; ET calendar-day comparison


def _parse(ts):
    if isinstance(ts, dt.datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _et_date(when):
    """The ET calendar date of a UTC instant — so 'a later trading day'
    means what a US reader means by it."""
    if not when:
        return None
    return (when.astimezone(dt.timezone.utc)
            + dt.timedelta(hours=ET_OFFSET_HOURS)).date()


def _call_phase(release, now):
    """From the release instant and the report clock, which side of the
    earnings call are we on: 'pre_call', 'in_progress' or 'concluded'.

    A later ET calendar day than the release means the call is over,
    regardless of the hour — the fix for a report written the morning
    after that still called the prior evening's call 'not concluded'."""
    if not (release and now):
        return "concluded"
    if _et_date(now) and _et_date(release) and _et_date(now) > _et_date(release):
        return "concluded"
    mins = (now - release).total_seconds() / 60.0
    if mins < CALL_DELAY_MIN:
        return "pre_call"
    if mins < CALL_DELAY_MIN + CALL_DURATION_MIN:
        return "in_progress"
    return "concluded"


def _released(catalyst, now):
    """Has a results event for THIS period actually been released? True
    only for a company primary release (8-K item 2.02) or an
    unverified-but-present release candidate dated at or before now."""
    kind = catalyst.get("event_kind")
    if kind not in ("primary_release", "unverified_release"):
        return False
    ev = _parse(catalyst.get("event_dt"))
    return bool(ev and now and ev <= now)


def _guidance_ok(exhibit):
    ex = exhibit or {}
    if ex.get("disposition") != "ADMITTED":
        return False
    return bool(ex.get("guidance"))


def event_state(catalyst, exhibit=None, report_time=None, call_status=None):
    """Resolve the single v4 event state and the permissions that follow.

    call_status is an optional out-of-band signal from a real call feed:
    'live' forces CALL IN PROGRESS, 'concluded' allows POST-CALL VERIFIED.
    None means unknown — the honest default; the gate then infers the
    post-release phase from the clock and never claims POST-CALL VERIFIED.
    """
    catalyst = catalyst or {}
    now = _parse(report_time) or dt.datetime.now(dt.timezone.utc)
    ev = _parse(catalyst.get("event_dt"))
    ver = catalyst.get("verification") or {}
    reasons = []

    released = _released(catalyst, now)

    if not released:
        state = PRE_RELEASE
        reasons.append("No results release dated on or before the report "
                       "time for this period.")
    elif ver.get("is_results_disclosure") is not True:
        state = DATA_HOLD
        reasons.append("Results appear released but the primary document "
                       "did not verify as a results disclosure: %s"
                       % (ver.get("reason") or "not verified"))
    elif call_status == "live":
        state = CALL_IN_PROGRESS
        reasons.append("A call-status feed reports the earnings call is "
                       "live; guidance may be revised on the call.")
    elif call_status == "concluded":
        state = POST_CALL_VERIFIED
        reasons.append("Release verified as results and a feed confirms the "
                       "call has concluded.")
    else:
        phase = _call_phase(ev, now)
        hrs = (now - ev).total_seconds() / 3600.0 if ev else 0.0
        if phase == "pre_call":
            state = RELEASED_PRE_CALL
            reasons.append("Results are out; the earnings call has not yet "
                           "started (%.1fh since release)." % hrs)
        elif phase == "in_progress":
            state = CALL_IN_PROGRESS
            reasons.append("The earnings call is within its likely window "
                           "(%.1fh since release) and no feed confirms it "
                           "has ended; guidance may still change." % hrs)
        else:
            state = POST_CALL_UNVERIFIED
            reasons.append("Results are out and the call has concluded by "
                           "the clock (%.0fh since release%s), but no "
                           "transcript feed verifies what was said."
                           % (hrs, ", a later ET day"
                              if _et_date(now) and _et_date(ev)
                              and _et_date(now) > _et_date(ev) else ""))
        if not _guidance_ok(exhibit):
            reasons.append("Guidance did not parse from the earnings "
                           "exhibit; the forward view is incomplete.")

    rating_allowed = state in RATING_ALLOWED

    # Once results are out, the vendor next-earnings date belongs to the
    # FOLLOWING period and must never be shown as this period's pending
    # catalyst. Only PRE-RELEASE has a genuinely pending print.
    next_earnings_is_pending = (state == PRE_RELEASE)

    return {
        "state": state,
        "rating_allowed": rating_allowed,
        "next_earnings_is_pending": next_earnings_is_pending,
        "call_concluded": state in (POST_CALL_UNVERIFIED, POST_CALL_VERIFIED),
        "flash": flash(catalyst, exhibit, now) if state == DATA_HOLD
                 else None,
        "reasons": reasons,
        "event_dt": catalyst.get("event_dt"),
        "event_kind": catalyst.get("event_kind"),
        "verified": bool(ver.get("is_results_disclosure") is True),
        "guidance_parsed": _guidance_ok(exhibit),
        "call_status": call_status or "unknown",
        "resolved_at": now.isoformat(),
    }


def flash(catalyst, exhibit=None, now=None):
    ver = (catalyst or {}).get("verification") or {}
    ev = (catalyst or {}).get("event_dt")
    why = ver.get("reason") or "the primary release could not be parsed"
    return {
        "headline": "Earnings update pending verification",
        "body": ("A results release dated %s appears to be out, but this "
                 "report could not verify it: %s. No rating is issued until "
                 "the primary release and its guidance are read from a filed "
                 "source. This is a data-availability hold, not a view on "
                 "the company." % (ev or "recently", why)),
        "rating": None,
        "state": DATA_HOLD,
    }


def forbidden_for_state(state):
    """Phrases the narrative must not use in a given state."""
    pre = ["reported today", "results showed", "beat expectations",
           "missed expectations", "post-earnings", "after the print"]
    post = ["ahead of earnings", "into the print", "before the report",
            "upcoming earnings", "next earnings", "earnings are next",
            "call has not concluded", "call had not concluded"]
    directional = ["we rate", "our rating", "price target of", "buy-rated",
                   "sell-rated", "overweight", "underweight"]
    return {
        PRE_RELEASE: pre,
        RELEASED_PRE_CALL: [p for p in post if "call" not in p],
        POST_CALL_UNVERIFIED: post,
        POST_CALL_VERIFIED: post,
        CALL_IN_PROGRESS: post + directional,
        DATA_HOLD: post + directional,
    }.get(state, [])
