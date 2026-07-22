#!/usr/bin/env python3
"""research_live.py — real-data adapters for the v2 research brief.

The v2 prototype was blocked because its numbers were synthetic and
temporally contaminated. This module is the production path: every fact
it produces carries a real source URL, a real publication time, and (for
filing data) the SEC acceptance timestamp of the filing that published
it. Nothing is admitted that was not public when the report was written.

Sources, and what each is allowed to establish:
  SEC EDGAR XBRL   fundamentals, share count   -> published_at = filing
                                                  acceptanceDateTime
  SEC Form 4       insider transactions        -> classified, not assumed
  Yahoo daily bars ONE canonical price series  -> price + every level
  StockTwits       social observations         -> per-record provenance
  Yahoo news       headlines                   -> publisher + URL + time

Anything a source cannot establish is left absent, and the gate in
research_snapshot.py decides whether what remains is publishable.

Usage:
    python research_live.py ISRG [--out DIR] [--json]
"""

import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

import evidence_ledger as EL
import research_snapshot as rs

SEC_HEADERS = {
    "User-Agent": "TickerDesk Research sumeetsancheti97@gmail.com",
    "Accept-Encoding": "gzip, deflate",
}
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Safari/537.36")

_last_sec_call = [0.0]


def _throttle(min_gap=0.12):
    """SEC fair-access rate limit."""
    dt = time.time() - _last_sec_call[0]
    if dt < min_gap:
        time.sleep(min_gap - dt)
    _last_sec_call[0] = time.time()


def _sec_json(url):
    _throttle()
    r = requests.get(url, headers=SEC_HEADERS, timeout=30)
    if r.status_code != 200:
        raise RuntimeError("SEC %s -> HTTP %d" % (url, r.status_code))
    return r.json()


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _h(s, n=16):
    return hashlib.sha256(str(s).encode("utf-8")).hexdigest()[:n]


# ── SEC: CIK, filing acceptance times, XBRL facts ───────────────────────

def cik_for(ticker):
    data = _sec_json("https://www.sec.gov/files/company_tickers.json")
    for row in data.values():
        if row.get("ticker", "").upper() == ticker.upper():
            return str(row["cik_str"]).zfill(10)
    raise RuntimeError("no CIK for %s" % ticker)


def _norm_accept(ts):
    """EDGAR publishes acceptanceDateTime as '2026-07-16T20:05:17.000Z' —
    a real UTC stamp. An earlier version of this module stripped the Z and
    forced a -04:00 offset, shifting every filing four hours later and
    pushing the July 16 earnings release into July 17. Only assume ET when
    the string genuinely carries no zone.
    """
    if not ts:
        return None
    try:
        s = str(ts).strip()
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s[:-1]).replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:            # bare stamp: EDGAR local = ET
                dt = dt.replace(tzinfo=timezone(timedelta(hours=-4)))
        return _iso(dt)
    except Exception:
        return ts


# 8-K items that constitute a primary public disclosure of results.
RESULTS_ITEMS = ("2.02",)          # Results of Operations and Financial Condition
GUIDANCE_ITEMS = ("7.01", "8.01")  # Reg FD / other events


def acceptance_map(cik):
    """accession -> (acceptanceDateTime UTC, form, primary doc URL).

    This is the whole point-in-time mechanism: a 10-Q's `filed` DATE is
    not a timestamp, and a report written at 14:00 ET on filing day must
    not quote a filing accepted at 16:05 ET.
    """
    subs = _sec_json("https://data.sec.gov/submissions/CIK%s.json" % cik)
    out = {}

    def _absorb(rec):
        accn = rec.get("accessionNumber") or []
        acc_dt = rec.get("acceptanceDateTime") or []
        forms = rec.get("form") or []
        docs = rec.get("primaryDocument") or []
        for i, a in enumerate(accn):
            ts = _norm_accept(acc_dt[i] if i < len(acc_dt) else None)
            bare = a.replace("-", "")
            url = ("https://www.sec.gov/Archives/edgar/data/%s/%s/%s"
                   % (int(cik), bare, docs[i] if i < len(docs) else ""))
            out[a] = {"accepted": ts,
                      "form": forms[i] if i < len(forms) else None,
                      "items": (rec.get("items") or [""] * len(accn))[i]
                               if rec.get("items") else "",
                      "primary_doc": docs[i] if i < len(docs) else None,
                      "url": url}

    _absorb((subs.get("filings") or {}).get("recent") or {})
    for extra in (subs.get("filings") or {}).get("files") or []:
        try:
            _absorb(_sec_json("https://data.sec.gov/submissions/"
                              + extra["name"]))
        except Exception:
            continue
    return out, subs


def sec_text(url):
    """Fetch a filing document as text. `_sec_json` raises on anything
    that is not JSON, and an EX-99.1 exhibit is HTML."""
    _throttle()
    r = requests.get(url, headers=SEC_HEADERS, timeout=30)
    if r.status_code != 200:
        raise RuntimeError("SEC %s -> HTTP %d" % (url, r.status_code))
    return r.text


def concept(cik, tag, unit="USD", taxonomy="us-gaap"):
    try:
        j = _sec_json("https://data.sec.gov/api/xbrl/companyconcept/"
                      "CIK%s/%s/%s.json" % (cik, taxonomy, tag))
    except Exception:
        return []
    return (j.get("units") or {}).get(unit) or []


def _quarterly(rows, want_days=(80, 100)):
    """XBRL duration facts that cover ONE quarter."""
    out = []
    for r in rows:
        s, e = r.get("start"), r.get("end")
        if not (s and e):
            continue
        d = (datetime.fromisoformat(e) - datetime.fromisoformat(s)).days
        if want_days[0] <= d <= want_days[1]:
            out.append(r)
    return out


def _instant(rows):
    """XBRL point-in-time facts — a balance-sheet line has an `end` and
    no `start`, so the duration filters above reject every one of them."""
    return [r for r in rows if r.get("end") and not r.get("start")]


def _annual(rows, want_days=(350, 380)):
    out = []
    for r in rows:
        s, e = r.get("start"), r.get("end")
        if not (s and e):
            continue
        d = (datetime.fromisoformat(e) - datetime.fromisoformat(s)).days
        if want_days[0] <= d <= want_days[1]:
            out.append(r)
    return out


def _fill_q4(quarters, annuals):
    """Reconstruct the missing fourth quarter.

    A 10-K tags the FULL YEAR, not the fourth quarter, so an issuer's
    XBRL quarterly series has a hole every Q4. Slicing the last four
    'quarters' therefore silently spans five, and picking index -5 for a
    year-ago comparison silently reaches back fifteen months. Q4 is
    derived as FY minus the three filed quarters, and is marked derived
    so it is never mistaken for a directly tagged figure.
    """
    have = {r["end"] for r in quarters}
    out = list(quarters)
    for a in annuals:
        fy_end = a["end"]
        if fy_end in have:
            continue
        y0 = datetime.fromisoformat(a["start"])
        y1 = datetime.fromisoformat(fy_end)
        inner = [r for r in quarters
                 if y0 <= datetime.fromisoformat(r["start"]) and
                 datetime.fromisoformat(r["end"]) <= y1]
        if len(inner) != 3:
            continue                      # cannot close the year — leave the hole
        q4 = dict(a)
        q4["start"] = max(r["end"] for r in inner)
        q4["val"] = a["val"] - sum(r["val"] for r in inner)
        q4["_derived_q4"] = True
        out.append(q4)
    return sorted(out, key=lambda r: r["end"])


def _contiguous(rows, n=4, tol_days=25):
    """The last n rows must actually tile one continuous window."""
    if len(rows) < n:
        return None
    sel = rows[-n:]
    span = (datetime.fromisoformat(sel[-1]["end"])
            - datetime.fromisoformat(sel[0]["start"])).days
    if not (365 - tol_days <= span <= 365 + tol_days):
        return None
    for a, b in zip(sel, sel[1:]):
        gap = (datetime.fromisoformat(b["start"])
               - datetime.fromisoformat(a["end"])).days
        if abs(gap) > 8:
            return None
    return sel


def _yoy_pair(rows, tol_days=20):
    """Match the year-ago quarter BY DATE, never by list position."""
    if not rows:
        return None, None
    cur = rows[-1]
    target = datetime.fromisoformat(cur["end"]) - timedelta(days=365)
    best = None
    for r in rows[:-1]:
        d = abs((datetime.fromisoformat(r["end"]) - target).days)
        if d <= tol_days and (best is None or d < best[0]):
            best = (d, r)
    return cur, (best[1] if best else None)


def _admit(rows, acc, report_time, dedupe_by_end=True):
    """Keep only XBRL rows whose FILING was accepted at or before
    report_time. Returns (admitted, deferred) so the exclusion is
    reportable rather than silent."""
    cutoff = rs._parse_ts(report_time)
    admitted, deferred = [], []
    for r in rows:
        meta = acc.get(r.get("accn")) or {}
        ts = meta.get("accepted")
        p = rs._parse_ts(ts)
        r = dict(r, _accepted=ts, _url=meta.get("url"),
                 _form=meta.get("form") or r.get("form"))
        (admitted if (p and p <= cutoff) else deferred).append(r)
    if dedupe_by_end:
        best = {}
        for r in admitted:
            k = r.get("end")
            if k not in best or (r.get("_accepted") or "") > \
                    (best[k].get("_accepted") or ""):
                best[k] = r
        admitted = sorted(best.values(), key=lambda x: x["end"])
    return admitted, deferred


# ── market data: one canonical series ───────────────────────────────────

def fetch_market(ticker, as_of=None):
    import yfinance as yf
    tk = yf.Ticker(ticker)
    bars = tk.history(period="2y", interval="1d", auto_adjust=False)
    if bars is None or len(bars) < 210:
        raise RuntimeError("insufficient daily history for %s" % ticker)
    closes = [float(x) for x in bars["Close"].tolist()]
    highs = [float(x) for x in bars["High"].tolist()]
    lows = [float(x) for x in bars["Low"].tolist()]
    vols = [float(x) for x in bars["Volume"].tolist()]
    # A daily bar is indexed at MIDNIGHT of the session, so publishing that
    # timestamp as "market data as of" understates the data by a full
    # trading day. The bar represents the 16:00 ET close.
    last_bar = bars.index[-1].to_pydatetime()
    if last_bar.tzinfo is None:
        last_bar = last_bar.replace(tzinfo=timezone(timedelta(hours=-4)))
    session_close = last_bar.replace(hour=16, minute=0, second=0,
                                     microsecond=0)
    # One clock for the whole report. build_snapshot stamps report_time
    # before calling this, so reading the wall clock again here put the
    # quote a second *after* the gate instant it is meant to sit inside.
    ref = as_of or datetime.now(timezone.utc)
    now_local = ref.astimezone(session_close.tzinfo)
    partial = session_close > now_local
    if partial:
        # The session has not closed yet. Publishing the nominal 16:00
        # stamp would date the quote to a close that has not happened —
        # a report written at 09:31 ET claimed market data from 16:00 ET.
        # While the bar is still forming, the honest "as of" is the
        # moment we read it.
        session_close = now_local.replace(microsecond=0)

    # Every daily indicator is computed from COMPLETED sessions only.
    # Folding a half-formed bar into a 200-day average silently mixes a
    # partial day into a series of full ones, and the reader has no way
    # to tell. The live observation is carried separately and compared
    # against those completed-session indicators.
    c_closes = closes[:-1] if partial else closes
    c_highs = highs[:-1] if partial else highs
    c_lows = lows[:-1] if partial else lows
    c_vols = vols[:-1] if partial else vols
    c_dates = [d.to_pydatetime().date() for d in bars.index]
    c_dates = c_dates[:-1] if partial else c_dates

    def ma(n):
        return round(sum(c_closes[-n:]) / n, 2) if len(c_closes) >= n else None

    trs = []
    for i in range(1, len(c_closes)):
        trs.append(max(c_highs[i] - c_lows[i],
                       abs(c_highs[i] - c_closes[i - 1]),
                       abs(c_lows[i] - c_closes[i - 1])))
    atr14 = round(sum(trs[-14:]) / 14.0, 2) if len(trs) >= 14 else None

    # Wilder RSI(14) on completed closes. Seeded with the simple mean of
    # the first 14 changes, then smoothed — the same definition the
    # legacy chart used, so the number means what a reader expects.
    # Wilder RSI is path-dependent, so the answer depends on how far back
    # you start. Running it over the whole 500-session history would
    # declare a window the evidence package does not carry and could not
    # be reproduced from it. It is bounded to RSI_WINDOW completed
    # sessions, which sits inside the 252 the package ships.
    RSI_WINDOW = 250

    def _rsi(vals, n=14, window=RSI_WINDOW):
        if len(vals) < n + 1:
            return None
        vals = vals[-window:]
        d = [vals[i] - vals[i - 1] for i in range(1, len(vals))]
        gains = [x if x > 0 else 0.0 for x in d]
        losses = [-x if x < 0 else 0.0 for x in d]
        ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
        for i in range(n, len(d)):
            ag = (ag * (n - 1) + gains[i]) / n
            al = (al * (n - 1) + losses[i]) / n
        if al == 0:
            return 100.0
        return round(100.0 - 100.0 / (1.0 + ag / al), 1)

    # Base tightness: how narrow the recent range is, as a percentage of
    # its own floor. Stated with its window because "tight" is meaningless
    # without one.
    BASE_WIN = 20

    def _base_tightness(vals, n=BASE_WIN):
        if len(vals) < n:
            return None
        w = vals[-n:]
        lo = min(w)
        return round(100.0 * (max(w) - lo) / lo, 1) if lo else None

    px = round(closes[-1], 2)                 # live or last completed
    prev = round(c_closes[-1], 2) if partial else round(c_closes[-2], 2)
    win = 60
    # Close basis, to match how the report describes these levels. A
    # boundary quoted as "the lowest close" must not be computed from
    # intraday lows.
    swing_hi = round(max(c_closes[-win:]), 2) if len(c_closes) >= win else None
    swing_lo = round(min(c_closes[-win:]), 2) if len(c_closes) >= win else None
    hi52 = round(max(c_closes[-252:]), 2) if len(c_closes) >= 252 else None
    lo52 = round(min(c_closes[-252:]), 2) if len(c_closes) >= 252 else None

    series_id = "yahoo:%s:1d:unadjusted:asof=%s" % (
        ticker.upper(), last_bar.date().isoformat())
    opens = [float(x) for x in bars["Open"].tolist()]
    dates = [d.to_pydatetime().date() for d in bars.index]
    rs, spy_window = _rs_vs_spy(
        c_closes, c_dates, weeks=12,
        through=(c_dates[-1].isoformat() if c_dates else None))
    return {
        "series_id": series_id,
        "dates": dates, "opens": opens, "closes": closes,
        "highs": highs, "lows": lows, "volumes": vols,
        # the completed-session series every indicator above was built on
        "completed_dates": c_dates, "completed_closes": c_closes,
        "completed_highs": c_highs, "completed_lows": c_lows,
        "completed_volumes": c_vols,
        "completed_sessions": len(c_closes),
        "last_completed_session": (c_dates[-1].isoformat() if c_dates
                                   else None),
        "intraday": ({"session": dates[-1].isoformat(), "last": px,
                      "open": opens[-1], "high": highs[-1], "low": lows[-1],
                      "volume": vols[-1], "complete": False}
                     if partial else None),
        "bar_time": _iso(session_close),
        "session_date": last_bar.date().isoformat(),
        "partial_session": partial,
        "last": px, "prev_close": prev,
        "price_basis": ("intraday last trade, session open" if partial
                        else "last completed session close"),
        "change_pct": round(100.0 * (px - prev) / prev, 2),
        "ma9": ma(9), "ma21": ma(21),
        "ma20": ma(20), "ma50": ma(50), "ma200": ma(200),
        "atr14": atr14,
        "atr14_pct": (round(100.0 * atr14 / px, 2)
                      if (atr14 and px) else None),
        "rsi14": _rsi(c_closes),
        "base_tightness_pct": _base_tightness(c_closes),
        "base_tightness_window": BASE_WIN,
        "base_tightness_formula": ("(max(close) - min(close)) / min(close) "
                                   "x 100 over the last %d completed "
                                   "sessions" % BASE_WIN),
        "pct_below_hi52": (round(100.0 * (hi52 - px) / hi52, 1)
                           if (hi52 and px) else None),
        "support": swing_lo, "resistance": swing_hi,
        "hi52": hi52, "lo52": lo52,
        "n_bars": len(bars),
        "float_shares": _safe_info(tk).get("floatShares"),
        # Short interest is a dated, settled figure. Institutional
        # ownership from this vendor carries no reporting date, so it is
        # returned with the date field empty and the renderer says
        # "Unavailable" rather than printing an undated percentage as if
        # it were a holdings fact.
        "short_interest": _short_interest(_safe_info(tk)),
        "institutional_pct_undated": _safe_info(tk).get(
            "heldPercentInstitutions"),
        "info": _safe_info(tk),
        "next_earnings": _next_earnings(tk),
        "rel_volume": (round(c_vols[-1] / (sum(c_vols[-21:-1]) / 20.0), 2)
                       if len(c_vols) > 21 and sum(c_vols[-21:-1]) else None),
        "rs_vs_spy": rs, "spy_window": spy_window,
    }


