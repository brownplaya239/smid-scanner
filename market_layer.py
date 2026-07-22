#!/usr/bin/env python3
"""market_layer.py — the universal market read that opens the brief.

The email has to answer two questions in order: what environment is the
user walking into, and what changed in their names because of it. This
module owns the first one. It is deliberately separate from
daily_brief.py so the regime call can be tested without rendering an
email, and so the same layer can feed the site later.

Every value carries the session date and an as-of time. Nothing here
infers a number it did not fetch: when a source is missing the field is
absent and the renderer says so, rather than printing a stale figure as
if it were live.

    python market_layer.py            # human-readable dump
    python market_layer.py --json     # machine-readable
    python market_layer.py --self-test
"""

import json
import os
import sys
from datetime import datetime, timedelta

import brief_time as BT

try:
    import pytz
    ET = pytz.timezone("America/New_York")
except ImportError:                                  # pragma: no cover
    from datetime import timezone
    ET = timezone(timedelta(hours=-4))

_BASE = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(_BASE, "docs", "reports")

# ── session naming ──────────────────────────────────────────────────────
# The brief is sent several times a day and must never call a 3pm send a
# "Pre-Market Brief" — the label is derived from the clock, not a flag.
SESSIONS = (
    ("premarket", "Pre-Market Brief", (0, 0), (9, 30)),
    ("opening",   "Opening Brief",    (9, 30), (11, 0)),
    ("midday",    "Midday Brief",     (11, 0), (14, 30)),
    ("closing",   "Closing Brief",    (14, 30), (23, 59)),
)

INDICES = ["SPY", "QQQ", "IWM", "DIA"]
VOL_TICKER = "^VIX"
RATE_TICKER = "^TNX"          # 10-year yield, quoted x10
SECTORS = {
    "XLK": "Technology", "SMH": "Semis", "XLF": "Financials",
    "XLE": "Energy", "XLV": "Health Care", "XLI": "Industrials",
    "XLY": "Cons. Disc.", "XLP": "Cons. Staples", "XLU": "Utilities",
    "XLB": "Materials", "XLRE": "Real Estate", "XLC": "Comm. Svcs",
}


def session_for(now_et=None):
    """(key, label) for the send time. Weekend sends fall back to the
    most recent session's frame rather than pretending to be pre-market."""
    now_et = now_et or datetime.now(ET)
    hm = (now_et.hour, now_et.minute)
    for key, label, start, end in SESSIONS:
        if start <= hm < end:
            return key, label
    return "closing", "Closing Brief"


# ── price layer ─────────────────────────────────────────────────────────

def _pct(a, b):
    return None if not b else round(100.0 * (a - b) / b, 2)


def fetch_quotes(tickers, period="1y"):
    """Last close, 1D, 1W and distance from the 20-day mean, from ONE
    daily series per ticker so the three derived numbers cannot disagree."""
    out = {}
    try:
        import yfinance as yf
    except ImportError:
        return out
    try:
        data = yf.download(tickers, period=period, interval="1d",
                           auto_adjust=False, progress=False,
                           group_by="ticker", threads=True)
    except Exception:
        return out
    for tk in tickers:
        try:
            df = data[tk] if len(tickers) > 1 else data
            closes = [float(x) for x in df["Close"].dropna().tolist()]
            if len(closes) < 21:
                continue
            idx = df["Close"].dropna().index
            last_dt = idx[-1].to_pydatetime()
            ma20 = sum(closes[-20:]) / 20.0
            # YTD measured from the first session of the current year in
            # this same series — never a second vendor field
            yr = last_dt.year
            ytd_base = None
            for i, ts in enumerate(idx):
                if ts.year == yr:
                    ytd_base = closes[i]
                    break
            out[tk] = {
                "last": round(closes[-1], 2),
                "chg_1d_pct": _pct(closes[-1], closes[-2]),
                "chg_1w_pct": _pct(closes[-1], closes[-6]) if len(closes) >= 6
                              else None,
                "chg_ytd_pct": _pct(closes[-1], ytd_base) if ytd_base else None,
                "ma20": round(ma20, 2),
                "dist_ma20_pct": _pct(closes[-1], ma20),
                "above_ma20": closes[-1] > ma20,
                "session_date": last_dt.date().isoformat(),
            }
        except Exception:
            continue
    return out


