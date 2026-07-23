#!/usr/bin/env python3
"""report_v4_event.py — the Equity Research v4 event gate.

The v4 spec makes one thing non-negotiable: before any conclusion, the
report must reconcile the clock against the earnings calendar, the
issuer's release, the SEC filings and the call, and collapse all of it
into ONE event state. A directional rating is only permitted in states
where the facts a rating rests on are actually verified.

Five states, and what each permits:

  PRE-EVENT           no results out for this period yet. A rating is
                      allowed; it is a pre-print view and says so.
  RESULTS RELEASED    the 8-K item 2.02 is out and reads as results, but
                      the call has not verifiably concluded. Rating
                      allowed, flagged that the call may revise the read.
  CALL IN PROGRESS    the earnings call is live. No rating — the guidance
                      colour changes on the call. Reachable only from a
                      real call-status feed; this codebase has none, so
                      the gate never asserts it from a guessed clock
                      window. Inventing "the call is live" is a fact we
                      did not observe.
  POST-CALL VERIFIED  results out, verified, and enough time has passed
                      that the call has concluded. The strongest state; a
                      full rating is permitted.
  DATA HOLD           results are out but the primary release or its
                      guidance could not be parsed. This is the
                      fail-closed state: NO rating, a "pending
                      verification" flash instead, and the vendor's
                      next-earnings date is never presented as "next" —
                      the print already happened, we just could not read
                      it.

Why a separate module. The gate is the spec's first requirement and it
has to be testable in isolation from the renderer and the data layer, so
a change to page layout can never quietly weaken it. It consumes the
reconciliation the snapshot already did — catalyst discovery, the 8-K
exhibit read, the release verification — and adds only the mapping and
the permission rules.
"""

import datetime as dt

# ── the five states ─────────────────────────────────────────────────────
PRE_EVENT = "PRE-EVENT"
RESULTS_RELEASED = "RESULTS RELEASED"
CALL_IN_PROGRESS = "CALL IN PROGRESS"
POST_CALL_VERIFIED = "POST-CALL VERIFIED"
DATA_HOLD = "DATA HOLD"

STATES = (PRE_EVENT, RESULTS_RELEASED, CALL_IN_PROGRESS,
          POST_CALL_VERIFIED, DATA_HOLD)

# A rating is only meaningful where its inputs are verified. These are the
# states in which a directional rating may be published at all; the report
# must withhold it everywhere else.
RATING_ALLOWED = {PRE_EVENT, RESULTS_RELEASED, POST_CALL_VERIFIED}

# Hours after a release before the call has reliably concluded. Earnings
# calls run within the first few hours after a 16:00 ET release; by the
# next session they are always done. This is only used to distinguish
# RESULTS RELEASED from POST-CALL VERIFIED — never to claim a call is
# *live*, which would need a real feed.
CALL_CONCLUDED_HOURS = 18


