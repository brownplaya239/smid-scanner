#!/usr/bin/env python3
"""test_oi_states.py — one fixture per state, and the reconciliation rules.

Seven states, each reachable and each mutually exclusive. The fixtures are
the documentation: if you want to know what STRUCTURE UNCLEAR means, read
the one that produces it.

The two properties this file exists to prove:

  * a session-T print stays PENDING until a snapshot the vendor has
    explicitly dated to session T arrives — not until the clock says it
    should have;
  * the evaluation reproduces exactly from the immutable print records.

    python test_oi_states.py
    python test_oi_states.py --write   # dump fixtures for inspection
"""

import json
import os
import sys
from datetime import datetime

import brief_time as BT
import exchange_calendar as EC
import oi_followthrough as FT
import oi_pipeline as OP

T = "2026-07-21"                     # a Tuesday session
NOW = datetime(2026, 7, 22, 10, 0, tzinfo=BT.ET)     # inside the SLA
LATE = datetime(2026, 7, 22, 15, 0, tzinfo=BT.ET)    # past SLA + grace


def mk(contract, ts, size, tid, oi=1000, oi_date="2026-07-20",
       side="call_buyer", direction="bullish", prem=1_000_000.0, batch="b1"):
    return FT.flow_record(
        {"contract": contract, "ticker": EC.root_of(contract),
         "trade_ts": ts, "contracts": size, "premium": prem,
         "open_interest": oi, "trade_id": tid, "flow_side": side,
         "direction": direction, "spot": 100.0, "exchange": "XCBO",
         "price": 1.25, "bid": 1.20, "ask": 1.30},
        batch_id=batch, source_ts=ts, oi_before=oi,
        oi_before_trade_date=oi_date)


def snap(oi, date=T, status="complete", received=None, rev="rev-1"):
    return {"open_interest": oi, "oi_trade_date": date,
            "dataset_status": status,
            "oi_received_at": (received or NOW).isoformat(),
            "revision_id": rev}


# ── one fixture per state ───────────────────────────────────────────────

def fx_pending():
    p = mk("O:PEND260821C00100000", "2026-07-21T14:00:00-04:00", 900, "P1")
    return [p], {}, NOW


def fx_delayed():
    p = mk("O:DELY260821C00100000", "2026-07-21T14:00:00-04:00", 900, "D1")
    return [p], {}, LATE


def fx_strong():
    p = mk("O:STRG260821C00100000", "2026-07-21T14:00:00-04:00", 900, "S1")
    return [p], snap(1760), NOW          # +760 of 900 = 84%


def fx_partial():
    p = mk("O:PART260821C00100000", "2026-07-21T14:00:00-04:00", 900, "R1")
    return [p], snap(1270), NOW          # +270 of 900 = 30%


def fx_no_follow():
    p = mk("O:NONE260821C00100000", "2026-07-21T14:00:00-04:00", 900, "N1")
    return [p], snap(1020), NOW          # +20 of 900 = 2%


def fx_structure_unclear():
    a = mk("O:STRU260821C00100000", "2026-07-21T14:00:00-04:00", 500, "U1")
    b = mk("O:STRU260821C00120000", "2026-07-21T14:00:01-04:00", 500, "U2",
           side="call_seller", direction="bearish")
    return [a, b], snap(1400), NOW


def fx_not_evaluable():
    # adjusted series: OI before and after the adjustment are different
    # instruments and cannot be differenced
    p = mk("O:NEVL1260821C00100000", "2026-07-21T14:00:00-04:00", 900, "E1")
    return [p], snap(1760), NOW


FIXTURES = [
    ("pending", fx_pending, FT.OI_PENDING),
    ("delayed", fx_delayed, FT.OI_DELAYED),
    ("strong", fx_strong, FT.STRONG),
    ("partial", fx_partial, FT.PARTIAL),
    ("no_follow_through", fx_no_follow, FT.NO_FOLLOW),
    ("structure_unclear", fx_structure_unclear, FT.STRUCTURE_UNCLEAR),
    ("not_evaluable", fx_not_evaluable, FT.NOT_EVALUABLE),
]


