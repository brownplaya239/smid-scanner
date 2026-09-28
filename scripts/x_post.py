#!/usr/bin/env python3
"""x_post.py — publish a TickerDesk card to X.

    python scripts/x_post.py KIND [--date YYYY-MM-DD] [--dry-run]
                                  [--scheduled] [--bundle-out F | --bundle F]

KIND: callouts flow earnings_week earnings_preview earnings_receipt
      stockbee qm levels weekly_recap

Builds the card (social_cards.py) and caption from the site's own JSON,
then posts image + caption + alt text to X via the API (OAuth 1.0a user
context, credentials from env: X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN,
X_ACCESS_SECRET).

Two-step use (what the workflow does, so a reviewer approves exactly
what gets posted):
    --dry-run --bundle-out post.json   render + caption + gate, no post
    --bundle post.json                 post that exact bundle

Publishing gate (every run):
  BLOCK  (never posts)  empty card, stale source data on a scheduled
         run, not a trading session, identical caption already posted
  REVIEW (needs a human) outlier numbers worth double-checking before
         they go out under the brand — huge prints, 1000%+ peaks,
         penny entries, 20%+ implied moves, extreme momentum. With
         SOCIAL_AUTO != 1 every post is reviewed anyway.

Safety:
  - identity check: the credentials must belong to X_EXPECTED_HANDLE
    (default tickerdeskio) or nothing is posted
  - one post per (kind, date): data/social_posts/ holds one record per
    post (older posts: data/social_log.jsonl)
  - SOCIAL_PAUSE=1 (repo variable) stops all posting
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
from social_cards import (KINDS, SOCIAL_POSTS, _day_label, _pct,  # noqa
                          social_log_rows)
EXPECTED = os.environ.get("X_EXPECTED_HANDLE", "tickerdeskio").lower()
ET = timezone(timedelta(hours=-4))

# Where each post sends people. api.tickerdesk.io/go/<kind> 302s to the
# matching tab with UTM tags (worker.js), so clicks are attributable per
# content type and the link in the post stays short and readable.
LINK = "api.tickerdesk.io/go/{kind}"


def link(kind):
    return LINK.format(kind=kind)


def _now_et():
    # ET offset via zoneinfo when available (handles EST in winter).
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now(ET)


def _log_rows():
    return social_log_rows()


def _posted(kind, date):
    for r in _log_rows():
        if r.get("kind") == kind and r.get("date") == date \
                and r.get("tweet_id"):
            return r
    return None


# ----------------------------------------------------------- length

_URL = re.compile(r"(https?://\S+|\b[\w.-]+\.(?:io|com)(?:/\S*)?)")


def x_len(text):
    """Length as X counts it: every link is 23 characters."""
    return len(_URL.sub("x" * 23, text))


def _fit(head, items, tail):
    """head + as many item lines as fit in 280 (X-weighted) + tail."""
    keep = list(items)
    while keep:
        text = "\n".join(head + keep + tail)
        if x_len(text) <= 280:
            return text
        keep.pop()
    return "\n".join(head + tail)


def _tags(tickers, text, n=3):
    tags = []
    for t in tickers:
        tag = "$" + t
        if tag not in tags and tag not in text:
            tags.append(tag)
    tagline = " ".join(tags[:n])
    if tagline and x_len(text) + 2 + len(tagline) <= 280:
        return text + "\n\n" + tagline
    return text


# ----------------------------------------------------------- captions

def callouts_caption(f):
    day, top = f["day"], f["top"]
    head = [f"Options callouts: how they did ({_day_label(day['date'])})",
            ""]
    items = [f"{t['label']}: ${t['entry']:.2f} → ${t['peak']:.2f} "
             f"({_pct(t['peak_pct'])})" for t in top[:3]]
    tail = ["",
            f"{day['hit_100']} of {day['n']} hit +100% at peak. Median "
            f"peak {_pct(day['median_peak_pct'])}, median now "
            f"{_pct(day['median_last_pct'])}.",
            f"Every callout, winners and losers: {link('callouts')}"]
    text = _fit(head, items, tail)
    return _tags([t["ticker"] for t in top], text)


def flow_caption(f):
    head = [f"Unusual options activity, midday {_day_label(f['day'])}", "",
            "Biggest premium with volume 2x+ open interest:"]
    calls = [b for b in f["body"] if b[1] == "Calls"]
    puts = [b for b in f["body"] if b[1] == "Puts"]
    order = [b for pair in zip(calls, puts) for b in pair]
    order += [b for b in calls + puts if b not in order]
    items = [f"${b[0]}: {b[2]} ({b[4]} OI)" for b in order]
    tail = ["", "Buy/sell side not classified: size + newness, not "
                "direction.", link("flow")]
    return _fit(head, items, tail)


def earnings_week_caption(f):
    by_day = {}
    for b in f["body"]:
        by_day.setdefault(b[1].split()[0], []).append("$" + b[0])
    head = [f"Earnings this week (week of {_day_label(f['week_of'])})", ""]
    items = [f"{d}: {' '.join(t)}" for d, t in by_day.items()]
    return _fit(head, items,
                ["", "Card: what options price vs each stock's usual move, "
                     f"plus 1M/YTD/1Y. {link('earnings_week')}"])


def _preview_lead(names):
    ratios = [n["implied"] / n["realized_med"] for n in names]
    rich = sum(1 for r in ratios if r >= 1.3)
    cheap = sum(1 for r in ratios if r <= 0.85)
    if rich >= max(2, len(ratios) // 2):
        return "Options are pricing bigger-than-usual earnings moves."
    if cheap >= max(2, len(ratios) // 2):
        return "Options are pricing smaller moves than these stocks " \
               "usually make on earnings."
    return "What options are pricing into these reports vs how each " \
           "stock usually moves."


def earnings_preview_caption(f):
    head = [_preview_lead(f["names"]), ""]
    def when(n):
        dt = datetime.strptime(n["date"], "%Y-%m-%d")
        return f"{dt:%a} {n.get('session') or ''}".strip()
    items = [f"${n['t']} ({when(n)}): ±{n['implied']:.1f}% priced vs "
             f"±{n['realized_med']:.1f}% usual" for n in f["names"]]
    return _fit(head, items,
                ["", "We'll post the actual moves after they report. "
                     + link("earnings_preview")])


def earnings_receipt_caption(f):
    n = len(f["body"])
    head = [f"Earnings receipts: {f['beat']} of {n} moved more than "
            "options priced.", ""]
    items = [f"${b[0]}: priced {b[2]}, moved {b[4]}" for b in f["body"]]
    return _fit(head, items,
                ["", "Every preview gets a receipt, hits and misses. "
                     + link("earnings_receipt")])


def momentum_caption(f):
    title = {"stockbee": "Biggest 5-day gainers",
             "qm": "Strongest 1-month gainers"}[f["which"]]
    new = f.get("new") or []
    head = [f"{title} ({_day_label(f['date'])})",
            f"{f['count']} names on the list, {len(new)} new this week.", ""]
    items = [f"${b[0].replace('  NEW', '')} {b[2]}"
             + (" (new)" if b[0].endswith("NEW") else "")
             for b in f["body"][:8]]
    return _fit(head, items,
                ["", f"Past strength, not picks. Full list: "
                     f"{link(f['which'])}"])


def levels_caption(f):
    base = datetime.strptime(f["base"], "%Y-%m-%d")
    head = [f"SPY & QQQ trading roadmap for {_day_label(f['day'])}",
            f"Based on {base:%A}'s close:", ""]
    items = []
    for b in f["body"]:
        items.append(f"${b[0]} {b[1].lstrip('$')} · expected move "
                     f"{b[2].split(' ')[0]} ({b[3]}) · walls {b[4]}")
    gam = sorted({b[6].lower() for b in f["body"]})
    tail = ["", "Estimated dealer gamma: " + "/".join(gam)
            + (" (tends to dampen moves)" if gam == ["positive"] else
               " (tends to amplify moves)" if gam == ["negative"] else "")]
    if f.get("check"):
        tail.append(f"{base:%a} check: " + "; ".join(f["check"]))
    tail.append(link("levels"))
    return _fit(head, items, tail)


def weekly_recap_caption(f):
    best = sorted((t for d in f["days"] for t in d.get("top") or []),
                  key=lambda t: -t["peak_pct"])[:3]
    head = [f"This week's options callouts, all {f['n']} of them:", "",
            f"{f['hit_50']} hit +50% at peak",
            f"{f['hit_100']} hit +100% at peak", ""]
    items = [f"{t['label']} {_pct(t['peak_pct'])}" for t in best]
    return _fit(head, ["Best:"] + items if items else [],
                ["", "Peak = best exit available, not typical. Full "
                     f"record: {link('weekly_recap')}"])


CAPTIONS = {
    "callouts": callouts_caption,
    "flow": flow_caption,
    "earnings_week": earnings_week_caption,
    "earnings_preview": earnings_preview_caption,
    "earnings_receipt": earnings_receipt_caption,
    "stockbee": momentum_caption,
    "qm": momentum_caption,
    "levels": levels_caption,
    "weekly_recap": weekly_recap_caption,
}


def _post_date(kind, f):
    if kind == "callouts":
        return f.get("day", {}).get("date")
    return f.get("date") or f.get("day") or f.get("week_of")


# What each kind stores in the post log beyond the basics — the
# follow-up posts (receipts, levels check) read these back.
def _log_extra(kind, f):
    if kind == "earnings_preview":
        return {"names": [{k: n.get(k) for k in
                           ("t", "date", "session", "implied",
                            "realized_med", "n_reports")}
                          for n in f["names"]]}
    if kind == "earnings_receipt":
        return {"keys": f["keys"]}
    if kind == "levels":
        return {"em": f["em"], "base": f["base"]}
    return {}


# ------------------------------------------------------------- gate

SESSION_KINDS = {"callouts", "flow", "levels", "stockbee", "qm",
                 "earnings_receipt", "earnings_preview", "weekly_recap"}


def _age_h(iso):
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except Exception:
        return None


def _money(s):
    """'$12.3M' -> 12.3e6 (card strings back to numbers)."""
    m = re.match(r"\$([\d.]+)([KMB]?)", s or "")
    if not m:
        return 0
    return float(m.group(1)) * {"": 1, "K": 1e3, "M": 1e6,
                                "B": 1e9}[m.group(2)]


def gate(kind, f, text, scheduled):
    blocks, review = [], []
    today = _now_et().date()
    try:
        import exchange_calendar as xc
        session_today = xc.is_session(today)
    except Exception:
        session_today = today.weekday() < 5
    body = f.get("body") or f.get("top") or f.get("days")
    if not body:
        blocks.append("card has no rows")
    if kind in SESSION_KINDS and not session_today and scheduled:
        blocks.append(f"{today} is not a trading session")

    # freshness — strict on scheduled runs, advisory on manual ones
    stale = []
    t = today.isoformat()
    if kind == "flow":
        if f.get("day") != t:
            stale.append(f"flow list is for {f.get('day')}, not {t}")
        a = _age_h(f.get("generated"))
        if a is not None and a > 2:
            stale.append(f"flow list is {a:.1f}h old")
    elif kind == "callouts" and f["day"]["date"] != t:
        stale.append(f"latest callout day is {f['day']['date']}, not {t}")
    elif kind in ("stockbee", "qm") and f.get("date") != t:
        stale.append(f"latest {kind} run is {f.get('date')}, not {t}")
    elif kind == "levels":
        try:
            import exchange_calendar as xc
            want = xc.previous_session(today).isoformat()
        except Exception:
            want = None
        if want and f.get("base") != want:
            stale.append(f"prices are from {f.get('base')}, expected {want}")
        if any(b[2] == "—" for b in f["body"]):
            stale.append("expected move missing")
    elif kind == "weekly_recap" and f.get("date") != t:
        stale.append(f"latest callout day is {f.get('date')}, not {t}")
    elif kind == "earnings_preview":
        if min(n["date"] for n in f["names"]) <= t:
            stale.append("preview includes names already reporting today")
    (blocks if scheduled else review).extend(stale)

    # outliers worth a human look before they go out under the brand
    if kind == "flow":
        for b in f["body"]:
            if _money(b[2]) >= 50e6:
                review.append(f"{b[0]} {b[2]} premium — check it isn't a "
                              "spread, roll or hedge before calling it "
                              "unusual")
    if kind in ("callouts", "weekly_recap"):
        tops = (f["top"][:3] if kind == "callouts" else
                sorted((t for d in f["days"] for t in d.get("top") or []),
                       key=lambda t: -t["peak_pct"])[:3])
        for tp in tops:
            if tp["peak_pct"] >= 1000:
                review.append(f"{tp['label']} peak {tp['peak_pct']:.0f}%")
            if tp["entry"] < 0.10:
                review.append(f"{tp['label']} entry ${tp['entry']:.2f} "
                              "(penny option: % gains overstate)")
    if kind == "earnings_preview":
        for n in f["names"]:
            if n["implied"] >= 20 or n["implied"] / n["realized_med"] >= 3:
                review.append(f"{n['t']} implied ±{n['implied']:.1f}% vs "
                              f"±{n['realized_med']:.1f}% usual")
    if kind == "earnings_receipt":
        for b in f["body"]:
            if abs(float(b[4].rstrip("%"))) >= 25:
                review.append(f"{b[0]} moved {b[4]} — confirm no split/"
                              "bad print")
    if kind in ("stockbee", "qm"):
        for b in f["body"]:
            if float(b[2].strip("+%")) >= 300:
                review.append(f"{b[0]} {b[2]} — confirm no split/reverse "
                              "split")
    if kind == "levels":
        for b, pct in zip(f["body"], f["em"].values()):
            if pct and pct > 2:
                review.append(f"{b[0]} expected move {pct}% looks high")

    # identical caption already posted (any kind, any date)
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()
    if any(r.get("text_sha1") == h for r in _log_rows()):
        blocks.append("identical caption already posted")
    if x_len(text) > 280:
        blocks.append(f"caption too long ({x_len(text)})")
    return blocks, review


# ----------------------------------------------------------- posting

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


def _gh_out(**kv):
    p = os.environ.get("GITHUB_OUTPUT")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            for k, v in kv.items():
                f.write(f"{k}={v}\n")


def _summary(md):
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(md + "\n")


def _card_url(path):
    """Upload the card to R2 (cards/<run>_<name>.png) so Slack and the
    run summary can show it inline via api.tickerdesk.io/cards/. The run
    id in the name keeps every uploaded card immutable (no stale cache
    when a card is re-rendered). Best effort: None if it can't upload."""
    run = os.environ.get("GITHUB_RUN_ID")
    if not (run and os.environ.get("CLOUDFLARE_API_TOKEN")):
        return None
    try:
        import r2_sync
        name = f"{run}_{os.path.basename(path)}"
        _, err = r2_sync.put_key(path, "cards/" + name)
        if err:
            print("card upload failed:", err)
            return None
        return "https://api.tickerdesk.io/cards/" + name
    except Exception as e:
        print("card upload failed:", str(e)[:120])
        return None


