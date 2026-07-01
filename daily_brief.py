"""
daily_brief.py — Personalized morning trading-desk email for TickerDesk.

Runs ~8:45 AM ET on weekdays (GitHub Actions cron). For every user with
`profiles.daily_brief_enabled = true` it builds a personalized brief that
answers four questions at a glance:

    What changed?   What matters?   What should I look at first?   Where's the risk?

Sections (in order):
  1. Today's Playbook — 3 bullets: best opportunity / watch today / risk
  2. Confluence — watchlist names where grade + flow + catalyst align
  3. What Changed Since Yesterday — upgrades, downgrades, new A+, new flow,
     new earnings ≤7d, new reports
  4. Unusual Options Flow — expanded rows (tier/score, why, tags, B/E, etc.)
  5. Earnings in the next 7 days
  6. Market Context — global risk read-through + today's high-impact events
  7. New Research — human-labeled scanner/report PDFs
  8. Watchlist snapshot

Then: Resend send + email_log idempotency (unchanged).

Idempotency: before sending we check email_log for an existing 'sent' row
for (user_id, kind='daily_brief', brief_date=today) and skip if found, so
re-running the workflow same-day never spams.

Required env vars:
  SUPABASE_URL              https://uaeojibmhxbwkhpvmjwy.supabase.co
  SUPABASE_SERVICE_KEY      service_role key (bypasses RLS — keep in CI only)
  RESEND_API_KEY            re_XXXXXX
  FROM_EMAIL                e.g. "TickerDesk <brief@tickerdesk.io>"

Run locally:
  python daily_brief.py                 # sends to all opted-in users
  python daily_brief.py --dry-run       # build + write preview HTML, don't send
  python daily_brief.py --me you@x.com  # restrict to one email
  python daily_brief.py --preview       # force a synthetic demo brief (no Supabase)

Dry-run / preview always writes docs/email-previews/daily_brief_preview.html
so you can eyeball the email in a browser without sending anything.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytz

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ET = pytz.timezone("America/New_York")
_BASE = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(_BASE, "docs", "reports")
CARRYOVER_PATH = os.path.join(REPORTS_DIR, "carryover_flow.json")
PREVIEW_PATH = os.path.join(_BASE, "docs", "email-previews", "daily_brief_preview.html")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL") or "TickerDesk <brief@tickerdesk.io>"
SITE_URL = os.environ.get("SITE_URL") or "https://tickerdesk.io"
# Worker that hosts the one-click /unsubscribe endpoint (flips
# profiles.daily_brief_enabled=false after verifying the signed token).
WORKER_URL = (os.environ.get("WORKER_URL")
              or "https://smid-scanner-discord-bot.sumeetsancheti97.workers.dev")


def _unsub_sig(user_id: str) -> str:
    """HMAC-SHA256(service_key, user_id) — the worker recomputes this with
    the same shared SUPABASE_SERVICE_KEY to authorize a one-click
    unsubscribe without any login. The key never leaves the server; only
    the 40-hex digest travels in the URL."""
    key = (SUPABASE_SERVICE_KEY or "td-dev-unsub").encode("utf-8")
    return hmac.new(key, user_id.encode("utf-8"), hashlib.sha256).hexdigest()[:40]


def unsub_url(user_id: str) -> str:
    """Signed one-click unsubscribe link for a given user."""
    if not user_id:
        return f"{SITE_URL}/#watchlist"
    q = urllib.parse.urlencode({"u": user_id, "t": _unsub_sig(user_id)})
    return f"{WORKER_URL}/unsubscribe?{q}"

GRADE_ORDER = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-",
               "D+", "D", "D-", "E+", "E", "E-", "F+", "F", "F-", "G+", "G"]
GRADE_RANK = {g: i for i, g in enumerate(GRADE_ORDER)}


def esc(s: Any) -> str:
    """HTML-escape any dynamic value. All user/data-derived strings that
    reach the template MUST go through this."""
    return html.escape("" if s is None else str(s), quote=True)


def _is_a_tier(g: str | None) -> bool:
    return bool(g) and g[0] == "A"


# ─────────────────────────────────────────────────────────────────────
# Data loaders — local JSON files written by the daily scan pipeline
# ─────────────────────────────────────────────────────────────────────

def _safe_load(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def load_swing() -> dict[str, Any]:
    """Returns {ticker: {grade, prev_grade, name, sector, price, chg,
    change, rvol, themes}}. `change` ∈ new/upgrade/downgrade/same."""
    data = _safe_load(os.path.join(REPORTS_DIR, "swing_report.json"))
    if not data or not data.get("runs"):
        return {}
    runs = sorted(data["runs"], key=lambda r: r.get("date", ""), reverse=True)
    today_run = runs[0]
    prev_run = runs[1] if len(runs) > 1 else None

    def flatten(run: dict) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for grade, names in (run.get("grades") or {}).items():
            for n in names:
                t = n.get("t")
                if t:
                    out[t] = {**n, "grade": grade}
        return out

    today = flatten(today_run)
    prev = flatten(prev_run) if prev_run else {}

    merged: dict[str, dict] = {}
    for t, row in today.items():
        pg = (prev.get(t) or {}).get("grade")
        if pg is None:
            change = "new"
        elif GRADE_RANK.get(row["grade"], 99) < GRADE_RANK.get(pg, 99):
            change = "upgrade"
        elif GRADE_RANK.get(row["grade"], 99) > GRADE_RANK.get(pg, 99):
            change = "downgrade"
        else:
            change = "same"
        merged[t] = {
            "grade": row["grade"],
            "prev_grade": pg,
            "name": row.get("n", ""),
            "sector": row.get("sec", ""),
            "price": row.get("p"),
            "chg": row.get("chg"),
            "rvol": row.get("rvol"),
            "themes": row.get("th") or [],
            "change": change,
        }
    return merged


def load_uoa() -> tuple[dict[str, list[dict]], list[dict]]:
    """Returns (by_ticker, market_top).

    by_ticker  : {ticker: [top 3 contract rows by premium]} — full field set.
    market_top : highest-conviction flow across the whole tape (deduped by
                 ticker, sorted by trade_score) for the market-wide teaser.
    """
    data = _safe_load(os.path.join(REPORTS_DIR, "uoa_latest.json"))
    if not data:
        return {}, []
    rows = data.get("rows", []) or []
    by_ticker: dict[str, list[dict]] = {}
    for row in rows:
        t = row.get("ticker")
        if t:
            by_ticker.setdefault(t, []).append(row)
    for t in by_ticker:
        by_ticker[t] = sorted(by_ticker[t],
                              key=lambda r: -(r.get("premium") or 0))[:3]
    # Market-wide top flow — one row per ticker (its best), by trade_score
    best_per_tk: dict[str, dict] = {}
    for row in rows:
        t = row.get("ticker")
        if not t:
            continue
        cur = best_per_tk.get(t)
        if not cur or (row.get("trade_score") or 0) > (cur.get("trade_score") or 0):
            best_per_tk[t] = row
    market_top = sorted(best_per_tk.values(),
                        key=lambda r: -(r.get("trade_score") or 0))
    return by_ticker, market_top


def load_earnings_within(days: int = 7) -> dict[str, dict]:
    """Returns {ticker: {when_date, dow, bmo_amc, company, days_away}}."""
    data = _safe_load(os.path.join(REPORTS_DIR, "earnings_anticipated.json"))
    if not data:
        return {}
    today = datetime.now(ET).date()
    horizon = today + timedelta(days=days)
    out: dict[str, dict] = {}
    for day in data.get("days", []) or []:
        try:
            d = datetime.strptime(day.get("date", ""), "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < today or d > horizon:
            continue
        for slot in ("bmo", "amc"):
            for c in day.get(slot, []) or []:
                tk = c.get("ticker")
                if tk and tk not in out:
                    out[tk] = {
                        "when_date": day.get("date"),
                        "dow": day.get("dow"),
                        "bmo_amc": slot.upper(),
                        "company": c.get("company"),
                        "days_away": (d - today).days,
                    }
    return out


def load_reports_by_ticker(hours: int = 24) -> dict[str, list[dict]]:
    """{ticker: [{file, label, type}]} for ticker/altdata reports < N hours old."""
    data = _safe_load(os.path.join(REPORTS_DIR, "manifest.json"))
    if not data:
        return {}
    cutoff = datetime.now(ET) - timedelta(hours=hours)
    by_ticker: dict[str, list[dict]] = {}
    for _rtype, files in (data.get("reports") or {}).items():
        for f in files:
            fname = f.get("file", "")
            parts = fname.split("_")
            if len(parts) < 4:
                continue
            head, tk = parts[0], parts[1]
            if head not in ("ticker", "altdata"):
                continue
            if not tk.isupper() or not tk.isalpha():
                continue
            try:
                stamp = ET.localize(datetime.strptime(
                    f"{parts[2]}_{parts[3].split('.')[0]}", "%Y-%m-%d_%H%M"))
            except (ValueError, IndexError):
                continue
            if stamp < cutoff:
                continue
            by_ticker.setdefault(tk, []).append({
                "file": fname,
                "label": f.get("label", ""),
                "type": "Alt-Data" if head == "altdata" else "Ticker Report",
            })
    return by_ticker


# Human labels keyed by the manifest report TYPE (its `label` field is just
# a timestamp, so we never use that as the display name).
_REPORT_TYPE_LABELS = {
    "smid-scanner": "SMID Breakout Scanner",
    "iwm-scanner": "IWM Breakout Scanner",
    "smid-setup": "SMID Setup Builder",
    "iwm-setup": "IWM Setup Builder",
    "qm-monthly": "Qullamaggie Monthly Momentum",
    "stockbee-weekly": "Stockbee Weekly Momentum",
    "adhoc": "Ticker Research",
    "alt-data": "Alt-Data Report",
}


def load_new_reports_all(hours: int = 24) -> list[dict]:
    """Every report archived in the last N hours, with a human label
    (never a raw filename or a bare timestamp). [{label, file, when}]."""
    data = _safe_load(os.path.join(REPORTS_DIR, "manifest.json"))
    if not data:
        return []
    cutoff = datetime.now(ET) - timedelta(hours=hours)
    out: list[dict] = []
    for rtype, files in (data.get("reports") or {}).items():
        base = _REPORT_TYPE_LABELS.get(rtype, rtype.replace("-", " ").title())
        for f in files:
            fname = f.get("file", "")
            parts = fname.split("_")
            if len(parts) < 4:
                continue
            try:
                stamp = ET.localize(datetime.strptime(
                    f"{parts[-2]}_{parts[-1].split('.')[0]}", "%Y-%m-%d_%H%M"))
            except (ValueError, IndexError):
                continue
            if stamp < cutoff:
                continue
            label = base
            # Per-ticker reports: append the symbol so it's specific.
            if parts[0] in ("ticker", "altdata") and parts[1].isupper():
                label = f"{base} · {parts[1]}"
            out.append({"label": label, "file": fname, "when": stamp})
    out.sort(key=lambda r: r["when"], reverse=True)
    return out


def load_market_context() -> dict:
    """Global risk read-through (country ETFs) + today's high-impact events."""
    ctx: dict[str, Any] = {"global": None, "events": []}
    cetf = _safe_load(os.path.join(REPORTS_DIR, "country_etfs.json"))
    if cetf:
        anchor = cetf.get("anchor") or {}
        etfs = [e for e in (cetf.get("etfs") or [])
                if isinstance(e.get("vs_anchor"), (int, float))]
        ranked = sorted(etfs, key=lambda e: -e["vs_anchor"])
        em = [e["vs_anchor"] for e in etfs if e.get("developed") is False]
        dev = [e["vs_anchor"] for e in etfs if e.get("developed") is True]
        tone = "mixed"
        if em and dev:
            avg_em, avg_dev = sum(em) / len(em), sum(dev) / len(dev)
            if avg_em > avg_dev + 1:
                tone = "risk-on tilt (EM leading)"
            elif avg_dev > avg_em + 1:
                tone = "risk-off tilt (developed leading)"
            else:
                tone = "balanced"
        ctx["global"] = {
            "anchor_ticker": anchor.get("ticker", "SPY"),
            "anchor_ytd": anchor.get("ytd_pct"),
            "leaders": ranked[:2],
            "laggards": ranked[-2:][::-1],
            "tone": tone,
        }
    cal = _safe_load(os.path.join(REPORTS_DIR, "economic_calendar.json"))
    if cal:
        today_ff = datetime.now(ET).strftime("%m-%d-%Y")
        todays = [e for e in (cal.get("events") or []) if e.get("date") == today_ff]
        hi = [e for e in todays
              if (e.get("impact") or "").lower() in ("high", "medium")]
        ctx["events"] = (hi or todays)[:6]
    return ctx


