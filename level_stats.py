"""Historical index-level statistics engine (Index Levels decision page).

Measures — on real intraday bars, ~250 sessions of 5-minute data per
symbol — how SPY/QQQ/IWM actually behave around the levels the Index
Levels page plots: probability of touch, first-touch hold vs break,
average extension after a break, average rejection after a hold, and
second-touch break rate. Also publishes the daily-timeframe reference
set the frontend can't compute from 5 days of bars: ATR14 (+ 1y
percentile), SMA50/200, and prior completed week / month / quarter
highs and lows.

Data source: Polygon aggregates when POLYGON_API_KEY is set (CI — one
request per symbol covers a full year of 5m bars), yfinance fallback
otherwise (local runs; Yahoo caps 5m history at ~60 days, so local
output is a small-sample smoke test, clearly labeled by the published
`sessions` count). Everything published is computed from bars —
nothing estimated, nothing hardcoded.

Outputs:
  docs/reports/level_stats.json   (site payload)
  data/level_stats_cache.json     (per-session results; nightly runs
                                   only compute dates not yet cached)
"""

import json
import os
import statistics
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                                    # pragma: no cover
    ET = timezone(timedelta(hours=-5))

_BASE = os.path.dirname(os.path.abspath(__file__))
SITE_PATH = os.path.join(_BASE, "docs", "reports", "level_stats.json")
CACHE_PATH = os.path.join(_BASE, "data", "level_stats_cache.json")

SYMS = ["SPY", "QQQ", "IWM"]
POLY_KEY = os.environ.get("POLYGON_API_KEY", "").strip()
# sessions of 5m bars to measure (Polygon path). Yahoo fallback is
# capped by the source at ~60 calendar days regardless of this value.
TARGET_SESSIONS = int(os.environ.get("LEVEL_STATS_SESSIONS", "250"))
CACHE_CAP_DATES = 400

RTH_OPEN, RTH_CLOSE = 570, 960          # ET minutes
OR_END = 585                            # opening range = first 15m
VWAP_STATS_FROM = 600                   # VWAP touch stats count from 10:00
BREAK_BARS = 3                          # close beyond level within 15m = break
REJ_BARS = 6                            # rejection window after a hold


def _http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


# ── bar fetching ─────────────────────────────────────────────────────────

def fetch_daily(sym):
    """[{d, o, h, l, c}] ascending, ~2 years."""
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=740)
    if POLY_KEY:
        u = ("https://api.polygon.io/v2/aggs/ticker/%s/range/1/day/%s/%s"
             "?adjusted=true&sort=asc&limit=50000&apiKey=%s"
             % (sym, start.isoformat(), end.isoformat(), POLY_KEY))
        rows = _http_json(u).get("results") or []
        return [{"d": datetime.fromtimestamp(r["t"] / 1000, tz=ET)
                 .date().isoformat(),
                 "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"]}
                for r in rows]
    import yfinance as yf
    df = yf.Ticker(sym).history(period="2y", interval="1d",
                                auto_adjust=False)
    out = []
    for idx, row in df.iterrows():
        out.append({"d": idx.date().isoformat(), "o": float(row["Open"]),
                    "h": float(row["High"]), "l": float(row["Low"]),
                    "c": float(row["Close"])})
    return out


def fetch_5m(sym):
    """[{d, m, o, h, l, c, v}] ascending incl extended hours.
    d = ET session date, m = ET minute-of-day."""
    out = []
    if POLY_KEY:
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=int(TARGET_SESSIONS * 1.5) + 10)
        u = ("https://api.polygon.io/v2/aggs/ticker/%s/range/5/minute/%s/%s"
             "?adjusted=true&sort=asc&limit=50000&apiKey=%s"
             % (sym, start.isoformat(), end.isoformat(), POLY_KEY))
        data = _http_json(u)
        rows = data.get("results") or []
        # one request caps at 50k rows (~1y incl extended); page if needed
        while data.get("next_url"):
            data = _http_json(data["next_url"] + "&apiKey=" + POLY_KEY)
            rows += data.get("results") or []
        for r in rows:
            dt = datetime.fromtimestamp(r["t"] / 1000, tz=ET)
            out.append({"d": dt.date().isoformat(),
                        "m": dt.hour * 60 + dt.minute,
                        "o": r["o"], "h": r["h"], "l": r["l"],
                        "c": r["c"], "v": r.get("v") or 0})
        return out
    import yfinance as yf
    df = yf.Ticker(sym).history(period="60d", interval="5m",
                                prepost=True, auto_adjust=False)
    for idx, row in df.iterrows():
        dt = idx.tz_convert(ET) if idx.tzinfo else idx.tz_localize(ET)
        out.append({"d": dt.date().isoformat(),
                    "m": dt.hour * 60 + dt.minute,
                    "o": float(row["Open"]), "h": float(row["High"]),
                    "l": float(row["Low"]), "c": float(row["Close"]),
                    "v": float(row["Volume"] or 0)})
    return out


