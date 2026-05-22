"""
push_alerts.py — Web Push notifications for TickerDesk subscribers.

Runs after each UOA scan + each momentum scan (via workflow_run trigger in
push_alerts.yml). For every user with at least one push_subscriptions row:

  1. Pull their watchlist
  2. Cross-reference today's signal data:
       - UOA: new flagged contracts since last push for this user
       - swing grades: any upgrade vs yesterday's run
       - earnings: any ticker with earnings <24h away (only on first push of day)
  3. Build a concise notification (1 sentence, max 4 names)
  4. Send via Web Push to each device
  5. Log every attempt to push_log; expire endpoints with 5+ failures

Idempotency is handled per-event:
  - UOA push uses a hash of (user, top 3 ticker IDs) — same hash within an
    hour = skip (don't spam the same flow twice)
  - Grade upgrades only push when the swing_report's `runs[-1].date` is
    new vs the last push_log row for this user+alert_type

Required env vars (all from GH Actions secrets):
  SUPABASE_URL
  SUPABASE_SERVICE_KEY
  VAPID_PUBLIC_KEY
  VAPID_PRIVATE_KEY
  VAPID_SUBJECT             "mailto:you@example.com" — required by Web Push spec
  TRIGGER_SOURCE            "uoa" | "momentum" — passed by the calling workflow
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import pytz

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ET = pytz.timezone("America/New_York")
_BASE = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(_BASE, "docs", "reports")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = (os.environ.get("VAPID_SUBJECT") or
                 "mailto:brief@tickerdesk.io").strip()
# Belt-and-suspenders: a blank GH Actions secret returns '' (not None),
# which os.environ.get(... default) doesn't fall through. Coerce.
if not VAPID_SUBJECT.startswith("mailto:") and \
   not VAPID_SUBJECT.startswith("https:"):
    VAPID_SUBJECT = "mailto:" + VAPID_SUBJECT.lstrip(":")
TRIGGER_SOURCE = os.environ.get("TRIGGER_SOURCE", "manual")
SITE_URL = os.environ.get("SITE_URL", "https://tickerdesk.io")


# ─────────────────────────────────────────────────────────────────────
# Data loaders (mirror daily_brief.py but trimmed to what we need here)
# ─────────────────────────────────────────────────────────────────────

def _safe_load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def load_swing_grade_changes():
    """Return {ticker: {grade, prev_grade, change}} for grade upgrades only."""
    data = _safe_load(os.path.join(REPORTS_DIR, "swing_report.json"))
    if not data or not data.get("runs"):
        return {}, None
    runs = sorted(data["runs"], key=lambda r: r.get("date", ""), reverse=True)
    today = runs[0]
    prev = runs[1] if len(runs) > 1 else None
    if not prev:
        return {}, today.get("date")

    def flatten(run):
        out = {}
        for grade, names in (run.get("grades") or {}).items():
            for n in names:
                t = n.get("t")
                if t:
                    out[t] = {**n, "grade": grade}
        return out

    today_g = flatten(today)
    prev_g = flatten(prev)
    order = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-",
             "D+", "D", "D-", "E+", "E", "E-", "F+", "F", "F-", "G+", "G"]
    rank = {g: i for i, g in enumerate(order)}
    upgrades = {}
    for t, row in today_g.items():
        pg = (prev_g.get(t) or {}).get("grade")
        if pg and rank.get(row["grade"], 99) < rank.get(pg, 99):
            upgrades[t] = {
                "grade": row["grade"], "prev_grade": pg,
                "name": row.get("n", ""), "change": "upgrade",
            }
        # Brand new A+/A appearances are also "newsworthy"
        elif not pg and row["grade"] in ("A+", "A"):
            upgrades[t] = {
                "grade": row["grade"], "prev_grade": None,
                "name": row.get("n", ""), "change": "new_top_tier",
            }
    return upgrades, today.get("date")


def load_uoa_flagged():
    """Return {ticker: top_premium_contract}. Considers a ticker "flagged"
    if its top contract clears the same UOA bar the dashboard uses."""
    data = _safe_load(os.path.join(REPORTS_DIR, "uoa_latest.json"))
    if not data:
        return {}, None
    by_ticker = {}
    for row in data.get("rows", []):
        t = row.get("ticker")
        if not t:
            continue
        prem = row.get("premium") or 0
        if prem < 100000:        # $100k+ is the UOA push threshold
            continue
        if (t not in by_ticker) or (prem > by_ticker[t].get("premium", 0)):
            by_ticker[t] = row
    return by_ticker, data.get("generated")


def load_earnings_today_tomorrow():
    """Return {ticker: {when, bmo_amc}} for earnings <= 1 day out."""
    data = _safe_load(os.path.join(REPORTS_DIR, "earnings_anticipated.json"))
    if not data:
        return {}
    today = datetime.now(ET).date()
    tomorrow = today + timedelta(days=1)
    out = {}
    for day in data.get("days", []):
        try:
            d = datetime.strptime(day.get("date", ""), "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < today or d > tomorrow:
            continue
        for slot in ("bmo", "amc"):
            for c in day.get(slot, []) or []:
                tk = c.get("ticker")
                if tk and tk not in out:
                    out[tk] = {
                        "when_date": day.get("date"),
                        "bmo_amc": slot.upper(),
                        "company": c.get("company"),
                    }
    return out


# ─────────────────────────────────────────────────────────────────────
# Supabase REST helpers
# ─────────────────────────────────────────────────────────────────────

def _sb_get(path, params=None):
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY not set")
    q = ""
    if params:
        q = "?" + "&".join("{}={}".format(k, v) for k, v in params.items())
    url = "{}/rest/v1/{}{}".format(SUPABASE_URL, path, q)
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": "Bearer " + SUPABASE_SERVICE_KEY,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _sb_post(path, body, prefer=""):
    url = "{}/rest/v1/{}".format(SUPABASE_URL, path)
    data = json.dumps(body).encode("utf-8")
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": "Bearer " + SUPABASE_SERVICE_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            txt = r.read().decode("utf-8")
            return json.loads(txt) if txt else None
    except urllib.error.HTTPError as e:
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8")
        except Exception:
            pass
        raise RuntimeError("Supabase POST {} HTTP {}: {}".format(
            path, e.code, body_txt)) from e


def _sb_patch(path, body, where_params):
    q = "?" + "&".join("{}={}".format(k, v) for k, v in where_params.items())
    url = "{}/rest/v1/{}{}".format(SUPABASE_URL, path, q)
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": "Bearer " + SUPABASE_SERVICE_KEY,
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }, method="PATCH")
    try:
        with urllib.request.urlopen(req, timeout=30):
            return True
    except urllib.error.HTTPError as e:
        print("  PATCH {} failed: {}".format(path, e.code))
        return False


def _sb_delete(path, where_params):
    q = "?" + "&".join("{}={}".format(k, v) for k, v in where_params.items())
    url = "{}/rest/v1/{}{}".format(SUPABASE_URL, path, q)
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": "Bearer " + SUPABASE_SERVICE_KEY,
    }, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=30):
            return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────
# Per-ticker alert overrides
#
# The drilldown panel lets users opt OUT of specific alert types for
# specific tickers (e.g. "I want UOA alerts on my watchlist, but mute
# them for SPY because the flow there is noisy"). Those overrides live
# in public.alerts(user_id, ticker, alert_type, enabled).
#
# UI key names don't match the push_alerts.py internal names — translate
# via ALERT_KEY_MAP. Missing rows default to ON (opt-out model): if the
# user has never visited the drilldown, every applicable alert fires.
# ─────────────────────────────────────────────────────────────────────

# UI alert_type → push_alerts internal name. Both keys map to the same
# canonical types we actually fire on; news_sentiment is recognized but
# deferred (no fire path yet).
ALERT_KEY_MAP = {
    "new_flow":          "uoa_watchlist",
    "grade_change":      "grade_upgrade",
    "earnings_imminent": "earnings_imminent",
    "news_sentiment":    "news_sentiment",
}


def fetch_per_ticker_overrides(user_ids):
    """Return {(user_id, ticker, push_alert_type): enabled_bool}.

    Only rows where the user has explicitly set enabled=false are
    actually consulted at fire-time (we treat missing as ON), but we
    return everything so the caller can also surface user-opted-in
    state if needed.
    """
    if not user_ids:
        return {}
    in_clause = "(" + ",".join(user_ids) + ")"
    rows = _sb_get("alerts", {
        "select": "user_id,ticker,alert_type,enabled",
        "user_id": "in." + in_clause,
    }) or []
    out = {}
    for r in rows:
        ui_key = r.get("alert_type")
        push_key = ALERT_KEY_MAP.get(ui_key, ui_key)
        out[(r["user_id"], r["ticker"], push_key)] = bool(r.get("enabled"))
    return out


def per_ticker_allowed(per_ticker, user_id, ticker, alert_type):
    """True unless the user has explicitly turned off this alert for
    this ticker. Default ON matches the drilldown panel's defaults."""
    val = per_ticker.get((user_id, ticker, alert_type))
    return True if val is None else val