def load_premarket() -> dict:
    """Market-wide pre-market movers (top gainers/losers + a catalyst per
    name) from the worker's Pre-Market Buzz endpoint. {} on failure."""
    try:
        req = urllib.request.Request(
            f"{WORKER_URL}/?premarket=1",
            headers={"User-Agent": "TickerDesk-Brief/1.0",
                     "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8")) or {}
    except Exception as e:
        print(f"  premarket load failed (non-fatal): {e}")
        return {}


def load_overnight_news(tickers, hours: int = 15, limit: int = 5) -> list[dict]:
    """Watchlist-relevant headlines from the news_live wire published since
    last night (Benzinga via Supabase). [] on failure / no config."""
    if not tickers or not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
        return []
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)
                 ).strftime("%Y-%m-%dT%H:%M:%SZ")
        syms = ",".join(sorted(set(tickers))[:40])
        rows = _supabase_get("news_live", {
            "select": "headline,url,symbols,published_at,source",
            "symbols": f"ov.%7B{syms}%7D",
            "published_at": f"gte.{since}",
            "order": "published_at.desc",
            "limit": str(limit * 4),
        }) or []
        seen, out = set(), []
        for r in rows:
            h = (r.get("headline") or "").strip()
            k = h.lower()
            if not h or k in seen:
                continue
            seen.add(k)
            out.append(r)
            if len(out) >= limit:
                break
        return out
    except Exception as e:
        print(f"  overnight-news load failed (non-fatal): {e}")
        return []


# ─────────────────────────────────────────────────────────────────────
# Supabase REST helpers (service role — CI only)
# ─────────────────────────────────────────────────────────────────────

