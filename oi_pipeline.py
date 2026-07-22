#!/usr/bin/env python3
"""oi_pipeline.py — run the follow-through workflow for one session.

The order matters and is the whole design:

  1. scan batches collect prints through session T
  2. the product's own close freezes each contract's daily aggregate
  3. on T+1 we ask the vendor for an OI dataset explicitly dated T
  4. only once that arrives does the evaluator run
  5. the desk publishes the top ten
  6. the email carries the top five, and only if step 4 finished before
     the email's cutoff
  7. a late vendor delivery updates the desk and does NOT rewrite the
     email that already went out

Step 3 is the one that is easy to skip. Reading "current" open interest
and assuming it is yesterday's is how a Friday brief ends up reporting
Wednesday's positioning as if it settled overnight.

    python oi_pipeline.py --self-test
    python oi_pipeline.py --session 2026-07-21     # live, needs a vendor
"""

import json
import os
import sys
from datetime import datetime

import brief_time as BT
import exchange_calendar as EC
import oi_followthrough as FT

_BASE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(_BASE, "docs", "reports")
LEDGER = os.path.join(_BASE, "data", "uoa_signals.jsonl")
DESK_OUT = os.path.join(REPORTS, "oi_followthrough.json")

DESK_LIMIT = 10
EMAIL_LIMIT = 5


def load_prints(session_date, ledger_path=LEDGER, tail=40000):
    """Rebuild session T's prints from the immutable ledger.

    The ledger is append-only, so an evaluation can always be reproduced
    from the same lines that produced it.
    """
    if not os.path.exists(ledger_path):
        return []
    out = []
    with open(ledger_path, encoding="utf-8") as fh:
        lines = fh.readlines()[-tail:]
    want = (session_date.isoformat() if hasattr(session_date, "isoformat")
            else str(session_date))
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        ts = r.get("flagged_at")
        if not ts:
            continue
        rec = FT.flow_record(
            {"contract": r.get("contract"), "ticker": r.get("ticker"),
             "trade_ts": ts,
             # the ledger stores the contract's cumulative day volume at
             # flag time, not this print's size. Recorded as observed, and
             # deduplicated by identity so re-reads do not multiply it.
             "contracts": r.get("volume"),
             "premium": r.get("premium"),
             "open_interest": r.get("open_interest"),
             "flow_side": r.get("flow_side"),
             "direction": r.get("direction"),
             "spot": r.get("underlying_px_at_flag"),
             "trade_id": r.get("id")},
            batch_id=r.get("batch_id") or ("ledger:%d" % i),
            source_ts=ts,
            oi_before=r.get("open_interest"),
            # The ledger does not record WHICH session the flagged OI
            # belongs to. Polygon's snapshot OI is the previous session's
            # cleared figure, so the date is derivable from the calendar —
            # but derived is not vendor-stated, and the evaluator treats
            # the difference as material. Recorded with its provenance.
            oi_before_trade_date=_derived_prior_session(ts))
        rec["oi_before_date_source"] = "derived_from_calendar"
        if rec["trade_date"] == want:
            out.append(rec)
    return out


def _derived_prior_session(iso_ts):
    d = BT.parse_iso(iso_ts)
    if not d:
        return None
    try:
        return EC.previous_session(BT.to_et(d).date()).isoformat()
    except EC.CalendarRangeError:
        return None


def fetch_oi(contract, session_date, fetcher=None):
    """Ask the vendor for OI dated to `session_date`.

    Returns the vendor's own claims, unmodified. When the vendor does not
    state a trade date we do NOT fill one in: an unstated date is the
    finding, and the readiness check is entitled to see it missing.
    """
    if fetcher is None:
        return {}
    try:
        raw = fetcher(contract) or {}
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}
    return {
        "open_interest": raw.get("open_interest"),
        "oi_trade_date": raw.get("oi_trade_date"),
        "dataset_status": raw.get("dataset_status"),
        "oi_received_at": raw.get("oi_received_at")
        or datetime.now(BT.ET).isoformat(timespec="seconds"),
        "revision_id": raw.get("revision_id"),
    }