def fetch_subscriptions_with_watchlist():
    """Return [{user_id, subs: [...], tickers: [...], alert_types: {}}]."""
    subs = _sb_get("push_subscriptions", {
        "select": "id,user_id,endpoint,p256dh,auth,alert_types,fail_count",
        "fail_count": "lt.5",
    })
    if not subs:
        return []
    ids = sorted({s["user_id"] for s in subs if s.get("user_id")})
    if not ids:
        return []
    in_clause = "(" + ",".join(ids) + ")"
    wl = _sb_get("watchlists", {
        "select": "user_id,ticker",
        "user_id": "in." + in_clause,
    })
    by_user = {}
    for row in wl or []:
        by_user.setdefault(row["user_id"], []).append(row["ticker"])
    grouped = {}
    for s in subs:
        uid = s["user_id"]
        if not uid:
            continue
        grouped.setdefault(uid, {
            "user_id": uid, "subs": [], "tickers": by_user.get(uid, []),
            "alert_types": s.get("alert_types") or {},
        })
        grouped[uid]["subs"].append(s)
    return [g for g in grouped.values() if g["tickers"]]


# ─────────────────────────────────────────────────────────────────────
# Web Push send — pywebpush wraps VAPID signing + AES-GCM encryption.
# Falls back to a raw HTTP send if pywebpush isn't installed.
# ─────────────────────────────────────────────────────────────────────

