"""
daily_brief.py — Personalized morning email for TickerDesk subscribers.

Runs ~8:45 AM ET on weekdays (GitHub Actions cron). For every user with
`profiles.daily_brief_enabled = true`:

  1. Pull their watchlist from Supabase
  2. Cross-reference today's signal JSON for each watched ticker:
       - swing grade today + change vs prior run
       - UOA flow (top contract by premium, if any new today)
       - earnings within the next 7 days
       - new reports archived in the last 24h
  3. Compose a clean HTML email (mobile-friendly, single column)
  4. Send via Resend API
  5. Write the result to public.email_log (idempotent per brief_date)

Idempotency: before sending, we check email_log for an existing
'sent' row for (user_id, kind='daily_brief', brief_date=today). If we
find one we skip — so re-running the workflow same-day never spams.

Required env vars:
  SUPABASE_URL              https://uaeojibmhxbwkhpvmjwy.supabase.co
  SUPABASE_SERVICE_KEY      service_role key (bypasses RLS — keep in CI only)
  RESEND_API_KEY            re_XXXXXX
  FROM_EMAIL                e.g. "TickerDesk <brief@tickerdesk.io>"
                             (defaults to brief@tickerdesk.io if unset)

Run locally:
  python daily_brief.py            # sends to all opted-in users
  python daily_brief.py --dry-run  # build + log to stdout, don't send
  python daily_brief.py --me you@example.com  # restrict to one email
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any

import pytz

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ET = pytz.timezone("America/New_York")
_BASE = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(_BASE, "docs", "reports")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL") or "TickerDesk <brief@tickerdesk.io>"
SITE_URL = os.environ.get("SITE_URL") or "https://tickerdesk.io"


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
    """Returns {ticker: {grade, prev_grade, name, sector, price, chg, change}}.

    "change" is one of:
      - "new"      first time appearing today
      - "upgrade"  grade improved vs prior day
      - "downgrade" grade fell vs prior day
      - "same"     unchanged
    """
    data = _safe_load(os.path.join(REPORTS_DIR, "swing_report.json"))
    if not data or not data.get("runs"):
        return {}
    runs = data["runs"]
    # Sort by date desc just in case
    runs = sorted(runs, key=lambda r: r.get("date", ""), reverse=True)
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

    # Grade ordering for upgrade/downgrade detection (lower index = better)
    order = [
        "A+", "A", "A-", "B+", "B", "B-",
        "C+", "C", "C-", "D+", "D", "D-",
        "E+", "E", "E-", "F+", "F", "F-",
        "G+", "G",
    ]
    rank = {g: i for i, g in enumerate(order)}

    merged: dict[str, dict] = {}
    for t, row in today.items():
        pg = (prev.get(t) or {}).get("grade")
        change = "same"
        if pg is None:
            change = "new"
        elif rank.get(row["grade"], 99) < rank.get(pg, 99):
            change = "upgrade"
        elif rank.get(row["grade"], 99) > rank.get(pg, 99):
            change = "downgrade"
        merged[t] = {
            "grade": row["grade"],
            "prev_grade": pg,
            "name": row.get("n", ""),
            "sector": row.get("sec", ""),
            "price": row.get("p"),
            "chg": row.get("chg"),
            "change": change,
        }
    return merged


def load_uoa() -> dict[str, list[dict]]:
    """Returns {ticker: [top_contract_rows]} — top 3 by premium per ticker.

    "New flow" gating happens implicitly because uoa_latest.json is rewritten
    every scan with the day's signals only.
    """
    data = _safe_load(os.path.join(REPORTS_DIR, "uoa_latest.json"))
    if not data:
        return {}
    by_ticker: dict[str, list[dict]] = {}
    for row in data.get("rows", []):
        t = row.get("ticker")
        if not t:
            continue
        by_ticker.setdefault(t, []).append(row)
    # Top 3 contracts per ticker by premium
    for t in by_ticker:
        by_ticker[t] = sorted(by_ticker[t],
                              key=lambda r: -(r.get("premium") or 0))[:3]
    return by_ticker


def load_earnings_within(days: int = 7) -> dict[str, dict]:
    """Returns {ticker: {when_date, dow, bmo_amc}} for earnings in next N days."""
    data = _safe_load(os.path.join(REPORTS_DIR, "earnings_anticipated.json"))
    if not data:
        return {}
    today = datetime.now(ET).date()
    horizon = today + timedelta(days=days)
    out: dict[str, dict] = {}
    for day in data.get("days", []):
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
                    }
    return out


def load_new_reports(hours: int = 24) -> dict[str, list[dict]]:
    """Returns {ticker: [{file, label, type}]} for ticker-tagged reports
    archived within the last N hours. Only ticker_*.pdf and altdata_*.pdf
    are tied to a specific name; broader scans (smid_scanner, qm_monthly)
    are surfaced site-wide but not per-watchlist."""
    data = _safe_load(os.path.join(REPORTS_DIR, "manifest.json"))
    if not data:
        return {}
    cutoff = datetime.now(ET) - timedelta(hours=hours)
    by_ticker: dict[str, list[dict]] = {}
    for rtype, files in (data.get("reports") or {}).items():
        for f in files:
            fname = f.get("file", "")
            # ticker_NVDA_2026-05-20_2250.pdf or altdata_PLTR_2026-05-15_1740.pdf
            parts = fname.split("_")
            if len(parts) < 4:
                continue
            head = parts[0]
            tk = parts[1]
            if head not in ("ticker", "altdata"):
                continue
            if not tk.isupper() or not tk.isalpha():
                continue
            # Parse date+time from filename
            try:
                stamp = datetime.strptime(
                    f"{parts[2]}_{parts[3].split('.')[0]}", "%Y-%m-%d_%H%M"
                )
                stamp = ET.localize(stamp)
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


# ─────────────────────────────────────────────────────────────────────
# Supabase REST helpers — service role bypasses RLS so we can read
# everyone's watchlists in one shot. Keep the service key OUT of the
# client; this script only runs in CI.
# ─────────────────────────────────────────────────────────────────────

def _supabase_get(path: str, params: dict | None = None) -> Any:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY not set")
    q = ""
    if params:
        q = "?" + "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{SUPABASE_URL}/rest/v1/{path}{q}"
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _supabase_post(path: str, body: dict | list, prefer: str = "") -> Any:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    data = json.dumps(body).encode("utf-8")
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
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
        # Surfacing the body helps debug Supabase RLS / constraint errors
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8")
        except Exception:
            pass
        raise RuntimeError(f"Supabase POST {path} HTTP {e.code}: {body_txt}") from e


def fetch_opted_in_users(restrict_email: str | None = None) -> list[dict]:
    """Returns [{user_id, email, display_name, last_seen, plan, tickers:[..]}].

    We need to hit auth.users for the email — that table is server-only,
    accessible via the admin API. The simplest path is to call the
    /auth/v1/admin/users endpoint.
    """
    # 1) Profiles where daily_brief is on
    profiles = _supabase_get("profiles", {
        "select": "id,display_name,last_seen,subscription_tier,daily_brief_enabled",
        "daily_brief_enabled": "eq.true",
    })
    if not profiles:
        return []
    ids = [p["id"] for p in profiles]

    # 2) Watchlist tickers for those profiles
    in_clause = "(" + ",".join(ids) + ")"
    wl = _supabase_get("watchlists", {
        "select": "user_id,ticker",
        "user_id": f"in.{in_clause}",
    })
    by_user: dict[str, list[str]] = {}
    for row in wl or []:
        by_user.setdefault(row["user_id"], []).append(row["ticker"])

    # 3) Emails via Supabase admin endpoint. Avoid one-call-per-user — the
    # admin/users endpoint paginates. Pull a generous page and filter.
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
            continue  # nothing to brief about
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
    """Idempotency check — don't re-send the same brief day."""
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
# Brief assembly — per-user data slice
# ─────────────────────────────────────────────────────────────────────

