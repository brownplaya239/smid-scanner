#!/usr/bin/env python3
"""report_chart_v3.py — the two charts the v3 brief draws.

v2 shipped with no chart at all, which is the single feature readers of
the original one-pager missed most: a table of moving averages does not
tell you whether price is rolling over or basing.

    trading_chart(mk, ...)      page 3 — the chart a reader trades from:
                                120 completed sessions, candles, SMA 9/21/
                                50/200, volume against its own average,
                                and RSI(14)
    mini_chart(mk)              close and the three averages, compact
    full_chart(mk, spy_closes)  appendix — 12-month structural view with
                                relative strength

Both take the market dict `research_live.fetch_market()` already builds,
so no new data is fetched and nothing is recomputed from a second
source. The relative-strength panel is drawn only when a benchmark
series is actually supplied — an absent panel is labelled, never faked.
"""

import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt                            # noqa: E402
from matplotlib.ticker import (FuncFormatter, LogLocator,
                               NullFormatter, NullLocator)                # noqa: E402

INK = "#111418"
MUTED = "#5b6570"
GRID = "#e3e7eb"
ACCENT = "#1f3a5f"
MA20, MA50, MA200 = "#e08a1e", "#1a7f4b", "#b3261e"


def _sma(vals, n):
    out, run = [], 0.0
    for i, v in enumerate(vals):
        run += v
        if i >= n:
            run -= vals[i - n]
        out.append(run / n if i >= n - 1 else None)
    return out


def _partial_index(mk, n):
    """Position of the still-forming session inside the tail window, or
    None. Drawing it identically to a settled bar invites the reader to
    treat a half-day of volume as a full one."""
    if not mk.get("intraday"):
        return None
    return n - 1


def _tail(mk, months):
    n = max(30, int(months * 21))
    d = mk.get("dates") or []
    return (d[-n:], (mk.get("closes") or [])[-n:],
            (mk.get("volumes") or [])[-n:])


def _finish(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=170, bbox_inches="tight",
                facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def _style(ax):
    ax.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=7.5, length=3)


def mini_chart(mk, months=12):
    """Page 1: small enough to sit under the ladder, readable enough to
    show the shape of the last year."""
    dates, closes, _ = _tail(mk, months)
    if len(closes) < 25:
        return None
    full = mk.get("closes") or []
    off = len(full) - len(closes)
    # A wide, short figure cannot fill the space page 1 gives it:
    # at full page width the aspect ratio caps its height. Taller
    # here, and the renderer still shrinks it when space is tight.
    fig, ax = plt.subplots(figsize=(9.4, 2.9))
    x = range(len(closes))
    ax.plot(x, closes, color=INK, linewidth=1.5, label="Close", zorder=5)
    for n, col, lab in ((20, MA20, "20d"), (50, MA50, "50d"),
                        (200, MA200, "200d")):
        ma = _sma(full, n)[off:]
        if any(v is not None for v in ma):
            ax.plot(x, ma, color=col, linewidth=1.0, label=lab, alpha=0.9)
    _style(ax)
    ax.set_xticks([])
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, p: "%.0f" % v))
    ax.legend(loc="upper left", fontsize=7, frameon=False, ncol=4,
              labelcolor=MUTED)
    ax.set_title("%s — last %d months" % (mk.get("ticker", ""), months),
                 fontsize=8, color=MUTED, loc="left")
    return _finish(fig)


