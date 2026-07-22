"""
research_snapshot.py — canonical state + publication gate for Research Brief v2.

v1 (scanner.py one-pagers, alt_data.py reports) is UNTOUCHED and still
ships. This module is the correctness core of v2: every section of a v2
brief reads from ONE ResearchSnapshot, and nothing renders until the
snapshot passes the contradiction gate.

WHY THIS EXISTS — the 2026-07-16 ISRG review found the narrative was more
confident than the evidence, and every defect was a STATE problem rather
than a missing-data problem:
  1. catalyst contradiction  alt-data said "earnings after the close",
     the ticker memo one minute later said "reported today / post-
     catalyst". ISRG actually released at 16:05 ET — both were pre-event.
  2. level contradiction     memo called $360 the 200-day while its own
     chart drew it near $486, and claimed price below every MA.
  3. reversed logic          AVOID followed by a long entry/stop/target,
     and "thesis breaks below $390" when a break below $390 CONFIRMS the
     bearish thesis.
  4. incoherent confidence   page 1 "85/100 model confidence", page 2
     "Conviction: Low".
  5. template contamination  ISRG called mega-cap, then judged "normal
     for SMID" and compared to "unprofitable growth companies" — ISRG
     earned $818M GAAP net income in the quarter.
  6. insider overstatement   RSU vesting with shares withheld for taxes
     reported as a "glaring red flag" of bearish selling.
  7. ownership incompleteness "no 13D/13G in 12 months" while Vanguard
     filed a 13G/A on 2026-03-27; and 13G was called "lower conviction"
     when it simply means passive/non-control.
  8. unsupported inference   29 StockTwits messages described as "heavy
     institutional call flow"; PT cuts described as downgrades.
  9. sample contamination    Reddit posts about NRED/VYNE//earnings
     calendars counted toward ISRG buzz.
 10. unlabeled multiples     33.8x (forward) vs 50x (unstated basis).

Every one of those is encoded as a blocking check below, and the ISRG
fixture in --self-test reproduces the July 16 state and asserts the gate
catches all ten. A snapshot that would render the July 16 brief CANNOT
publish under v2.

    python research_snapshot.py --self-test
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone

# ── provenance-carrying value ───────────────────────────────────────────
# Every figure in a snapshot is a Fact. An unsourced or undated number
# cannot enter the brief, and a multiple without a basis is rejected —
# that is finding #10, enforced structurally rather than by review.


PROD = "production"
DEMO = "demo"

# Quality states a value may carry.
Q_OK = "ok"                  # reconciled to a cited primary source
Q_UNVERIFIED = "unverified"  # sourced but not reconciled
Q_DERIVED = "derived"        # computed from other facts in this snapshot
Q_DEMO = "DEMO"              # synthetic — can never leave as a real report
Q_STALE = "stale"


def fact(value, source=None, as_of=None, basis=None, unit=None, note=None,
         metric=None, source_url=None, source_type=None, period_end=None,
         published_at=None, retrieved_at=None, market_asof=None,
         calc_version=None, quality=Q_UNVERIFIED, evidence_id=None,
         series_id=None, gaap=None, evidence_refs=None):
    """A value plus everything needed to audit it.

    period_end is WHEN THE PERIOD ENDED; published_at is WHEN THE NUMBER
    BECAME PUBLIC. Conflating the two is what let a report written at
    17:49 UTC quote results released at 20:05 UTC — a quarter-end date of
    2026-06-30 says nothing about availability. The point-in-time gate
    below keys on published_at ONLY.

    basis stays REQUIRED for ratios (forward | trailing | ntm | ltm), and
    gaap must be stated for income-statement figures."""
    return {"v": value, "src": source, "as_of": as_of,
            "basis": basis, "unit": unit, "note": note,
            "metric": metric, "source_url": source_url,
            "source_type": source_type, "period_end": period_end,
            "published_at": published_at, "retrieved_at": retrieved_at,
            "market_asof": market_asof, "calc_version": calc_version,
            "quality": quality, "evidence_id": evidence_id,
            "series_id": series_id, "gaap": gaap,
            # exact records/calculations this value stands on, so a reader
            # can reproduce it from the companion export
            "evidence_refs": list(evidence_refs or [])}


def demo_fact(value, **kw):
    """Synthetic value. Permanently marked; a snapshot containing one
    cannot be exported as a normal research report."""
    kw["quality"] = Q_DEMO
    kw.setdefault("source", "SYNTHETIC DEMO FIXTURE")
    kw.setdefault("source_type", "demo")
    return fact(value, **kw)


def is_demo(snap):
    """True if the snapshot is demo-mode or carries ANY synthetic value.
    Checked recursively so a single demo fact poisons the whole export."""
    if (snap or {}).get("mode") == DEMO:
        return True
    def walk(o):
        if isinstance(o, dict):
            if o.get("quality") == Q_DEMO:
                return True
            return any(walk(v) for v in o.values())
        if isinstance(o, (list, tuple)):
            return any(walk(v) for v in o)
        return False
    return walk(snap)


def demo_fact_paths(snap):
    """Where the synthetic values are — so the block message can name
    them instead of just refusing."""
    out = []
    def walk(o, path):
        if isinstance(o, dict):
            if o.get("quality") == Q_DEMO:
                out.append(path or "?")
                return
            for k, v in o.items():
                walk(v, "%s.%s" % (path, k) if path else str(k))
        elif isinstance(o, (list, tuple)):
            for i, v in enumerate(o):
                walk(v, "%s[%d]" % (path, i))
    walk(snap, "")
    return out


def fv(f, default=None):
    """Read a Fact's value (None-safe)."""
    if isinstance(f, dict) and "v" in f:
        return f["v"] if f["v"] is not None else default
    return f if f is not None else default


# ── catalyst state machine ──────────────────────────────────────────────

PRE_EVENT = "PRE_EVENT"
EVENT_IN_PROGRESS = "EVENT_IN_PROGRESS"
POST_EVENT_UNGRADED = "POST_EVENT_UNGRADED"
POST_EVENT_GRADED = "POST_EVENT_GRADED"
CATALYST_STATES = (PRE_EVENT, EVENT_IN_PROGRESS, POST_EVENT_UNGRADED,
                   POST_EVENT_GRADED)

# Language that is only valid in specific states. The July 16 memo used
# post-event language 2h17m BEFORE the release; that is now unpublishable.
STATE_FORBIDDEN_PHRASES = {
    PRE_EVENT: ["post-catalyst", "post catalyst", "reported today",
                "reported this morning", "after the print", "the print was",
                "results showed", "beat expectations", "missed expectations",
                "post-earnings", "following the report", "reaction to earnings"],
    EVENT_IN_PROGRESS: ["post-catalyst", "reported today", "results showed",
                        "beat expectations", "missed expectations"],
    POST_EVENT_UNGRADED: ["ahead of earnings", "into the print",
                          "before the report", "upcoming earnings"],
    POST_EVENT_GRADED: ["ahead of earnings", "into the print",
                        "before the report", "upcoming earnings"],
}


def resolve_catalyst_state(event_dt, now=None, grade_after_min=90):
    """Point-in-time catalyst state. event_dt/now are tz-aware datetimes.

    PRE_EVENT              now < event
    EVENT_IN_PROGRESS      event <= now < event + grade_after_min
    POST_EVENT_UNGRADED    released, market reaction not yet measurable
                           (i.e. before the next session's close)
    POST_EVENT_GRADED      a full session has traded on the news
    """
    if event_dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    if now < event_dt:
        return PRE_EVENT
    if now < event_dt + timedelta(minutes=grade_after_min):
        return EVENT_IN_PROGRESS
    # a session boundary must pass before a reaction can be graded
    if now < event_dt + timedelta(hours=20):
        return POST_EVENT_UNGRADED
    return POST_EVENT_GRADED


# ── decision vocabulary ─────────────────────────────────────────────────
# Finding #3: "AVOID + entry/stop/target" and "thesis breaks below X"
# (when a break below X confirms the thesis). v2 replaces the ambiguous
# single recommendation with three explicitly-named fields.

ACTIONS = ("BUY", "ACCUMULATE", "HOLD", "WAIT", "AVOID", "REDUCE")
NON_LONG_ACTIONS = ("WAIT", "AVOID", "REDUCE")


def new_snapshot(ticker, report_time, market_data_time):
    """Empty canonical snapshot. Every v2 section reads from this and
    ONLY this — the July 16 brief drew levels from one calculation and
    the chart from another, which is how $360 and $486 both claimed to
    be the 200-day."""
    return {
        "schema": "research_snapshot/v2",
        "ticker": ticker.upper(),
        "report_time": report_time,
        "market_data_time": market_data_time,
        "company": {},        # name, sector, market_cap, profitable, universe
        "price": {},          # last, prev_close, change_pct
        "levels": {},         # ma20/50/200, support, resistance, atr, chart_*
        "fundamentals": {},   # revenue/eps growth, margins, installed base…
        "valuation": {},      # each multiple a Fact WITH basis
        "catalyst": {},       # event_dt, state, description
        "ownership": {},      # institutional_pct, filings[]
        "insiders": {},       # classified transactions
        "sentiment": {},      # alt-data with relevance accounting
        "decision": {},       # current_action, upgrade_trigger, downside_confirmation
        "evidence": {},       # quality + completeness (NOT a 0-100 score)
        "flags": [],          # data-quality flags
    }


# ── insider classification (finding #6) ─────────────────────────────────
# Form 4 transaction codes. The July 16 brief treated every disposition
# as bearish selling; the July 10 ISRG CEO transaction was RSU vesting
# with shares withheld for taxes — a compensation mechanic, not a view.

OPEN_MARKET_BUY = "open_market_buy"
OPEN_MARKET_SALE = "open_market_sale"
PLANNED_SALE = "planned_sale_10b5_1"
OPTION_EXERCISE = "option_exercise"
TAX_WITHHOLDING = "tax_withholding"
GRANT_AWARD = "grant_award"
OTHER_NONMARKET = "other_non_market"