def build_brief(tickers: list[str], swing: dict, uoa: dict,
                earnings: dict, new_reports: dict) -> dict:
    """Returns the structured payload we render into HTML."""
    sections = {
        "grade_changes": [],   # upgrades / downgrades / new A-tier appearances
        "flow": [],            # tickers with UOA today
        "earnings": [],        # earnings within 7 days
        "reports": [],         # new ticker-specific reports
        "snapshot": [],        # every watched ticker's current grade
    }
    for tk in sorted(set(tickers)):
        sw = swing.get(tk)
        if sw:
            sections["snapshot"].append({"ticker": tk, **sw})
            if sw["change"] in ("upgrade", "downgrade", "new"):
                sections["grade_changes"].append({"ticker": tk, **sw})
        flows = uoa.get(tk)
        if flows:
            sections["flow"].append({
                "ticker": tk,
                "contracts": [
                    {
                        "type": c.get("type"),
                        "strike": c.get("strike"),
                        "expiry": c.get("expiry"),
                        "premium": c.get("premium"),
                        "vol_oi": c.get("vol_oi"),
                        "dte": c.get("dte"),
                    } for c in flows
                ],
            })
        er = earnings.get(tk)
        if er:
            sections["earnings"].append({"ticker": tk, **er})
        rep = new_reports.get(tk)
        if rep:
            sections["reports"].append({"ticker": tk, "reports": rep})
    # Sort grade_changes: upgrades first, then new, then downgrades
    pri = {"upgrade": 0, "new": 1, "downgrade": 2, "same": 3}
    sections["grade_changes"].sort(
        key=lambda r: (pri.get(r["change"], 9), r["ticker"]))
    # Sort flow by highest premium first
    sections["flow"].sort(
        key=lambda r: -(r["contracts"][0].get("premium") or 0))
    # Earnings sorted by date
    sections["earnings"].sort(key=lambda r: r.get("when_date") or "")
    return sections


