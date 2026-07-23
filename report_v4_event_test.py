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

print("\nresults out, before the call starts")
r = E.event_state(_cat("primary_release", REL, is_results_disclosure=True,
                       document="exhibit"),
                  EXHIBIT_OK, report_time=REL + dt.timedelta(minutes=10))
chk("10 min after release -> RELEASED PRE-CALL",
    r["state"] == E.RELEASED_PRE_CALL)
chk("rating allowed pre-call", r["rating_allowed"] is True)
chk("next earnings NOT labelled pending after release",
    r["next_earnings_is_pending"] is False)

print("\nthe call's likely window, same session, no feed")
r = E.event_state(_cat("primary_release", REL, is_results_disclosure=True),
                  EXHIBIT_OK, report_time=REL + dt.timedelta(minutes=60))
chk("1h after (same ET day) -> CALL IN PROGRESS",
    r["state"] == E.CALL_IN_PROGRESS)
chk("no rating while the call may be live", r["rating_allowed"] is False)

print("\nthe morning after: call concluded, transcript not verified")
r = E.event_state(_cat("primary_release", REL, is_results_disclosure=True),
                  EXHIBIT_OK, report_time=REL + dt.timedelta(hours=20))
chk("20h / next ET day -> POST-CALL NOT TRANSCRIPT-VERIFIED",
    r["state"] == E.POST_CALL_UNVERIFIED)
chk("rating allowed post-call", r["rating_allowed"] is True)
chk("call_concluded flag set", r["call_concluded"] is True)
chk("a later ET day is never still 'in progress'",
    r["state"] != E.CALL_IN_PROGRESS)

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

print("\nverified results but the release exhibit could not be parsed")
# The release is filed and verified, but its exhibit (guidance + KPIs) is
# AVAILABLE_NOT_INGESTED — the quarter cannot be read in full, so the gate
# holds rather than shipping GAAP figures without the metrics that define it.
r = E.event_state(_cat("primary_release", REL, is_results_disclosure=True),
                  EXHIBIT_UNPARSED, report_time=REL + dt.timedelta(hours=20))
chk("readable results, UNINGESTED exhibit -> DATA HOLD",
    r["state"] == E.DATA_HOLD)
chk("no rating when the release detail is unreadable",
    r["rating_allowed"] is False)
chk("flash names the missing guidance and KPIs",
    "cRPO" in r["flash"]["body"] or "operating metrics" in r["flash"]["body"])

print("\nverified results with a PARSED exhibit but thin guidance")
# disposition ADMITTED but the guidance block happens to be empty: the
# release WAS read, so this is not a hold — only a completeness note.
r = E.event_state(_cat("primary_release", REL, is_results_disclosure=True),
                  {"disposition": "ADMITTED", "guidance": {}},
                  report_time=REL + dt.timedelta(hours=20))
chk("parsed exhibit, empty guidance -> POST-CALL (not a hold)",
    r["state"] == E.POST_CALL_UNVERIFIED)
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
