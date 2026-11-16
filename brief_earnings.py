#!/usr/bin/env python3
"""brief_earnings.py — today's reporters, what the options market expects,
and what the stock has actually done.

Three numbers matter before a print: when it lands (before the open or
after the close), how big a move the options are paying for, and how that
compares with what the name usually does. The third is what turns the
first two into a decision — an implied 6.9% on a stock that typically
moves 3.7% is a different trade from the same 6.9% on one that moves 7%.

Implied move and IV come from the live chain via the public worker; the
realized history comes from the nightly earnings-edge file. Anything the
sources do not supply is left blank and labelled, never estimated.

The comparison is the site's (earnings_move_math.py): the priced move is
the EVENT-ONLY move for the report session (the straddle with the normal
days before its expiry stripped out), and the typical move is the MEAN
past reaction. The raw straddle is kept as `implied_raw_pct` and the
median past move as `realized_med_pct`, for context only.

    python brief_earnings.py --self-test
    python brief_earnings.py 2026-07-22        # live pull
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date

import earnings_edge
import earnings_move_math as EM

WORKER = "https://api.tickerdesk.io"
_BASE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(_BASE, "docs", "reports")

BMO, AMC = "BMO", "AMC"
# The schedule is long; the pane is a heads-up, not the calendar. Names are
# ranked by analyst coverage, which is the closest proxy the anticipated
# file carries for "the market cares about this one".
MAX_PER_SESSION = 4


def _load(name):
    try:
        with open(os.path.join(REPORTS, name), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def schedule(date_iso, anticipated=None):
    """(bmo, amc) name lists for one date, richest coverage first."""
    d = anticipated if anticipated is not None else _load("earnings_anticipated.json")
    for day in d.get("days") or []:
        if day.get("date") == date_iso:

            def rank(rows):
                return sorted(rows or [], key=lambda r: -(r.get("score") or 0))

            return rank(day.get("bmo")), rank(day.get("amc"))
    return [], []


def edge_index(edge=None):
    """ticker -> the nightly realized/implied record."""
    d = edge if edge is not None else _load("earnings_edge.json")
    return {r.get("t"): r for r in (d.get("names") or []) if r.get("t")}


def fetch_iv(ticker, timeout=15):
    """Live ATM straddle read. {} on any failure — a pane that cannot
    price one name must not take the brief down."""
    try:
        req = urllib.request.Request(
            "%s/?iv=%s" % (WORKER, urllib.parse.quote(ticker)),
            headers={"User-Agent": "TickerDesk-Brief/1.0", "Origin": "https://tickerdesk.io"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")) or {}
    except Exception:
        return {}


def _verdict(cmp):
    """Cheap / rich / fair, stated as the comparison itself so the reader
    can disagree with it. `cmp` is EM.priced_vs_typical's result."""
    if not cmp:
        return "", ""
    p, t, ratio = cmp["priced"], cmp["typical"], cmp["ratio"]
    if cmp["call"] is None:
        return "", (
            "priced move ±%.1f%% vs typical past move ±%.1f%%; one past report "
            "dominates the average, so no rich/cheap call" % (p, t)
        )
    label = {"rich": "RICH", "cheap": "CHEAP", "fair": "FAIR"}[cmp["call"]]
    why = "priced move ±%.1f%% vs typical past move ±%.1f%%" % (p, t)
    if label != "FAIR":
        why += " (%.2f×)" % ratio
    return label, why


def _live_event_move(iv, date_iso, session, daily_vol, read_date):
    """The live straddle restated as the report session's own move, with
    the same arithmetic the nightly file uses (earnings_edge.edge_read).
    Returns (implied_event or None, reason when None)."""
    raw = iv.get("implied_move_pct")
    if raw is None:
        return None, None
    read = earnings_edge.edge_read(
        raw, "straddle", iv.get("expiry"), read_date, date_iso, session, daily_vol
    )
    return read.get("implied_event"), read.get("implied_why")


