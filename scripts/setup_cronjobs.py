#!/usr/bin/env python3
"""
setup_cronjobs.py — register every scheduled scan as a cron-job.org HTTP job
that fires the matching GitHub Actions workflow via workflow_dispatch.

Why this exists: GitHub-native cron is best-effort — it was firing scans
1-3 hours late or dropping them entirely. cron-job.org triggers
workflow_dispatch punctually and handles US DST itself via the
America/New_York timezone, so no more summer/winter cron duplication.

One-time setup before running:
  1. Create a cron-job.org account, then generate an API key:
       console.cron-job.org -> Settings -> API
  2. Create a GitHub fine-grained PAT for brownplaya239/smid-scanner with
     Repository permissions -> Actions: Read and write.
  3. Run (PowerShell):
       $env:CRONJOB_API_KEY="..."; $env:GH_DISPATCH_PAT="github_pat_..."
       python scripts/setup_cronjobs.py

Re-running is safe: jobs are matched by title and skipped if they exist.
"""
import json
import os
import sys
import urllib.request
import urllib.error

REPO = "brownplaya239/smid-scanner"
CRONJOB_API = "https://api.cron-job.org"

CRONJOB_API_KEY = os.environ.get("CRONJOB_API_KEY", "").strip()
GH_PAT = os.environ.get("GH_DISPATCH_PAT", "").strip()

# (title, workflow file, hour, minute) — weekdays only, America/New_York
JOBS = [
    ("UOA 11:07 AM ET",     "uoa.yml",      11,  7),
    ("UOA 1:37 PM ET",      "uoa.yml",      13, 37),
    ("UOA 3:47 PM ET",      "uoa.yml",      15, 47),
    ("Scanner 10:07 AM ET", "scanner.yml",  10,  7),
    ("Scanner 11:37 AM ET", "scanner.yml",  11, 37),
    ("Scanner 3:07 PM ET",  "scanner.yml",  15,  7),
    ("Scanner 4:17 PM ET",  "scanner.yml",  16, 17),
    ("Momentum 4:33 PM ET", "momentum.yml", 16, 33),
]


def cronjob_api(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(CRONJOB_API + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + CRONJOB_API_KEY)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode()
        return json.loads(body) if body else {}


def github_dispatch_headers():
    # These are sent by cron-job.org on every fire. GitHub requires a
    # User-Agent; the PAT authorises the workflow_dispatch.
    return {
        "Authorization": "Bearer " + GH_PAT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "cron-job.org",
    }


def build_job(title, workflow, hour, minute):
    return {
        "job": {
            "url": "https://api.github.com/repos/%s/actions/workflows/%s/dispatches"
                   % (REPO, workflow),
            "enabled": True,
            "title": title,
            "saveResponses": True,
            "requestMethod": 1,                 # 1 = POST
            "extendedData": {
                "headers": github_dispatch_headers(),
                "body": json.dumps({"ref": "master"}),
            },
            "schedule": {
                "timezone": "America/New_York",
                "hours": [hour],
                "minutes": [minute],
                "mdays": [-1],                  # -1 = every
                "months": [-1],
                "wdays": [1, 2, 3, 4, 5],       # Mon-Fri
            },
        }
    }


def main():
    if not CRONJOB_API_KEY or not GH_PAT:
        sys.exit("Set CRONJOB_API_KEY and GH_DISPATCH_PAT env vars first "
                 "(see the docstring at the top of this file).")

    existing = {j.get("title") for j in cronjob_api("GET", "/jobs").get("jobs", [])}
    print("%d existing cron-job.org job(s).\n" % len(existing))

    created = skipped = failed = 0
    for title, workflow, hour, minute in JOBS:
        if title in existing:
            print("  skip   %s  (already exists)" % title)
            skipped += 1
            continue
        try:
            res = cronjob_api("PUT", "/jobs", build_job(title, workflow, hour, minute))
            print("  create %s  -> jobId %s" % (title, res.get("jobId")))
            created += 1
        except urllib.error.HTTPError as e:
            print("  FAIL   %s  -> HTTP %d: %s"
                  % (title, e.code, e.read().decode()[:200]))
            failed += 1

    print("\n%d created, %d skipped, %d failed." % (created, skipped, failed))
    print("Verify at https://console.cron-job.org/")


if __name__ == "__main__":
    main()