def send_webpush(sub_row, payload):
    """Returns (ok: bool, error: str|None, gone: bool).
    gone=True means the endpoint is permanently dead (410) and we should
    delete this subscription row immediately."""
    if not VAPID_PRIVATE_KEY or not VAPID_PUBLIC_KEY:
        return False, "VAPID keys not set", False
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        return False, "pywebpush not installed", False

    sub_info = {
        "endpoint": sub_row["endpoint"],
        "keys": {"p256dh": sub_row["p256dh"], "auth": sub_row["auth"]},
    }
    vapid_claims = {"sub": VAPID_SUBJECT}
    try:
        webpush(
            subscription_info=sub_info,
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims=vapid_claims,
            ttl=86400,                # 24h — push service stores until then
            timeout=10,
        )
        return True, None, False
    except Exception as e:
        msg = str(e)
        # 410 Gone or 404 Not Found = endpoint is dead, never retry
        gone = ("410" in msg or "404" in msg or "Gone" in msg or
                "Subscription has expired" in msg)
        return False, msg[:300], gone


# ─────────────────────────────────────────────────────────────────────
# Push message composers — keep notification text short + actionable
# ─────────────────────────────────────────────────────────────────────

def compose_uoa_push(matched, brief_date_label):
    """matched: list of {ticker, premium, contract_meta, vol_oi}."""
    matched.sort(key=lambda r: -(r.get("premium") or 0))
    n = len(matched)
    top = matched[0]
    prem = top.get("premium") or 0
    prem_str = "${:.1f}M".format(prem / 1e6) if prem >= 1e6 \
               else "${:.0f}K".format(prem / 1e3)
    typ = (top.get("type") or "").upper()
    body_parts = ["{} {} {}".format(top["ticker"], typ, prem_str)]
    if n > 1:
        more = [m["ticker"] for m in matched[1:4]]
        body_parts.append("+ " + " · ".join(more))
        if n > 4:
            body_parts.append("+ {} more".format(n - 4))
    return {
        "title": "Unusual flow on {} watchlist name{}".format(
            n, "s" if n > 1 else ""),
        "body": " ".join(body_parts),
        "url": "{}/?t={}".format(SITE_URL, top["ticker"]),
        "ticker": top["ticker"],
        "tag": "tickerdesk-uoa-" + brief_date_label,
        "priority": "high" if n >= 3 else "normal",
    }


