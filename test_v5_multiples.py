#!/usr/bin/env python3
"""test_v5_multiples.py — slice-1 proof for the historical-multiples
engine. The tests that matter are the point-in-time ones: historical
valuation must use information available ON that date, not the fiscal
period it describes. Run: python test_v5_multiples.py [--live]

--live adds the real-data spot check against NOW's latest 10-Q filing
date (network + Polygon key)."""

import sys

import report_v5_multiples as M

PASS, FAIL = 0, []


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL.append(name)
        print("  FAIL  %s  %s" % (name, detail))


def q(end, val, filed, form="10-Q", start=None):
    from datetime import datetime, timedelta
    e = datetime.strptime(end, "%Y-%m-%d")
    return {"start": start or (e - timedelta(days=90)).strftime("%Y-%m-%d"),
            "end": end, "val": val, "filed": filed, "form": form,
            "accn": "TEST-" + end}


# ── 1. filing-date alignment: the heart of the module ────────────────
rows = [q("2026-03-31", 1.0, "2026-04-25"),
        q("2025-12-31", 1.0, "2026-01-28"),
        q("2025-09-30", 1.0, "2025-10-24"),
        q("2025-06-30", 1.0, "2025-07-23"),
        q("2026-06-30", 9.0, "2026-07-23")]        # the new quarter
ev = M.quarterly_events(rows)

ttm_on_filing_day = M.ttm_at(ev, M._dt("2026-07-23"))
check("fact NOT usable on its own filing date",
      ttm_on_filing_day == 4.0, "got %s" % ttm_on_filing_day)
ttm_after = M.ttm_at(ev, M._dt("2026-07-24"))
check("fact usable the day after filing",
      ttm_after == 12.0, "got %s" % ttm_after)

# ── 2. as-first-reported: restatements are lookahead and ignored ─────
rows2 = rows + [q("2025-06-30", 5.0, "2026-02-01")]   # later restatement
ev2 = M.quarterly_events(rows2)
# 2026-05-01: four quarters known (06-30 through 03-31); the June-2025
# quarter must contribute its ORIGINAL 1.0, not the restated 5.0.
ttm2 = M.ttm_at(ev2, M._dt("2026-05-01"))
check("restated value ignored in favour of as-first-reported",
      ttm2 == 4.0, "got %s" % ttm2)

# ── 3. Q4 derivation + split rebasing subtract like from like ────────
# FY2025 EPS filed post-split (new basis, 5:1): FY = 3.0 new-basis.
# Q1-Q3 filed pre-split (old basis, 5.0 each = 1.0 new basis each).
split = M.split_adjuster([{"execution_date": "2025-12-18",
                           "split_from": 1, "split_to": 5}])
qrows = [q("2025-03-31", 5.0, "2025-04-25"),
         q("2025-06-30", 5.0, "2025-07-23"),
         q("2025-09-30", 5.0, "2025-10-24")]
arows = [q("2025-12-31", 3.0, "2026-01-28", form="10-K",
           start="2025-01-01")]
qe = M._rebase(M.quarterly_events(qrows), split)
ae = M._rebase(M.annual_events(arows), split)
q4 = M.derive_q4(qe, ae)
check("Q4 derived across a split in one basis",
      len(q4) == 1 and abs(q4[0]["val"] - 0.0) < 1e-9,
      "got %s" % q4)
check("pre-split quarter rebased to current basis",
      abs(qe[0]["val"] - 1.0) < 1e-9, "got %s" % qe[0]["val"])

# ── 4. coverage floor withholds instead of computing thin bands ──────
bars = [("2026-0%d-01" % m, 100.0) for m in range(1, 8)]
band = M.multiple_band(bars, [], years=1)
check("no-TTM window is withheld with a reason",
      band.get("available") is False and "computable" in band.get("reason", ""),
      band.get("reason"))

# ── 5. negative trailing metric excluded, never a negative multiple ──
neg = [q("2026-03-31", -2.0, "2026-04-01"),
       q("2025-12-31", 0.1, "2026-01-05"),
       q("2025-09-30", 0.1, "2025-10-05"),
       q("2025-06-30", 0.1, "2025-07-05")]
band2 = M.multiple_band([("2026-05-01", 50.0)],
                        M.quarterly_events(neg), years=1)
check("negative TTM excluded from the series",
      band2.get("available") is False
      and band2.get("excluded_negative_ttm") == 1,
      str({k: band2.get(k) for k in ("available",
                                     "excluded_negative_ttm")}))

# ── live spot check (network) ────────────────────────────────────────
if "--live" in sys.argv:
    import research_live as RL
    cik = RL.cik_for("NOW")
    rows = RL.concept(cik, "EarningsPerShareDiluted", unit="USD/shares")
    # Newest QUARTER, then its FIRST filing — the latest 10-Q also
    # restates comparative periods, and picking by max(filed) alone
    # grabs one of those instead of the new quarter.
    tenq = [r for r in rows if r.get("form") == "10-Q"
            and r.get("start") and r.get("end")
            and 80 <= (M._dt(r["end"]) - M._dt(r["start"])).days <= 100]
    newest_end = max(str(r["end"]) for r in tenq)
    latest = min((r for r in tenq if str(r["end"]) == newest_end),
                 key=lambda r: str(r.get("filed")))
    filed = str(latest["filed"])
    ev = M.quarterly_events(rows)
    before = M.ttm_at(ev, M._dt(filed))
    from datetime import timedelta
    after = M.ttm_at(ev, M._dt(filed) + timedelta(days=1))
    newest_in = any(e["end"] == str(latest["end"])
                    and M._dt(e["available_from"]) <= M._dt(filed)
                    for e in ev)
    check("LIVE: latest 10-Q (%s, filed %s) invisible on filing day"
          % (latest["end"], filed), not newest_in and before is not None,
          "before=%s" % before)
    check("LIVE: visible the next day and TTM changes",
          after is not None and abs(after - before) > 1e-9,
          "before=%s after=%s" % (before, after))

print("\n%d/%d checks passed" % (PASS, PASS + len(FAIL)))
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
