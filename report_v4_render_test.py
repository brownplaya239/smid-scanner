#!/usr/bin/env python3
"""report_v4_render_test.py — the v4 renderer does not crash, and emits a
real multi-page PDF for the core and the appendix on every cached
snapshot, plus a one-page flash in DATA HOLD.

This is a smoke test, not a pixel test: it proves the story builds and
finalises to a valid PDF of a sane size and page count, on the free-tier
state (no estimate feed, no chart) that CI actually runs. The layout
itself is checked by eye through render_v4_preview.

    python report_v4_render_test.py
"""

import glob
import io
import os
import pickle
import sys

import fitz  # PyMuPDF

import report_v4 as R4
import report_v4_model as V4
import report_v4_event as EV

_pass = _fail = 0


def chk(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print("  PASS  %s" % name)
    else:
        _fail += 1
        print("  FAIL  %s" % name)


def _load(t):
    obj = pickle.load(io.open(".snapcache/%s.pkl" % t, "rb"))
    return (obj[0], obj[3] if len(obj) > 3 else {}) if isinstance(obj, tuple) \
        else (obj, {})


def _pages(data):
    d = fitz.open(stream=data, filetype="pdf")
    return d.page_count


CACHED = sorted(os.path.basename(p)[:-4]
                for p in glob.glob(".snapcache/*.pkl"))
if not CACHED:
    print("no cached snapshots — run report_v3_run on a ticker first")
    sys.exit(0)

for t in CACHED:
    snap, prov = _load(t)
    view = V4.build(snap)                       # free tier, no chart
    core = R4.build_core(snap, view)
    apx = R4.build_appendix(snap, view, prov=prov)
    chk("%s core is a PDF" % t, core[:4] == b"%PDF")
    chk("%s appendix is a PDF" % t, apx[:4] == b"%PDF")
    if not view.get("flash"):
        chk("%s core is 6 pages" % t, _pages(core) == 6)
    else:
        chk("%s DATA HOLD -> 1-page flash" % t, _pages(core) == 1)
    chk("%s appendix has content (>=2 pages)" % t, _pages(apx) >= 2)

# A forced DATA HOLD must collapse the core to the flash, never 6 pages.
snap, _ = _load(CACHED[0])
snap["catalyst"] = dict(snap.get("catalyst") or {},
                        event_kind="primary_release",
                        event_dt="2099-01-01T21:00:00+00:00",
                        verification={"is_results_disclosure": False,
                                      "reason": "unparseable"})
view = V4.build(snap, report_time="2099-01-02T00:00:00+00:00")
chk("forced DATA HOLD event", view["event"]["state"] == EV.DATA_HOLD)
chk("DATA HOLD core is the 1-page flash",
    _pages(R4.build_core(snap, view)) == 1)

print("\n%d/%d checks passed" % (_pass, _pass + _fail))
sys.exit(1 if _fail else 0)