def compose_grade_push(matched, brief_date_label):
    """matched: list of {ticker, grade, prev_grade, change}."""
    matched.sort(key=lambda r: r["ticker"])
    n = len(matched)
    head = matched[0]
    if head["change"] == "new_top_tier":
        head_txt = "{} entered {}".format(head["ticker"], head["grade"])
    else:
        head_txt = "{} {} → {}".format(
            head["ticker"], head.get("prev_grade") or "?", head["grade"])
    body_parts = [head_txt]
    if n > 1:
        more = [m["ticker"] for m in matched[1:4]]
        body_parts.append("+ " + " · ".join(more))
        if n > 4:
            body_parts.append("+ {} more".format(n - 4))
    return {
        "title": "Watchlist grade upgrade{}".format("s" if n > 1 else ""),
        "body": " ".join(body_parts),
        "url": "{}/?t={}".format(SITE_URL, head["ticker"]),
        "ticker": head["ticker"],
        "tag": "tickerdesk-grade-" + brief_date_label,
        "priority": "normal",
    }


def compose_earnings_push(matched):
    """matched: list of {ticker, when_date, bmo_amc, company}."""
    matched.sort(key=lambda r: (r.get("when_date") or "", r["ticker"]))
    head = matched[0]
    when = "today" if head["when_date"] == datetime.now(ET).date().isoformat() \
           else "tomorrow"
    body = "{} reports {} {}".format(
        head["ticker"], when, head["bmo_amc"] or "")
    if len(matched) > 1:
        body += " · + " + " · ".join(m["ticker"] for m in matched[1:4])
    return {
        "title": "Watchlist earnings in <24h",
        "body": body.strip(),
        "url": "{}/?t={}".format(SITE_URL, head["ticker"]),
        "ticker": head["ticker"],
        "tag": "tickerdesk-earnings-" + datetime.now(ET).date().isoformat(),
        "priority": "high",
    }


# ─────────────────────────────────────────────────────────────────────
# De-dup against push_log so we don't spam the same alert twice
# ─────────────────────────────────────────────────────────────────────

def alert_hash(user_id, alert_type, tickers):
    """Stable hash for an alert to detect duplicates within ~6h."""
    h = hashlib.sha256()
    h.update(user_id.encode())
    h.update(b"|" + alert_type.encode())
    for tk in sorted(set(tickers)):
        h.update(b"|" + tk.encode())
    return h.hexdigest()[:16]


def already_pushed_recently(user_id, alert_type, tickers, hours=6):
    """True if this same (user, alert_type, ticker set) was pushed <hours ago."""
    if globals().get("FORCE_NO_DEDUP"):
        return False
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat() + "Z"
    rows = _sb_get("push_log", {
        "select": "id,payload",
        "user_id": "eq." + user_id,
        "alert_type": "eq." + alert_type,
        "status": "eq.sent",
        "sent_at": "gte." + cutoff,
        "order": "sent_at.desc",
        "limit": "5",
    })
    target = alert_hash(user_id, alert_type, tickers)
    for r in rows or []:
        payload = r.get("payload") or {}
        if payload.get("hash") == target:
            return True
    return False


def log_push(user_id, sub_id, alert_type, status, title, body,
             tickers, error=None):
    payload = {
        "hash": alert_hash(user_id, alert_type, tickers),
        "tickers": tickers,
    }
    try:
        _sb_post("push_log", {
            "user_id": user_id,
            "subscription_id": sub_id,
            "alert_type": alert_type,
            "status": status,
            "title": title,
            "body": body,
            "payload": payload,
            "error": error,
        }, prefer="return=minimal")
    except Exception as e:
        print("  push_log insert failed: {}".format(e))