# ─────────────────────────────────────────────────────────────────────
# HTML rendering — inline-styled, single column, mobile friendly
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


def _fmt_premium(p) -> str:
    if not p:
        return "—"
    try:
        p = float(p)
    except (TypeError, ValueError):
        return "—"
    if p >= 1e6:
        return f"${p/1e6:.1f}M"
    if p >= 1e3:
        return f"${p/1e3:.0f}K"
    return f"${p:.0f}"


def _grade_color(g: str) -> str:
    if not g:
        return CSS_MUTED
    head = g[0]
    return {
        "A": CSS_GREEN, "B": CSS_GREEN, "C": CSS_ACCENT,
        "D": CSS_AMBER, "E": "#ff8c1a", "F": CSS_RED, "G": CSS_RED,
    }.get(head, CSS_MUTED)


def _change_badge(change: str, prev_grade: str | None) -> str:
    if change == "upgrade":
        return (f'<span style="color:{CSS_GREEN};font-weight:600">'
                f'▲ upgrade{f" from {prev_grade}" if prev_grade else ""}</span>')
    if change == "downgrade":
        return (f'<span style="color:{CSS_RED};font-weight:600">'
                f'▼ downgrade{f" from {prev_grade}" if prev_grade else ""}</span>')
    if change == "new":
        return f'<span style="color:{CSS_AMBER};font-weight:600">★ new appearance</span>'
    return ""


