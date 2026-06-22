#!/usr/bin/env python3
"""Intraday freshness watchdog for the UOA (options-flow) dataset.

Runs from a GitHub Actions schedule a few times during RTH. Fetches the
DEPLOYED uoa_latest.json, checks how old its `generated` timestamp is, and
if it is staler than MAX_AGE_MIN during market hours on a weekday, emails
an alert via Resend.

Design note: this checks the OUTCOME (is the data fresh?) rather than any
single trigger, so it catches every failure mode at once — a dropped
GitHub schedule, a missed Cloudflare cron, a PAT-expired backstop dispatch,
or a scan that ran but failed to publish. It is intentionally independent
of both the UOA workflow and the worker cron so it can't fail the same way
they do.

Env:
  REPORT_URL          default https://tickerdesk.io/reports/uoa_latest.json
  MAX_AGE_MIN         default 180  (UOA cadence ~75-90 min; 180 = ~2 missed
                                    scans before paging, so no false alarms)
  RESEND_API_KEY      required to actually send the alert
  FROM_EMAIL          verified Resend sender (required to send)
  ALERT_EMAIL         recipient; defaults to the operator address below

Exit code: 0 when fresh / skipped, 1 when STALE (so the Actions run also
shows red and GitHub emails the owner — a second, independent page).
"""
import datetime
import json
import os
import sys
import urllib.parse
import urllib.request

REPORT_URL = os.environ.get(
    "REPORT_URL", "https://tickerdesk.io/reports/uoa_latest.json")
MAX_AGE_MIN = int(os.environ.get("MAX_AGE_MIN", "180"))
# If the DATA (`generated`) is stale but the scanner ATTEMPTED a run within this
# window, it's an upstream-empty preserve (Polygon came back blank) — the
# scanner is healthy, so we send a calm [INFO] and exit 0 instead of redding
# the monitor + screaming "pipeline down". Scans run ~every 75 min in RTH.
ATTEMPT_MAX_MIN = int(os.environ.get("ATTEMPT_MAX_MIN", "120"))
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "")
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "") or "sumeetsancheti97@gmail.com"


