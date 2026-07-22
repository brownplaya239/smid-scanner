#!/usr/bin/env python3
"""report_v3_model.py — the semantics behind Stock Research Brief v3.

This module holds every decision the v3 brief makes about *meaning*. It
imports no rendering library on purpose: the ordering of a trigger
ladder, the difference between a risk boundary and an invalidation, and
whether a catalyst is still the current driver are data questions, and
data questions should be testable without building a PDF.

Three rules run through all of it.

1. Every conclusion carries an evidence grade. OBSERVED means a source
   reported it. DERIVED means we computed it from observed facts and the
   formula is in the ledger. INFERRED means a human reading — those are
   never dressed up as measurements.
2. Missing data is missing, never bearish. A company that did not file a
   gross-margin tag has not reported a bad gross margin.
3. Levels are sorted, never assumed. The 20-day average is below the
   50-day in a downtrend and above it in an uptrend, so a ladder that
   hardcodes an order is wrong half the time.
"""

import datetime as dt
import json
import os
import re

import research_snapshot as rs

# ── evidence grades ─────────────────────────────────────────────────────

OBSERVED = "OBSERVED"
DERIVED = "DERIVED"
INFERRED = "INFERRED"

GRADE_NOTE = {
    OBSERVED: "reported by the cited source",
    DERIVED: "computed here from observed facts; formula in the appendix",
    INFERRED: "our reading of the evidence, not a measurement",
}

STATE_DIR = os.environ.get("TD_REPORT_STATE", ".report_state")


def grade(f):
    """Grade a Fact. A dict with a calc_version was computed by us; a
    dict with a source was reported to us; anything else is a reading."""
    if not isinstance(f, dict):
        return INFERRED
    if f.get("calc_version") or f.get("quality") == rs.Q_DERIVED:
        return DERIVED
    if f.get("src") or f.get("source_url"):
        return OBSERVED
    return INFERRED


def fact_row(d, key, label, fmt="%s"):
    """One display row for a Fact: label, formatted value, grade, as-of.
    Returns None when the fact is absent so callers can omit the row
    rather than print a placeholder that reads like a zero."""
    f = (d or {}).get(key)
    v = rs.fv(f)
    if v is None:
        return None
    try:
        txt = fmt % v
    except Exception:
        txt = str(v)
    return {"label": label, "value": txt, "grade": grade(f),
            "as_of": (f.get("as_of") if isinstance(f, dict) else None),
            "src": (f.get("src") if isinstance(f, dict) else None),
            "refs": (f.get("evidence_refs") or []) if isinstance(f, dict) else []}


# ── time ────────────────────────────────────────────────────────────────

ET = "America/New_York"

try:
    from zoneinfo import ZoneInfo
except Exception:                                    # pragma: no cover
    ZoneInfo = None


def to_et(ts):
    """Users read Eastern. Metadata keeps UTC. A naive timestamp is a bug
    we surface rather than guess about."""
    if not ts:
        return None, None
    s = str(ts).strip().replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return str(ts), None
    if d.tzinfo is None:
        return str(ts), "timestamp carries no zone; shown as published"
    if ZoneInfo is None:                             # pragma: no cover
        return d.isoformat(), "zoneinfo unavailable"
    e = d.astimezone(ZoneInfo(ET))
    return e.strftime("%Y-%m-%d %H:%M ET"), None