def build(
    date_iso,
    watch_tickers=(),
    live=True,
    anticipated=None,
    edge=None,
    iv_fetch=None,
    max_per_session=MAX_PER_SESSION,
    read_date=None,
):
    """Records for the earnings pane, plus what could not be priced.

    read_date: the session a live chain read belongs to (default: now,
    via EM.read_session_date)."""
    bmo, amc = schedule(date_iso, anticipated)
    ei = edge_index(edge)
    watch = {t.upper() for t in (watch_tickers or [])}
    get_iv = iv_fetch if iv_fetch is not None else (fetch_iv if live else (lambda t: {}))

    rows, unpriced = [], []
    for session, names in ((BMO, bmo), (AMC, amc)):
        # a name the reader owns outranks a bigger name they do not
        ranked = sorted(
            names,
            key=lambda r: ((r.get("ticker") or "").upper() not in watch, -(r.get("score") or 0)),
        )
        for r in ranked[:max_per_session]:
            tk = (r.get("ticker") or "").upper()
            if not tk:
                continue
            e = ei.get(tk) or {}
            iv = get_iv(tk) or {}
            raw = iv.get("implied_move_pct")
            gap = None
            if raw is not None:
                if read_date is None:
                    read_date = EM.read_session_date()
                implied, gap = _live_event_move(
                    iv, date_iso, session, e.get("daily_vol"), read_date
                )
            else:
                # the nightly read, already on the like-for-like basis
                raw, implied, gap = e.get("implied"), e.get("implied_event"), e.get("implied_why")
            cmp = EM.priced_vs_typical(
                {
                    "implied_event": implied,
                    "fair_move": e.get("fair_move"),
                    "fair_move_dominated": e.get("fair_move_dominated"),
                }
            )
            verdict, why = _verdict(cmp)
            if raw is None:
                unpriced.append(tk)
            elif implied is None and gap:
                why = "no report-day priced move: %s" % gap
            rows.append(
                {
                    "ticker": tk,
                    "company": (r.get("company") or "").strip(),
                    "session": session,
                    "on_watchlist": tk in watch,
                    "analysts": r.get("analysts"),
                    "confirmed": bool(r.get("confirmed")),
                    "implied_move_pct": implied,
                    "implied_raw_pct": raw,
                    "iv_pct": iv.get("iv_pct"),
                    "iv_level": iv.get("iv_level"),
                    "expiry": iv.get("expiry"),
                    "dte": iv.get("dte"),
                    "typical_move_pct": e.get("fair_move"),
                    "realized_med_pct": e.get("realized_med"),
                    "n_reports": e.get("n_reports"),
                    "drift_5d_pct": e.get("drift_5d"),
                    "verdict": verdict,
                    "why": why,
                }
            )
    note = ""
    if unpriced:
        note = (
            "No liquid option read for %s; implied move left blank "
            "rather than estimated." % ", ".join(sorted(set(unpriced))[:6])
        )
    return {
        "date": date_iso,
        "records": rows,
        "note": note,
        "counts": {
            "bmo": len(bmo),
            "amc": len(amc),
            "bmo_shown": sum(1 for r in rows if r["session"] == BMO),
            "amc_shown": sum(1 for r in rows if r["session"] == AMC),
        },
    }