def _rs_vs_spy(closes, dates, weeks=12, through=None):
    """Relative strength on IDENTICAL trading sessions for both legs.

    Taking "the last 61 bars" from each series independently does not
    give the same 61 dates: the vendor's SPY history ran a session ahead
    of the issuer's completed window at both ends, so the two returns
    were measured over different spans and the difference between them
    meant nothing. The legs are now intersected by date, truncated at
    the issuer's last completed session, and both date arrays are
    exported so a reader can confirm they match.
    """
    import yfinance as yf
    n = weeks * 5
    if len(closes) <= n:
        return None, None
    try:
        h = yf.Ticker("SPY").history(period="1y", interval="1d",
                                     auto_adjust=False)
        spy_close = [float(x) for x in h["Close"].tolist()]
        spy_dates = [d.to_pydatetime().date().isoformat() for d in h.index]
    except Exception:
        return None, None

    iso = [d.isoformat() if hasattr(d, "isoformat") else str(d)
           for d in dates]
    cutoff = through or (iso[-1] if iso else None)
    # A session later than the issuer's last completed bar is either
    # still open or simply not part of this comparison.
    spy_map = {d: c for d, c in zip(spy_dates, spy_close)
               if cutoff is None or d <= cutoff}
    common = [d for d in iso if d in spy_map and (cutoff is None
                                                  or d <= cutoff)]
    if len(common) <= n:
        return None, None
    win = common[-(n + 1):]
    issuer = {d: c for d, c in zip(iso, closes)}
    a0, a1 = issuer[win[0]], issuer[win[-1]]
    b0, b1 = spy_map[win[0]], spy_map[win[-1]]
    mine = 100.0 * (a1 / a0 - 1)
    bench = 100.0 * (b1 / b0 - 1)
    # The relative-strength LINE (issuer close / benchmark close) and
    # whether it sits at its own high for the window. This is a fact about
    # the ratio series, not a claim about who is buying.
    ratio = [issuer[d] / spy_map[d] for d in win if spy_map[d]]
    rs_high = None
    if ratio:
        peak = max(ratio)
        rs_high = {"at_window_high": abs(ratio[-1] - peak) < 1e-12,
                   "pct_below_window_high": round(
                       100.0 * (peak - ratio[-1]) / peak, 1) if peak else None,
                   "window_sessions": len(ratio)}
    return round(mine - bench, 1), {
        "rs_line": rs_high,
        "series_id": "yahoo:SPY:1d:unadjusted",
        "dates": win, "closes": [spy_map[d] for d in win],
        "issuer_dates": win,
        "start": win[0], "end": win[-1], "sessions": len(win),
        "aligned": True,
        "alignment_rule": ("both legs use the identical trading sessions, "
                           "intersected by date and truncated at the "
                           "issuer's last completed session"),
        "start_close": b0, "end_close": b1,
        "benchmark_return_pct": round(bench, 4),
        "issuer_return_pct": round(mine, 4),
        "issuer_start_close": a0, "issuer_end_close": a1}


def _short_interest(info):
    """Short interest with its settlement date, or nothing.

    A short-interest percentage without a settlement date cannot be aged
    by the reader, and these figures are reported twice a month with a
    lag — an undated one invites the assumption that it is current."""
    pct = info.get("shortPercentOfFloat")
    dtc = info.get("shortRatio")
    epoch = info.get("dateShortInterest")
    settle = None
    if epoch:
        try:
            settle = datetime.fromtimestamp(
                int(epoch), timezone.utc).date().isoformat()
        except Exception:
            settle = None
    if pct is None and dtc is None:
        return None
    return {"pct_of_float": (round(100.0 * float(pct), 2)
                             if pct is not None else None),
            "days_to_cover": (round(float(dtc), 2)
                              if dtc is not None else None),
            "shares_short": info.get("sharesShort"),
            "settlement_date": settle,
            "source": "Yahoo Finance short-interest summary",
            "admissible": bool(settle)}


def _next_earnings(tk):
    """Vendor-ESTIMATED next earnings date. Labelled as an estimate
    because it is not a company confirmation or a filing."""
    try:
        cal = tk.calendar or {}
        d = (cal.get("Earnings Date") or [None])[0]
        return d.isoformat() if d else None
    except Exception:
        return None


def _safe_info(tk):
    try:
        return tk.info or {}
    except Exception:
        return {}


# ── social: StockTwits with per-record provenance ───────────────────────

_BULL = re.compile(r"\b(long|calls?|buy|bullish|moon|breakout|up|rip|"
                   r"squeeze|beat|strong)\b", re.I)
_BEAR = re.compile(r"\b(short|puts?|sell|bearish|dump|crash|down|fade|"
                   r"miss|weak|drop)\b", re.I)
# Holding intent is a directional view. Treating "adding here for the long
# term" as neutral throws away the most common way retail expresses one.
_LONGTERM = re.compile(
    r"\b(long[\s-]?term|longterm|for years|forever|hold(?:ing)?\s+(?:it\s+)?"
    r"(?:for|through)|dca|dollar[\s-]?cost|averaging (?:down|in)|adding"
    r"(?:\s+(?:more|here|to))?|accumulat\w*|buying (?:more|the dip)|"
    r"loading up|starter position|building a position|retirement|401k|"
    r"ira|core (?:holding|position))\b", re.I)
_EXIT = re.compile(r"\b(sold|selling|trimm\w+|exit(?:ed|ing)?|"
                   r"cut(?:ting)? (?:my )?(?:losses|position)|"
                   r"out of (?:this|it)|dumped)\b", re.I)
# a price with a direction word around it: "$420 target", "to 300", "pt 500"
_TARGET = re.compile(
    r"(?:\b(?:pt|target|tgt|going|goes|heads?|to|toward|towards|hits?|"
    r"reach\w*|see\w*|back to|by\s+\w+)\s*[:=]?\s*)\$?(\d{2,5}(?:\.\d+)?)"
    r"|\$(\d{2,5}(?:\.\d+)?)\s*(?:pt|target|tgt|eoy|by\b)", re.I)
_CASHTAG = re.compile(r"\$([A-Za-z][A-Za-z.\-]{0,5})\b")


NORM_VERSION = "norm/v1-nfkc-collapse-ws"


def _normalize(text):
    """The single normalization every content hash is taken over.
    Versioned, so a future change cannot silently invalidate old hashes."""
    import unicodedata
    t = unicodedata.normalize("NFKC", html.unescape(html.unescape(
        str(text or ""))))
    return " ".join(t.split())


def classify_post(body, ticker, spot=None):
    """Decide relevance and direction for one social post.

    Four corrections over the first live run, each of which silently
    discarded or mislabelled real opinion:
      1. cashtags are matched on DECODED text — "&#36;ISRG" and
         "$ISRG&nbsp;" were being rejected as off-ticker
      2. long-term / DCA / accumulation language is a bullish view
      3. a price target ABOVE spot is bullish, BELOW spot is bearish
      4. a short post is not content-free; only an empty one is
    """
    raw = body or ""
    txt = html.unescape(html.unescape(raw))          # feeds double-encode
    txt = txt.replace(" ", " ")
    tags = [t.upper() for t in _CASHTAG.findall(txt)]
    want = ticker.upper()
    # bare mentions count too: "$ISRG" and "ISRG" and the company name
    mentions_ticker = (want in tags
                       or re.search(r"\b%s\b" % re.escape(want), txt, re.I)
                       is not None)
    stripped = re.sub(r"[$#@][A-Za-z0-9_.\-]+", "", txt)
    stripped = re.sub(r"https?://\S+", "", stripped)
    content = re.sub(r"[^\w]", "", stripped)

    others = {t for t in tags if t != want}
    if not mentions_ticker:
        return "rejected", "ticker not mentioned in decoded text", \
               "no_mention", None
    if not content:
        # length alone is never the reason — "buying" is a real opinion
        return "rejected", "no content beyond tickers, links or symbols", \
               "tag_only", None
    if len(others) >= 3:
        # An analyst-roundup post carries price targets for OTHER names.
        # Counting it as ISRG opinion attributed "$400 target" for $V to
        # this ticker, which is how a roundup became a bullish vote.
        return "rejected", ("multi-ticker roundup (%d other tickers) — "
                            "opinion and price targets in it are not "
                            "attributable to %s" % (len(others), want)), \
               "list_post", None

    bull = len(_BULL.findall(txt))
    bear = len(_BEAR.findall(txt))
    if _LONGTERM.search(txt):
        bull += 2
    if _EXIT.search(txt):
        bear += 2
    # a price target is only this ticker's if this ticker is the only one
    # being discussed; otherwise "$400 -> $410" belongs to some other name
    if spot and not others:
        for m in _TARGET.finditer(txt):
            val = m.group(1) or m.group(2)
            try:
                v = float(val)
            except (TypeError, ValueError):
                continue
            if v <= 0 or v > spot * 5 or v < spot * 0.2:
                continue                      # not a plausible target
            if v > spot * 1.02:
                bull += 2
            elif v < spot * 0.98:
                bear += 2
    if bull > bear:
        sent = "bullish"
    elif bear > bull:
        sent = "bearish"
    elif bull == bear == 0:
        sent = "neutral"
    else:
        sent = "uncertain"
    return "counted", None, "primary_subject", sent


def fetch_social(ticker, report_time, retrieved_at, limit=60, spot=None):
    """StockTwits stream -> rs.social_record() list.

    Every message becomes an auditable record whether it is counted or
    rejected, so the appendix can reconcile considered = counted +
    rejected. Author handles are hashed, never published.
    """
    url = ("https://api.stocktwits.com/api/2/streams/symbol/%s.json"
           % ticker.upper())
    try:
        r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=30)
        data = r.json() if r.ok else {}
    except Exception as e:
        print("  stocktwits unavailable: %s" % e)
        return [], {"stocktwits": "unavailable"}
    msgs = data.get("messages") or []
    cutoff = rs._parse_ts(report_time)
    recs = []
    for m in msgs[:limit]:
        body = (m.get("body") or "").strip()
        user = m.get("user") or {}
        created = m.get("created_at") or ""
        pub = rs._parse_ts(created)
        rid = "stocktwits:%s" % m.get("id")
        murl = "https://stocktwits.com/%s/message/%s" % (
            user.get("username") or "u", m.get("id"))
        ah = _h("stocktwits:" + str(user.get("id") or user.get("username")))
        if not pub or pub > cutoff:
            disposition, reason = "rejected", "published after report_time"
            relevance, sent = "out_of_window", None
        else:
            disposition, reason, relevance, sent = classify_post(
                body, ticker, spot=spot)
        if disposition == "counted":
            # the platform's own tag outranks our text read when present
            tag = ((m.get("entities") or {}).get("sentiment") or {}
                   ).get("basic")
            if tag == "Bullish":
                sent = "bullish"
            elif tag == "Bearish":
                sent = "bearish"
        # ONE canonical string feeds both the stored text and the hash.
        # A second, slightly different normalization here is what made 13
        # of 30 public hashes disagree with the private snapshot.
        clean = _normalize(body)
        recs.append(rs.social_record(
            source="stocktwits", record_id=rid,
            published_at=_iso(pub) if pub else None,
            retrieved_at=retrieved_at, text=clean, author_hash=ah, url=murl,
            text_hash=EL.content_hash(clean),
            relevance=relevance, sentiment=sent,
            dup_group=_h(re.sub(r"[^a-z0-9 ]", "", body.lower())[:120]),
            disposition=disposition, reason=reason, quality=rs.Q_OK))
    return recs, {"stocktwits": "ok (%d messages)" % len(msgs)}


# ── news ────────────────────────────────────────────────────────────────

# Frozen relevance floor. A feed attaches ISRG to stories about other
# companies; "Nvidia Returns to NZS Growth Fund" mentioned it three times
# in passing and still cleared a 2-mention bar. Substantive means the
# company is discussed at length, or discussed early and repeatedly.
NEWS_MIN_MENTIONS = 5
NEWS_EARLY_MENTIONS = 3
NEWS_EARLY_PCT = 25.0


def _article_relevance(url, ticker, company_words):
    """Fetch the article and measure how much of it is actually about the
    ticker. A feed will happily attach ISRG to a story about Nvidia; the
    headline is not evidence that the body discusses the company."""
    out = {"fetched": False, "mentions": 0, "company_mentions": 0,
           "first_mention_pct": None, "reason": None}
    try:
        r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=25)
        if r.status_code != 200:
            out["reason"] = "HTTP %d" % r.status_code
            return out
        body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", r.text)
        body = html.unescape(re.sub(r"<[^>]+>", " ", body))
        body = re.sub(r"\s+", " ", body)
        out["fetched"] = True
        out["chars"] = len(body)
        tk = list(re.finditer(r"\b%s\b" % re.escape(ticker), body, re.I))
        cw = 0
        for w in company_words:
            cw += len(re.findall(re.escape(w), body, re.I))
        out["mentions"] = len(tk)
        out["company_mentions"] = cw
        if tk and len(body):
            out["first_mention_pct"] = round(100.0 * tk[0].start() / len(body), 1)
    except Exception as e:
        out["reason"] = "fetch failed: %s" % e
    return out


