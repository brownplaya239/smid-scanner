"""
earnings_edge.py — "trade, watch, or avoid this report?"

For every covered name reporting within the next 14 days (from the
uoa_meta_cache the Earnings tile already uses), compute the only earnings
numbers a trader can act on. Implied and realized are put on the SAME
basis (see earnings_move_math.py for the arithmetic and its assumptions):

  fair_move      MEAN |reaction move| over the last <=8 reports — what the
                 stock actually does on a report (close-before -> first
                 close after the report timestamp, BMO/AMC aware via
                 yfinance timestamps). A straddle prices a mean |move|, so
                 this is the like-for-like comparison. TickerDesk Fair Move.
  realized_med   median of the same moves — context only (it sits below the
                 mean and would make options look richer than they are)
  implied        RAW option read, kept for transparency: the ATM straddle
                 (% of spot) of the first expiry after the report, or the
                 flow scanner's 1-sigma IV move when no straddle could be
                 read (implied_src says which)
  implied_event  the raw read restated as the mean |move| of the report
                 session alone: non-earnings sessions between the read
                 close and the expiry are stripped out using recent daily
                 volatility (earnings days excluded)
  edge_ratio     implied_event / fair_move:  >=1.35 rich · <=0.85 cheap
  drift_5d       avg signed move from reaction close to +5 sessions — does
                 the move continue or fade?
  flow_into      count + direction of "Into ERN" tagged flow on the ledger
  verdict        Trade / Watch / Avoid — with the reason spelled out

Data: yfinance (keyless) for report history + daily bars; Polygon chain for
the straddle; repo JSON for the fallback read + flow. Runs in momentum.yml
post-close, non-fatal.

    python earnings_edge.py             # generate
    python earnings_edge.py --dry-run   # print, don't write
"""

from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from statistics import mean, median

import yfinance as yf

import earnings_move_math as EM

_BASE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(_BASE, "docs", "reports")
OUT_PATH = os.path.join(REPORTS, "earnings_edge.json")

HORIZON_D = 14  # names reporting within this many days
MAX_NAMES = 40  # runtime cap (2 yf calls per name)
MAX_REPORTS = 8  # realized history depth
RICH_RATIO = 1.35  # implied/realized >= this -> options rich
CHEAP_RATIO = 0.85  # <= this -> options cheap
MIN_REPORTS = 4  # minimum history before we trust realized stats
WORKERS = 8