def load_breadth():
    """Participation from the nightly regime file — a real count across
    the scanned universe, not a guess derived from four index prints."""
    p = os.path.join(REPORTS_DIR, "regime_history.json")
    try:
        with open(p, encoding="utf-8") as fh:
            hist = json.load(fh)
    except Exception:
        return {}
    days = hist.get("days") or []
    if not days:
        return {}
    cur = days[-1]
    prior = days[-2] if len(days) > 1 else {}
    week_ago = days[-6] if len(days) >= 6 else {}
    out = {
        "date": cur.get("date"),
        "breadth_pct": cur.get("breadth"),
        "universe": cur.get("universe"),
        "avg_change_pct": cur.get("avg_chg"),
        "label_prior": prior.get("label"),
        "label_current": cur.get("label"),
        "breadth_prior": prior.get("breadth"),
        "breadth_week_ago": week_ago.get("breadth"),
        "updated": hist.get("updated"),
    }
    if out["breadth_pct"] is not None and out["breadth_week_ago"] is not None:
        out["breadth_wow"] = out["breadth_pct"] - out["breadth_week_ago"]
    return out


# ── regime ──────────────────────────────────────────────────────────────

RISK_ON, RISK_OFF, BALANCED, TRANSITION = (
    "RISK-ON", "RISK-OFF", "BALANCED", "TRANSITION")


def classify_regime(indices, vix, tnx, breadth):
    """Four named inputs, stated rules, and a sentence that cites all of
    them. A regime label with no visible reasoning is just a mood."""
    spy, qqq = indices.get("SPY") or {}, indices.get("QQQ") or {}
    b = (breadth or {}).get("breadth_pct")
    vix_last = (vix or {}).get("last")
    vix_chg = (vix or {}).get("chg_1d_pct")
    tnx_chg = (tnx or {}).get("chg_1d_pct")

    above = [x.get("above_ma20") for x in indices.values()
             if x.get("above_ma20") is not None]
    n_above = sum(1 for x in above if x)
    trend_ok = bool(above) and n_above >= max(1, len(above) - 1)
    trend_bad = bool(above) and n_above <= 1

    inputs = {"breadth_pct": b, "indices_above_20d": "%d of %d"
              % (n_above, len(above)) if above else None,
              "vix": vix_last, "vix_1d_pct": vix_chg,
              "ten_year_1d_pct": tnx_chg}

    # rules, evaluated in order of severity
    if b is not None and b <= 40 and trend_bad:
        label = RISK_OFF
    elif b is not None and b >= 55 and trend_ok and (
            vix_chg is None or vix_chg <= 0):
        label = RISK_ON
    elif (breadth or {}).get("label_prior") and \
            (breadth or {}).get("label_current") and \
            breadth["label_prior"] != breadth["label_current"]:
        label = TRANSITION
    elif (b is not None and b <= 45) or trend_bad or (
            vix_chg is not None and vix_chg >= 8):
        label = RISK_OFF if trend_bad else TRANSITION
    else:
        label = BALANCED

    bits = []
    if b is not None:
        uni = breadth.get("universe") or 0
        bits.append("%d%% of the %s-name universe is above its 20-day average"
                    % (b, format(uni, ",")) if uni else
                    "breadth %d%% (universe size unavailable)" % b)
    if above:
        bits.append("%d of %d major indices above their 20-day"
                    % (n_above, len(above)))
    if vix_last is not None:
        bits.append("VIX %.1f%s" % (vix_last,
                    (" (%+.1f%%)" % vix_chg) if vix_chg is not None else ""))
    ty = (tnx or {}).get("yield_pct")
    if ty is not None:
        bp = round(ty * tnx_chg) if tnx_chg is not None else None
        bits.append("10-year yield %.2f%%%s"
                    % (ty, (" (%+d bp)" % bp) if bp is not None else ""))
    why = ("; ".join(bits) + ".") if bits else \
        "insufficient market data to explain the regime."
    return {"label": label, "why": why, "inputs": inputs,
            "changed_from": (breadth or {}).get("label_prior")}


# ── macro events ────────────────────────────────────────────────────────