CODE_CLASS = {
    "P": OPEN_MARKET_BUY,
    "S": OPEN_MARKET_SALE,
    "M": OPTION_EXERCISE,     # exercise of derivative — NOT a market trade
    "F": TAX_WITHHOLDING,     # shares withheld to cover tax on vesting
    "A": GRANT_AWARD,         # grant/award — compensation, not conviction
    "G": OTHER_NONMARKET,     # gift
    "C": OTHER_NONMARKET,     # conversion
    "X": OPTION_EXERCISE,     # exercise of in-the-money option
    "D": OTHER_NONMARKET,     # disposition to issuer
}
# Only these carry directional information about an insider's view.
SIGNAL_CLASSES = (OPEN_MARKET_BUY, OPEN_MARKET_SALE)


def classify_insider(txn):
    """Classify one Form 4 row and mark whether it carries a view.

    txn: {code, shares, price, value, date, owner, title,
          shares_owned_after?, footnotes?}"""
    code = (txn.get("code") or "").strip().upper()
    cls = CODE_CLASS.get(code, OTHER_NONMARKET)
    foot = " ".join(str(x) for x in (txn.get("footnotes") or [])).lower()
    plan = bool(re.search(r"10b5[- ]?1", foot))
    if cls == OPEN_MARKET_SALE and plan:
        cls = PLANNED_SALE
    owned_after = txn.get("shares_owned_after")
    shares = txn.get("shares") or 0
    pct_of_holdings = None
    if owned_after is not None and (owned_after + shares) > 0 and \
            cls in (OPEN_MARKET_SALE, PLANNED_SALE):
        pct_of_holdings = round(100.0 * shares / (owned_after + shares), 1)
    out = dict(txn)
    out.update({
        "class": cls,
        "carries_view": cls in SIGNAL_CLASSES,
        "is_planned": plan,
        "pct_of_holdings": pct_of_holdings,
    })
    return out


def summarize_insiders(txns):
    """Only open-market activity may drive an insider read. Everything
    else is reported as mechanics, never as a 'red flag'."""
    rows = [classify_insider(t) for t in (txns or [])]
    view = [r for r in rows if r["carries_view"]]
    buys = [r for r in view if r["class"] == OPEN_MARKET_BUY]
    sells = [r for r in view if r["class"] == OPEN_MARKET_SALE]
    mech = [r for r in rows if not r["carries_view"]]
    by_class = {}
    for r in rows:
        by_class[r["class"]] = by_class.get(r["class"], 0) + 1
    if not view:
        read = ("no open-market insider transactions in window — "
                "remaining activity is compensation mechanics "
                "(vesting, withholding, exercises), which carry no "
                "directional view")
    else:
        bv = sum(r.get("value") or 0 for r in buys)
        sv = sum(r.get("value") or 0 for r in sells)
        if bv > sv * 1.5:
            read = "net open-market buying"
        elif sv > bv * 1.5:
            read = "net open-market selling"
        else:
            read = "mixed open-market activity"
    return {
        "rows": rows,
        "n_total": len(rows),
        "n_view_bearing": len(view),
        "n_mechanics": len(mech),
        "by_class": by_class,
        "read": read,
        "note": ("Vesting (F), grants (A), and exercises (M/X) are "
                 "compensation mechanics, not market views. Planned "
                 "10b5-1 sales are pre-scheduled and reported "
                 "separately from discretionary sales."),
    }


# ── alt-data relevance (findings #8 and #9) ─────────────────────────────

def score_alt_data(posts, ticker, options_feed_verified=False):
    """Relevance-gate social posts and refuse to infer institutional
    activity from them.

    A post counts only if it actually references the ticker. The July 16
    alt-data page counted Reddit posts about NRED, VYNE and generic
    earnings calendars toward ISRG buzz, then described 29 StockTwits
    messages as 'heavy institutional call flow'."""
    tk = (ticker or "").upper()
    pat = re.compile(r"(?:^|[^A-Z])\$?" + re.escape(tk) + r"(?:[^A-Z]|$)")
    kept, dropped, authors = [], [], set()
    seen_pairs = set()          # (author, text) — self-repeats only
    text_authors = {}           # text -> distinct authors saying it
    for p in posts or []:
        body = str(p.get("text") or p.get("body") or "")
        if not pat.search(body.upper()):
            dropped.append(p)
            continue
        norm = re.sub(r"\s+", " ", body.strip().lower())
        who = str(p.get("author") or p.get("user") or "?")
        # De-dup is PER AUTHOR. The same wording from DIFFERENT accounts
        # is not noise to be discarded — it is the only evidence that
        # could reveal coordination, so it is counted and measured below.
        if (who, norm) in seen_pairs:
            dropped.append(p)
            continue
        seen_pairs.add((who, norm))
        text_authors.setdefault(norm, set()).add(who)
        authors.add(who)
        kept.append(p)
    # coordination = identical wording repeated across >=3 distinct
    # accounts, expressed as a share of the relevant sample
    echoed = [t for t, a in text_authors.items() if len(a) >= 3]
    echo_posts = sum(len(text_authors[t]) for t in echoed)
    echo_share = (100.0 * echo_posts / len(kept)) if kept else 0.0
    srcs = {}
    for p in kept:
        s = str(p.get("source") or "unknown")
        srcs[s] = srcs.get(s, 0) + 1
    # Language discipline: social NEVER implies institutional execution.
    if options_feed_verified:
        flow_language = "options feed shows institutional-size execution"
    else:
        flow_language = ("retail call discussion — social posts cannot "
                         "evidence institutional activity")
    return {
        "n_considered": len(posts or []),
        "n_relevant": len(kept),
        "n_dropped_irrelevant": len(dropped),
        "unique_authors": len(authors),
        "source_mix": srcs,
        "flow_language": flow_language,
        "echoed_phrases": len(echoed),
        "echoed_share_pct": round(echo_share, 1),
        "coordination": (
            "sample too small to assess" if len(kept) < 20 else
            ("%d phrase(s) repeated verbatim across 3+ accounts "
             "(%.0f%% of the sample) — possible echo, not verified"
             % (len(echoed), echo_share)) if echoed else
            "no coordination detected in this limited sample"),
        "sample_adequacy": ("adequate" if len(kept) >= 50 and
                            len(authors) >= 25 else "thin"),
        "note": ("Buzz and sentiment count only ticker-relevant, "
                 "de-duplicated posts. Unique authors are reported "
                 "because N posts from few authors is not N opinions."),
    }


def classify_analyst_action(prev_rating, new_rating, prev_pt, new_pt):
    """Finding #8b: a price-target cut with the rating maintained is NOT
    a downgrade."""
    rating_changed = (prev_rating and new_rating and
                      prev_rating.strip().lower() != new_rating.strip().lower())
    pt_changed = (prev_pt is not None and new_pt is not None and
                  prev_pt != new_pt)
    if rating_changed:
        return "rating_change"
    if pt_changed:
        return ("price_target_cut_rating_maintained" if new_pt < prev_pt
                else "price_target_raise_rating_maintained")
    return "reiteration"


# ── ownership (finding #7) ──────────────────────────────────────────────

def summarize_ownership(filings, institutional_pct=None):
    """13G is passive/non-control reporting — not 'lower conviction'.
    Amendments (13G/A, 13D/A) count as filings."""
    rows = filings or []
    kinds = {}
    for f in rows:
        k = (f.get("form") or "").upper().replace("SC ", "").strip()
        kinds[k] = kinds.get(k, 0) + 1
    return {
        "filings": rows,
        "n_filings": len(rows),
        "by_form": kinds,
        "institutional_pct": institutional_pct,
        "note": ("13D = activist/control intent; 13G = passive or "
                 "non-control position, including amendments (13G/A). "
                 "Passive filing status is a regulatory classification, "
                 "not a statement about conviction. High institutional "
                 "ownership does not imply an absence of buyers."),
    }


# ── contradiction gate ──────────────────────────────────────────────────

class Contradiction(Exception):
    pass


def _iter_facts(o, path=""):
    if isinstance(o, dict):
        if "v" in o and "quality" in o:
            yield path, o
            return
        for k, v in o.items():
            yield from _iter_facts(v, "%s.%s" % (path, k) if path else str(k))
    elif isinstance(o, (list, tuple)):
        for i, v in enumerate(o):
            yield from _iter_facts(v, "%s[%d]" % (path, i))


def _parse_ts(v):
    if not v:
        return None
    s = str(v).strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d"):
        try:
            dt = (datetime.fromisoformat(s) if fmt is None
                  else datetime.strptime(s, fmt))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def check_point_in_time(snap):
    """A value may enter a report only if it was PUBLIC when the report
    was written. Quarter-end is not publication."""
    v = []
    asof = _parse_ts(snap.get("report_time"))
    if not asof:
        return ["report_time missing or unparseable — point-in-time gate "
                "cannot run"]
    for path, f in _iter_facts(snap):
        if f.get("v") is None:
            continue
        pub = _parse_ts(f.get("published_at"))
        pe = _parse_ts(f.get("period_end"))
        if pub is None:
            if f.get("source_type") in ("filing", "press_release",
                                        "company_release", "transcript"):
                v.append("%s: %s has no published_at — a filing figure "
                         "cannot be admitted without its publication time"
                         % (path, f.get("metric") or "value"))
            continue
        if pub > asof:
            v.append("%s: published_at %s is AFTER report_time %s — this "
                     "information was not available when the report was "
                     "written" % (path, f.get("published_at"),
                                  snap.get("report_time")))
        if pe and pub and pe > pub:
            v.append("%s: period_end %s is after published_at %s "
                     "(impossible)" % (path, f.get("period_end"),
                                       f.get("published_at")))
    return v