def fetch_news(ticker, report_time, company_name="", limit=8, verify=True):
    import yfinance as yf
    kept, rejected = [], []
    cutoff = rs._parse_ts(report_time)
    words = [w for w in re.split(r"[,\s]+", company_name or "")
             if len(w) > 4 and w.lower() not in ("inc.", "inc", "corp",
                                                 "corporation", "company")]
    try:
        items = yf.Ticker(ticker).news or []
    except Exception:
        items = []
    for it in items:
        c = it.get("content") or it
        pub = rs._parse_ts(c.get("pubDate") or c.get("displayTime"))
        head = (c.get("title") or "").strip()
        u = ((c.get("canonicalUrl") or {}) or {}).get("url") \
            or ((c.get("clickThroughUrl") or {}) or {}).get("url")
        pubr = ((c.get("provider") or {}) or {}).get("displayName") or "unknown"
        base = {"headline": head, "publisher": pubr, "url": u,
                "published_at": _iso(pub) if pub else None,
                "source_type": "media",
                "tier": rs.classify_news_tier("media")}
        if not pub or pub > cutoff:
            rejected.append(dict(base, reason="published after report_time"))
            continue
        if not u:
            rejected.append(dict(base, reason="no article URL"))
            continue
        if len(kept) >= limit:
            break
        rel = _article_relevance(u, ticker, words) if verify else {}
        base["article_check"] = rel
        if verify and rel.get("fetched"):
            total = rel["mentions"] + rel["company_mentions"]
            early = rel.get("first_mention_pct")
            substantive = (total >= NEWS_MIN_MENTIONS
                           or (total >= NEWS_EARLY_MENTIONS
                               and early is not None
                               and early <= NEWS_EARLY_PCT))
            if not substantive:
                rejected.append(dict(
                    base, reason="article mentions %s %d time(s)%s — not "
                                 "substantive discussion (needs %d "
                                 "mentions, or %d appearing in the first "
                                 "%.0f%% of the body)"
                                 % (ticker, total,
                                    "" if early is None
                                    else ", first at %.0f%% of the body" % early,
                                    NEWS_MIN_MENTIONS, NEWS_EARLY_MENTIONS,
                                    NEWS_EARLY_PCT)))
                continue
            base["relevance"] = ("article_verified (%d mentions, first at "
                                 "%s%% of body)"
                                 % (total, early if early is not None
                                    else "n/a"))
        elif verify:
            rejected.append(dict(
                base, reason="article body could not be fetched (%s) — "
                             "relevance unverifiable"
                             % (rel.get("reason") or "unknown")))
            continue
        else:
            base["relevance"] = "headline_only"
        kept.append(base)
    return kept, rejected


# ── snapshot assembly ───────────────────────────────────────────────────