def utc_iso(ts):
    if not ts:
        return None
    s = str(ts).strip().replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return str(ts)
    if d.tzinfo is None:
        return s
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def spot(snap):
    """The last price, wherever this snapshot happens to keep it. The
    live builder writes `price.last` and `levels.price_used`; older
    fixtures write neither. Reading only one of them is how v2's page 1
    ended up with an empty price cell on real data."""
    lv, pr = snap.get("levels") or {}, snap.get("price") or {}
    for src, key in ((lv, "price_used"), (lv, "price"), (pr, "last")):
        v = rs.fv(src.get(key))
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _hours_between(a, b):
    """Signed hours from a to b. Days are too coarse near a close: an
    8-K accepted at 20:05Z and a report stamped 17:49Z are two hours
    apart, and rounding that to '-1 days' hid the fact that the filing
    had not happened yet."""
    try:
        da = dt.datetime.fromisoformat(str(a).replace("Z", "+00:00"))
        db = dt.datetime.fromisoformat(str(b).replace("Z", "+00:00"))
    except Exception:
        return None
    if da.tzinfo is None:
        da = da.replace(tzinfo=dt.timezone.utc)
    if db.tzinfo is None:
        db = db.replace(tzinfo=dt.timezone.utc)
    return (db - da).total_seconds() / 3600.0


def _days_between(a, b):
    for parse in (lambda x: dt.datetime.fromisoformat(
            str(x).replace("Z", "+00:00")),):
        try:
            da, db = parse(a), parse(b)
        except Exception:
            return None
        if da.tzinfo is None:
            da = da.replace(tzinfo=dt.timezone.utc)
        if db.tzinfo is None:
            db = db.replace(tzinfo=dt.timezone.utc)
        return (db - da).days
    return None


# ── the trigger ladder ──────────────────────────────────────────────────

LADDER_KEYS = [
    ("support", "60-session low"),
    ("ma200", "200-day average"),
    ("ma50", "50-day average"),
    ("ma20", "20-day average"),
    ("resistance", "60-session high"),
    ("resistance_major", "52-week high"),
]


def ladder(levels, price):
    """Order the levels by price, because that is the only order that is
    always true. In a downtrend the 20-day sits *below* the 50-day, so a
    'reclaim the 20-day then the 50-day' ladder written from habit tells
    the reader to wait for a level they have already passed.

    Returns stages sorted ascending with the side relative to spot."""
    out = []
    for key, label in LADDER_KEYS:
        v = rs.fv((levels or {}).get(key))
        if v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        out.append({"key": key, "label": label, "value": v,
                    "grade": grade((levels or {}).get(key))})
    out.sort(key=lambda r: r["value"])
    if price is None:
        return out
    for r in out:
        r["side"] = "above" if r["value"] > price else "below"
        r["distance_pct"] = (r["value"] - price) / price * 100.0
    return out


def recovery_stages(levels, price):
    """The upside ladder: every level above spot, nearest first. Strictly
    increasing by construction — the validator re-checks it anyway."""
    return [r for r in ladder(levels, price) if r.get("side") == "above"]


def downside_stages(levels, price):
    """Levels below spot, nearest first (descending)."""
    return list(reversed([r for r in ladder(levels, price)
                          if r.get("side") == "below"]))


# ── technical state ─────────────────────────────────────────────────────

def technical_state(levels, price):
    """A precise sentence, not 'below key averages'. The reader needs to
    know which averages and in what order, because 'below the 20-day but
    above the 200-day' and 'below all three' are different situations."""
    if price is None:
        return None, []
    pairs = [("ma20", "20-day"), ("ma50", "50-day"), ("ma200", "200-day")]
    have = []
    for key, name in pairs:
        v = rs.fv((levels or {}).get(key))
        if v is None:
            continue
        try:
            have.append((name, float(v), float(v) < price))
        except (TypeError, ValueError):
            continue
    if not have:
        return None, []
    above = [n for n, _, isab in have if isab]
    below = [n for n, _, isab in have if not isab]
    long_term = [h for h in have if h[0] == "200-day"]
    parts = []
    if long_term:
        parts.append("Long-term uptrend intact" if long_term[0][2]
                     else "Long-term trend broken")
    short = [h for h in have if h[0] in ("20-day", "50-day")]
    if short:
        s_below = [h[0] for h in short if not h[2]]
        s_above = [h[0] for h in short if h[2]]
        if s_below and not s_above:
            parts.append("short-term correction below the %s average%s"
                         % (" and ".join(s_below),
                            "s" if len(s_below) > 1 else ""))
        elif s_above and not s_below:
            parts.append("holding above the %s average%s"
                         % (" and ".join(s_above),
                            "s" if len(s_above) > 1 else ""))
        else:
            parts.append("above the %s but below the %s"
                         % (" and ".join(s_above), " and ".join(s_below)))
    return "; ".join(parts) + ".", [("above", above), ("below", below)]