def render_html(user: dict, brief: dict, brief_date: str) -> tuple[str, str]:
    """Returns (subject, html)."""
    name = (user.get("display_name") or "").strip() or "trader"
    n_changes = len(brief["grade_changes"])
    n_flow = len(brief["flow"])
    n_earn = len(brief["earnings"])
    n_rep = len(brief["reports"])
    headline_bits = []
    if n_changes:
        headline_bits.append(f"{n_changes} grade chg")
    if n_flow:
        headline_bits.append(f"{n_flow} flow")
    if n_earn:
        headline_bits.append(f"{n_earn} earnings ≤7d")
    if n_rep:
        headline_bits.append(f"{n_rep} new report{'s' if n_rep > 1 else ''}")
    headline = " · ".join(headline_bits) or "Watchlist quiet today"
    subject = f"TickerDesk Brief — {brief_date} · {headline}"

    # ── header ──
    parts: list[str] = []
    parts.append(f'''
<div style="background:{CSS_BG};padding:24px 16px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:{CSS_TEXT};">
  <div style="max-width:600px;margin:0 auto;">
    <div style="padding:0 0 16px;border-bottom:1px solid {CSS_BORDER};">
      <div style="font-size:11px;letter-spacing:1.5px;color:{CSS_MUTED};text-transform:uppercase;">TickerDesk · Daily Brief</div>
      <div style="font-size:22px;font-weight:600;margin-top:4px;">{brief_date}</div>
      <div style="font-size:14px;color:{CSS_MUTED};margin-top:6px;">Good morning, {name}. {headline}.</div>
    </div>
''')

    # ── grade changes ──
    if brief["grade_changes"]:
        parts.append(_section("Grade changes", brief["grade_changes"],
                              _render_grade_row))
    # ── unusual flow ──
    if brief["flow"]:
        parts.append(_section("Unusual options flow today", brief["flow"],
                              _render_flow_row))
    # ── earnings ──
    if brief["earnings"]:
        parts.append(_section("Earnings in the next 7 days", brief["earnings"],
                              _render_earnings_row))
    # ── new reports ──
    if brief["reports"]:
        parts.append(_section("New research reports", brief["reports"],
                              _render_report_row))
    # ── snapshot ──
    if brief["snapshot"]:
        parts.append(_section_snapshot(brief["snapshot"]))

    # ── footer ──
    parts.append(f'''
    <div style="margin-top:32px;padding-top:16px;border-top:1px solid {CSS_BORDER};font-size:12px;color:{CSS_MUTED};">
      You're getting this because Daily Brief is enabled on your TickerDesk account.
      <a href="{SITE_URL}/#watchlist" style="color:{CSS_ACCENT};text-decoration:none;">Manage on your dashboard</a>
      &nbsp;·&nbsp;
      <a href="{SITE_URL}" style="color:{CSS_ACCENT};text-decoration:none;">Open TickerDesk</a>
    </div>
  </div>
</div>
''')
    return subject, "".join(parts)


def _section(title: str, rows: list[dict], render_row) -> str:
    inner = "".join(render_row(r) for r in rows)
    return f'''
    <div style="margin-top:24px;">
      <div style="font-size:13px;font-weight:600;color:{CSS_MUTED};text-transform:uppercase;letter-spacing:1px;margin-bottom:10px;">{title}</div>
      <div style="background:{CSS_PANEL};border:1px solid {CSS_BORDER};border-radius:10px;overflow:hidden;">
        {inner}
      </div>
    </div>'''


def _render_grade_row(r: dict) -> str:
    color = _grade_color(r["grade"])
    badge = _change_badge(r["change"], r.get("prev_grade"))
    chg = r.get("chg")
    chg_txt = ""
    if isinstance(chg, (int, float)):
        col = CSS_GREEN if chg >= 0 else CSS_RED
        chg_txt = (f'<span style="color:{col};font-size:12px;">'
                   f'{chg:+.2f}%</span>')
    price = (f"${r['price']:.2f}"
             if isinstance(r.get("price"), (int, float)) else "")
    return f'''
        <div style="padding:12px 14px;border-bottom:1px solid {CSS_BORDER};">
          <div style="display:flex;align-items:baseline;justify-content:space-between;">
            <div>
              <a href="{SITE_URL}/?t={r['ticker']}" style="font-weight:600;color:{CSS_TEXT};text-decoration:none;font-size:15px;">{r['ticker']}</a>
              <span style="color:{CSS_MUTED};font-size:12px;margin-left:8px;">{r.get('name','')[:50]}</span>
            </div>
            <div style="font-size:14px;color:{color};font-weight:600;">{r['grade']}</div>
          </div>
          <div style="font-size:12px;color:{CSS_MUTED};margin-top:4px;">
            {badge} &nbsp; {price} {chg_txt}
          </div>
        </div>'''