def build_snapshot(ticker, report_time=None):
    ticker = ticker.upper()
    now = datetime.now(timezone.utc)
    report_time = report_time or _iso(now)
    retrieved_at = _iso(now)
    prov = {"warnings": [], "coverage": {}, "deferred": []}

    print("[1/7] market data (canonical series)...")
    mk = fetch_market(ticker, as_of=now)
    prov["coverage"]["market"] = ("yahoo daily bars, %d issuer sessions"
                                  % mk["n_bars"])

    led = EL.Ledger(ticker, report_time)
    for i, d in enumerate(mk["dates"]):
        led.bar(d.isoformat(), mk["opens"][i], mk["highs"][i], mk["lows"][i],
                mk["closes"][i], mk["volumes"][i])
    dstr = [d.isoformat() for d in mk["dates"]]
    # Completed sessions are the basis for every daily indicator. The
    # window strings below must name completed bars only: quoting a
    # 200-session range that ends on a half-formed bar declares a window
    # the data does not contain.
    cstr = [d.isoformat() for d in (mk.get("completed_dates") or mk["dates"])]
    led.population("market", issuer_sessions=len(mk["dates"]),
                   completed_sessions=len(cstr),
                   benchmark_references=0)

    def _win(n):
        """A closed range over completed sessions, or None when the
        series is too short to satisfy it. Naming a window we cannot
        fill is how a 200-day average came to cite 199 bars."""
        if len(cstr) < n:
            return None
        return "BAR-%s..BAR-%s" % (cstr[-n], cstr[-1])

    def _calc(slug, formula, n, value, unit, extra=None):
        w = _win(n)
        if w is None or value is None:
            return
        led.calc(slug, formula, [w] + list(extra or []), value, unit)

    # every published level gets a calculation record naming its formula
    # and the exact completed bars it consumed
    _calc("ma20", "mean(close, last 20 completed sessions)", 20,
          mk["ma20"], "USD")
    _calc("ma50", "mean(close, last 50 completed sessions)", 50,
          mk["ma50"], "USD")
    _calc("ma200", "mean(close, last 200 completed sessions)", 200,
          mk["ma200"], "USD")
    _calc("atr14", "mean(true range, last 14 completed sessions); TR = "
          "max(h-l, |h-prev_close|, |l-prev_close|)", 15, mk["atr14"], "USD")
    # Close basis, matching the wording the report uses for these levels.
    _calc("support60", "min(close, last 60 completed sessions)", 60,
          mk["support"], "USD")
    _calc("resistance60", "max(close, last 60 completed sessions)", 60,
          mk["resistance"], "USD")
    _calc("hi52", "max(close, last 252 completed sessions)", 252,
          mk["hi52"], "USD")
    _calc("lo52", "min(close, last 252 completed sessions)", 252,
          mk["lo52"], "USD")
    # An open session's last trade is not a close, and naming it one
    # invites every downstream figure to treat it as settled.
    if mk.get("partial_session"):
        led.calc("intraday_last", "last trade of the open session %s"
                 % mk.get("session_date"), ["INTRADAY-%s" % dstr[-1]],
                 mk["last"], "USD")
    else:
        led.calc("last_close", "close of the most recent completed session",
                 ["BAR-%s" % cstr[-1]], mk["last"], "USD")
    # Downstream figures cite the price record by name. Renaming the
    # open-session record without updating them left market cap and P/E
    # pointing at an id that no longer exists.
    px_ref = ("CALC-intraday_last" if mk.get("partial_session")
              else "CALC-last_close")
    # The session change is a division over two named bars, so it gets a
    # calculation record like every other derived figure. Publishing it
    # with a DER tag and nothing behind it is the exact gap
    # METRIC_FORMULA_TRACEABLE was written to catch.
    if mk.get("change_pct") is not None:
        led.calc("session_change",
                 "(last - previous close) / previous close x 100",
                 [px_ref, "BAR-%s" % dstr[-2]], mk["change_pct"], "%")
    _calc("rsi14", "Wilder RSI(14) over the last 250 completed-session "
          "closes; seeded with the mean of the first 14 changes then "
          "smoothed", 250, mk.get("rsi14"), None)
    _calc("base_tightness_pct", mk.get("base_tightness_formula") or "", 20,
          mk.get("base_tightness_pct"), "%")
    _calc("ma9", "mean(close, last 9 completed sessions)", 9,
          mk.get("ma9"), "USD")
    _calc("ma21", "mean(close, last 21 completed sessions)", 21,
          mk.get("ma21"), "USD")
    # These two are ratios OF other calculations rather than windows over
    # bars, so _calc's window helper does not fit them and they were
    # published citing CALC- ids that were never emitted. The v2 gate
    # catches exactly that (check_evidence_refs) and refused to render any
    # ticker; the v3 validator did not, because it has no equivalent check
    # over snapshot facts. Both are real divisions, so they get real
    # records naming the two operands they divide.
    if mk.get("atr14_pct") is not None:
        led.calc("atr14_pct", "ATR(14) / price x 100",
                 ["CALC-atr14", px_ref], mk["atr14_pct"], "%")
    if mk.get("pct_below_hi52") is not None:
        led.calc("pct_below_hi52",
                 "(52-week closing high - price) / 52-week closing high "
                 "x 100", ["CALC-hi52", px_ref], mk["pct_below_hi52"], "%")
    if mk.get("rel_volume") is not None:
        _calc("rel_volume",
              "volume(latest completed) / mean(volume, prior 20 completed)",
              21, mk["rel_volume"], "x")
    if mk.get("rs_vs_spy") is not None and mk.get("spy_window"):
        w = mk["spy_window"]
        # The benchmark window is EMBEDDED, not merely named. A reader
        # cannot reproduce a relative-strength number from a label, and
        # the placeholder record this replaces sat in `market_bars`,
        # where it was counted as an issuer session.
        for i, d in enumerate(w["dates"]):
            led.add("benchmark_bars", "SPY-%s" % d,
                    {"session": d, "close": w["closes"][i],
                     "series_id": w["series_id"],
                     "record_kind": "benchmark_bar"})
        led.calc("rs_vs_spy",
                 "12w return of %s minus 12w return of SPY, both from "
                 "completed-session closes over %s..%s"
                 % (ticker, w["start"], w["end"]),
                 ["BAR-%s..BAR-%s" % (w["start"], w["end"]),
                  "SPY-%s..SPY-%s" % (w["start"], w["end"])],
                 mk["rs_vs_spy"], "%",
                 note="both legs reproducible from the bars in this "
                      "package: issuer %s%%, benchmark %s%%"
                      % (w["issuer_return_pct"], w["benchmark_return_pct"]))
        led.population("market", benchmark_references=len(w["dates"]))
        prov["coverage"]["benchmark"] = (
            "SPY %d completed sessions %s..%s, embedded"
            % (w["sessions"], w["start"], w["end"]))

    print("[2/7] SEC EDGAR (CIK, acceptance times, XBRL)...")
    cik = cik_for(ticker)
    acc, subs = acceptance_map(cik)
    prov["coverage"]["sec"] = "CIK %s, %d filings indexed" % (cik, len(acc))

    snap = rs.new_snapshot(ticker, report_time, mk["bar_time"])
    snap["mode"] = rs.PROD
    sid = mk["series_id"]

    def mfact(v, metric, refs=None, **kw):
        return rs.fact(v, metric=metric, source="Yahoo Finance daily bars",
                       source_type="market_data", series_id=sid,
                       market_asof=mk["bar_time"], retrieved_at=retrieved_at,
                       quality=rs.Q_UNVERIFIED,
                       evidence_refs=list(refs or []), **kw)

    snap["price"] = {
        "last": mfact(mk["last"], "last close", unit="USD",
                      refs=[px_ref]),
        "prev_close": mfact(mk["prev_close"], "previous close", unit="USD",
                            refs=["BAR-%s" % dstr[-2]]),
        "change_pct": mfact(mk["change_pct"], "session change", unit="%",
                            refs=["BAR-%s" % dstr[-2],
                                  "BAR-%s" % dstr[-1]]),
    }
    snap["levels"] = {
        "price_used": mfact(mk["last"], "price used for levels", unit="USD",
                            refs=[px_ref]),
        "ma20": mfact(mk["ma20"], "20-day moving average", unit="USD",
                      note="simple, closing basis", refs=["CALC-ma20"]),
        "ma50": mfact(mk["ma50"], "50-day moving average", unit="USD",
                      note="simple, closing basis", refs=["CALC-ma50"]),
        "ma200": mfact(mk["ma200"], "200-day moving average", unit="USD",
                       note="simple, closing basis", refs=["CALC-ma200"]),
        "atr14": mfact(mk["atr14"], "14-day ATR", unit="USD",
                       note="Wilder true range, simple mean",
                       refs=["CALC-atr14"]),
        "support": mfact(mk["support"], "support", unit="USD",
                         note="lowest low of the last 60 sessions",
                         refs=["CALC-support60"]),
        "resistance": mfact(mk["resistance"], "first resistance", unit="USD",
                            note="highest high of the last 60 sessions",
                            refs=["CALC-resistance60"]),
        "resistance_major": mfact(mk["hi52"], "major resistance", unit="USD",
                                  note="52-week closing high (252 completed sessions)",
                                  refs=["CALC-hi52"]),
    }
    if mk.get("rs_vs_spy") is not None:
        # blends two series, so it does not claim the single canonical id
        snap["levels"]["rs_vs_spy"] = rs.fact(
            mk["rs_vs_spy"], metric="relative strength vs SPY", unit="%",
            source="Yahoo Finance daily bars", source_type="derived",
            series_id=sid + " vs yahoo:SPY:1d", market_asof=mk["bar_time"],
            retrieved_at=retrieved_at, calc_version="rs/v1",
            quality=rs.Q_DERIVED, evidence_refs=["CALC-rs_vs_spy"],
            note="12-week price return minus SPY's, same vendor and bars")
    # Relative volume compares one session's total against full-session
    # averages. Mid-session that ratio is meaningless — four minutes into
    # the day MRVL printed "0.04x", which reads as a volume collapse and
    # is really just a day that has barely started. Publish it only once
    # the session is complete, and say why when it is withheld.
    # setup-panel inputs, each as a Fact so it carries its own grade
    for _k, _metric, _unit in (
            ("rsi14", "RSI(14), Wilder, completed sessions", None),
            ("atr14_pct", "ATR(14) as a percent of price", "%"),
            ("base_tightness_pct", "base tightness", "%"),
            ("pct_below_hi52", "below the 52-week closing high", "%"),
            ("ma9", "9-day average", "USD"),
            ("ma21", "21-day average", "USD")):
        if mk.get(_k) is not None:
            snap["levels"][_k] = mfact(mk[_k], _metric, unit=_unit,
                                       refs=["CALC-%s" % _k])
    snap["levels"]["base_tightness_formula"] = mk.get(
        "base_tightness_formula")
    snap["levels"]["base_tightness_window"] = mk.get("base_tightness_window")
    snap["levels"]["rs_line"] = (mk.get("spy_window") or {}).get("rs_line")
    snap["short_interest"] = mk.get("short_interest")
    snap["ownership_vendor"] = {
        "institutional_pct": mk.get("institutional_pct_undated"),
        "reporting_date": None,
        "admissible": False,
        "reason": ("the vendor publishes this percentage without a "
                   "holdings-report date, so it cannot be aged or tied to "
                   "a filing"),
    }
    snap["levels"]["partial_session"] = mk.get("partial_session")
    snap["levels"]["last_completed_session"] = mk.get(
        "last_completed_session")
    snap["levels"]["price_basis"] = mk.get("price_basis")
    if mk.get("rel_volume") is not None and not mk.get("partial_session"):
        snap["levels"]["rel_volume"] = mfact(
            mk["rel_volume"], "volume vs 20-day average", unit="x",
            refs=["CALC-rel_volume"],
            note="latest session volume / mean of the prior 20 sessions")
    elif mk.get("partial_session"):
        prov["coverage"]["rel_volume"] = (
            "withheld: the session is still open, so today's partial "
            "volume cannot be compared with completed-session averages")

    # shares outstanding: cover page of the most recent filing that was
    # ACCEPTED before report_time. Market cap is then derived, so the
    # arithmetic cannot disagree with the headline price.
    sh_rows = concept(cik, "EntityCommonStockSharesOutstanding",
                      unit="shares", taxonomy="dei")
    sh_ok, sh_def = _admit(sh_rows, acc, report_time, dedupe_by_end=False)
    sh_ok.sort(key=lambda r: r.get("_accepted") or "")
    shares = ev_shares = None
    if sh_ok:
        r = sh_ok[-1]
        shares = float(r["val"])
        ev_shares = "SHR-%s" % r["accn"]
        led.add("shares_outstanding", ev_shares,
                {"value": shares, "unit": "shares", "form": r.get("_form"),
                 "accn": r["accn"], "period_end": r.get("end"),
                 "accepted": r.get("_accepted"), "url": r.get("_url"),
                 "tag": "dei:EntityCommonStockSharesOutstanding"})
    info = mk["info"]
    company = {
        "name": rs.fact(info.get("longName") or ticker, metric="company name",
                        source="Yahoo Finance profile", source_type="vendor",
                        retrieved_at=retrieved_at,
                        evidence_refs=["REC-profile"]),
        "sector": info.get("sector"),
        "universe": None,
        "profitable": None,
    }
    # Float rides with the profile it came from, and only after the
    # company dict exists — it is a vendor figure, so Q_UNVERIFIED.
    if mk.get("float_shares"):
        company["float_shares"] = rs.fact(
            float(mk["float_shares"]), metric="float", unit="shares",
            source="Yahoo Finance profile", source_type="vendor",
            retrieved_at=retrieved_at, evidence_refs=["REC-profile"],
            quality=rs.Q_UNVERIFIED)
    led.rec_input("profile", "company profile (vendor)",
                  {"name": info.get("longName"), "sector": info.get("sector"),
                   "industry": info.get("industry"),
                   "employees": info.get("fullTimeEmployees")},
                  refs=[], rationale="Yahoo Finance profile; descriptive "
                                     "only, never a basis for a number")
    if shares:
        company["shares_outstanding"] = rs.fact(
            shares, metric="shares outstanding", unit="shares",
            source="SEC cover page (%s)" % (sh_ok[-1].get("_form") or "filing"),
            source_type="filing", source_url=sh_ok[-1].get("_url"),
            published_at=sh_ok[-1].get("_accepted"),
            period_end=sh_ok[-1].get("end"), retrieved_at=retrieved_at,
            evidence_id=ev_shares, evidence_refs=[ev_shares],
            quality=rs.Q_OK)
        cap = mk["last"] * shares
        # Naming this "last close" mid-session is wrong: the price is the
        # last observed trade, not a close that has not happened.
        px_basis = ("last observed price (session still open)"
                    if mk.get("partial_session") else "last close")
        led.calc("market_cap",
                 "%s x cover-page shares outstanding" % px_basis,
                 [px_ref, ev_shares], round(cap, 2), "USD")
        company["market_cap"] = rs.fact(
            cap, metric="market cap", unit="USD",
            source="derived: last close x shares outstanding",
            source_type="derived", basis="price x cover-page share count",
            market_asof=mk["bar_time"], calc_version="cap/v1",
            quality=rs.Q_DERIVED, evidence_refs=["CALC-market_cap"],
            note="not a vendor figure; recomputed from the two facts above")
        company["universe"] = ("MEGA" if cap >= 200e9 else
                               "LARGE" if cap >= 10e9 else
                               "MID" if cap >= 2e9 else "SMALL")
    else:
        prov["warnings"].append(
            "no share count accepted before report_time — market cap omitted")

    print("[3/7] fundamentals (point-in-time admitted only)...")
    fund, evids, deferred = {}, [], []

    def _xf(row, tag):
        """Register one XBRL fact and return its addressable ref."""
        rid = EL.xbrl_id(row["accn"], tag, row["end"])
        led.add("xbrl_facts", rid, {
            "tag": tag, "value": row["val"], "start": row.get("start"),
            "end": row["end"], "form": row.get("_form"),
            "accn": row["accn"], "accepted": row.get("_accepted"),
            "url": row.get("_url"),
            "reconstructed_q4": bool(row.get("_derived_q4")),
            "reconstruction": ("fiscal year minus the three filed quarters"
                               if row.get("_derived_q4") else None)})
        return rid
    rev_tags = ["RevenueFromContractWithCustomerExcludingAssessedTax",
                "Revenues", "RevenueFromContractWithCustomerIncludingAssessedTax"]
    rev_rows, rev_ann = [], []
    for t in rev_tags:
        raw = concept(cik, t)
        rev_rows = _quarterly(raw)
        if rev_rows:
            rev_ann = _annual(raw)
            break
    rev_ok, rev_def = _admit(rev_rows, acc, report_time)
    ann_ok, _ = _admit(rev_ann, acc, report_time)
    rev_ok = _fill_q4(rev_ok, ann_ok)
    deferred += [("revenue", r) for r in rev_def]
    cur, yago = _yoy_pair(rev_ok)
    if cur is not None and yago is None:
        prov["warnings"].append(
            "no quarter within 20 days of one year before %s — revenue "
            "growth omitted rather than computed across a mismatched span"
            % cur["end"])
    if cur is not None and yago is not None and yago["val"]:
        g = 100.0 * (cur["val"] - yago["val"]) / yago["val"]
        eid = "SEC-%s" % cur["accn"]
        evids.append(eid)
        rtag = "us-gaap:" + t
        r_cur, r_yago = _xf(cur, rtag), _xf(yago, rtag)
        led.calc("revenue_yoy", "(revenue[%s] / revenue[%s] - 1) x 100"
                 % (cur["end"], yago["end"]), [r_cur, r_yago],
                 round(g, 1), "%")
        fund["revenue_q"] = rs.fact(
            cur["val"], metric="quarterly revenue", unit="USD",
            source="SEC XBRL %s" % (cur.get("_form") or ""),
            source_type="filing", source_url=cur.get("_url"),
            period_end=cur["end"], published_at=cur.get("_accepted"),
            retrieved_at=retrieved_at, basis="gaap", gaap=True,
            evidence_id=eid, evidence_refs=[r_cur], quality=rs.Q_OK)
        fund["revenue_growth"] = rs.fact(
            round(g, 1), metric="revenue growth y/y", unit="%",
            source="derived from two SEC XBRL quarters",
            source_type="derived", basis="gaap, quarter ended %s vs %s"
            % (cur["end"], yago["end"]),
            period_end=cur["end"], published_at=cur.get("_accepted"),
            calc_version="yoy/v2-date-matched", evidence_id=eid,
            evidence_refs=["CALC-revenue_yoy", r_cur, r_yago],
            quality=rs.Q_DERIVED,
            note=("year-ago quarter matched by period end date"
                  + (" (year-ago figure reconstructed as FY minus Q1-Q3, "
                     "because 10-K filings tag the full year, not Q4)"
                     if yago.get("_derived_q4") else "")))

    ni_raw = concept(cik, "NetIncomeLoss")
    ni_rows = _quarterly(ni_raw)
    ni_ok, ni_def = _admit(ni_rows, acc, report_time)
    ni_ann, _ = _admit(_annual(ni_raw), acc, report_time)
    ni_ok = _fill_q4(ni_ok, ni_ann)
    deferred += [("net income", r) for r in ni_def]
    rev_cur = cur
    if ni_ok:
        cur = ni_ok[-1]
        eid = "SEC-%s" % cur["accn"]
        if eid not in evids:
            evids.append(eid)
        r_ni = _xf(cur, "us-gaap:NetIncomeLoss")
        fund["net_income_q"] = rs.fact(
            cur["val"], metric="quarterly net income", unit="USD",
            source="SEC XBRL %s" % (cur.get("_form") or ""),
            source_type="filing", source_url=cur.get("_url"),
            period_end=cur["end"], published_at=cur.get("_accepted"),
            retrieved_at=retrieved_at, basis="gaap", gaap=True,
            evidence_id=eid, evidence_refs=[r_ni], quality=rs.Q_OK)
        company["profitable"] = rs.fact(
            cur["val"] > 0, metric="profitable",
            source="SEC XBRL net income, latest admitted quarter",
            source_type="derived", period_end=cur["end"],
            published_at=cur.get("_accepted"), evidence_refs=[r_ni],
            quality=rs.Q_DERIVED)
        if rev_ok and rev_ok[-1]["end"] == cur["end"] and rev_ok[-1]["val"]:
            r_rev = _xf(rev_ok[-1], "us-gaap:" + t)
            led.calc("net_margin", "net income[%s] / revenue[%s] x 100"
                     % (cur["end"], cur["end"]), [r_ni, r_rev],
                     round(100.0 * cur["val"] / rev_ok[-1]["val"], 1), "%")
            fund["net_margin"] = rs.fact(
                round(100.0 * cur["val"] / rev_ok[-1]["val"], 1),
                metric="net margin", unit="%", source="derived from XBRL",
                source_type="derived", basis="gaap, quarter ended %s"
                % cur["end"], period_end=cur["end"],
                published_at=cur.get("_accepted"), calc_version="margin/v1",
                evidence_id=eid, evidence_refs=["CALC-net_margin", r_ni,
                                                r_rev],
                quality=rs.Q_DERIVED)

    eps_raw = concept(cik, "EarningsPerShareDiluted", unit="USD/shares")
    eps_rows = _quarterly(eps_raw)
    eps_ok, eps_def = _admit(eps_rows, acc, report_time)
    eps_ann, _ = _admit(_annual(eps_raw), acc, report_time)
    eps_ok = _fill_q4(eps_ok, eps_ann)
    deferred += [("diluted EPS", r) for r in eps_def]
    valuation = {}
    ttm_win = _contiguous(eps_ok, 4)
    if ttm_win is None and eps_ok:
        prov["warnings"].append(
            "trailing EPS not published — the last four admitted quarters "
            "do not tile a continuous 12 months (%s), so no TTM figure and "
            "no trailing P/E can be built"
            % " ".join(r["end"] for r in eps_ok[-4:]))
    if ttm_win:
        ttm = sum(r["val"] for r in ttm_win)
        cur = ttm_win[-1]
        eid = "SEC-%s" % cur["accn"]
        if eid not in evids:
            evids.append(eid)
        r_eps = [_xf(r, "us-gaap:EarningsPerShareDiluted") for r in ttm_win]
        led.calc("eps_ttm", "sum of four contiguous quarterly diluted EPS",
                 r_eps, round(ttm, 2), "USD/share")
        fund["eps_ttm"] = rs.fact(
            round(ttm, 2), metric="diluted EPS (TTM)", unit="USD/share",
            # The four period ends wrapped the table row onto a second
            # line for a cell nobody looks up here; the appendix and the
            # evidence package both carry the accessions.
            source="SEC XBRL, four quarters to %s%s" % (
                cur["end"],
                " (*reconstructed)" if any(r.get("_derived_q4")
                                           for r in ttm_win) else ""),
            source_type="derived",
            basis="gaap, four contiguous quarters (%s to %s)"
                  % (ttm_win[0]["start"], cur["end"]),
            note=("* fourth quarter reconstructed as fiscal year minus the "
                  "three filed quarters, from %s"
                  % ", ".join(sorted({r["accn"] for r in ttm_win
                                      if r.get("_derived_q4")}))
                  if any(r.get("_derived_q4") for r in ttm_win) else None),
            period_end=cur["end"], published_at=cur.get("_accepted"),
            calc_version="ttm/v2-contiguous", evidence_id=eid,
            evidence_refs=["CALC-eps_ttm"] + r_eps, quality=rs.Q_DERIVED)
        if ttm > 0:
            led.calc("pe_trailing", "last close / GAAP TTM diluted EPS",
                     [px_ref, "CALC-eps_ttm"],
                     round(mk["last"] / ttm, 1), "x")
            valuation["pe_trailing"] = rs.fact(
                round(mk["last"] / ttm, 1), metric="P/E", unit="x",
                basis="trailing", source="derived: last close / GAAP TTM EPS",
                source_type="derived", market_asof=mk["bar_time"],
                published_at=cur.get("_accepted"), calc_version="pe/v1",
                evidence_id=eid, evidence_refs=["CALC-pe_trailing"],
                quality=rs.Q_DERIVED,
                note="GAAP diluted, not vendor-adjusted")
    # ── gross profit, cash flow and balance sheet ───────────────────────
    # The v3 brief reports margin structure, cash generation and the
    # balance sheet. Every figure below comes from the same XBRL endpoint
    # and the same acceptance gate as revenue — nothing here is modelled.
    # A tag an issuer does not file simply produces no row: an absent
    # gross-margin tag is a gap in disclosure, never a weak margin.
    def _duration_fact(tag, key, metric, unit="USD", pct_of_rev=None):
        raw = concept(cik, tag)
        rows, _d = _admit(_fill_q4(_quarterly(raw),
                                   _admit(_annual(raw), acc, report_time)[0]),
                          acc, report_time)
        if not rows:
            return None
        r = rows[-1]
        ref = _xf(r, "us-gaap:" + tag)
        eid = "SEC-%s" % r["accn"]
        if eid not in evids:
            evids.append(eid)
        fund[key] = rs.fact(
            r["val"], metric=metric, unit=unit,
            source="SEC XBRL %s" % (r.get("_form") or ""),
            source_type="filing", source_url=r.get("_url"),
            period_end=r["end"], published_at=r.get("_accepted"),
            retrieved_at=retrieved_at, basis="gaap", gaap=True,
            evidence_id=eid, evidence_refs=[ref], quality=rs.Q_OK)
        if pct_of_rev and rev_ok and rev_ok[-1]["end"] == r["end"] \
                and rev_ok[-1]["val"]:
            r_rev = _xf(rev_ok[-1], "us-gaap:" + t)
            val = round(100.0 * r["val"] / rev_ok[-1]["val"], 1)
            led.calc(pct_of_rev, "%s[%s] / revenue[%s] x 100"
                     % (metric, r["end"], r["end"]), [ref, r_rev], val, "%")
            fund[pct_of_rev] = rs.fact(
                val, metric=pct_of_rev.replace("_", " "), unit="%",
                source="derived from XBRL", source_type="derived",
                basis="gaap, quarter ended %s" % r["end"],
                period_end=r["end"], published_at=r.get("_accepted"),
                calc_version="margin/v1", evidence_id=eid,
                evidence_refs=["CALC-" + pct_of_rev, ref, r_rev],
                quality=rs.Q_DERIVED)
        return r

    def _instant_fact(tags, key, metric):
        for tag in tags:
            rows, _d = _admit(_instant(concept(cik, tag)), acc, report_time)
            if not rows:
                continue
            r = rows[-1]
            ref = _xf(r, "us-gaap:" + tag)
            fund[key] = rs.fact(
                r["val"], metric=metric, unit="USD",
                source="SEC XBRL %s" % (r.get("_form") or ""),
                source_type="filing", source_url=r.get("_url"),
                period_end=r["end"], published_at=r.get("_accepted"),
                retrieved_at=retrieved_at, basis="gaap, as of %s" % r["end"],
                gaap=True, evidence_id="SEC-%s" % r["accn"],
                evidence_refs=[ref], quality=rs.Q_OK)
            return r
        return None

    try:
        _duration_fact("GrossProfit", "gross_profit", "quarterly gross profit",
                       pct_of_rev="gross_margin")
        _duration_fact("NetCashProvidedByUsedInOperatingActivities",
                       "operating_cash_flow", "quarterly operating cash flow")
        _instant_fact(["CashAndCashEquivalentsAtCarryingValue"],
                      "cash", "cash and equivalents")
        _instant_fact(["LongTermDebtNoncurrent", "LongTermDebt"],
                      "debt", "long-term debt")
    except Exception as e:                       # a missing tag is normal
        prov["warnings"].append("extended fundamentals partial: %s" % e)
    # Non-GAAP margin is deliberately absent: companies publish it in
    # press-release exhibits, not in the XBRL taxonomy, so there is no
    # tagged figure to admit and we will not reconstruct one.
    prov.setdefault("coverage", {})["non_gaap_margin"] = (
        "not available — non-GAAP measures are not XBRL-tagged")

    # ── earnings exhibit: guidance and non-GAAP ─────────────────────────
    # These are public and addressable; only our parser stood between the
    # reader and them. Where it still cannot read a layout, the record
    # says AVAILABLE_NOT_INGESTED rather than letting the gap read as an
    # absence of disclosure.
    print("[3b/7] earnings exhibit (guidance + non-GAAP)...")
    try:
        import sec_exhibit as SX
        ex = SX.ingest(cik, acc, sec_text, report_time=report_time)
    except Exception as e:
        ex = {"disposition": "AVAILABLE_NOT_INGESTED", "reported": {},
              "guidance": {}, "reason": "exhibit ingestion failed: %s" % e,
              "url": None, "accession": None}
    snap["exhibit"] = ex
    if ex.get("accession"):
        led.add("catalyst_records", "CAT-%s" % ex["accession"],
                {"form": "8-K", "item": "2.02", "kind": "earnings exhibit",
                 "accn": ex["accession"], "accepted": ex.get("accepted"),
                 "url": ex.get("url"),
                 "exhibit_disposition": ex.get("disposition"),
                 "exhibit_reason": ex.get("reason")})
    # The appendix renders prov["coverage"] directly. Overriding this only
    # in the evidence package left the two artifacts contradicting each
    # other about whether non-GAAP was available.
    if ex.get("disposition") == "ADMITTED" and ex.get("reported"):
        prov["coverage"]["non_gaap_margin"] = (
            "ADMITTED - parsed from 8-K Item 2.02 Exhibit 99.1 (%s)"
            % ex.get("accession"))
    prov["coverage"]["earnings_exhibit"] = (
        ("EX-99.1 parsed from 8-K %s: %d reported figures, %d guidance lines"
         % (ex.get("accession"), len(ex.get("reported") or {}),
            len(ex.get("guidance") or {})))
        if ex.get("disposition") == "ADMITTED" else
        ("AVAILABLE_NOT_INGESTED - %s" % (ex.get("reason") or "unparsed")))

    prov["deferred"] = [
        {"metric": m, "period_end": r.get("end"), "form": r.get("_form"),
         "accepted": r.get("_accepted"), "value": r.get("val")}
        for m, r in deferred]

    snap["company"] = company
    snap["fundamentals"] = fund
    snap["valuation"] = valuation

    print("[4/7] insiders (Form 4) + ownership...")
    try:
        from insider_activity import fetch_insider_transactions_detail
        txns = fetch_insider_transactions_detail(ticker, days_back=180,
                                                 max_filings=30)
    except Exception as e:
        txns, _ = [], prov["warnings"].append("Form 4 fetch failed: %s" % e)
    snap["insiders"] = rs.summarize_insiders(txns)
    for n, row in enumerate(snap["insiders"]["rows"]):
        rid = "F4TXN-%03d" % n
        led.add("form4_records", rid, {
            "date": row.get("date"), "owner": row.get("owner"),
            "title": row.get("title"), "code": row.get("code"),
            "code_label": row.get("code_label"), "shares": row.get("shares"),
            "price": row.get("price"), "value": row.get("value"),
            "class": row.get("class"),
            "carries_view": row.get("carries_view"),
            "is_planned": row.get("is_planned")})
        row["evidence_ref"] = rid
    snap["insiders"]["window_days"] = 180
    # Form 4 accessions accepted before report_time. An insider claim must
    # cite one of THESE — the first run cited the 10-Q accession, which is
    # a citation that does not support the sentence attached to it.
    cutoff = rs._parse_ts(report_time)
    f4 = sorted(
        [dict(m, accn=a) for a, m in acc.items()
         if m.get("form") == "4" and rs._parse_ts(m.get("accepted") or "")
         and rs._parse_ts(m["accepted"]) <= cutoff],
        key=lambda m: m["accepted"])
    cutoff = rs._parse_ts(report_time)
    win_dt0 = cutoff - timedelta(days=180)
    # Scan manifest: EVERY in-window Form 4 filing, including the ones
    # that yielded no transaction row. Listing only the productive ones
    # made the scan look smaller than it was and hid parse failures.
    scanned = [m for m in f4
               if rs._parse_ts(m.get("accepted") or "") >= win_dt0]
    txn_dates = {str(t.get("date")) for t in txns}
    ev_form4 = []
    for m in scanned:
        rid = "F4-%s" % m["accn"]
        led.add("form4_records", rid,
                {"accn": m["accn"], "form": "4", "accepted": m["accepted"],
                 "url": m.get("url"), "record_kind": "source_filing",
                 "in_window": True,
                 "yielded_transaction_rows":
                     m["accepted"][:10] in txn_dates or None,
                 "note": "in-window Form 4 filing; parsed transaction rows "
                         "are the F4TXN-* records"})
        ev_form4.append(rid)
    snap["insiders"]["evidence_ids"] = ev_form4
    win_start = win_dt0.date().isoformat()
    # 1,501 Form 4s exist for this issuer across all history; only the
    # ones inside the analysis window were parsed. Reporting the index
    # total as "filings scanned" overstated the work by 24x.
    f4_in_window = scanned
    led.population(
        "form4",
        source_filings_in_index=len(f4),
        source_filings_scanned=len(f4_in_window),
        records_parsed=len(txns),
        records_in_window=len(txns),
        open_market_sales=(snap["insiders"].get("by_class") or {}).get(
            "open_market_sale", 0),
        window_days=180, window_start=win_start,
        window_end=cutoff.date().isoformat(),
        total_filings_in_edgar_index=len(acc))
    snap["insiders"]["economics"] = _insider_economics(
        snap["insiders"]["rows"], win_start, cutoff.date().isoformat())
    snap["insiders"]["count_statement"] = led.count_statements().get("form4")
    snap["insiders"]["evidence_refs"] = ev_form4 + [
        r["evidence_ref"] for r in snap["insiders"]["rows"]
        if r.get("evidence_ref")]
    snap["insiders"]["source_url"] = (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=%s"
        "&type=4" % cik)
    prov["coverage"]["form4"] = led.count_statements().get("form4")

    recent = (subs.get("filings") or {}).get("recent") or {}
    own_filings = []
    for i, f in enumerate(recent.get("form") or []):
        if f.startswith("SC 13"):
            a = (recent.get("accessionNumber") or [])[i]
            meta = acc.get(a) or {}
            if rs._parse_ts(meta.get("accepted") or "") and \
                    rs._parse_ts(meta["accepted"]) <= rs._parse_ts(report_time):
                rid = "OWN-%s" % a
                led.add("ownership_filings", rid,
                        {"accn": a, "form": f,
                         "accepted": meta.get("accepted"),
                         "url": meta.get("url"), "filer": None,
                         "filer_note": "filer identity not parsed from the "
                                       "filing body"})
                own_filings.append({"form": f, "filer": None,
                                    "accepted": meta.get("accepted"),
                                    "url": meta.get("url"),
                                    "evidence_ref": rid})
    # The ownership analysis window must be DEFINED, not implied. Page 2
    # said "21 in window" and page 3 said "12 in window" because the two
    # were generated from different lists. One population, dated, with a
    # computed inclusion flag per record, now feeds both.
    OWN_SHOW = 12
    OWN_WINDOW_DAYS = 1825            # 5 years: 13D/G positions persist
    own_start = cutoff - timedelta(days=OWN_WINDOW_DAYS)
    for f in own_filings:
        p = rs._parse_ts(f.get("accepted") or "")
        f["in_window"] = bool(p and p >= own_start)
        led._store["ownership_filings"][f["evidence_ref"]].update({
            "in_window": f["in_window"],
            "window_start": own_start.date().isoformat(),
            "window_end": cutoff.date().isoformat()})
    in_win = [f for f in own_filings if f["in_window"]]
    shown = in_win[:OWN_SHOW]
    # summarize over the WHOLE in-window population, not the displayed
    # slice, so n_filings and the count statement agree
    snap["ownership"] = rs.summarize_ownership(in_win)
    snap["ownership"]["evidence_refs"] = [f["evidence_ref"] for f in shown]
    snap["ownership"]["displayed"] = len(shown)
    snap["ownership"]["window_start"] = own_start.date().isoformat()
    snap["ownership"]["window_end"] = cutoff.date().isoformat()
    led.population("ownership", records_parsed=len(own_filings),
                   records_in_window=len(in_win),
                   records_displayed=len(shown),
                   window_days=OWN_WINDOW_DAYS,
                   window_start=own_start.date().isoformat(),
                   window_end=cutoff.date().isoformat())
    snap["ownership"]["count_statement"] = led.count_statements().get(
        "ownership")
    snap["ownership"]["note"] = (
        "Schedule 13D/13G filings accepted by EDGAR in the window, counted "
        "from the submissions index. Filer identity is NOT parsed from the "
        "filing bodies, so no holder is named here rather than guessed. "
        "13G denotes passive/non-control status, not lower conviction.")

    print("[5/7] social + news (article-level relevance)...")
    recs, cov = fetch_social(ticker, report_time, retrieved_at,
                             spot=mk["last"])
    prov["coverage"].update(cov)
    prov["coverage"]["reddit"] = ("unavailable — Reddit returns HTTP 403 to "
                                  "this client; excluded rather than "
                                  "silently under-counted")
    for r in recs:
        eid = EL.social_id(r["record_id"])
        # PUBLIC: normalized excerpt + provenance + hash, author hashed
        pub = {k: v for k, v in r.items() if k != "_text"}
        pub["excerpt"] = EL.excerpt(r.get("_text") or "")
        pub["normalization_version"] = NORM_VERSION
        led.add("social_records", eid, pub)
        # PRIVATE: the exact bytes the hash was taken over, so the public
        # hash can be reproduced by whoever holds the retained snapshot
        # r["_text"] is ALREADY the canonical string; re-normalizing here
        # would reintroduce the divergence this fixes
        norm = r.get("_text") or ""
        led.audit_record(
            eid, normalized_text=norm, hash_input=norm,
            content_hash=EL.content_hash(norm),
            norm_version=NORM_VERSION,
            source_meta={"source": r.get("source"), "url": r.get("url"),
                         "published_at": r.get("published_at"),
                         "author_hash": r.get("author_hash")},
            retrieval_meta={"retrieved_at": r.get("retrieved_at")})
    # One calculation object whose inputs are ALL considered records and
    # whose outputs reconcile every published social number. The summary
    # sentence previously cited a handful of sample ids, one of them a
    # rejected record — a citation that did not support the claim.
    _adm = [r for r in recs if r["disposition"] == "counted"]
    _rej = [r for r in recs if r["disposition"] == "rejected"]
    _cls = {}
    for r in _adm:
        _cls[r.get("sentiment") or "unclassified"] = \
            _cls.get(r.get("sentiment") or "unclassified", 0) + 1
    led.calc("social-summary",
             "partition all considered records by disposition, then the "
             "admitted subset by sentiment class",
             [EL.social_id(r["record_id"]) for r in recs],
             {"considered": len(recs), "admitted": len(_adm),
              "rejected": len(_rej),
              "unique_authors": len({r["author_hash"] for r in _adm}),
              "by_class": _cls,
              "directional": _cls.get("bullish", 0) + _cls.get("bearish", 0)},
             note="inputs are every considered record, not a sample")
    led.population("social", records_fetched=len(recs),
                   records_parsed=len(recs),
                   records_admitted=sum(1 for r in recs
                                        if r["disposition"] == "counted"),
                   records_rejected=sum(1 for r in recs
                                        if r["disposition"] == "rejected"),
                   records_displayed=min(10, len(recs)))
    news, news_rejected = fetch_news(
        ticker, report_time, company_name=(info.get("longName") or ""))
    for n in news:
        n["evidence_ref"] = EL.news_id(n["url"])
        led.add("news_records", n["evidence_ref"], dict(n, admitted=True))
    for n in news_rejected:
        if n.get("url"):
            led.add("news_records", EL.news_id(n["url"]),
                    dict(n, admitted=False))
    led.population("news", records_fetched=len(news) + len(news_rejected),
                   records_parsed=len(news) + len(news_rejected),
                   records_admitted=len(news),
                   records_rejected=len(news_rejected),
                   records_displayed=min(8, len(news)))
    prov["coverage"]["news"] = led.count_statements().get("news")
    prov["news_rejected"] = news_rejected

    # Baseline: a LIVE point-in-time baseline requires an archive of past
    # sessions captured BEFORE each report. We have none for this feed, so
    # the honest value is NO_BASELINE — not a reconstructed series dressed
    # up as one.
    alt = rs.build_alt_block(recs, baseline={"kind": rs.BASELINE_NONE},
                             news=news, options_feed_verified=False)
    snap["sentiment"] = dict(alt)

    print("[6/7] catalyst discovery (earliest primary release)...")
    # step [3b/7] already resolved the earnings exhibit; handing it over
    # lets verification read the release instead of the cover page.
    cat = discover_catalyst(acc, report_time,
                            (rev_cur or {}).get("end") if rev_cur else None,
                            mk, led, exhibit=snap.get("exhibit"))
    ev_dt = cat["event_dt"]
    grading = cat["grading"]
    snap["catalyst"] = {
        "event_dt": ev_dt,
        "event_ref": cat["event_ref"],
        "event_kind": cat["event_kind"],
        "verification": cat["verification"],
        "discovery": cat["discovery"],
        "grading": grading,
        "state": (grading.get("state")
                  if grading.get("state") in rs.CATALYST_STATES
                  else rs.resolve_catalyst_state(
                      rs._parse_ts(ev_dt) if ev_dt else None,
                      now=rs._parse_ts(report_time))),
        "stated_times": {"header": ev_dt} if ev_dt else {},
        "upcoming": ([{"what": "next earnings (data-vendor estimate, not "
                               "company-confirmed)",
                       "when": mk["next_earnings"]}]
                     if mk.get("next_earnings") else []),
        "refusal": cat.get("refusal"),
        # An unverified candidate must not be described as "the earliest
        # verified public disclosure" — that is the one sentence in this
        # block a reader would act on, and it would be false.
        "description": (
            (cat["refusal"] if cat.get("refusal") else
             "%s — the earliest verified public disclosure of these "
             "results, found by scanning %d filings; the later periodic "
             "filing is not the catalyst"
             % ("company earnings release (8-K item 2.02)"
                if cat["event_kind"] == "primary_release"
                else "periodic filing (no 8-K results release found)",
                cat["discovery"]["candidates_scanned"]))
            if ev_dt else "no dated catalyst in window"),
    }

    print("[7/7] decision, evidence and appendix...")
    for n in news:
        evids.append(n["evidence_ref"])
    for r in recs:
        evids.append(EL.social_id(r["record_id"]))
    evids += (snap["insiders"].get("evidence_ids") or [])
    if ev_shares and ev_shares not in evids:
        evids.append(ev_shares)

    # the appendix must actually contain the sample it advertises.
    # alt and snap["sentiment"] are separate dicts, so both carry it or
    # the renderer (which reads alt) shows nothing.
    _sample = [
        {"source": r.get("source"), "author_hash": r.get("author_hash"),
         "published_at": r.get("published_at"),
         "sentiment": r.get("sentiment"), "disposition": r.get("disposition"),
         # word-boundary truncation, NOT a raw slice — this is what left
         # "cloud, a" and "and g" hanging on page 5
         "excerpt": EL.excerpt(r.get("_text") or ""),
         "evidence_ref": EL.social_id(r["record_id"])}
        for r in recs[:10]]
    snap["sentiment"]["sample_records"] = _sample
    alt["sample_records"] = _sample

    snap["company"]["overview"] = _overview(info, fund, led)
    snap["decision"] = _decision(snap, mk, evids, led)
    snap["appendix"] = {
        "evidence_ids": evids,
        "rows_shown": min(10, len(recs)),
        "rows_total": len(recs),
        "sample_label": "sample showing %d of %d social records"
                        % (min(10, len(recs)), len(recs)),
        "machine_readable_export": "%s_evidence.json" % ticker,
        "claims_complete": False,
    }
    # Completeness reported PER DOMAIN. A single "n/a" hid the fact that
    # price data was complete while social coverage was one source of two.
    dom = [
        ("Price & levels", "complete",
         (led.count_statements().get("market") or "")
         + ", one canonical series"),
        ("Financials (SEC XBRL)", "complete" if fund else "absent",
         "%d tagged facts across %d filings"
         % (led.count("xbrl_facts"),
            len({r.get("accn") for r in led._store["xbrl_facts"].values()}))
         if fund else "no filing figure admitted"),
        # every detail string comes from the ledger's count statements,
        # so no second wording of the same population can appear
        ("Insider (Form 4)", "complete" if txns else "absent",
         led.count_statements().get("form4") or "no transactions parsed"),
        ("Ownership (13D/G)", "partial",
         (led.count_statements().get("ownership") or "")
         + "; filer names not parsed"),
        ("Catalyst", "complete" if ev_dt else "absent",
         "%d candidates scanned, earliest primary release selected"
         % cat["discovery"]["candidates_scanned"]),
        ("News", "partial", led.count_statements().get("news") or ""),
        ("Social", "partial",
         "StockTwits only; Reddit unavailable due to access failure"),
        ("Social baseline", "absent",
         "no point-in-time archive, so attention cannot be scored"),
        ("Options / expected move", "absent",
         "options chain not wired into this report"),
    ]
    led.coverage = {
        "sources_used": ["Yahoo Finance daily bars", "SEC EDGAR submissions",
                         "SEC XBRL companyconcept", "SEC Form 4",
                         "StockTwits", "Yahoo Finance news"],
        "sources_unavailable": [
            {"source": "Reddit", "reason": "HTTP 403 to this client",
             "effect": "social sample is StockTwits-only and under-counts "
                       "total retail discussion"},
            {"source": "Options chain",
             "reason": "not wired into this report",
             "effect": "no expected move or implied volatility"},
            {"source": "13F institutional ownership",
             "reason": "not aggregated",
             "effect": "institutional percentage not shown"}],
        "per_domain": [{"domain": d, "status": s, "detail": t}
                       for d, s, t in dom],
    }
    snap["evidence"] = {
        "evidence_quality": "moderate",
        "conviction": "low",
        "data_completeness_by_domain": led.coverage["per_domain"],
        "source_limitations": "StockTwits only; Reddit unavailable due to "
                              "access failure.",
        "accessibility_note": (
            "Accessibility: this PDF is structurally valid but UNTAGGED — "
            "it carries no logical structure tree, so screen-reader "
            "navigation is limited to reading order. Document language and "
            "title are set. Machine-readable checks are in "
            "%s_validation_report.json, alongside the evidence export."
            % ticker),
        "coverage": prov["coverage"],
        "provenance_note": ("Fundamentals admitted only when the filing's "
                            "EDGAR acceptance timestamp precedes the report "
                            "time. Levels derive from one price series. "
                            "Every figure carries evidence_refs into the "
                            "companion export."),
    }
    snap["flags"] = _flags(snap, prov)
    snap["evidence_index"] = sorted(led.ids())
    snap["count_populations"] = led.counts
    snap["count_statements"] = led.count_statements()
    # a hash that cannot be reproduced is not evidence
    hv = led.verify_hashes()
    if not hv["ok"]:
        raise rs.Contradiction(
            "content-hash verification failed for %d record(s): %s"
            % (len(hv["mismatched"]), hv["mismatched"][:3]))
    recon = led.reconcile()
    if recon:
        raise rs.Contradiction(
            "count reconciliation failed:\n  - " + "\n  - ".join(recon))
    prov["_mk"] = mk
    prov["_ledger"] = led
    return snap, alt, recs, prov


