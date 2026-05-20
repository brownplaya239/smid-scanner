"""
earnings_whisper_tweet.py — Auto-discover and cache the latest @eWhispers
weekly Most Anticipated Earnings tweet image.

X.com itself blocks scraping (403 to every UA, including crawler bots),
but Twitter's syndication endpoints — the ones widgets.js uses for
embeds — remain publicly callable with no auth required:

  syndication.twitter.com/srv/timeline-profile/screen-name/eWhispers
    Returns a Next.js page whose __NEXT_DATA__ JSON contains the latest
    ~10 tweets from the account with full metadata. We scan this for
    the most recent "Most Anticipated" / "#earnings for the week of"
    post — that's the chart image we want.

  cdn.syndication.twimg.com/tweet-result?id={tweet_id}
    Returns full tweet JSON for any tweet ID, including
    mediaDetails[0].media_url_https — the direct pbs.twimg.com URL
    of the attached image. No auth, no token, no scraping involved.

Together: we discover the latest weekly tweet by polling the timeline,
then fetch its image URL via tweet-result. Result is written to
docs/reports/whisper_tweet.json. The momentum workflow runs this
daily so a new weekly tweet shows up on the dashboard within hours
of being posted.

No third-party deps. urllib only.
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime

import pytz

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ET = pytz.timezone("America/New_York")
_BASE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(_BASE, "docs", "reports", "whisper_tweet.json")

HDRS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0 Safari/537.36")}

# Patterns that identify a Most Anticipated weekly tweet. EW historically
# uses two phrasings — match either. The accompanying t.co URL is the
# permalink to their interactive web chart, but the image is always
# attached as a media item we'll find via tweet-result.
ANTICIPATED_PATTERNS = [
    r"#earnings for the week of",
    r"Most Anticipated Earnings",
]


def _fetch_timeline():
    """Pull @eWhispers's recent timeline via syndication (no auth)."""
    url = ("https://syndication.twitter.com/srv/timeline-profile/"
           "screen-name/eWhispers")
    req = urllib.request.Request(url, headers=HDRS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def _parse_next_data(html):
    m = re.search(r'id="__NEXT_DATA__"[^>]*>([\s\S]*?)</script>', html)
    if not m:
        return None
    return json.loads(m.group(1))


def _walk_tweets(o, out, depth=0):
    """Recursively scan __NEXT_DATA__ for entries containing tweet text +
    id. Timeline entries live at varying depths so a generic walker is
    the most robust read."""
    if depth > 10:
        return
    if isinstance(o, dict):
        # An entry that looks like a tweet
        if o.get("type") == "tweet" and isinstance(o.get("content"), dict):
            tw = o["content"].get("tweet", {})
            eid = o.get("entry_id", "")
            # entry_id is like "tweet-1234567890" — extract the numeric id
            m = re.match(r"tweet-(\d+)", eid)
            tid = m.group(1) if m else (
                tw.get("id_str") or str(tw.get("id")) if tw.get("id") else None)
            text = tw.get("text") or tw.get("full_text") or ""
            created = tw.get("created_at") or ""
            if tid and text:
                out.append({"id": tid, "text": text, "created": created})
        for v in o.values():
            _walk_tweets(v, out, depth + 1)
    elif isinstance(o, list):
        for v in o:
            _walk_tweets(v, out, depth + 1)


def find_latest_anticipated_tweet():
    """Scan the @eWhispers timeline for the most-recent Most Anticipated
    weekly tweet. Returns {id, text, created} or None."""
    html = _fetch_timeline()
    data = _parse_next_data(html)
    if not data:
        return None
    tweets = []
    _walk_tweets(data, tweets)
    # Match by text pattern
    matches = []
    pat = re.compile("|".join(ANTICIPATED_PATTERNS), re.IGNORECASE)
    for t in tweets:
        if pat.search(t["text"]):
            matches.append(t)
    if not matches:
        return None
    # Highest numeric tweet id = most recent
    matches.sort(key=lambda x: int(x["id"]), reverse=True)
    return matches[0]


def _syndication_token(tweet_id):
    """Twitter's reverse-engineered token formula. Required by the
    tweet-result endpoint — without it the endpoint returns an empty
    object. The exact value isn't validated server-side; any string
    derived from the id works, but matching Twitter's own algorithm
    is safest against future tightening. Source: published widget.js
    de-obfuscation, used by every public embed."""
    val = (int(tweet_id) / 1e15) * 3.141592653589793
    # toString(36) equivalent for the integer part + fractional approximation
    s = ""
    n = val
    while n >= 1:
        d = int(n % 36)
        s = ("0123456789abcdefghijklmnopqrstuvwxyz"[d]) + s
        n = n // 36
    frac = val - int(val)
    if frac > 0:
        s += "."
        for _ in range(11):
            frac *= 36
            d = int(frac)
            s += "0123456789abcdefghijklmnopqrstuvwxyz"[d]
            frac -= d
    # Twitter strips '0' chars and '.' from the result
    return re.sub(r"(0+|\.)", "", s)[:11] or "1"


def fetch_tweet_image(tweet_id):
    """Pull the tweet's media URL via Twitter's public syndication
    tweet-result endpoint. Returns the pbs.twimg.com URL or None."""
    token = _syndication_token(tweet_id)
    url = (f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}"
           f"&lang=en&token={token}")
    req = urllib.request.Request(url, headers=HDRS)
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode("utf-8"))
    media = d.get("mediaDetails") or []
    for m in media:
        if m.get("type") == "photo" and m.get("media_url_https"):
            return {
                "image_url": m["media_url_https"],
                "image_url_large": m["media_url_https"] + "?format=jpg&name=large",
                "text":      d.get("text", ""),
                "created":   d.get("created_at", ""),
                "user":      (d.get("user") or {}).get("screen_name", "eWhispers"),
                "favorites": d.get("favorite_count"),
                "tickers":   _extract_tickers(d.get("text", "")),
            }
    return None


