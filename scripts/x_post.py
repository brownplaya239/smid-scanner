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
from social_cards import KINDS, _day_label, _pct  # noqa: E402

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


def _fit(head, items, tail):
    """head + as many item lines as fit in 280 (X-weighted) + tail."""
    keep = list(items)
    while keep:
        text = "\n".join(head + keep + tail)
        if x_len(text) <= 280:
            return text
        keep.pop()
    return "\n".join(head + tail)


def flow_caption(f):
    head = [f"Biggest options bets today ({_day_label(f['day'])})", ""]
    items = []
    for label, rows in (("Calls", f["calls"]), ("Puts", f["puts"])):
        if rows:
            items.append(label + ":")
            for b in f["body"]:
                if (b[1] == "CALLS") == (label == "Calls"):
                    items.append(f"${b[0]} — {b[2]}")
    return _fit(head, items, ["", "Full flow: tickerdesk.io"])


def earnings_week_caption(f):
    by_day = {}
    for b in f["body"]:
        by_day.setdefault(b[1].split()[0], []).append("$" + b[0])
    head = [f"Earnings this week (week of {_day_label(f['week_of'])})", ""]
    items = [f"{d}: {' '.join(t)}" for d, t in by_day.items()]
    return _fit(head, items,
                ["", "Options-implied vs typical move for each on the "
                     "card. Full calendar: tickerdesk.io"])


def earnings_preview_caption(f):
    head = ["Earnings: what options are pricing vs the usual move", ""]
    items = [f"${b[0]} ±{b[2].lstrip('±')} priced vs ±{b[3].lstrip('±')} "
             "usual" for b in f["body"]]
    return _fit(head, items,
                ["", "We'll post how they actually moved. tickerdesk.io"])


def momentum_caption(f):
    title = {"stockbee": "Biggest 5-day gainers",
             "qm": "Strongest 1-month gainers"}[f["which"]]
    head = [f"{title} ({_day_label(f['date'])})", ""]
    items = [f"${b[0]} {b[2]}" for b in f["body"][:8]]
    return _fit(head, items,
                ["", f"All {f['count']} names: tickerdesk.io"])


def levels_caption(f):
    head = [f"SPY & QQQ levels for {_day_label(f['day'])}", ""]
    items = [f"${b[0]} {b[1].lstrip('$')} · exp. move {b[2]} · "
             f"walls {b[4]}" for b in f["body"]]
    gam = {b[5] for b in f["body"]}
    tail = ["", f"Dealer gamma: {'/'.join(sorted(gam)).lower()}",
            "tickerdesk.io"]
    return _fit(head, items, tail)


CAPTIONS = {
    "callouts": lambda f: callouts_caption(f),
    "flow": flow_caption,
    "earnings_week": earnings_week_caption,
    "earnings_preview": earnings_preview_caption,
    "stockbee": momentum_caption,
    "qm": momentum_caption,
    "levels": levels_caption,
}


def _post_date(kind, f):
    return (f.get("day", {}).get("date") if kind == "callouts"
            else f.get("date") or f.get("day") or f.get("week_of"))


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
    ap.add_argument("kind", choices=list(CAPTIONS))
    ap.add_argument("--date")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    path, alt, facts = KINDS[a.kind](a)
    date = _post_date(a.kind, facts)
    text = CAPTIONS[a.kind](facts)
    print("card:", path)
    print("caption (%d chars as X counts):\n%s\n" % (x_len(text), text))

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
    import tweepy
    try:
        resp = client.create_tweet(text=text, media_ids=[media.media_id],
                                   user_auth=True)
    except tweepy.errors.HTTPException as e:
        # X explains billing/permission refusals in the body (e.g. 402:
        # no credits / plan not attached to this app's project).
        body = ""
        try:
            body = e.response.text[:600]
        except Exception:
            pass
        sys.exit(f"X refused the post: HTTP {e.response.status_code} {body}")
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