def check_market_data_coherence(snap):
    """One point-in-time price series behind every derived level, and
    market cap that survives arithmetic."""
    v = []
    px = snap.get("price") or {}
    lv = snap.get("levels") or {}
    co = snap.get("company") or {}
    head = fv(px.get("last"))
    tech = fv(lv.get("price_used"))
    if head is not None and tech is not None and head:
        if abs(head - tech) / abs(head) > 0.001:
            v.append("headline price %.2f differs from the price used for "
                     "technical levels %.2f — levels must derive from one "
                     "point-in-time series" % (head, tech))
    # every level must declare the same series id
    sid = None
    for k in ("ma20", "ma50", "ma200", "support", "resistance",
              "resistance_major", "atr14", "price_used"):
        f = lv.get(k)
        if isinstance(f, dict) and f.get("series_id"):
            if sid is None:
                sid = f["series_id"]
            elif f["series_id"] != sid:
                v.append("level '%s' derives from series '%s' but others "
                         "use '%s'" % (k, f["series_id"], sid))
    # market cap must reconcile with price x shares
    cap = fv(co.get("market_cap"))
    sh = fv(co.get("shares_outstanding"))
    if cap is not None and sh and head:
        implied = head * sh
        if abs(implied - cap) / max(abs(cap), 1) > 0.02:
            v.append("market cap %.1fB disagrees with price %.2f x shares "
                     "%.1fM = %.1fB (>2%%) — one of them is wrong"
                     % (cap / 1e9, head, sh / 1e6, implied / 1e9))
    elif cap is not None and not sh:
        v.append("market cap supplied without shares_outstanding — cannot "
                 "verify the arithmetic")
    # resistance ordering must be explained
    r1, r2 = fv(lv.get("resistance")), fv(lv.get("resistance_major"))
    if r1 is not None and r2 is not None:
        if r2 < r1:
            v.append("major resistance %.2f below first resistance %.2f"
                     % (r2, r1))
        if not (lv.get("resistance") or {}).get("note") or \
           not (lv.get("resistance_major") or {}).get("note"):
            v.append("first and major resistance are both present but not "
                     "distinguished — each needs a note explaining what it "
                     "is")
    return v


def check_evidence_links(snap, decision_claims=None):
    """Every decision claim must point at an appendix evidence record,
    and 'complete' may not be claimed unless every dependency is there."""
    v = []
    ap = snap.get("appendix") or {}
    ids = set(ap.get("evidence_ids") or [])
    claims = decision_claims or (snap.get("decision") or {}).get("claims") or []
    for c in claims:
        eid = (c or {}).get("evidence_id")
        if not eid:
            v.append("decision claim %r has no evidence_id"
                     % str((c or {}).get("text", ""))[:60])
        elif eid not in ids:
            v.append("decision claim cites evidence_id '%s' which is not "
                     "in the appendix" % eid)
    if ap.get("claims_complete"):
        need = set()
        for path, f in _iter_facts(snap):
            if f.get("v") is not None and f.get("evidence_id"):
                need.add(f["evidence_id"])
        # a completeness claim also covers everything the decision cites,
        # including claims that cite nothing at all
        uncited = sum(1 for c in claims if not (c or {}).get("evidence_id"))
        for c in claims:
            if (c or {}).get("evidence_id"):
                need.add(c["evidence_id"])
        if uncited:
            v.append("appendix claims completeness while %d decision "
                     "claim(s) cite no evidence record at all" % uncited)
        missing = sorted(need - ids)
        if missing:
            v.append("appendix claims completeness but %d referenced "
                     "record(s) are absent: %s"
                     % (len(missing), ", ".join(missing[:5])))
    return v


# ── alt-data provenance, baseline and accounting ────────────────────────

BASELINE_LIVE = "LIVE_PIT_BASELINE"          # archived before each report
BASELINE_RECON = "RECONSTRUCTED_BASELINE"    # fetched/rebuilt after the fact
BASELINE_NONE = "NO_BASELINE"
BASELINE_KINDS = (BASELINE_LIVE, BASELINE_RECON, BASELINE_NONE)

# Only originators qualify as PRIMARY. Channel checks and media are
# secondary no matter how good the outlet.
PRIMARY_NEWS_TYPES = ("company_ir", "sec", "regulator", "exchange",
                      "company_release")
SENTIMENT_CLASSES = ("bullish", "bearish", "neutral", "uncertain")
COORD_MIN_AUTHORS = 3            # frozen: phrase shared by >=3 accounts
ALT_MIN_AUTHORS = 10


def social_record(source, record_id, published_at, retrieved_at, text,
                  author_hash, url=None, relevance=None, sentiment=None,
                  dup_group=None, disposition=None, reason=None,
                  text_hash=None, quality=Q_UNVERIFIED):
    """One auditable social/news observation. Every field is required for
    production; a record missing provenance is rejected rather than
    quietly counted. author_hash (not the handle) keeps the appendix
    publishable without exposing individual accounts."""
    import hashlib
    return {
        "source": source, "record_id": record_id, "url": url,
        "author_hash": author_hash, "published_at": published_at,
        "retrieved_at": retrieved_at,
        "text_hash": text_hash or hashlib.sha256(
            str(text or "").encode("utf-8")).hexdigest()[:16],
        "relevance": relevance, "sentiment": sentiment,
        "dup_group": dup_group, "disposition": disposition,
        "reason": reason, "quality": quality, "_text": text,
    }


def validate_social_records(records):
    """Provenance gate for the raw feed."""
    v = []
    need = ("source", "record_id", "author_hash", "published_at",
            "retrieved_at", "text_hash", "relevance", "disposition")
    for i, r in enumerate(records or []):
        for k in need:
            if r.get(k) in (None, ""):
                v.append("social record %s missing '%s'"
                         % (r.get("record_id") or "#%d" % i, k))
        if r.get("disposition") == "counted" and not r.get("sentiment"):
            v.append("counted record %s has no sentiment classification"
                     % r.get("record_id"))
        if r.get("disposition") not in (None, "counted", "rejected"):
            v.append("record %s has unknown disposition '%s'"
                     % (r.get("record_id"), r.get("disposition")))
        if r.get("disposition") == "rejected" and not r.get("reason"):
            v.append("rejected record %s gives no reason"
                     % r.get("record_id"))
    return v


def build_alt_block(records, baseline=None, news=None,
                    options_feed_verified=False):
    """Full alt-data accounting from provenance-carrying records.

    Every published number is derived here so the identities below
    cannot drift apart: considered = counted + rejected, and
    counted = bullish + bearish + neutral + uncertain."""
    recs = list(records or [])
    counted = [r for r in recs if r.get("disposition") == "counted"]
    rejected = [r for r in recs if r.get("disposition") == "rejected"]
    by_class, authors_by_class = {}, {}
    for c in SENTIMENT_CLASSES:
        sel = [r for r in counted if (r.get("sentiment") or "").lower() == c]
        by_class[c] = len(sel)
        authors_by_class[c] = len({r.get("author_hash") for r in sel})
    authors = {r.get("author_hash") for r in counted}
    # coordination: a phrase shared by >= COORD_MIN_AUTHORS distinct
    # accounts. Reported as posts AND authors affected, never as a bare
    # percentage that could be mistaken for the whole sample.
    groups = {}
    for r in counted:
        groups.setdefault(r.get("dup_group") or r.get("text_hash"),
                          []).append(r)
    echo = {g: rs_ for g, rs_ in groups.items()
            if len({x.get("author_hash") for x in rs_}) >= COORD_MIN_AUTHORS}
    echo_posts = sum(len(v) for v in echo.values())
    echo_authors = len({x.get("author_hash")
                        for v in echo.values() for x in v})
    src_mix = {}
    for r in counted:
        src_mix[r.get("source")] = src_mix.get(r.get("source"), 0) + 1
    # author- vs post-weighted direction, reported separately
    # Directional share is computed on the DIRECTIONAL subset (bull+bear),
    # which on a real feed is usually a minority of the counted sample —
    # a live ISRG run produced "100% bullish" from 6 bullish, 0 bearish
    # and 15 neutral posts. The base travels with the number so the
    # renderer cannot print the percentage without it.
    dir_posts = by_class["bullish"] + by_class["bearish"]
    dir_authors = authors_by_class["bullish"] + authors_by_class["bearish"]

    def _pw():
        return round(100.0 * by_class["bullish"] / dir_posts) if dir_posts \
            else None

    def _aw():
        return round(100.0 * authors_by_class["bullish"] / dir_authors) \
            if dir_authors else None
    bl = dict(baseline or {"kind": BASELINE_NONE})
    if bl.get("kind") not in BASELINE_KINDS:
        bl["kind"] = BASELINE_NONE
    cur = len(counted)
    if bl.get("mean") is not None and bl.get("stdev"):
        bl["z_score"] = round((cur - bl["mean"]) / bl["stdev"], 2)
    block = {
        "n_considered": len(recs),
        "n_relevant": cur,
        "n_rejected": len(rejected),
        "unique_authors": len(authors),
        "by_class": by_class,
        "authors_by_class": authors_by_class,
        "post_weighted_bull_pct": _pw(),
        "author_weighted_bull_pct": _aw(),
        "directional_posts": dir_posts,
        "directional_authors": dir_authors,
        "non_directional_posts": len(counted) - dir_posts,
        "source_mix": src_mix,
        "baseline": bl,
        "coordination": {
            "phrase_groups": len(echo),
            "posts_affected": echo_posts,
            "authors_affected": echo_authors,
            "pct_of_relevant_posts": round(100.0 * echo_posts / cur, 1)
                                     if cur else 0.0,
            "threshold": "phrase shared verbatim by >=%d distinct accounts"
                         % COORD_MIN_AUTHORS,
            "label": ("possible echo — not verified as manipulation"
                      if echo else "no repeated-phrase groups detected"),
        },
        "flow_language": ("options feed shows institutional-size execution"
                          if options_feed_verified else
                          "retail discussion — social posts cannot evidence "
                          "institutional activity"),
        "news": list(news or []),
    }
    block["classification"] = ("INSUFFICIENT SAMPLE"
                               if len(authors) < ALT_MIN_AUTHORS else "SCORED")
    # "Divergence" is a claim about a comparison. It requires a defined
    # baseline, a stated comparison period, a sample above the floor,
    # comparable source coverage, and a stored calculation. Absent any of
    # those this is CONTEXT, and the heading says so.
    _div_reqs = {
        "baseline_defined": bl.get("kind") in (BASELINE_LIVE, BASELINE_RECON),
        "comparison_period_stated": bl.get("sessions") is not None,
        "sample_above_floor": len(authors) >= ALT_MIN_AUTHORS,
        "calculation_stored": bl.get("z_score") is not None,
    }
    block["divergence_requirements"] = _div_reqs
    block["divergence_supported"] = all(_div_reqs.values())
    block["section_title"] = ("Alt-data divergence" if block["divergence_supported"]
                              else "Alt-data context")
    # decision read — explicitly observational when evidence is weak
    weak = (block["classification"] == "INSUFFICIENT SAMPLE"
            or bl.get("kind") != BASELINE_LIVE)
    block["decision_read"] = {
        "attention": ("elevated" if bl.get("z_score") and bl["z_score"] >= 2
                      else "normal" if bl.get("z_score") is not None
                      else "unknown (no baseline)"),
        "direction": (
            "no directional read — sample below author floor"
            if block["classification"] == "INSUFFICIENT SAMPLE"
            else "no directional posts — every counted post is neutral "
                 "or uncertain"
            if not dir_posts
            else "%s%% bullish of the %d directional posts (%d of %d "
                 "counted posts express no direction); author-weighted "
                 "%s%% of %d directional authors"
                 % (block["post_weighted_bull_pct"], dir_posts,
                    block["non_directional_posts"], len(counted),
                    block["author_weighted_bull_pct"], dir_authors)),
        "reliability": ("weak" if weak else "moderate"),
        "changed_vs_baseline": (
            "no baseline available" if bl["kind"] == BASELINE_NONE else
            "z=%s vs %s (%s)" % (bl.get("z_score"), bl["kind"],
                                 bl.get("sessions"))),
        # the mandated conclusion wording, used verbatim in PDF and JSON
        "implication": ("Observational context only; not an independent "
                        "trade signal." if weak else
                        "Corroborating context only; not a standalone "
                        "signal."),
    }
    return block