def _render_flow_row(r: dict) -> str:
    top = r["contracts"][0]
    typ = (top.get("type") or "").upper()
    typ_col = CSS_GREEN if typ == "CALL" else CSS_RED
    # Defensive formatting — vol_oi / strike / dte can occasionally be None
    # in source data, and we don't want one bad row to fail the whole brief.
    voi = top.get("vol_oi")
    voi_txt = f"{voi:.1f}" if isinstance(voi, (int, float)) else "—"
    strike = top.get("strike")
    strike_txt = f"${strike}" if strike is not None else "—"
    dte = top.get("dte")
    dte_txt = f"{dte}d" if dte is not None else "—"
    more = (f" · {len(r['contracts']) - 1} more"
            if len(r["contracts"]) > 1 else "")
    return f'''
        <div style="padding:12px 14px;border-bottom:1px solid {CSS_BORDER};">
          <div style="display:flex;align-items:baseline;justify-content:space-between;">
            <div>
              <a href="{SITE_URL}/?t={r['ticker']}" style="font-weight:600;color:{CSS_TEXT};text-decoration:none;font-size:15px;">{r['ticker']}</a>
              <span style="color:{typ_col};font-size:12px;margin-left:8px;font-weight:600;">{typ}</span>
              <span style="color:{CSS_MUTED};font-size:12px;margin-left:6px;">{strike_txt} · {top.get('expiry') or '—'} ({dte_txt})</span>
            </div>
            <div style="font-size:14px;color:{CSS_AMBER};font-weight:600;">{_fmt_premium(top.get('premium'))}</div>
          </div>
          <div style="font-size:12px;color:{CSS_MUTED};margin-top:4px;">
            vol/oi {voi_txt} on top contract{more}
          </div>
        </div>'''


def _render_earnings_row(r: dict) -> str:
    return f'''
        <div style="padding:12px 14px;border-bottom:1px solid {CSS_BORDER};">
          <div style="display:flex;align-items:baseline;justify-content:space-between;">
            <div>
              <a href="{SITE_URL}/?t={r['ticker']}" style="font-weight:600;color:{CSS_TEXT};text-decoration:none;font-size:15px;">{r['ticker']}</a>
              <span style="color:{CSS_MUTED};font-size:12px;margin-left:8px;">{r.get('company','')[:48]}</span>
            </div>
            <div style="font-size:12px;color:{CSS_AMBER};font-weight:600;">{r.get('dow','')} {r.get('bmo_amc','')}</div>
          </div>
          <div style="font-size:12px;color:{CSS_MUTED};margin-top:4px;">{r.get('when_date','')}</div>
        </div>'''


def _render_report_row(r: dict) -> str:
    reps = r["reports"]
    items = "".join(
        f'<div style="font-size:12px;color:{CSS_ACCENT};margin-top:3px;">'
        f'<a href="{SITE_URL}/reports/{rep["file"]}" style="color:{CSS_ACCENT};text-decoration:none;">{rep["type"]} · {rep["label"]}</a>'
        f'</div>' for rep in reps
    )
    return f'''
        <div style="padding:12px 14px;border-bottom:1px solid {CSS_BORDER};">
          <a href="{SITE_URL}/?t={r['ticker']}" style="font-weight:600;color:{CSS_TEXT};text-decoration:none;font-size:15px;">{r['ticker']}</a>
          {items}
        </div>'''


def _section_snapshot(rows: list[dict]) -> str:
    cells = []
    for r in rows:
        color = _grade_color(r["grade"])
        cells.append(
            f'<a href="{SITE_URL}/?t={r["ticker"]}" '
            f'style="display:inline-block;padding:6px 10px;margin:3px;background:{CSS_BG};'
            f'border:1px solid {CSS_BORDER};border-radius:6px;text-decoration:none;'
            f'color:{CSS_TEXT};font-size:12px;">'
            f'<b>{r["ticker"]}</b> '
            f'<span style="color:{color};font-weight:600;">{r["grade"]}</span>'
            f'</a>'
        )
    inner = "".join(cells)
    return f'''
    <div style="margin-top:24px;">
      <div style="font-size:13px;font-weight:600;color:{CSS_MUTED};text-transform:uppercase;letter-spacing:1px;margin-bottom:10px;">Watchlist snapshot</div>
      <div style="background:{CSS_PANEL};border:1px solid {CSS_BORDER};border-radius:10px;padding:10px;">
        {inner}
      </div>
    </div>'''


