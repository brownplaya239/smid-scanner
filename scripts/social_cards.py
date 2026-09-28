#!/usr/bin/env python3
"""social_cards.py — branded image cards for TickerDesk's social posts.

Renders from the site's own published JSON, so a card can never show a
number the site doesn't. matplotlib is used for layout because it ships
its own fonts (DejaVu), so cards render identically on a CI runner and
locally.

    python scripts/social_cards.py callouts [--date YYYY-MM-DD]
        -> docs/cards/callouts_<date>.png  (1200x675, X/Twitter)

Each card returns (path, alt_text, caption_facts) for the poster.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.axes import Axes  # noqa: E402

# Card text is plain text: "$7.52 → $17.50" must not be parsed as
# mathtext (matplotlib treats paired $...$ as math and drops the $).
_orig_text = Axes.text


def _plain_text(self, *a, **kw):
    kw.setdefault("parse_math", False)
    return _orig_text(self, *a, **kw)


Axes.text = _plain_text

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARDS = os.path.join(_BASE, "docs", "cards")

BG = "#0a0f24"
PANEL = "#111936"
LINE = "#2a3a6e"
TEXT = "#e8ecf4"
DIM = "#8b93b7"
GOLD = "#ffc800"
GREEN = "#1fc36e"
RED = "#ff5b5b"


def _load(name):
    with open(os.path.join(_BASE, "docs", "reports", name),
              encoding="utf-8") as f:
        return json.load(f)


def _pct(v):
    return "—" if v is None else f"{'+' if v > 0 else ''}{round(v)}%"


def _day_label(d):
    dt = datetime.strptime(d, "%Y-%m-%d")
    return f"{dt:%a} {dt:%b} {dt.day}"


def callouts_card(date=None):
    doc = _load("callouts.json")
    days = doc.get("days") or []
    if not days:
        raise SystemExit("callouts.json has no days")
    day = next((d for d in days if d["date"] == date), None) \
        if date else days[0]
    if day is None:
        raise SystemExit(f"no callouts for {date}")
    top = day["top"][:4]

    fig = plt.figure(figsize=(12, 6.75), dpi=100, facecolor=BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1200)
    ax.set_ylim(675, 0)
    ax.axis("off")

    # header
    ax.text(60, 70, "TICKERDESK", color=GOLD, fontsize=15,
            fontweight="bold", va="center")
    ax.text(1140, 70, _day_label(day["date"]), color=DIM, fontsize=15,
            ha="right", va="center")
    ax.text(60, 135, "Options callouts — how they did", color=TEXT,
            fontsize=30, fontweight="bold", va="center")
    ax.text(60, 185,
            f"Best {len(top)} of {day['n']} callouts  ·  "
            f"{day['hit_100']} hit +100%  ·  {day['hit_50']} hit +50% at peak",
            color=DIM, fontsize=16, va="center")

    # rows
    y0, rh = 245, 88
    for i, t in enumerate(top):
        y = y0 + i * rh
        ax.add_patch(plt.Rectangle((50, y), 1100, rh - 14,
                                   facecolor=PANEL, edgecolor=LINE,
                                   linewidth=1))
        cy = y + (rh - 14) / 2
        ax.text(80, cy, t["label"], color=TEXT, fontsize=20,
                fontweight="bold", va="center")
        ax.text(640, cy, f"${t['entry']:.2f} → ${t['peak']:.2f}",
                color=DIM, fontsize=17, va="center")
        ax.text(1120, cy, _pct(t["peak_pct"]), color=GREEN, fontsize=26,
                fontweight="bold", ha="right", va="center")

    # footer
    ax.text(60, 632,
            "Entry = first trade after TickerDesk flagged it. Peak = best "
            "price afterwards, not a typical exit.",
            color=DIM, fontsize=11.5, va="center")
    ax.text(1140, 632, "tickerdesk.io", color=GOLD, fontsize=15,
            fontweight="bold", ha="right", va="center")

    os.makedirs(CARDS, exist_ok=True)
    path = os.path.join(CARDS, f"callouts_{day['date']}.png")
    fig.savefig(path, facecolor=BG)
    plt.close(fig)

    alt = (f"TickerDesk options callout results for {_day_label(day['date'])}: "
           + "; ".join(f"{t['label']} from ${t['entry']:.2f} to a peak of "
                       f"${t['peak']:.2f} ({_pct(t['peak_pct'])})"
                       for t in top)
           + f". {day['hit_100']} of {day['n']} callouts hit +100% at peak.")
    return path, alt[:1000], {"day": day, "top": top}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["callouts"])
    ap.add_argument("--date")
    a = ap.parse_args()
    p, alt, _ = callouts_card(a.date)
    print(p)
    print("alt:", alt)