UPCOMING, IN_PROGRESS, COMPLETED = "UPCOMING", "IN PROGRESS", "COMPLETED"
_MARQUEE = ("cpi", "fomc", "rate decision", "nonfarm", "non-farm", "payroll",
            "pce", "gdp", "powell", "jobless claims", "ppi", "ism",
            "michigan", "retail sales")


def event_impact(ev):
    impact = (ev.get("impact") or "").lower()
    stars = 3 if impact == "high" else 2 if impact == "medium" else 1
    if any(k in (ev.get("title") or "").lower() for k in _MARQUEE):
        stars += 2
    return min(stars, 5)


def _ev_time_et(ev, now_et):
    """When the event actually starts, as an aware Eastern datetime.

    Prefers `starts_at`, which the calendar writer produces by converting
    from the feed's own zone. The older `time` key held the feed's clock
    (UTC) and was previously copied into `time_et` unchanged, which is how
    a 10:30 ET release came to be printed as 2:30pm.
    """
    iso = ev.get("starts_at")
    if iso:
        dt = BT.parse_iso(iso)
        if dt is not None and dt.tzinfo is not None:
            return BT.to_et(dt)
    # legacy payload: the clock is whatever zone the file was written in,
    # so trust the label only when the writer says it converted
    c = BT.parse_clock(ev.get("time_et") or "")
    if c is None:
        return None
    return now_et.replace(hour=c[0], minute=c[1], second=0, microsecond=0)


UNSCHEDULED = "UNSCHEDULED"


def event_status(ev, now_et, grace_min=30):
    """A released number is never shown as upcoming. Once the actual is
    published the event leaves the action queue.

    An event with no resolvable clock is UNSCHEDULED, not UPCOMING:
    calling it upcoming asserts it has not happened yet, which is exactly
    what we do not know.
    """
    if ev.get("actual") not in (None, "", "-"):
        return COMPLETED
    when = _ev_time_et(ev, now_et)
    if when is None:
        return UNSCHEDULED
    if now_et < when:
        return UPCOMING
    if now_et < when + timedelta(minutes=grace_min):
        return IN_PROGRESS
    return COMPLETED


def load_events(now_et=None):
    now_et = now_et or datetime.now(ET)
    p = os.path.join(REPORTS_DIR, "economic_calendar.json")
    try:
        with open(p, encoding="utf-8") as fh:
            cal = json.load(fh)
    except Exception:
        return []
    # the writer emits ISO dates now; accept the legacy MM-DD-YYYY too so a
    # stale file still filters to the right day instead of silently
    # returning nothing
    today = {now_et.strftime("%Y-%m-%d"), now_et.strftime("%m-%d-%Y")}
    evs = [e for e in (cal.get("events") or []) if e.get("date") in today]
    for e in evs:
        e["status"] = event_status(e, now_et)
        e["stars"] = event_impact(e)
        when = _ev_time_et(e, now_et)
        e["_when"] = when
        if not e.get("time_et"):
            e["time_et"] = BT.fmt_time(when) if when else ""
    evs.sort(key=lambda e: (-e["stars"],
                            e["_when"].isoformat() if e.get("_when") else "~"))
    return evs


def calendar_problems(now_et=None):
    """Send-time check: is the on-disk calendar converted, and do its
    releases still land on their published times?"""
    p = os.path.join(REPORTS_DIR, "economic_calendar.json")
    try:
        with open(p, encoding="utf-8") as fh:
            cal = json.load(fh)
    except Exception:
        return []                       # no calendar is not a wrong calendar
    evs = cal.get("events") or []
    if not evs:
        return []
    if not cal.get("converted"):
        return ["economic_calendar.json predates timezone conversion "
                "(no `converted` flag); regenerate before sending"]
    v = []
    bad = [e for e in evs if e.get("on_schedule") is False]
    if bad:
        v.append("%d calendar event(s) disagree with their published release "
                 "time, e.g. %s at %s (published %s ET)"
                 % (len(bad), bad[0].get("title"), bad[0].get("time_et"),
                    bad[0].get("anchor_et")))
    return v + event_problems(evs, now_et)