def _parse(ts):
    if isinstance(ts, dt.datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _released(catalyst, now):
    """Has a results event for THIS period actually been released?

    True only for a company primary release (8-K item 2.02) or an
    unverified-but-present release candidate dated at or before now. A
    periodic filing (10-Q/10-K) alone is a filing, not the results event,
    and does not flip the gate out of PRE-EVENT on its own — the release
    is what the market reacts to.
    """
    kind = catalyst.get("event_kind")
    if kind not in ("primary_release", "unverified_release"):
        return False
    ev = _parse(catalyst.get("event_dt"))
    return bool(ev and now and ev <= now)


def _guidance_ok(exhibit):
    """Did the earnings exhibit yield usable guidance?

    ADMITTED with a non-empty guidance block is the bar. A wrapper we
    could locate but not parse (AVAILABLE_NOT_INGESTED) does not clear it:
    that is exactly the case DATA HOLD exists for.
    """
    ex = exhibit or {}
    if ex.get("disposition") != "ADMITTED":
        return False
    return bool(ex.get("guidance"))


def event_state(catalyst, exhibit=None, report_time=None, call_status=None):
    """Resolve the single v4 event state and the permissions that follow.

    call_status is an optional out-of-band signal from a real call feed:
    "live" forces CALL IN PROGRESS, "concluded" allows POST-CALL VERIFIED
    early. None means unknown — the honest default, since this codebase
    has no call feed, and the gate then relies on the clock only to tell
    RESULTS RELEASED from POST-CALL VERIFIED, never to claim the call is
    live.
    """
    catalyst = catalyst or {}
    now = _parse(report_time) or dt.datetime.now(dt.timezone.utc)
    ev = _parse(catalyst.get("event_dt"))
    ver = catalyst.get("verification") or {}
    reasons = []

    released = _released(catalyst, now)

    if not released:
        state = PRE_EVENT
        reasons.append("No results release dated on or before the report "
                       "time for this period.")
    else:
        # Results are out. Can we actually read them?
        is_results = ver.get("is_results_disclosure") is True
        guid = _guidance_ok(exhibit)
        if not is_results:
            state = DATA_HOLD
            reasons.append("Results appear released but the primary "
                           "document did not verify as a results "
                           "disclosure: %s"
                           % (ver.get("reason") or "not verified"))
        elif call_status == "live":
            state = CALL_IN_PROGRESS
            reasons.append("A call-status feed reports the earnings call "
                           "is live; guidance may be revised on the call.")
        else:
            hrs = (now - ev).total_seconds() / 3600.0 if ev else 0.0
            concluded = call_status == "concluded" or hrs >= CALL_CONCLUDED_HOURS
            if concluded:
                state = POST_CALL_VERIFIED
                reasons.append("Release verified as results; the call has "
                               "concluded (%s)."
                               % (call_status or "%.0fh since release"
                                  % hrs))
            else:
                state = RESULTS_RELEASED
                reasons.append("Release verified as results; the call has "
                               "not verifiably concluded (%.0fh since "
                               "release, no call-status feed)." % hrs)
            if not guid:
                # Verified release, but guidance did not parse. Not a hold
                # — the reported results are readable — but the rating
                # must know guidance is missing rather than assume it.
                reasons.append("Guidance did not parse from the earnings "
                               "exhibit; the forward view is incomplete.")

    rating_allowed = state in RATING_ALLOWED

    # "Never label earnings as 'next' after the release." Once results are
    # out, the vendor next-earnings date belongs to the FOLLOWING period
    # and must not be shown as the pending catalyst for this one.
    next_earnings_is_pending = (state == PRE_EVENT)

    out = {
        "state": state,
        "rating_allowed": rating_allowed,
        "next_earnings_is_pending": next_earnings_is_pending,
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
    return out


def flash(catalyst, exhibit=None, now=None):
    """The DATA HOLD flash: an honest 'we cannot read this yet' notice.

    It states what was found, what could not be parsed, and refuses a
    rating outright. It is not a report; it is the thing published INSTEAD
    of a report when the gate holds.
    """
    ver = (catalyst or {}).get("verification") or {}
    ev = (catalyst or {}).get("event_dt")
    why = ver.get("reason") or "the primary release could not be parsed"
    return {
        "headline": "Earnings update pending verification",
        "body": ("A results release dated %s appears to be out, but this "
                 "report could not verify it: %s. No rating is issued "
                 "until the primary release and its guidance are read from "
                 "a filed source. This is a data-availability hold, not a "
                 "view on the company." % (ev or "recently", why)),
        "rating": None,
        "state": DATA_HOLD,
    }


def forbidden_for_state(state):
    """Phrases the narrative must not use in a given state — a superset of
    the v3 rules, extended for the two new states. RESULTS RELEASED and
    POST-CALL VERIFIED must not talk about the print as upcoming; DATA
    HOLD and CALL IN PROGRESS must not assert a directional result at
    all."""
    pre = ["reported today", "results showed", "beat expectations",
           "missed expectations", "post-earnings", "after the print"]
    post = ["ahead of earnings", "into the print", "before the report",
            "upcoming earnings", "next earnings", "earnings are next"]
    directional = ["we rate", "our rating", "price target of", "buy-rated",
                   "sell-rated", "overweight", "underweight"]
    return {
        PRE_EVENT: pre,
        RESULTS_RELEASED: post,
        POST_CALL_VERIFIED: post,
        CALL_IN_PROGRESS: post + directional,
        DATA_HOLD: post + directional,
    }.get(state, [])