# ── risk boundary vs invalidation ───────────────────────────────────────

HORIZON_MIN_ATR = [
    (re.compile(r"\b(day|week|1-3\s*month|short)", re.I), 1.0),
    (re.compile(r"\b(3-12|6-12|medium|quarter)", re.I), 2.0),
    (re.compile(r"\b(12|18|24|year|long|multi)", re.I), 3.0),
]


def horizon_floor(horizon):
    """How far away an exit has to sit before it is compatible with the
    stated holding period. A twelve-month thesis stopped out by one day
    of normal range is not a twelve-month thesis."""
    for rx, floor in HORIZON_MIN_ATR:
        if rx.search(str(horizon or "")):
            return floor
    return 1.5


def exit_level(snap):
    """An exit level only becomes an 'invalidation' once there is a
    position to invalidate. With no entry on the book it is a risk
    boundary: the price at which the read stops being right, stated so
    the reader can size around it rather than a stop we never placed."""
    dec = snap.get("decision") or {}
    levels = snap.get("levels") or {}
    plan = dec.get("position_plan") or {}
    active = bool(plan.get("entry") or plan.get("entry_zone")
                  or plan.get("shares") or plan.get("size"))
    price = spot(snap)
    support = rs.fv(levels.get("support"))
    atr = rs.fv(levels.get("atr14"))
    label = "Invalidation" if active else "Risk boundary"
    out = {"label": label, "active_entry": active, "grade": DERIVED,
           "basis": None, "value": None, "atr_multiple": None,
           "horizon": dec.get("horizon"),
           "floor": horizon_floor(dec.get("horizon"))}
    floor = out["floor"]
    # Two candidates: the level the stock has actually traded to, and the
    # distance the stated holding period requires. Take the WIDER of the
    # two. When price is sitting just above its 60-session low that low
    # is only a fraction of a day's range away, and quoting it as the
    # boundary for a multi-week thesis promises an exit that ordinary
    # noise would trigger. Which rule bound the level is stated, so the
    # reader can see whether it came from the tape or from the horizon.
    documented = float(support) if support is not None else None
    horizon_based = (float(price) - floor * float(atr)
                     if (atr and price) else None)
    if documented is not None and horizon_based is not None:
        if documented <= horizon_based:
            out["value"], out["bound_by"] = documented, "documented low"
            out["basis"] = ("60-session low, the lowest close the stock has "
                            "actually traded to in this window")
        else:
            out["value"], out["bound_by"] = horizon_based, "horizon"
            out["basis"] = ("spot less %.1f x ATR(14) — the 60-session low "
                            "at %.2f sits inside one %.1f x ATR band, too "
                            "close to survive normal daily range over this "
                            "holding period" % (floor, documented, floor))
    elif documented is not None:
        out["value"], out["bound_by"] = documented, "documented low"
        out["basis"] = ("60-session low, the lowest close the stock has "
                        "actually traded to in this window")
    elif horizon_based is not None:
        out["value"], out["bound_by"] = horizon_based, "horizon"
        out["basis"] = ("spot less %.1f x ATR(14); no documented low in the "
                        "window" % floor)
    if out["value"] is not None and atr and price:
        try:
            out["atr_multiple"] = (float(price) - out["value"]) / float(atr)
        except ZeroDivisionError:
            out["atr_multiple"] = None
    return out


# ── catalyst separation ─────────────────────────────────────────────────

CURRENT_DRIVER_DAYS = 21