def event_problems(events, now_et=None):
    """Blocking checks on the events that will actually be DISPLAYED.

    Provenance is only useful if something refuses to ship without it, so
    each of these corresponds to a way the calendar has been wrong:
    a zone nobody declared, a conversion that does not reproduce, a vendor
    category standing in for an event title, and a past event still
    advertised as upcoming.
    """
    now = now_et or datetime.now(ET)
    v = []
    for e in events or []:
        t = e.get("title") or "(untitled)"
        if not e.get("source_tz"):
            v.append("%s has no source timezone; its time cannot be "
                     "converted, only relabelled" % t)
        if e.get("generic_title"):
            v.append("%r is a vendor category, not an event title; it needs "
                     "an attributed override before it can be displayed" % t)
        # the conversion must reproduce from the stored provenance
        src_tz = BT.zone(e.get("source_tz")) or (
            BT.UTC if (e.get("source_tz") or "").upper() == "UTC" else None)
        if src_tz is not None and e.get("source_time") and e.get("starts_at"):
            want = BT.aware(e.get("source_date") or e.get("date"),
                            e["source_time"], src_tz)
            got = BT.parse_iso(e["starts_at"])
            if want is not None and got is not None:
                delta = abs((BT.to_et(want) - BT.to_et(got)).total_seconds())
                # a date-line shift shows up as a whole number of days and
                # is a date bug, not a zone bug; either way it is wrong
                if delta % 86400 > 60:
                    v.append("%s: stored ET time %s does not reproduce from "
                             "%s %s" % (t, e.get("time_et"), e["source_time"],
                                        e["source_tz"]))
        when = _ev_time_et(e, now)
        if when is not None and when < now and e.get("status") == UPCOMING:
            v.append("%s was scheduled for %s but is still labelled UPCOMING"
                     % (t, e.get("time_et")))
    return v


# ── assembly ────────────────────────────────────────────────────────────

def build(now_et=None, with_sectors=True):
    now_et = now_et or datetime.now(ET)
    key, label = session_for(now_et)
    want = INDICES + [VOL_TICKER, RATE_TICKER]
    q = fetch_quotes(want)
    idx = {t: q[t] for t in INDICES if t in q}
    vix = q.get(VOL_TICKER) or {}
    tnx = dict(q.get(RATE_TICKER) or {})
    if tnx.get("last") is not None:
        # ^TNX has historically quoted the 10-year x10 (46.3 = 4.63%), but
        # the feed now returns the percent directly. Dividing blindly
        # printed "10Y 0.46%". Detect the convention instead of assuming:
        # no plausible 10-year yield sits above 20%.
        raw = tnx["last"]
        tnx["yield_pct"] = round(raw / 10.0 if raw > 20 else raw, 2)
        tnx["quote_convention"] = "x10" if raw > 20 else "percent"
    breadth = load_breadth()
    regime = classify_regime(idx, vix, tnx, breadth)
    events = load_events(now_et)
    actionable = [e for e in events if e["status"] != COMPLETED]

    sectors = {}
    if with_sectors:
        sq = fetch_quotes(list(SECTORS))
        ranked = sorted(
            [{"ticker": t, "name": SECTORS[t], **v} for t, v in sq.items()
             if v.get("chg_1d_pct") is not None],
            key=lambda x: -x["chg_1d_pct"])
        sectors = {"leaders": ranked[:3], "laggards": ranked[-3:][::-1]}

    # Different symbols can carry different last bars — ^VIX printed a
    # 07-22 bar while ^TNX was still on 07-21. Showing them side by side
    # without saying so silently mixes two sessions.
    dates = {}
    for name, d in [(t, idx.get(t)) for t in INDICES] + \
            [("VIX", vix), ("10Y", tnx)]:
        if d and d.get("session_date"):
            dates.setdefault(d["session_date"], []).append(name)
    session_date = (idx.get("SPY") or {}).get("session_date")
    mixed = None
    if len(dates) > 1:
        mixed = {"session_dates": {k: sorted(v) for k, v in dates.items()},
                 "note": ("quotes span more than one session date; each "
                          "value is labelled with its own as-of")}
    return {
        "mixed_sessions": mixed,
        "schema": "market_layer/v1",
        "session_key": key,
        "session_label": label,
        "as_of_et": now_et.strftime("%Y-%m-%d %H:%M ET"),
        # the absolute instant, for consumers that must compare
        # against source timestamps rather than display them
        "as_of_utc": now_et.astimezone(BT.UTC).isoformat(
            timespec="seconds"),
        "session_date": session_date,
        "indices": idx,
        "vix": vix,
        "ten_year": tnx,
        "breadth": breadth,
        "regime": regime,
        "events": events,
        "top_event": (actionable or events or [None])[0],
        "sectors": sectors,
        "unavailable": [k for k, v in (("indices", idx), ("vix", vix),
                                       ("ten_year", tnx), ("breadth", breadth))
                        if not v],
    }