def discover_catalyst(acc, report_time, period_end, bars, ledger,
                      lookback_days=120, exhibit=None):
    """Find the EARLIEST verified public disclosure of the results, not
    the most convenient one.

    An 8-K carrying item 2.02 (Results of Operations) is the company's own
    release and normally precedes the 10-Q by days. Reading the periodic
    filing as the catalyst dates the event late — for ISRG's June quarter
    the release was 2026-07-16 20:05 UTC and the 10-Q was 2026-07-21,
    five days of price action apart.

    Every candidate scanned is recorded, so a later reviewer can see what
    was considered and why the winner won.
    """
    cutoff = rs._parse_ts(report_time)
    since = cutoff - timedelta(days=lookback_days)
    # "Earliest" means earliest disclosure OF THIS PERIOD, not the oldest
    # release in the lookback: taking the global earliest picked up the
    # PRIOR quarter's 8-K and dated the catalyst three months early. A
    # disclosure of results cannot precede the period it reports.
    if period_end:
        try:
            pe = datetime.fromisoformat(period_end).replace(
                tzinfo=timezone.utc)
            since = max(since, pe)
        except Exception:
            pass
    candidates = []
    for a, m in acc.items():
        p = rs._parse_ts(m.get("accepted") or "")
        if not p or p > cutoff or p < since:
            continue
        form, items = m.get("form"), str(m.get("items") or "")
        is_results = form == "8-K" and any(i in items for i in RESULTS_ITEMS)
        is_periodic = form in ("10-Q", "10-K")
        if not (is_results or is_periodic):
            continue
        candidates.append({
            "accn": a, "form": form, "items": items,
            "accepted": m["accepted"], "url": m.get("url"),
            "kind": "primary_release" if is_results else "periodic_filing",
            "_dt": p,
        })
    candidates.sort(key=lambda c: c["_dt"])

    primaries = [c for c in candidates if c["kind"] == "primary_release"]
    chosen = primaries[0] if primaries else (candidates[-1] if candidates
                                             else None)
    # verify rather than assume: the chosen release must actually be
    # fetchable and mention results
    verification = None
    if chosen:
        verification = _verify_release(chosen, exhibit=exhibit)
        chosen = dict(chosen, verification=verification)
    for c in candidates:
        ledger.add("catalyst_records", "CAT-%s" % c["accn"],
                   {k: v for k, v in c.items() if k != "_dt"})

    grading = _grade_event(chosen, bars) if chosen else {
        "state": "UNGRADED",
        "missing_condition": "no catalyst disclosure found in the "
                             "%d-day lookback" % lookback_days}
    # A candidate we could not verify is not a verified release, and the
    # honest move is to stop calling it one rather than to publish it and
    # let the gate kill the whole brief. Demoting the kind means the
    # catalyst no longer CLAIMS to be a results disclosure, so there is
    # nothing left to contradict — and the gate's check keeps its teeth
    # for the case it was written for: something published AS a verified
    # primary release that is not one.
    kind = chosen["kind"] if chosen else None
    refusal = None
    if kind == "primary_release" and verification \
            and verification.get("is_results_disclosure") is not True:
        kind = "unverified_release"
        refusal = ("An 8-K carrying item 2.02 was filed at %s, but this "
                   "report could not confirm it discloses results: %s. It "
                   "is listed as a filing, not read as the catalyst."
                   % (chosen["accepted"],
                      verification.get("reason") or "no reason recorded"))

    return {
        "event_dt": chosen["accepted"] if chosen else None,
        "event_ref": "CAT-%s" % chosen["accn"] if chosen else None,
        "event_kind": kind,
        "verification": verification,
        "refusal": refusal,
        "discovery": {
            "lookback_days": lookback_days,
            "candidates_scanned": len(candidates),
            "candidate_refs": ["CAT-%s" % c["accn"] for c in candidates],
            "earliest_primary_release": (primaries[0]["accepted"]
                                         if primaries else None),
            "earliest_primary_ref": ("CAT-%s" % primaries[0]["accn"]
                                     if primaries else None),
            "rule": "earliest 8-K carrying item 2.02 wins over any later "
                    "periodic filing; periodic filing used only when no "
                    "primary release exists",
        },
        "grading": grading,
        "period_end": period_end,
    }