def _extract_tickers(text):
    """Pull $TICKER cashtags out of the tweet text — handy for cross-
    referencing the anticipated names against our own universe."""
    return re.findall(r"\$([A-Z]{1,6})\b", text or "")


def _week_of_from_text(text):
    """The tweet's "Week of May 18, 2026" → "2026-05-18"."""
    m = re.search(r"week of\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})", text or "",
                  re.IGNORECASE)
    if not m:
        return None
    try:
        # Normalize "May 18, 2026" / "May 18 2026" to YYYY-MM-DD
        dt = datetime.strptime(m.group(1).replace(",", ""), "%B %d %Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return None


def run():
    """Best-effort weekly tweet discovery. Always returns exit code 0 —
    Twitter's syndication endpoint rate-limits aggressively and a 429
    here should NEVER fail the parent workflow (publishing swing report,
    momentum scans, etc. all live in the same job and must run regardless).
    The existing whisper_tweet.json stays in place until the next
    successful fetch."""
    print("Finding latest @eWhispers Most Anticipated tweet...")
    try:
        tweet = find_latest_anticipated_tweet()
    except urllib.error.HTTPError as e:
        print(f"  Twitter syndication returned HTTP {e.code} — "
              f"keeping prior whisper_tweet.json untouched, exiting 0.")
        return
    except Exception as e:
        print(f"  Timeline fetch failed ({type(e).__name__}: {e}) — "
              f"keeping prior whisper_tweet.json untouched, exiting 0.")
        return
    if not tweet:
        print("  No matching tweet found in current timeline — keeping "
              "prior whisper_tweet.json untouched.")
        return
    print(f"  Found tweet {tweet['id']}  ({tweet['created']})")
    print(f"  Text: {tweet['text'][:100]}...")

    try:
        media = fetch_tweet_image(tweet["id"])
    except Exception as e:
        print(f"  Tweet-result fetch failed ({e}) — keeping prior json.")
        return
    if not media:
        print("  Tweet has no attached photo — skipping (prior json kept).")
        return

    payload = {
        "tweet_id":     tweet["id"],
        "tweet_url":    f"https://x.com/eWhispers/status/{tweet['id']}",
        "image_url":    media["image_url_large"],
        "image_url_md": media["image_url"],
        "week_of":      _week_of_from_text(media["text"]),
        "posted_at":    media["created"],
        "text":         media["text"],
        "tickers":      media["tickers"],
        "favorites":    media["favorites"],
        "updated":      datetime.now(ET).isoformat(timespec="seconds"),
        "source":       "syndication.twitter.com (no auth, public timeline)",
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    print(f"  Wrote whisper_tweet.json  week_of={payload['week_of']}  "
          f"image={media['image_url']}")


if __name__ == "__main__":
    # Wrap once more at top level so ANY exception falls out as exit 0.
    # The publish step (swing report, scans, calendar) must run.
    try:
        run()
    except Exception as e:
        print(f"  Unexpected error ({type(e).__name__}: {e}) — exiting 0 "
              f"to keep downstream workflow steps running.")