def _load(name):
    try:
        with open(os.path.join(REPORTS, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def upcoming_names():
    """(ticker, earnings_date, session) reporting within HORIZON_D days,
    from the same meta cache the desk Earnings tile renders."""
    meta = _load("uoa_meta_cache.json") or {}
    today = datetime.now(UTC).date()
    out = []
    for t, m in meta.items():
        d = (m or {}).get("earnings_date")
        if not d:
            continue
        try:
            dt = datetime.strptime(d, "%Y-%m-%d").date()
        except Exception:
            continue
        days = (dt - today).days
        if 0 <= days <= HORIZON_D:
            out.append(
                {
                    "t": t,
                    "date": d,
                    "days": days,
                    "session": (m or {}).get("earnings_session") or "TBD",
                    "mcap": (m or {}).get("mkt_cap"),
                }
            )
    out.sort(key=lambda x: (x["days"], x["t"]))
    return out[:MAX_NAMES]


def flow_context():
    """Per-ticker option-move candidates + into-earnings flow from the live
    flow file. IMPORTANT: the scanner's expected_move_pct is the 1-sigma
    move over the CONTRACT'S remaining life (iv * sqrt(dte/365)) — only a
    front-expiry contract that just brackets the report is a fair proxy for
    the earnings-priced move, so we keep (em, dte) candidates and let the
    caller pick the bracketing expiry (or honestly report None).

    Also returns the session the scan's IVs belong to (its `generated`
    stamp), so a fallback read is dated by when it was priced."""
    uoa = _load("uoa_latest.json") or {}
    asof = None
    try:
        asof = EM.read_session_date(datetime.fromisoformat(uoa["generated"]))
    except KeyError, TypeError, ValueError:
        asof = None
    cands, into = {}, {}
    for r in uoa.get("rows") or []:
        t = r.get("ticker")
        if not t:
            continue
        em, dte = r.get("expected_move_pct"), r.get("dte")
        if em is not None and dte is not None:
            cands.setdefault(t, []).append((dte, em, r.get("trade_score") or 0, r.get("expiry")))
        if "Into ERN" in (r.get("tags") or []):
            e = into.setdefault(t, {"n": 0, "bull": 0, "bear": 0})
            e["n"] += 1
            d = (r.get("direction") or "").lower()
            if d == "bullish":
                e["bull"] += 1
            elif d == "bearish":
                e["bear"] += 1
    return cands, into, asof


def implied_direct(ticker, report_date, session, window=5):
    """Front-expiry ATM straddle straight from the Polygon chain — the
    same read the calendar's IV badge uses, computed here per reporter
    instead of hoping the name also fired a UOA signal (it almost never
    did, which left the implied column structurally dashed).

    An AMC print realizes at the NEXT session's open, so the contract
    must expire strictly after the report date; BMO can use same-day
    expiry. Only an expiry within `window` days after that is event-
    priced. Monthlies-only names return (None, nearest_expiry) so the
    gap can say WHY."""
    try:
        import polygon_data as PG

        if not PG.available():
            return None, None
        rows = PG.option_chain(ticker, max_pages=4)
    except Exception:
        return None, None
    if not rows:
        return None, None
    spot = None
    for r in rows:
        u = (r.get("underlying_asset") or {}).get("price")
        if u:
            spot = u
            break
    if not spot:
        return None, None
    try:
        rep = datetime.strptime(report_date, "%Y-%m-%d").date()
    except Exception:
        return None, None
    eff = rep if (session or "").upper() == "BMO" else rep + timedelta(days=1)
    lim = eff + timedelta(days=window)

    def px(c):
        lq = (c.get("last_quote") or {}).get("midpoint")
        if lq:
            return lq
        return (c.get("day") or {}).get("close") or (c.get("last_trade") or {}).get("price")

    by_exp = {}
    nearest = None
    for c in rows:
        det = c.get("details") or {}
        exp, k = det.get("expiration_date"), det.get("strike_price")
        if not exp or not k:
            continue
        if exp >= eff.isoformat() and (nearest is None or exp < nearest):
            nearest = exp
        if not (eff.isoformat() <= exp <= lim.isoformat()):
            continue
        by_exp.setdefault(exp, {}).setdefault(k, {})[det.get("contract_type")] = c
    for exp in sorted(by_exp):
        strikes = sorted(by_exp[exp], key=lambda k: abs(k - spot))
        for k in strikes[:3]:
            pair = by_exp[exp][k]
            cp, pp = px(pair.get("call") or {}), px(pair.get("put") or {})
            if cp and pp:
                return round((cp + pp) / spot * 100, 1), exp
    return None, nearest


def implied_for(cands, days_to_report, window=3):
    """The front-expiry read: only a contract expiring within `window` days
    AFTER the report is near-pure event pricing (longer-dated contracts add
    diffusion days + skew and systematically overstate the event move — the
    first cut with window=10 read JPM at ±5.8%, which no earnings straddle
    prices). No true bracketing contract -> (None, None), honestly.

    Returns (expected_move_pct, expiry). NOTE the value is a 1-SIGMA move
    (IV x sqrt(dte/365)), not a straddle: callers tag it src="iv_1sigma"
    so earnings_move_math puts it on the straddle's mean-|move| basis."""
    if not cands:
        return None, None
    ok = [c for c in cands if days_to_report <= c[0] <= days_to_report + window]
    if not ok:
        return None, None
    ok.sort(key=lambda x: (x[0], -x[2]))  # closest expiry, best score
    best = ok[0]
    return best[1], (best[3] if len(best) > 3 else None)


def reaction_moves(dates, closes, past):
    """(moves, drifts, reaction_idx) for report timestamps `past`.

    Reaction move = close strictly before the report -> first close after
    it, session-aware. `dates` are the bar dates, `closes` floats."""
    moves, drifts, react_idx = [], [], []
    n = len(closes)
    for ts in past:
        # SESSION-AWARE pairing. Daily bars are stamped at midnight, so a
        # naive "strictly before the report timestamp" comparison includes
        # the report day itself for BMO prints — measuring the day-AFTER
        # drift instead of the reaction (caught in validation: DAL's
        # reactions read ±1% when the true prints are several %).
        #   BMO (hour < 12): reaction = prev session close -> report-day close
        #   AMC (hour >= 12): reaction = report-day close -> next session close
        rd = ts.date()
        bmo = ts.hour < 12
        later = [i for i, d in enumerate(dates) if d >= rd]
        if not later:
            continue
        ri = later[0]  # report-day (or next session) bar
        if bmo:
            i0, i1 = ri - 1, ri
        else:
            i0, i1 = ri, ri + 1
        if i0 < 0 or i1 >= n:
            continue
        c0, c1 = float(closes[i0]), float(closes[i1])
        if not (c0 > 0 and c1 > 0):  # also rejects NaN
            continue
        moves.append(100.0 * (c1 - c0) / c0)
        react_idx.append(i1)
        if i1 + 5 < n:  # drift: reaction close -> +5 sessions
            c5 = float(closes[i1 + 5])
            if c5 > 0:
                drifts.append(100.0 * (c5 - c1) / c1)
    return moves, drifts, react_idx


def summarize_moves(moves, drifts):
    """Fair Move (mean |move|) + context stats, or None below MIN_REPORTS."""
    if len(moves) < MIN_REPORTS:
        return None
    absm = [abs(m) for m in moves]
    avg = round(mean(absm), 1)
    return {
        "fair_move": avg,  # TickerDesk Fair Move: MEAN |reaction|
        "realized_avg": avg,  # same number, legacy key
        "realized_med": round(median(absm), 1),  # context only
        "n_reports": len(moves),
        # one report moved more than all the others combined: the mean is
        # that one event, so no rich/cheap call is made from it
        "fair_move_dominated": EM.dominated_by_one(absm),
        "max_reaction": round(max(absm), 1),
        "drift_5d": round(mean(drifts), 1) if drifts else None,
    }


def realized_stats(ticker):
    """Report history + daily volatility for one ticker.

    Returns a dict (or None when Yahoo returned nothing usable):
      daily_vol   RMS daily move (%) over the last ~40 sessions with
                  earnings reaction days (and the day after) left out;
                  needed to strip normal days out of the option read
      fair_move / realized_med / n_reports / drift_5d   only with
                  >= MIN_REPORTS usable reports"""
    tk = yf.Ticker(ticker)
    hist = tk.history(period="2y", interval="1d", auto_adjust=True)
    if hist is None or hist.empty:
        return None
    closes = [float(x) for x in hist["Close"]]
    dates = [d.date() for d in hist.index]
    past = []
    try:
        ed = tk.get_earnings_dates(limit=MAX_REPORTS + 6)
        if ed is not None and not ed.empty:
            now = datetime.now(UTC)
            past = sorted((ts for ts in ed.index if ts.tz_convert("UTC") < now), reverse=True)[
                :MAX_REPORTS
            ]
    except Exception:
        past = []
    moves, drifts, react_idx = reaction_moves(dates, closes, past)
    exclude = set(react_idx) | {i + 1 for i in react_idx}
    # normal-day volatility up to the read close (earnings days excluded)
    out = {"daily_vol": EM.daily_vol_pct(closes, exclude_idx=exclude)}
    s = summarize_moves(moves, drifts)
    if s:
        out.update(s)
    return out


def edge_read(raw, src, expiry, read_date, report_date, session, daily_vol):
    """The like-for-like option read for one report. Pure; see
    earnings_move_math for the arithmetic."""
    out = {
        "implied": raw,
        "implied_src": src if raw is not None else None,
        "implied_expiry": expiry,
        "implied_asof": read_date.isoformat() if read_date else None,
        "implied_event": None,
        "nonevent_sessions": None,
    }
    if raw is None:
        return out
    k = None
    if expiry and read_date:
        try:
            k = EM.nonevent_sessions(read_date, expiry, report_date, session)
        except ValueError:
            k = None
    if not read_date:
        out["implied_why"] = "time of the option read unknown"
        return out
    if not EM.is_pre_event(read_date, report_date, session):
        out["implied_why"] = "read taken after the report"
        return out
    ev = EM.event_move(raw, src, k, daily_vol)
    out["nonevent_sessions"] = k
    out["implied_event"] = ev.get("implied_event")
    out["implied_straddle_equiv"] = ev.get("straddle_equiv")
    out["nonevent_var_share"] = ev.get("nonevent_var_share")
    if ev.get("why"):
        out["implied_why"] = ev["why"]
    return out


def verdict_for(implied, stats, flow):
    """Transparent Trade / Watch / Avoid + the reason, no black box.

    `implied` is the EVENT-ONLY mean |move| (implied_event), compared with
    the Fair Move (mean |reaction|) — the same quantity on both sides."""
    if not stats or stats.get("fair_move") is None:
        return "Watch", "not enough report history to price the move"
    fair = stats["fair_move"]
    med = stats.get("realized_med")
    if implied is None:
        return "Watch", (
            f"averages ±{fair}% on reports — no usable event-only option read to compare against"
        )
    ratio = round(implied / fair, 2) if fair else None
    ctx = f" (median ±{med}%)" if med is not None else ""
    if stats.get("fair_move_dominated"):
        return "Watch", (
            f"report move priced at ±{implied}%; the ±{fair}% average is dominated by "
            f"one ±{stats.get('max_reaction')}% reaction{ctx} — no rich/cheap call"
        )
    fl = flow or {}
    fdir = (
        "bullish"
        if fl.get("bull", 0) > fl.get("bear", 0)
        else "bearish"
        if fl.get("bear", 0) > fl.get("bull", 0)
        else None
    )
    if ratio is not None and ratio >= RICH_RATIO:
        return "Avoid", (
            f"options rich: report move priced at ±{implied}% vs ±{fair}% "
            f"average reaction{ctx} ({ratio}×) — long premium overpays; "
            f"spreads only"
        )
    if ratio is not None and ratio <= CHEAP_RATIO:
        why = (
            f"options cheap: report move priced at ±{implied}% vs ±{fair}% "
            f"average reaction{ctx} ({ratio}×)"
        )
        if fdir:
            return "Trade", why + f" + {fdir} flow into the report"
        return "Trade", why + " — long premium has the edge"
    why = f"report move priced at ±{implied}% ≈ ±{fair}% average reaction{ctx} — fairly priced"
    if fdir and fl.get("n", 0) >= 2:
        return "Trade", why + f"; {fl['n']} {fdir} into-earnings prints"
    return "Watch", why


def build():
    names = upcoming_names()
    cands_by, into_by, flow_asof = flow_context()
    print(f"  Earnings edge: {len(names)} names reporting ≤{HORIZON_D}d")
    results = []
    # The session tonight's chain snapshot belongs to (the scheduled job runs
    # post-close; an intraday run is stamped today, so a report earlier
    # today can never look "before" it). Fallback reads carry the flow
    # scan's own session.
    read_date = EM.read_session_date()

    def work(n):
        try:
            stats = realized_stats(n["t"])
        except Exception as e:
            print(f"    {n['t']}: history failed ({str(e)[:60]})")
            stats = None
        stats = stats or {}
        raw, exp = implied_direct(n["t"], n["date"], n["session"])
        src, asof = "straddle", read_date
        nearest = None
        if raw is None:
            nearest = exp  # nearest listed expiry, which does not bracket the print
            # fallback: a UOA contract that brackets the report (1-sigma IV move)
            raw, exp = implied_for(cands_by.get(n["t"]), n["days"])
            src, asof = "iv_1sigma", flow_asof
        read = edge_read(raw, src, exp, asof, n["date"], n["session"], stats.get("daily_vol"))
        flow = into_by.get(n["t"])
        v, why = verdict_for(read["implied_event"], stats, flow)
        if read["implied_event"] is None:
            gap = read.get("implied_why")
            if raw is None and nearest:
                gap = "nearest expiry %s does not bracket the print (no weeklies)" % nearest
            if gap:
                why = why.replace("no usable event-only option read to compare against", gap)
        row = {**n, **read, "flow_into": flow, "verdict": v, "why": why}
        if raw is None and nearest:
            row["implied_expiry"] = nearest  # shown as the reason for the gap
        row.update(stats)
        if read["implied_event"] is not None and stats.get("fair_move"):
            row["edge_ratio"] = round(read["implied_event"] / stats["fair_move"], 2)
        return row

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for row in ex.map(work, names):
            results.append(row)
    results.sort(key=lambda r: (r["days"], r["t"]))
    counts = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print("  verdicts: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    return {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "horizon_days": HORIZON_D,
        "count": len(results),
        "names": results,
        "read_date": read_date.isoformat(),
        "params": {
            "rich_ratio": RICH_RATIO,
            "cheap_ratio": CHEAP_RATIO,
            "max_reports": MAX_REPORTS,
            "min_reports": MIN_REPORTS,
            "daily_vol_window": EM.DAILY_VOL_WINDOW,
        },
        "method": METHOD,
    }


# Published with the payload so every number on the page can be traced.
METHOD = {
    "fair_move": "TickerDesk Fair Move = MEAN |reaction| over the last <=8 "
    "reports (close before the report -> first close after it, BMO/AMC "
    "aware). realized_med (the median of the same moves) is context only. "
    "When one report moved more than all the others combined "
    "(fair_move_dominated) no rich/cheap call is made.",
    "implied": "RAW option read: ATM straddle mid of the first expiry after "
    "the report as % of spot (implied_src=straddle), or the flow "
    "scanner's 1-sigma IV move (implied_src=iv_1sigma) when no straddle "
    "could be read. Covers every session from the read close to the expiry.",
    "implied_event": "the raw read restated as the mean |move| of the report "
    "session alone: sigma_total = straddle/0.798 (a 1-sigma read is used "
    "as is); sigma_event^2 = sigma_total^2 - k * daily_vol^2 with k = "
    "nonevent_sessions (sessions from the read close to the expiry, minus "
    "the report session) and daily_vol = RMS daily move over the last "
    "~40 sessions with earnings reaction days left out and one-off "
    "catalyst gaps clipped at 3 robust standard deviations; implied_event "
    "= 0.798 * sigma_event.",
    "edge_ratio": "implied_event / fair_move (1.00 = fairly priced); >= "
    f"{RICH_RATIO} rich, <= {CHEAP_RATIO} cheap.",
    "assumptions": [
        (
            "moves are treated as normal when converting between a mean |move| "
            "and a sigma (E|X| = 0.798 sigma)"
        ),
        (
            "recent realized daily volatility stands in for the market's "
            "non-earnings volatility; implied usually runs slightly above "
            "realized, so implied_event is if anything slightly too high"
        ),
        (
            "implied_asof is the session a read belongs to: the scheduled "
            "job reads after the close; an intraday read counts as today's, "
            "so a report earlier today is never treated as still ahead; "
            "fallback reads are dated by the flow scan that priced them; "
            "options settle at the expiry close"
        ),
        (
            "a fairly priced straddle still beats the realized move ~58-63% of "
            "the time (57.5% for normal moves, 63.2% for fat-tailed ones), so "
            "graded win rates are compared with that, not with 50%"
        ),
    ],
}


def _finite(o):
    """Recursively replace NaN/Infinity with None (same rule as
    trade_desk_validation.json_sanitize, kept local to avoid importing
    the Trade Desk engine here)."""
    if isinstance(o, dict):
        return {k: _finite(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_finite(v) for v in o]
    if isinstance(o, float) and (o != o or o in (float("inf"), float("-inf"))):
        return None
    return o


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    payload = build()
    if args.dry_run:
        print(json.dumps(payload, indent=1)[:4000])
        return
    os.makedirs(REPORTS, exist_ok=True)
    # NaN/Infinity -> null. Python emits bare NaN, which browsers reject
    # wholesale: on 2026-10-30 one NaN blanked the Earnings page's
    # implied-move section for the whole session. allow_nan=False makes
    # any future miss a loud failure instead of a silently broken page.
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(_finite(payload), f, separators=(",", ":"), allow_nan=False)
    print(f"  Wrote {os.path.relpath(OUT_PATH, _BASE)}")


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