# ── daily-timeframe reference values ─────────────────────────────────────

def daily_context(daily):
    """ATR14 series by date + latest SMA/period H-L snapshot."""
    atr_by_date, trs = {}, []
    for i, b in enumerate(daily):
        if i == 0:
            tr = b["h"] - b["l"]
        else:
            pc = daily[i - 1]["c"]
            tr = max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc))
        trs.append(tr)
        if len(trs) >= 14:
            atr_by_date[b["d"]] = sum(trs[-14:]) / 14
    ctx = {"atr_by_date": atr_by_date}
    if not daily:
        return ctx
    closes = [b["c"] for b in daily]
    ctx["sma50"] = (round(sum(closes[-50:]) / 50, 2)
                    if len(closes) >= 50 else None)
    ctx["sma200"] = (round(sum(closes[-200:]) / 200, 2)
                     if len(closes) >= 200 else None)
    last_atr = atr_by_date.get(daily[-1]["d"])
    ctx["atr14"] = round(last_atr, 2) if last_atr else None
    atrs_1y = [v for d, v in sorted(atr_by_date.items())][-252:]
    if last_atr and len(atrs_1y) >= 60:
        ctx["atr_pctile"] = round(
            100 * sum(1 for v in atrs_1y if v <= last_atr) / len(atrs_1y))
    # prior COMPLETED week / month / quarter high & low
    def period_hl(keyf):
        groups = {}
        for b in daily:
            groups.setdefault(keyf(b["d"]), []).append(b)
        keys = sorted(groups)
        if len(keys) < 2:
            return None
        g = groups[keys[-2]]
        return {"h": round(max(x["h"] for x in g), 2),
                "l": round(min(x["l"] for x in g), 2)}
    iso_week = lambda d: "%d-W%02d" % datetime.fromisoformat(d) \
        .isocalendar()[:2]
    ctx["week"] = period_hl(iso_week)
    ctx["month"] = period_hl(lambda d: d[:7])
    ctx["quarter"] = period_hl(
        lambda d: d[:4] + "-Q" + str((int(d[5:7]) - 1) // 3 + 1))
    return ctx


# ── per-session level measurement ────────────────────────────────────────

def measure_session(prev_daily, bars_today, atr):
    """One session's level interactions from its 5m bars.

    Returns {level_key: {touched, held, ext, rej, t2, t2brk}} where
    held/ext/rej/t2brk are None when not applicable. All %s are of the
    level price. Definitions:
      touch  = a bar's [low, high] crosses the level (after it exists)
      break  = a close beyond the level by >0.05 ATR within 3 bars of
               the first touch (in the crossing direction); else held
      ext    = max excursion beyond the level through the session close
               after a break
      rej    = max move back away from the level within 6 bars of a hold
      t2brk  = same break test on the second distinct touch (price must
               first leave the level by >0.10 ATR)
    """
    rth = [b for b in bars_today if RTH_OPEN <= b["m"] < RTH_CLOSE]
    if len(rth) < 20 or not atr:
        return {}
    tol = 0.05 * atr
    away = 0.10 * atr

    pre = [b for b in bars_today if 240 <= b["m"] < RTH_OPEN]
    levels = {}
    if prev_daily:
        levels["PDH"] = (prev_daily["h"], RTH_OPEN)
        levels["PDL"] = (prev_daily["l"], RTH_OPEN)
        levels["PC"] = (prev_daily["c"], RTH_OPEN)
    if pre:
        levels["PMH"] = (max(b["h"] for b in pre), RTH_OPEN)
        levels["PML"] = (min(b["l"] for b in pre), RTH_OPEN)
    orb = [b for b in rth if b["m"] < OR_END]
    if orb:
        levels["ORH"] = (max(b["h"] for b in orb), OR_END)
        levels["ORL"] = (min(b["l"] for b in orb), OR_END)

    out = {}
    for key, (px, from_m) in levels.items():
        seq = [b for b in rth if b["m"] >= from_m]
        out[key] = _measure_level(seq, px, tol, away, static=True)

    # VWAP: running level; stats from 10:00 so the early chop doesn't
    # count every open print as a "touch"
    pv = vv = 0.0
    vwap_seq = []
    for b in rth:
        tp = (b["h"] + b["l"] + b["c"]) / 3
        pv += tp * b["v"]
        vv += b["v"]
        vwap_seq.append((b, pv / vv if vv else None))
    seq = [(b, w) for b, w in vwap_seq if b["m"] >= VWAP_STATS_FROM and w]
    out["VWAP"] = _measure_vwap(seq, tol, away)
    return out


def _measure_level(seq, px, tol, away, static=True):
    res = {"touched": 0, "held": None, "ext": None, "rej": None,
           "t2brk": None}
    i0 = None
    for i, b in enumerate(seq):
        if b["l"] <= px <= b["h"]:
            i0 = i
            break
    if i0 is None:
        return res
    res["touched"] = 1
    prev_c = seq[i0 - 1]["c"] if i0 > 0 else seq[i0]["o"]
    up_break = prev_c <= px          # approached from below → break is up
    res["held"] = 1
    for b in seq[i0:i0 + BREAK_BARS + 1]:
        if (up_break and b["c"] > px + tol) or \
           (not up_break and b["c"] < px - tol):
            res["held"] = 0
            break
    if res["held"] == 0:
        rest = seq[i0:]
        if up_break:
            mx = max(b["h"] for b in rest)
            res["ext"] = round((mx - px) / px * 100, 3)
        else:
            mn = min(b["l"] for b in rest)
            res["ext"] = round((px - mn) / px * 100, 3)
    else:
        win = seq[i0:i0 + REJ_BARS + 1]
        if up_break:
            mn = min(b["l"] for b in win)
            res["rej"] = round((px - mn) / px * 100, 3)
        else:
            mx = max(b["h"] for b in win)
            res["rej"] = round((mx - px) / px * 100, 3)
        # second touch: leave the level by `away`, then re-touch
        left_at = None
        for j in range(i0 + 1, len(seq)):
            b = seq[j]
            if abs(b["c"] - px) > away:
                left_at = j
                break
        if left_at:
            for j in range(left_at + 1, len(seq)):
                b = seq[j]
                if b["l"] <= px <= b["h"]:
                    pc2 = seq[j - 1]["c"]
                    up2 = pc2 <= px
                    brk = 0
                    for bb in seq[j:j + BREAK_BARS + 1]:
                        if (up2 and bb["c"] > px + tol) or \
                           (not up2 and bb["c"] < px - tol):
                            brk = 1
                            break
                    res["t2brk"] = brk
                    break
    return res


def _measure_vwap(seq, tol, away):
    """VWAP variant: the level moves with each bar."""
    res = {"touched": 0, "held": None, "ext": None, "rej": None,
           "t2brk": None}
    i0 = None
    for i, (b, w) in enumerate(seq):
        if b["l"] <= w <= b["h"]:
            i0 = i
            break
    if i0 is None:
        return res
    res["touched"] = 1
    b0, w0 = seq[i0]
    prev_c = seq[i0 - 1][0]["c"] if i0 > 0 else b0["o"]
    up_break = prev_c <= w0
    res["held"] = 1
    for b, w in seq[i0:i0 + BREAK_BARS + 1]:
        if (up_break and b["c"] > w + tol) or \
           (not up_break and b["c"] < w - tol):
            res["held"] = 0
            break
    if res["held"] == 1:
        win = seq[i0:i0 + REJ_BARS + 1]
        if up_break:
            mn = min(b["l"] for b, _ in win)
            res["rej"] = round((w0 - mn) / w0 * 100, 3)
        else:
            mx = max(b["h"] for b, _ in win)
            res["rej"] = round((mx - w0) / w0 * 100, 3)
    else:
        rest = seq[i0:]
        if up_break:
            mx = max(b["h"] for b, _ in rest)
            res["ext"] = round((mx - w0) / w0 * 100, 3)
        else:
            mn = min(b["l"] for b, _ in rest)
            res["ext"] = round((w0 - mn) / w0 * 100, 3)
    return res


# ── aggregation ─────────────────────────────────────────────────────────

LEVEL_KEYS = ["PDH", "PDL", "PC", "PMH", "PML", "ORH", "ORL", "VWAP"]


def aggregate(per_day):
    """per_day: {date: {key: session-result}} → published per-key stats."""
    out = {}
    for key in LEVEL_KEYS:
        rows = [d[key] for d in per_day.values() if key in d]
        n = len(rows)
        if n < 20:
            out[key] = {"status": "accruing", "n": n}
            continue
        touched = [r for r in rows if r["touched"]]
        held = [r for r in touched if r["held"] == 1]
        broke = [r for r in touched if r["held"] == 0]
        exts = [r["ext"] for r in broke if r.get("ext") is not None]
        rejs = [r["rej"] for r in held if r.get("rej") is not None]
        t2 = [r["t2brk"] for r in rows if r.get("t2brk") is not None]
        touch_rate = round(100 * len(touched) / n)
        held_rate = (round(100 * len(held) / len(touched))
                     if touched else None)
        stat = {
            "n": n,
            "touch": touch_rate,
            "held": held_rate,
            "brk_ext": round(statistics.mean(exts), 2) if exts else None,
            "rej": round(statistics.mean(rejs), 2) if rejs else None,
            "t2_brk": (round(100 * sum(t2) / len(t2)) if len(t2) >= 10
                       else None),
        }
        # quality score: reliability of the FIRST-touch read (how far the
        # hold/break split sits from a coin flip), weighted by touch
        # frequency and sample. Documented, deterministic, re-derived
        # nightly — not a hand-tuned constant per level.
        if held_rate is not None:
            edge = abs(held_rate - 50)          # 0 (coin flip) .. 50
            score = min(100, round(40 + edge * 1.6
                                   + min(20, touch_rate / 5)
                                   + min(10, n / 25)))
            stat["score"] = score
            stat["grade"] = ("A+" if score >= 85 else "A" if score >= 75
                             else "B" if score >= 60 else "C")
        out[key] = stat
    return out


def main():
    site = {"generated": datetime.now(timezone.utc)
            .isoformat(timespec="seconds"),
            "source": "polygon" if POLY_KEY else "yfinance-fallback",
            "sessions": None, "by_sym": {}}
    try:
        cache = json.load(open(CACHE_PATH, encoding="utf-8"))
    except Exception:
        cache = {}
    sessions_min = None

    for sym in SYMS:
        try:
            daily = fetch_daily(sym)
            ctx = daily_context(daily)
            bars = fetch_5m(sym)
        except Exception as e:
            print("%s: fetch failed: %s" % (sym, e))
            continue
        by_date = {}
        for b in bars:
            by_date.setdefault(b["d"], []).append(b)
        dates = sorted(by_date)
        daily_by_d = {b["d"]: b for b in daily}
        daily_dates = sorted(daily_by_d)
        sym_cache = cache.setdefault(sym, {})
        done = 0
        for d in dates[-TARGET_SESSIONS:]:
            if d in sym_cache:
                continue
            # prior TRADING day's daily bar
            prevs = [x for x in daily_dates if x < d]
            prev_daily = daily_by_d.get(prevs[-1]) if prevs else None
            atr = ctx["atr_by_date"].get(prevs[-1]) if prevs else None
            res = measure_session(prev_daily, by_date[d], atr)
            if res:
                sym_cache[d] = res
                done += 1
        # cap cache
        for extra in sorted(sym_cache)[:-CACHE_CAP_DATES]:
            del sym_cache[extra]
        n_days = len(sym_cache)
        sessions_min = n_days if sessions_min is None \
            else min(sessions_min, n_days)
        site["by_sym"][sym] = {
            "atr14": ctx.get("atr14"), "atr_pctile": ctx.get("atr_pctile"),
            "sma50": ctx.get("sma50"), "sma200": ctx.get("sma200"),
            "week": ctx.get("week"), "month": ctx.get("month"),
            "quarter": ctx.get("quarter"),
            "levels": aggregate(sym_cache),
        }
        print("%s: %d cached sessions (+%d new)" % (sym, n_days, done))

    site["sessions"] = sessions_min or 0
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, separators=(",", ":"))
    os.makedirs(os.path.dirname(SITE_PATH), exist_ok=True)
    with open(SITE_PATH, "w", encoding="utf-8") as f:
        json.dump(site, f, separators=(",", ":"))
    print("published %s (sessions=%s, source=%s)"
          % (SITE_PATH, site["sessions"], site["source"]))


if __name__ == "__main__":
    sys.exit(main())