def main():
    write = "--write" in sys.argv
    fails, ran = [], [0]

    def chk(n, c, d=""):
        ran[0] += 1
        print(("  PASS  " if c else "  FAIL  ") + n
              + ("" if c else "  <- %s" % (d,)))
        if not c:
            fails.append(n)

    print("-- one fixture per state --")
    dumps = {}
    seen_states = set()
    for name, fn, want in FIXTURES:
        prints, oi, now = fn()
        payload = OP.run(T, prints, oi_fetcher=lambda c, _o=oi: _o,
                         now_et=now)
        groups, _ = FT.aggregate_session(prints, T, now)
        FT.detect_structures(groups)
        evs = [FT.evaluate(g, oi, now) for g in groups]
        got = {e["state"] for e in evs}
        chk("%s -> %s" % (name, want), got == {want}, got)
        seen_states |= got
        dumps[name] = {"prints": prints, "evaluations": evs,
                       "payload": payload}
    chk("all seven states are reachable",
        seen_states == set(FT.STATES), sorted(seen_states))
    chk("states are mutually exclusive (each fixture yields exactly one)",
        all(len({e["state"] for e in dumps[n]["evaluations"]}) == 1
            for n, _, _ in FIXTURES))

    print("\n-- pending until an explicitly session-T snapshot arrives --")
    p = mk("O:PROOF260821C00100000", "2026-07-21T14:00:00-04:00", 900, "K1")
    steps = [
        ("no vendor response", {}, FT.OI_PENDING),
        ("vendor returns OI with no trade date",
         {"open_interest": 1760, "dataset_status": "complete",
          "oi_received_at": NOW.isoformat()}, FT.OI_PENDING),
        ("vendor returns OI dated to the PRIOR session",
         snap(1760, date="2026-07-20"), FT.OI_PENDING),
        ("vendor returns session-T OI but flags it partial",
         snap(1760, status="partial"), FT.OI_PENDING),
        ("vendor returns session-T OI with no retrieval timestamp",
         {"open_interest": 1760, "oi_trade_date": T,
          "dataset_status": "complete"}, FT.OI_PENDING),
        ("vendor returns a complete snapshot explicitly dated to T",
         snap(1760), FT.STRONG),
    ]
    for label, oi, want in steps:
        g, _ = FT.aggregate_session([p], T, NOW)
        ev = FT.evaluate(g[0], oi, NOW)
        chk("%s -> %s" % (label, want), ev["state"] == want, ev["state"])
    chk("the clock alone never unblocks evaluation",
        FT.evaluate(FT.aggregate_session([p], T, NOW)[0][0], {},
                    datetime(2026, 7, 30, 12, 0, tzinfo=BT.ET))["state"]
        == FT.OI_DELAYED)

    print("\n-- reproducibility from immutable records --")
    prints, oi, now = fx_strong()
    a = OP.run(T, prints, oi_fetcher=lambda c: oi, now_et=now)
    # round-trip the prints through JSON: an evaluation must be derivable
    # from the stored records alone, not from objects held in memory
    revived = json.loads(json.dumps(prints))
    b = OP.run(T, revived, oi_fetcher=lambda c: oi, now_et=now)
    chk("evaluation reproduces from serialized records",
        [r["follow_through_ratio"] for r in a["rows"]]
        == [r["follow_through_ratio"] for r in b["rows"]]
        and [r["state"] for r in a["rows"]] == [r["state"] for r in b["rows"]])
    chk("the print identity survives serialization",
        [p["identity"] for p in prints] == [p["identity"] for p in revived])
    chk("the threshold version is recorded on every evaluation",
        all(r["threshold_version"] for r in a["rows"]))

    print("\n-- reconciliation blocks --")
    dupe = prints + [dict(prints[0])]
    r = OP.run(T, dupe, oi_fetcher=lambda c: oi, now_et=now)
    chk("a repeated print is deduplicated, not double-counted",
        r["reconciliation"]["duplicates_dropped"] == 1
        and r["rows"][0]["observed_contracts"] == 900,
        r["reconciliation"])
    noqty = [dict(prints[0], contracts=None)]
    g, probs = FT.aggregate_session(noqty, T, now)
    chk("a print with no quantity is rejected, not defaulted",
        not g and any("contracts" in p for p in probs), probs)
    nodate = [dict(prints[0], oi_before_trade_date=None)]
    g2, _ = FT.aggregate_session(nodate, T, now)
    chk("unknown prior-OI date blocks evaluation",
        FT.evaluate(g2[0], oi, now)["state"] == FT.NOT_EVALUABLE)
    badsum = dict(FT.aggregate_session(prints, T, now)[0][0], reconciles=False)
    chk("an aggregate that does not reconcile is NOT EVALUABLE",
        FT.evaluate(badsum, oi, now)["state"] == FT.NOT_EVALUABLE)

    print("\n-- calendar behaviour --")
    chk("Monday's evaluation looks back to Friday",
        EC.previous_session("2026-07-20").isoformat() == "2026-07-17")
    fri = mk("O:CAL260821C00100000", "2026-07-17T15:00:00-04:00", 500, "F1")
    chk("a Friday print is dated to Friday's session",
        fri["trade_date"] == "2026-07-17", fri["trade_date"])
    mon = datetime(2026, 7, 20, 10, 0, tzinfo=BT.ET)
    gg, _ = FT.aggregate_session([fri], "2026-07-17", mon)
    chk("Friday's aggregate is frozen by Monday morning", gg[0]["frozen"])
    chk("a holiday is skipped when looking back",
        EC.previous_session("2026-11-27").isoformat() == "2026-11-25")
    half = mk("O:HALF260821C00100000", "2026-11-27T13:30:00-05:00", 100, "H1")
    chk("a print after the half-day close rolls to the next session",
        half["trade_date"] == "2026-11-30", half["trade_date"])
    spx = mk("O:SPXW260821P06000000", "2026-07-21T16:07:00-04:00", 100, "X1")
    chk("an index option print at 16:07 stays in that session",
        spx["trade_date"] == T, spx["trade_date"])

    print("\n-- threshold calibration report --")
    hist = []
    for i in range(160):
        ratio = (i % 20) / 20.0
        hist.append({"date": "2026-%02d-%02d" % (1 + i // 40, 1 + i % 28),
                     "ratio": ratio,
                     "outcome": ratio >= 0.5 or (i % 7 == 0)})
    cal = FT.calibrate(hist)
    chk("calibration splits chronologically", cal["ok"] and cal["n_train"] > 0)
    chk("out-of-sample buckets are reported",
        set(cal["out_of_sample"]) == {"strong", "partial", "none"})
    chk("thresholds are ordered", cal["partial_min"] < cal["strong_min"],
        (cal["partial_min"], cal["strong_min"]))
    chk("a thin history refuses rather than fitting noise",
        not FT.calibrate(hist[:10])["ok"])

    print("\n-- language --")
    ev = dumps["strong"]["evaluations"][0]
    lines = FT.lines_for(ev)
    blob = " ".join(lines.values()).upper()
    chk("nothing claims direction was confirmed",
        "CONFIRM" not in blob, blob)
    chk("the measure is named as follow-through",
        ev["measure"] == FT.MEASURE_LABEL)
    chk("data date and retrieval time are distinct strings",
        "EOD OI:" in lines["oi_change"] and "verified" in lines["oi"])
    chk("direction is stated without reference to OI",
        "OI" not in lines["direction"], lines["direction"])

    if write:
        d = os.path.join("docs", "reports", "oi_fixtures")
        os.makedirs(d, exist_ok=True)
        for name, blob2 in dumps.items():
            with open(os.path.join(d, "%s.json" % name), "w",
                      encoding="utf-8") as f:
                json.dump(blob2, f, indent=1, default=str)
        with open(os.path.join(d, "calibration.json"), "w",
                  encoding="utf-8") as f:
            json.dump(cal, f, indent=1)
        print("\n  wrote %d fixtures + calibration.json to %s"
              % (len(dumps), d))

    print("\n%d/%d checks passed" % (ran[0] - len(fails), ran[0]))
    if fails:
        print("FAILED: " + "; ".join(fails[:6]))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