# Field names are for code. A reader should see "earnings release", not
# the snake_case tag we happen to store it under.
EVENT_KIND_LABEL = {
    "primary_release": "earnings release",
    "periodic_filing": "periodic filing",
}


def event_label(kind):
    return EVENT_KIND_LABEL.get(kind, str(kind or "filing").replace("_", " "))

CONFIRMATION = {
    "regulator": "regulator-confirmed (EDGAR acceptance timestamp)",
    "company": "company-confirmed (issuer disclosure)",
    "vendor": "vendor-estimated (data provider, not confirmed by the issuer)",
}


def catalysts(snap, now=None):
    """Three different questions the original report ran together: what
    was the last confirmed event, what is moving the stock now, and what
    is next. An earnings release from seven weeks ago is a fact about the
    past, not an explanation of today's tape."""
    cat = snap.get("catalyst") or {}
    now = now or snap.get("report_time")
    ev_dt = cat.get("event_dt")
    hours = _hours_between(ev_dt, now) if (ev_dt and now) else None
    age = (hours / 24.0) if hours is not None else None
    # A disclosure stamped after the report has not been reported yet. It
    # is a scheduled event, and calling it the "last reported" catalyst
    # would date the analysis to something that had not happened.
    future = hours is not None and hours < 0
    last, scheduled = None, None
    if ev_dt:
        et, warn = to_et(ev_dt)
        rec = {"when": et, "when_utc": utc_iso(ev_dt), "tz_warning": warn,
               "what": cat.get("description") or cat.get("event_kind"),
               "kind": cat.get("event_kind"),
               "confirmation": CONFIRMATION["regulator"],
               "grade": OBSERVED, "age_days": age, "hours": hours,
               "refs": [cat.get("event_ref")] if cat.get("event_ref") else []}
        if future:
            scheduled = dict(rec, confirmation=CONFIRMATION["company"],
                             note="filed after this report's timestamp; it "
                                  "is ahead of the analysis, not behind it")
        else:
            last = rec
    news = ((snap.get("sentiment") or {}).get("news") or [])
    driver = {"grade": INFERRED, "text": None, "stale_catalyst": False}
    if future:
        driver["text"] = ("The next confirmed company event is still ahead "
                          "of this report, so nothing disclosed by the "
                          "company explains the current tape.")
        driver["grade"] = OBSERVED
    elif age is not None and age <= CURRENT_DRIVER_DAYS and last:
        whole = int(round(age))
        driver["text"] = ("The %s disclosed %s remains the most recent "
                          "confirmed company event."
                          % (event_label(cat.get("event_kind")),
                             "today" if whole == 0 else
                             "%d day%s ago" % (whole,
                                               "" if whole == 1 else "s")))
        driver["grade"] = DERIVED
    elif news:
        driver["text"] = ("No company disclosure inside the last %d days. "
                          "The most recent coverage is \"%s\" (%s)."
                          % (CURRENT_DRIVER_DAYS,
                             news[0].get("headline") or "",
                             news[0].get("publisher") or "unattributed"))
        driver["stale_catalyst"] = bool(age is not None
                                        and age > CURRENT_DRIVER_DAYS)
    else:
        driver["text"] = ("No confirmed company event and no qualifying "
                          "coverage inside the window. We cannot name a "
                          "driver, which is a gap in evidence and not a "
                          "statement about the business.")
    nxt = []
    for u in cat.get("upcoming") or []:
        when = u.get("when")
        et, warn = to_et(when)
        vendor = "estimate" in str(u.get("what", "")).lower() or \
                 "not company-confirmed" in str(u.get("what", "")).lower()
        nxt.append({"what": u.get("what"), "when": et or str(when),
                    "when_utc": utc_iso(when), "tz_warning": warn,
                    "confirmation": CONFIRMATION["vendor" if vendor
                                                 else "company"],
                    "grade": OBSERVED})
    if scheduled:
        nxt.insert(0, scheduled)
    # the same event often arrives twice — once as the filing we read and
    # once as the vendor's calendar entry. Listing it twice would imply
    # two separate catalysts.
    seen, uniq = set(), []
    for n in nxt:
        k = (n.get("when_utc") or n.get("when"), str(n.get("what"))[:40])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(n)
    return {"last_reported": last, "current_driver": driver, "next": uniq,
            "scheduled": scheduled}