# ─────────────────────────────────────────────────────────────────────
# Per-user dispatch
# ─────────────────────────────────────────────────────────────────────

def process_user(user, swing_upgrades, uoa_flagged, earnings_imminent,
                 brief_date_label, per_ticker=None, dry_run=False):
    """For one user: figure out what to push (if anything), send + log.

    per_ticker: dict from fetch_per_ticker_overrides() — lets the user
    opt OUT of specific (ticker, alert_type) combinations via the
    drilldown alerts panel. Missing entries default to ON.
    """
    per_ticker = per_ticker or {}
    uid = user["user_id"]
    tickers = set(user["tickers"])
    if not tickers:
        return 0, 0, 0
    sent = failed = skipped = 0
    at = user["alert_types"] or {}

    alerts_to_send = []
    # "both" comes from manual dispatch — fires every applicable alert.
    # "uoa" / "momentum" come from workflow_run triggers and limit to the
    # alert type that the source workflow could actually inform.
    fire_uoa = TRIGGER_SOURCE in ("uoa", "both")
    fire_momentum = TRIGGER_SOURCE in ("momentum", "both")

    # 1. UOA alerts — only if this run is from a UOA scan (or manual both)
    if fire_uoa and at.get("uoa_watchlist", True):
        matched = []
        for tk in tickers:
            if tk not in uoa_flagged:
                continue
            # Per-ticker mute: user toggled off UOA alerts for this name
            # in the drilldown panel. Skip silently.
            if not per_ticker_allowed(per_ticker, uid, tk, "uoa_watchlist"):
                continue
            row = uoa_flagged[tk]
            matched.append({
                "ticker": tk, "premium": row.get("premium"),
                "type": row.get("type"), "strike": row.get("strike"),
                "expiry": row.get("expiry"), "vol_oi": row.get("vol_oi"),
            })
        if matched:
            uoa_tickers = [m["ticker"] for m in matched]
            if not already_pushed_recently(uid, "uoa_watchlist",
                                           uoa_tickers, hours=4):
                alerts_to_send.append(("uoa_watchlist",
                    compose_uoa_push(matched, brief_date_label), uoa_tickers))

    # 2. Grade upgrades — only if this run is from a momentum scan (or both)
    if fire_momentum and at.get("grade_upgrade", True):
        matched = []
        for tk in tickers:
            if tk not in swing_upgrades:
                continue
            if not per_ticker_allowed(per_ticker, uid, tk, "grade_upgrade"):
                continue
            matched.append({"ticker": tk, **swing_upgrades[tk]})
        if matched:
            g_tickers = [m["ticker"] for m in matched]
            if not already_pushed_recently(uid, "grade_upgrade",
                                           g_tickers, hours=20):
                alerts_to_send.append(("grade_upgrade",
                    compose_grade_push(matched, brief_date_label), g_tickers))

    # 3. Earnings imminent — once per day
    if at.get("earnings_imminent", True):
        matched = []
        for tk in tickers:
            if tk not in earnings_imminent:
                continue
            if not per_ticker_allowed(per_ticker, uid, tk, "earnings_imminent"):
                continue
            matched.append({"ticker": tk, **earnings_imminent[tk]})
        if matched:
            e_tickers = [m["ticker"] for m in matched]
            if not already_pushed_recently(uid, "earnings_imminent",
                                           e_tickers, hours=20):
                alerts_to_send.append(("earnings_imminent",
                    compose_earnings_push(matched), e_tickers))

    # Now send each alert to every device the user has
    for alert_type, payload, t_list in alerts_to_send:
        for sub_row in user["subs"]:
            sub_id = sub_row["id"]
            if dry_run:
                print("    [DRY] {} → {} ({}): {} | {}".format(
                    user["user_id"][:8], alert_type, sub_id[:8],
                    payload["title"], payload["body"]))
                sent += 1
                continue
            ok, err, gone = send_webpush(sub_row, payload)
            if ok:
                print("    sent {} → {} ({}): {}".format(
                    alert_type, sub_id[:8], user["user_id"][:8],
                    payload["title"]))
                log_push(user["user_id"], sub_id, alert_type, "sent",
                         payload["title"], payload["body"], t_list)
                _sb_patch("push_subscriptions",
                          {"last_used_at": datetime.utcnow().isoformat() + "Z",
                           "fail_count": 0},
                          {"id": "eq." + sub_id})
                sent += 1
            elif gone:
                log_push(user["user_id"], sub_id, alert_type, "expired",
                         payload["title"], payload["body"], t_list, err)
                _sb_delete("push_subscriptions", {"id": "eq." + sub_id})
                skipped += 1
            else:
                print("    FAILED {} → {} ({}): {}".format(
                    alert_type, sub_id[:8], user["user_id"][:8], err))
                log_push(user["user_id"], sub_id, alert_type, "failed",
                         payload["title"], payload["body"], t_list, err)
                # Increment fail counter — at 5 we stop trying this endpoint
                current_failed = (sub_row.get("fail_count") or 0) + 1
                _sb_patch("push_subscriptions",
                          {"fail_count": current_failed,
                           "last_failed_at":
                              datetime.utcnow().isoformat() + "Z"},
                          {"id": "eq." + sub_id})
                failed += 1
    return sent, failed, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Don't actually send pushes; print what would be sent")
    ap.add_argument("--trigger", default=None,
                    help="Override TRIGGER_SOURCE env (uoa|momentum|both)")
    ap.add_argument("--force", action="store_true",
                    help="Bypass the 4-hour push_log dedup (testing only)")
    args = ap.parse_args()
    global FORCE_NO_DEDUP
    FORCE_NO_DEDUP = bool(args.force)
    global TRIGGER_SOURCE
    if args.trigger:
        TRIGGER_SOURCE = args.trigger

    today = datetime.now(ET).date()
    brief_date_label = today.isoformat()
    print("Push alerts run · trigger={} · {}".format(
        TRIGGER_SOURCE, brief_date_label))

    # Weekend short-circuit — no scans on weekends, no need to push
    if today.weekday() >= 5:
        print("  Weekend — skipping.")
        return

    print("  Loading signal data...")
    swing_upgrades, swing_date = load_swing_grade_changes()
    uoa_flagged, uoa_ts = load_uoa_flagged()
    earnings_imminent = load_earnings_today_tomorrow()
    print("    swing upgrades: {} · uoa flagged: {} · earnings <=1d: {}".format(
        len(swing_upgrades), len(uoa_flagged), len(earnings_imminent)))
    print("    swing run date: {} · uoa generated: {}".format(
        swing_date, uoa_ts))

    print("  Fetching subscribers...")
    users = fetch_subscriptions_with_watchlist()
    n_subs = sum(len(u["subs"]) for u in users)
    print("    {} user(s) with {} active device(s)".format(len(users), n_subs))
    if not users:
        return

    # Pre-fetch per-ticker overrides for every subscribing user in one
    # query so each process_user() call is O(1) lookup instead of an
    # extra round-trip per ticker.
    user_ids = [u["user_id"] for u in users]
    per_ticker = fetch_per_ticker_overrides(user_ids)
    if per_ticker:
        muted = sum(1 for v in per_ticker.values() if v is False)
        print("    per-ticker overrides loaded: {} rows ({} muted)".format(
            len(per_ticker), muted))

    total_sent = total_failed = total_skipped = 0
    for u in users:
        s, f, sk = process_user(u, swing_upgrades, uoa_flagged,
                                earnings_imminent, brief_date_label,
                                per_ticker=per_ticker,
                                dry_run=args.dry_run)
        if s or f or sk:
            print("  user {}: sent={} failed={} expired={}".format(
                u["user_id"][:8], s, f, sk))
        total_sent += s
        total_failed += f
        total_skipped += sk

    print("\nDone. total_sent={}  failed={}  expired_endpoints={}".format(
        total_sent, total_failed, total_skipped))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print("Fatal: {}: {}".format(type(e).__name__, e))
        traceback.print_exc()