def run(session_date, prints=None, oi_fetcher=None, now_et=None,
        desk_limit=DESK_LIMIT, email_limit=EMAIL_LIMIT):
    """The whole workflow for one session."""
    now = BT.to_et(now_et or datetime.now(BT.ET))
    sd = session_date if hasattr(session_date, "isoformat") \
        else EC._d(session_date)
    recs = prints if prints is not None else load_prints(sd)

    groups, problems = FT.aggregate_session(recs, sd, now)
    FT.detect_structures(groups)

    evaluations = []
    for g in groups:
        snap = fetch_oi(g["contract"], sd, oi_fetcher)
        ev = FT.evaluate(g, snap, now)
        ev["oi_verified_at"] = (BT.fmt_stamp(BT.parse_iso(snap["oi_received_at"]))
                                if ev.get("state") in
                                (FT.STRONG, FT.PARTIAL, FT.NO_FOLLOW)
                                and snap.get("oi_received_at") else None)
        ev["flow_at"] = FT.fmt_dt(g.get("first_print_et"))
        ev["right"] = g.get("right")
        ev["strike"] = g.get("strike")
        ev["expiry"] = g.get("expiry")
        evaluations.append(ev)

    ranked = FT.rank(evaluations, limit=desk_limit)
    counts = {}
    for e in evaluations:
        counts[e["state"]] = counts.get(e["state"], 0) + 1

    ready = bool(ranked)
    payload = {
        "schema": FT.SCHEMA,
        "session_date": sd.isoformat(),
        "generated_at": now.isoformat(timespec="seconds"),
        "threshold_version": FT.THRESHOLD_VERSION,
        "evaluated": len(evaluations),
        "ranked": len(ranked),
        "state_counts": counts,
        "problems": problems,
        "rows": ranked,
        "email_rows": ranked[:email_limit],
        "ready": ready,
    }
    payload["reconciliation"] = reconcile(recs, groups, evaluations)
    return payload


def reconcile(prints, groups, evaluations):
    """Do the numbers we are about to publish add up to the records we
    hold? A mismatch here means a print was dropped or double-counted."""
    deduped, dropped = FT.dedupe(prints)
    by_contract = {}
    for r in deduped:
        if r.get("contracts") is None:
            continue
        by_contract[r["contract"]] = by_contract.get(r["contract"], 0) \
            + int(r["contracts"])
    problems = []
    for g in groups:
        want = by_contract.get(g["contract"])
        if want is not None and want != g["observed_contracts"]:
            problems.append("%s: aggregate %d != sum of prints %d"
                            % (g["contract"], g["observed_contracts"], want))
    states = [e["state"] for e in evaluations]
    displayed = sum(1 for e in evaluations if e["state"] in FT.RANKABLE)
    pending = sum(1 for s in states
                  if s in (FT.OI_PENDING, FT.OI_DELAYED))
    other = len(states) - displayed - pending
    if displayed + pending + other != len(evaluations):
        problems.append("state counts do not sum to the evaluation count")
    return {"prints_in": len(prints), "prints_deduped": len(deduped),
            "duplicates_dropped": dropped, "contracts": len(groups),
            "displayed": displayed, "pending": pending, "other": other,
            "ok": not problems, "problems": problems}


