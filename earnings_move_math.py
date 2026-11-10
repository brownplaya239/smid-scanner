"""earnings_move_math.py: like-for-like earnings-move arithmetic.

The Earnings Edge, the idea log and the Trade Desk all compare what the
options market prices for an earnings report with what the stock has
actually done on past reports. Those two numbers are only comparable if
they measure the same thing over the same window. This module is the one
place that converts between them. Pure functions, no I/O.

What the raw option read is
  straddle   ATM call + put mid of the first expiry after the report,
             as % of spot, priced at the read-day close. A straddle is
             worth the expected ABSOLUTE move to expiry (risk-neutral),
             so it is a MEAN-absolute number, not a median and not a
             1-sigma number. It also covers every session between the
             read and the expiry, not just the earnings session.
  iv_1sigma  fallback when no straddle could be read: the flow scanner's
             expected_move_pct = IV x sqrt(dte/365), a 1-SIGMA move over
             the contract's life. For a normal move E|X| = 0.798 sigma,
             so a 1-sigma number is ~1.25x a straddle's.

Like-for-like conversion (both sources end up on the same basis)
  sigma_total   = straddle / 0.798          (iv_1sigma: the value itself)
  sigma_event^2 = sigma_total^2 - k * sigma_daily^2
                  k           = non-earnings sessions between the read
                                close and the expiry (sessions in
                                (read, expiry] minus the event session)
                  sigma_daily = RMS of the last 40 daily log returns
                                with earnings reaction days left out
                                and one-off catalyst gaps clipped at 3
                                robust standard deviations
  implied_event = 0.798 * sigma_event       (mean |move| of the report
                                             session alone)

That is compared with the MEAN |reaction| of past reports (the straddle
prices a mean). The median is kept as context only: |moves| are
right-skewed, so a median sits below the mean and makes options look
richer than they are.

Assumptions (also published in the JSON payloads): moves are treated as
normal when converting between a mean-absolute move and a sigma; recent
REALIZED daily volatility stands in for the market's non-earnings
volatility (implied usually runs a little above realized, so the
event-only figure is, if anything, slightly too high); option expiries
settle at that day's close.

A fairly priced straddle still "beats" the realized move more often than
not: for a normal move P(|X| < E|X|) = 57.5%, for a fat-tailed (Laplace)
move 63.2%. A win rate is therefore compared with that baseline, never
with 50%.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, time, timedelta

MEAN_ABS_PER_SIGMA = math.sqrt(2.0 / math.pi)  # 0.7979: E|X| / sigma for a normal X
# P(|X| < E|X|) when the implied move is exactly fair
NORMAL_FAIR_WIN = math.erf(MEAN_ABS_PER_SIGMA / math.sqrt(2.0))  # 0.5751 (normal)
LAPLACE_FAIR_WIN = 1.0 - math.exp(-1.0)  # 0.6321 (fat-tailed)

DAILY_VOL_WINDOW = 40  # daily returns used for sigma_daily (~2 months)
DAILY_VOL_MIN = 20  # fewer usable returns -> no estimate
CLIP_SIGMAS = 3.0  # catalyst gaps beyond 3 robust SDs are clipped, not counted
WEEKLY_WINDOW_DAYS = 5  # earnings_edge only reads an expiry within 5 days of the event


def sigma_total_pct(raw_pct, src):
    """1-sigma move (in % of spot) to expiry implied by a raw read."""
    if raw_pct is None or raw_pct <= 0:
        return None
    if src == "iv_1sigma":
        return float(raw_pct)
    return float(raw_pct) / MEAN_ABS_PER_SIGMA


def event_move(raw_pct, src, k_nonevent, daily_vol_pct):
    """Event-only mean |move| (% of spot) from a raw option read.

    Returns a dict:
      implied_event        mean |move| of the earnings session alone, or None
      sigma_event          its sigma (%)
      straddle_equiv       the raw read restated as a straddle (mean |move|
                           to expiry), so both sources compare
      nonevent_var_share   share of the read's variance that was normal days
      why                  set when implied_event is None
    """
    sig = sigma_total_pct(raw_pct, src)
    if sig is None:
        return {"implied_event": None, "why": "no option read"}
    out = {"straddle_equiv": round(sig * MEAN_ABS_PER_SIGMA, 2)}
    if k_nonevent is None or k_nonevent < 0:
        out.update(implied_event=None, why="expiry does not cover the report session")
        return out
    if k_nonevent > 0 and not daily_vol_pct:
        out.update(implied_event=None, why="no daily volatility to strip the non-earnings days")
        return out
    total_var = sig * sig
    strip = k_nonevent * (daily_vol_pct or 0.0) ** 2
    ev_var = total_var - strip
    if ev_var <= 0:
        out.update(
            implied_event=None,
            nonevent_var_share=1.0,
            why="normal-day volatility explains the whole option price "
            "(no measurable earnings premium)",
        )
        return out
    sig_e = math.sqrt(ev_var)
    out.update(
        implied_event=round(sig_e * MEAN_ABS_PER_SIGMA, 2),
        sigma_event=round(sig_e, 3),
        nonevent_var_share=round(strip / total_var, 3),
    )
    return out


def frame_move(implied_event_pct, daily_vol_pct, extra_sessions):
    """Mean |move| over a grading window that holds the event session plus
    `extra_sessions` normal sessions (an unknown-session report is graded
    close D-1 -> close D+1, i.e. one extra session)."""
    if implied_event_pct is None:
        return None
    if not extra_sessions:
        return float(implied_event_pct)
    if not daily_vol_pct:
        return None
    sig_e = implied_event_pct / MEAN_ABS_PER_SIGMA
    return MEAN_ABS_PER_SIGMA * math.sqrt(sig_e * sig_e + extra_sessions * daily_vol_pct**2)


def daily_vol_pct(closes, exclude_idx=(), window=DAILY_VOL_WINDOW, min_n=DAILY_VOL_MIN):
    """Normal-day volatility: RMS daily log return (%) over the last
    `window` usable returns, each clipped at CLIP_SIGMAS robust standard
    deviations (1.4826 x median |return|).

    closes: chronological floats. Return i is ln(closes[i] / closes[i-1]);
    any i in `exclude_idx` (earnings reaction sessions) is skipped. Mean is
    taken as zero (the option-pricing convention for a few days). The clip
    keeps one-off catalyst gaps (an FDA decision, a takeover rumour) from
    counting as normal-day volatility: one +115% day inside the window
    would otherwise read as ~20%/day and strip the whole straddle."""
    ex = set(exclude_idx)
    rets = []
    for i in range(len(closes) - 1, 0, -1):
        if len(rets) >= window:
            break
        if i in ex:
            continue
        a, b = closes[i - 1], closes[i]
        if not (isinstance(a, (int, float)) and isinstance(b, (int, float))):
            continue
        if a <= 0 or b <= 0 or a != a or b != b:
            continue
        rets.append(math.log(b / a))
    if len(rets) < min_n:
        return None
    absr = sorted(abs(r) for r in rets)
    mid = len(absr) // 2
    med = absr[mid] if len(absr) % 2 else 0.5 * (absr[mid - 1] + absr[mid])
    cap = CLIP_SIGMAS * 1.4826 * med
    if cap > 0:
        rets = [max(-cap, min(cap, r)) for r in rets]
    return round(100.0 * math.sqrt(sum(r * r for r in rets) / len(rets)), 3)


def dominated_by_one(abs_moves):
    """True when one report moved more than all the others combined — a
    mean over <= 8 reports is then one event, not a typical reaction."""
    xs = [abs(x) for x in abs_moves if isinstance(x, (int, float))]
    if len(xs) < 2:
        return False
    return max(xs) > sum(xs) - max(xs)


# ------------------------------------------------------------ calendar


def _cal():
    try:
        import exchange_calendar as XC

        return XC
    except Exception:
        return None


def _is_session(d):
    XC = _cal()
    if XC is not None:
        try:
            return XC.is_session(d)
        except Exception:
            pass
    return d.weekday() < 5


def sessions_after(a, b):
    """Trading sessions in (a, b] (dates)."""
    out, cur = [], a + timedelta(days=1)
    while cur <= b:
        if _is_session(cur):
            out.append(cur)
        cur += timedelta(days=1)
    return out


def previous_session(d):
    cur = d - timedelta(days=1)
    for _ in range(14):
        if _is_session(cur):
            return cur
        cur -= timedelta(days=1)
    return d - timedelta(days=1)


def next_session(d):
    cur = d + timedelta(days=1)
    for _ in range(14):
        if _is_session(cur):
            return cur
        cur += timedelta(days=1)
    return d + timedelta(days=1)


def _as_date(x):
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    return date.fromisoformat(str(x)[:10])


def reaction_session(event_date, session):
    """The session whose close-to-close move IS the reaction.
    BMO on D -> D; AMC on D -> next session; an event dated on a
    non-session day reacts on the next session. Unknown session -> None
    (it is one of D or the next session)."""
    d = _as_date(event_date)
    if not _is_session(d):
        return next_session(d)
    s = str(session or "").upper()
    if s == "BMO":
        return d
    if s == "AMC":
        return next_session(d)
    return None


def nonevent_sessions(read_date, expiry, event_date, session):
    """k = sessions in (read, expiry] minus the one event session, or None
    when the expiry does not cover the reaction (or the read is not
    before it)."""
    r, e, d = _as_date(read_date), _as_date(expiry), _as_date(event_date)
    sess = sessions_after(r, e)
    react = reaction_session(d, session)
    if react is not None:
        if react not in sess:
            return None
    else:  # unknown session: it is D or the next session; cover both
        if next_session(d) not in sess or previous_session(d) < r:
            return None
    return len(sess) - 1


def is_pre_event(read_date, event_date, session):
    """True when a read taken at the close of `read_date` is before the
    report: BMO on D needs a read at or before close D-1, AMC on D a read
    at or before close D, unknown session the stricter BMO rule."""
    r, d = _as_date(read_date), _as_date(event_date)
    if str(session or "").upper() == "AMC" and _is_session(d):
        return r <= d
    return r < d


def assumed_weekly_expiry(event_date, session):
    """Legacy reconstruction only: the expiry earnings_edge would have read
    for an idea logged before expiries were recorded. implied_direct takes
    the first listed expiry on/after the effective date (the report day
    for BMO, the day after otherwise) and no later than 5 days after it;
    listed single-stock weeklies expire on Fridays (Thursday when Friday
    is a holiday). None when no Friday falls in that window."""
    d = _as_date(event_date)
    eff = d if str(session or "").upper() == "BMO" else d + timedelta(days=1)
    cur = eff
    while cur.weekday() != 4:
        cur += timedelta(days=1)
    if not _is_session(cur):
        cur = previous_session(cur)
    if cur < eff or cur > eff + timedelta(days=WEEKLY_WINDOW_DAYS):
        return None
    return cur


def read_session_date(now=None):
    """The session a read taken at `now` belongs to: today once the 09:30
    ET open has passed (after the close the read IS today's close;
    during the session it already reflects today's trading, so a
    before-the-open report today is behind it), otherwise the previous
    session."""
    now = now or datetime.now(UTC)
    try:
        from zoneinfo import ZoneInfo

        et = now.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        et = now.astimezone(UTC) - timedelta(hours=5)
    d = et.date()
    if _is_session(d) and et.time() >= time(9, 30):
        return d
    return previous_session(d)


# ------------------------------------------------------------ baselines


def fair_win_baseline(ratios):
    """Empirical 'fair' win rate for a short-premium idea (%).

    ratios: |realized| / implied per event. If implied were exactly fair
    on average, the ratios would be these ratios divided by their mean;
    the share of those below 1.0 is the win rate a fairly priced straddle
    would have scored with THIS sample's dispersion. Compare a vol_rich
    win rate with this, not with 50%."""
    rs = [r for r in ratios if isinstance(r, (int, float)) and r == r and r >= 0]
    if not rs:
        return None
    m = sum(rs) / len(rs)
    if m <= 0:
        return None
    return round(100.0 * sum(1 for r in rs if r < m) / len(rs), 1)
