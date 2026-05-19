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

import pytz

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ET_TZ = pytz.timezone("America/New_York")
_BASE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(_BASE, "docs", "reports", "economic_calendar.json")
FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"


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

    events = []
    for e in root.findall("event"):
        if _text(e, "country") != "USD":
            continue                                         # US events only
        events.append({
            "date":     _text(e, "date"),                    # MM-DD-YYYY
            "time":     _text(e, "time"),                    # e.g. "8:30am"
            "title":    _text(e, "title"),
            "impact":   _text(e, "impact"),                  # Low/Medium/High/Holiday
            "forecast": _text(e, "forecast"),
            "previous": _text(e, "previous"),
            "actual":   _text(e, "actual"),
        })
    print(f"  {len(events)} US events this week")

    now = datetime.now(ET_TZ)
    payload = {
        "updated": now.isoformat(timespec="seconds"),
        "tz":      "America/New_York",
        "events":  events,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    print(f"  Wrote economic_calendar.json")


if __name__ == "__main__":
    run()