# ─────────────────────────────────────────────────────────────────────
# Resend send — single tiny HTTP call per recipient
# ─────────────────────────────────────────────────────────────────────

def send_email(to_email: str, subject: str, html: str) -> tuple[bool, str]:
    if not RESEND_API_KEY:
        return False, "RESEND_API_KEY not set"
    body = json.dumps({
        "from": FROM_EMAIL,
        "to": [to_email],
        "subject": subject,
        "html": html,
    }).encode("utf-8")
    # NOTE: Resend's API is fronted by Cloudflare and the default urllib
    # User-Agent ("Python-urllib/3.12") trips Cloudflare WAF rule 1010
    # (banned browser signature). A real-looking UA gets through cleanly.
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=body,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": ("Mozilla/5.0 (compatible; TickerDesk-Brief/1.0; "
                           "+https://tickerdesk.io)"),
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            txt = r.read().decode("utf-8")
            return True, txt
    except urllib.error.HTTPError as e:
        try:
            err = e.read().decode("utf-8")
        except Exception:
            err = str(e)
        return False, f"HTTP {e.code}: {err}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Build emails but don't send; print first 300 chars")
    ap.add_argument("--me", default=None,
                    help="Restrict to one email address (testing)")
    ap.add_argument("--skip-idempotency", action="store_true",
                    help="Re-send even if email_log already has a sent row")
    args = ap.parse_args()

    today = datetime.now(ET).date()
    brief_date = today.isoformat()
    print(f"TickerDesk Daily Brief — {brief_date}")

    # Skip on weekends — market data is stale and we don't want noise.
    if today.weekday() >= 5:
        print("  Weekend — skipping. (Cron should already exclude this, but"
              " belt-and-suspenders.)")
        return

    print("  Loading signal data...")
    swing = load_swing()
    uoa = load_uoa()
    earnings = load_earnings_within(days=7)
    new_reports = load_new_reports(hours=24)
    print(f"    swing: {len(swing)} tickers · uoa: {len(uoa)} tickers"
          f" · earnings/7d: {len(earnings)} · new reports: {len(new_reports)}")

    print("  Fetching opted-in users...")
    users = fetch_opted_in_users(restrict_email=args.me)
    print(f"    {len(users)} user(s) to brief")
    if not users:
        return

    sent = skipped = failed = 0
    for u in users:
        brief = build_brief(u["tickers"], swing, uoa, earnings, new_reports)
        subject, html = render_html(u, brief, brief_date)
        prefix = f"  → {u['email']:32s} ({len(u['tickers'])} tickers): "

        if not args.skip_idempotency and already_sent_today(u["user_id"], brief_date):
            print(prefix + "skipped (already sent today)")
            skipped += 1
            continue

        if args.dry_run:
            print(prefix + f"DRY-RUN  subject={subject!r}")
            print(f"      preview: {html[:300]}...")
            sent += 1
            continue

        ok, resp = send_email(u["email"], subject, html)
        if ok:
            print(prefix + f"sent  ({subject})")
            log_email(u["user_id"], u["email"], brief_date, "sent",
                      subject,
                      {"counts": {k: len(v) for k, v in brief.items()},
                       "tickers": u["tickers"]})
            sent += 1
        else:
            print(prefix + f"FAILED  {resp[:200]}")
            log_email(u["user_id"], u["email"], brief_date, "failed",
                      subject,
                      {"tickers": u["tickers"]}, error=resp[:500])
            failed += 1

    print(f"\nDone. sent={sent}  skipped={skipped}  failed={failed}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Never crash the workflow — log and exit 0 so other jobs unaffected.
        print(f"  Fatal: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
