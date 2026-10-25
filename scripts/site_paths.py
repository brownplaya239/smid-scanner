#!/usr/bin/env python3
"""site_paths.py — publish clean-path copies of the single-page app.

GitHub Pages serves <name>.html at /<name>, so a path-routed tab
(/earnings instead of /#whisper) is the app itself, saved under that
name with its own <title>, description, canonical and og:url. The app's
router (PATH_ROUTES in docs/index.html) opens the right tab when loaded
at that path. Runs in pages.yml on the assembled _site/ — nothing is
committed; docs/index.html stays the only source.

    python scripts/site_paths.py _site
"""

from __future__ import annotations

import os
import re
import sys

# path name -> (title, description). Keep in step with PATH_ROUTES in
# docs/index.html and the sitemap in landing_pages.py.
PATHS = {
    "earnings": (
        "Earnings Calendar — This Week's Reports, Implied Moves & Options "
        "Flow | TickerDesk",
        "This week's earnings calendar with options-implied vs typical "
        "moves, pre-print flow, and graded earnings trade ideas. Updated "
        "nightly; every idea self-graded against real closes."),
}

SITE = "https://tickerdesk.io"


def rewrite(html, name, title, desc):
    url = f"{SITE}/{name}"
    n = 0
    html, k = re.subn(r"<title>[^<]*</title>", f"<title>{title}</title>",
                      html, count=1)
    n += k
    html, k = re.subn(r'(<link rel="canonical" href=")[^"]*(")',
                      rf"\g<1>{url}\g<2>", html, count=1)
    n += k
    html, k = re.subn(r'(<meta property="og:url" content=")[^"]*(")',
                      rf"\g<1>{url}\g<2>", html, count=1)
    n += k
    for attr in ('name="description"', 'property="og:description"'):
        html, k = re.subn(rf'(<meta {attr} content=")[^"]*(")',
                          rf"\g<1>{desc}\g<2>", html, count=1)
        n += k
    html, k = re.subn(r'(<meta property="og:title" content=")[^"]*(")',
                      rf"\g<1>{title}\g<2>", html, count=1)
    n += k
    if n < 6:
        raise SystemExit(f"{name}: only {n}/6 head tags found — the "
                         "index.html <head> changed; update site_paths.py")
    return html


def main(site_dir):
    src = os.path.join(site_dir, "index.html")
    with open(src, encoding="utf-8") as f:
        html = f.read()
    for name, (title, desc) in PATHS.items():
        out = os.path.join(site_dir, name + ".html")
        with open(out, "w", encoding="utf-8") as f:
            f.write(rewrite(html, name, title, desc))
        print(f"site_paths: /{name} -> {out} ({os.path.getsize(out)} bytes)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "_site")