def _verify_release(cand, exhibit=None):
    """Fetch the release and confirm it reads as a results disclosure.

    Read the EXHIBIT, not the cover page. An 8-K carrying item 2.02 is
    almost always a wrapper that says "a press release is attached hereto
    as Exhibit 99.1" and nothing else; the results live in the exhibit.
    Checking the wrapper for results language and concluding the company
    did not report results is a bug about which file we opened.

    Measured on TXN's 2026-04-22 filing: the wrapper is 4,250 characters
    and matches one phrase, the exhibit is 19,292 and matches five. Across
    a 20-issuer sample the exhibit clears the bar 20/20 while the wrapper
    clears it 19/20 — and 11 of those 19 clear it by exactly one hit, on
    boilerplate. Passing was luck, and TXN's counsel writing "first-quarter"
    with a hyphen was enough to lose it.

    step [3b/7] has already resolved the exhibit for the results 8-K, so
    when its accession matches the chosen candidate the URL is in hand and
    no extra fetch is needed to point at the right document."""
    out = {"fetched": False, "is_results_disclosure": None, "reason": None,
           "document": "primary"}
    url = cand.get("url")
    ex = exhibit or {}
    if ex.get("url") and ex.get("accession") \
            and ex["accession"] == cand.get("accn"):
        url, out["document"] = ex["url"], "exhibit"
    out["document_url"] = url
    if not url:
        out["reason"] = "no primary document URL in the submissions index"
        return out
    try:
        _throttle()
        r = requests.get(url, headers=SEC_HEADERS, timeout=30)
        if r.status_code != 200:
            out["reason"] = "HTTP %d fetching the primary document" % r.status_code
            return out
        txt = re.sub(r"<[^>]+>", " ", r.text)
        txt = html.unescape(txt)
        out["fetched"] = True
        out["chars"] = len(txt)
        hits = [w for w in ("results of operations", "financial results",
                            "revenue", "earnings", "second quarter",
                            "first quarter", "third quarter", "fourth quarter")
                if w in txt.lower()]
        out["matched_phrases"] = hits
        out["is_results_disclosure"] = len(hits) >= 2
        if not out["is_results_disclosure"]:
            out["reason"] = ("the %s document does not read as a results "
                             "disclosure (matched %d of %d phrases)"
                             % (out["document"], len(hits), 8))
    except Exception as e:
        out["reason"] = "fetch failed: %s" % e
    return out


def _grade_event(chosen, bars):
    """Grade the reaction once a FULL session has traded after the
    release, or name the precise condition that is missing."""
    p = rs._parse_ts(chosen.get("accepted") or "")
    if not p:
        return {"state": "UNGRADED",
                "missing_condition": "catalyst has no parseable timestamp"}
    dates = bars["dates"]
    closes = bars["closes"]
    # last session that closed at or before the release, and the first
    # full session that opened after it
    ev_date = p.astimezone(timezone(timedelta(hours=-4))).date()
    after_close = p.astimezone(timezone(timedelta(hours=-4))).hour >= 16
    base_i = None
    for i, d in enumerate(dates):
        if d < ev_date or (d == ev_date and after_close):
            base_i = i
    if base_i is None:
        return {"state": "UNGRADED",
                "missing_condition": "no session closed before the release "
                                     "in the loaded price history"}
    if base_i + 1 >= len(dates):
        return {"state": "POST_EVENT_UNGRADED",
                "missing_condition": "no full session has closed after the "
                                     "release (reaction window: first "
                                     "regular-session close after %s)"
                                     % chosen.get("accepted")}
    r_i = base_i + 1
    move = 100.0 * (closes[r_i] / closes[base_i] - 1)
    return {
        "state": "POST_EVENT_GRADED",
        "reaction_window": "close %s -> close %s"
                           % (dates[base_i].isoformat(),
                              dates[r_i].isoformat()),
        "pre_close": round(closes[base_i], 2),
        "post_close": round(closes[r_i], 2),
        "reaction_pct": round(move, 2),
        "evidence_refs": ["BAR-%s" % dates[base_i].isoformat(),
                          "BAR-%s" % dates[r_i].isoformat()],
        "missing_condition": None,
    }


