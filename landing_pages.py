"""SEO landing pages — static, keyword-targeted, generated nightly.

One page per keyword cluster from the site audit. Each page:
  - targets a distinct query (no thin/duplicate content),
  - cites REAL numbers pulled from published docs/reports/*.json (any
    stat that can't be found is omitted, never guessed),
  - carries canonical + OG + JSON-LD,
  - deep-links into the app via the hash router (/#uoa etc.),
  - is registered in sitemap.xml.

Runs in CI after the report JSONs are refreshed so the live-stat
callouts track the latest ledger. Pure static output — no client JS.
"""

import json
import os
import re
import sys

_BASE = os.path.dirname(os.path.abspath(__file__))
R = lambda *p: os.path.join(_BASE, *p)
OUT_DIR = R("docs")
SITE = "https://tickerdesk.io"


def _load(path, default=None):
    try:
        with open(R("docs", "reports", path), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {} if default is None else default


def _fmt_int(n):
    try:
        return "{:,}".format(int(n))
    except Exception:
        return None


# ── live stats (defensive — return None when absent) ──────────────────────
EDGE = _load("uoa_edge.json")
LEVELS = _load("level_stats.json")


def edge_stat():
    """Headline flow numbers, or {} if the ledger isn't published."""
    ro = (EDGE.get("rich_overall") or {}).get("5") or {}
    oi = EDGE.get("oi_confirmation") or {}
    out = {}
    if EDGE.get("total_signals"):
        out["total"] = _fmt_int(EDGE["total_signals"])
    if EDGE.get("matured_5d"):
        out["matured"] = _fmt_int(EDGE["matured_5d"])
    if ro.get("ev") is not None:
        out["ev"] = ro["ev"]
    if ro.get("win_rate") is not None:
        out["win"] = ro["win_rate"]
    if ro.get("profit_factor") is not None:
        out["pf"] = ro["profit_factor"]
    if oi.get("confirm_rate") is not None:
        out["oi_rate"] = oi["confirm_rate"]
    if oi.get("checked"):
        out["oi_checked"] = _fmt_int(oi["checked"])
    gs = (EDGE.get("by_type") or {}).get("golden_sweep") or {}
    grich = gs.get("rich") or gs
    if grich.get("n"):
        out["gs_n"] = _fmt_int(grich["n"])
    if LEVELS.get("sessions"):
        out["lvl_sessions"] = LEVELS["sessions"]
    return out


ST = edge_stat()


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
"""


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def render(page, all_pages):
    slug = page["slug"]
    canon = SITE + "/" + slug
    # Cross-links to the other landing pages (internal linking for SEO).
    related = "".join(
        '<li><a href="/' + p["slug"] + '">' + esc(p["link_label"]) +
        "</a></li>"
        for p in all_pages if p["slug"] != slug)
    related += '<li><a href="/transparency">Does options flow work? Our ' \
               'self-graded numbers</a></li>'
    faq_html = ""
    faq_ld = []
    for q, a in page.get("faq", []):
        faq_html += ('<details><summary>' + esc(q) + '</summary><p>' +
                     a + '</p></details>')
        faq_ld.append({"@type": "Question", "name": q,
                       "acceptedAnswer": {"@type": "Answer",
                                          "text": re.sub("<[^>]+>", "", a)}})
    ld = {"@context": "https://schema.org", "@type": "WebPage",
          "name": page["title"], "url": canon,
          "description": page["meta"],
          "isPartOf": {"@type": "WebApplication", "name": "TickerDesk",
                       "url": SITE + "/"}}
    blocks = [ld]
    if faq_ld:
        blocks.append({"@context": "https://schema.org", "@type": "FAQPage",
                       "mainEntity": faq_ld})
    ld_html = "\n".join(
        '<script type="application/ld+json">' +
        json.dumps(b, separators=(",", ":")) + "</script>" for b in blocks)

    callout = ""
    if page.get("callout"):
        callout = '<div class="callout">' + page["callout"] + "</div>"

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
  {callout}
  {body}
  <h2>Frequently asked</h2>
  <div class="faq">{faq}</div>
  <p style="margin-top:24px"><a class="cta" href="{cta_href}">{cta_label}</a></p>
  <p class="disc">{disc}</p>
  <h2>More from TickerDesk</h2>
  <ul class="related">{related}</ul>
  <footer>
    <a href="/">TickerDesk</a> &middot; <a href="/transparency">Transparency</a>
    &middot; <a href="/terms">Terms</a> &middot; <a href="/privacy">Privacy</a>
    &middot; Educational research, not investment advice. Market data
    15-minute delayed.
  </footer>
</main>
</body>
</html>
""".format(
        meta=esc(page["meta"]), canon=canon, title=esc(page["title"]),
        site=SITE, ld=ld_html, css=CSS, h1=esc(page["h1"]),
        sub=esc(page["sub"]), intro=page["intro"], callout=callout,
        body=page["body"], faq=faq_html, cta_href=page["cta_href"],
        cta_label=esc(page["cta_label"]), related=related,
        disc=("Every signal referenced is logged and graded against real "
              "closes; see the <a href='/transparency'>transparency page</a> "
              "for the full self-graded track record."))


# ── page specs ────────────────────────────────────────────────────────────
def _flow_callout():
    if not (ST.get("total") and ST.get("ev") is not None):
        return None
    return ('<div class="stat-row">'
            '<div><b>' + ST["total"] + '</b>signals tracked</div>'
            '<div><b>' + ("+" if ST["ev"] >= 0 else "") + str(ST["ev"]) +
            '%</b>avg +5d excess vs SPY</div>'
            '<div><b>' + str(ST.get("win", "")) + '%</b>win rate · PF ' +
            str(ST.get("pf", "")) + '</div></div>'
            '<p style="margin:10px 0 0;font-size:13px;color:#9a9a9a">These '
            'are the honest aggregate numbers — a near-coin-flip baseline. '
            'The edge lives in specific cohorts, and we publish where it '
            "doesn't work too.</p>")


def _oi_callout():
    if not ST.get("oi_rate"):
        return None
    return ('<div class="stat-row">'
            '<div><b>' + str(ST["oi_rate"]) + '%</b>of flagged positions '
            'confirmed by next-day OI</div>'
            '<div><b>' + str(ST.get("oi_checked", "")) +
            '</b>signals OI-checked</div></div>')


def _levels_callout():
    if not ST.get("lvl_sessions"):
        return None
    return ('<div class="stat-row"><div><b>' + str(ST["lvl_sessions"]) +
            '</b>sessions of level statistics per symbol</div></div>'
            '<p style="margin:10px 0 0;font-size:13px;color:#9a9a9a">Each '
            'key level ships with its measured touch / hold / break rate — '
            'not just a line on a chart.</p>')


PAGES = [
    {
        "slug": "options-flow-scanner",
        "title": "Options Flow Scanner — Unusual Options Activity, Graded | TickerDesk",
        "meta": "A daily options-flow scanner that flags unusual options activity, sweeps, and repeat buyers — then grades every signal against real closes. See what actually predicts moves.",
        "h1": "Options flow scanner that grades itself",
        "sub": "Unusual options activity, golden sweeps, and repeat-buyer flow — with the one thing most scanners hide: what happened next.",
        "intro": "<p>Most options-flow tools show you a firehose of big trades and leave you to guess which ones matter. TickerDesk scans ~180 liquid optionable names six times a market day (15-minute delayed), flags the unusual activity, and then <b>tracks every flagged signal to a real outcome</b> at +1/+3/+5/+10/+20 days versus SPY.</p>",
        "callout": _flow_callout(),
        "body": (
            "<h2>What it flags</h2><ul>"
            "<li><b>Golden sweeps</b> — aggressive, multi-exchange sweep orders paying the ask.</li>"
            "<li><b>Volume &gt; open interest</b> — new positioning, not existing books being traded.</li>"
            "<li><b>Repeat buyers</b> — the same contract accumulating across sessions.</li>"
            "<li><b>Opening-buyer premium</b> — bull vs bear dollar-premium tilt per name and market-wide.</li>"
            "</ul>"
            "<h2>Why the grading matters</h2>"
            "<p>A scan is only worth what it predicts. Every cohort — by signal type, DTE bucket, liquidity tier and flag — carries its own expected value, win rate, and confidence interval, recomputed nightly. Where a cohort shows no edge, the app says so instead of dressing it up.</p>"),
        "faq": [
            ("Is the options flow real-time?", "Data is 15-minute delayed (Polygon Options Starter), clearly labeled everywhere. The scan runs six times per market day."),
            ("Does options flow actually work?", "On the full ledger, following flow blindly is close to a coin flip — we publish that. The edge is in specific cohorts and decays within days. The <a href='/transparency'>transparency page</a> has the numbers."),
        ],
        "cta_href": "/#uoa",
        "cta_label": "Open the flow scanner",
    },
    {
        "slug": "options-open-interest-confirmation",
        "title": "Options Open-Interest Confirmation — Did the Flow Actually Stick? | TickerDesk",
        "meta": "Next-day open-interest confirmation on every flagged options signal: was the position actually opened and held, or gone by the next session? A check most flow scanners skip.",
        "h1": "Open-interest confirmation on every flow signal",
        "sub": "A big print only matters if the position is still there the next morning. We check.",
        "intro": "<p>Unusual options activity is noisy: a lot of eye-catching prints are closed, rolled, or hedged by the next session. TickerDesk re-reads each flagged contract's <b>open interest the next trading day</b> and tells you whether the position was confirmed (OI grew), weak, or already closed.</p>",
        "callout": _oi_callout(),
        "body": (
            "<h2>Three outcomes, tracked</h2><ul>"
            "<li><b>Confirmed</b> — open interest rose next day; the position was opened and held.</li>"
            "<li><b>Weak</b> — little or no OI follow-through.</li>"
            "<li><b>Closed</b> — OI fell; the flow was likely a same-day trade, not positioning.</li>"
            "</ul>"
            "<p>This is the differentiator most flow products don't offer. A sweep that shows up on a dozen scanners looks identical everywhere — until you check whether the money actually stayed in the trade.</p>"),
        "faq": [
            ("How is confirmation measured?", "By comparing each flagged contract's open interest the session after the flag to its level at flag time. OCC-settled OI updates once per day, so this is a next-morning read."),
            ("Why does it matter?", "Positioning that persists is a stronger signal than a print that's gone by the close. Confirmation separates the two."),
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
        "callout": _flow_callout(),
        "body": (
            "<h2>What makes a sweep 'golden'</h2><ul>"
            "<li>Multi-exchange execution — filled across venues in a hurry.</li>"
            "<li>Aggressor paying the ask — urgency, not patience.</li>"
            "<li>Size relative to the contract's normal liquidity.</li>"
            "</ul>"
            "<h2>Then the honest part</h2>"
            "<p>Golden sweeps are a real cohort in our edge attribution, tracked against SPY across horizons. You see the win rate and expected value, not just the alert.</p>"),
        "faq": [
            ("Are golden sweeps bullish?", "Direction depends on whether calls or puts are being swept and where price is. The scanner shows the premium tilt; the graded cohort shows how it has paid."),
            ("How fast do I see them?", "Within the 15-minute-delayed scan cadence, six times per market day."),
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
        "intro": "<p>Anyone can draw prior-day high, VWAP and the opening range. The question is which one holds. TickerDesk computes the full level set for SPY, QQQ and IWM each day and attaches a <b>measured touch, hold and break rate</b> to every level from hundreds of sessions of history.</p>",
        "callout": _levels_callout(),
        "body": (
            "<h2>The level set</h2><ul>"
            "<li>Prior-day high/low, prior close, overnight high/low/VWAP.</li>"
            "<li>Opening-range high/low, VWAP, VPOC / VAH / VAL.</li>"
            "<li>Dealer gamma flip, largest-gamma strike, call/put walls, expected-move band.</li>"
            "</ul>"
            "<h2>Plus rule-based trade ideas</h2>"
            "<p>A deterministic engine reads the day's structure and gamma regime into if/then playbook paths (fade-to-VWAP in positive gamma, break-and-go in negative), each graded on a same-session frame. All data is 15-minute delayed and labeled as such.</p>"),
        "faq": [
            ("Is this for 0DTE trading?", "It's an index-structure map built for 0DTE and weekly options decisions. It is not a live feed — data is 15-minute delayed — and it is educational, not advice."),
            ("Where do the hold/break rates come from?", "From an engine that replays ~250 sessions of 5-minute bars per symbol and measures how each level actually behaved on first touch."),
        ],
        "cta_href": "/#charts",
        "cta_label": "Open Index Levels",
    },
    {
        "slug": "earnings-options-flow",
        "title": "Earnings Options Flow — Implied vs Realized Moves + Trade Ideas | TickerDesk",
        "meta": "Earnings options flow: implied move vs the stock's historically realized move, post-report drift, flow into the print, and self-graded earnings trade ideas for large caps.",
        "h1": "Earnings options flow and trade ideas",
        "sub": "What the options are pricing into the print — versus what the stock has actually done.",
        "intro": "<p>The interesting earnings edge isn't the calendar, it's the mispricing. TickerDesk compares each name's <b>implied move to its historically realized move</b>, measures post-report drift, reads the options flow going into the print, and turns it into rule-based, self-graded trade ideas for names with real size.</p>",
        "body": (
            "<h2>Idea types</h2><ul>"
            "<li><b>Momentum into print</b> — leaders with bullish flow and positive historical post-report drift.</li>"
            "<li><b>Post-report drift</b> — the after-the-print trade, no binary overnight risk.</li>"
            "<li><b>Vol rich / vol cheap</b> — implied move well above or below the realized history.</li>"
            "<li><b>Binary caution</b> — big typical mover already extended into the event.</li>"
            "</ul>"
            "<p>Ideas are capped to $1B+ market caps and every type carries its own graded track record. Nothing publishes a hit rate before it has 30 real graded outcomes.</p>"),
        "faq": [
            ("Does it tell me to buy earnings?", "No. It surfaces where options pricing diverges from history and frames defined-risk ideas. It's educational, not advice, and flags crowded binaries to avoid."),
            ("Which names are covered?", "Large, liquid names reporting within about a week; the trade-idea layer is floored at $1B market cap."),
        ],
        "cta_href": "/#whisper",
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
            "<p>Grades come from a post-close batch; intraday percentage moves refresh live (15-minute delayed).</p>"),
        "faq": [
            ("How are setups graded?", "By a systematic momentum model, then tracked against real next-day and multi-day price action. The published record includes losing stretches."),
            ("Is this financial advice?", "No — it's a research tool. Verify with your broker before any trade."),
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
        "intro": "<p>The Volatility Contraction Pattern (VCP) is a base that tightens through progressively smaller pullbacks on declining volume before a breakout. TickerDesk's Setup Builder screens for VCP structure each afternoon and scores every candidate on setup quality so you're not eyeballing a hundred charts.</p>",
        "body": (
            "<h2>What the screen looks for</h2><ul>"
            "<li>A series of contractions — each pullback shallower than the last.</li>"
            "<li>Volume drying up into the apex of the base.</li>"
            "<li>A definable pivot / buy point with a tight stop.</li>"
            "</ul>"
            "<p>Each setup gets a quality score so the cleanest bases rise to the top. Refreshed post-close daily.</p>"),
        "faq": [
            ("What is a VCP?", "A volatility contraction pattern — a consolidation that tightens through successive smaller pullbacks on falling volume, popularized by Mark Minervini, often preceding a breakout."),
            ("How often does it update?", "Once per day after the close (16:15 ET)."),
        ],
        "cta_href": "/#setup",
        "cta_label": "Open the VCP screener",
    },
    {
        "slug": "ai-stock-research-reports",
        "title": "AI Stock Research Reports — On-Demand Deep Dives | TickerDesk",
        "meta": "On-demand AI stock research reports: an 8-12 page PDF on any ticker with the bull/bear case, recent options flow, key levels, filings and catalysts — generated in minutes.",
        "h1": "AI stock research reports on any ticker",
        "sub": "An 8–12 page deep dive generated on demand — bull/bear case, flow, levels, filings, catalysts.",
        "intro": "<p>Ask for any ticker and TickerDesk generates a structured research report with Claude: the bull and bear case, recent options flow, key technical levels, a filings and catalyst read, and a clear takeaway. Typically two to three minutes, delivered as a clean PDF you can keep.</p>",
        "body": (
            "<h2>What's in a report</h2><ul>"
            "<li>Bull case / bear case, stated plainly.</li>"
            "<li>Recent unusual options flow and positioning.</li>"
            "<li>Key support/resistance levels and trend context.</li>"
            "<li>Recent filings (EDGAR) and the upcoming catalyst calendar.</li>"
            "<li>A one-line takeaway, not a hedge-everything essay.</li>"
            "</ul>"
            "<p>Free accounts get 3 lifetime reports; Pro 10/month; Premium 100/month. Reports are on-demand only — you ask, it runs.</p>"),
        "faq": [
            ("How long does a report take?", "Usually two to three minutes end to end, delivered as a downloadable PDF."),
            ("What model generates them?", "Claude, over current market data, recent filings, news and alt-data at request time."),
        ],
        "cta_href": "/#adhoc",
        "cta_label": "Generate a report",
    },
    {
        "slug": "stock-market-morning-brief",
        "title": "Stock Market Morning Brief — Pre-Market Movers & Catalysts | TickerDesk",
        "meta": "A daily stock-market morning brief: pre-market movers, overnight news, macro calendar, before-the-open earnings, and the flow and setups that matter — in your inbox by 8:45 ET.",
        "h1": "Your stock market morning brief",
        "sub": "Pre-market movers, overnight news, the macro calendar and the day's setups — before the open.",
        "intro": "<p>Start the session already briefed. TickerDesk's daily email lands by 8:45 ET on weekdays with the pre-market movers, overnight headlines, the day's macro calendar, before-the-open earnings, and the flow and setups worth watching — a single decision-first read instead of ten open tabs.</p>",
        "body": (
            "<h2>What's in the brief</h2><ul>"
            "<li>Pre-market movers and why they're moving.</li>"
            "<li>Overnight news and the macro/economic calendar.</li>"
            "<li>Before-the-open earnings and notable after-close reports.</li>"
            "<li>The top flow, setups and watchlist changes from the desk.</li>"
            "</ul>"
            "<p>Opt in from any account; one-click unsubscribe in every email.</p>"),
        "faq": [
            ("When does it arrive?", "By 8:45 ET on market weekdays, ahead of the open."),
            ("Is it free?", "Opt in from any tier. You control it from your settings and can unsubscribe in one click."),
        ],
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
    "ai-stock-research-reports": "AI stock research reports",
    "stock-market-morning-brief": "Stock market morning brief",
}
for _p in PAGES:
    _p["link_label"] = LINK_LABELS.get(_p["slug"], _p["slug"])


def main():
    written = []
    for page in PAGES:
        html = render(page, PAGES)
        path = os.path.join(OUT_DIR, page["slug"] + ".html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        written.append(page["slug"])
    _write_sitemap(written)
    print("  landing pages: %d written (%s)" %
          (len(written), ", ".join(written)))
    return 0


def _write_sitemap(slugs):
    """Regenerate sitemap.xml: app root, transparency, landing pages, legal."""
    static = [
        ("/", "daily", "1.0"),
        ("/transparency", "weekly", "0.8"),
        ("/glossary", "monthly", "0.6"),
        ("/sample-report", "monthly", "0.6"),
    ]
    lp = [("/" + s, "weekly", "0.7") for s in slugs]
    legal = [("/privacy", "monthly", "0.3"), ("/terms", "monthly", "0.3")]
    rows = static + lp + legal
    body = ['<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, freq, pri in rows:
        body.append("  <url><loc>%s%s</loc><changefreq>%s</changefreq>"
                    "<priority>%s</priority></url>" % (SITE, loc, freq, pri))
    body.append("</urlset>")
    with open(os.path.join(OUT_DIR, "sitemap.xml"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(body) + "\n")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