def self_test():
    fails, ran = [], [0]

    def chk(n, c, d=""):
        ran[0] += 1
        print(("  PASS  " if c else "  FAIL  ") + n + ("" if c else "  <- %s" % (d,)))
        if not c:
            fails.append(n)

    anticipated = {
        "days": [
            {
                "date": "2026-07-22",
                "dow": "Wednesday",
                "bmo": [
                    {
                        "ticker": "GEV",
                        "company": "GE Vernova",
                        "score": 52,
                        "analysts": 32,
                        "confirmed": True,
                    },
                    {
                        "ticker": "HAL",
                        "company": "Halliburton",
                        "score": 28,
                        "analysts": 26,
                        "confirmed": True,
                    },
                    {"ticker": "TINY", "company": "Tiny Co", "score": 1, "analysts": 1},
                ],
                "amc": [
                    {
                        "ticker": "COF",
                        "company": "Capital One",
                        "score": 40,
                        "analysts": 20,
                        "confirmed": True,
                    }
                ],
            }
        ]
    }
    edge = {
        "names": [
            {
                "t": "GEV",
                "implied": None,
                "fair_move": 5.1,
                "realized_med": 4.4,
                "daily_vol": 2.0,
                "n_reports": 8,
                "drift_5d": 1.2,
            },
            {
                "t": "COF",
                "implied": 6.9,
                "implied_event": 6.0,
                "fair_move": 3.7,
                "realized_med": 3.2,
                "n_reports": 9,
                "drift_5d": -0.4,
            },
            {"t": "HAL", "fair_move": 4.0, "realized_med": 3.5},
        ]
    }
    ivs = {
        "GEV": {
            "implied_move_pct": 9.5,
            "iv_pct": 237.1,
            "iv_level": "high",
            "expiry": "2026-07-24",
            "dte": 2,
        }
    }

    m = build(
        "2026-07-22",
        watch_tickers=["GEV"],
        anticipated=anticipated,
        edge=edge,
        iv_fetch=lambda t: ivs.get(t, {}),
        read_date=date(2026, 7, 21),
    )
    recs = {r["ticker"]: r for r in m["records"]}

    chk(
        "both sessions represented",
        {r["session"] for r in m["records"]} == {"BMO", "AMC"},
        [r["session"] for r in m["records"]],
    )
    chk(
        "a watch-list name is ranked first in its session",
        m["records"][0]["ticker"] == "GEV",
        m["records"][0]["ticker"],
    )
    chk("watch-list membership is flagged", recs["GEV"]["on_watchlist"])
    chk("live IV wins over the nightly implied", recs["GEV"]["implied_raw_pct"] == 9.5)
    chk(
        "the live read is restated as the report-day move (2 normal days stripped)",
        recs["GEV"]["implied_move_pct"] == 9.23,
        recs["GEV"]["implied_move_pct"],
    )
    chk("IV percentage carried", recs["GEV"]["iv_pct"] == 237.1)
    chk(
        "IV level and expiry carried", recs["GEV"]["iv_level"] == "high" and recs["GEV"]["dte"] == 2
    )
    chk("nightly implied used when the chain is silent", recs["COF"]["implied_move_pct"] == 6.0)
    chk("rich options are called rich", recs["COF"]["verdict"] == "RICH", recs["COF"])
    chk(
        "the verdict shows its arithmetic",
        "6.0" in recs["COF"]["why"] and "3.7" in recs["COF"]["why"],
        recs["COF"]["why"],
    )
    chk("GEV priced 9.2 vs 5.1 typical reads rich", recs["GEV"]["verdict"] == "RICH", recs["GEV"])
    chk(
        "an unpriceable name is disclosed, not estimated",
        recs["HAL"]["implied_move_pct"] is None and "HAL" in m["note"],
        m["note"],
    )
    chk("typical (mean) move carried", recs["COF"]["typical_move_pct"] == 3.7)
    chk("median kept as context", recs["COF"]["realized_med_pct"] == 3.2)
    chk(
        "session counts reported",
        m["counts"]["bmo"] == 3 and m["counts"]["bmo_shown"] == 3,
        m["counts"],
    )
    chk("no verdict is invented without both numbers", recs["HAL"]["verdict"] == "")

    empty = build("2026-01-01", anticipated=anticipated, edge=edge, iv_fetch=lambda t: {})
    chk("a day with no reporters yields no records", not empty["records"])

    print("\n%d/%d checks passed" % (ran[0] - len(fails), ran[0]))
    return 1 if fails else 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--self-test" in sys.argv or not args:
        raise SystemExit(self_test())
    print(json.dumps(build(args[0], args[1:]), indent=1)[:4000])