def _slack_esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _run_url():
    return (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
            f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/"
            f"{os.environ.get('GITHUB_RUN_ID', '')}")


def review_blocks(rec, buttons, footer=None):
    """Slack blocks for a review draft. Same layout as reviewBlocks() in
    worker.js (which re-renders it after clicks) — keep the two in step."""
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text":
               f"*{rec['kind']}* · {rec['date']}: {rec['status_line']}"}}]
    if rec.get("review"):
        blocks.append({"type": "section", "text": {
            "type": "mrkdwn", "text": "*Check before approving:*\n"
            + "\n".join(f"• {_slack_esc(m)}" for m in rec["review"])}})
    edited = (f"\n_Edited by <@{rec['edited_by']}> · {x_len(rec['text'])}"
              "/280_" if rec.get("edited_by") else "")
    blocks.append({"type": "section", "text": {
        "type": "mrkdwn", "text": "```" + _slack_esc(rec["text"]) + "```"
        + edited}})
    if rec.get("card_url"):
        blocks.append({"type": "image", "image_url": rec["card_url"],
                       "alt_text": (rec.get("alt") or rec["kind"])[:1900]})
    if buttons:
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "action_id": "td_approve", "style": "primary",
             "text": {"type": "plain_text", "text": "Approve Post"},
             "value": rec["run_id"]},
            {"type": "button", "action_id": "td_edit",
             "text": {"type": "plain_text", "text": "Edit Caption"},
             "value": rec["run_id"]},
            {"type": "button", "action_id": "td_reject", "style": "danger",
             "text": {"type": "plain_text", "text": "Reject"},
             "value": rec["run_id"]}]})
    ctx = ([footer] if footer else []) + [f"<{rec['run_url']}|Open the run>"]
    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn", "text": "  ·  ".join(ctx)}]})
    return blocks


