"""SEO landing pages — static, keyword-targeted, generated nightly.

One page per keyword cluster from the site audit. Each page:
  - targets a distinct query (no thin/duplicate content),
  - cites REAL numbers pulled from published docs/reports/*.json (any
    stat that can't be found is omitted, never guessed),
  - carries canonical + OG + JSON-LD,
  - deep-links into the app via the hash router (/#uoa etc.),
  - is registered in sitemap.xml.

Runs in CI after the report JSONs are refreshed so the live-stat
callouts track the latest ledger. Static output; the only client JS is the
small Daily Brief sign-up handler (SIGNUP_JS), and the form still works
without it (plain POST to the worker, which answers with an HTML page).
"""

import itertools
import json
import math
import os
import re
import sys

_BASE = os.path.dirname(os.path.abspath(__file__))
R = lambda *p: os.path.join(_BASE, *p)
# Overridable so tests can build from fixture JSON into a temp folder.
OUT_DIR = os.environ.get("TD_SITE_OUT") or R("docs")
REPORTS_DIR = os.environ.get("TD_REPORTS_DIR") or R("docs", "reports")
WORKER_JS = R("cloudflare-worker", "worker.js")
SITE = "https://tickerdesk.io"


def _load(path, default=None):
    try:
        with open(os.path.join(REPORTS_DIR, path), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {} if default is None else default


def _num(v):
    """v as a float, or None for anything that is not a finite real number.

    Every published stat goes through this: a string, NaN or infinity in the
    data means the stat is left out, never pasted into the page."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def _fmt_int(n):
    """'6,951' for a finite number, else None."""
    v = _num(n)
    return None if v is None else f"{int(v):,}"


def _numtxt(v):
    """A finite number as plain text ('54', '0.66'), else None."""
    return None if _num(v) is None else str(v)


def _pct(v, dp=2):
    """Signed percent for HTML: +0.33% / &minus;0.23%."""
    v = _num(v)
    if v is None:
        return None
    v = round(v, dp)
    s = f"{abs(v):.{dp}f}%"
    return ("+" if v > 0 else "&minus;" if v < 0 else "") + s


def _ci_bounds(ci):
    if isinstance(ci, (list, tuple)) and len(ci) == 2:
        lo, hi = _num(ci[0]), _num(ci[1])
        if lo is not None and hi is not None:
            return lo, hi
    return None


def _ci_text(ci):
    b = _ci_bounds(ci)
    return None if b is None else _pct(b[0]) + " to " + _pct(b[1])


def _ci_read(ci):
    """Plain-English read of a 95% range: does it rule out zero?"""
    b = _ci_bounds(ci)
    if b is None:
        return None
    if b[0] > 0:
        return "measurably positive"
    if b[1] < 0:
        return "measurably negative"
    return "not distinguishable from zero"


def _date_of(iso):
    """'2026-11-16T19:17:37+00:00' -> 'November 16, 2026' (US/Eastern)."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if d.tzinfo is not None:
            d = d.astimezone(ZoneInfo("America/New_York"))
        return d.strftime("%B ") + str(d.day) + d.strftime(", %Y")
    except Exception:
        return None


# ── scan cadence (read from the worker that actually fires the scans) ─────
def uoa_slots():
    """The ET clock times the Cloudflare worker dispatches the flow scan."""
    try:
        with open(WORKER_JS, encoding="utf-8") as f:
            m = re.search(r"const UOA_SLOTS\s*=\s*\[([^\]]*)\]", f.read())
        return re.findall(r'"(\d{2}:\d{2})"', m.group(1)) if m else []
    except Exception:
        return []


def _clock(hhmm):
    h, m = (int(x) for x in hhmm.split(":"))
    return f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def scan_cadence(slots=None):
    """'every 30 minutes from 9:45 AM to 3:45 PM ET' or None."""
    slots = uoa_slots() if slots is None else slots
    if len(slots) < 2:
        return None
    mins = [int(s[:2]) * 60 + int(s[3:]) for s in slots]
    gaps = {b - a for a, b in itertools.pairwise(mins)}
    every = f"every {gaps.pop()} minutes" if len(gaps) == 1 else f"{len(slots)} times a day"
    return f"{every} from {_clock(slots[0])} to {_clock(slots[-1])} ET"


# ── live stats (defensive — return None when absent) ──────────────────────
EDGE = _load("uoa_edge.json")
LEVELS = _load("level_stats.json")
TOP25 = _load("uoa_top25_daily.json")
RESEARCH = _load("uoa_research.json")
HLAB = _load("hypothesis_lab.json")
SCANS = _load("scan_outcomes.json")


def edge_stat():
    """Headline flow numbers, or {} if the ledger isn't published."""
    ro = (EDGE.get("rich_overall") or {}).get("5") or {}
    oi = EDGE.get("oi_confirmation") or {}
    out = {}
    # Only finite numbers get in; anything else (a string, NaN, a stray
    # HTML snippet) drops the stat. Text values are pre-formatted here.
    if _num(EDGE.get("total_signals")):
        out["total"] = _fmt_int(EDGE["total_signals"])
    if _num(EDGE.get("matured_5d")):
        out["matured"] = _fmt_int(EDGE["matured_5d"])
    if _num(ro.get("ev")) is not None:
        out["ev"] = ro["ev"]
    if _numtxt(ro.get("win_rate")) is not None:
        out["win"] = _numtxt(ro["win_rate"])
    if _numtxt(ro.get("profit_factor")) is not None:
        out["pf"] = _numtxt(ro["profit_factor"])
    if _ci_bounds(ro.get("ci95")):
        out["ci"] = ro["ci95"]
    if _numtxt(oi.get("confirm_rate")) is not None:
        out["oi_rate"] = _numtxt(oi["confirm_rate"])
    if _num(oi.get("checked")):
        out["oi_checked"] = _fmt_int(oi["checked"])
    gs = (EDGE.get("by_type") or {}).get("golden_sweep") or {}
    grich = gs.get("rich") or {}
    if (
        grich.get("status") == "active"
        and _num(grich.get("n"))
        and _num(grich.get("ev")) is not None
    ):
        out["gs"] = grich
    if _num(LEVELS.get("sessions")):
        out["lvl_sessions"] = LEVELS["sessions"]
    if _num(TOP25.get("universe_total")):
        out["universe"] = _fmt_int(TOP25["universe_total"])
    asof = _date_of(EDGE.get("generated"))
    if asof:
        out["asof"] = asof
    return out


ST = edge_stat()
CADENCE = scan_cadence()


# ── shared template ───────────────────────────────────────────────────────
CSS = """
  body { margin:0; padding:48px 20px 80px; background:#000; color:#e8e8e8;
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
    line-height:1.6; }
  main { max-width:680px; margin:0 auto; }
  .nav { display:flex; justify-content:space-between; align-items:center;
    border-bottom:1px solid #2b2b2b; padding-bottom:16px; margin-bottom:32px; }
  .nav a { color:#ff9e1b; text-decoration:none; font-size:13px; font-weight:600; }
  .nav a:hover { text-decoration:underline; }
  h1 { font-size:30px; font-weight:800; margin:0 0 10px; line-height:1.2; }
  h2 { font-size:18px; font-weight:700; margin:34px 0 10px; }
  .sub { color:#9a9a9a; font-size:15px; margin-bottom:26px; }
  p, li { font-size:14.5px; color:#c9c9c9; }
  a { color:#ff9e1b; }
  ul { padding-left:20px; }
  li { margin:6px 0; }
  .callout { background:#0e0e0e; border:1px solid #2b2b2b; border-radius:10px;
    padding:16px 18px; margin:20px 0; }
  .callout b { color:#e8e8e8; }
  .stat-row { display:flex; flex-wrap:wrap; gap:22px; margin:6px 0 2px; }
  .stat-row div { font-size:12px; color:#7a7a7a; }
  .stat-row b { display:block; font-size:22px; color:#e8e8e8;
    font-variant-numeric:tabular-nums; }
  .faq { margin-top:12px; }
  .faq details { border-bottom:1px solid #1e1e1e; padding:10px 0; }
  .faq summary { cursor:pointer; font-weight:600; font-size:14px; color:#e8e8e8; }
  .faq p { margin:8px 0 0; }
  .cta { display:inline-block; background:#ff9e1b; color:#000; font-weight:700;
    padding:11px 24px; border-radius:8px; text-decoration:none; margin-top:10px; }
  .related { padding-left:20px; }
  .related li { margin:4px 0; }
  .disc { margin-top:30px; font-size:12px; color:#7a7a7a; }
  footer { margin-top:44px; padding-top:16px; border-top:1px solid #2b2b2b;
    font-size:12px; color:#7a7a7a; }
  footer a { color:#7a7a7a; }
  .signup { background:#0e0e0e; border:1px solid #2b2b2b; border-radius:10px;
    padding:18px 20px; margin:26px 0; }
  .signup.big { border-color:#ff9e1b; padding:22px; }
  .signup h2, .signup h3 { margin:0 0 6px; }
  .signup h3 { font-size:15px; font-weight:700; color:#e8e8e8; }
  .signup p { margin:0 0 12px; font-size:13.5px; }
  .bs-form { display:flex; gap:8px; flex-wrap:wrap; position:relative; }
  .bs-form input[type=email] { flex:1 1 220px; background:#000; color:#e8e8e8;
    border:1px solid #3a3a3a; border-radius:8px; padding:10px 12px; font-size:14px; }
  .bs-form input[type=email]:focus { outline:none; border-color:#ff9e1b; }
  .bs-form button { background:#ff9e1b; color:#000; font-weight:700; border:0;
    border-radius:8px; padding:10px 20px; font-size:14px; cursor:pointer; }
  .bs-form button:disabled { opacity:.6; cursor:default; }
  .bs-hp { position:absolute; left:-9999px; width:1px; height:1px; opacity:0; }
  .bs-status { margin-top:8px; font-size:13px; min-height:1em; }
  .bs-status.ok { color:#3ddc84; }
  .bs-status.err { color:#ff6b6b; }
"""

# Daily Brief sign-up. Posts to the worker, which saves the address and
# sends a confirm-your-email message (double opt-in). Success is shown only
# on a 2xx answer; any other answer shows the worker's own error text.
SIGNUP_ENDPOINT = "https://api.tickerdesk.io/brief/subscribe"
BRIEF_ARRIVAL = "shortly before 9:00 AM ET on market days"

SIGNUP_JS = r"""
document.querySelectorAll("form.bs-form").forEach(function (f) {
  f.addEventListener("submit", function (e) {
    e.preventDefault();
    var em = f.querySelector("input[type=email]");
    var hp = f.querySelector("input[name=website]");
    var btn = f.querySelector("button");
    var st = f.parentNode.querySelector(".bs-status");
    var email = (em.value || "").trim();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
      st.className = "bs-status err";
      st.textContent = "Please enter a valid email address.";
      return;
    }
    var label = btn.textContent;
    btn.disabled = true; btn.textContent = "Sending...";
    st.className = "bs-status"; st.textContent = "";
    fetch(f.action, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: email, source: f.getAttribute("data-source"),
                             website: hp ? hp.value : "" })
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (!r.ok) throw new Error(d.error ||
          ("Sign-up failed (error " + r.status + "). Please try again."));
        st.className = "bs-status ok";
        st.textContent = "✓ " + (d.message ||
          "Check your inbox to confirm your subscription.");
        em.value = "";
      });
    }).catch(function (err) {
      st.className = "bs-status err";
      st.textContent = (err && err.message && !/fetch/i.test(err.message))
        ? err.message
        : "Couldn't reach TickerDesk. Check your connection and try again.";
    }).then(function () { btn.disabled = false; btn.textContent = label; });
  });
});
"""


def signup_form(source, big=False):
    """The Daily Brief sign-up block. `big` is the prominent version for the
    morning-brief page; the compact one sits at the foot of every other page."""
    if big:
        head = (
            "<h2>Get the brief by email</h2>"
            "<p>Free. It arrives " + BRIEF_ARRIVAL + ": the market setup, unusual "
            "options flow, top news and the day's calendar. We'll email you a link "
            "to confirm, and every brief has a one-click unsubscribe.</p>"
        )
    else:
        head = (
            "<h3>Get the free TickerDesk Daily Brief</h3>"
            "<p>One email " + BRIEF_ARRIVAL + ". Confirm by email; "
            "unsubscribe in one click.</p>"
        )
    src = esc(source)
    return (
        '<section class="signup'
        + (" big" if big else "")
        + '">'
        + head
        + '<form class="bs-form" method="post" action="'
        + SIGNUP_ENDPOINT
        + '" data-source="'
        + src
        + '">'
        '<input type="email" name="email" placeholder="you@example.com" '
        'autocomplete="email" required aria-label="Email address" />'
        '<input type="hidden" name="source" value="' + src + '" />'
        '<input type="text" name="website" class="bs-hp" tabindex="-1" '
        'autocomplete="off" aria-hidden="true" />'
        '<button type="submit">Get the brief</button></form>'
        '<div class="bs-status" role="status" aria-live="polite"></div></section>'
    )


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render(page, all_pages):
    slug = page["slug"]
    canon = SITE + "/" + slug
    # Cross-links to the other landing pages (internal linking for SEO).
    related = "".join(
        '<li><a href="/' + p["slug"] + '">' + esc(p["link_label"]) + "</a></li>"
        for p in all_pages
        if p["slug"] != slug
    )
    related += (
        '<li><a href="/transparency">Does options flow work? Our self-graded numbers</a></li>'
    )
    faq_html = ""
    faq_ld = []
    for q, a in page.get("faq", []):
        faq_html += "<details><summary>" + esc(q) + "</summary><p>" + a + "</p></details>"
        faq_ld.append(
            {
                "@type": "Question",
                "name": q,
                "acceptedAnswer": {"@type": "Answer", "text": re.sub("<[^>]+>", "", a)},
            }
        )
    ld = {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "name": page["title"],
        "url": canon,
        "description": page["meta"],
        "isPartOf": {"@type": "WebApplication", "name": "TickerDesk", "url": SITE + "/"},
    }
    blocks = [ld]
    if faq_ld:
        blocks.append({"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": faq_ld})
    ld_html = "\n".join(
        '<script type="application/ld+json">' + json.dumps(b, separators=(",", ":")) + "</script>"
        for b in blocks
    )

    callout = ""
    if page.get("callout"):
        callout = '<div class="callout">' + page["callout"] + "</div>"

    # Daily Brief sign-up: prominent on the brief's own page (right under
    # the intro), compact at the foot of every other page.
    source = "lp_" + slug.replace("-", "_")[:37]
    signup_top = signup_form(source, big=True) if page.get("signup_top") else ""
    signup_foot = "" if page.get("signup_top") else signup_form(source)

    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="description" content="{meta}" />
<link rel="icon" href="favicon.png" />
<link rel="canonical" href="{canon}" />
<meta property="og:type" content="article" />
<meta property="og:site_name" content="TickerDesk" />
<meta property="og:title" content="{title}" />
<meta property="og:description" content="{meta}" />
<meta property="og:url" content="{canon}" />
<meta property="og:image" content="{site}/og-image.jpg" />
<meta name="twitter:card" content="summary_large_image" />
<title>{title}</title>
{ld}
<style>{css}</style>
</head>
<body>
<main>
  <div class="nav">
    <a href="/">&larr; TickerDesk</a>
    <a href="{cta_href}">Open the app &rarr;</a>
  </div>
  <h1>{h1}</h1>
  <p class="sub">{sub}</p>
  {intro}
  {signup_top}
  {callout}
  {body}
  <h2>Frequently asked</h2>
  <div class="faq">{faq}</div>
  <p style="margin-top:24px"><a class="cta" href="{cta_href}">{cta_label}</a></p>
  <p class="disc">{disc}</p>
  <h2>More from TickerDesk</h2>
  <ul class="related">{related}</ul>
  {signup_foot}
  <footer>
    <a href="/">TickerDesk</a> &middot; <a href="/transparency">Transparency</a>
    &middot; <a href="/terms">Terms</a> &middot; <a href="/privacy">Privacy</a>
    &middot; Educational research, not investment advice. Market data
    15-minute delayed.
  </footer>
</main>
<script>{signup_js}</script>
</body>
</html>
""".format(
        meta=esc(page["meta"]),
        canon=canon,
        title=esc(page["title"]),
        site=SITE,
        ld=ld_html,
        css=CSS,
        h1=esc(page["h1"]),
        sub=esc(page["sub"]),
        intro=page["intro"],
        signup_top=signup_top,
        signup_foot=signup_foot,
        signup_js=SIGNUP_JS,
        callout=callout,
        body=page["body"],
        faq=faq_html,
        cta_href=page["cta_href"],
        cta_label=esc(page["cta_label"]),
        related=related,
        disc=(
            "Every signal referenced is logged and graded against real "
            "closes; see the <a href='/transparency'>transparency page</a> "
            "for the full self-graded track record."
        ),
    )


# ── page specs ────────────────────────────────────────────────────────────
def _asof_note():
    return (" Numbers as of " + ST["asof"] + ".") if ST.get("asof") else ""


def _flow_callout():
    if not (ST.get("total") and ST.get("ev") is not None):
        return None
    read = _ci_read(ST.get("ci"))
    if read == "measurably positive":
        verdict = "Overall the flagged flow has done slightly better than the market"
    elif read == "measurably negative":
        verdict = "Overall, following every flagged alert has lost slightly against the market"
    else:
        verdict = "Overall, following every flagged alert has been close to a coin flip"
    ci = _ci_text(ST.get("ci"))
    return (
        '<div class="stat-row">'
        "<div><b>" + ST["total"] + "</b>alerts tracked</div>"
        "<div><b>"
        + _pct(ST["ev"])
        + "</b>avg +5d stock excess vs SPY, direction-adjusted</div>"
        + (
            "<div><b>"
            + ST["win"]
            + "%</b>win rate"
            + (" · PF " + ST["pf"] if ST.get("pf") is not None else "")
            + "</div>"
            if ST.get("win") is not None
            else ""
        )
        + "</div>"
        '<p style="margin:10px 0 0;font-size:13px;color:#9a9a9a">'
        + verdict
        + (" (95% range " + ci + ")" if ci else "")
        + ". These numbers measure the underlying stock (bought puts graded "
        "as bearish calls), not option P&amp;L. The "
        "<a href='/transparency'>transparency page</a> breaks it down by "
        "cohort, including the ones that don't work." + _asof_note() + "</p>"
    )


def _gs_callout():
    g = ST.get("gs")
    if not g:
        return None
    read = _ci_read(g.get("ci95"))
    ci = _ci_text(g.get("ci95"))
    return (
        '<div class="stat-row">'
        "<div><b>" + _fmt_int(g["n"]) + "</b>golden sweeps graded at +5 days</div>"
        "<div><b>" + _pct(g["ev"]) + "</b>avg +5d stock excess vs SPY, "
        "in the sweep's direction</div>"
        + (
            "<div><b>"
            + _numtxt(g.get("win_rate"))
            + "%</b>win rate"
            + (
                " · PF " + _numtxt(g.get("profit_factor"))
                if _numtxt(g.get("profit_factor")) is not None
                else ""
            )
            + "</div>"
            if _numtxt(g.get("win_rate")) is not None
            else ""
        )
        + "</div>"
        '<p style="margin:10px 0 0;font-size:13px;color:#9a9a9a">'
        + (
            "The 95% range is " + ci + ", so the golden-sweep result is " + read + ". "
            if ci and read
            else ""
        )
        + "Measured on the underlying stock, not the option."
        + _asof_note()
        + "</p>"
    )


def _oi_callout():
    if not ST.get("oi_rate"):
        return None
    return (
        '<div class="stat-row">'
        "<div><b>" + ST["oi_rate"] + "%</b>of alerts confirmed by "
        "next-day open interest</div>"
        + (
            "<div><b>" + ST["oi_checked"] + "</b>alerts OI-checked</div>"
            if ST.get("oi_checked")
            else ""
        )
        + "</div>"
    )


def _levels_callout():
    if not ST.get("lvl_sessions"):
        return None
    return (
        '<div class="stat-row"><div><b>'
        + _fmt_int(ST["lvl_sessions"])
        + "</b>sessions of level statistics per symbol</div></div>"
        '<p style="margin:10px 0 0;font-size:13px;color:#9a9a9a">Each '
        "key level ships with its measured touch / hold / break rate — "
        "not just a line on a chart.</p>"
    )


def _sessions_phrase():
    """'339 sessions' from level_stats, or a hedge when it isn't published."""
    return (
        (_fmt_int(ST["lvl_sessions"]) + " sessions") if ST.get("lvl_sessions") else "many sessions"
    )


def _universe_phrase():
    if ST.get("universe"):
        return ST["universe"] + " optionable names (the latest session's universe)"
    return "a broad universe of liquid optionable names"


def _cadence_phrase():
    return CADENCE or "several times each market day"


PAGES = [
    {
        "slug": "options-flow-scanner",
        "title": "Options Flow Scanner — Unusual Options Activity, Graded | TickerDesk",
        "meta": "A daily options-flow scanner that flags unusual options activity, sweeps, and repeat buyers — then grades every signal against real closes and publishes what didn't work too.",
        "h1": "Options flow scanner that grades itself",
        "sub": "Unusual options activity, golden sweeps, and repeat-buyer flow — with the one thing most scanners hide: what happened next.",
        "intro": "<p>Most options-flow tools show you a firehose of big trades and leave you to guess which ones matter. TickerDesk scans "
        + _universe_phrase()
        + " "
        + _cadence_phrase()
        + " (15-minute delayed data), flags the unusual activity, and then <b>tracks every flagged signal to a real outcome</b> at +1/+3/+5/+10/+20 days versus SPY.</p>",
        "callout": _flow_callout(),
        "body": (
            "<h2>What it flags</h2><ul>"
            "<li><b>Golden sweeps</b> — aggressive, multi-exchange sweep orders paying the ask.</li>"
            "<li><b>Volume &gt; open interest</b> — new positioning, not existing books being traded.</li>"
            "<li><b>Repeat buyers</b> — the same contract accumulating across sessions.</li>"
            "<li><b>Opening-buyer premium</b> — bull vs bear dollar-premium tilt per name and market-wide.</li>"
            "</ul>"
            "<h2>Why the grading matters</h2>"
            "<p>A scan is only worth what it predicts. Every cohort — by signal type, DTE bucket, liquidity tier and flag — carries its own expected value, win rate, and confidence interval, recomputed nightly. Where a cohort shows no edge, the app says so instead of dressing it up.</p>"
        ),
        "faq": [
            (
                "Is the options flow real-time?",
                "Data is 15-minute delayed (Polygon Options Starter), clearly labeled everywhere. The scan runs "
                + _cadence_phrase()
                + " on market days.",
            ),
            (
                "Does options flow actually work?",
                "We grade every alert and publish the result, good or bad. The <a href='/transparency'>transparency page</a> has the current numbers, with a 95% range on each cohort so you can see which differences are bigger than the noise.",
            ),
        ],
        "cta_href": "/#uoa",
        "cta_label": "Open the flow scanner",
    },
    {
        "slug": "options-open-interest-confirmation",
        "title": "Options Open-Interest Confirmation — Did the Flow Actually Stick? | TickerDesk",
        "meta": "Next-day open-interest confirmation on every flagged options signal: did open interest rise, barely move, or fall by the next session? A check most flow scanners skip.",
        "h1": "Open-interest confirmation on every flow signal",
        "sub": "A big print means more if open interest is still up the next morning. We check.",
        "intro": "<p>Unusual options activity is noisy: a lot of eye-catching prints are closed, rolled, or hedged by the next session. TickerDesk re-reads each flagged contract's <b>open interest the next trading day</b> and tells you whether the position was confirmed (OI grew), weak, or already closed.</p>",
        "callout": _oi_callout(),
        "body": (
            "<h2>Three outcomes, tracked</h2><ul>"
            "<li><b>Confirmed</b> — open interest rose next day. That is consistent with new positions staying open; it can't prove a specific trade was held.</li>"
            "<li><b>Weak</b> — little or no OI follow-through.</li>"
            "<li><b>Closed</b> — OI fell; the flow was likely a same-day trade, not positioning.</li>"
            "</ul>"
            "<p>This is the differentiator most flow products don't offer. A sweep that shows up on a dozen scanners looks identical everywhere — until you check whether open interest actually rose the next day.</p>"
        ),
        "faq": [
            (
                "How is confirmation measured?",
                "By comparing each flagged contract's open interest the session after the flag to its level at flag time. OCC-settled OI updates once per day, so this is a next-morning read.",
            ),
            (
                "Why does it matter?",
                "Positioning that persists is a stronger signal than a print that's gone by the close. Confirmation separates the two.",
            ),
        ],
        "cta_href": "/#uoa",
        "cta_label": "See confirmed flow",
    },
    {
        "slug": "golden-sweeps-scanner",
        "title": "Golden Sweeps Scanner — Aggressive Options Sweeps, Tracked | TickerDesk",
        "meta": "Track golden sweeps — aggressive multi-exchange options orders paying the ask — with next-day OI confirmation and a graded track record at +1/+3/+5 days.",
        "h1": "Golden sweeps, with a real track record",
        "sub": "The most aggressive footprint in the tape — swept across exchanges, paying up for urgency.",
        "intro": "<p>A golden sweep is an order large and urgent enough to sweep multiple exchanges and pay the offer. It's the closest thing options flow has to a conviction tell. TickerDesk flags them intraday, confirms them against next-day open interest, and grades the cohort so you know how this signal type has actually performed.</p>",
        "callout": _gs_callout(),
        "body": (
            "<h2>What makes a sweep 'golden'</h2><ul>"
            "<li>Multi-exchange execution — filled across venues in a hurry.</li>"
            "<li>Aggressor paying the ask — urgency, not patience.</li>"
            "<li>Size relative to the contract's normal liquidity.</li>"
            "</ul>"
            "<h2>Then the honest part</h2>"
            "<p>Golden sweeps are graded as their own cohort, against SPY across horizons. You see the win rate, the average result and its 95% range, not just the alert, including when the cohort has underperformed.</p>"
        ),
        "faq": [
            (
                "Are golden sweeps bullish?",
                "Direction depends on whether calls or puts are being swept and where price is. The scanner shows the premium tilt; the graded cohort shows how it has paid.",
            ),
            (
                "How fast do I see them?",
                "On the next scan: the flow scan runs "
                + _cadence_phrase()
                + ", on 15-minute delayed data.",
            ),
        ],
        "cta_href": "/#uoa",
        "cta_label": "Open the sweeps scanner",
    },
    {
        "slug": "spy-qqq-iwm-levels",
        "title": "SPY, QQQ & IWM Key Levels + 0DTE Structure Map | TickerDesk",
        "meta": "Daily key levels for SPY, QQQ and IWM: prior-day high/low, VWAP, opening range, VPOC/VAH/VAL, dealer gamma walls and expected move — each with its measured hold/break rate.",
        "h1": "SPY / QQQ / IWM levels that carry their own stats",
        "sub": "The index structure map for 0DTE and weekly options — every level graded by how it has actually behaved.",
        "intro": "<p>Anyone can draw prior-day high, VWAP and the opening range. The question is which one holds. TickerDesk computes the full level set for SPY, QQQ and IWM each day and attaches a <b>measured touch, hold and break rate</b> to every level from "
        + _sessions_phrase()
        + " of history.</p>",
        "callout": _levels_callout(),
        "body": (
            "<h2>The level set</h2><ul>"
            "<li>Prior-day high/low, prior close, overnight high/low/VWAP.</li>"
            "<li>Opening-range high/low, VWAP, VPOC / VAH / VAL.</li>"
            "<li>Dealer gamma flip, largest-gamma strike, call/put walls, expected-move band.</li>"
            "</ul>"
            "<h2>Plus if/then level scenarios</h2>"
            "<p>A deterministic engine reads the day's structure and gamma regime into hypothetical if/then scenarios: for example, if price holds above VWAP, the next reference above is the nearest mapped resistance. Each scenario type is graded on a same-session frame. These describe structure, not trades to take. All data is 15-minute delayed and labeled as such.</p>"
        ),
        "faq": [
            (
                "Is this for 0DTE trading?",
                "It's a reference map of index structure for people who follow 0DTE and weekly options. It is not a live feed — data is 15-minute delayed — and it is educational, not advice.",
            ),
            (
                "Where do the hold/break rates come from?",
                "From an engine that replays "
                + _sessions_phrase()
                + " of 5-minute bars per symbol and measures how each level actually behaved on first touch.",
            ),
        ],
        "cta_href": "/#charts",
        "cta_label": "Open Index Levels",
    },
    {
        "slug": "earnings-options-flow",
        "title": "Earnings Options Flow — Implied vs Realized Moves + Graded Setups | TickerDesk",
        "meta": "Earnings options flow: implied move vs the stock's historically realized move, post-report drift, flow into the print, and self-graded earnings setups for large caps.",
        "h1": "Earnings options flow and graded setups",
        "sub": "What the options are pricing into the print — versus what the stock has actually done.",
        "intro": "<p>The calendar tells you when a company reports; it doesn't tell you how options are priced. TickerDesk compares each name's <b>implied move to its historically realized move</b>, measures post-report drift, reads the options flow going into the print, and sorts names into rule-based setup types for names with real size. Each type is graded in public, including when it fails.</p>",
        "body": (
            "<h2>Setup types</h2><ul>"
            "<li><b>Momentum into print</b> — leaders with bullish flow and positive historical post-report drift.</li>"
            "<li><b>Post-report drift</b> — how the stock has tended to move in the days after past reports.</li>"
            "<li><b>Vol rich / vol cheap</b> — implied move well above or below the realized history.</li>"
            "<li><b>Binary caution</b> — big typical mover already extended into the event.</li>"
            "</ul>"
            "<p>Setups are capped to $1B+ market caps and every type carries its own graded track record. Nothing publishes a hit rate before it has 30 real graded outcomes.</p>"
        ),
        "faq": [
            (
                "Does it tell me to buy earnings?",
                "No. It shows where options pricing differs from history and labels crowded, extended names. It's educational, not advice.",
            ),
            (
                "Which names are covered?",
                "Large, liquid names reporting within about a week; the setup layer is floored at $1B market cap.",
            ),
        ],
        "cta_href": "/earnings",
        "cta_label": "Open the Earnings desk",
    },
    {
        "slug": "swing-trading-scanner",
        "title": "Swing Trading Scanner — Graded Momentum Setups | TickerDesk",
        "meta": "A swing-trading scanner that grades momentum names A through F, tracks next-day and multi-day follow-through, and publishes its own hit rate — good days and bad.",
        "h1": "Swing trading scanner that keeps score",
        "sub": "Graded momentum setups with a published follow-through record — not a wall of green arrows.",
        "intro": "<p>TickerDesk grades momentum and swing candidates each day and, crucially, tracks how yesterday's grades actually performed. The Momentum Lab shows the running record — including the sessions it got wrong — so the scanner earns trust instead of asserting it.</p>",
        "body": (
            "<h2>What you get</h2><ul>"
            "<li>Letter-graded swing candidates with relative strength, ADR%, extension and theme tags.</li>"
            "<li>Cross-confirmation: which names show up in more than one scan.</li>"
            "<li>An Engine Room self-audit with the running next-day hit rate.</li>"
            "</ul>"
            "<p>Grades come from a post-close batch; intraday percentage moves refresh live (15-minute delayed).</p>"
        ),
        "faq": [
            (
                "How are setups graded?",
                "By a systematic momentum model, then tracked against real next-day and multi-day price action. The published record includes losing stretches.",
            ),
            (
                "Is this financial advice?",
                "No — it's a research tool. Verify with your broker before any trade.",
            ),
        ],
        "cta_href": "/#swing",
        "cta_label": "Open the Momentum Lab",
    },
    {
        "slug": "vcp-screener",
        "title": "VCP Screener — Volatility Contraction Pattern Setups | TickerDesk",
        "meta": "A daily VCP screener for volatility-contraction-pattern setups: tightening range, declining volume, and a defined pivot — each setup scored for quality.",
        "h1": "VCP screener with quality scores",
        "sub": "Volatility contraction pattern setups — tightening base, drying volume, a clear pivot.",
        "intro": "<p>The Volatility Contraction Pattern (VCP) is a base that tightens through progressively smaller pullbacks on declining volume before a breakout. TickerDesk's Setup Builder screens for VCP structure after each close and scores every candidate on setup quality so you're not eyeballing a hundred charts.</p>",
        "body": (
            "<h2>What the screen looks for</h2><ul>"
            "<li>A series of contractions — each pullback shallower than the last.</li>"
            "<li>Volume drying up into the apex of the base.</li>"
            "<li>A clearly defined pivot level at the top of the base.</li>"
            "</ul>"
            "<p>Each setup gets a quality score so the cleanest bases rise to the top. Refreshed post-close daily.</p>"
        ),
        "faq": [
            (
                "What is a VCP?",
                "A volatility contraction pattern — a consolidation that tightens through successive smaller pullbacks on falling volume, popularized by Mark Minervini, often preceding a breakout.",
            ),
            ("How often does it update?", "Once per day, after the close."),
        ],
        "cta_href": "/#setup",
        "cta_label": "Open the VCP screener",
    },
    {
        "slug": "ai-stock-research-reports",
        # WHAT A RESEARCH REPORT IS: the full description for this page.
        # Keep it in step with the Research tab in docs/index.html and
        # docs/sample-report.html. Source of truth: report_v4.py (build_core:
        # the _pageN functions + the appendix), run by ticker-lookup.yml.
        "title": "Stock Research Reports — On-Demand Deep Dives | TickerDesk",
        "meta": "On-demand stock research reports: normally a 5-6 page PDF on any ticker plus an evidence appendix, built from market data and SEC filings and usually ready in about a minute.",
        "h1": "Stock research reports on any ticker",
        "sub": "Normally a 5-6 page equity report plus an evidence appendix, built on demand from market data and SEC filings.",
        "intro": "<p>Ask for any ticker and TickerDesk builds a structured equity research report from market data and SEC filings. Every figure is traced to its source in an evidence appendix, and a number the sources can't support is shown as unavailable, never guessed. Usually ready in about a minute, delivered as a PDF you can keep.</p>",
        "body": (
            "<h2>What's in a report</h2><ul>"
            "<li>Investment summary: fundamental and tactical ratings, the thesis and key risks.</li>"
            "<li>Business model and competitive position, from cited filings.</li>"
            "<li>The latest quarter and the estimate context.</li>"
            "<li>Valuation on real inputs, with the formula behind each figure, when there is enough data for it.</li>"
            "<li>Price chart with moving averages, relative strength vs SPY and key levels.</li>"
            "<li>Catalysts, ranked risks and what to monitor.</li>"
            "<li>An evidence appendix: sources, estimates, Form 4 insider trades, institutional filings, options and sampled social posts.</li>"
            "</ul>"
            "<p>If the data needed for a full report is temporarily unavailable, you get a short one-page notice instead. "
            "Free accounts get 3 lifetime reports; Pro 10/month; Premium 100/month. Reports are on-demand only — you ask, it runs. "
            "<a href='/sample-report'>See a sample</a>.</p>"
        ),
        "faq": [
            (
                "How long does a report take?",
                "Usually about a minute end to end, delivered as a downloadable PDF.",
            ),
            (
                "Does AI write the report?",
                "No. The report is built from market data and SEC filings by fixed rules, and every figure is traced to its source in the appendix.",
            ),
        ],
        "cta_href": "/#adhoc",
        "cta_label": "Generate a report",
    },
    {
        "slug": "stock-market-morning-brief",
        "title": "Stock Market Morning Brief — Pre-Market Movers & Catalysts | TickerDesk",
        "meta": "A free daily stock-market morning brief: pre-market movers, overnight news, macro calendar, before-the-open earnings, and the flow and setups that matter. Arrives shortly before 9:00 AM ET on market days.",
        "h1": "Your stock market morning brief",
        "sub": "Pre-market movers, overnight news, the macro calendar and the day's setups — before the open.",
        "intro": "<p>Start the session already briefed. TickerDesk's daily email arrives shortly before 9:00 AM ET on market days with the pre-market movers, overnight headlines, the day's macro calendar, before-the-open earnings, and the flow and setups worth watching — a single decision-first read instead of ten open tabs.</p>",
        "body": (
            "<h2>What's in the brief</h2><ul>"
            "<li>Pre-market movers and why they're moving.</li>"
            "<li>Overnight news and the macro/economic calendar.</li>"
            "<li>Before-the-open earnings and notable after-close reports.</li>"
            "<li>The top flow, setups and watchlist changes from the desk.</li>"
            "</ul>"
            "<p>Sign up above with just your email for the market-wide brief. "
            "With a free TickerDesk account you can also turn it on from your "
            "settings and get your own watchlist names in it. Every email has "
            "a one-click unsubscribe.</p>"
        ),
        "faq": [
            (
                "When does it arrive?",
                "Shortly before 9:00 AM ET on market days, ahead of the open.",
            ),
            (
                "Is it free?",
                (
                    "Yes. Sign up with your email, confirm from the link we send, "
                    "and unsubscribe in one click whenever you like."
                ),
            ),
        ],
        "signup_top": True,
        "cta_href": "/#desk",
        "cta_label": "See today's desk",
    },
]


# Short labels for the cross-link list (the <title> is too long for a list).
LINK_LABELS = {
    "options-flow-scanner": "Options flow scanner",
    "options-open-interest-confirmation": "Open-interest confirmation",
    "golden-sweeps-scanner": "Golden sweeps scanner",
    "spy-qqq-iwm-levels": "SPY / QQQ / IWM key levels",
    "earnings-options-flow": "Earnings options flow",
    "swing-trading-scanner": "Swing trading scanner",
    "vcp-screener": "VCP screener",
    "ai-stock-research-reports": "Stock research reports",
    "stock-market-morning-brief": "Stock market morning brief",
}
for _p in PAGES:
    _p["link_label"] = LINK_LABELS.get(_p["slug"], _p["slug"])


# ── transparency page (generated from the same live JSON) ────────────────
T_CSS = """
  body { margin:0; padding:48px 20px 80px; background:#000; color:#e8e8e8;
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
    line-height:1.6; }
  main { max-width:680px; margin:0 auto; }
  .nav { display:flex; justify-content:space-between; align-items:center;
    border-bottom:1px solid #2b2b2b; padding-bottom:16px; margin-bottom:32px; }
  .nav a { color:#ff9e1b; text-decoration:none; font-size:13px; font-weight:600; }
  .nav a:hover { text-decoration:underline; }
  h1 { font-size:28px; font-weight:800; margin:0 0 8px; }
  h2 { font-size:18px; font-weight:700; margin:36px 0 10px; }
  .sub { color:#7a7a7a; font-size:13px; margin-bottom:32px; }
  p, li { font-size:14.5px; color:#c9c9c9; }
  a { color:#ff9e1b; }
  .stat-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
    gap:10px; margin:18px 0; }
  .stat { background:#0e0e0e; border:1px solid #2b2b2b; border-radius:8px;
    padding:14px 12px; }
  .stat b { display:block; font-size:20px; font-weight:800; color:#e8e8e8;
    font-variant-numeric:tabular-nums; }
  .stat span { font-size:11px; color:#7a7a7a; line-height:1.4; display:block;
    margin-top:4px; }
  table { width:100%; border-collapse:collapse; margin:14px 0; font-size:13px; }
  th, td { text-align:left; padding:7px 10px; border-bottom:1px solid #1e1e1e;
    font-variant-numeric:tabular-nums; }
  th { color:#7a7a7a; font-size:11px; text-transform:uppercase; letter-spacing:.04em; }
  .note { background:#0e0e0e; border-left:3px solid #ff9e1b; border-radius:0 8px 8px 0;
    padding:12px 16px; font-size:13.5px; color:#b4b4b4; margin:18px 0; }
  .cta { display:inline-block; background:#ff9e1b; color:#000; font-weight:700;
    padding:10px 22px; border-radius:8px; text-decoration:none; margin-top:8px; }
  footer { margin-top:48px; padding-top:16px; border-top:1px solid #2b2b2b;
    font-size:12px; color:#7a7a7a; }
"""

# (label, path into uoa_edge.json) for the cohort table; a row whose source
# is missing or still accruing is left out, never filled from an old copy.
T_COHORTS = [
    ("Golden sweeps ($1M+ buy-side sweeps)", ("by_type", "golden_sweep")),
    ("Flow into earnings", ("by_tag", "Into ERN")),
    ("Expiries 15&ndash;90 days out", ("by_dte", "swing")),
    ("Expiries 3&ndash;12 months out", ("by_dte", "positioning")),
    ("Repeat buyers", ("by_tag", "Repeat")),
    ("Top-scored alerts (score 80+)", ("by_score", "80-100")),
]
T_BOOKS = [("put_buy", "Puts bought"), ("call_buy", "Calls bought"), ("seller", "Premium sold")]


def _signed(v, fmt):
    """Signed number with an HTML minus sign."""
    return format(v, fmt).replace("-", "&minus;")


def _cohort_row(label, rich):
    if not rich or rich.get("status") != "active" or not _num(rich.get("n")):
        return ""
    ev, win = _pct(rich.get("ev")), _numtxt(rich.get("win_rate"))
    if ev is None or win is None:
        return ""
    return (
        "<tr><td>"
        + label
        + "</td><td>"
        + win
        + "%</td><td>"
        + ev
        + "</td><td>"
        + (_ci_text(rich.get("ci95")) or "&mdash;")
        + "</td><td>"
        + (_ci_read(rich.get("ci95")) or "&mdash;")
        + "</td><td>"
        + _fmt_int(rich["n"])
        + "</td></tr>"
    )


def _t_headline():
    ro = (EDGE.get("rich_overall") or {}).get("5") or {}
    tiles = []
    since = _date_of(EDGE.get("oldest_signal_ts"))
    if ST.get("total"):
        tiles.append((ST["total"], "alerts tracked" + (" since " + since if since else "")))
    if ST.get("matured"):
        tiles.append((ST["matured"], "matured &amp; graded at the +5-day horizon"))
    if ST.get("ev") is not None:
        ci = _ci_text(ST.get("ci"))
        tiles.append(
            (
                _pct(ST["ev"]),
                "average +5d excess return vs SPY, in the flow's direction"
                + (" &middot; 95% range " + ci if ci else ""),
            )
        )
    if ST.get("win") is not None:
        bits = []
        if _pct(ro.get("avg_win")) and _pct(ro.get("avg_loss")):
            bits.append(
                "winners average " + _pct(ro["avg_win"]) + ", losers " + _pct(ro["avg_loss"])
            )
        if ST.get("pf") is not None:
            bits.append("profit factor " + ST["pf"])
        tiles.append((ST["win"] + "%", "win rate" + "".join(" &middot; " + b for b in bits)))
    if not tiles:
        return ""
    html = (
        '<h2>The headline, honestly</h2>\n  <div class="stat-grid">'
        + "".join(
            '<div class="stat"><b>' + v + "</b><span>" + k + "</span></div>" for v, k in tiles
        )
        + "</div>"
    )
    read = _ci_read(ST.get("ci"))
    if read == "not distinguishable from zero":
        html += (
            "\n  <p><b>Translation: buying every flow signal blindly is a coin flip.</b> "
            "The average is not distinguishable from zero. We publish that because it's "
            "true, and because it's the honest baseline that makes the cohort numbers "
            "below meaningful instead of marketing.</p>"
        )
    elif read == "measurably negative":
        html += (
            "\n  <p><b>Translation: buying every flow signal blindly has lost slightly "
            "against the market.</b> We publish that because it's true, and because it's "
            "the honest baseline for the cohort numbers below.</p>"
        )
    elif read == "measurably positive":
        html += (
            "\n  <p><b>Translation: the flagged flow as a whole has done slightly better "
            "than the market so far.</b> The margin is small; check the cohorts below and "
            "their 95% ranges before leaning on it.</p>"
        )
    return html


def _t_cohorts():
    rows = ""
    for label, (group, key) in T_COHORTS:
        rows += _cohort_row(label, ((EDGE.get(group) or {}).get(key) or {}).get("rich"))
    if not rows:
        return ""
    return (
        '<h2>By cohort</h2>\n  <p>The signal isn\'t "flow good." The question is <i>which</i> '
        "flow, and whether the difference is bigger than the noise. Each row is the +5-day "
        "stock move vs SPY in the flow's direction, with its 95% range. A range that "
        "includes zero means we can't tell the result apart from luck.</p>\n  <table>"
        "<tr><th>Cohort</th><th>Win rate</th><th>Avg +5d vs SPY</th><th>95% range</th>"
        "<th>Read</th><th>n</th></tr>" + rows + "</table>"
    )


def _t_attribution():
    books = (RESEARCH.get("attribution") or {}).get("books") or {}
    items = []
    for key, label in T_BOOKS:
        b = books.get(key) or {}
        if b.get("status") != "active" or _pct(b.get("dir_ev")) is None:
            continue
        fa, fci = _pct(b.get("flow_alpha")), _ci_text(b.get("flow_alpha_ci95"))
        if fa is None or fci is None:
            continue
        hit = _num(b.get("hit"))
        items.append(
            "<li><b>"
            + label
            + "</b>"
            + (" (" + _fmt_int(b.get("n")) + " alerts)" if _fmt_int(b.get("n")) else "")
            + ": "
            + (str(round(hit)) + "% beat SPY in the flow's direction, " if hit is not None else "")
            + _pct(b["dir_ev"])
            + " average vs SPY. After removing that day's drift of all "
            "flagged stocks, the flow itself adds "
            + fa
            + " (95% range "
            + fci
            + "), "
            + (_ci_read(b.get("flow_alpha_ci95")) or "")
            + ".</li>"
        )
    if not items:
        return ""
    html = (
        "<h2>Does the flow itself add anything?</h2>\n  <p>Part of any result is simply "
        "how the flagged stocks drifted that week. Our research engine splits each book "
        "into that drift and the part the flow's direction adds on top:</p>\n  <ul>"
        + "".join(items)
        + "</ul>"
    )
    pb = books.get("put_buy") or {}
    if (
        pb.get("status") == "active"
        and (_num(pb.get("dir_ev")) or 0) > 0
        and _ci_read(pb.get("flow_alpha_ci95")) == "not distinguishable from zero"
    ):
        html += (
            "\n  <p>So when put buyers look right, it is mostly because the stocks they "
            "were flagged on drifted down anyway, not because the puts predicted it.</p>"
        )
    return html


def _t_doesnt_work():
    items = []
    ic = EDGE.get("ic") or {}
    icv = _num(ic.get("ic_spearman"))
    if icv is not None and _num(ic.get("n")):
        dec = EDGE.get("score_deciles") or {}
        tmb = _num(dec.get("top_minus_bottom"))
        items.append(
            "<li><b>"
            + ("Our score barely ranks." if abs(icv) < 0.05 else "Score ranking.")
            + "</b> The rank correlation (IC) between our trade "
            "score and the result is "
            + _signed(icv, ".3f")
            + " across "
            + _fmt_int(ic["n"])
            + " alerts"
            + ("; we treat 0.05 or more as meaningful" if abs(icv) < 0.05 else "")
            + "."
            + (
                " The top tenth of scores beat the bottom tenth by "
                + _signed(tmb, ".2f")
                + " points."
                if tmb is not None and dec.get("status") == "active"
                else ""
            )
            + " We use the score as an admission filter, not a ranking.</li>"
        )
    ro = EDGE.get("rich_overall") or {}
    hz = [(h, ro.get(h) or {}) for h in ("5", "10", "20")]
    hz = [(h, r) for h, r in hz if r.get("status") == "active" and _pct(r.get("ev")) is not None]
    if len(hz) >= 2:
        items.append(
            "<li><b>"
            + (
                "Holding longer makes it worse."
                if _num(hz[-1][1]["ev"]) < _num(hz[0][1]["ev"])
                else "By holding period."
            )
            + "</b> Average excess vs SPY: "
            + "; ".join(
                "+"
                + h
                + "d "
                + _pct(r["ev"])
                + " ("
                + (_ci_read(r.get("ci95")) or "no range")
                + ")"
                for h, r in hz
            )
            + ".</li>"
        )
    surv = HLAB.get("survivors")
    if HLAB.get("status") == "active" and _num(HLAB.get("n_tested")) and isinstance(surv, list):
        mt = HLAB.get("multiple_testing") or {}
        ran = _date_of(HLAB.get("generated"))
        items.append(
            "<li><b>Most ideas fail.</b> Our hypothesis lab has tested "
            + _fmt_int(HLAB["n_tested"])
            + " feature combinations against later, out-of-sample data. "
            + _fmt_int(len(surv))
            + " survived"
            + (
                " and " + _fmt_int(HLAB["graveyard"]) + " are in the graveyard"
                if _fmt_int(HLAB.get("graveyard")) is not None
                else ""
            )
            + "."
            + (
                " After correcting for testing so many, "
                + _fmt_int(mt["bh_survivors"])
                + (" still passes." if mt["bh_survivors"] == 1 else " still pass.")
                if _fmt_int(mt.get("bh_survivors")) is not None
                else ""
            )
            + " Survivors are candidates for review, not proven signals."
            + (" (Lab last run " + ran + ".)" if ran else "")
            + "</li>"
        )
    ov = SCANS.get("overall") or {}
    if ov.get("status") == "active" and _pct(ov.get("ev")) is not None:
        # Each clause needs its own source field; a missing one is left out.
        win, hold, n = (
            _numtxt(ov.get("win_rate")),
            _fmt_int(ov.get("hold_d")),
            _fmt_int(ov.get("n")),
        )
        items.append(
            "<li><b>"
            + ("Momentum lists fade." if ov["ev"] < 0 else "Momentum lists.")
            + "</b> Names on the QM Monthly and Stockbee Weekly lists "
            + (
                "won " + win + "% of the time with a " + _pct(ov["ev"]) + " average"
                if win
                else "averaged " + _pct(ov["ev"])
            )
            + (" over the next " + hold + " sessions" if hold and hold != "0" else "")
            + (" (" + n + " graded)" if n else "")
            + ".</li>"
        )
    if not items:
        return ""
    return "<h2>What we've found doesn't work</h2>\n  <ul>" + "".join(items) + "</ul>"


def _t_calibration():
    drifts = [
        _num(b.get("drift"))
        for b in (EDGE.get("calibration") or {}).values()
        if isinstance(b, dict) and _num(b.get("drift")) is not None
    ]
    if not drifts:
        return ""
    lo, hi = min(drifts), max(drifts)
    rng = _signed(lo, "+g") + ("" if lo == hi else " to " + _signed(hi, "+g")) + " points"
    return (
        "\n    <li>A nightly calibration check compares trained expectations with the live "
        "tape and publishes the drift, currently " + rng + " depending on score band.</li>"
    )


def transparency_html():
    asof = ST.get("asof")
    total = ST.get("total")
    desc = (
        "Does options flow actually work? We track every signal we publish"
        + (" (" + total + " so far)" if total else "")
        + " and grade them against real closes. The honest numbers: averages, win "
        "rates, ranges, and what doesn't work."
    )
    og = (
        (total + " alerts tracked, " if total else "Every alert ")
        + "graded against real closes. Averages, win rates, 95% ranges, and the cohorts "
        "that don't work, published, not hidden."
    )
    sub = (
        ("Numbers as of " + asof + " &middot; rebuilt" if asof else "Rebuilt")
        + ' from the live data every weeknight &middot; the <a href="/#uoa">live '
        "Performance dashboard</a> updates more often &middot; every figure is "
        "self-graded against real closing prices, never estimated"
    )
    intro = (
        "<p>Most options-flow products show you the winners. We log <b>every</b> alert "
        "our scanner publishes"
        + (" (" + total + " so far)" if total else "")
        + ", wait for real market closes, and grade all of them. Wins, losses, and the "
        "cohorts that turned out to be noise. One alert is a contract's first flag of the "
        "day; repeat scans of the same contract are folded in. Results measure the "
        "underlying stock against SPY, in the flow's direction (a bought put counts as a "
        "bet on the stock falling), not option P&amp;L.</p>"
    )
    how = (
        "<h2>How the grading works</h2>\n  <ul>"
        "\n    <li>Every published alert is logged the moment it appears: no retroactive "
        "additions, no deletions.</li>"
        "\n    <li>Returns are graded against real closing prices at +1/+3/+5/+10/+20 "
        "days, benchmarked against SPY.</li>"
        "\n    <li>Next-day open-interest checks tell us whether open interest rose, which is "
        "consistent with new positions staying open (it can't prove a specific trade was held).</li>"
        "\n    <li>Cohort statistics wait for a minimum sample and ship with 95% ranges. "
        'Below the minimum they say "accruing", never a made-up number.</li>'
        + _t_calibration()
        + "\n  </ul>"
    )
    sections = "\n\n  ".join(
        s for s in (_t_headline(), _t_cohorts(), _t_attribution(), _t_doesnt_work(), how) if s
    )
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="description" content="{desc}" />
<link rel="icon" href="favicon.png" />
<link rel="canonical" href="{site}/transparency" />
<meta property="og:type" content="article" />
<meta property="og:site_name" content="TickerDesk" />
<meta property="og:title" content="Does Options Flow Work? Our Self-Graded Numbers — TickerDesk" />
<meta property="og:description" content="{og}" />
<meta property="og:url" content="{site}/transparency" />
<meta property="og:image" content="{site}/og-image.jpg" />
<title>Does Options Flow Work? Our Self-Graded Numbers — TickerDesk</title>
<!-- Generated by landing_pages.py from the live report JSON. Do not hand-edit. -->
<style>{css}</style>
</head>
<body>
<main>
  <div class="nav">
    <a href="/">&larr; TickerDesk</a>
    <a href="/#uoa">Live dashboard &rarr;</a>
  </div>

  <h1>Does options flow actually work?</h1>
  <p class="sub">{sub}</p>

  {intro}

  {sections}

  <div class="note">
    <b>What this isn't:</b> a track record of trades, a promise of returns, or
    financial advice. It's the measured behavior of a signal engine, published so
    you can judge it the way we do. Data is 15-minute delayed. Transaction costs
    are not modeled and matter at these margins.
  </div>

  <p><a class="cta" href="/#uoa">See the live numbers &rarr;</a></p>

  <footer>
    <a href="/">TickerDesk</a> &middot; <a href="/terms">Terms</a> &middot;
    <a href="/privacy">Privacy</a> &middot; Educational research, not investment
    advice. This page is rebuilt from the live data every weeknight; a number
    whose source is unavailable is left out rather than shown stale.
  </footer>
</main>
</body>
</html>
""".format(
        desc=esc(desc).replace('"', "&quot;"),
        og=esc(og).replace('"', "&quot;"),
        site=SITE,
        css=T_CSS,
        sub=sub,
        intro=intro,
        sections=sections,
    )


def main():
    written = []
    for page in PAGES:
        html = render(page, PAGES)
        path = os.path.join(OUT_DIR, page["slug"] + ".html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        written.append(page["slug"])
    _write_sitemap(written)
    with open(os.path.join(OUT_DIR, "transparency.html"), "w", encoding="utf-8") as f:
        f.write(transparency_html())
    print("  landing pages: %d written (%s) + transparency" % (len(written), ", ".join(written)))
    return 0


def _write_sitemap(slugs):
    """Regenerate sitemap.xml: app root, transparency, landing pages, legal."""
    static = [
        ("/", "daily", "1.0"),
        ("/earnings", "daily", "0.9"),  # path-routed app tab (site_paths.py)
        ("/transparency", "daily", "0.8"),
        ("/glossary", "monthly", "0.6"),
        ("/sample-report", "monthly", "0.6"),
    ]
    lp = [("/" + s, "weekly", "0.7") for s in slugs]
    legal = [("/privacy", "monthly", "0.3"), ("/terms", "monthly", "0.3")]
    rows = static + lp + legal
    body = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for loc, freq, pri in rows:
        body.append(
            "  <url><loc>%s%s</loc><changefreq>%s</changefreq>"
            "<priority>%s</priority></url>" % (SITE, loc, freq, pri)
        )
    body.append("</urlset>")
    with open(os.path.join(OUT_DIR, "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write("\n".join(body) + "\n")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
