#!/usr/bin/env python3
"""report_v4_event_test.py — prove the v4 event gate holds when it must.

The gate's whole value is that DATA HOLD fires on unreadable results and
that a rating is refused wherever its inputs are not verified. These tests
drive each state and, more importantly, each fail-closed transition.

    python report_v4_event_test.py
"""

import datetime as dt
import sys

import report_v4_event as E

UTC = dt.timezone.utc
_pass = _fail = 0


def chk(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print("  PASS  %s" % name)
    else:
        _fail += 1
        print("  FAIL  %s" % name)


def _cat(kind, ev, **ver):
    return {"event_kind": kind, "event_dt": ev.isoformat(),
            "verification": ver}


REL = dt.datetime(2026, 7, 22, 20, 5, tzinfo=UTC)   # 16:05 ET release
EXHIBIT_OK = {"disposition": "ADMITTED",
              "guidance": {"revenue": {"raw": "$2.7B +/- 5%"}}}
EXHIBIT_UNPARSED = {"disposition": "AVAILABLE_NOT_INGESTED", "guidance": {}}


print("pre-event")
# next earnings is upcoming; no release yet
r = E.event_state(_cat("primary_release", REL, is_results_disclosure=True),
                  EXHIBIT_OK, report_time=REL - dt.timedelta(hours=3))
chk("2h before release -> PRE-EVENT", r["state"] == E.PRE_EVENT)
chk("rating allowed pre-event", r["rating_allowed"] is True)
chk("next earnings is the pending catalyst", r["next_earnings_is_pending"])
chk("no flash pre-event", r["flash"] is None)

print("\nno catalyst at all")
r = E.event_state({"event_kind": None}, None, report_time=REL)
chk("nothing released -> PRE-EVENT", r["state"] == E.PRE_EVENT)

print("\nperiodic filing is not the release")
r = E.event_state(_cat("periodic_filing", REL, is_results_disclosure=True),
                  None, report_time=REL + dt.timedelta(hours=3))
chk("10-Q alone stays PRE-EVENT", r["state"] == E.PRE_EVENT)

print("\nresults released, verified, call not concluded")
r = E.event_state(_cat("primary_release", REL, is_results_disclosure=True,
                       document="exhibit"),
                  EXHIBIT_OK, report_time=REL + dt.timedelta(hours=2))
chk("2h after verified release -> RESULTS RELEASED",
    r["state"] == E.RESULTS_RELEASED)
chk("rating allowed", r["rating_allowed"] is True)
chk("next earnings NOT labelled pending after release",
    r["next_earnings_is_pending"] is False)

print("\nresults released, verified, call concluded by next session")
r = E.event_state(_cat("primary_release", REL, is_results_disclosure=True),
                  EXHIBIT_OK, report_time=REL + dt.timedelta(hours=20))
chk("20h after -> POST-CALL VERIFIED", r["state"] == E.POST_CALL_VERIFIED)
chk("rating allowed post-call", r["rating_allowed"] is True)

print("\ncall-status feed overrides the clock")
r = E.event_state(_cat("primary_release", REL, is_results_disclosure=True),
                  EXHIBIT_OK, report_time=REL + dt.timedelta(hours=1),
                  call_status="live")
chk("call live -> CALL IN PROGRESS", r["state"] == E.CALL_IN_PROGRESS)
chk("no rating while call is live", r["rating_allowed"] is False)

r = E.event_state(_cat("primary_release", REL, is_results_disclosure=True),
                  EXHIBIT_OK, report_time=REL + dt.timedelta(hours=1),
                  call_status="concluded")
chk("call concluded early -> POST-CALL VERIFIED",
    r["state"] == E.POST_CALL_VERIFIED)

print("\nDATA HOLD: results out but the release will not verify")
r = E.event_state(
    _cat("primary_release", REL, is_results_disclosure=False,
         reason="the primary document did not read as a results disclosure"),
    EXHIBIT_UNPARSED, report_time=REL + dt.timedelta(hours=2))
chk("unparseable release -> DATA HOLD", r["state"] == E.DATA_HOLD)
chk("NO rating in DATA HOLD", r["rating_allowed"] is False)
chk("a flash is published instead", r["flash"] is not None)
chk("flash headline is the pending-verification notice",
    r["flash"]["headline"] == "Earnings update pending verification")
chk("flash carries no rating", r["flash"]["rating"] is None)
chk("next earnings NOT labelled pending in DATA HOLD",
    r["next_earnings_is_pending"] is False)

print("\nunverified_release candidate is treated as released")
r = E.event_state(
    _cat("unverified_release", REL, is_results_disclosure=None,
         reason="could not confirm"),
    EXHIBIT_UNPARSED, report_time=REL + dt.timedelta(hours=2))
chk("unverified release -> DATA HOLD (not a clean rating)",
    r["state"] == E.DATA_HOLD)

print("\nverified results but guidance did not parse")
r = E.event_state(_cat("primary_release", REL, is_results_disclosure=True),
                  EXHIBIT_UNPARSED, report_time=REL + dt.timedelta(hours=20))
chk("readable results, unreadable guidance -> POST-CALL VERIFIED "
    "(not a hold)", r["state"] == E.POST_CALL_VERIFIED)
chk("but guidance-incomplete is on the record",
    any("Guidance did not parse" in x for x in r["reasons"]))

print("\nforbidden-phrase sets")
chk("DATA HOLD forbids directional language",
    "price target of" in E.forbidden_for_state(E.DATA_HOLD))
chk("POST-CALL forbids 'upcoming earnings'",
    "upcoming earnings" in E.forbidden_for_state(E.POST_CALL_VERIFIED))
chk("PRE-EVENT forbids 'results showed'",
    "results showed" in E.forbidden_for_state(E.PRE_EVENT))

print("\n%d/%d checks passed" % (_pass, _pass + _fail))
sys.exit(1 if _fail else 0)