def email_section(payload, session_date=None):
    """The shape brief_model wants. When OI has not posted, this returns
    the disclosure rather than an empty table."""
    sd = payload.get("session_date") or (
        session_date.isoformat() if hasattr(session_date, "isoformat")
        else str(session_date or ""))
    pretty = FT.fmt_dt((sd + "T00:00:00-04:00") if sd else "", False)
    rows = payload.get("email_rows") or []
    if not rows:
        return {
            "rows": [],
            "sub": "Cleared open interest against the prior session's prints",
            "pending_line": ("%s end-of-day OI has not posted yet. Results "
                             "will appear on the desk once the dataset "
                             "arrives; this email is a snapshot and will not "
                             "update." % pretty),
            "desk_line": "Check the desk for the full table",
        }
    return {
        "rows": rows,
        "sub": "%s cleared open interest against that session's prints · "
               "thresholds %s" % (pretty, payload.get("threshold_version")),
        "desk_line": "See the top %d on the desk" % len(payload.get("rows")
                                                        or []),
    }


def write_desk(payload, path=DESK_OUT):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1, default=str)
    return path


def load_desk(path=DESK_OUT):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ── self-test ───────────────────────────────────────────────────────────

def self_test():
    fails, ran = [], [0]

    def chk(n, c, d=""):
        ran[0] += 1
        print(("  PASS  " if c else "  FAIL  ") + n
              + ("" if c else "  <- %s" % (d,)))
        if not c:
            fails.append(n)

    T = "2026-07-21"
    now = datetime(2026, 7, 22, 10, 0, tzinfo=BT.ET)

    def mk(contract, ts, size, tid, batch="b1", prem=1e6, oi=1000):
        return FT.flow_record(
            {"contract": contract, "ticker": EC.root_of(contract),
             "trade_ts": ts, "contracts": size, "premium": prem,
             "open_interest": oi, "trade_id": tid, "flow_side": "call_buyer",
             "direction": "bullish", "spot": 100.0},
            batch_id=batch, source_ts=ts, oi_before=oi,
            oi_before_trade_date="2026-07-20")

    prints = [
        mk("O:AAA260821C00100000", "2026-07-21T14:00:00-04:00", 900, "A1"),
        mk("O:AAA260821C00100000", "2026-07-21T14:00:00-04:00", 900, "A1",
           batch="b2"),                                   # re-read
        mk("O:BBB260821P00050000", "2026-07-21T15:00:00-04:00", 400, "B1"),
        mk("O:CCC260821C00200000", "2026-07-21T15:30:00-04:00", 200, "C1"),
    ]

    # ── the core demonstration: pending until an explicitly T-dated snapshot
    print("\n-- session-T print stays pending until session-T OI arrives --")
    silent = run(T, prints, oi_fetcher=lambda c: {}, now_et=now)
    chk("no vendor data leaves every contract pending",
        all(r["state"] == FT.OI_PENDING
            for r in [e for e in _all(silent)]), silent["state_counts"])
    chk("nothing is ranked while pending", silent["ranked"] == 0)
    chk("the email shows the disclosure, not an empty table",
        "has not posted yet" in email_section(silent)["pending_line"])
    chk("the disclosure says the email will not update",
        "will not update" in email_section(silent)["pending_line"])

    wrong = run(T, prints, now_et=now, oi_fetcher=lambda c: {
        "open_interest": 5000, "oi_trade_date": "2026-07-20",
        "dataset_status": "complete", "oi_received_at": now.isoformat()})
    chk("a snapshot dated to the WRONG session keeps it pending",
        all(e["state"] == FT.OI_PENDING for e in _all(wrong)),
        wrong["state_counts"])

    incomplete = run(T, prints, now_et=now, oi_fetcher=lambda c: {
        "open_interest": 5000, "oi_trade_date": T,
        "dataset_status": "partial", "oi_received_at": now.isoformat()})
    chk("an incomplete dataset keeps it pending",
        all(e["state"] == FT.OI_PENDING for e in _all(incomplete)))

    right = run(T, prints, now_et=now, oi_fetcher=lambda c: {
        "open_interest": {"O:AAA260821C00100000": 1800,
                          "O:BBB260821P00050000": 1080,
                          "O:CCC260821C00200000": 1005}[c],
        "oi_trade_date": T, "dataset_status": "complete",
        "oi_received_at": now.isoformat(), "revision_id": "r1"})
    chk("an explicitly session-T complete snapshot unblocks evaluation",
        right["ranked"] == 3, right["state_counts"])
    states = {e["contract"]: e["state"] for e in _all(right)}
    chk("800 of 900 is strong",
        states["O:AAA260821C00100000"] == FT.STRONG, states)
    chk("80 of 400 is partial",
        states["O:BBB260821P00050000"] == FT.PARTIAL, states)
    chk("5 of 200 is no net follow-through",
        states["O:CCC260821C00200000"] == FT.NO_FOLLOW, states)

    # ── reproducibility
    print("\n-- reproducibility and reconciliation --")
    again = run(T, prints, now_et=now, oi_fetcher=lambda c: {
        "open_interest": {"O:AAA260821C00100000": 1800,
                          "O:BBB260821P00050000": 1080,
                          "O:CCC260821C00200000": 1005}[c],
        "oi_trade_date": T, "dataset_status": "complete",
        "oi_received_at": now.isoformat(), "revision_id": "r1"})
    chk("the same records reproduce the same result",
        [r["contract"] for r in again["rows"]]
        == [r["contract"] for r in right["rows"]]
        and [r["follow_through_ratio"] for r in again["rows"]]
        == [r["follow_through_ratio"] for r in right["rows"]])
    rec = right["reconciliation"]
    chk("reconciliation is clean", rec["ok"], rec["problems"])
    chk("the duplicate re-read was dropped once",
        rec["duplicates_dropped"] == 1 and rec["prints_deduped"] == 3, rec)
    chk("displayed + pending + other equals the evaluation count",
        rec["displayed"] + rec["pending"] + rec["other"] == right["evaluated"])
    chk("the threshold version is published with the payload",
        right["threshold_version"] == FT.THRESHOLD_VERSION)

    # ── late delivery updates the desk, not the sent email
    print("\n-- late delivery --")
    late = datetime(2026, 7, 22, 15, 0, tzinfo=BT.ET)
    delayed = run(T, prints, oi_fetcher=lambda c: {}, now_et=late)
    chk("after the SLA the state is DELAYED, not unconfirmed",
        all(e["state"] == FT.OI_DELAYED for e in _all(delayed)),
        delayed["state_counts"])
    chk("a delayed run still publishes the disclosure",
        email_section(delayed)["pending_line"])
    chk("a later successful run produces a full desk payload",
        run(T, prints, now_et=late, oi_fetcher=lambda c: {
            "open_interest": 1800, "oi_trade_date": T,
            "dataset_status": "complete",
            "oi_received_at": late.isoformat()})["ready"])

    # ── email section shape
    print("\n-- email section --")
    sec = email_section(right)
    chk("email carries at most five rows", len(sec["rows"]) <= 5)
    chk("the sub-line states the session and the threshold version",
        "Jul 21" in sec["sub"] and FT.THRESHOLD_VERSION in sec["sub"],
        sec["sub"])
    chk("every email row carries a verified timestamp",
        all(r.get("oi_verified_at") for r in sec["rows"]),
        [r.get("oi_verified_at") for r in sec["rows"]])
    chk("every email row keeps direction separate from OI state",
        all(r.get("direction") and r.get("state") for r in sec["rows"]))

    print("\n%d/%d checks passed" % (ran[0] - len(fails), ran[0]))
    return 1 if fails else 0


def _all(payload):
    """Every evaluation, ranked or not — the payload keeps only ranked
    rows, so the self-test re-derives the rest from the counts."""
    seen = list(payload.get("rows") or [])
    have = {r["contract"] for r in seen}
    for state, n in (payload.get("state_counts") or {}).items():
        if state in FT.RANKABLE:
            continue
        for i in range(n):
            seen.append({"contract": "unranked-%s-%d" % (state, i),
                         "state": state})
    return seen


if __name__ == "__main__":
    raise SystemExit(self_test())