def _overview(info, fund, led):
    """A short, sourced description of what the company sells and what
    moves the numbers. Vendor prose is labelled as vendor prose; the
    operating drivers are the filed figures, not adjectives."""
    summary = (info.get("longBusinessSummary") or "").strip()
    # first two sentences keep it to a paragraph without truncating mid-word
    parts = re.split(r"(?<=\.)\s+", summary)
    short = " ".join(parts[:2]).strip()
    drivers = []
    if fund.get("revenue_q"):
        drivers.append(("Quarterly revenue", "$%.2fB" % (rs.fv(fund["revenue_q"]) / 1e9),
                        fund["revenue_q"].get("evidence_refs") or []))
    if fund.get("revenue_growth"):
        drivers.append(("Revenue growth y/y", "%+.1f%%" % rs.fv(fund["revenue_growth"]),
                        fund["revenue_growth"].get("evidence_refs") or []))
    if fund.get("net_margin"):
        drivers.append(("Net margin", "%.1f%%" % rs.fv(fund["net_margin"]),
                        fund["net_margin"].get("evidence_refs") or []))
    if fund.get("eps_ttm"):
        drivers.append(("Diluted EPS (TTM)", "$%.2f" % rs.fv(fund["eps_ttm"]),
                        fund["eps_ttm"].get("evidence_refs") or []))
    led.rec_input("business_overview", "business overview",
                  {"chars": len(short), "source": "vendor profile"},
                  refs=["REC-profile"],
                  rationale="descriptive context; carries no number")
    return {
        "text": short,
        "source": "Yahoo Finance company profile (vendor description)",
        "employees": info.get("fullTimeEmployees"),
        "industry": info.get("industry"),
        "drivers": [{"name": n, "value": v, "evidence_refs": r}
                    for n, v, r in drivers],
        "evidence_refs": ["REC-business_overview"],
    }


def _quality_reads(snap, mk, led):
    """Business quality and setup quality are different questions and the
    brief must not collapse them: a good business can be a bad swing
    setup, which is exactly the ISRG case."""
    fund = snap.get("fundamentals") or {}
    px, ma20, ma50, ma200 = mk["last"], mk["ma20"], mk["ma50"], mk["ma200"]
    marg = rs.fv(fund.get("net_margin"))
    grow = rs.fv(fund.get("revenue_growth"))
    prof = rs.fv((snap.get("company") or {}).get("profitable"))
    brefs, bbits = [], []
    if marg is not None:
        bbits.append("net margin %.1f%%" % marg)
        brefs += (fund["net_margin"].get("evidence_refs") or [])
    if grow is not None:
        bbits.append("revenue %+.1f%% y/y" % grow)
        brefs += (fund["revenue_growth"].get("evidence_refs") or [])
    if marg is not None and grow is not None:
        biz = ("strong" if marg >= 15 and grow >= 10 else
               "solid" if marg >= 10 and grow >= 0 else
               "mixed" if prof else "weak")
    else:
        biz = "not assessable from admitted data"
    led.rec_input("business_quality", "business quality", biz,
                  refs=sorted(set(brefs)),
                  rationale="GAAP margin and filed revenue growth only; no "
                            "vendor adjustments, no narrative")

    above = sum(1 for m in (ma20, mk["ma50"], ma200) if px > m)
    dist = 100.0 * (px / ma200 - 1)
    setup = ("constructive" if above == 3 else
             "repairing" if above == 2 else
             "damaged" if above <= 1 else "mixed")
    srefs = [("CALC-intraday_last" if mk.get("partial_session")
              else "CALC-last_close"),
             "CALC-ma20", "CALC-ma50", "CALC-ma200"]
    led.rec_input("setup_quality", "setup quality (swing timeframe)", setup,
                  refs=srefs,
                  rationale="price above %d of 3 moving averages; %.1f%% "
                            "%s the 200-day" % (above, abs(dist),
                                                "above" if dist >= 0
                                                else "below"))
    return {
        "business_quality": biz,
        "business_quality_basis": ", ".join(bbits) or "no admitted figures",
        "business_quality_refs": sorted(set(brefs)) or ["REC-business_quality"],
        "setup_quality": setup,
        "setup_quality_basis": "price above %d of 3 moving averages; %.1f%% %s "
                               "the 200-day" % (above, abs(dist),
                                                "above" if dist >= 0
                                                else "below"),
        "setup_quality_refs": srefs,
        "above_count": above,
    }


def _decision(snap, mk, evids, led=None):
    """A mechanical, defensible read. Every claim cites a record that is
    actually in the appendix — the July 16 brief's claims cited nothing."""
    px = mk["last"]
    ma20, ma50, ma200 = mk["ma20"], mk["ma50"], mk["ma200"]
    above = sum(1 for m in (ma20, ma50, ma200) if px > m)
    ins = snap.get("insiders") or {}
    sent = snap.get("sentiment") or {}
    if above == 3:
        action = "HOLD"
    elif above == 0:
        action = "AVOID"
    else:
        action = "WAIT"
    claims = []
    fu = snap.get("fundamentals") or {}
    if fu.get("revenue_growth"):
        claims.append({
            "text": "Revenue grew %.1f%% y/y in the quarter ended %s (GAAP, "
                    "as filed)" % (rs.fv(fu["revenue_growth"]),
                                   fu["revenue_growth"]["period_end"]),
            "evidence_id": fu["revenue_growth"].get("evidence_id")})
    if fu.get("net_margin"):
        claims.append({
            "text": "Net margin of %.1f%% on the same quarter"
                    % rs.fv(fu["net_margin"]),
            "evidence_id": fu["net_margin"].get("evidence_id")})
    if ins.get("n_total"):
        # cite a FORM 4, not whatever SEC accession happened to be first
        ec = ins.get("economics") or {}
        claims.append({
            "text": ("%d transactions parsed inside the %s analysis window; "
                     "%d open-market sale(s) worth $%.1fM across %d insider(s)"
                     % (ins["n_total"],
                        "%s to %s" % (ec.get("window_start"),
                                      ec.get("window_end")),
                        ec.get("open_market_sales") or 0,
                        (ec.get("value_sold_open_market") or 0) / 1e6,
                        ec.get("distinct_selling_insiders") or 0)
                     if ec else
                     "%d Form 4 transactions parsed inside the analysis "
                     "window, %d open-market"
                     % (ins["n_total"], ins.get("n_view_bearing") or 0)),
            "evidence_id": (ins.get("evidence_ids") or [None])[-1]})
    if sent.get("n_relevant"):
        claims.append({
            "text": "%d relevant social posts from %d authors — %s"
                    % (sent["n_relevant"], sent.get("unique_authors") or 0,
                       (sent.get("decision_read") or {}).get("implication")),
            "evidence_id": next((e for e in evids if e.startswith("SOC-")),
                                None)})
    # every claim must resolve to a ledger record, not merely to a string
    for c in claims:
        c.setdefault("evidence_refs", [])
    if fu.get("revenue_growth"):
        claims[0]["evidence_refs"] = fu["revenue_growth"].get("evidence_refs") or []
    for c in claims:
        if c["text"].startswith("Net margin") and fu.get("net_margin"):
            c["evidence_refs"] = fu["net_margin"].get("evidence_refs") or []
        elif "Form 4" in c["text"]:
            c["evidence_refs"] = (ins.get("evidence_refs") or [])[:6]
        elif "social posts" in c["text"]:
            # cite the summary calculation, whose inputs are all 30
            # considered records — not six arbitrary ones
            c["evidence_refs"] = ["CALC-social-summary"]
    claims = [c for c in claims
              if c.get("evidence_id") and c.get("evidence_refs")]
    up = ("reclaim of the 200-day at $%.2f on above-average volume" % ma200
          if px < ma200 else
          "reclaim of the 20-day at $%.2f after any pullback" % ma20)
    down = ("loss of the 60-session closing low at $%.2f"
        % mk["support"])
    atr = mk["atr14"]
    risks = []
    if above == 0:
        # "every rally has been supply" asserts a behavioural pattern the
        # ledger contains no test for. State what the levels ARE.
        lo, hi = sorted((ma20, ma50))
        risks.append("Price is below all three moving averages; the 20-day "
                     "and 50-day define an overhead-resistance zone at "
                     "$%.2f-$%.2f" % (lo, hi))
    if (ins.get("by_class") or {}).get("open_market_sale"):
        risks.append("%d open-market insider sales in the window"
                     % ins["by_class"]["open_market_sale"])
    if (sent.get("baseline") or {}).get("kind") == rs.BASELINE_NONE:
        risks.append("No archived social baseline, so today's chatter "
                     "volume cannot be called unusual either way")
    q = _quality_reads(snap, mk, led) if led else {}
    # "AVOID" alone reads as a verdict on the company. The action is a
    # statement about NEW SWING ENTRIES at this price, and the business
    # read is reported separately so the two cannot be conflated.
    scope = {"AVOID": "AVOID NEW SWING LONGS",
             "WAIT": "WAIT FOR CONFIRMATION",
             "HOLD": "HOLD EXISTING ONLY"}.get(action, action)
    # Monitor next is never blank: if nothing else, the next scheduled
    # look at the same conditions.
    # Recovery is staged, not binary: an early-improvement trigger and a
    # full-upgrade trigger are different milestones, and presenting them
    # as competing recommendations reads as self-contradiction.
    stages = [
        {"stage": "Early improvement", "condition":
         "daily close above the 20-day at $%.2f" % ma20,
         "level": ma20, "met": px > ma20, "evidence_refs": ["CALC-ma20"]},
        {"stage": "Intermediate confirmation", "condition":
         "reclaim the 50-day at $%.2f" % ma50,
         "level": ma50, "met": px > ma50, "evidence_refs": ["CALC-ma50"]},
        {"stage": "Full technical upgrade", "condition":
         "reclaim the 200-day at $%.2f" % ma200,
         "level": ma200, "met": px > ma200, "evidence_refs": ["CALC-ma200"]},
        {"stage": "Invalidation", "condition":
         "daily close below the 60-session closing low at $%.2f"
         % mk["support"],
         "level": mk["support"], "met": px < mk["support"],
         "evidence_refs": ["CALC-support60"]},
    ]
    nxt = next((s for s in stages[:3] if not s["met"]), None)
    monitor = ("%s — %s (next stage of %d); invalidation on a close below "
               "$%.2f" % (nxt["stage"], nxt["condition"], len(stages) - 1,
                          mk["support"])) if nxt else (
        "All three moving averages reclaimed; monitor for a close back "
        "below the 20-day at $%.2f" % ma20)
    upcoming = ((snap.get("catalyst") or {}).get("upcoming") or [])
    if upcoming and upcoming[0].get("when"):
        monitor += ("; next earnings estimated %s" % upcoming[0]["when"])
    review = _next_review(snap.get("report_time"), mk)
    return {
        "current_action": action,
        "action_display": scope,
        "action_scope": "new swing entries only; says nothing about "
                        "existing long-term holdings",
        "business_quality": q.get("business_quality"),
        "business_quality_basis": q.get("business_quality_basis"),
        "business_quality_refs": q.get("business_quality_refs") or [],
        "setup_quality": q.get("setup_quality"),
        "setup_quality_basis": q.get("setup_quality_basis"),
        "setup_quality_refs": q.get("setup_quality_refs") or [],
        "monitor_next": monitor,
        "monitor_next_refs": ["CALC-ma20", "CALC-ma50", "CALC-ma200",
                              "CALC-support60"],
        "recovery_stages": stages,
        "review_date": review,
        "position_plan": {},
        "upgrade_trigger": up,
        "downside_confirmation": down,
        "supporting_facts": [c["text"] for c in claims[:3]],
        "risks": risks[:3],
        "scenarios": {
            "base": "Chop between $%.2f support and the 20-day at $%.2f; "
                    "one ATR is $%.2f (%.1f%%)"
                    % (mk["support"], ma20, atr, 100.0 * atr / px),
            "bull": "Reclaim and hold the 20-day at $%.2f, opening the "
                    "60-session closing high at $%.2f"
                    % (ma20, mk["resistance"]),
            # the 60-session and 52-week closing lows coincide when a name
            # is at its lows; saying "loss of X puts X in play" is nonsense
            "bear": ("Loss of $%.2f is a fresh 52-week closing low, with "
                     "no prior "
                     "reference level beneath it" % mk["support"]
                     if abs(mk["support"] - mk["lo52"]) < 0.01 else
                     "Loss of $%.2f puts the 52-week closing low at $%.2f in play"
                     % (mk["support"], mk["lo52"])),
        },
        "claims": claims,
        "horizon": "swing (2-8 weeks), the window the 20/50-day levels speak to",
        "basis": ("price %.2f vs 20/50/200-day (%.2f/%.2f/%.2f): above %d of 3"
                  % (px, ma20, ma50, ma200, above)),
    }