def migrate_alt_block(block):
    """Upgrade a v1-shaped alt-data block to the v2 schema.

    v1 stored `coordination` as a bare sentence and carried no
    directional base or section title. The v2 renderer reads those as
    structured fields, so a stored v1 block crashed it outright. This
    normalizes rather than guesses: counts that v1 never recorded stay
    absent, and the block is marked so a reader knows which fields were
    reconstructed and which were simply never captured.
    """
    b = dict(block or {})
    if b.get("schema_version") == 2:
        return b
    migrated = []
    co = b.get("coordination")
    if isinstance(co, str):
        b["coordination"] = {"label": co, "phrase_groups": None,
                             "posts_affected": None,
                             "authors_affected": None,
                             "pct_of_relevant_posts": None,
                             "threshold": None,
                             "note": "counts not recorded in v1"}
        migrated.append("coordination")
    elif co is None:
        b["coordination"] = {"label": "not assessed",
                             "phrase_groups": None}
        migrated.append("coordination")
    bc = b.get("by_class") or {}
    if bc and b.get("directional_posts") is None:
        b["directional_posts"] = bc.get("bullish", 0) + bc.get("bearish", 0)
        ac = b.get("authors_by_class") or {}
        b["directional_authors"] = (ac.get("bullish", 0)
                                    + ac.get("bearish", 0)) or None
        if b.get("n_relevant") is not None:
            b["non_directional_posts"] = (b["n_relevant"]
                                          - b["directional_posts"])
        migrated.append("directional_base")
    if not b.get("section_title"):
        bl = b.get("baseline") or {}
        ok = (bl.get("kind") in (BASELINE_LIVE, BASELINE_RECON)
              and bl.get("z_score") is not None)
        b["section_title"] = ("Alt-data divergence" if ok
                              else "Alt-data context")
        b["divergence_supported"] = bool(ok)
        migrated.append("section_title")
    if b.get("baseline") is None:
        b["baseline"] = {"kind": BASELINE_NONE}
        migrated.append("baseline")
    b["schema_version"] = 2
    b["migrated_fields"] = migrated
    return b


def classify_news_tier(source_type):
    """PRIMARY is reserved for originators."""
    return ("PRIMARY SOURCE" if source_type in PRIMARY_NEWS_TYPES
            else "SECONDARY")


def check_alt_data_integrity(snap):
    """Accounting identities, baseline honesty, and appendix truthfulness.
    Any failure blocks export."""
    v = []
    s = snap.get("sentiment") or {}
    if not s:
        return v
    con, rel, rej = (s.get("n_considered"), s.get("n_relevant"),
                     s.get("n_rejected"))
    if None not in (con, rel, rej) and con != rel + rej:
        v.append("alt-data accounting fails: considered %d != relevant %d + "
                 "rejected %d" % (con, rel, rej))
    bc = s.get("by_class") or {}
    if bc:
        tot = sum(bc.get(c, 0) for c in SENTIMENT_CLASSES)
        if rel is not None and tot != rel:
            v.append("sentiment classes sum to %d but relevant is %d "
                     "(bull/bear/neutral/uncertain must partition the "
                     "counted sample)" % (tot, rel))
    elif rel and s.get("classification") != "INSUFFICIENT SAMPLE":
        # below the author floor the sample is descriptive-only, so no
        # breakdown is required (and none may be scored). Above it, a
        # directional read without the counts is unpublishable.
        v.append("alt-data reports %d relevant posts with no directional "
                 "breakdown — a signal cannot be called bullish without "
                 "bull/bear/neutral/uncertain counts" % rel)
    # A directional share is computed on bull+bear only. Publishing it
    # without that base reads as a share of the whole sample: a live run
    # produced "100% bullish" from 6 bullish / 0 bearish / 15 neutral.
    pw = s.get("post_weighted_bull_pct")
    if pw is not None:
        dp = s.get("directional_posts")
        if dp is None:
            v.append("directional share of %s%% published without the "
                     "number of directional posts it was computed on"
                     % pw)
        else:
            if rel is not None and dp > rel:
                v.append("directional posts %d exceed the counted sample "
                         "%d" % (dp, rel))
            dirline = str((s.get("decision_read") or {}).get("direction")
                          or "")
            if dirline and re.search(r"\d+\s*%", dirline) and \
                    not re.search(r"\b%d\b" % dp, dirline):
                v.append("direction reads %r without stating that it "
                         "covers %d of %s counted posts"
                         % (dirline[:60], dp, rel))
    bl = s.get("baseline") or {}
    kind = bl.get("kind")
    if kind and kind not in BASELINE_KINDS:
        v.append("unknown baseline kind '%s'" % kind)
    if kind == BASELINE_RECON and bl.get("presented_as_live"):
        v.append("reconstructed baseline is presented as information "
                 "available at report time — it was not")
    if kind in (BASELINE_LIVE, BASELINE_RECON):
        for k in ("sessions", "missing_sessions", "mean", "median", "stdev"):
            if bl.get(k) is None:
                v.append("baseline missing '%s'" % k)
    co = s.get("coordination") or {}
    if isinstance(co, str):        # legacy string form carries no counts
        co = {"label": co}
    if co:
        pa, rp = co.get("posts_affected"), s.get("n_relevant")
        if pa is not None and rp and pa > rp:
            v.append("coordination claims %d affected posts out of %d "
                     "relevant" % (pa, rp))
        if co.get("pct_of_relevant_posts") == 100 and pa != rp:
            v.append("coordination reports 100%% of the sample but only "
                     "%s of %s posts are covered" % (pa, rp))
        lab = str(co.get("label") or "")
        # flag an ASSERTION of manipulation, not an explicit denial such
        # as "possible echo — not verified as manipulation"
        if re.search(r"manipulat", lab, re.I) and not re.search(
                r"not verified|no evidence|possible echo|cannot confirm",
                lab, re.I):
            v.append("coordination label asserts manipulation — use "
                     "'possible echo' without stronger evidence")
    for n in (s.get("news") or []):
        if not n.get("url"):
            v.append("news item %r has no URL" % str(n.get("headline"))[:48])
        if not n.get("published_at"):
            v.append("news item %r has no publication time"
                     % str(n.get("headline"))[:48])
        if n.get("tier") == "PRIMARY SOURCE" and \
                n.get("source_type") not in PRIMARY_NEWS_TYPES:
            v.append("news item %r marked PRIMARY but source_type '%s' is "
                     "not an originator" % (str(n.get("headline"))[:40],
                                            n.get("source_type")))
    ap = snap.get("appendix") or {}
    shown, total = ap.get("rows_shown"), ap.get("rows_total")
    if shown is not None and total is not None and shown < total:
        if not ap.get("sample_label"):
            v.append("appendix shows %d of %d records without a 'sample "
                     "showing X of Y' label" % (shown, total))
        if not ap.get("machine_readable_export"):
            v.append("appendix is truncated (%d of %d) with no "
                     "machine-readable companion export" % (shown, total))
    return v


def check_alt_data_sample(snap):
    """Below the author floor, alt-data may be described but never
    scored directionally."""
    v = []
    s = snap.get("sentiment") or {}
    if not s:
        return v
    ua = s.get("unique_authors") or 0
    if ua < ALT_MIN_AUTHORS:
        if s.get("classification") != "INSUFFICIENT SAMPLE":
            v.append("alt-data has %d unique authors (<%d) and must be "
                     "classified INSUFFICIENT SAMPLE, not scored"
                     % (ua, ALT_MIN_AUTHORS))
        for k in ("sentiment_score", "divergence_score", "bull_bear_ratio"):
            if s.get(k) is not None:
                v.append("alt-data carries directional '%s' on a sample of "
                         "%d authors — descriptive only below %d"
                         % (k, ua, ALT_MIN_AUTHORS))
    return v


