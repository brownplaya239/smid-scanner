#!/usr/bin/env python3
"""report_chart.py — the price chart for page 3 of the research brief.

Page 3 asserted levels in a table and showed nothing. A reader could not
see whether $398 was a wall the stock had failed at four times or a line
it crossed daily. This draws the same numbers the table publishes — from
the same canonical series, never a second calculation — so the chart and
the table cannot disagree.
"""

import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt          # noqa: E402
from matplotlib.ticker import FuncFormatter   # noqa: E402

INK = "#1f2d3d"
MUTED = "#8a94a6"
GRID = "#e6e9ef"
MA20 = "#2f6fb5"
MA50 = "#c98a1b"
MA200 = "#8b5fa8"
SUP = "#2e8b57"
RES = "#b0413e"
EVENT = "#b0413e"


def price_chart(mk, months=12, width=9.6, height=3.5, dpi=170,
                event_date=None, event_label=None):
    """Daily closes with the 20/50/200-day means and the support and
    resistance the brief quotes. mk is the dict from
    research_live.fetch_market — one series, one truth."""
    dates = mk["dates"]
    closes = mk["closes"]
    n = min(len(dates), int(months * 21))
    d, c = dates[-n:], closes[-n:]

    def _ma(period):
        out = []
        for i in range(len(closes) - n, len(closes)):
            if i + 1 < period:
                out.append(None)
            else:
                out.append(sum(closes[i + 1 - period:i + 1]) / float(period))
        return out

    fig, ax = plt.subplots(figsize=(width, height), dpi=dpi)
    fig.patch.set_alpha(0.0)
    ax.set_facecolor("none")

    ax.plot(d, c, color=INK, linewidth=1.5, zorder=5, label="Close")
    for period, col, lab in ((20, MA20, "20-day"), (50, MA50, "50-day"),
                             (200, MA200, "200-day")):
        series = _ma(period)
        if any(v is not None for v in series):
            ax.plot(d, series, color=col, linewidth=1.0, alpha=0.9,
                    zorder=4, label=lab)

    # level labels sit ON the plot, so they get a backing box — without
    # one the text ran straight through the price line and was unreadable
    box = dict(facecolor="white", alpha=0.78, edgecolor="none",
               boxstyle="square,pad=0.18")
    sup, res = mk.get("support"), mk.get("resistance")
    if sup:
        ax.axhline(sup, color=SUP, linewidth=0.9, linestyle=(0, (4, 3)),
                   alpha=0.85, zorder=3)
        ax.annotate("support %.2f" % sup, xy=(d[0], sup), xytext=(3, 4),
                    textcoords="offset points", fontsize=6.5, color=SUP,
                    zorder=7, bbox=box)
    if res:
        ax.axhline(res, color=RES, linewidth=0.9, linestyle=(0, (4, 3)),
                   alpha=0.85, zorder=3)
        ax.annotate("resistance %.2f" % res, xy=(d[0], res), xytext=(3, 4),
                    textcoords="offset points", fontsize=6.5, color=RES,
                    zorder=7, bbox=box)

    # the catalyst, marked where it actually happened
    if event_date:
        try:
            if event_date >= d[0]:
                ax.axvline(event_date, color=EVENT, linewidth=0.9,
                           alpha=0.55, zorder=2)
                ax.annotate(event_label or "catalyst",
                            xy=(event_date, max(c)), xytext=(3, -8),
                            textcoords="offset points", fontsize=6.5,
                            color=EVENT, rotation=90, va="top")
        except TypeError:
            pass

    last = c[-1]
    ax.scatter([d[-1]], [last], s=16, color=INK, zorder=6)
    ax.annotate("%.2f" % last, xy=(d[-1], last), xytext=(4, -2),
                textcoords="offset points", fontsize=7, color=INK,
                fontweight="bold")

    ax.grid(True, color=GRID, linewidth=0.6, alpha=0.9)
    ax.set_axisbelow(True)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=6.5, length=0)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, p: "%.0f" % v))
    leg = ax.legend(loc="upper right", fontsize=6.5, frameon=False,
                    ncol=4, handlelength=1.6, columnspacing=1.1)
    for t in leg.get_texts():
        t.set_color(MUTED)
    fig.tight_layout(pad=0.4)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True, dpi=dpi)
    plt.close(fig)
    return buf.getvalue()