def _fmt(v, suffix="%", plus=True):
    if v is None:
        return "n/a"
    return ("%+.2f%s" if plus else "%.2f%s") % (v, suffix)


def summary_lines(m):
    """The compact strings the email and the subject line both read from,
    so the header and the body cannot disagree."""
    idx = m["indices"]
    parts = []
    for t in INDICES:
        d = idx.get(t)
        if d:
            parts.append("%s %s" % (t, _fmt(d.get("chg_1d_pct"))))
    vix_s = ""
    if (m["vix"] or {}).get("last") is not None:
        vix_s = "VIX %.1f %s" % (m["vix"]["last"],
                                 _fmt(m["vix"].get("chg_1d_pct")))
    ty = (m["ten_year"] or {}).get("yield_pct")
    ty_s = ""
    if ty is not None:
        # basis points, matching the regime sentence. The preheader used to
        # say "+0.65%" for the same move the body called "+3 bp" -- two
        # different units for one number, side by side in the inbox.
        chg = (m["ten_year"] or {}).get("chg_1d_pct")
        bp = ("(%+d bp)" % round(ty * chg)) if chg is not None else ""
        ty_s = ("10Y %.2f%% %s" % (ty, bp)).strip()
    return {"indices": "  ".join(parts), "vix": vix_s, "ten_year": ty_s}


def main():
    args = sys.argv[1:]
    if "--self-test" in args:
        return self_test()
    m = build(with_sectors="--fast" not in args)
    if "--json" in args:
        print(json.dumps(m, indent=2, default=str))
        return 0
    print("%s  |  %s" % (m["session_label"], m["as_of_et"]))
    print("REGIME: %s — %s" % (m["regime"]["label"], m["regime"]["why"]))
    s = summary_lines(m)
    print("  %s   %s  %s" % (s["indices"], s["vix"], s["ten_year"]))
    b = m["breadth"]
    if b:
        print("  breadth %s%% of %s (prior %s%%)"
              % (b.get("breadth_pct"), format(b.get("universe") or 0, ","),
                 b.get("breadth_prior")))
    if m["sectors"].get("leaders"):
        print("  leaders:  " + ", ".join(
            "%s %s" % (x["ticker"], _fmt(x["chg_1d_pct"]))
            for x in m["sectors"]["leaders"]))
        print("  laggards: " + ", ".join(
            "%s %s" % (x["ticker"], _fmt(x["chg_1d_pct"]))
            for x in m["sectors"]["laggards"]))
    print("  events (%d):" % len(m["events"]))
    for e in m["events"][:5]:
        print("    [%s] %s %s  %s" % (e["status"], e.get("time") or "--",
                                      "*" * e["stars"], e.get("title")))
    if m["unavailable"]:
        print("  UNAVAILABLE: " + ", ".join(m["unavailable"]))
    return 0