def _slack_api(method, body):
    """Slack Web API with the TickerDesk Preview bot token. Best effort."""
    tok = os.environ.get("SLACK_BOT_TOKEN")
    if not tok:
        return None
    import urllib.request
    req = urllib.request.Request(
        "https://slack.com/api/" + method,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + tok,
                 "Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.loads(r.read())
        if not j.get("ok"):
            print(f"slack {method} failed:", j.get("error"))
        return j
    except Exception as e:
        print(f"slack {method} failed:", str(e)[:120])
        return None


REVIEW_KEY = "social_reviews/{run}.json"


def slack_review(b, card_url):
    """Interactive draft in #social-review-tickerdesk: Approve Post / Edit
    Caption / Reject (handled by the worker's /slack/interact). Saves the
    review record to R2 so the worker can update it and the post job can
    pick up an edited caption. Returns False if the bot isn't set up (the
    caller then falls back to the one-way webhook preview)."""
    channel = os.environ.get("SLACK_REVIEW_CHANNEL")
    run = os.environ.get("GITHUB_RUN_ID")
    if not (os.environ.get("SLACK_BOT_TOKEN") and channel and run):
        return False
    if b["blocks"]:
        _slack_api("chat.postMessage", {
            "channel": channel,
            "text": f"Not posting {b['kind']} {b['date']}",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text":
                f"Not posting *{b['kind']}* · {b['date']} (publishing "
                "gate):\n" + "\n".join(f"• {_slack_esc(m)}"
                                         for m in b["blocks"])}},
                {"type": "context", "elements": [{"type": "mrkdwn",
                 "text": f"<{_run_url()}|Open the run>"}]}]})
        return True
    dry = os.environ.get("SOCIAL_DRY_RUN") == "true"
    auto = os.environ.get("SOCIAL_AUTO") == "1" and not b["review"]
    rec = {"run_id": run, "kind": b["kind"], "date": b["date"],
           "text": b["text"], "alt": b["alt"], "review": b["review"],
           "card_url": card_url, "run_url": _run_url(), "channel": channel,
           "status": "pending" if not (dry or auto) else
           ("dry_run" if dry else "auto"),
           "status_line": ("DRY RUN (preview only, won't post)" if dry else
                           "auto-posting (no review flags)" if auto else
                           "waiting for your approval")}
    j = _slack_api("chat.postMessage", {
        "channel": channel, "text": f"{b['kind']} {b['date']}: "
        f"{rec['status_line']}",
        "blocks": review_blocks(rec, buttons=rec["status"] == "pending")})
    if not (j and j.get("ok")):
        return False
    rec["channel"], rec["ts"] = j["channel"], j["ts"]
    try:
        import r2_sync
        r2_sync.put_json(REVIEW_KEY.format(run=run), rec)
    except Exception as e:
        print("review record not saved:", str(e)[:120])
    print("slack review posted")
    return True