def check_evidence_refs(snap, ledger_ids=None):
    """Every rendered number and every decision claim must name the exact
    records it stands on, and those records must exist.

    `evidence_id` said WHICH FILING; `evidence_refs` says which fact,
    which bar, which calculation — the difference between a citation and
    a reproducible one.
    """
    v = []
    known = set(ledger_ids or snap.get("evidence_index") or [])

    def _resolve(refs):
        bad = []
        for r in refs:
            r = str(r)
            if ".." in r:
                a, b = r.split("..", 1)
                if not (a.strip() in known and b.strip() in known):
                    bad.append(r)
            elif r not in known:
                bad.append(r)
        return bad

    for path, f in _iter_facts(snap):
        if f.get("v") is None:
            continue
        if f.get("source_type") == "vendor" and not f.get("evidence_refs"):
            continue                      # descriptive vendor strings
        refs = f.get("evidence_refs") or []
        if not refs:
            v.append("%s: %s is rendered with no evidence_refs — a "
                     "published number must name the records it derives "
                     "from" % (path, f.get("metric") or "value"))
        elif known:
            bad = _resolve(refs)
            if bad:
                v.append("%s cites %d evidence ref(s) absent from the "
                         "export: %s" % (path, len(bad), ", ".join(bad[:4])))
    dec = snap.get("decision") or {}
    for c in dec.get("claims") or []:
        if not (c or {}).get("evidence_refs"):
            v.append("decision claim %r carries no evidence_refs"
                     % str((c or {}).get("text", ""))[:60])
        elif known:
            bad = _resolve(c["evidence_refs"])
            if bad:
                v.append("decision claim %r cites missing record(s): %s"
                         % (str(c.get("text", ""))[:40], ", ".join(bad[:3])))
    for key in ("business_quality", "setup_quality", "monitor_next"):
        if dec.get(key) and not dec.get(key + "_refs"):
            v.append("decision field '%s' is displayed without "
                     "evidence_refs" % key)
    return v


def check_catalyst_discovery(snap):
    """The catalyst must be the earliest VERIFIED primary disclosure.

    Reading the 10-Q as the event when an 8-K item 2.02 released the same
    results days earlier dates the event late and silently changes what
    counts as the reaction window.
    """
    v = []
    cat = snap.get("catalyst") or {}
    if not cat.get("event_dt"):
        return v
    disc = cat.get("discovery") or {}
    if not disc:
        v.append("catalyst is published without a discovery record — the "
                 "set of candidate disclosures scanned must be shown")
        return v
    if not disc.get("candidates_scanned"):
        v.append("catalyst discovery scanned no candidates")
    ep = disc.get("earliest_primary_release")
    chosen = _parse_ts(cat.get("event_dt"))
    if ep:
        epd = _parse_ts(ep)
        if epd and chosen and epd < chosen:
            v.append("catalyst dated %s but an earlier primary release "
                     "exists at %s (%s) — the company's own disclosure is "
                     "the event, not the later filing"
                     % (cat["event_dt"], ep,
                        disc.get("earliest_primary_ref") or "unref'd"))
    ver = cat.get("verification") or {}
    if cat.get("event_kind") == "primary_release":
        # Still fatal, and deliberately so: this fires only when something
        # is published AS a verified results release while its own
        # verification says otherwise. That is a lie about provenance and
        # no brief should carry it.
        if not ver.get("fetched"):
            v.append("primary release was not fetched, so it is unverified "
                     "(%s)" % (ver.get("reason") or "no reason given"))
        elif ver.get("is_results_disclosure") is False:
            v.append("document chosen as the earnings catalyst does not "
                     "read as a results disclosure: %s"
                     % (ver.get("reason") or ""))
    elif cat.get("event_kind") == "unverified_release":
        # The pipeline found a candidate, could not confirm it, and said
        # so instead of claiming it. Nothing is being misrepresented, so
        # this is not a contradiction — but the refusal has to be on the
        # record, or the demotion becomes a silent downgrade.
        if not str(cat.get("refusal") or "").strip():
            v.append("catalyst was demoted to unverified but states no "
                     "refusal — say what could not be confirmed")
    g = cat.get("grading") or {}
    if not g:
        v.append("catalyst carries no grading block")
    elif g.get("state") != "POST_EVENT_GRADED" and \
            not g.get("missing_condition"):
        v.append("catalyst is ungraded but states no missing condition — "
                 "say precisely what has not happened yet")
    return v


def check_decision_completeness(snap):
    """Fields a reader acts on may not be blank."""
    v = []
    dec = snap.get("decision") or {}
    if not dec:
        return v
    if not str(dec.get("monitor_next") or "").strip():
        v.append("'Monitor next' is empty — a report that names no next "
                 "condition cannot be acted on or reviewed")
    if not str(dec.get("review_date") or "").strip():
        v.append("no review date — 'revisit periodically' is not a date")
    for key in ("business_quality", "setup_quality"):
        if not str(dec.get(key) or "").strip():
            v.append("'%s' is not stated; business quality and setup "
                     "quality must be reported separately"
                     % key.replace("_", " "))
    return v


def check_contradictions(snap, prose_sections=None):
    """Return a list of BLOCKING violations. Empty list = publishable.

    prose_sections: {section_name: text} — narrative is checked against
    the numbers, because the July 16 failures were all prose disagreeing
    with the snapshot it was supposedly describing."""
    v = []
    prose_sections = prose_sections or {}
    lv = snap.get("levels") or {}
    px = fv((snap.get("price") or {}).get("last"))

    # 1. price/MA prose vs numeric MAs -----------------------------------
    ma = {k: fv(lv.get(k)) for k in ("ma20", "ma50", "ma200")}
    if px is not None:
        below_all = all(m is not None and px < m for m in ma.values()
                        if m is not None) and any(m is not None for m in ma.values())
        above_all = all(m is not None and px > m for m in ma.values()
                        if m is not None) and any(m is not None for m in ma.values())
        for sec, text in prose_sections.items():
            t = (text or "").lower()
            if "below every moving average" in t or \
               "below all moving averages" in t:
                if not below_all:
                    v.append("[%s] claims price below every MA, but price "
                             "%.2f vs MAs %s" % (sec, px, ma))
            if "above every moving average" in t or \
               "above all moving averages" in t:
                if not above_all:
                    v.append("[%s] claims price above every MA, but price "
                             "%.2f vs MAs %s" % (sec, px, ma))

    # 2. stated MA vs chart-drawn MA (the $360 vs $486 defect) -----------
    for k in ("ma20", "ma50", "ma200"):
        a, b = fv(lv.get(k)), fv(lv.get("chart_" + k))
        if a is not None and b is not None and a:
            if abs(a - b) / abs(a) > 0.02:
                v.append("%s disagrees between table (%.2f) and chart "
                         "(%.2f) — levels must come from one canonical "
                         "object" % (k, a, b))

    # 3. recommendation vs position plan ---------------------------------
    dec = snap.get("decision") or {}
    action = (dec.get("current_action") or "").upper()
    plan = dec.get("position_plan") or {}
    if action in NON_LONG_ACTIONS and any(
            plan.get(k) is not None for k in ("entry", "stop", "target")):
        v.append("action %s is paired with an active long plan "
                 "(entry/stop/target) — remove the plan or change the "
                 "action" % action)
    if action and action not in ACTIONS:
        v.append("unknown action '%s' (allowed: %s)" % (action, ", ".join(ACTIONS)))

    # 3b. trigger direction sanity (the "$390" reversal) -----------------
    up_trig = dec.get("upgrade_trigger") or ""
    down_conf = dec.get("downside_confirmation") or ""
    if action in ("AVOID", "REDUCE", "WAIT"):
        if re.search(r"thesis (breaks|fails|invalidated) below", str(up_trig),
                     re.I):
            v.append("upgrade_trigger describes a BREAK DOWN while action "
                     "is %s — a break lower confirms the bearish thesis, "
                     "it does not upgrade it" % action)
        if up_trig and re.search(r"\bbelow\b", up_trig, re.I) and \
                not re.search(r"reclaim|back above|recover", up_trig, re.I):
            v.append("upgrade_trigger for %s should describe a RECLAIM "
                     "(price back above a level), not a move below" % action)
        if down_conf and re.search(r"\babove\b", down_conf, re.I) and \
                not re.search(r"fails at|rejected at|below", down_conf, re.I):
            v.append("downside_confirmation should describe a breakdown, "
                     "not a move above")

    # 4. catalyst state vs prose ----------------------------------------
    cat = snap.get("catalyst") or {}
    state = cat.get("state")
    if state and state not in CATALYST_STATES:
        v.append("unknown catalyst state '%s'" % state)
    if state:
        for sec, text in prose_sections.items():
            t = (text or "").lower()
            for phrase in STATE_FORBIDDEN_PHRASES.get(state, []):
                if phrase in t:
                    v.append("[%s] uses post/pre-event language '%s' while "
                             "catalyst state is %s" % (sec, phrase, state))
    # cross-section catalyst timing agreement
    times = cat.get("stated_times") or {}
    uniq = set(str(x) for x in times.values() if x)
    if len(uniq) > 1:
        v.append("catalyst timing disagrees across sections: %s" % times)

    # 5. company template contamination ---------------------------------
    co = snap.get("company") or {}
    cap = fv(co.get("market_cap"))
    universe = (co.get("universe") or "").upper()
    profitable = fv(co.get("profitable"))
    if cap is not None:
        implied = ("MEGA" if cap >= 200e9 else "LARGE" if cap >= 10e9
                   else "MID" if cap >= 2e9 else "SMALL")
        if universe and universe not in ("", implied) and not (
                universe == "SMID" and implied in ("MID", "SMALL")):
            v.append("universe label '%s' contradicts market cap $%.1fB "
                     "(implies %s)" % (universe, cap / 1e9, implied))
        for sec, text in prose_sections.items():
            t = (text or "").lower()
            if cap >= 10e9 and re.search(r"\bnormal for smid\b|\bsmid[- ]cap\b", t):
                v.append("[%s] applies SMID framing to a $%.1fB company"
                         % (sec, cap / 1e9))
    if profitable is True:
        for sec, text in prose_sections.items():
            if re.search(r"unprofitable growth", str(text or ""), re.I):
                v.append("[%s] compares a profitable company to "
                         "'unprofitable growth companies'" % sec)

    # 6. level coherence: support/stop/target vs price -------------------
    sup, res = fv(lv.get("support")), fv(lv.get("resistance"))
    if sup is not None and res is not None and sup >= res:
        v.append("support %.2f >= resistance %.2f" % (sup, res))
    if px is not None and plan:
        e, s, tg = plan.get("entry"), plan.get("stop"), plan.get("target")
        if e is not None and s is not None and s >= e:
            v.append("stop %.2f is not below entry %.2f" % (s, e))
        if e is not None and tg is not None and tg <= e:
            v.append("target %.2f is not above entry %.2f" % (tg, e))

    # 7. valuation multiples must state a basis --------------------------
    val = snap.get("valuation") or {}
    seen = {}
    for name, f in val.items():
        if not isinstance(f, dict):
            continue
        if fv(f) is None:
            continue
        if not f.get("basis"):
            v.append("valuation '%s' has no basis (forward/trailing/ntm) "
                     "— unlabeled multiples caused the 33.8x vs 50x "
                     "contradiction" % name)
        else:
            key = (name.split("_")[0], f.get("basis"))
            if key in seen and seen[key] != fv(f):
                v.append("valuation %s on %s basis stated twice with "
                         "different values (%s vs %s)"
                         % (key[0], key[1], seen[key], fv(f)))
            seen[key] = fv(f)

    # 8. confidence coherence -------------------------------------------
    ev = snap.get("evidence") or {}
    if ev.get("model_confidence_score") is not None:
        v.append("uncalibrated model_confidence_score present — v2 "
                 "reports evidence quality, data completeness and "
                 "historically calibrated confidence separately")
    conv = str(ev.get("conviction") or "").lower()
    qual = str(ev.get("evidence_quality") or "").lower()
    RANK = {"low": 0, "limited": 0, "medium": 1, "moderate": 1,
            "high": 2, "strong": 2}
    if conv in RANK and qual in RANK and abs(RANK[conv] - RANK[qual]) > 1:
        v.append("conviction '%s' and evidence quality '%s' disagree"
                 % (conv, qual))

    # 9. alt-data inference discipline ----------------------------------
    sent = snap.get("sentiment") or {}
    if sent:
        verified = "options feed" in str(sent.get("flow_language") or "")
        for sec, text in prose_sections.items():
            t = (text or "").lower()
            if re.search(r"institutional (call |put )?flow|heavy institutional",
                         t) and not verified:
                v.append("[%s] infers institutional flow from social data "
                         "(%d relevant posts, %d authors) without an "
                         "options-feed verification"
                         % (sec, sent.get("n_relevant") or 0,
                            sent.get("unique_authors") or 0))
            if "downgrade" in t and sent.get("analyst_actions"):
                kinds = set(sent["analyst_actions"])
                if "rating_change" not in kinds:
                    v.append("[%s] says 'downgrade' but only price-target "
                             "changes occurred (rating maintained)" % sec)

    # 10. insider language discipline -----------------------------------
    ins = snap.get("insiders") or {}
    if ins and ins.get("n_view_bearing") == 0:
        for sec, text in prose_sections.items():
            if re.search(r"red flag|heavy selling|dumping|insiders? (are )?selling",
                         str(text or ""), re.I):
                v.append("[%s] characterises insider activity as selling "
                         "pressure, but 0 of %d transactions are "
                         "open-market sales (rest are vesting/"
                         "withholding/exercises)"
                         % (sec, ins.get("n_total") or 0))

    # 11. ownership completeness ----------------------------------------
    own = snap.get("ownership") or {}
    if own.get("n_filings"):
        for sec, text in prose_sections.items():
            if re.search(r"no 13[dg]|no schedule 13", str(text or ""), re.I):
                v.append("[%s] claims no 13D/13G filings while %d are in "
                         "the snapshot" % (sec, own["n_filings"]))
    for sec, text in prose_sections.items():
        if re.search(r"13G.{0,30}(lower|less) conviction", str(text or ""), re.I):
            v.append("[%s] treats 13G as 'lower conviction' — it denotes "
                     "passive/non-control filing status" % sec)

    # 12-15. provenance, availability, arithmetic, evidence, sample floor
    v += check_point_in_time(snap)
    v += check_market_data_coherence(snap)
    v += check_evidence_links(snap)
    v += check_alt_data_sample(snap)
    v += check_alt_data_integrity(snap)
    v += check_evidence_refs(snap)
    v += check_catalyst_discovery(snap)
    v += check_decision_completeness(snap)

    return v