def self_test():
    fails = []

    def chk(name, cond, detail=""):
        print(("  PASS  " if cond else "  FAIL  ") + name +
              ("" if cond else "  <- %s" % detail))
        if not cond:
            fails.append(name)

    base = datetime(2026, 7, 22, 8, 0, tzinfo=ET) if hasattr(ET, "localize") \
        else datetime(2026, 7, 22, 8, 0)
    mk = lambda h, mi: datetime(2026, 7, 22, h, mi).replace(
        tzinfo=base.tzinfo)
    chk("08:00 -> Pre-Market", session_for(mk(8, 0))[1] == "Pre-Market Brief")
    chk("09:35 -> Opening", session_for(mk(9, 35))[1] == "Opening Brief")
    chk("12:00 -> Midday", session_for(mk(12, 0))[1] == "Midday Brief")
    chk("15:30 -> Closing", session_for(mk(15, 30))[1] == "Closing Brief")
    chk("09:29 is still Pre-Market",
        session_for(mk(9, 29))[1] == "Pre-Market Brief")

    now = mk(14, 0)
    chk("event with an actual is COMPLETED",
        event_status({"time_et": "8:30am", "actual": "3.1%"}, now) == COMPLETED)
    chk("future event is UPCOMING",
        event_status({"time_et": "3:00pm"}, now) == UPCOMING)
    chk("just-released event is IN PROGRESS",
        event_status({"time_et": "1:45pm"}, now) == IN_PROGRESS)
    chk("old event with no actual is COMPLETED",
        event_status({"time_et": "9:00am"}, now) == COMPLETED)
    # a converted payload carries an absolute instant; status must follow
    # that, not a clock string that could be in any zone
    chk("status follows the converted instant, not a bare clock",
        event_status({"starts_at": "2026-07-22T15:00:00-04:00"},
                     now) == UPCOMING)
    chk("the pre-conversion `time` key can no longer drive status",
        event_status({"time": "1:45pm"}, now) == UNSCHEDULED)
    chk("an event with no resolvable clock is not called UPCOMING",
        event_status({"title": "Tentative"}, now) == UNSCHEDULED)
    # provenance gates
    chk("an event with no source timezone is blocked",
        event_problems([{"title": "X", "starts_at":
                         "2026-07-22T10:30:00-04:00"}], now))
    chk("a vendor category standing in for a title is blocked",
        event_problems([{"title": "President Trump Speaks",
                         "source_tz": "UTC", "generic_title": True}], now))
    chk("an attributed override clears the generic-title gate",
        event_problems([{"title": "State Arrival Ceremony",
                         "source_tz": "Europe/Istanbul",
                         "generic_title": False}], now) == [])
    chk("a conversion that does not reproduce is blocked",
        event_problems([{"title": "Crude Oil Inventories", "source_tz": "UTC",
                         "date": "2026-07-22", "source_time": "2:30pm",
                         "starts_at": "2026-07-22T14:30:00-04:00"}], now))
    chk("a reproducible conversion passes",
        event_problems([{"title": "Crude Oil Inventories", "source_tz": "UTC",
                         "date": "2026-07-22", "source_time": "2:30pm",
                         "starts_at": "2026-07-22T10:30:00-04:00"}],
                       now) == [])
    chk("a past event still labelled UPCOMING is blocked",
        event_problems([{"title": "Old", "source_tz": "UTC",
                         "starts_at": "2026-07-22T08:00:00-04:00",
                         "status": UPCOMING}], now))

    on = classify_regime(
        {"SPY": {"above_ma20": True}, "QQQ": {"above_ma20": True}},
        {"last": 14.0, "chg_1d_pct": -3.0},
        {"yield_pct": 4.63, "chg_1d_pct": 0.1},
        {"breadth_pct": 62, "universe": 1000})
    chk("breadth 62 + above 20d + VIX down -> RISK-ON",
        on["label"] == RISK_ON, on)
    off = classify_regime(
        {"SPY": {"above_ma20": False}, "QQQ": {"above_ma20": False}},
        {"last": 28.0, "chg_1d_pct": 12.0}, {"chg_1d_pct": -0.5},
        {"breadth_pct": 33, "universe": 1000})
    chk("breadth 33 + below 20d -> RISK-OFF", off["label"] == RISK_OFF, off)
    trans = classify_regime(
        {"SPY": {"above_ma20": True}, "QQQ": {"above_ma20": False}},
        {"last": 18.0, "chg_1d_pct": 1.0}, {"chg_1d_pct": 0.2},
        {"breadth_pct": 50, "universe": 1000,
         "label_prior": "risk_off", "label_current": "risk_on"})
    chk("label flip -> TRANSITION", trans["label"] == TRANSITION, trans)
    # the sentence states breadth as a measured number, not the word
    # "breadth" — checking for the label would pass on a sentence that
    # never says what the reading actually was
    chk("regime sentence cites breadth, indices, VIX and rates",
        all(k in on["why"] for k in ("62%", "1,000-name", "20-day", "VIX",
                                     "10-year")),
        on["why"])
    chk("regime never returns an unknown label",
        all(r["label"] in (RISK_ON, RISK_OFF, BALANCED, TRANSITION)
            for r in (on, off, trans)))

    n = len(fails) + sum(1 for _ in range(0))
    total = 14
    print("\n%d/%d checks passed" % (total - len(fails), total))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