def et_now():
    """Current time in America/New_York (GitHub runners ship tzdata)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        # Fallback: assume EDT (UTC-4). Only affects the RTH-window guard;
        # the age math below is timezone-agnostic (uses UTC throughout).
        return datetime.datetime.now(datetime.timezone.utc) - \
            datetime.timedelta(hours=4)


def send_alert(subject, body):
    if not RESEND_API_KEY or not FROM_EMAIL:
        print("[monitor] RESEND not configured — would have alerted:\n"
              + subject + "\n" + body)
        return
    try:
        data = json.dumps({
            "from": FROM_EMAIL,
            "to": [ALERT_EMAIL],
            "subject": subject,
            "text": body,
        }).encode()
        req = urllib.request.Request(
            "https://api.resend.com/emails", data=data,
            headers={"Authorization": "Bearer " + RESEND_API_KEY,
                     "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
        print("[monitor] email alert sent to " + ALERT_EMAIL)
    except Exception as e:
        print("[monitor] email send failed: " + str(e))


def main():
    now = et_now()
    # Weekday + RTH guard. Give the first scan until ~9:45 ET to publish so
    # a pre-open run never false-alarms on yesterday's (legitimate) data.
    if now.weekday() >= 5:
        print("[monitor] weekend — skip.")
        return 0
    mins = now.hour * 60 + now.minute
    if not (9 * 60 + 45 <= mins <= 16 * 60 + 30):
        print("[monitor] outside RTH window (%02d:%02d ET) — skip."
              % (now.hour, now.minute))
        return 0

    try:
        url = REPORT_URL + ("&" if "?" in REPORT_URL else "?") + \
            "cb=" + now.strftime("%H%M%S")
        raw = urllib.request.urlopen(url, timeout=20).read()
        d = json.loads(raw)
    except Exception as e:
        send_alert("TickerDesk: UOA monitor could not fetch data",
                   "Failed to fetch/parse uoa_latest.json: " + str(e)[:200])
        return 1

    gen = d.get("generated")
    if not gen:
        send_alert("TickerDesk: UOA data has no timestamp",
                   "uoa_latest.json is missing the `generated` field.")
        return 1
    try:
        t = datetime.datetime.fromisoformat(str(gen).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        send_alert("TickerDesk: UOA timestamp unparseable",
                   "Could not parse `generated`: " + str(gen)[:80])
        return 1

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    age_min = (now_utc - t).total_seconds() / 60.0

    if age_min <= MAX_AGE_MIN:
        print("[monitor] fresh: %.0f min old (<= %d) — OK." % (age_min, MAX_AGE_MIN))
        return 0

    # Data is stale. Separate "scanner alive but upstream (Polygon) returned
    # empty so we preserved the last good payload" from "pipeline down — nothing
    # ran". The scanner stamps `last_attempt` on every run (even empty ones);
    # `generated` only advances when a run produced real rows.
    attempt = d.get("last_attempt")
    attempt_age = None
    if attempt:
        try:
            at = datetime.datetime.fromisoformat(str(attempt).replace("Z", "+00:00"))
            if at.tzinfo is None:
                at = at.replace(tzinfo=datetime.timezone.utc)
            attempt_age = (now_utc - at).total_seconds() / 60.0
        except Exception:
            attempt_age = None

    if attempt_age is not None and attempt_age <= ATTEMPT_MAX_MIN:
        # Healthy scanner, empty upstream — the preserve guard is doing its job.
        # Inform (paying users' flow IS stale), but don't redden the monitor or
        # imply our pipeline broke. Persisting >2-3h would suggest a real
        # Polygon outage/quota issue worth escalating.
        body = (
            "Options-flow DATA is %.0f min old (%s), but the scanner ran only "
            "%.0f min ago and returned 0 rows — it's healthy and is "
            "intentionally preserving the last good payload because the upstream "
            "options feed (Polygon) came back empty.\n\n"
            "No action needed unless this persists past ~2-3 hours (then suspect "
            "a Polygon outage or a quota/rate issue — e.g. a day of heavy manual "
            "workflow dispatches). Front-end freshness pills already show users "
            "the data is delayed."
            % (age_min, str(gen), attempt_age))
        send_alert("[INFO] TickerDesk: UOA upstream empty — data preserved (%.1fh)"
                   % (age_min / 60.0), body)
        print("[monitor] upstream-empty: data %.0fm old, scanner ran %.0fm ago "
              "— preserved, not a pipeline failure." % (age_min, attempt_age))
        return 0

    # No last_attempt (old payload) or the scanner itself hasn't run in a long
    # time -> genuine pipeline failure.
    attempt_note = (" and the scanner has not run for %.0f min" % attempt_age
                    if attempt_age is not None
                    else " (no last_attempt stamp — old data or scanner never ran)")
    body = (
        "Options-flow dataset (uoa_latest.json) last updated %.0f min ago "
        "(%s) — over the %d-min RTH threshold%s.\n\n"
        "The scanner does NOT appear to be running. Check:\n"
        "  1. The 'Unusual Options Activity' GitHub workflow runs.\n"
        "  2. The Cloudflare worker cron backstop — Workers dashboard -> "
        "Logs/Cron Triggers. A 'dispatch uoa.yml -> HTTP 401' means the "
        "PAT secret expired (regenerate + update the worker secret).\n\n"
        "Quick fix: gh workflow run uoa.yml --ref master"
        % (age_min, str(gen), MAX_AGE_MIN, attempt_note))
    send_alert("[ALERT] TickerDesk: UOA flow is STALE — pipeline down (%.1fh)"
               % (age_min / 60.0), body)
    print("[monitor] STALE/pipeline-down: %.0f min > %d — alerted."
          % (age_min, MAX_AGE_MIN))
    return 1


if __name__ == "__main__":
    sys.exit(main())