# ── ownership and insiders ──────────────────────────────────────────────

def ownership_view(snap):
    """A count of 13D/G filings is only meaningful if each one can be
    named. Our filer parser does not read the filing body, so we can
    report that filings exist and point at their accession numbers, but
    we cannot say who filed or how big a stake — and we say exactly
    that instead of implying accumulation."""
    own = snap.get("ownership") or {}
    rows, unnamed = [], 0
    for f in own.get("filings") or []:
        if not f.get("in_window", True):
            continue
        accn = None
        ref = f.get("evidence_ref") or ""
        if ref.startswith("OWN-"):
            accn = ref[4:]
        if not f.get("filer"):
            unnamed += 1
        et, _ = to_et(f.get("accepted"))
        rows.append({"form": f.get("form"), "filer": f.get("filer"),
                     "accepted": et or f.get("accepted"),
                     "accession": accn, "url": f.get("url"),
                     "stake": None, "ref": ref})
    # A filing count is only interpretable if each row can be *looked up*
    # as well as named. A filer with no accession number cannot be
    # verified by the reader, and no stake size is parsed from any 13D/G
    # body at all — so a count on its own must never imply accumulation.
    no_accn = len([r for r in rows if not r.get("accession")])
    complete = bool(rows) and unnamed == 0 and no_accn == 0
    if not rows:
        note = "No Schedule 13D or 13G filing in the window."
    elif complete:
        note = ("Stake sizes are not parsed from filing bodies, so these "
                "filings are reported as events, not as position changes.")
    else:
        gaps = []
        if unnamed:
            gaps.append("%d of %d could not be attributed to a named filer"
                        % (unnamed, len(rows)))
        if no_accn:
            gaps.append("%d carry no accession number to look up"
                        % no_accn)
        note = ("Ownership interpretation unavailable: %s. No stake size is "
                "parsed from any filing body, so the count below is a count "
                "of filings and nothing more." % "; ".join(gaps))
    return {"rows": rows, "n": len(rows), "filers_parsed": complete,
            "unnamed": unnamed, "without_accession": no_accn,
            "institutional_pct": own.get("institutional_pct"),
            "interpretation": note, "grade": OBSERVED}


INSIDER_LABEL = [
    ("open_market_buy", "Open-market purchases"),
    ("open_market_sale", "Open-market sales"),
    ("planned_sale_10b5_1", "Sales under a documented 10b5-1 plan"),
    ("option_exercise", "Option exercises"),
    ("tax_withholding", "Shares withheld for tax"),
    ("grant_award", "Grants and awards"),
    ("other_non_market", "Other non-market transactions"),
]


def insider_view(snap):
    """Split by what the transaction actually was. A CFO's quarterly
    tax-withholding sale and a director buying on the open market are
    both 'insider selling/buying' in a naive summary, and they carry
    opposite information."""
    ins = snap.get("insiders") or {}
    econ = ins.get("economics") or {}
    by = ins.get("by_class") or {}
    rows = [{"label": lab, "n": by.get(k, 0), "carries_view":
             k in ("open_market_buy", "open_market_sale")}
            for k, lab in INSIDER_LABEL if by.get(k)]
    return {"rows": rows, "window_days": ins.get("window_days"),
            "window_start": econ.get("window_start"),
            "window_end": econ.get("window_end"),
            "plan_status": econ.get("plan_status"),
            "net_open_market_value": econ.get("net_open_market_value"),
            "distinct_selling_insiders": econ.get("distinct_selling_insiders"),
            "n_view_bearing": ins.get("n_view_bearing"),
            "n_mechanics": ins.get("n_mechanics"),
            "count_statement": ins.get("count_statement"),
            "grade": OBSERVED}