class DemoExportBlocked(Exception):
    """Raised when synthetic data is asked to leave as a real report."""


def assert_exportable(snap, allow_demo=False):
    """Hard gate between prototypes and research output.

    A snapshot in demo mode, or containing ANY value marked Q_DEMO,
    cannot be exported as a normal research report. Renderers must call
    this; the only way past it is an explicit demo export, which is
    watermarked and named so it can never be mistaken for research."""
    if is_demo(snap) and not allow_demo:
        paths = demo_fact_paths(snap)
        raise DemoExportBlocked(
            "SYNTHETIC DATA — export as a research report is blocked.\n"
            "  mode: %s\n  %d demo value(s): %s\n"
            "  Use build_demo(...) for a watermarked prototype, or supply "
            "sourced facts." % (snap.get("mode"), len(paths),
                                ", ".join(paths[:8]) or "(mode flag only)"))
    return True


def gate(snap, prose_sections=None, raise_on_fail=True):
    """Publication gate. v2 renderers MUST call this and refuse to emit
    a PDF when it returns violations."""
    vs = check_contradictions(snap, prose_sections)
    if vs and raise_on_fail:
        raise Contradiction("Blocked — %d contradiction(s):\n  - %s"
                            % (len(vs), "\n  - ".join(vs)))
    return vs


# ── ISRG regression fixture (2026-07-16) ────────────────────────────────

def isrg_july16_fixture():
    """Reconstructs the state that produced the July 16 ISRG brief.
    Every one of the ten findings must be caught by the gate."""
    ev = datetime(2026, 7, 16, 20, 5, tzinfo=timezone.utc)   # 16:05 ET release
    now = datetime(2026, 7, 16, 17, 49, tzinfo=timezone.utc)  # 13:49 ET report
    snap = new_snapshot("ISRG", now.isoformat(), now.isoformat())
    snap["company"] = {
        "name": fact("Intuitive Surgical", "yfinance", "2026-07-16"),
        "market_cap": fact(178e9, "yfinance", "2026-07-16"),
        "universe": "SMID",                       # contamination
        "profitable": fact(True, "10-Q", "2026-07-16"),
        "sector": fact("Health Care", "yfinance", "2026-07-16"),
    }
    snap["price"] = {"last": fact(445.0, "yfinance", "2026-07-16T17:45Z")}
    snap["levels"] = {
        "ma20": fact(470.0, "calc", "2026-07-16"),
        "ma50": fact(478.0, "calc", "2026-07-16"),
        "ma200": fact(360.0, "calc", "2026-07-16"),     # stated
        "chart_ma200": fact(486.0, "chart", "2026-07-16"),  # drawn
        "support": fact(390.0, "calc", "2026-07-16"),
        "resistance": fact(505.0, "calc", "2026-07-16"),
    }
    snap["catalyst"] = {
        "event_dt": ev.isoformat(),
        "state": resolve_catalyst_state(ev, now),          # PRE_EVENT
        "stated_times": {"alt_data": "2026-07-16 after close",
                         "ticker_report": "2026-07-16 reported today"},
    }
    snap["valuation"] = {
        "pe_forward": fact(33.8, "yfinance", "2026-07-16", basis="forward"),
        "pe_alt": fact(50.0, "alt_data", "2026-07-16"),     # no basis
    }
    snap["decision"] = {
        "current_action": "AVOID",
        "position_plan": {"entry": 452.0, "stop": 428.0, "target": 505.0},
        "upgrade_trigger": "thesis breaks below $390",
        "downside_confirmation": "",
    }
    snap["evidence"] = {"model_confidence_score": 85, "conviction": "low",
                        "evidence_quality": "high"}
    snap["insiders"] = summarize_insiders([
        {"code": "F", "shares": 1200, "price": 470.0, "value": 564000,
         "date": "2026-07-10", "owner": "CEO", "title": "CEO",
         "shares_owned_after": 48000},
        {"code": "M", "shares": 5000, "price": 0.0, "value": 0,
         "date": "2026-07-10", "owner": "CEO", "title": "CEO",
         "shares_owned_after": 53000},
        {"code": "A", "shares": 900, "price": 0.0, "value": 0,
         "date": "2026-07-01", "owner": "CFO", "title": "CFO"},
    ])
    snap["ownership"] = summarize_ownership([
        {"form": "SC 13G/A", "filer": "Vanguard Group",
         "filed": "2026-03-27"},
    ], institutional_pct=88.0)
    snap["sentiment"] = score_alt_data(
        posts=[{"text": "ISRG calls printing", "author": "a", "source": "stocktwits"}] * 3
              + [{"text": "NRED squeeze incoming", "author": "b", "source": "reddit"},
                 {"text": "VYNE to the moon", "author": "c", "source": "reddit"},
                 {"text": "earnings calendar this week", "author": "d",
                  "source": "reddit"}],
        ticker="ISRG", options_feed_verified=False)
    snap["sentiment"]["analyst_actions"] = [
        "price_target_cut_rating_maintained"]
    prose = {
        "page1": ("ISRG is a mega-cap medtech leader. The stock is below "
                  "every moving average after the post-catalyst move; "
                  "earnings reported today. Heavy institutional call flow "
                  "supports a bounce. Recent analyst downgrades weigh."),
        "page2": ("Valuation is normal for SMID peers and cheap versus "
                  "unprofitable growth companies. Insider selling is a "
                  "glaring red flag. There were no 13D/13G filings in the "
                  "last 12 months, and 13G filings indicate lower "
                  "conviction anyway."),
    }
    return snap, prose