def full_chart(mk, spy_closes=None, months=12):
    """Page 3: price with the three averages, session volume, and relative
    strength against SPY when the benchmark series was retained."""
    dates, closes, vols = _tail(mk, months)
    if len(closes) < 25:
        return None
    full = mk.get("closes") or []
    off = len(full) - len(closes)
    have_rs = bool(spy_closes) and len(spy_closes) >= len(closes)
    rows = 3 if have_rs else 2
    heights = [3.2, 0.9, 1.1][:rows]
    fig, axes = plt.subplots(rows, 1, figsize=(9.4, sum(heights) + 0.5),
                             sharex=True,
                             gridspec_kw={"height_ratios": heights,
                                          "hspace": 0.12})
    ax = axes[0]
    x = list(range(len(closes)))
    ax.plot(x, closes, color=INK, linewidth=1.6, label="Close", zorder=5)
    for n, col, lab in ((20, MA20, "20-day"), (50, MA50, "50-day"),
                        (200, MA200, "200-day")):
        ma = _sma(full, n)[off:]
        if any(v is not None for v in ma):
            ax.plot(x, ma, color=col, linewidth=1.1, label=lab, alpha=0.9)
    # Same reasoning as the trading chart, on the close series this one
    # plots. The axis label carries the disclosure here: the appendix
    # caption is not threaded through the renderer.
    log_scale = (max(closes) / min(closes)) >= LOG_SPAN if min(closes) else 0
    if log_scale:
        ax.set_yscale("log")
        # A 4x range crosses one decade boundary, so the default decade
        # locator puts a single labelled tick on the whole axis. These
        # subdivisions give a readable price gridline every 30-50%.
        ax.yaxis.set_major_locator(LogLocator(base=10.0, numticks=14,
                                              subs=(1.0, 1.5, 2.0, 3.0,
                                                    5.0, 7.0)))
        ax.yaxis.set_major_formatter(FuncFormatter(
            lambda v, p: "%.0f" % v if v >= 10 else "%.1f" % v))
        ax.yaxis.set_minor_locator(NullLocator())
        ax.yaxis.set_minor_formatter(NullFormatter())
    _style(ax)
    ax.legend(loc="upper left", fontsize=7.5, frameon=False, ncol=4,
              labelcolor=MUTED)
    ax.set_ylabel("Price (log)" if log_scale else "Price", fontsize=8,
                  color=MUTED)

    av = axes[1]
    if vols and len(vols) == len(closes):
        mean = sum(vols) / float(len(vols)) or 1.0
        cols = ["#c9d2db" if v < 1.5 * mean else
                ("#e08a1e" if v < 3 * mean else "#b3261e") for v in vols]
        av.bar(x, vols, color=cols, width=0.9)
        av.axhline(mean, color=MUTED, linewidth=0.7, linestyle="--")
    pi = _partial_index(mk, len(x))
    if pi is not None and vols and len(vols) == len(closes):
        # hatch the forming session so it cannot be read as a full day
        av.bar([x[pi]], [vols[pi]], color="#ffffff", edgecolor="#b3261e",
               hatch="////", linewidth=0.8, width=0.9, zorder=6)
        ax.axvline(x[pi], color="#b3261e", linewidth=0.7, alpha=0.5,
                   linestyle=":")
        ax.annotate("PARTIAL", xy=(x[pi], closes[pi]),
                    xytext=(-46, 8), textcoords="offset points",
                    fontsize=7, color="#b3261e", weight="bold")
    _style(av)
    av.set_ylabel("Volume", fontsize=8, color=MUTED)
    av.yaxis.set_major_formatter(
        FuncFormatter(lambda v, p: "%.0fM" % (v / 1e6) if v >= 1e6
                      else ("%.0fk" % (v / 1e3) if v >= 1e3 else "0")))

    if have_rs:
        rax = axes[2]
        spy = spy_closes[-len(closes):]
        base = closes[0] / spy[0] if spy[0] else 1.0
        rsl = [(c / s) / base * 100.0 if s else None
               for c, s in zip(closes, spy)]
        rax.plot(x, rsl, color=ACCENT, linewidth=1.3)
        rax.axhline(100.0, color=MUTED, linewidth=0.7, linestyle="--")
        _style(rax)
        rax.set_ylabel("RS vs SPY", fontsize=8, color=MUTED)
    step = max(1, len(x) // 8)
    axes[-1].set_xticks(x[::step])
    axes[-1].set_xticklabels([str(d)[:10] for d in dates[::step]],
                             rotation=0, fontsize=7)
    note = ("" if have_rs else
            "  ·  relative-strength panel omitted: benchmark series not "
            "retained for this run")
    if mk.get("intraday"):
        note += ("  ·  final bar and its volume are PARTIAL: the session "
                 "was open when this was drawn")
    axes[0].set_title("%s — %d months, unadjusted daily closes%s"
                      % (mk.get("ticker", ""), months, note),
                      fontsize=8.5, color=MUTED, loc="left")
    return _finish(fig)


# ── the trading chart ───────────────────────────────────────────────────
#
# The 12-month line chart shows structure. It does not show what a trader
# needs to judge a setup: where each session opened and closed, whether
# participation confirmed the move, and whether momentum is stretched.
# This is the legacy chart's readability rebuilt on v3's data discipline —
# completed sessions only, with the forming bar drawn so it cannot be
# mistaken for a settled one.

TRADING_SESSIONS = 120
SMA_SET = ((9, "#e08a1e"), (21, "#1a7f4b"), (50, "#1f3a5f"),
           (200, "#b3261e"))
VOL_AVG_WIN = 20
LOG_SPAN = 2.5


def trading_chart(mk, levels=None, sessions=TRADING_SESSIONS):
    """Candles, moving averages, volume against its average, and RSI.

    Everything is computed on completed sessions. The open session, if
    there is one, is drawn hollow in red and annotated PARTIAL, and is
    excluded from every average on the page."""
    levels = levels or {}
    cd = list(mk.get("completed_dates") or [])
    cc = list(mk.get("completed_closes") or [])
    ch = list(mk.get("completed_highs") or [])
    cl = list(mk.get("completed_lows") or [])
    cv = list(mk.get("completed_volumes") or [])
    if len(cc) < 30:
        return None
    opens_all = list(mk.get("opens") or [])
    n_all = len(mk.get("closes") or [])
    o_off = n_all - len(cc) if mk.get("partial_session") else 0
    co = opens_all[:len(opens_all) - o_off] if o_off else opens_all
    co = co[-len(cc):] if len(co) >= len(cc) else cc[:]

    k = min(sessions, len(cc))
    d = [str(x)[:10] for x in cd[-k:]]
    o, c = co[-k:], cc[-k:]
    hi, lo, vol = ch[-k:], cl[-k:], cv[-k:]
    x = list(range(k))

    intr = mk.get("intraday") or None
    rows = 3
    # Wide and short. Page 3 carries the level tables and the insider
    # evidence as well, so the chart gets roughly three inches of height;
    # a 9.4x6.4 figure scaled into that comes out two-thirds page width
    # and the candles stop being legible. Drawing it at this aspect keeps
    # the full text width and spends the height on the price panel.
    fig, axes = plt.subplots(
        rows, 1, figsize=(11.5, 4.3), sharex=True,
        gridspec_kw={"height_ratios": [3.0, 0.8, 1.0], "hspace": 0.10})
    ax, av, rx = axes

    # candles
    up = "#1a7f4b"
    down = "#b3261e"
    for i in range(k):
        col = up if c[i] >= o[i] else down
        ax.vlines(i, lo[i], hi[i], color=col, linewidth=0.7, zorder=3)
        body_lo, body_hi = min(o[i], c[i]), max(o[i], c[i])
        ax.add_patch(plt.Rectangle((i - 0.32, body_lo), 0.64,
                                   max(body_hi - body_lo, 1e-6),
                                   facecolor=col if c[i] >= o[i] else "white",
                                   edgecolor=col, linewidth=0.7, zorder=4))
    for win, colr in SMA_SET:
        ma = _sma(cc, win)[-k:]
        if any(v is not None for v in ma):
            ax.plot(x, ma, color=colr, linewidth=1.0, alpha=0.9,
                    label="SMA %d" % win, zorder=5)

    # the still-forming session, unmistakably marked and never averaged
    if intr:
        xp = k
        ax.vlines(xp, intr.get("low") or intr["last"],
                  intr.get("high") or intr["last"], color=down,
                  linewidth=0.8, linestyle=":", zorder=6)
        ax.add_patch(plt.Rectangle(
            (xp - 0.32, min(intr.get("open") or intr["last"], intr["last"])),
            0.64, max(abs(intr["last"] - (intr.get("open") or intr["last"])),
                      1e-6),
            facecolor="none", edgecolor=down, hatch="////", linewidth=0.9,
            zorder=7))
        # above the candle, not across it
        ax.annotate("PARTIAL", xy=(xp, max(intr.get("high") or intr["last"],
                                           intr["last"])),
                    xytext=(0, 22), textcoords="offset points",
                    fontsize=7, color=down, weight="bold", ha="center",
                    arrowprops=dict(arrowstyle="-", color=down,
                                    linewidth=0.6, shrinkB=2))
        x = x + [xp]

    # annotations a reader acts on
    last = (intr or {}).get("last", c[-1])
    ax.axhline(last, color=INK, linewidth=0.7, linestyle="--", alpha=0.55)
    ax.annotate("last %.2f" % last, xy=(0, last), xytext=(2, 3),
                textcoords="offset points", fontsize=7.5, color=INK)
    conf = levels.get("confirmation")
    if conf:
        ax.axhline(conf["value"], color=up, linewidth=0.8, linestyle="-.",
                   alpha=0.75)
        ax.annotate("confirmation %.2f (%s)" % (conf["value"], conf["label"]),
                    xy=(0, conf["value"]), xytext=(2, 3),
                    textcoords="offset points", fontsize=7.5, color=up)
    bnd = levels.get("boundary")
    if bnd:
        ax.axhline(bnd["value"], color=down, linewidth=0.8, linestyle="-.",
                   alpha=0.75)
        ax.annotate("structural boundary %.2f" % bnd["value"],
                    xy=(0, bnd["value"]), xytext=(2, 3),
                    textcoords="offset points", fontsize=7.5, color=down)
    span = (max(hi) / min(lo)) if min(lo) else 1.0
    log_scale = span >= LOG_SPAN
    if log_scale:
        ax.set_yscale("log")
        # A 4x range crosses one decade boundary, so the default decade
        # locator puts a single labelled tick on the whole axis. These
        # subdivisions give a readable price gridline every 30-50%.
        ax.yaxis.set_major_locator(LogLocator(base=10.0, numticks=14,
                                              subs=(1.0, 1.5, 2.0, 3.0,
                                                    5.0, 7.0)))
        ax.yaxis.set_major_formatter(FuncFormatter(
            lambda v, p: "%.0f" % v if v >= 10 else "%.1f" % v))
        ax.yaxis.set_minor_locator(NullLocator())
        ax.yaxis.set_minor_formatter(NullFormatter())
    _style(ax)
    ax.legend(loc="upper left", fontsize=7.5, frameon=False, ncol=4,
              labelcolor=MUTED)
    ax.set_ylabel("Price (log)" if log_scale else "Price", fontsize=8,
                  color=MUTED)

    # volume against its own 20-session average
    cols = [up if c[i] >= o[i] else down for i in range(k)]
    av.bar(range(k), vol, color=cols, width=0.7, alpha=0.55)
    vma = _sma(cv, VOL_AVG_WIN)[-k:]
    if any(v is not None for v in vma):
        av.plot(range(k), vma, color=INK, linewidth=1.0,
                label="%d-session average" % VOL_AVG_WIN)
        av.legend(loc="upper left", fontsize=7, frameon=False,
                  labelcolor=MUTED)
    if intr:
        av.bar([k], [intr.get("volume") or 0], color="white", edgecolor=down,
               hatch="////", linewidth=0.8, width=0.7)
    _style(av)
    av.set_ylabel("Volume", fontsize=8, color=MUTED)
    av.yaxis.set_major_formatter(
        FuncFormatter(lambda v, p: "%.0fM" % (v / 1e6) if v >= 1e6
                      else ("%.0fk" % (v / 1e3) if v >= 1e3 else "0")))

    # RSI(14), completed sessions only
    r = _rsi_series(cc)[-k:]
    rx.plot(range(k), r, color=ACCENT, linewidth=1.2)
    rx.axhline(70, color=down, linewidth=0.7, linestyle="--", alpha=0.7)
    rx.axhline(30, color=up, linewidth=0.7, linestyle="--", alpha=0.7)
    rx.set_ylim(0, 100)
    rx.set_yticks([30, 50, 70])
    _style(rx)
    rx.set_ylabel("RSI(14)", fontsize=8, color=MUTED)

    step = max(1, k // 9)
    rx.set_xticks(list(range(0, k, step)))
    rx.set_xticklabels([d[i] for i in range(0, k, step)], fontsize=7)
    note = ""
    if intr:
        note = ("  ·  final bar PARTIAL and excluded from every average "
                "on this page")
    ax.set_title("%s — %d completed sessions%s"
                 % (mk.get("ticker", ""), k, note),
                 fontsize=8.5, color=MUTED, loc="left")
    # The caption has to describe the chart that exists, not the chart
    # the constants asked for: a name with 60 sessions of history gets 60,
    # and a log axis is only announced when one was actually used.
    return _finish(fig), {"sessions": k, "log_scale": log_scale,
                          "partial": bool(intr),
                          "last_completed": d[-1] if d else None}


def _rsi_series(closes, n=14):
    """Wilder RSI at every point, for the panel."""
    out = [None] * len(closes)
    if len(closes) < n + 1:
        return out
    d = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    g = [v if v > 0 else 0.0 for v in d]
    l = [-v if v < 0 else 0.0 for v in d]
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    out[n] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    for i in range(n, len(d)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
        out[i + 1] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return out