# ── options ─────────────────────────────────────────────────────────────

def options_view(snap):
    """The live pipeline has no options feed. Rather than leave a blank
    heading that reads like 'no flow', state the coverage gap."""
    lv = snap.get("levels") or {}
    em = rs.fv(lv.get("expected_move"))
    if em is None:
        return {"available": False,
                "note": ("Options coverage unavailable: no chain, open "
                         "interest or implied-volatility feed is wired into "
                         "this report. This is a gap in our data, not an "
                         "absence of options activity."),
                "grade": OBSERVED}
    return {"available": True, "expected_move": em,
            "print_time": (lv.get("expected_move") or {}).get("as_of"),
            "oi_status": (lv.get("expected_move") or {}).get("note"),
            "grade": grade(lv.get("expected_move"))}


# ── social themes ───────────────────────────────────────────────────────

def social_view(snap):
    """Themes and counts in the brief; the raw posts belong in the
    appendix. A reader deciding on a position should not have to scroll
    past anonymous message-board text to reach the next fact.

    Two alt-data shapes exist in this codebase: the live block, which
    reports `n_rejected` and a structured `coordination` dict, and an
    older one that reports `n_dropped_irrelevant` and a coordination
    *sentence*. v2 read the live keys against the old shape, printed
    'Phrase groups: None' beside a sentence claiming five phrase groups,
    and failed its own population identity. Both shapes are normalised
    here so the page can only render what actually reconciles."""
    alt = snap.get("sentiment") or {}
    raw = alt.get("coordination")
    if isinstance(raw, dict):
        coord = raw
    elif raw:
        # a prose assessment with no counts behind it: keep the words,
        # but do not manufacture the numbers it implies
        coord = {"label": raw, "phrase_groups": alt.get("echoed_phrases"),
                 "pct_of_relevant_posts": alt.get("echoed_share_pct"),
                 "posts_affected": None, "threshold": None}
    else:
        coord = {}
    n_rel = alt.get("n_relevant")
    n_rej = alt.get("n_rejected")
    if n_rej is None:
        n_rej = alt.get("n_dropped_irrelevant")
    return {
        "n_considered": alt.get("n_considered"),
        "n_counted": n_rel,
        "n_rejected": n_rej,
        "unique_authors": alt.get("unique_authors"),
        "by_class": alt.get("by_class") or {},
        "post_bull_pct": alt.get("post_weighted_bull_pct"),
        "author_bull_pct": alt.get("author_weighted_bull_pct"),
        "classification": alt.get("classification"),
        "reliability": (alt.get("decision_read") or {}).get("reliability"),
        "attention": (alt.get("decision_read") or {}).get("attention"),
        "coordination": {
            "phrase_groups": coord.get("phrase_groups"),
            "posts_affected": coord.get("posts_affected"),
            "pct": coord.get("pct_of_relevant_posts"),
            "threshold": coord.get("threshold"),
            "label": coord.get("label"),
        },
        "source_mix": alt.get("source_mix"),
        "grade": DERIVED,
    }


# ── what changed ────────────────────────────────────────────────────────

def _state_path(ticker):
    return os.path.join(STATE_DIR, "%s.json" % str(ticker).upper())