def _supabase_get(path: str, params: dict | None = None) -> Any:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY not set")
    q = ("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else ""
    req = urllib.request.Request(f"{SUPABASE_URL}/rest/v1/{path}{q}", headers={
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _supabase_post(path: str, body: dict | list, prefer: str = "") -> Any:
    data = json.dumps(body).encode("utf-8")
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    req = urllib.request.Request(f"{SUPABASE_URL}/rest/v1/{path}",
                                 data=data, headers=headers, method="POST")
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
        raise RuntimeError(f"Supabase POST {path} HTTP {e.code}: {body_txt}") from e


def fetch_opted_in_users(restrict_email: str | None = None) -> list[dict]:
    profiles = _supabase_get("profiles", {
        "select": "id,display_name,last_seen,subscription_tier,daily_brief_enabled",
        "daily_brief_enabled": "eq.true",
    })
    if not profiles:
        return []
    ids = [p["id"] for p in profiles]
    in_clause = "(" + ",".join(ids) + ")"
    wl = _supabase_get("watchlists", {
        "select": "user_id,ticker",
        "user_id": f"in.{in_clause}",
    })
    by_user: dict[str, list[str]] = {}
    for row in wl or []:
        by_user.setdefault(row["user_id"], []).append(row["ticker"])

    emails: dict[str, str] = {}
    page = 1
    while True:
        url = f"{SUPABASE_URL}/auth/v1/admin/users?page={page}&per_page=1000"
        req = urllib.request.Request(url, headers={
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        })
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.loads(r.read().decode("utf-8"))
        users = payload.get("users") or []
        if not users:
            break
        for u in users:
            if u.get("id") and u.get("email"):
                emails[u["id"]] = u["email"]
        if len(users) < 1000:
            break
        page += 1

    out: list[dict] = []
    for p in profiles:
        uid = p["id"]
        em = emails.get(uid)
        if not em:
            continue
        if restrict_email and em.lower() != restrict_email.lower():
            continue
        tickers = by_user.get(uid, [])
        if not tickers:
            continue
        out.append({
            "user_id": uid,
            "email": em,
            "display_name": p.get("display_name") or "",
            "last_seen": p.get("last_seen"),
            "plan": p.get("subscription_tier") or "free",
            "tickers": tickers,
        })
    return out


def already_sent_today(user_id: str, brief_date: str) -> bool:
    rows = _supabase_get("email_log", {
        "select": "id",
        "user_id": f"eq.{user_id}",
        "kind": "eq.daily_brief",
        "brief_date": f"eq.{brief_date}",
        "status": "eq.sent",
        "limit": "1",
    })
    return bool(rows)


def log_email(user_id: str, email: str, brief_date: str, status: str,
              subject: str, payload: dict, error: str | None = None) -> None:
    try:
        _supabase_post("email_log", {
            "user_id": user_id,
            "email": email,
            "kind": "daily_brief",
            "status": status,
            "subject": subject,
            "payload": payload,
            "error": error,
            "brief_date": brief_date,
        }, prefer="return=minimal")
    except Exception as e:
        print(f"  email_log insert failed: {e}")


# ─────────────────────────────────────────────────────────────────────
# Formatting helpers
# ─────────────────────────────────────────────────────────────────────

CSS_BG = "#0b1020"
CSS_PANEL = "#121933"
CSS_BORDER = "#1f2a4a"
CSS_TEXT = "#dfe6ff"
CSS_MUTED = "#8a9bc7"
CSS_GREEN = "#1fb363"
CSS_RED = "#ff5b78"
CSS_AMBER = "#ffc800"
CSS_ACCENT = "#7aa9ff"
CSS_GOLD = "#ffd54a"


def _fmt_premium(p) -> str:
    try:
        p = float(p)
    except (TypeError, ValueError):
        return "—"
    if p >= 1e6:
        return f"${p/1e6:.1f}M"
    if p >= 1e3:
        return f"${p/1e3:.0f}K"
    return f"${p:.0f}"


def _fmt_num(v, fmt="{:.1f}") -> str:
    try:
        return fmt.format(float(v))
    except (TypeError, ValueError):
        return "—"


def _fmt_ts_et(iso) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return dt.astimezone(ET).strftime("%I:%M %p").lstrip("0") + " ET"
    except Exception:
        return ""


def _grade_color(g: str | None) -> str:
    if not g:
        return CSS_MUTED
    return {"A": CSS_GREEN, "B": CSS_GREEN, "C": CSS_ACCENT, "D": CSS_AMBER,
            "E": "#ff8c1a", "F": CSS_RED, "G": CSS_RED}.get(g[0], CSS_MUTED)


def _tier_color(t: str | None) -> str:
    return {"A": CSS_GREEN, "B": CSS_ACCENT, "C": CSS_MUTED,
            "golden": CSS_GOLD}.get((t or "").lower()[:1] if t else "", CSS_MUTED)


def _dir_meta(d: str | None) -> tuple[str, str]:
    d = (d or "").lower()
    if d == "bullish":
        return "Bullish", CSS_GREEN
    if d == "bearish":
        return "Bearish", CSS_RED
    return "Mixed", CSS_MUTED


def _link(tk: str) -> str:
    return f"{SITE_URL}/?t={esc(tk)}"


# ─────────────────────────────────────────────────────────────────────
# Analysis — confluence, what-changed, playbook
# ─────────────────────────────────────────────────────────────────────

def compute_confluence(tickers, swing, uoa, earnings, reports_by_tk) -> list[dict]:
    """Rank watchlist names where multiple TickerDesk signals align, with a
    plain-English reason. Positive score = bull stack, negative = caution."""
    out = []
    for tk in set(tickers):
        sw = swing.get(tk) or {}
        grade = sw.get("grade")
        top = (uoa.get(tk) or [None])[0]
        er = earnings.get(tk)
        rep = reports_by_tk.get(tk)
        score = 0.0
        bull, bear, ctx = [], [], []

        if grade:
            if _is_a_tier(grade):
                score += 3 if grade == "A+" else 2 if grade == "A" else 1.5
                bull.append(f"{grade} swing grade")
            elif grade[0] in ("D", "E", "F", "G"):
                score -= 1.5
                bear.append(f"weak {grade} grade")
        if sw.get("change") == "upgrade":
            score += 1
            bull.append(f"upgraded from {sw.get('prev_grade')}")
        elif sw.get("change") == "downgrade":
            score -= 1
            bear.append(f"downgraded from {sw.get('prev_grade')}")
        elif sw.get("change") == "new" and _is_a_tier(grade):
            score += 0.5
            bull.append("new A-tier today")

        if top:
            direction = (top.get("direction") or "").lower()
            ts = top.get("trade_score") or 0
            golden = bool(top.get("is_golden") or top.get("golden"))
            voi = top.get("vol_oi")
            voi_txt = f"{voi:.0f}× OI" if isinstance(voi, (int, float)) else ""
            flow_desc = (f"{'golden ' if golden else ''}bullish flow "
                         f"({_fmt_premium(top.get('premium'))}"
                         f"{', ' + voi_txt if voi_txt else ''}, score {int(ts)})")
            if direction == "bullish":
                score += 3 if golden else 2 if ts >= 70 else 1
                bull.append(flow_desc)
            elif direction == "bearish":
                score -= 3 if golden else 2 if ts >= 70 else 1
                bear.append(flow_desc.replace("bullish", "bearish"))

        if er:
            da = er.get("days_away")
            ctx.append(f"earnings {er.get('dow', '')} {er.get('bmo_amc', '')}"
                       + (f" ({da}d)" if isinstance(da, int) else ""))
        if rep:
            ctx.append("fresh research today")

        # Need at least two aligned signals to count as confluence.
        signal_ct = len(bull) + len(bear) + (1 if er else 0) + (1 if rep else 0)
        if signal_ct < 2:
            continue

        net = "bull" if score > 0.5 else "bear" if score < -0.5 else "mixed"
        if net == "bull":
            reason = "Bull stack — " + ", ".join(bull)
            if ctx:
                reason += "; " + ", ".join(ctx)
        elif net == "bear":
            reason = "Caution — " + ", ".join(bear)
            if ctx:
                reason += "; " + ", ".join(ctx)
        else:
            reason = "Mixed — " + ", ".join(bull + bear)
            if ctx:
                reason += "; " + ", ".join(ctx)

        out.append({
            "ticker": tk, "score": round(score, 1), "net": net,
            "grade": grade, "reason": reason,
            "top": top, "earnings": er,
        })
    out.sort(key=lambda r: -abs(r["score"]))
    return out


def compute_what_changed(tickers, swing, uoa, earnings, reports_by_tk) -> dict:
    wl = set(tickers)
    ch = {"upgrades": [], "downgrades": [], "new_a": [], "new_flow": [],
          "new_earnings": [], "new_reports": []}
    for tk in sorted(wl):
        sw = swing.get(tk) or {}
        if sw.get("change") == "upgrade":
            ch["upgrades"].append((tk, sw.get("prev_grade"), sw.get("grade")))
        elif sw.get("change") == "downgrade":
            ch["downgrades"].append((tk, sw.get("prev_grade"), sw.get("grade")))
        if sw.get("change") == "new" and _is_a_tier(sw.get("grade")):
            ch["new_a"].append((tk, sw.get("grade")))
        top = (uoa.get(tk) or [None])[0]
        if top:
            ch["new_flow"].append((tk, top))
        er = earnings.get(tk)
        if er:
            ch["new_earnings"].append((tk, er))
        rep = reports_by_tk.get(tk)
        if rep:
            ch["new_reports"].append((tk, rep))
    ch["new_flow"].sort(key=lambda x: -(x[1].get("premium") or 0))
    ch["new_earnings"].sort(key=lambda x: x[1].get("days_away", 99))
    return ch


def _earnings_soon(c, within: int = 2) -> bool:
    """True when a confluence row reports earnings inside `within` days.
    These are binary-event / post-move names — deliberately kept OUT of the
    top-of-brief ideas (they belong in the earnings section, not the idea
    list). Distinct from 'watch/risk' bullets, which may cite a catalyst."""
    er = c.get("earnings")
    da = er.get("days_away") if er else None
    return isinstance(da, int) and 0 <= da <= within


def compute_ideas(conf, swing=None, tickers=None, earnings=None,
                  n: int = 5) -> list[dict]:
    """A few concrete, actionable NON-earnings ideas for the very top of
    the brief. Earnings-soon names are excluded (event risk / post-move —
    they belong under Watch/Risk, not the idea list). Order:
      1. bull-stacked confluence (score ≥ 2)
      2. any bullish confluence (score > 0)
      3. strong A-tier swing grades even without a full confluence stack
    so the line reliably shows a few names, not just one."""
    earn_soon = set()
    if earnings:
        earn_soon = {tk for tk, e in earnings.items()
                     if isinstance(e.get("days_away"), int)
                     and 0 <= e["days_away"] <= 2}
    prim = [c for c in conf
            if c["net"] == "bull" and c["score"] >= 2 and not _earnings_soon(c)]
    seen = {c["ticker"] for c in prim}
    if len(prim) < 3:
        for c in conf:
            if (c["net"] == "bull" and c["score"] > 0
                    and not _earnings_soon(c) and c["ticker"] not in seen):
                prim.append(c); seen.add(c["ticker"])
    if len(prim) < 3 and swing and tickers:
        atier = []
        for tk in set(tickers):
            sw = swing.get(tk) or {}
            g = sw.get("grade")
            if (g and _is_a_tier(g) and tk not in seen and tk not in earn_soon):
                atier.append({"ticker": tk, "grade": g, "score": 0.0,
                              "net": "bull", "reason": f"{g} swing grade",
                              "top": None, "earnings": None})
        atier.sort(key=lambda c: GRADE_RANK.get(c["grade"], 99))
        prim += atier
    return prim[:n]


def compute_playbook(conf, market_top, market, earnings, tickers) -> list[dict]:
    """Exactly three bullets: best opportunity / watch today / risk."""
    wl = set(tickers)
    used = set()

    # 1) Best opportunity — strongest bull-stack watchlist name, else the
    #    tape's top golden/high-score flow. Earnings-soon names are excluded
    #    here (event risk, not a clean setup) — they surface under Watch.
    best = None
    bulls = [c for c in conf
             if c["net"] == "bull" and c["score"] >= 2 and not _earnings_soon(c)]
    if bulls:
        c = bulls[0]
        used.add(c["ticker"])
        best = {"kind": "Best opportunity", "ticker": c["ticker"],
                "text": c["reason"]}
    elif market_top:
        m = market_top[0]
        best = {"kind": "Best opportunity", "ticker": m.get("ticker"),
                "text": f"Tape's top edge: {m.get('why') or ''}"
                        f" — tier {m.get('tier')}, score {int(m.get('trade_score') or 0)}"}
    else:
        best = {"kind": "Best opportunity", "ticker": None,
                "text": "No standout setup on your watchlist today — check the "
                        "desk for market-wide movers."}

    # 2) Watch today — nearest catalyst: earnings today/tomorrow on the
    #    watchlist, else biggest fresh flow, else today's marquee macro event.
    watch = None
    soonest = sorted(
        [(tk, e) for tk, e in earnings.items() if tk in wl
         and isinstance(e.get("days_away"), int)],
        key=lambda x: x[1]["days_away"])
    if soonest and soonest[0][1]["days_away"] <= 1:
        tk, e = soonest[0]
        when = "today" if e["days_away"] == 0 else "tomorrow"
        watch = {"kind": "Watch today", "ticker": tk,
                 "text": f"Reports {when} {e.get('bmo_amc', '')} — expect an IV "
                         f"crush / gap; size accordingly."}
    if not watch:
        fresh = [c for c in conf if c["ticker"] not in used and c["top"]]
        if fresh:
            c = fresh[0]
            t = c["top"]
            watch = {"kind": "Watch today", "ticker": c["ticker"],
                     "text": f"Fresh flow: {t.get('why') or ''}. "
                             f"Biggest print {_fmt_ts_et(t.get('biggest_print_ts'))}."}
    if not watch and market and market.get("events"):
        ev = market["events"][0]
        watch = {"kind": "Watch today", "ticker": None,
                 "text": f"Macro: {ev.get('title')} at "
                         f"{ev.get('time')} — {ev.get('impact')} impact."}
    if not watch:
        watch = {"kind": "Watch today", "ticker": None,
                 "text": "Quiet catalyst calendar for your names — let setups come to you."}
    if watch.get("ticker"):
        used.add(watch["ticker"])

    # 3) Risk / avoid chase — bearish/downgraded name, else an overextended
    #    one (far from break-even / big recent run), else earnings risk.
    risk = None
    bears = [c for c in conf if c["net"] == "bear"]
    if bears:
        c = bears[0]
        risk = {"kind": "Risk / avoid chase", "ticker": c["ticker"],
                "text": c["reason"]}
    if not risk:
        overext = None
        for c in conf:
            t = c["top"] or {}
            bed = t.get("be_distance_pct")
            if isinstance(bed, (int, float)) and bed >= 10:
                overext = (c["ticker"], bed)
                break
        if overext:
            risk = {"kind": "Risk / avoid chase", "ticker": overext[0],
                    "text": f"Flow strike sits {overext[1]:.0f}% out — the easy "
                            f"money's priced in; don't chase the premium here."}
    if not risk and soonest:
        tk, e = soonest[0]
        risk = {"kind": "Risk / avoid chase", "ticker": tk,
                "text": f"Earnings in {e['days_away']}d — binary event risk; "
                        f"trim or hedge into the print."}
    if not risk:
        risk = {"kind": "Risk / avoid chase", "ticker": None,
                "text": "No obvious blow-up risk flagged — still, respect stops "
                        "and don't chase extended names."}
    return [best, watch, risk]


# ─────────────────────────────────────────────────────────────────────
# Brief assembly
# ─────────────────────────────────────────────────────────────────────

def build_brief(tickers, swing, uoa, market_top, earnings, reports_by_tk,
                new_reports_all, market) -> dict:
    conf = compute_confluence(tickers, swing, uoa, earnings, reports_by_tk)
    changed = compute_what_changed(tickers, swing, uoa, earnings, reports_by_tk)
    playbook = compute_playbook(conf, market_top, market, earnings, tickers)
    ideas = compute_ideas(conf, swing, tickers, earnings)

    flow = []
    for tk in sorted(set(tickers)):
        rows = uoa.get(tk)
        if rows:
            flow.append({"ticker": tk, "contracts": rows})
    flow.sort(key=lambda r: -(r["contracts"][0].get("premium") or 0))

    earn = sorted(
        [{"ticker": tk, **e} for tk, e in earnings.items() if tk in set(tickers)],
        key=lambda r: r.get("days_away", 99))

    snapshot = []
    for tk in sorted(set(tickers)):
        sw = swing.get(tk)
        if sw:
            snapshot.append({"ticker": tk, "grade": sw["grade"],
                             "chg": sw.get("chg")})

    return {
        "playbook": playbook,
        "ideas": ideas,
        "confluence": conf[:6],
        "changed": changed,
        "flow": flow[:6],
        "earnings": earn,
        "market": market,
        "market_top": market_top[:4],
        "reports": new_reports_all[:6],
        "snapshot": snapshot,
    }


# ─────────────────────────────────────────────────────────────────────
# Subject + preheader — most important personalized hook
# ─────────────────────────────────────────────────────────────────────

def build_subject(brief) -> tuple[str, str]:
    ch = brief["changed"]
    n_up = len(ch["upgrades"])
    n_atier = sum(1 for s in brief["snapshot"] if _is_a_tier(s["grade"]))
    n_flow = len(brief["flow"])
    top_flow_tk = brief["flow"][0]["ticker"] if brief["flow"] else None

    if top_flow_tk and n_up:
        hook = (f"{top_flow_tk} flow + {n_up} watchlist "
                f"upgrade{'s' if n_up != 1 else ''}")
    elif n_atier and n_flow:
        hook = (f"{n_atier} A-tier name{'s' if n_atier != 1 else ''}, "
                f"{n_flow} fresh flow hit{'s' if n_flow != 1 else ''}")
    elif top_flow_tk:
        extra = n_flow - 1
        hook = (f"{top_flow_tk} unusual flow"
                + (f" +{extra} more" if extra > 0 else ""))
    elif n_up:
        hook = f"{n_up} watchlist upgrade{'s' if n_up != 1 else ''}"
    elif n_atier:
        hook = f"{n_atier} A-tier name{'s' if n_atier != 1 else ''} holding"
    else:
        hook = "Quiet watchlist — market movers inside"

    subject = f"TickerDesk · {hook}"
    # Preheader = the playbook's best-opportunity line (what to look at first)
    best = brief["playbook"][0]
    pre_tk = f"{best['ticker']}: " if best.get("ticker") else ""
    preheader = (pre_tk + best["text"])[:140]
    return subject, preheader


# ─────────────────────────────────────────────────────────────────────
# HTML rendering — inline CSS, single column, ≤600px, Gmail-safe
# ─────────────────────────────────────────────────────────────────────

def _card(title: str, inner: str, sub: str = "") -> str:
    sub_html = (f'<span style="color:{CSS_MUTED};font-weight:400;'
                f'text-transform:none;letter-spacing:0;font-size:11px;"> · {esc(sub)}</span>'
                if sub else "")
    return f'''
    <div style="margin-top:22px;">
      <div style="font-size:12px;font-weight:700;color:{CSS_MUTED};text-transform:uppercase;letter-spacing:1px;margin-bottom:9px;">{esc(title)}{sub_html}</div>
      <div style="background:{CSS_PANEL};border:1px solid {CSS_BORDER};border-radius:10px;overflow:hidden;">{inner}</div>
    </div>'''


def _row_wrap(inner: str) -> str:
    return (f'<div style="padding:12px 14px;border-bottom:1px solid {CSS_BORDER};">'
            f'{inner}</div>')


def _ticker_link(tk, size=15) -> str:
    if not tk:
        return ""
    return (f'<a href="{_link(tk)}" style="font-weight:700;color:{CSS_TEXT};'
            f'text-decoration:none;font-size:{size}px;">{esc(tk)}</a>')


def _badge(text, color, bg=None) -> str:
    bg = bg or "rgba(255,255,255,0.06)"
    return (f'<span style="display:inline-block;font-size:10px;font-weight:700;'
            f'color:{color};background:{bg};border-radius:4px;padding:1px 6px;'
            f'margin:2px 4px 2px 0;letter-spacing:.3px;">{esc(text)}</span>')


def _render_playbook(playbook, ideas=None) -> str:
    icons = {"Best opportunity": ("🎯", CSS_GREEN),
             "Watch today": ("👁", CSS_ACCENT),
             "Risk / avoid chase": ("⚠️", CSS_RED)}
    rows = []
    # Ideas chip line — a few concrete, NON-earnings names up top so the
    # brief opens with tradeable setups, not the earnings calendar.
    if ideas:
        chips = "".join(
            f'<a href="{_link(c["ticker"])}" style="display:inline-block;'
            f'text-decoration:none;background:rgba(31,179,99,0.12);'
            f'border:1px solid rgba(31,179,99,0.4);border-radius:6px;'
            f'padding:3px 9px;margin:3px 6px 3px 0;font-size:12.5px;'
            f'font-weight:700;color:{CSS_TEXT};white-space:nowrap;">'
            f'{esc(c["ticker"])}'
            + (f' <span style="color:{_grade_color(c.get("grade"))};">{esc(c["grade"])}</span>'
               if c.get("grade") else "")
            + '</a>'
            for c in ideas)
        rows.append(
            f'<div style="padding:11px 14px;border-bottom:1px solid {CSS_BORDER};">'
            f'<div style="font-size:11px;font-weight:700;color:{CSS_GREEN};'
            f'text-transform:uppercase;letter-spacing:.5px;margin-bottom:5px;">'
            f'💡 Today\'s ideas <span style="color:{CSS_MUTED};font-weight:400;'
            f'text-transform:none;letter-spacing:0;">· non-earnings, bull-stacked</span></div>'
            f'<div>{chips}</div></div>')
    for b in playbook:
        icon, col = icons.get(b["kind"], ("•", CSS_TEXT))
        tk = (f'{_ticker_link(b["ticker"])} — ' if b.get("ticker") else "")
        rows.append(
            f'<div style="padding:11px 14px;border-bottom:1px solid {CSS_BORDER};">'
            f'<div style="font-size:11px;font-weight:700;color:{col};'
            f'text-transform:uppercase;letter-spacing:.5px;">{icon} {esc(b["kind"])}</div>'
            f'<div style="font-size:13.5px;color:{CSS_TEXT};margin-top:3px;line-height:1.45;">'
            f'{tk}{esc(b["text"]) if not b.get("ticker") else esc(b["text"])}</div>'
            f'</div>')
    inner = "".join(rows)
    return f'''
    <div style="margin-top:4px;">
      <div style="font-size:13px;font-weight:800;color:{CSS_GOLD};text-transform:uppercase;letter-spacing:1.2px;margin-bottom:9px;">📋 Today's Playbook</div>
      <div style="background:{CSS_PANEL};border:1px solid {CSS_GOLD};border-radius:10px;overflow:hidden;">{inner}</div>
    </div>'''


def _render_confluence(conf) -> str:
    rows = []
    for c in conf:
        col = CSS_GREEN if c["net"] == "bull" else CSS_RED if c["net"] == "bear" else CSS_MUTED
        gcol = _grade_color(c.get("grade"))
        gtxt = (f'<span style="color:{gcol};font-weight:700;font-size:12px;'
                f'margin-left:8px;">{esc(c["grade"])}</span>' if c.get("grade") else "")
        sign = "+" if c["score"] >= 0 else ""
        rows.append(_row_wrap(
            f'<div style="display:flex;align-items:baseline;justify-content:space-between;">'
            f'<div>{_ticker_link(c["ticker"])}{gtxt}</div>'
            f'<div style="font-size:12px;font-weight:700;color:{col};">{sign}{c["score"]}</div></div>'
            f'<div style="font-size:12.5px;color:{CSS_MUTED};margin-top:4px;line-height:1.4;">{esc(c["reason"])}</div>'))
    return _card("Confluence", "".join(rows),
                 "where grade + flow + catalyst align")


def _render_changed(ch) -> str:
    lines = []

    def _tk_list(items, fmt):
        return ", ".join(fmt(i) for i in items)

    if ch["upgrades"]:
        lines.append((CSS_GREEN, "▲ Upgrades",
                      _tk_list(ch["upgrades"],
                               lambda x: f'{esc(x[0])} {esc(x[1])}→{esc(x[2])}')))
    if ch["new_a"]:
        lines.append((CSS_GOLD, "★ New A-tier",
                      _tk_list(ch["new_a"], lambda x: f'{esc(x[0])} ({esc(x[1])})')))
    if ch["new_flow"]:
        lines.append((CSS_AMBER, "⚡ New flow",
                      _tk_list(ch["new_flow"][:5],
                               lambda x: f'{esc(x[0])} {_fmt_premium(x[1].get("premium"))}')))
    if ch["new_earnings"]:
        lines.append((CSS_ACCENT, "📅 Earnings ≤7d",
                      _tk_list(ch["new_earnings"][:5],
                               lambda x: f'{esc(x[0])} ({x[1].get("days_away")}d)')))
    if ch["new_reports"]:
        lines.append((CSS_ACCENT, "📄 New reports",
                      _tk_list(ch["new_reports"][:5], lambda x: esc(x[0]))))
    if ch["downgrades"]:
        lines.append((CSS_RED, "▼ Downgrades",
                      _tk_list(ch["downgrades"],
                               lambda x: f'{esc(x[0])} {esc(x[1])}→{esc(x[2])}')))
    if not lines:
        return ""
    inner = "".join(
        f'<div style="padding:10px 14px;border-bottom:1px solid {CSS_BORDER};">'
        f'<span style="font-size:11px;font-weight:700;color:{col};">{esc(label)}</span>'
        f'<div style="font-size:12.5px;color:{CSS_TEXT};margin-top:3px;line-height:1.4;">{body}</div>'
        f'</div>'
        for col, label, body in lines)
    return _card("What changed since yesterday", inner)


def _render_flow(flow) -> str:
    rows = []
    for r in flow:
        top = r["contracts"][0]
        dir_txt, dir_col = _dir_meta(top.get("direction"))
        typ = (top.get("type") or "").upper()
        tier = top.get("tier")
        ts = top.get("trade_score")
        golden = bool(top.get("is_golden") or top.get("golden"))
        # Header line: ticker · direction · tier/score
        score_txt = (f'<span style="color:{_tier_color(tier)};font-weight:700;'
                     f'font-size:12px;margin-left:8px;">'
                     f'{esc(tier)} · {int(ts) if isinstance(ts,(int,float)) else "—"}</span>')
        # Contract line
        strike = top.get("strike")
        contract = (f'{typ} ${esc(strike)} {esc(top.get("expiry") or "")}'
                    f' ({esc(top.get("dte"))}d)')
        # Badges
        badges = ""
        if golden:
            badges += _badge("★ Golden", CSS_GOLD, "rgba(255,213,74,0.14)")
        if (top.get("opening") or "").startswith("likely"):
            badges += _badge("Opening", CSS_ACCENT)
        ask = top.get("ask_pct")
        if isinstance(ask, (int, float)) and ask >= 60:
            badges += _badge(f"{int(ask)}% at ask", CSS_GREEN)
        elif isinstance(top.get("bid_pct"), (int, float)) and top["bid_pct"] >= 60:
            badges += _badge(f"{int(top['bid_pct'])}% at bid", CSS_RED)
        rc = top.get("repeat_count")
        if isinstance(rc, (int, float)) and rc >= 2:
            badges += _badge(f"repeat ×{int(rc)}", CSS_AMBER)
        for tag in (top.get("tags") or [])[:2]:
            if tag not in ("In Universe",):
                badges += _badge(tag, CSS_MUTED)
        # Break-even + earnings risk line
        meta_bits = []
        be = top.get("break_even")
        bed = top.get("be_distance_pct")
        if isinstance(be, (int, float)):
            bd = (f" ({bed:+.1f}% away)" if isinstance(bed, (int, float)) else "")
            meta_bits.append(f"B/E ${be:.2f}{bd}")
        ed = top.get("earnings_days")
        if isinstance(ed, (int, float)) and 0 <= ed <= 21:
            meta_bits.append(f"⚠ earns in {int(ed)}d")
        bp = _fmt_ts_et(top.get("biggest_print_ts"))
        if bp:
            meta_bits.append(f"biggest print {bp}")
        why = top.get("why") or ""
        more = (f'<span style="color:{CSS_MUTED};"> · +{len(r["contracts"])-1} more contract{"s" if len(r["contracts"])>2 else ""}</span>'
                if len(r["contracts"]) > 1 else "")
        rows.append(_row_wrap(
            f'<div style="display:flex;align-items:baseline;justify-content:space-between;">'
            f'<div>{_ticker_link(r["ticker"])}'
            f'<span style="color:{dir_col};font-weight:700;font-size:12px;margin-left:8px;">{esc(dir_txt)}</span>'
            f'{score_txt}</div>'
            f'<div style="font-size:14px;color:{CSS_AMBER};font-weight:700;">{_fmt_premium(top.get("premium"))}</div></div>'
            f'<div style="font-size:12.5px;color:{CSS_TEXT};margin-top:4px;">{contract}'
            f'<span style="color:{CSS_MUTED};"> · {_fmt_num(top.get("vol_oi"),"{:.0f}")}× OI</span>{more}</div>'
            f'<div style="margin-top:5px;">{badges}</div>'
            + (f'<div style="font-size:11.5px;color:{CSS_MUTED};margin-top:4px;line-height:1.4;">{esc(why)}</div>' if why else "")
            + (f'<div style="font-size:11px;color:{CSS_MUTED};margin-top:3px;">{esc(" · ".join(meta_bits))}</div>' if meta_bits else "")))
    return _card("Unusual options flow", "".join(rows), "intraday batch · 15m delayed")


def load_carryover() -> dict:
    """Pre-open OI-confirmed carryover payload (from carryover_flow.py).
    Returns {} when the file is absent or unreadable."""
    try:
        with open(CARRYOVER_PATH, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _fmt_expiry_short(iso: str) -> str:
    """'2026-07-02' → '7/2' (no leading zeros, no year)."""
    try:
        y, m, d = iso.split("-")
        return f"{int(m)}/{int(d)}"
    except Exception:
        return iso or ""


def _fmt_strike(v) -> str:
    if not isinstance(v, (int, float)):
        return esc(v)
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def _render_carryover(cf) -> str:
    """@flowgod-style 'did the whales hold?' section: yesterday's notable
    prints re-checked against this morning's OCC-settled open interest."""
    contracts = (cf or {}).get("contracts") or []
    if not contracts:
        return ""
    sess = _fmt_expiry_short(cf.get("session_date") or "") or "yesterday"
    held = [c for c in contracts if c.get("held") is True]

    # Block 1 — the confirmed ideas (whale held > half), as trade lines.
    idea_rows = []
    for c in held:
        typ = (c.get("type") or "").upper().replace("CALL", "Call").replace("PUT", "Put")
        typ = "Call" if typ.startswith("C") else ("Put" if typ.startswith("P") else typ)
        exp = _fmt_expiry_short(c.get("expiry") or "")
        prem = _fmt_premium(c.get("premium"))
        px = c.get("avg_px")
        px_txt = f" @ {px:g}" if isinstance(px, (int, float)) else ""
        idea_rows.append(_row_wrap(
            f'<div style="display:flex;align-items:baseline;justify-content:space-between;gap:8px;">'
            f'<div>{_ticker_link(c.get("ticker"))}'
            f'<span style="color:{CSS_TEXT};font-size:12.5px;margin-left:8px;">'
            f'{_fmt_strike(c.get("strike"))} {esc(typ)} '
            f'<span style="color:{CSS_MUTED};">({esc(exp)})</span></span></div>'
            f'<div style="font-size:12px;color:{CSS_GREEN};font-weight:700;white-space:nowrap;">'
            f'{esc(prem)}{esc(px_txt)} ✅</div></div>'))

    # Block 2 — the OI update line for every carried contract (✅/❌).
    oi_rows = []
    for c in contracts:
        held_c = c.get("held")
        delta = c.get("delta")
        vol = c.get("volume")
        if held_c is None or delta is None or not vol:
            frac = '<span style="color:%s;">OI pending</span>' % CSS_MUTED
            mark = "…"
            col = CSS_MUTED
        else:
            num = max(0, int(delta))
            frac = f"{num:,}/{int(vol):,}"
            mark = "✅" if held_c else "❌"
            col = CSS_GREEN if held_c else CSS_RED
        oi_rows.append(
            f'<div style="display:flex;align-items:baseline;justify-content:space-between;'
            f'padding:6px 14px;border-bottom:1px solid {CSS_BORDER};">'
            f'<span>{_ticker_link(c.get("ticker"), 13)}</span>'
            f'<span style="font-size:12px;color:{col};font-weight:700;'
            f'font-variant-numeric:tabular-nums;">{frac} {mark}</span></div>')

    parts = []
    if idea_rows:
        parts.append(
            f'<div style="font-size:11px;font-weight:700;color:{CSS_MUTED};'
            f'padding:10px 14px 4px;text-transform:uppercase;letter-spacing:.5px;">'
            f'Noteworthy flow — OI confirmed</div>' + "".join(idea_rows))
    parts.append(
        f'<div style="font-size:11px;font-weight:700;color:{CSS_MUTED};'
        f'padding:10px 14px 4px;text-transform:uppercase;letter-spacing:.5px;">'
        f'Open interest update</div>' + "".join(oi_rows))
    parts.append(
        f'<div style="font-size:10.5px;color:{CSS_MUTED};padding:9px 14px;line-height:1.5;">'
        f'✅ = whale held &gt;50% of the print overnight (Δ open interest ÷ flag-day volume). '
        f'Not advice.</div>')
    sub = f"{cf.get('held_count', len(held))}/{cf.get('total', len(contracts))} held · session {sess}"
    return _card("Overnight flow — did the whales hold?", "".join(parts), sub)


def _render_premarket(premarket, overnight, brief) -> str:
    """Pre-open pulse: what reports before the bell, what's already moving
    pre-market, and the overnight headlines on your names. Only blocks with
    data render; returns "" when there's nothing pre-open to say."""
    blocks = []

    # 1) Reporting before the open today (watchlist).
    bmo = [r for r in (brief.get("earnings") or [])
           if r.get("days_away") == 0
           and "bmo" in (r.get("bmo_amc") or "").lower()]
    if bmo:
        chips = "".join(
            f'<a href="{_link(r["ticker"])}" style="display:inline-block;'
            f'text-decoration:none;background:rgba(255,200,0,0.12);'
            f'border:1px solid rgba(255,200,0,0.4);border-radius:6px;'
            f'padding:3px 9px;margin:3px 6px 3px 0;font-size:12.5px;'
            f'font-weight:700;color:{CSS_TEXT};">{esc(r["ticker"])}</a>'
            for r in bmo[:8])
        blocks.append(
            f'<div style="padding:10px 14px;border-bottom:1px solid {CSS_BORDER};">'
            f'<div style="font-size:11px;font-weight:700;color:{CSS_AMBER};'
            f'text-transform:uppercase;letter-spacing:.5px;margin-bottom:5px;">'
            f'⏰ Reports before the open</div><div>{chips}</div></div>')

    # 2) Pre-market movers (market-wide) with a catalyst per name.
    def _mover_rows(items, col, arrow):
        out = []
        for m in (items or [])[:3]:
            pct = m.get("pct")
            pct_txt = f'{arrow}{abs(pct):.1f}%' if isinstance(pct, (int, float)) else ""
            cat = (m.get("catalyst") or {}).get("label") if isinstance(m.get("catalyst"), dict) else m.get("catalyst")
            cat_txt = (f'<div style="font-size:11px;color:{CSS_MUTED};margin-top:2px;'
                       f'line-height:1.35;">{esc(str(cat)[:88])}</div>' if cat else "")
            out.append(
                f'<div style="padding:6px 0;border-bottom:1px solid {CSS_BORDER};">'
                f'<div style="display:flex;justify-content:space-between;gap:8px;">'
                f'{_ticker_link(m.get("ticker"), 13)}'
                f'<span style="font-weight:700;color:{col};font-variant-numeric:tabular-nums;">'
                f'{pct_txt}</span></div>{cat_txt}</div>')
        return "".join(out)
    g = _mover_rows((premarket or {}).get("gainers"), CSS_GREEN, "+")
    l = _mover_rows((premarket or {}).get("losers"), CSS_RED, "−")
    if g or l:
        blocks.append(
            f'<div style="padding:10px 14px;border-bottom:1px solid {CSS_BORDER};">'
            f'<div style="font-size:11px;font-weight:700;color:{CSS_ACCENT};'
            f'text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px;">'
            f'📈 Pre-market movers <span style="color:{CSS_MUTED};font-weight:400;'
            f'text-transform:none;letter-spacing:0;">· extended-hours, list ~15m delayed</span></div>'
            + (g or "") + (l or "") + '</div>')

    # 3) Overnight headlines on your names.
    if overnight:
        news = "".join(
            f'<div style="padding:6px 0;border-bottom:1px solid {CSS_BORDER};">'
            f'<a href="{esc(n.get("url") or "#")}" style="color:{CSS_TEXT};'
            f'text-decoration:none;font-size:12.5px;line-height:1.4;">'
            f'{esc((n.get("headline") or "")[:120])}</a>'
            f'<div style="font-size:10.5px;color:{CSS_MUTED};margin-top:2px;">'
            f'{esc(", ".join((n.get("symbols") or [])[:4]))}'
            + (f' · {esc(n.get("source"))}' if n.get("source") else "") + '</div></div>'
            for n in overnight[:5])
        blocks.append(
            f'<div style="padding:10px 14px;">'
            f'<div style="font-size:11px;font-weight:700;color:{CSS_MUTED};'
            f'text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px;">'
            f'📰 Overnight headlines on your names</div>' + news + '</div>')

    if not blocks:
        return ""
    return _card("Pre-market pulse", "".join(blocks))


def _render_earnings(earn) -> str:
    rows = []
    for r in earn:
        col = CSS_RED if r.get("days_away", 9) <= 1 else CSS_AMBER
        rows.append(_row_wrap(
            f'<div style="display:flex;align-items:baseline;justify-content:space-between;">'
            f'<div>{_ticker_link(r["ticker"])}'
            f'<span style="color:{CSS_MUTED};font-size:12px;margin-left:8px;">{esc((r.get("company") or "")[:42])}</span></div>'
            f'<div style="font-size:12px;color:{col};font-weight:700;">{esc(r.get("dow",""))} {esc(r.get("bmo_amc",""))}</div></div>'
            f'<div style="font-size:11.5px;color:{CSS_MUTED};margin-top:3px;">{esc(r.get("when_date",""))} · in {r.get("days_away","?")}d</div>'))
    return _card("Earnings in the next 7 days", "".join(rows))


def _render_market(market, market_top) -> str:
    inner = ""
    g = market.get("global")
    if g:
        leaders = ", ".join(
            f'{esc(e.get("ticker"))} {e.get("vs_anchor"):+.1f}%'
            for e in g.get("leaders", []) if isinstance(e.get("vs_anchor"), (int, float)))
        laggards = ", ".join(
            f'{esc(e.get("ticker"))} {e.get("vs_anchor"):+.1f}%'
            for e in g.get("laggards", []) if isinstance(e.get("vs_anchor"), (int, float)))
        ay = g.get("anchor_ytd")
        inner += (
            f'<div style="padding:11px 14px;border-bottom:1px solid {CSS_BORDER};">'
            f'<div style="font-size:12.5px;color:{CSS_TEXT};">Global backdrop: '
            f'<b style="color:{CSS_ACCENT};">{esc(g.get("tone"))}</b>'
            + (f' · {esc(g.get("anchor_ticker"))} {ay:+.1f}% YTD' if isinstance(ay, (int, float)) else "")
            + '</div>'
            f'<div style="font-size:11.5px;color:{CSS_MUTED};margin-top:3px;">'
            f'Leaders {leaders or "—"} · Laggards {laggards or "—"} '
            f'<span style="color:{CSS_MUTED};">(YTD vs SPY)</span></div></div>')
    for ev in market.get("events", []):
        impact = (ev.get("impact") or "").lower()
        icol = CSS_RED if impact == "high" else CSS_AMBER if impact == "medium" else CSS_MUTED
        act = ev.get("actual")
        act_html = (f'<span style="color:{CSS_TEXT};font-weight:700;"> · actual {esc(act)}</span>'
                    if act else "")
        inner += (
            f'<div style="padding:9px 14px;border-bottom:1px solid {CSS_BORDER};">'
            f'<span style="color:{icol};font-weight:700;font-size:11px;">●</span> '
            f'<span style="font-size:12.5px;color:{CSS_TEXT};">{esc(ev.get("time"))} {esc(ev.get("title"))}</span>'
            f'<span style="font-size:11px;color:{CSS_MUTED};"> · fcst {esc(ev.get("forecast") or "—")} · prior {esc(ev.get("previous") or "—")}</span>{act_html}</div>')
    # Bonus: tape's top edge (market-wide) so the email always has alpha
    if market_top:
        tape = " · ".join(
            f'{esc(m.get("ticker"))} ({esc(m.get("tier"))}·{int(m.get("trade_score") or 0)})'
            for m in market_top[:4])
        inner += (f'<div style="padding:10px 14px;">'
                  f'<span style="font-size:11px;font-weight:700;color:{CSS_MUTED};">TOP TAPE EDGE</span>'
                  f'<div style="font-size:12.5px;color:{CSS_TEXT};margin-top:3px;">{tape}</div></div>')
    if not inner:
        return ""
    return _card("Market context", inner)


def _render_reports(reports) -> str:
    if not reports:
        return ""
    inner = "".join(
        f'<div style="padding:9px 14px;border-bottom:1px solid {CSS_BORDER};">'
        f'<a href="{SITE_URL}/reports/{esc(r["file"])}" style="color:{CSS_ACCENT};text-decoration:none;font-size:12.5px;">📄 {esc(r["label"])}</a>'
        f'<span style="font-size:11px;color:{CSS_MUTED};"> · {r["when"].strftime("%I:%M %p").lstrip("0")} ET</span></div>'
        for r in reports)
    return _card("New research (24h)", inner)


def _render_snapshot(snapshot) -> str:
    if not snapshot:
        return ""
    cells = []
    for r in snapshot:
        color = _grade_color(r["grade"])
        chg = r.get("chg")
        chg_html = ""
        if isinstance(chg, (int, float)):
            cc = CSS_GREEN if chg >= 0 else CSS_RED
            chg_html = f'<span style="color:{cc};font-size:10px;"> {chg:+.1f}%</span>'
        cells.append(
            f'<a href="{_link(r["ticker"])}" style="display:inline-block;padding:6px 9px;'
            f'margin:3px;background:{CSS_BG};border:1px solid {CSS_BORDER};border-radius:6px;'
            f'text-decoration:none;color:{CSS_TEXT};font-size:12px;">'
            f'<b>{esc(r["ticker"])}</b> <span style="color:{color};font-weight:700;">{esc(r["grade"])}</span>{chg_html}</a>')
    return f'''
    <div style="margin-top:22px;">
      <div style="font-size:12px;font-weight:700;color:{CSS_MUTED};text-transform:uppercase;letter-spacing:1px;margin-bottom:9px;">Watchlist snapshot</div>
      <div style="background:{CSS_PANEL};border:1px solid {CSS_BORDER};border-radius:10px;padding:8px;">{"".join(cells)}</div>
    </div>'''


def render_html(user, brief, brief_date, carryover=None,
                premarket=None, overnight=None) -> tuple[str, str]:
    name = esc((user.get("display_name") or "").strip() or "trader")
    subject, preheader = build_subject(brief)

    parts = []
    # Preheader (hidden preview text) + spacer so Gmail doesn't pull body text
    parts.append(
        f'<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;'
        f'font-size:1px;line-height:1px;color:{CSS_BG};opacity:0;">{esc(preheader)}'
        + ("&nbsp;&zwnj;" * 40) + '</div>')
    parts.append(
        f'<div style="background:{CSS_BG};padding:22px 14px;'
        f"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;"
        f'color:{CSS_TEXT};">'
        f'<div style="max-width:600px;margin:0 auto;">'
        f'<div style="padding:0 0 14px;border-bottom:1px solid {CSS_BORDER};">'
        f'<div style="font-size:11px;letter-spacing:1.5px;color:{CSS_MUTED};text-transform:uppercase;">TickerDesk · Morning Brief</div>'
        f'<div style="font-size:20px;font-weight:700;margin-top:4px;">{esc(brief_date)}</div>'
        f'<div style="font-size:13px;color:{CSS_MUTED};margin-top:5px;">Good morning, {name}. {esc(preheader)}</div>'
        f'</div>')

    parts.append(_render_playbook(brief["playbook"], brief.get("ideas")))
    # Pre-market pulse — what reports before the bell, what's already moving,
    # and overnight headlines on your names. Pre-open context up top.
    pm_html = _render_premarket(premarket, overnight, brief)
    if pm_html:
        parts.append(pm_html)
    # Overnight OI-confirmed carryover — pre-open flow context, so it sits
    # high, right under the playbook.
    co_html = _render_carryover(carryover)
    if co_html:
        parts.append(co_html)
    if brief["confluence"]:
        parts.append(_render_confluence(brief["confluence"]))
    parts.append(_render_changed(brief["changed"]))
    if brief["flow"]:
        parts.append(_render_flow(brief["flow"]))
    if brief["earnings"]:
        parts.append(_render_earnings(brief["earnings"]))
    parts.append(_render_market(brief["market"], brief["market_top"]))
    parts.append(_render_reports(brief["reports"]))
    parts.append(_render_snapshot(brief["snapshot"]))

    # Data-honesty footer
    parts.append(
        f'<div style="margin-top:26px;padding-top:14px;border-top:1px solid {CSS_BORDER};font-size:11px;color:{CSS_MUTED};line-height:1.6;">'
        f'<div style="font-weight:700;color:{CSS_MUTED};margin-bottom:4px;">Data freshness</div>'
        f'Flow: intraday batch (Polygon Options Starter, 15-min delayed) · '
        f'Grades: prior close / EOD batch · '
        f'Quotes: delayed 15m where shown · '
        f'Calendar: live feed, actuals fill in through the day · '
        f'News: cached intraday.<br>'
        f'Not investment advice. Verify against your broker before trading.'
        f'</div>')
    uurl = unsub_url(user.get("user_id") or "")
    parts.append(
        f'<div style="margin-top:14px;font-size:11px;color:{CSS_MUTED};">'
        f"You're receiving this because Daily Brief is on for your account. "
        f'<a href="{uurl}" style="color:{CSS_ACCENT};text-decoration:none;">Unsubscribe</a> · '
        f'<a href="{SITE_URL}/#watchlist" style="color:{CSS_ACCENT};text-decoration:none;">Manage preferences</a> · '
        f'<a href="{SITE_URL}" style="color:{CSS_ACCENT};text-decoration:none;">Open TickerDesk</a>'
        f'</div></div></div>')
    return subject, "".join(parts)


# ─────────────────────────────────────────────────────────────────────
# Resend send
# ─────────────────────────────────────────────────────────────────────

def send_email(to_email: str, subject: str, html_body: str,
               unsub: str = "") -> tuple[bool, str]:
    if not RESEND_API_KEY:
        return False, "RESEND_API_KEY not set"
    payload: dict[str, Any] = {
        "from": FROM_EMAIL, "to": [to_email],
        "subject": subject, "html": html_body,
    }
    # RFC 8058 one-click unsubscribe. Gmail/Yahoo/Apple render a native
    # "Unsubscribe" control from these headers and (for List-Unsubscribe-Post)
    # POST the URL directly — no login, no round trip through the inbox.
    # Required by Gmail/Yahoo bulk-sender rules before scaling volume.
    if unsub and unsub.startswith("http"):
        payload["headers"] = {
            "List-Unsubscribe": f"<{unsub}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }
    body = json.dumps(payload).encode("utf-8")
    # Resend sits behind Cloudflare; the default urllib UA trips WAF rule
    # 1010, so send a real-looking UA.
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=body,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": ("Mozilla/5.0 (compatible; TickerDesk-Brief/1.0; "
                           "+https://tickerdesk.io)"),
            "Accept": "application/json",
        }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return True, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        try:
            err = e.read().decode("utf-8")
        except Exception:
            err = str(e)
        return False, f"HTTP {e.code}: {err}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ─────────────────────────────────────────────────────────────────────
# Dry-run preview
# ─────────────────────────────────────────────────────────────────────

def write_preview(subject: str, html_body: str) -> None:
    os.makedirs(os.path.dirname(PREVIEW_PATH), exist_ok=True)
    doc = (f'<!doctype html><html><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<title>{esc(subject)}</title></head>'
           f'<body style="margin:0;background:{CSS_BG};">{html_body}</body></html>')
    with open(PREVIEW_PATH, "w", encoding="utf-8") as f:
        f.write(doc)
    kb = len(doc.encode("utf-8")) / 1024
    print(f"  Preview written: {PREVIEW_PATH} ({kb:.0f} KB"
          + (" ⚠ over 102KB Gmail clip limit" if kb > 102 else "") + ")")


def demo_user(swing, uoa, market_top, earnings) -> dict:
    """Synthetic watchlist for local preview — a BALANCED mix (earnings +
    flow + A-tier) so every section renders without Supabase."""
    a_tier = [tk for tk, sw in swing.items() if _is_a_tier(sw.get("grade"))]
    tickers: list[str] = []
    tickers += list(earnings.keys())[:3]                 # exercise earnings
    tickers += list(uoa.keys())[:4]                      # exercise flow
    tickers += [m.get("ticker") for m in market_top[:3]]  # tape edge
    tickers += a_tier                                    # fill with A-tier
    seen, uniq = set(), []
    for t in tickers:
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)
    return {"user_id": "demo", "email": "preview@tickerdesk.io",
            "display_name": "", "plan": "premium", "tickers": uniq[:16]}


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Build + write preview HTML, don't send")
    ap.add_argument("--me", default=None, help="Restrict to one email")
    ap.add_argument("--preview", action="store_true",
                    help="Force a synthetic demo brief (no Supabase needed)")
    ap.add_argument("--skip-idempotency", action="store_true")
    args = ap.parse_args()

    today = datetime.now(ET).date()
    brief_date = today.strftime("%A, %B %-d, %Y") if os.name != "nt" else \
        today.strftime("%A, %B ") + str(today.day) + today.strftime(", %Y")
    iso_date = today.isoformat()
    print(f"TickerDesk Daily Brief — {iso_date}")

    if today.weekday() >= 5 and not (args.dry_run or args.preview):
        print("  Weekend — skipping.")
        return

    print("  Loading signal data...")
    swing = load_swing()
    uoa, market_top = load_uoa()
    earnings = load_earnings_within(days=7)
    reports_by_tk = load_reports_by_ticker(hours=24)
    new_reports_all = load_new_reports_all(hours=24)
    market = load_market_context()
    carryover = load_carryover()
    if carryover.get("contracts"):
        print(f"    carryover: {carryover.get('held_count')}/{carryover.get('total')} "
              f"held (session {carryover.get('session_date')})")
    premarket = load_premarket()
    if premarket.get("gainers") or premarket.get("losers"):
        print(f"    premarket: {len(premarket.get('gainers', []))}↑ "
              f"{len(premarket.get('losers', []))}↓")
    print(f"    swing:{len(swing)} uoa:{len(uoa)} earn/7d:{len(earnings)} "
          f"reports:{len(new_reports_all)} events:{len(market.get('events', []))}")

    # Preview / dry-run without users → synthetic demo
    users: list[dict] = []
    if args.preview:
        users = [demo_user(swing, uoa, market_top, earnings)]
        print("  --preview: using synthetic demo watchlist")
    else:
        try:
            print("  Fetching opted-in users...")
            users = fetch_opted_in_users(restrict_email=args.me)
        except Exception as e:
            print(f"  Could not fetch users ({e}).")
            if args.dry_run:
                users = [demo_user(swing, uoa, market_top, earnings)]
                print("  dry-run: falling back to synthetic demo watchlist")
    print(f"    {len(users)} user(s) to brief")
    if not users:
        return

    sent = skipped = failed = 0
    preview_written = False
    for u in users:
        brief = build_brief(u["tickers"], swing, uoa, market_top, earnings,
                            reports_by_tk, new_reports_all, market)
        overnight = load_overnight_news(u["tickers"])
        subject, html_body = render_html(u, brief, brief_date, carryover,
                                         premarket, overnight)
        prefix = f"  → {u['email']:32s} ({len(u['tickers'])} tickers): "

        # Always write the first rendered email to the preview file.
        if (args.dry_run or args.preview) and not preview_written:
            write_preview(subject, html_body)
            preview_written = True

        if args.dry_run or args.preview:
            print(prefix + f"DRY-RUN subject={subject!r}")
            sent += 1
            continue

        if not args.skip_idempotency and already_sent_today(u["user_id"], iso_date):
            print(prefix + "skipped (already sent today)")
            skipped += 1
            continue

        ok, resp = send_email(u["email"], subject, html_body,
                              unsub=unsub_url(u.get("user_id") or ""))
        if ok:
            print(prefix + f"sent ({subject})")
            log_email(u["user_id"], u["email"], iso_date, "sent", subject,
                      {"tickers": u["tickers"],
                       "counts": {"flow": len(brief["flow"]),
                                  "confluence": len(brief["confluence"])}})
            sent += 1
        else:
            print(prefix + f"FAILED {resp[:200]}")
            log_email(u["user_id"], u["email"], iso_date, "failed", subject,
                      {"tickers": u["tickers"]}, error=resp[:500])
            failed += 1

    print(f"\nDone. sent={sent} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"  Fatal: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