def isrg_v2_prototype_fixture():
    """The v2 VISUAL PROTOTYPE exactly as rendered on 2026-07-21.

    It looked clean, which is the danger. This fixture pins every way it
    was not publishable so no future change can quietly re-admit them:
      A. Q2 figures dated period_end 2026-06-30 but published 20:05 UTC,
         quoted in a report written at 17:49 UTC (future information)
      B. $445 price with a $178B market cap — ~357M shares implies ~$159B
      C. Form 4 identity not verified against the filing
      D. appendix asserting completeness while records are missing
      E. first vs major resistance conflicting/unexplained
      F. synthetic values throughout -> normal export must be impossible
    """
    now = "2026-07-16T17:49:00+00:00"
    snap = new_snapshot("ISRG", now, "2026-07-16T17:45:00+00:00")
    snap["mode"] = DEMO
    snap["company"] = {
        "name": demo_fact("Intuitive Surgical", metric="company name"),
        # B: arithmetic that does not reconcile
        "market_cap": demo_fact(178e9, metric="market cap",
                                market_asof="2026-07-16T17:45:00+00:00"),
        "shares_outstanding": demo_fact(357e6, metric="shares outstanding"),
        "profitable": demo_fact(True, metric="profitable"),
        "universe": "LARGE",
    }
    snap["price"] = {"last": demo_fact(445.0, metric="last price",
                                       market_asof="2026-07-16T17:45:00+00:00")}
    snap["levels"] = {
        # A canonical series is claimed but the technical price disagrees
        "price_used": demo_fact(447.5, metric="price used for levels",
                                series_id="yf-1d-A"),
        "ma20": demo_fact(470.0, metric="20d MA", series_id="yf-1d-A"),
        "ma50": demo_fact(478.0, metric="50d MA", series_id="yf-1d-B"),
        "ma200": demo_fact(486.0, metric="200d MA", series_id="yf-1d-A"),
        "support": demo_fact(390.0, metric="support", series_id="yf-1d-A"),
        # E: two resistances, unexplained, and inverted
        "resistance": demo_fact(505.0, metric="first resistance",
                                series_id="yf-1d-A"),
        "resistance_major": demo_fact(486.0, metric="major resistance",
                                      series_id="yf-1d-A"),
    }
    # A: Q2 results published AFTER the report was written
    snap["fundamentals"] = {
        "revenue_growth": demo_fact(
            21.0, metric="revenue growth y/y", source_type="filing",
            source_url="https://investor.intuitive.com/q2",
            period_end="2026-06-30", published_at="2026-07-16T20:05:00+00:00",
            basis="gaap"),
        "installed_base": demo_fact(
            "11,710", metric="da Vinci installed base",
            source_type="company_release", period_end="2026-06-30",
            published_at="2026-07-16T20:05:00+00:00"),
    }
    snap["valuation"] = {
        "pe_forward": demo_fact(33.8, metric="P/E", basis="forward"),
    }
    snap["catalyst"] = {
        "event_dt": "2026-07-16T20:05:00+00:00",
        "state": PRE_EVENT,
        "stated_times": {"brief": "2026-07-16T20:05Z"},
    }
    # C: Form 4 rows not reconciled to the filing
    snap["insiders"] = summarize_insiders([
        {"code": "F", "shares": 1200, "owner": "Gary Guthart",
         "title": "CEO", "shares_owned_after": 48000,
         "identity_verified": False},
    ])
    snap["ownership"] = summarize_ownership(
        [{"form": "SC 13G/A", "filer": "Vanguard Group"}],
        institutional_pct=88.0)
    snap["sentiment"] = score_alt_data(
        [{"text": "$ISRG calls", "author": "a"}], "ISRG")
    # alt-data scored despite a 1-author sample
    snap["sentiment"]["sentiment_score"] = 0.7
    snap["decision"] = {
        "current_action": "WAIT",
        "position_plan": {},
        "upgrade_trigger": "reclaim of $486 on volume",
        "downside_confirmation": "loss of $390 support",
        # every claim should cite an appendix record; these do not
        "claims": [{"text": "Procedure growth 17% y/y"},
                   {"text": "Recurring revenue 84% of mix",
                    "evidence_id": "EV-NOT-IN-APPENDIX"}],
    }
    # D: completeness asserted while records are missing
    snap["appendix"] = {"evidence_ids": [], "claims_complete": True}
    snap["evidence"] = {"conviction": "low", "evidence_quality": "limited"}
    return snap


