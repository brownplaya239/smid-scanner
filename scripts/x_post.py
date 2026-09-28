#!/usr/bin/env python3
"""x_post.py — publish a TickerDesk card to X.

    python scripts/x_post.py callouts [--date YYYY-MM-DD] [--dry-run]

Builds the card (social_cards.py) and caption from the site's own JSON,
then posts image + caption + alt text to X via the API (OAuth 1.0a user
context, credentials from env: X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN,
X_ACCESS_SECRET).

Safety:
  - identity check: the credentials must belong to X_EXPECTED_HANDLE
    (default tickerdeskio) or nothing is posted
  - one post per (kind, date): data/social_log.jsonl records every post
  - SOCIAL_PAUSE=1 (repo variable) stops all posting
  - --dry-run renders + prints the caption and runs the identity check
    without posting
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from social_cards import callouts_card, _day_label, _pct  # noqa: E402

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(_BASE, "data", "social_log.jsonl")
EXPECTED = os.environ.get("X_EXPECTED_HANDLE", "tickerdeskio").lower()


def _posted(kind, date):
    if not os.path.exists(LOG):
        return None
    with open(LOG, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("kind") == kind and r.get("date") == date \
                    and r.get("tweet_id"):
                return r
    return None


def callouts_caption(facts):
    day, top = facts["day"], facts["top"]
    lines = [f"Options callouts — how they did ({_day_label(day['date'])})",
             ""]
    for t in top[:3]:
        lines.append(f"{t['label']}: ${t['entry']:.2f} → ${t['peak']:.2f} "
                     f"({_pct(t['peak_pct'])})")
    lines += ["",
              f"{day['hit_100']} of {day['n']} callouts hit +100% at peak.",
              "Every callout, winners and losers: tickerdesk.io"]
    tags = []
    for t in top:
        tag = "$" + t["ticker"]
        if tag not in tags:
            tags.append(tag)
    text = "\n".join(lines)
    tagline = " ".join(tags[:3])
    if x_len(text) + 2 + len(tagline) <= 280:
        text += "\n\n" + tagline
    if x_len(text) > 280:
        sys.exit(f"caption too long for X ({x_len(text)} weighted chars)")
    return text


def x_len(text):
    """Length as X counts it: every link (here tickerdesk.io) = 23."""
    return len(text) + text.count("tickerdesk.io") * (23 - len("tickerdesk.io"))


def _clients():
    import tweepy
    keys = [os.environ.get(k, "") for k in
            ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")]
    if not all(keys):
        return None, None
    client = tweepy.Client(consumer_key=keys[0], consumer_secret=keys[1],
                           access_token=keys[2], access_token_secret=keys[3])
    auth = tweepy.OAuth1UserHandler(keys[0], keys[1], keys[2], keys[3])
    return client, tweepy.API(auth)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["callouts"])
    ap.add_argument("--date")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    path, alt, facts = callouts_card(a.date)
    date = facts["day"]["date"]
    text = callouts_caption(facts)
    print("card:", path)
    print("caption (%d chars):\n%s\n" % (len(text), text))

    if os.environ.get("SOCIAL_PAUSE") == "1":
        print("SOCIAL_PAUSE=1 — posting disabled.")
        return
    prior = _posted(a.kind, date)
    if prior and not a.dry_run:
        print(f"already posted {a.kind} {date}: {prior.get('url')}")
        return

    client, api = _clients()
    if client is None:
        print("X credentials not set — dry run only.")
        return
    me = client.get_me(user_auth=True).data
    handle = (me.username or "").lower()
    print(f"credentials belong to @{me.username}")
    if handle != EXPECTED:
        sys.exit(f"REFUSING to post: keys are for @{me.username}, "
                 f"expected @{EXPECTED}")
    if a.dry_run:
        print("dry run — identity OK, nothing posted.")
        return

    media = api.media_upload(filename=path)
    try:
        api.create_media_metadata(media.media_id, alt[:1000])
    except Exception as e:  # alt text is best-effort
        print("alt text not set:", str(e)[:100])
    resp = client.create_tweet(text=text, media_ids=[media.media_id],
                               user_auth=True)
    tid = resp.data["id"]
    url = f"https://x.com/{me.username}/status/{tid}"
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": a.kind, "date": date, "tweet_id": tid, "url": url,
            "handle": me.username}) + "\n")
    print("POSTED:", url)


if __name__ == "__main__":
    main()