def prior_state(ticker):
    try:
        with open(_state_path(ticker), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def save_state(snap):
    """Persist the handful of scalars the next report needs to answer
    'what changed'. Deliberately tiny — this is a diff key, not a cache
    of the report."""
    lv = snap.get("levels") or {}
    dec = snap.get("decision") or {}
    st = {"ticker": snap.get("ticker"),
          "report_time": snap.get("report_time"),
          "price": spot(snap),
          "action": dec.get("current_action"),
          "ma20": rs.fv(lv.get("ma20")), "ma50": rs.fv(lv.get("ma50")),
          "ma200": rs.fv(lv.get("ma200")),
          "catalyst": (snap.get("catalyst") or {}).get("event_dt"),
          "evidence_quality": (snap.get("evidence") or {}).get(
              "evidence_quality")}
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(_state_path(snap.get("ticker")), "w",
                  encoding="utf-8") as fh:
            json.dump(st, fh, indent=1)
    except Exception:
        pass
    return st


def _side(price, ma):
    if price is None or ma is None:
        return None
    return "above" if price > ma else "below"


def what_changed(snap, prior=None):
    """The single most useful line in a recurring report, and the one the
    old brief never had: a reader who saw last week's version wants the
    delta, not a re-read."""
    prior = prior if prior is not None else prior_state(snap.get("ticker"))
    if not prior:
        return {"first_report": True, "items": [],
                "note": "No prior report on file for %s; this is the first "
                        "brief in the series, so there is nothing to compare "
                        "against." % snap.get("ticker", "this name"),
                "grade": OBSERVED}
    lv = snap.get("levels") or {}
    dec = snap.get("decision") or {}
    price = spot(snap)
    items = []
    p0 = prior.get("price")
    if price is not None and p0:
        pct = (price - p0) / p0 * 100.0
        items.append({"text": "Price %+.1f%% since the last report "
                              "(%.2f to %.2f)." % (pct, p0, price),
                      "grade": DERIVED})
    for key, name in (("ma20", "20-day"), ("ma50", "50-day"),
                      ("ma200", "200-day")):
        now_side = _side(price, rs.fv(lv.get(key)))
        was_side = _side(p0, prior.get(key))
        if now_side and was_side and now_side != was_side:
            items.append({"text": "Price crossed from %s to %s the %s "
                                  "average." % (was_side, now_side, name),
                          "grade": DERIVED})
    if dec.get("current_action") != prior.get("action"):
        items.append({"text": "Action changed from %s to %s."
                              % (prior.get("action"), dec.get("current_action")),
                      "grade": DERIVED})
    now_cat = (snap.get("catalyst") or {}).get("event_dt")
    if now_cat and now_cat != prior.get("catalyst"):
        items.append({"text": "A new company disclosure has been filed since "
                              "the last report.", "grade": OBSERVED})
    eq = (snap.get("evidence") or {}).get("evidence_quality")
    if eq and eq != prior.get("evidence_quality"):
        items.append({"text": "Evidence quality moved from %s to %s."
                              % (prior.get("evidence_quality"), eq),
                      "grade": DERIVED})
    et, _ = to_et(prior.get("report_time"))
    return {"first_report": False, "items": items, "since": et,
            "note": None if items else
            "Nothing material changed since the previous report.",
            "grade": DERIVED}


# ── the model ───────────────────────────────────────────────────────────

def build(snap, mk=None, prior=None):
    """Assemble everything the four pages draw from, once, so the pages
    cannot disagree with each other."""
    lv = snap.get("levels") or {}
    dec = snap.get("decision") or {}
    price = spot(snap)
    quote_et, quote_warn = to_et(snap.get("market_data_time"))
    rep_et, _ = to_et(snap.get("report_time"))
    state, sides = technical_state(lv, price)
    return {
        "schema": "stock_research_brief/v3",
        "ticker": snap.get("ticker"),
        "price": price,
        "quote_time_et": quote_et,
        "quote_time_utc": utc_iso(snap.get("market_data_time")),
        "quote_tz_warning": quote_warn,
        "report_time_et": rep_et,
        "report_time_utc": utc_iso(snap.get("report_time")),
        "technical_state": state,
        "technical_sides": sides,
        "ladder": ladder(lv, price),
        "recovery": recovery_stages(lv, price),
        "downside": downside_stages(lv, price),
        "exit": exit_level(snap),
        "changed": what_changed(snap, prior),
        "catalysts": catalysts(snap),
        "ownership": ownership_view(snap),
        "insiders": insider_view(snap),
        "options": options_view(snap),
        "social": social_view(snap),
        "grades": GRADE_NOTE,
    }