def self_test():
    fails = []

    ran = []

    def chk(name, cond):
        ran.append(name)
        print(("  PASS  " if cond else "  FAIL  ") + name)
        if not cond:
            fails.append(name)

    # --- catalyst state machine ---
    ev = datetime(2026, 7, 16, 20, 5, tzinfo=timezone.utc)
    chk("state: 2h before release -> PRE_EVENT",
        resolve_catalyst_state(ev, ev - timedelta(hours=2)) == PRE_EVENT)
    chk("state: 10m after -> EVENT_IN_PROGRESS",
        resolve_catalyst_state(ev, ev + timedelta(minutes=10)) == EVENT_IN_PROGRESS)
    chk("state: 3h after -> POST_EVENT_UNGRADED",
        resolve_catalyst_state(ev, ev + timedelta(hours=3)) == POST_EVENT_UNGRADED)
    chk("state: next session -> POST_EVENT_GRADED",
        resolve_catalyst_state(ev, ev + timedelta(hours=25)) == POST_EVENT_GRADED)

    # --- insider classification (finding 6) ---
    f = classify_insider({"code": "F", "shares": 1200, "shares_owned_after": 48000})
    chk("insider: code F = tax withholding, carries no view",
        f["class"] == TAX_WITHHOLDING and not f["carries_view"])
    m = classify_insider({"code": "M", "shares": 5000})
    chk("insider: code M = option exercise, carries no view",
        m["class"] == OPTION_EXERCISE and not m["carries_view"])
    s = classify_insider({"code": "S", "shares": 1000, "shares_owned_after": 9000})
    chk("insider: code S = open-market sale, 10% of holdings",
        s["carries_view"] and s["pct_of_holdings"] == 10.0)
    p = classify_insider({"code": "S", "shares": 100,
                          "footnotes": ["Sale under a Rule 10b5-1 plan"]})
    chk("insider: 10b5-1 sale separated from discretionary",
        p["class"] == PLANNED_SALE and not p["carries_view"])

    # --- alt-data relevance (findings 8, 9) ---
    sc = score_alt_data(
        [{"text": "$ISRG calls", "author": "a"},
         {"text": "$ISRG calls", "author": "a"},          # self-repeat
         {"text": "NRED to the moon", "author": "b"},
         {"text": "earnings calendar", "author": "c"}], "ISRG")
    chk("alt-data: irrelevant + self-repeat posts dropped",
        sc["n_relevant"] == 1 and sc["n_dropped_irrelevant"] == 3)
    # same wording from DIFFERENT accounts must be kept and measured,
    # not silently collapsed — that is the coordination signal
    echo = score_alt_data([{"text": "$ISRG to the moon", "author": u}
                           for u in "abcdefghijklmnopqrstuvwxy"], "ISRG")
    chk("alt-data: cross-author echo kept, not dropped",
        echo["n_relevant"] == 25 and echo["unique_authors"] == 25)
    chk("alt-data: coordination quantified on echo",
        echo["echoed_phrases"] == 1 and "3+ accounts" in echo["coordination"])
    chk("alt-data: never claims institutional from social",
        "retail call discussion" in sc["flow_language"])
    chk("alt-data: tiny sample says 'too small', never 'clean'",
        "too small to assess" in sc["coordination"])
    # adequate sample, no echo -> the honest non-verdict (never "CLEAN")
    varied = score_alt_data(
        [{"text": "$ISRG note number %d" % i, "author": "u%d" % i}
         for i in range(30)], "ISRG")
    chk("alt-data: CLEAN replaced with limited-sample wording",
        "no coordination detected in this limited sample"
        == varied["coordination"])
    chk("analyst: PT cut with rating held is not a downgrade",
        classify_analyst_action("Buy", "Buy", 600, 520)
        == "price_target_cut_rating_maintained")

    # --- ownership (finding 7) ---
    ow = summarize_ownership([{"form": "SC 13G/A", "filer": "Vanguard"}])
    chk("ownership: 13G/A counted as a filing", ow["n_filings"] == 1)
    chk("ownership: passive-status note present",
        "not a statement about conviction" in ow["note"])

    # --- the full ISRG regression: gate must catch all ten ---
    snap, prose = isrg_july16_fixture()
    vs = check_contradictions(snap, prose)
    blob = " || ".join(vs).lower()
    print("\n  ISRG 2026-07-16 fixture -> %d contradictions:" % len(vs))
    for x in vs:
        print("      * " + x)
    print("")
    chk("F1 catalyst: pre-event prose caught",
        "post-catalyst" in blob or "reported today" in blob)
    chk("F1b catalyst: cross-section timing disagreement caught",
        "catalyst timing disagrees" in blob)
    chk("F2 levels: stated MA vs chart MA caught", "chart" in blob and "ma200" in blob)
    chk("F2b levels: 'below every MA' vs numbers caught",
        "below every ma" in blob)
    chk("F3 decision: AVOID + long plan caught", "active long plan" in blob)
    chk("F3b decision: reversed upgrade trigger caught",
        "confirms the bearish thesis" in blob)
    chk("F4 confidence: uncalibrated score caught",
        "uncalibrated model_confidence_score" in blob)
    chk("F5 template: SMID framing on large cap caught",
        "smid" in blob)
    chk("F5b template: 'unprofitable growth' vs profitable caught",
        "unprofitable growth" in blob)
    chk("F6 insider: 'red flag' with 0 open-market sales caught",
        "open-market sales" in blob)
    chk("F7 ownership: 'no 13D/13G' vs filed 13G/A caught",
        "claims no 13d/13g" in blob)
    chk("F7b ownership: 13G-as-lower-conviction caught",
        "passive/non-control" in blob)
    chk("F8 alt-data: institutional inferred from social caught",
        "infers institutional flow" in blob)
    chk("F8b alt-data: PT cut called downgrade caught",
        "only price-target changes" in blob)
    chk("F10 valuation: unlabeled multiple caught", "no basis" in blob)
    chk("GATE: fixture is unpublishable",
        len(gate(snap, prose, raise_on_fail=False)) > 0)

    # --- a corrected snapshot must PASS ---
    good, _ = isrg_july16_fixture()
    # the new provenance/arithmetic gate applies here too: supply the
    # share count so market cap can be verified, and drop the directional
    # score on a 1-author alt-data sample.
    good["company"]["shares_outstanding"] = fact(357e6, "10-Q",
                                                 "2026-06-30")
    good["company"]["market_cap"] = fact(445.0 * 357e6, "derived",
                                         "2026-07-16", quality=Q_DERIVED)
    good["sentiment"]["classification"] = "INSUFFICIENT SAMPLE"
    good["company"]["universe"] = "LARGE"
    good["levels"]["ma200"] = fact(486.0, "calc", "2026-07-16")
    good["decision"] = {
        "current_action": "AVOID",
        "action_display": "AVOID NEW SWING LONGS",
        "position_plan": {},
        "upgrade_trigger": "reclaim of $486 (200-day) on above-average volume",
        "downside_confirmation": "loss of $390 support on expanding volume",
        # fields a reader acts on may not be blank
        "business_quality": "solid",
        "business_quality_basis": "GAAP margin and filed growth",
        "business_quality_refs": ["REC-business_quality"],
        "setup_quality": "damaged",
        "setup_quality_basis": "price below all three moving averages",
        "setup_quality_refs": ["CALC-ma200"],
        "monitor_next": "reclaim of $486, or a close below $390",
        "monitor_next_refs": ["CALC-ma200"],
        "review_date": "2026-07-23",
    }
    good["catalyst"]["discovery"] = {
        "candidates_scanned": 3,
        "earliest_primary_release": "2026-07-16T20:05:00+00:00",
        "earliest_primary_ref": "CAT-8K-2202",
    }
    good["catalyst"]["event_dt"] = "2026-07-16T20:05:00+00:00"
    good["catalyst"]["event_kind"] = "primary_release"
    good["catalyst"]["verification"] = {"fetched": True,
                                        "is_results_disclosure": True}
    good["catalyst"]["grading"] = {
        "state": POST_EVENT_GRADED, "reaction_pct": -14.15,
        "missing_condition": None}
    good["valuation"] = {
        "pe_forward": fact(33.8, "yfinance", "2026-07-16", basis="forward"),
        "pe_trailing": fact(50.0, "yfinance", "2026-07-16", basis="trailing"),
    }
    good["evidence"] = {"conviction": "low", "evidence_quality": "limited",
                        "data_completeness": 0.8}
    good["catalyst"]["stated_times"] = {
        "alt_data": "2026-07-16T20:05Z", "ticker_report": "2026-07-16T20:05Z"}
    # stamp every fact with a resolvable ref so this fixture exercises the
    # OTHER gates; the ref gate has its own dedicated cases below. This
    # runs LAST so sections assigned above are all covered.
    _idx = ["REC-business_quality", "CALC-ma200", "CAT-8K-2202"]
    for _p, _f in _iter_facts(good):
        if _f.get("v") is not None and not _f.get("evidence_refs"):
            _rid = "FIX-" + _p.replace(".", "-")
            _f["evidence_refs"] = [_rid]
            _idx.append(_rid)
    for _c in good["decision"].get("claims") or []:
        _c["evidence_refs"] = ["CALC-ma200"]
    good["evidence_index"] = _idx

    clean_prose = {
        "page1": ("Intuitive Surgical is a large-cap medtech leader. Price "
                  "sits below the 20- and 50-day averages ahead of "
                  "earnings, scheduled for 2026-07-16 after the close. "
                  "Retail users discussed bullish call positions."),
        "page2": ("Insider activity in the window is compensation "
                  "mechanics — vesting and withholding — with no "
                  "open-market sales. Vanguard filed a 13G/A in March."),
    }
    gv = check_contradictions(good, clean_prose)
    if gv:
        print("  corrected snapshot residual violations:")
        for x in gv:
            print("      ! " + x)
    chk("CORRECTED snapshot publishes cleanly", len(gv) == 0)

    # --- reproducibility: evidence_refs must exist and must resolve ---
    import copy
    r1 = copy.deepcopy(good)
    r1["levels"]["ma200"]["evidence_refs"] = []
    chk("R-A rendered number without evidence_refs is blocked",
        any("no evidence_refs" in x for x in check_evidence_refs(r1)))
    r2 = copy.deepcopy(good)
    r2["levels"]["ma200"]["evidence_refs"] = ["CALC-does-not-exist"]
    chk("R-B evidence_ref absent from the export is blocked",
        any("absent from the export" in x for x in check_evidence_refs(r2)))
    r3 = copy.deepcopy(good)
    r3["decision"]["claims"] = [{"text": "Procedure growth 17%",
                                 "evidence_id": "X"}]
    chk("R-C decision claim without evidence_refs is blocked",
        any("no evidence_refs" in x for x in check_evidence_refs(r3)))
    r4 = copy.deepcopy(good)
    r4["decision"]["monitor_next_refs"] = []
    chk("R-D displayed decision field without refs is blocked",
        any("without evidence_refs" in x for x in check_evidence_refs(r4)))

    # --- catalyst discovery: the earliest primary release wins ---
    c1 = copy.deepcopy(good)
    c1["catalyst"]["event_dt"] = "2026-07-21T21:25:51+00:00"
    chk("C-A later periodic filing chosen over earlier release is blocked",
        any("earlier primary release exists" in x
            for x in check_catalyst_discovery(c1)))
    c2 = copy.deepcopy(good)
    c2["catalyst"].pop("discovery")
    chk("C-B catalyst without a discovery record is blocked",
        any("without a discovery record" in x
            for x in check_catalyst_discovery(c2)))
    c3 = copy.deepcopy(good)
    c3["catalyst"]["verification"] = {"fetched": False,
                                      "reason": "HTTP 404"}
    chk("C-C unverified primary release is blocked",
        any("unverified" in x for x in check_catalyst_discovery(c3)))
    c4 = copy.deepcopy(good)
    c4["catalyst"]["grading"] = {"state": "POST_EVENT_UNGRADED",
                                 "missing_condition": None}
    chk("C-D ungraded event with no stated missing condition is blocked",
        any("states no missing condition" in x
            for x in check_catalyst_discovery(c4)))
    c5 = copy.deepcopy(good)
    c5["catalyst"]["grading"] = {
        "state": "POST_EVENT_UNGRADED",
        "missing_condition": "no full session has closed after the release"}
    chk("C-E ungraded WITH a precise missing condition is allowed",
        not check_catalyst_discovery(c5))

    # --- decision completeness ---
    d1 = copy.deepcopy(good)
    d1["decision"]["monitor_next"] = ""
    chk("D-A empty 'Monitor next' is blocked",
        any("Monitor next' is empty" in x
            for x in check_decision_completeness(d1)))
    d2 = copy.deepcopy(good)
    d2["decision"]["review_date"] = ""
    chk("D-B missing review date is blocked",
        any("no review date" in x for x in check_decision_completeness(d2)))
    d3 = copy.deepcopy(good)
    d3["decision"]["business_quality"] = ""
    chk("D-C business quality not stated separately is blocked",
        any("business quality" in x
            for x in check_decision_completeness(d3)))

    # --- alt-data accounting, as named assertions ---
    a1 = copy.deepcopy(good)
    a1["sentiment"] = {"n_considered": 30, "n_relevant": 24,
                       "n_rejected": 3}
    chk("A-A sample accounting that does not balance is blocked",
        any("accounting fails" in x for x in check_alt_data_integrity(a1)))
    a2 = copy.deepcopy(good)
    a2["sentiment"] = {"n_considered": 30, "n_relevant": 24, "n_rejected": 6,
                       "post_weighted_bull_pct": 100,
                       "unique_authors": 20, "classification": "SCORED",
                       "by_class": {"bullish": 6, "bearish": 0,
                                    "neutral": 15, "uncertain": 3},
                       "decision_read": {"direction": "100% bullish"}}
    chk("A-B bare directional percentage without its base is blocked",
        any("without stating that it covers" in x or
            "without the number of directional posts" in x
            for x in check_alt_data_integrity(a2)))

    # ── v2 prototype regression: every blocking condition ─────────────
    proto = isrg_v2_prototype_fixture()
    pv = check_contradictions(proto)
    pblob = " || ".join(pv).lower()
    print("  v2 prototype fixture -> %d contradictions:" % len(pv))
    for x in pv:
        print("      * " + x)
    print("")
    chk("P-A future-published Q2 data blocked", "after report_time" in pblob)
    chk("P-B $445/$178B arithmetic inconsistency blocked",
        "disagrees with price" in pblob)
    chk("P-C conflicting/unexplained resistance blocked", "resistance" in pblob)
    chk("P-D incomplete appendix completeness claim blocked",
        "claims completeness" in pblob)
    chk("P-E decision claim without evidence_id blocked",
        "no evidence_id" in pblob)
    chk("P-F alt-data scored below author floor blocked",
        "insufficient sample" in pblob or "directional" in pblob)
    chk("P-G mixed price series blocked",
        "one point-in-time series" in pblob or "derives from series" in pblob)
    blocked = False
    try:
        assert_exportable(proto)
    except DemoExportBlocked:
        blocked = True
    chk("P-H demo fixture cannot export as a normal report", blocked)
    chk("P-I demo export allowed only when explicitly requested",
        assert_exportable(proto, allow_demo=True) is True)
    chk("P-J demo values individually traceable",
        len(demo_fact_paths(proto)) >= 8)
    mixed = new_snapshot("TEST", "2026-07-16T17:49:00+00:00", "x")
    mixed["mode"] = PROD
    mixed["price"] = {"last": demo_fact(100.0, metric="price")}
    chk("P-K one demo fact poisons a production snapshot", is_demo(mixed))
    pit = new_snapshot("T", "2026-07-16T17:49:00+00:00", "x")
    pit["fundamentals"] = {"rev": fact(1.0, metric="rev",
        source_type="filing", period_end="2026-06-30",
        published_at="2026-07-16T20:05:00+00:00")}
    chk("P-L quarter-end alone does not admit a figure",
        any("after report_time" in x.lower()
            for x in check_point_in_time(pit)))
    pit2 = new_snapshot("T", "2026-07-16T17:49:00+00:00", "x")
    pit2["fundamentals"] = {"rev": fact(1.0, metric="rev",
        source_type="filing", period_end="2026-03-31",
        published_at="2026-04-18T20:05:00+00:00")}
    chk("P-M prior-quarter figure published pre-report is admitted",
        not check_point_in_time(pit2))


    # count what actually ran: a hardcoded total reported "42/42" no
    # matter how many checks existed, so new checks were invisible
    total = len(ran)
    print("\n%d/%d checks passed" % (total - len(fails), total))
    return 0 if not fails else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        sys.exit(self_test())
    print("research_snapshot v2 — import me, or run --self-test")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
