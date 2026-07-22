#!/usr/bin/env python3
"""report_chart_v3.py — the two charts the v3 brief draws.

v2 shipped with no chart at all, which is the single feature readers of
the original one-pager missed most: a table of moving averages does not
tell you whether price is rolling over or basing.

    mini_chart(mk)              page 1 — close and the three averages
    full_chart(mk, spy_closes)  page 3 — price, volume, relative strength

Both take the market dict `research_live.fetch_market()` already builds,
so no new data is fetched and nothing is recomputed from a second
source. The relative-strength panel is drawn only when a benchmark
series is actually supplied — an absent panel is labelled, never faked.
"""

import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt                            # noqa: E402
from matplotlib.ticker import FuncFormatter                # noqa: E402

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
    _style(ax)
    ax.legend(loc="upper left", fontsize=7.5, frameon=False, ncol=4,
              labelcolor=MUTED)
    ax.set_ylabel("Price", fontsize=8, color=MUTED)

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
                      else "%.0fk" % (v / 1e3)))

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
