"""
ledger_rotate.py — keep the hot UOA signal ledger under GitHub's hard
100 MB file limit without breaking append-only history.

Incident (2026-09-16): data/uoa_signals.jsonl reached 100.01 MB; every
UOA publish from ~9:55 AM ET on was rejected with GH001, the site's
flow data froze for the rest of the day, and each runner's ledger
appends died with the runner (publish_reports.sh is always-exit-0 by
design, so the jobs stayed green — only the independent freshness
monitor went red).

Fix: ROTATION, not deletion. Lines older than KEEP_DAYS move into
monthly gzip archives (data/uoa_signals_archive_YYYYMM.jsonl.gz,
opened in APPEND mode — concatenated gzip members are a valid stream,
and a closed month's archive is never rewritten). The hot file keeps
the recent window that the intraday modules (scanner append,
carryover, OI pipeline, research) actually read. Full-history readers
go through uoa_alpha.load_ledger, which now reads archives + hot.

Point-in-time contract unchanged: every line survives verbatim, in
order (archives sorted by month, then hot); nothing is ever edited.

Runs as a cheap no-op unless the hot file exceeds TRIGGER_MB (or
--force). Wired into uoa.yml before the publish step.

    python ledger_rotate.py            # rotate if hot > TRIGGER_MB
    python ledger_rotate.py --force    # rotate regardless of size
    python ledger_rotate.py --dry-run  # report what would move
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

_BASE = os.path.dirname(os.path.abspath(__file__))
HOT = os.path.join(_BASE, "data", "uoa_signals.jsonl")
ARCHIVE_FMT = os.path.join(_BASE, "data",
                           "uoa_signals_archive_{ym}.jsonl.gz")

TRIGGER_MB = 80
KEEP_DAYS = 45


def rotate(force=False, dry=False):
    if not os.path.exists(HOT):
        print("no hot ledger — nothing to do")
        return {"rotated": 0}
    size_mb = os.path.getsize(HOT) / 1e6
    if size_mb < TRIGGER_MB and not force:
        print(f"hot ledger {size_mb:.1f} MB < {TRIGGER_MB} MB — no-op")
        return {"rotated": 0, "hot_mb": round(size_mb, 1)}

    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=KEEP_DAYS)).date().isoformat()
    keep, move = [], defaultdict(list)
    bad = 0
    with open(HOT, encoding="utf-8") as f:
        for line in f:
            raw = line.rstrip("\n")
            if not raw.strip():
                continue
            try:
                day = (json.loads(raw).get("flagged_at") or "")[:10]
            except Exception:
                bad += 1
                keep.append(raw)      # never discard even a bad line
                continue
            if day and day < cutoff:
                move[day[:7].replace("-", "")].append(raw)
            else:
                keep.append(raw)

    n_move = sum(len(v) for v in move.values())
    print(f"hot {size_mb:.1f} MB · keep {len(keep)} lines · "
          f"archive {n_move} lines across {len(move)} months"
          + (f" · {bad} unparseable kept" if bad else ""))
    if dry or not n_move:
        return {"rotated": 0 if dry else n_move}

    for ym in sorted(move):
        path = ARCHIVE_FMT.format(ym=ym)
        # append mode: a re-run adds a new gzip member; gzip readers
        # consume concatenated members as one stream. Rotation removes
        # moved lines from hot, so re-archiving the same line cannot
        # happen in normal operation.
        with gzip.open(path, "ab") as gz:
            gz.write(("\n".join(move[ym]) + "\n").encode("utf-8"))
        print(f"  -> {os.path.basename(path)} +{len(move[ym])} lines "
              f"({os.path.getsize(path)/1e6:.1f} MB)")

    tmp = HOT + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(keep) + ("\n" if keep else ""))
    os.replace(tmp, HOT)
    print(f"hot ledger now {os.path.getsize(HOT)/1e6:.1f} MB")
    return {"rotated": n_move, "months": sorted(move),
            "hot_mb": round(os.path.getsize(HOT) / 1e6, 1)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    rotate(force=a.force, dry=a.dry_run)