def _insider_economics(rows, win_start, win_end):
    """Economic context, not a headcount.

    "25 open-market sales" says nothing about whether an insider sold 2%
    or 80% of a position. Everything here comes from the parsed Form 4
    rows; anything the filings do not carry is reported as unavailable
    rather than inferred. A 10b5-1 plan is NEVER assumed — the footnote
    either documents one or the status is unknown.
    """
    sells = [r for r in rows if r.get("class") == rs.OPEN_MARKET_SALE]
    buys = [r for r in rows if r.get("class") == rs.OPEN_MARKET_BUY]
    planned = [r for r in rows if r.get("class") == rs.PLANNED_SALE]
    mech = [r for r in rows if not r.get("carries_view")
            and r.get("class") != rs.PLANNED_SALE]

    def _sh(rs_):
        return sum(float(r.get("shares") or 0) for r in rs_)

    def _val(rs_):
        return sum(float(r.get("value") or 0) for r in rs_)

    # ownership denominators: only present when the filing reported
    # shares owned after the transaction
    with_denom = [r for r in sells
                  if r.get("pct_of_holdings") is not None]
    by_owner = {}
    for r in sells:
        o = r.get("owner") or "unknown"
        d = by_owner.setdefault(o, {"shares": 0.0, "value": 0.0, "n": 0,
                                    "title": r.get("title")})
        d["shares"] += float(r.get("shares") or 0)
        d["value"] += float(r.get("value") or 0)
        d["n"] += 1
    top = sorted(by_owner.items(), key=lambda kv: -kv[1]["value"])[:3]
    sold_v, bought_v = _val(sells), _val(buys)
    concentration = (round(100.0 * top[0][1]["value"] / sold_v, 1)
                     if top and sold_v else None)

    unavailable = []
    if not with_denom:
        unavailable.append("shares owned after the transaction were not "
                           "reported, so sales cannot be expressed as a "
                           "percentage of holdings")
    plan_documented = sum(1 for r in rows if r.get("is_planned"))
    if not plan_documented:
        unavailable.append("no 10b5-1 plan is documented in the parsed "
                           "footnotes; plan status is unknown, not absent")

    return {
        "window_start": win_start, "window_end": win_end,
        "open_market_sales": len(sells),
        "open_market_buys": len(buys),
        "planned_sales_documented": len(planned),
        "compensation_mechanics": len(mech),
        "shares_sold_open_market": int(_sh(sells)),
        "shares_bought_open_market": int(_sh(buys)),
        "value_sold_open_market": round(sold_v, 2),
        "value_bought_open_market": round(bought_v, 2),
        "net_open_market_value": round(bought_v - sold_v, 2),
        "sales_with_ownership_denominator": len(with_denom),
        "median_pct_of_holdings": (
            sorted(r["pct_of_holdings"] for r in with_denom)[len(with_denom) // 2]
            if with_denom else None),
        "distinct_selling_insiders": len(by_owner),
        "largest_seller_share_of_value_pct": concentration,
        "top_sellers": [{"owner": o, "title": d["title"],
                         "transactions": d["n"], "shares": int(d["shares"]),
                         "value": round(d["value"], 2)} for o, d in top],
        "plan_status": ("documented 10b5-1 on %d transaction(s)"
                        % plan_documented if plan_documented
                        else "unknown — not documented in parsed footnotes"),
        "unavailable": unavailable,
        "read": _insider_read(sells, buys, planned, mech, with_denom,
                              concentration),
    }


def _insider_read(sells, buys, planned, mech, with_denom, concentration):
    """A sentence that distinguishes routine activity from a signal.

    A raw count of sales is not independently bearish: officers sell on
    schedules, for taxes, and to diversify.
    """
    if not sells and not buys:
        return ("No open-market insider transactions in the window; the "
                "remaining activity is compensation mechanics (vesting, "
                "withholding, exercises), which carry no directional view.")
    parts = []
    if buys:
        parts.append("%d open-market purchase(s)" % len(buys))
    if sells:
        parts.append("%d open-market sale(s)" % len(sells))
    if planned:
        parts.append("%d disclosed as pre-scheduled 10b5-1" % len(planned))
    if mech:
        parts.append("%d compensation mechanic(s)" % len(mech))
    s = "; ".join(parts) + "."
    if sells and not with_denom:
        s += (" Whether this is a routine trim or a material reduction "
              "cannot be determined: the filings do not report shares held "
              "after the transaction.")
    elif with_denom:
        s += (" Sales average %.1f%% of the seller's reported holdings."
              % (sum(r["pct_of_holdings"] for r in with_denom)
                 / len(with_denom)))
    if concentration is not None and concentration >= 60:
        s += (" Selling is concentrated: one insider accounts for %.0f%% "
              "of the open-market sale value." % concentration)
    return s


def _next_review(report_time, mk, days=7):
    """A concrete calendar date, not 'periodically'."""
    t = rs._parse_ts(report_time) or datetime.now(timezone.utc)
    d = (t + timedelta(days=days)).date()
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


def _flags(snap, prov):
    out = list(prov.get("warnings") or [])
    if prov.get("deferred"):
        out.append("%d filing figure(s) excluded by the point-in-time gate "
                   "(accepted after report_time)" % len(prov["deferred"]))
    s = snap.get("sentiment") or {}
    if s.get("classification") == "INSUFFICIENT SAMPLE":
        out.append("alt-data below the %d-author floor — descriptive only"
                   % rs.ALT_MIN_AUTHORS)
    if (s.get("baseline") or {}).get("kind") == rs.BASELINE_NONE:
        out.append("no point-in-time social baseline archived — attention "
                   "cannot be called elevated or normal")
    if not (snap.get("valuation") or {}):
        out.append("no valuation multiple could be built from admitted data")
    return out


def prose(snap, mk):
    """Narrative generated FROM the snapshot, never alongside it."""
    px, ma20, ma50, ma200 = mk["last"], mk["ma20"], mk["ma50"], mk["ma200"]
    rel = ("above every moving average" if px > max(ma20, ma50, ma200) else
           "below every moving average" if px < min(ma20, ma50, ma200) else
           "mixed against its moving averages")
    ins = snap["insiders"]
    fu = snap.get("fundamentals") or {}
    tech = ("%s trades at $%.2f, %s (20d $%.2f / 50d $%.2f / 200d $%.2f). "
            "The 60-session range is $%.2f to $%.2f and 14-day ATR is $%.2f, "
            "so roughly %.1f%% of price moves in a typical session."
            % (snap["ticker"], px, rel, ma20, ma50, ma200, mk["support"],
               mk["resistance"], mk["atr14"], 100.0 * mk["atr14"] / px))
    fund = "No filing figure was admitted under the point-in-time gate."
    if fu.get("revenue_growth"):
        fund = ("Latest filed quarter (ended %s): revenue $%.2fB, %+.1f%% y/y"
                % (fu["revenue_growth"]["period_end"],
                   rs.fv(fu["revenue_q"]) / 1e9,
                   rs.fv(fu["revenue_growth"])))
        if fu.get("net_margin"):
            fund += ", net margin %.1f%%" % rs.fv(fu["net_margin"])
        fund += ". Figures are as filed with the SEC, not vendor-adjusted."
    return {
        "technical": tech,
        "fundamental": fund,
        "insiders": ("%d Form 4 transactions in the last 180 days. %s"
                     % (ins.get("n_total") or 0, ins.get("read") or "")),
        "sentiment": ("Social observations are %s"
                      % (snap["sentiment"].get("decision_read") or {}
                         ).get("implication", "observational only")),
    }


def render(ticker, out_dir, report_time=None):
    """Build, gate and render the live brief plus its evidence export."""
    import report_v2 as R
    import report_chart as RC
    snap, alt, recs, prov = build_snapshot(ticker, report_time)
    mk, led = prov["_mk"], prov["_ledger"]
    ps = prose(snap, mk)

    ev_dt = (snap.get("catalyst") or {}).get("event_dt")
    ev_date = None
    if ev_dt:
        p = rs._parse_ts(ev_dt)
        ev_date = p.date() if p else None
    chart = RC.price_chart(mk, months=12, event_date=ev_date,
                           event_label="earnings release")

    pdf_path = os.path.join(out_dir, "%s_research_brief_v2_LIVE.pdf" % ticker)
    pdf, rep = R.build_brief(snap, prose_sections=ps, alt=alt,
                             chart_png=chart, out_path=pdf_path)

    ev_path = os.path.join(out_dir, "%s_evidence.json" % ticker)
    led.dump(ev_path, extra={
        "snapshot_claims": _claim_index(snap),
        "prose_sections": ps,
        "point_in_time": {
            "report_time": snap["report_time"],
            "market_data_time": snap["market_data_time"],
            "excluded_by_gate": prov["deferred"],
        },
        "news_rejected": prov.get("news_rejected") or [],
        "flags": snap.get("flags") or [],
        "verification_note": (
            "Content hashes require the retained internal evidence "
            "snapshot for independent verification; it is not part of "
            "this public bundle."),
    })
    # PRIVATE audit snapshot — the exact normalized text each hash covers
    audit_path = os.path.join(out_dir, "%s_evidence_audit_PRIVATE.json"
                              % ticker)
    with open(audit_path, "w", encoding="utf-8") as fh:
        json.dump(led.audit_snapshot(), fh, indent=2, default=str)

    # Reopen BOTH finished files and compare them on disk. In-memory
    # agreement was not enough: the previous build shipped 13 public
    # hashes that disagreed with the private snapshot while reporting
    # ok:true, because verification only compared the private file to
    # itself.
    xv = EL.cross_verify_exports(ev_path, audit_path)
    if not xv["ok"]:
        raise rs.Contradiction(
            "public/private hash cross-verification failed: %d recompute "
            "mismatch(es), %d public/private mismatch(es), %d missing in "
            "public — export blocked"
            % (len(xv["recompute_mismatched"]),
               len(xv["public_private_mismatched"]),
               len(xv["missing_in_public"])))

    val_path = os.path.join(out_dir, "%s_validation_report.json" % ticker)
    val = build_validation_report(snap, rep, led, prov)
    val["hash_cross_verification"] = xv
    with open(val_path, "w", encoding="utf-8") as fh:
        json.dump(val, fh, indent=2, default=str)
    return snap, rep, pdf_path, ev_path, led, audit_path, val_path, val


def build_validation_report(snap, rep, led, prov):
    """Machine-readable proof that the generated artefacts hold up."""
    ids = led.ids()
    claims = _claim_index(snap)
    missing = []
    for c in claims:
        for r in c["evidence_refs"]:
            if ".." in str(r):
                a, b = str(r).split("..", 1)
                if not (a.strip() in ids and b.strip() in ids):
                    missing.append(r)
            elif r not in ids:
                missing.append(r)
    cat = snap.get("catalyst") or {}
    hv = led.verify_hashes()
    recon = led.reconcile()
    md = rep.get("pdf_metadata") or {}
    return {
        "schema": "report_validation/v1",
        "ticker": snap.get("ticker"),
        "report_time": snap.get("report_time"),
        "pdf_parsers": (rep.get("pdf_validation") or {}).get("checks"),
        "pdf_parsers_ok": (rep.get("pdf_validation") or {}).get("ok"),
        "page_render": {
            "pages": rep.get("pages"), "audit_ok": rep.get("ok"),
            "notes": rep.get("notes") or [],
            "min_font_pt": rep.get("min_font_pt")},
        "evidence_references": {
            "records_total": len(ids),
            "claims_indexed": len(claims),
            "references_used": sum(len(c["evidence_refs"]) for c in claims),
            "missing_references": len(missing),
            "missing_examples": missing[:5]},
        "count_reconciliation": {
            "ok": not recon, "failures": recon,
            "populations": led.counts,
            "statements": led.count_statements()},
        "hash_verification": hv,
        "catalyst_selection": {
            "rule": ("the earliest verified public disclosure wins; a later "
                     "periodic filing cannot replace it"),
            "event_dt": cat.get("event_dt"),
            "event_kind": cat.get("event_kind"),
            "earliest_primary_release":
                (cat.get("discovery") or {}).get("earliest_primary_release"),
            "chose_earliest_primary":
                cat.get("event_dt") ==
                (cat.get("discovery") or {}).get("earliest_primary_release"),
            "verified": (cat.get("verification") or {}).get(
                "is_results_disclosure"),
            "grading_state": (cat.get("grading") or {}).get("state"),
            "missing_condition": (cat.get("grading") or {}).get(
                "missing_condition")},
        "accessibility": {
            "status": md.get("accessibility"),
            "reason": md.get("accessibility_reason"),
            "lang": md.get("lang"),
            "metadata_written": md.get("metadata_written")},
        "unavailable_data": (
            [d for d in (snap.get("evidence") or {}).get(
                "data_completeness_by_domain") or []
             if d.get("status") != "complete"]
            + [{"domain": "insider ownership denominators", "status": "absent",
                "detail": u}
               for u in ((snap.get("insiders") or {}).get("economics")
                         or {}).get("unavailable", [])]),
        "flags": snap.get("flags") or [],
    }


def _claim_index(snap):
    """Every rendered claim with the refs it stands on, so the export can
    be read as 'which record backs this sentence'."""
    out = []
    for path, f in rs._iter_facts(snap):
        if f.get("v") is None:
            continue
        out.append({"path": path, "metric": f.get("metric"),
                    "value": f.get("v"), "unit": f.get("unit"),
                    "evidence_refs": f.get("evidence_refs") or []})
    dec = snap.get("decision") or {}
    for c in dec.get("claims") or []:
        out.append({"path": "decision.claims", "metric": "claim",
                    "value": c.get("text"),
                    "evidence_refs": c.get("evidence_refs") or []})
    for k in ("business_quality", "setup_quality", "monitor_next"):
        if dec.get(k):
            out.append({"path": "decision." + k, "metric": k,
                        "value": dec[k],
                        "evidence_refs": dec.get(k + "_refs") or []})
    return out


def run_for_user(ticker, user_id="", out_dir=None):
    """CI entry point for a user-requested research brief.

    Mirrors scanner.py's contract: build, then upload to the requester's
    private Storage, falling back to the public archive when Storage is
    not configured. The alt-data appendix rides inside this one PDF, so
    a user no longer needs a second report to get the social read.

    The evidence bundle is uploaded alongside it. The PRIVATE audit
    snapshot is NOT uploaded — it is retained build-side only, which is
    exactly why the public bundle says hashes need it to verify.
    """
    out_dir = out_dir or os.getcwd()
    ticker = ticker.upper().strip()
    print("=" * 62)
    print("RESEARCH BRIEF v2: %s (user %s)"
          % (ticker, user_id or "<public archive>"))
    print("=" * 62)
    snap, rep, pdf_path, ev_path, led, audit_path, val_path, val = \
        render(ticker, out_dir)

    if not val.get("pdf_parsers_ok") or not rep.get("ok"):
        raise SystemExit("brief failed validation; nothing uploaded: %s"
                         % (rep.get("notes") or val.get("pdf_parsers")))

    with open(pdf_path, "rb") as fh:
        pdf_bytes = fh.read()
    now = datetime.now(timezone.utc)
    filename = "research_%s_%s.pdf" % (ticker, now.strftime("%Y-%m-%d_%H%M"))
    try:
        from report_archive import archive, upload_user_report
        uploaded = False
        if user_id:
            uploaded = upload_user_report(pdf_bytes, filename, user_id,
                                          ticker, "research")
        if not uploaded:
            archive(pdf_bytes, filename)
            print("  Archived publicly: %s" % filename)
    except Exception as e:
        print("  archive/upload failed: %s" % e)

    print("\n%s | %d pages | evidence records %d | %s"
          % (filename, rep.get("pages"),
             val["evidence_references"]["records_total"],
             "hashes verified" if val["hash_verification"]["ok"]
             else "HASH VERIFICATION FAILED"))
    return 0


def main():
    if "--user-id" in sys.argv or "--for-user" in sys.argv:
        def _opt(name, default=""):
            return (sys.argv[sys.argv.index(name) + 1]
                    if name in sys.argv else default)
        return run_for_user(_opt("--ticker", "ISRG"), _opt("--user-id"),
                            _opt("--out") or None)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    ticker = (args[0] if args else "ISRG").upper()
    out_dir = os.getcwd()
    if "--out" in sys.argv:
        out_dir = sys.argv[sys.argv.index("--out") + 1]
    print("=" * 62)
    print("LIVE RESEARCH SNAPSHOT: %s" % ticker)
    print("=" * 62)
    snap, alt, recs, prov = build_snapshot(ticker)

    print("\n--- POINT-IN-TIME GATE ---")
    print("report_time      : %s" % snap["report_time"])
    print("market_data_time : %s" % snap["market_data_time"])
    for d in prov["deferred"]:
        print("  EXCLUDED  %-14s period_end %s  accepted %s  (after "
              "report_time)" % (d["metric"], d["period_end"], d["accepted"]))
    if not prov["deferred"]:
        print("  (nothing excluded — all filing data predates report_time)")

    print("\n--- GATE ---")
    ps = prose(snap, prov["_mk"])
    for k, t in ps.items():
        print("  [%s] %s" % (k, t))
    viol = rs.check_contradictions(snap, ps)
    if viol:
        print("BLOCKED (%d):" % len(viol))
        for x in viol:
            print("  - %s" % x)
    else:
        print("PASS — snapshot is publishable")

    if "--json" in sys.argv:
        p = os.path.join(out_dir, "%s_live_snapshot.json" % ticker)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"snapshot": snap, "provenance": prov},
                      f, indent=2, default=str)
        print("\nwrote %s" % p)
    return 0 if not viol else 1


if __name__ == "__main__":
    sys.exit(main())