def _review_record():
    run = os.environ.get("GITHUB_RUN_ID")
    if not (run and os.environ.get("CLOUDFLARE_API_TOKEN")):
        return None
    try:
        import r2_sync
        return r2_sync.get_json(REVIEW_KEY.format(run=run))
    except Exception as e:
        print("review record unavailable:", str(e)[:120])
        return None


def _review_done(rec, footer, thread_text):
    """After the post job: close out the Slack draft + reply in thread."""
    if not rec or not rec.get("ts"):
        return
    rec["status_line"] = footer
    _slack_api("chat.update", {"channel": rec["channel"], "ts": rec["ts"],
                               "text": footer,
                               "blocks": review_blocks(rec, False, footer)})
    _slack_api("chat.postMessage", {"channel": rec["channel"],
                                    "thread_ts": rec["ts"],
                                    "text": thread_text})


def slack_preview(b, card_url):
    """Post the review preview to #social-review-tickerdesk (incoming
    webhook in SLACK_WEBHOOK_URL). The GitHub app posts the Approve /
    Reject buttons for the same run in that channel. Best effort."""
    hook = os.environ.get("SLACK_WEBHOOK_URL")
    if not hook:
        return
    run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
               f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/"
               f"{os.environ.get('GITHUB_RUN_ID', '')}")
    kind = f"*{b['kind']}* · {b['date']}"
    if b["blocks"]:
        text = (f"Not posting {kind} (publishing gate):\n"
                + "\n".join(f"• {_slack_esc(m)}" for m in b["blocks"]))
        blocks = [{"type": "section", "text": {"type": "mrkdwn",
                                                "text": text}}]
    else:
        if os.environ.get("SOCIAL_DRY_RUN") == "true":
            status = "DRY RUN (preview only, won't post)"
        elif os.environ.get("SOCIAL_AUTO") == "1" and not b["review"]:
            status = "auto-posting (no review flags)"
        else:
            status = "waiting for your approval"
        text = f"{kind}: {status}"
        blocks = [{"type": "section", "text": {"type": "mrkdwn",
                                                "text": text}}]
        if b["review"]:
            blocks.append({"type": "section", "text": {
                "type": "mrkdwn", "text": "*Check before approving:*\n"
                + "\n".join(f"• {_slack_esc(m)}" for m in b["review"])}})
        blocks.append({"type": "section", "text": {
            "type": "mrkdwn",
            "text": "```" + _slack_esc(b["text"]) + "```"}})
        if card_url:
            blocks.append({"type": "image", "image_url": card_url,
                           "alt_text": b["alt"][:1900]})
    blocks.append({"type": "context", "elements": [{
        "type": "mrkdwn", "text": f"<{run_url}|Open the run>"}]})
    body = json.dumps({"text": text, "blocks": blocks}).encode("utf-8")
    try:
        import urllib.request
        req = urllib.request.Request(hook, data=body, headers={
            "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        print("slack preview sent")
    except Exception as e:
        print("slack preview failed:", str(e)[:120])


def render(a):
    path, alt, facts = KINDS[a.kind](a)
    date = _post_date(a.kind, facts)
    text = CAPTIONS[a.kind](facts)
    blocks, review = gate(a.kind, facts, text, a.scheduled)
    return {"kind": a.kind, "date": date, "card": path, "alt": alt,
            "text": text, "blocks": blocks, "review": review,
            "extra": _log_extra(a.kind, facts),
            "rendered": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", nargs="?", choices=list(CAPTIONS))
    ap.add_argument("--date")
    ap.add_argument("--source", choices=["posted", "site"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--scheduled", action="store_true",
                    help="strict freshness: stale data blocks the post")
    ap.add_argument("--bundle-out", help="write the rendered post here")
    ap.add_argument("--bundle", help="post this pre-rendered bundle")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    review = None
    if a.bundle:
        with open(a.bundle, encoding="utf-8") as f:
            b = json.load(f)
        age = _age_h(b["rendered"])
        review = _review_record()
        if age is not None and age > 12:
            _review_done(review, "Expired: not posted (approved more than "
                         "12h after render)", "Too old to post; skipped.")
            sys.exit(f"bundle rendered {age:.1f}h ago — too old to post")
        if review and review.get("text") and review["text"] != b["text"]:
            # Caption edited in Slack: post the edited text, but it must
            # still pass the checks that depend on the text itself.
            n = x_len(review["text"])
            dup = hashlib.sha1(review["text"].encode("utf-8")).hexdigest()
            if n > 280 or any(r.get("text_sha1") == dup for r in _log_rows()):
                why = (f"edited caption is {n}/280" if n > 280 else
                       "edited caption was already posted")
                _review_done(review, f"Not posted: {why}", f"Not posted: {why}.")
                sys.exit("edited caption rejected: " + why)
            print("using caption edited in Slack")
            b["text"] = review["text"]
            b["extra"]["edited_in_slack"] = True
    else:
        if not a.kind:
            ap.error("kind is required unless --bundle is given")
        try:
            b = render(a)
        except SystemExit as e:
            # Cards raise SystemExit("...") when there's nothing to post
            # (no receipts due, no callout days yet). That's a skip, not
            # a failure — scheduled runs shouldn't page anyone for it.
            if not isinstance(e.code, str):
                raise
            print("nothing to post:", e.code)
            _gh_out(blocked="1", review="0")
            _summary(f"### {a.kind}: nothing to post\n\n{e.code}")
            return
    print("card:", b["card"])
    print("caption (%d chars as X counts):\n%s\n" % (x_len(b["text"]),
                                                    b["text"]))
    for m in b["blocks"]:
        print("BLOCK:", m)
    for m in b["review"]:
        print("REVIEW:", m)
    if a.bundle_out:
        with open(a.bundle_out, "w", encoding="utf-8") as f:
            json.dump(b, f, indent=1)
        _gh_out(blocked="1" if b["blocks"] else "0",
                review="1" if b["review"] else "0")
        card_url = None if b["blocks"] else _card_url(b["card"])
        _summary(f"### {b['kind']} · {b['date']}\n\n```\n{b['text']}\n```\n"
                 + "".join(f"\n- **BLOCK** {m}" for m in b["blocks"])
                 + "".join(f"\n- **REVIEW** {m}" for m in b["review"])
                 + (f"\n\n![card]({card_url})" if card_url else ""))
        if not slack_review(b, card_url):
            slack_preview(b, card_url)

    if os.environ.get("SOCIAL_PAUSE") == "1":
        print("SOCIAL_PAUSE=1 — posting disabled.")
        return
    if b["blocks"]:
        print("blocked by the publishing gate — not posting.")
        return
    prior = _posted(b["kind"], b["date"])
    if prior and not a.dry_run:
        print(f"already posted {b['kind']} {b['date']}: {prior.get('url')}")
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

    media = api.media_upload(filename=b["card"])
    try:
        api.create_media_metadata(media.media_id, b["alt"][:1000])
    except Exception as e:  # alt text is best-effort
        print("alt text not set:", str(e)[:100])
    import tweepy
    try:
        resp = client.create_tweet(text=b["text"],
                                   media_ids=[media.media_id],
                                   user_auth=True)
    except tweepy.errors.HTTPException as e:
        # X explains billing/permission refusals in the body (e.g. 402:
        # no credits / plan not attached to this app's project).
        body = ""
        try:
            body = e.response.text[:600]
        except Exception:
            pass
        if a.bundle:
            _review_done(review, "X refused the post",
                         f"X refused the post: HTTP {e.response.status_code} "
                         f"{body[:300]}")
        sys.exit(f"X refused the post: HTTP {e.response.status_code} {body}")
    tid = resp.data["id"]
    url = f"https://x.com/{me.username}/status/{tid}"
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    os.makedirs(SOCIAL_POSTS, exist_ok=True)
    rec = os.path.join(SOCIAL_POSTS,
                       f"{ts[:19].replace(':', '')}_{b['kind']}_{tid}.json")
    with open(rec, "w", encoding="utf-8") as f:
        json.dump({
            "ts": ts, "kind": b["kind"], "date": b["date"], "tweet_id": tid,
            "url": url, "handle": me.username,
            "text_sha1": hashlib.sha1(b["text"].encode("utf-8")).hexdigest(),
            **b["extra"]}, f)
    print("POSTED:", url)
    if a.bundle:
        _review_done(review, f"Posted to X · <{url}|view post>",
                     f"Posted: {url}")


if __name__ == "__main__":
    main()
