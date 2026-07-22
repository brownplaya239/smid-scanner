#!/usr/bin/env python3
"""oi_followthrough.py — what traded, what it might mean, and what stuck.

Three questions that the previous implementation ran together:

  1. What traded?  An immutable record of prints, deduplicated across scan
     batches and aggregated by exact OCC contract.
  2. What does the tape suggest?  A directional inference, carried
     separately and never revised by anything that happens overnight.
  3. Did new open interest remain after clearing?  A measurement of net
     new positioning in the contract — not a verdict on the trade.

The old carryover module computed `delta / flag_volume` and printed "OI
confirmed ✅ — whale held overnight", then explained that a low ratio meant
the position was "likely closed/scalped". Every part of that overreaches.
Open interest is a NET figure for the whole contract: it moves because of
everyone's activity, not the one print we flagged, and it cannot tell you
who opened, who closed, or whether the position was one leg of a spread.
What it can tell you is whether net new positioning appeared. That is
worth reporting, under a name that says what it is.

    python oi_followthrough.py --self-test
"""

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta

import brief_time as BT
import exchange_calendar as EC

SCHEMA = "tickerdesk_oi_followthrough/v1"
_BASE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(_BASE, "docs", "reports")

# ── states ──────────────────────────────────────────────────────────────
# Mutually exclusive. Every evaluation lands in exactly one.
OI_PENDING = "OI PENDING"
OI_DELAYED = "OI DATA DELAYED"
STRONG = "STRONG FOLLOW-THROUGH"
PARTIAL = "PARTIAL FOLLOW-THROUGH"
NO_FOLLOW = "NO NET FOLLOW-THROUGH"
STRUCTURE_UNCLEAR = "STRUCTURE UNCLEAR"
NOT_EVALUABLE = "NOT EVALUABLE"
STATES = (OI_PENDING, OI_DELAYED, STRONG, PARTIAL, NO_FOLLOW,
          STRUCTURE_UNCLEAR, NOT_EVALUABLE)

# The measurement's name. It is not "DIRECTION CONFIRMED" and must never
# be rendered as such: OI says positioning appeared, not that our reading
# of who was aggressing was right.
MEASURE_LABEL = "OPENING-INTEREST FOLLOW-THROUGH"

# Vendor delivery expectations. OCC publishes cleared OI for session T on
# the morning of T+1; the SLA is when we expect our vendor to have it, and
# the grace period is how long we wait before calling it late.
OI_SLA_ET = (9, 15)
OI_GRACE_MINUTES = 90

_OCC = re.compile(r"^O:(?P<root>[A-Z][A-Z0-9.]{0,5})"
                  r"(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})"
                  r"(?P<cp>[CP])(?P<strike>\d{8})$")


def parse_occ(contract):
    """OCC symbol -> dict, or None when the symbol is malformed.

    The tail is fixed-width, so the root is what remains once fifteen
    characters are taken off the end. An ADJUSTED root (a digit appended
    after a split or spin-off) parses fine here — it is a well-formed
    symbol for a different instrument, and `is_adjusted` is what stops it
    being differenced against the pre-adjustment series.
    """
    s = (contract or "").upper()
    m = _OCC.match(s)
    if not m:
        return None
    g = m.groupdict()
    return {"root": g["root"],
            "expiry": "20%s-%s-%s" % (g["yy"], g["mm"], g["dd"]),
            "right": "call" if g["cp"] == "C" else "put",
            "strike": int(g["strike"]) / 1000.0}


def is_adjusted(contract):
    """Adjusted / non-standard series. OCC appends a digit to the root
    after a split, spin-off or special dividend; the contract before and
    after the adjustment are different instruments and their open
    interest cannot be differenced."""
    root = EC.root_of(contract)
    return bool(root) and root[-1].isdigit()


# ── 1. point-in-time flow record ────────────────────────────────────────

FLOW_FIELDS = (
    "contract", "ticker", "right", "strike", "expiry",
    "trade_date", "trade_ts_et", "exchange", "trade_id",
    "contracts", "price", "premium",
    "nbbo_bid", "nbbo_ask", "spot_at_detection",
    "action", "direction", "side_method",
    "oi_before", "oi_before_trade_date",
    "batch_id", "source_ts",
)
# Without these a print cannot be aggregated or compared. The rest are
# recorded when the vendor supplies them and left absent when it does not.
REQUIRED_FLOW_FIELDS = ("contract", "trade_date", "trade_ts_et", "contracts",
                        "batch_id", "source_ts")


def flow_record(raw, batch_id, source_ts, oi_before=None,
                oi_before_trade_date=None):
    """One print, as observed. Immutable once written."""
    occ = parse_occ(raw.get("contract")) or {}
    ts = BT.parse_iso(raw.get("trade_ts") or raw.get("last_print_ts") or "")
    et = BT.to_et(ts) if ts else None
    rec = {
        "contract": (raw.get("contract") or "").upper(),
        "ticker": raw.get("ticker") or occ.get("root"),
        "right": raw.get("type") or occ.get("right"),
        "strike": raw.get("strike", occ.get("strike")),
        "expiry": raw.get("expiry") or occ.get("expiry"),
        "trade_date": (EC.session_of(ts, raw.get("contract")).isoformat()
                       if ts else None),
        "trade_ts_et": et.isoformat() if et else None,
        "exchange": raw.get("exchange"),
        "trade_id": raw.get("trade_id"),
        "contracts": raw.get("contracts", raw.get("size", raw.get("volume"))),
        "price": raw.get("price", raw.get("last_price")),
        "premium": raw.get("premium"),
        "nbbo_bid": raw.get("bid"),
        "nbbo_ask": raw.get("ask"),
        "spot_at_detection": raw.get("spot_at_print", raw.get("spot")),
        "action": raw.get("flow_side"),
        "direction": raw.get("direction"),
        "side_method": raw.get("side_method"),
        "oi_before": oi_before if oi_before is not None
        else raw.get("open_interest"),
        # the DATE that prior OI belongs to. Unknown is recorded as
        # unknown; it is a blocking condition later, not a default.
        "oi_before_trade_date": oi_before_trade_date,
        "batch_id": batch_id,
        "source_ts": source_ts,
    }
    rec["identity"] = print_identity(rec)
    return rec


def print_identity(rec):
    """A stable identity for one execution.

    Prefer the vendor's trade id. Without one, the documented composite is
    contract + exact timestamp + exchange + price + size: two genuinely
    distinct executions differing in none of those are indistinguishable
    in the data we hold, so collapsing them is the conservative choice —
    double-counting a print inflates the denominator and understates
    follow-through.
    """
    if rec.get("trade_id"):
        return "tid:%s:%s" % (rec.get("contract"), rec["trade_id"])
    parts = "|".join(str(rec.get(k)) for k in
                     ("contract", "trade_ts_et", "exchange", "price",
                      "contracts"))
    return "composite:" + hashlib.sha256(parts.encode("utf-8")).hexdigest()[:20]


def validate_flow(rec):
    missing = [f for f in REQUIRED_FLOW_FIELDS if rec.get(f) in (None, "")]
    problems = ["missing %s" % f for f in missing]
    if rec.get("contracts") is not None:
        try:
            if int(rec["contracts"]) <= 0:
                problems.append("contract quantity is not positive")
        except (TypeError, ValueError):
            problems.append("contract quantity is not a number")
    if rec.get("contract") and not parse_occ(rec["contract"]) \
            and not is_adjusted(rec["contract"]):
        problems.append("contract symbol is not a parseable OCC symbol")
    return problems


# ── 2. cross-batch deduplication ────────────────────────────────────────

def dedupe(records):
    """Collapse prints seen in more than one scan batch.

    The scanner re-reads the tape every few minutes, so the same execution
    appears in several batches. Counting it once per batch inflates
    observed contracts and makes follow-through look worse than it was.
    """
    seen, out, dropped = {}, [], 0
    for r in records:
        k = r.get("identity") or print_identity(r)
        if k in seen:
            dropped += 1
            # keep the earliest observation; later batches are re-reads
            if (r.get("source_ts") or "") < (seen[k].get("source_ts") or ""):
                out[out.index(seen[k])] = r
                seen[k] = r
            continue
        seen[k] = r
        out.append(r)
    return out, dropped


# ── 3. session finalization ─────────────────────────────────────────────

def aggregate_session(records, session_date, now_et=None):
    """Freeze one session's flow, aggregated by EXACT OCC contract.

    Returns (contracts, problems). Nothing is aggregated until the
    product's own close has passed, because a running total is not a
    daily total.
    """
    now = BT.to_et(now_et or datetime.now(BT.ET))
    sd = EC._d(session_date) if hasattr(EC, "_d") else session_date
    deduped, dropped = dedupe(records)
    by_contract, problems = {}, []
    for r in deduped:
        probs = validate_flow(r)
        if probs:
            problems.append("%s: %s" % (r.get("contract"), "; ".join(probs)))
            continue
        if r.get("trade_date") != (sd.isoformat() if hasattr(sd, "isoformat")
                                   else str(sd)):
            continue
        c = r["contract"]
        g = by_contract.setdefault(c, {
            "contract": c, "ticker": r.get("ticker"), "right": r.get("right"),
            "strike": r.get("strike"), "expiry": r.get("expiry"),
            "session_date": r["trade_date"], "prints": [], "identities": [],
            "observed_contracts": 0, "premium": 0.0,
            "oi_before": r.get("oi_before"),
            "oi_before_trade_date": r.get("oi_before_trade_date"),
        })
        g["prints"].append(r)
        g["identities"].append(r["identity"])
        g["observed_contracts"] += int(r["contracts"])
        g["premium"] += float(r.get("premium") or 0)

    out = []
    for g in by_contract.values():
        frozen = EC.is_frozen(g["session_date"], now, g["contract"])
        g["frozen"] = frozen
        g["frozen_at"] = BT.fmt_stamp(
            EC.session_close_dt(g["session_date"], g["contract"]))
        g["print_count"] = len(g["prints"])
        # the reconciliation the validator checks: the aggregate must be
        # the sum of the prints that produced it
        g["reconciles"] = (g["observed_contracts"]
                           == sum(int(p["contracts"]) for p in g["prints"]))
        # first and last print, for repeat-flow ranking
        ts = sorted(p["trade_ts_et"] for p in g["prints"] if p["trade_ts_et"])
        g["first_print_et"] = ts[0] if ts else None
        g["last_print_et"] = ts[-1] if ts else None
        g["distinct_print_times"] = len({t[:16] for t in ts})
        out.append(g)
    if dropped:
        problems.append("deduplicated %d repeated print(s) across batches"
                        % dropped)
    return out, problems


# ── 4. structure detection ──────────────────────────────────────────────

STRUCT_SINGLE = "likely single-leg"
STRUCT_VERTICAL = "likely vertical spread"
STRUCT_CALENDAR = "likely calendar spread"
STRUCT_ROLL = "likely roll"
STRUCT_STRADDLE = "likely straddle or strangle"
STRUCT_SYNTHETIC = "likely synthetic"
STRUCT_MULTI = "likely multi-leg"

# Legs of one structure print within seconds of each other and in
# comparable size. Wider than a true tied execution, because vendors that
# do not flag multi-leg trades scatter the legs across a short window.
LINK_WINDOW_SECONDS = 3
SIZE_RATIO = 0.8


def detect_structures(contracts):
    """Tag each aggregated contract with the structure it probably belongs
    to, and with the sibling legs that suggest it.

    A vertical's short leg looks exactly like a bearish bet if you read it
    alone. Presenting it that way is not a rounding error; it inverts the
    trade.
    """
    by_ticker = {}
    for g in contracts:
        by_ticker.setdefault(g.get("ticker"), []).append(g)

    for g in contracts:
        g["structure"] = STRUCT_SINGLE
        g["structure_confidence"] = "high"
        g["linked_legs"] = []

    for tk, group in by_ticker.items():
        for a in group:
            for b in group:
                if a is b or not a.get("first_print_et") or \
                        not b.get("first_print_et"):
                    continue
                ta = BT.parse_iso(a["first_print_et"])
                tb = BT.parse_iso(b["first_print_et"])
                if ta is None or tb is None:
                    continue
                if abs((ta - tb).total_seconds()) > LINK_WINDOW_SECONDS:
                    continue
                sa, sb = a["observed_contracts"], b["observed_contracts"]
                if not sa or not sb:
                    continue
                if min(sa, sb) / float(max(sa, sb)) < SIZE_RATIO:
                    continue
                kind = _pair_kind(a, b)
                if not kind:
                    continue
                a["linked_legs"].append({"contract": b["contract"],
                                         "relation": kind})
                a["structure"] = kind
                a["structure_confidence"] = "low"
    for g in contracts:
        if len(g["linked_legs"]) > 1:
            g["structure"] = STRUCT_MULTI
            g["structure_confidence"] = "low"
    return contracts


def _pair_kind(a, b):
    same_right = a.get("right") == b.get("right")
    same_expiry = a.get("expiry") == b.get("expiry")
    same_strike = a.get("strike") == b.get("strike")
    if same_right and same_expiry and not same_strike:
        return STRUCT_VERTICAL
    if same_right and not same_expiry and same_strike:
        return STRUCT_CALENDAR
    if same_right and not same_expiry and not same_strike:
        return STRUCT_ROLL
    if not same_right and same_expiry:
        return STRUCT_STRADDLE if same_strike else STRUCT_SYNTHETIC
    return None


# ── 5. OI readiness ─────────────────────────────────────────────────────

def oi_readiness(session_date, oi_snapshot, now_et=None,
                 sla=OI_SLA_ET, grace_minutes=OI_GRACE_MINUTES):
    """Is the vendor's OI actually the snapshot we need?

    Eligibility requires the vendor to SAY so: a trade date equal to the
    session under evaluation, a complete/final dataset status, and a
    recorded retrieval time. None of that is inferable from the clock —
    "it is 9:30, therefore yesterday's OI must be loaded" is how stale
    numbers get published as fresh ones.
    """
    now = BT.to_et(now_et or datetime.now(BT.ET))
    snap = oi_snapshot or {}
    want = (session_date.isoformat() if hasattr(session_date, "isoformat")
            else str(session_date))
    got_date = snap.get("oi_trade_date")
    status = (snap.get("dataset_status") or "").lower()
    received = snap.get("oi_received_at")

    reasons = []
    if not got_date:
        reasons.append("vendor did not state the OI trade date")
    elif got_date != want:
        reasons.append("vendor OI is dated %s, not %s" % (got_date, want))
    if status not in ("complete", "final"):
        reasons.append("dataset status is %r" % (status or "absent"))
    if not received:
        reasons.append("no retrieval timestamp recorded")
    if snap.get("open_interest") is None and not reasons:
        reasons.append("dataset reports no open-interest value")

    if not reasons:
        return {"ready": True, "state": None, "reasons": [],
                "oi_after": snap.get("open_interest"),
                "oi_after_trade_date": got_date,
                "oi_received_at": received,
                "vendor_dataset_status": status,
                "vendor_revision_id": snap.get("revision_id")}

    # not ready: pending while the SLA is open, delayed once it is not
    deadline = EC.session_close_dt(EC.next_session(session_date))
    deadline = deadline.replace(hour=sla[0], minute=sla[1]) + \
        timedelta(minutes=grace_minutes)
    state = OI_PENDING if now <= deadline else OI_DELAYED
    return {"ready": False, "state": state, "reasons": reasons,
            "sla_deadline": BT.fmt_stamp(deadline),
            "oi_after": None, "oi_after_trade_date": got_date,
            "oi_received_at": received,
            "vendor_dataset_status": status or None,
            "vendor_revision_id": snap.get("revision_id")}


# ── 6. follow-through evaluation ────────────────────────────────────────

THRESHOLD_VERSION = "2026-07-22.oos-v1"
# Calibrated out-of-sample; see calibrate(). Stored WITH each evaluation so
# a historical result can be reproduced against the thresholds that
# produced it rather than whatever is current.
STRONG_MIN = 0.55
PARTIAL_MIN = 0.15


def evaluate(contract, oi_snapshot, now_et=None,
             strong_min=STRONG_MIN, partial_min=PARTIAL_MIN,
             threshold_version=THRESHOLD_VERSION):
    """One contract's follow-through, or the reason there isn't one."""
    g = dict(contract)
    out = {
        "contract": g.get("contract"), "ticker": g.get("ticker"),
        "session_date": g.get("session_date"),
        "observed_contracts": g.get("observed_contracts"),
        "print_count": g.get("print_count"),
        "premium": g.get("premium"),
        "direction": g.get("direction") or _group_direction(g),
        "action": _group_action(g),
        "structure": g.get("structure", STRUCT_SINGLE),
        "structure_confidence": g.get("structure_confidence", "high"),
        "linked_legs": g.get("linked_legs") or [],
        "oi_before": g.get("oi_before"),
        "oi_before_trade_date": g.get("oi_before_trade_date"),
        "threshold_version": threshold_version,
        "measure": MEASURE_LABEL,
        "first_print_et": g.get("first_print_et"),
        "last_print_et": g.get("last_print_et"),
        "distinct_print_times": g.get("distinct_print_times"),
    }

    # not evaluable: the instrument itself cannot be compared
    reasons = []
    if is_adjusted(g.get("contract")):
        reasons.append("adjusted or non-standard series; open interest is "
                       "not comparable across the adjustment")
    if not parse_occ(g.get("contract")) and not is_adjusted(g.get("contract")):
        reasons.append("contract symbol is malformed")
    if g.get("expiry") and g.get("session_date") and \
            g["expiry"] < g["session_date"]:
        reasons.append("contract expired before the session being evaluated")
    if not g.get("observed_contracts"):
        reasons.append("no observed contract quantity")
    if g.get("oi_before") is None:
        reasons.append("prior open interest unknown")
    if not g.get("oi_before_trade_date"):
        reasons.append("prior open interest has no trade date")
    if g.get("reconciles") is False:
        reasons.append("aggregate does not reconcile with its prints")
    if reasons:
        out.update(state=NOT_EVALUABLE, state_reasons=reasons)
        return out

    ready = oi_readiness(g["session_date"], oi_snapshot, now_et)
    out.update({k: ready.get(k) for k in
                ("oi_after", "oi_after_trade_date", "oi_received_at",
                 "vendor_dataset_status", "vendor_revision_id")})
    if not ready["ready"]:
        out.update(state=ready["state"], state_reasons=ready["reasons"],
                   sla_deadline=ready.get("sla_deadline"))
        return out

    before = int(g["oi_before"])
    after = int(ready["oi_after"])
    delta = after - before
    positive = max(0, delta)
    observed = int(g["observed_contracts"])
    raw_ratio = positive / float(observed) if observed else None
    out.update({
        "oi_before": before, "oi_after": after, "delta_oi": delta,
        "positive_delta_oi": positive,
        # Capped for display because a ratio over 100% invites the reading
        # that our print created more OI than it traded; kept uncapped
        # internally because OTHER activity legitimately adds to the same
        # contract and the excess is information.
        "follow_through_ratio": min(1.0, raw_ratio) if raw_ratio is not None
        else None,
        "follow_through_ratio_uncapped": raw_ratio,
    })

    if g.get("structure_confidence") == "low":
        out.update(state=STRUCTURE_UNCLEAR, state_reasons=[
            "linked legs suggest a %s; a single leg of a structure is not "
            "an independent directional bet" % g.get("structure")])
        return out

    if raw_ratio is None:
        out.update(state=NOT_EVALUABLE,
                   state_reasons=["no denominator"])
    elif raw_ratio >= strong_min:
        out.update(state=STRONG, state_reasons=[])
    elif raw_ratio >= partial_min:
        out.update(state=PARTIAL, state_reasons=[])
    else:
        out.update(state=NO_FOLLOW, state_reasons=[
            "complete data arrived; net open interest did not materially "
            "increase"])
    return out


def _group_direction(g):
    dirs = {p.get("direction") for p in g.get("prints") or []}
    dirs.discard(None)
    return dirs.pop() if len(dirs) == 1 else "unresolved"


def _group_action(g):
    acts = {p.get("action") for p in g.get("prints") or []}
    acts.discard(None)
    return acts.pop() if len(acts) == 1 else "two-sided"


# ── 7. ranking ──────────────────────────────────────────────────────────

RANKABLE = (STRONG, PARTIAL, NO_FOLLOW)
MAX_PER_TICKER = 2


def rank(evaluations, limit=10, max_per_ticker=MAX_PER_TICKER):
    """Rank only contracts whose OI dataset is complete."""
    pool = [e for e in evaluations if e.get("state") in RANKABLE]

    def score(e):
        s = (e.get("follow_through_ratio") or 0) * 100
        s += min(40, (e.get("positive_delta_oi") or 0) / 250.0)
        s += min(20, (e.get("observed_contracts") or 0) / 500.0)
        s += min(20, (e.get("premium") or 0) / 1e6)
        s += 6 * min(3, (e.get("distinct_print_times") or 1) - 1)
        if e.get("structure_confidence") == "high":
            s += 8
        return -s

    per, out = {}, []
    for e in sorted(pool, key=score):
        tk = e.get("ticker")
        if per.get(tk, 0) >= max_per_ticker:
            continue
        per[tk] = per.get(tk, 0) + 1
        out.append(dict(e, rank=len(out) + 1))
        if len(out) >= limit:
            break
    return out


# ── 8. threshold calibration ────────────────────────────────────────────

def calibrate(history, split=0.6, grid=None):
    """Choose STRONG/PARTIAL cut-offs on an out-of-sample split.

    `history` is a list of {ratio, outcome} where outcome is True when the
    position was still open at the next OI snapshot. Fitting the cut-offs
    on the same data they are judged on is how a threshold comes to look
    predictive without being so, and choosing them after seeing today is
    the same error with extra steps — so the split is chronological and
    the report states both halves.
    """
    rows = [h for h in (history or [])
            if h.get("ratio") is not None and h.get("outcome") is not None]
    rows.sort(key=lambda h: h.get("date") or "")
    if len(rows) < 20:
        return {"ok": False,
                "reason": "only %d labelled observations; need 20 before a "
                          "threshold means anything" % len(rows),
                "strong_min": STRONG_MIN, "partial_min": PARTIAL_MIN,
                "version": THRESHOLD_VERSION}
    cut = int(len(rows) * split)
    train, test = rows[:cut], rows[cut:]
    grid = grid or [round(x / 20.0, 2) for x in range(1, 20)]

    def rate(sample, lo, hi=1.01):
        sel = [h for h in sample if lo <= h["ratio"] < hi]
        return (sum(1 for h in sel if h["outcome"]) / len(sel), len(sel)) \
            if sel else (0.0, 0)

    best, best_lift = None, -1
    for s in grid:
        r, n = rate(train, s)
        if n < 5:
            continue
        base = sum(1 for h in train if h["outcome"]) / len(train)
        if r - base > best_lift:
            best_lift, best = r - base, s
    strong = best if best is not None else STRONG_MIN
    partial_grid = [g for g in grid if g < strong]
    best_p, best_p_lift = None, -1
    for p in partial_grid:
        r, n = rate(train, p, strong)
        if n < 5:
            continue
        base = sum(1 for h in train if h["outcome"]) / len(train)
        if r - base > best_p_lift:
            best_p_lift, best_p = r - base, p
    partial = best_p if best_p is not None else PARTIAL_MIN

    tr_s, tr_sn = rate(train, strong)
    te_s, te_sn = rate(test, strong)
    te_p, te_pn = rate(test, partial, strong)
    te_n, te_nn = rate(test, 0.0, partial)
    return {
        "ok": True, "version": THRESHOLD_VERSION,
        "strong_min": strong, "partial_min": partial,
        "n_train": len(train), "n_test": len(test),
        "in_sample_strong_rate": round(tr_s, 3), "in_sample_strong_n": tr_sn,
        "out_of_sample": {
            "strong": {"rate": round(te_s, 3), "n": te_sn},
            "partial": {"rate": round(te_p, 3), "n": te_pn},
            "none": {"rate": round(te_n, 3), "n": te_nn},
        },
        "monotonic": te_s >= te_p >= te_n,
    }


# ── 9. display ──────────────────────────────────────────────────────────

def _fmt_int(n):
    return "{:,}".format(int(n)) if n is not None else "n/a"


def _fmt_dt(iso, with_time=True):
    d = BT.parse_iso(iso) if iso else None
    if not d:
        return ""
    et = BT.to_et(d)
    return et.strftime("%b %-d, %-I:%M %p ET").replace("AM", "a.m.").replace(
        "PM", "p.m.") if with_time else et.strftime("%b %-d")


def _fmt_dt_win(iso, with_time=True):
    """Windows has no %-d/%-I."""
    d = BT.parse_iso(iso) if iso else None
    if not d:
        return ""
    et = BT.to_et(d)
    h = et.hour % 12 or 12
    day = "%s %d" % (et.strftime("%b"), et.day)
    if not with_time:
        return day
    return "%s, %d:%02d %s ET" % (day, h, et.minute,
                                  "a.m." if et.hour < 12 else "p.m.")


fmt_dt = _fmt_dt_win


def lines_for(ev):
    """The display block for one evaluation. Data date and retrieval time
    are always separate values; direction and OI are always separate
    claims."""
    out = {
        "flow": "Flow: %s" % fmt_dt(ev.get("first_print_et")),
        "direction": "Direction: %s%s" % (
            ev.get("direction") or "unresolved",
            (", inferred from %s" % ev["action"].replace("_", " "))
            if ev.get("action") else ""),
        "structure": "Structure: %s" % ev.get("structure", STRUCT_SINGLE),
    }
    st = ev.get("state")
    if st in (OI_PENDING, OI_DELAYED):
        out["oi"] = "%s as of %s" % (
            "OI pending" if st == OI_PENDING else "OI data delayed",
            fmt_dt(datetime.now(BT.ET).isoformat()))
        if ev.get("oi_before_trade_date"):
            out["oi_latest"] = "Latest vendor OI: %s EOD" % fmt_dt(
                ev["oi_before_trade_date"] + "T00:00:00-04:00", False)
        return out
    if st == NOT_EVALUABLE:
        out["oi"] = "Not evaluable: %s" % "; ".join(
            ev.get("state_reasons") or [])
        return out
    if ev.get("oi_after") is not None:
        out["oi"] = "OI verified %s" % fmt_dt(ev.get("oi_received_at"))
        out["oi_change"] = "%s EOD OI: %s → %s · ΔOI %+d" % (
            fmt_dt((ev.get("oi_before_trade_date") or "")
                   + "T00:00:00-04:00", False),
            _fmt_int(ev["oi_before"]), _fmt_int(ev["oi_after"]),
            ev.get("delta_oi") or 0)
        r = ev.get("follow_through_ratio")
        if r is not None:
            out["follow_through"] = (
                "Follow-through: %s of %s observed contracts · %d%%"
                % (_fmt_int(ev.get("positive_delta_oi")),
                   _fmt_int(ev.get("observed_contracts")), round(r * 100)))
    if st == STRUCTURE_UNCLEAR:
        out["structure"] = "Structure: %s — not read as an independent " \
                           "directional bet" % ev.get("structure")
    return out


# ── self-test ───────────────────────────────────────────────────────────

def _print(contract, ts, size, batch, prem=100000.0, oi=1000,
           oi_date="2026-07-20", tid=None, exch="XCBO", price=1.0,
           side="call_buyer", direction="bullish"):
    return flow_record(
        {"contract": contract, "ticker": EC.root_of(contract),
         "trade_ts": ts, "contracts": size, "premium": prem,
         "open_interest": oi, "trade_id": tid, "exchange": exch,
         "price": price, "flow_side": side, "direction": direction,
         "spot": 100.0},
        batch_id=batch, source_ts=ts, oi_before=oi,
        oi_before_trade_date=oi_date)


def self_test():
    fails, ran = [], [0]

    def chk(n, c, d=""):
        ran[0] += 1
        print(("  PASS  " if c else "  FAIL  ") + n
              + ("" if c else "  <- %s" % (d,)))
        if not c:
            fails.append(n)

    C = "O:TSLA260821C00400000"
    T = "2026-07-21"
    now = datetime(2026, 7, 22, 10, 0, tzinfo=BT.ET)

    # ── schema + dedup
    print("\n-- schema and deduplication --")
    p1 = _print(C, "2026-07-21T14:30:00-04:00", 400, "b1", tid="X1")
    p2 = _print(C, "2026-07-21T14:30:00-04:00", 400, "b2", tid="X1")
    p3 = _print(C, "2026-07-21T15:10:00-04:00", 500, "b2", tid="X2")
    chk("a print records its OCC contract and ET timestamp",
        p1["contract"] == C and p1["trade_ts_et"].startswith("2026-07-21T14:30"))
    chk("the trade date is the exchange session, not the calendar day",
        p1["trade_date"] == T, p1["trade_date"])
    chk("prior OI carries its own trade date",
        p1["oi_before_trade_date"] == "2026-07-20")
    chk("a vendor trade id is preferred for identity",
        p1["identity"].startswith("tid:"))
    no_tid = _print(C, "2026-07-21T14:30:00-04:00", 400, "b1")
    chk("a composite identity is used when no trade id exists",
        no_tid["identity"].startswith("composite:"))
    chk("the composite is stable across batches",
        _print(C, "2026-07-21T14:30:00-04:00", 400, "b9")["identity"]
        == no_tid["identity"])
    deduped, dropped = dedupe([p1, p2, p3])
    chk("the same execution in two batches counts once",
        len(deduped) == 2 and dropped == 1, (len(deduped), dropped))
    chk("a missing quantity is a blocking problem",
        validate_flow({"contract": C, "trade_date": T,
                       "trade_ts_et": "x", "batch_id": "b",
                       "source_ts": "s"}))

    # ── session finalization
    print("\n-- session finalization --")
    groups, probs = aggregate_session([p1, p2, p3], T, now)
    chk("one aggregate per exact contract", len(groups) == 1, len(groups))
    g = groups[0]
    chk("observed contracts sum the deduplicated prints",
        g["observed_contracts"] == 900, g["observed_contracts"])
    chk("the aggregate reconciles with its prints", g["reconciles"])
    chk("the session is frozen after the product close", g["frozen"])
    chk("dedup is reported, not silent",
        any("deduplicated" in p for p in probs), probs)
    mid = datetime(2026, 7, 21, 12, 0, tzinfo=BT.ET)
    chk("an aggregate mid-session is NOT frozen",
        not aggregate_session([p1, p3], T, mid)[0][0]["frozen"])
    spx = _print("O:SPXW260821P06000000", "2026-07-21T16:07:00-04:00", 100,
                 "b1", tid="S1")
    chk("an index print at 16:07 belongs to that session",
        spx["trade_date"] == T, spx["trade_date"])
    eq_late = _print(C, "2026-07-21T16:07:00-04:00", 100, "b1", tid="E1")
    chk("an equity print at 16:07 rolls to the next session",
        eq_late["trade_date"] == "2026-07-22", eq_late["trade_date"])

    # ── structure detection
    print("\n-- structure detection --")
    long_leg = _print("O:AAPL260821C00300000", "2026-07-21T14:00:00-04:00",
                      500, "b1", tid="L1")
    short_leg = _print("O:AAPL260821C00320000", "2026-07-21T14:00:01-04:00",
                       500, "b1", tid="L2", side="call_seller",
                       direction="bearish")
    gs, _ = aggregate_session([long_leg, short_leg], T, now)
    detect_structures(gs)
    chk("two same-expiry strikes seconds apart read as a vertical",
        all(x["structure"] == STRUCT_VERTICAL for x in gs),
        [x["structure"] for x in gs])
    chk("a detected structure lowers confidence",
        all(x["structure_confidence"] == "low" for x in gs))
    chk("the sibling leg is recorded",
        gs[0]["linked_legs"][0]["contract"] == gs[1]["contract"])
    far = _print("O:AAPL260821C00320000", "2026-07-21T15:30:00-04:00", 500,
                 "b1", tid="L3")
    gs2, _ = aggregate_session([long_leg, far], T, now)
    detect_structures(gs2)
    chk("legs far apart in time are not linked",
        all(x["structure"] == STRUCT_SINGLE for x in gs2),
        [x["structure"] for x in gs2])
    cal_a = _print("O:MSFT260821C00500000", "2026-07-21T14:00:00-04:00", 300,
                   "b1", tid="C1")
    cal_b = _print("O:MSFT261218C00500000", "2026-07-21T14:00:01-04:00", 300,
                   "b1", tid="C2")
    gs3, _ = aggregate_session([cal_a, cal_b], T, now)
    detect_structures(gs3)
    chk("same strike, different expiry reads as a calendar",
        gs3[0]["structure"] == STRUCT_CALENDAR, gs3[0]["structure"])
    str_a = _print("O:NVDA260821C00200000", "2026-07-21T14:00:00-04:00", 200,
                   "b1", tid="T1")
    str_b = _print("O:NVDA260821P00200000", "2026-07-21T14:00:01-04:00", 200,
                   "b1", tid="T2")
    gs4, _ = aggregate_session([str_a, str_b], T, now)
    detect_structures(gs4)
    chk("call and put at one strike reads as a straddle",
        gs4[0]["structure"] == STRUCT_STRADDLE, gs4[0]["structure"])

    # ── readiness
    print("\n-- OI readiness --")
    r = oi_readiness(T, {}, now)
    chk("no vendor snapshot is PENDING inside the SLA",
        not r["ready"] and r["state"] == OI_PENDING, r)
    late = datetime(2026, 7, 22, 14, 0, tzinfo=BT.ET)
    chk("after the SLA plus grace it becomes DELAYED",
        oi_readiness(T, {}, late)["state"] == OI_DELAYED)
    stale = {"oi_trade_date": "2026-07-20", "dataset_status": "complete",
             "open_interest": 5000, "oi_received_at": now.isoformat()}
    chk("a snapshot dated to the WRONG session is not eligible",
        not oi_readiness(T, stale, now)["ready"],
        oi_readiness(T, stale, now)["reasons"])
    chk("the wrong-date reason names both dates",
        "2026-07-20" in oi_readiness(T, stale, now)["reasons"][0])
    partial_ds = {"oi_trade_date": T, "dataset_status": "partial",
                  "open_interest": 5000, "oi_received_at": now.isoformat()}
    chk("an incomplete dataset is not eligible",
        not oi_readiness(T, partial_ds, now)["ready"])
    no_recv = {"oi_trade_date": T, "dataset_status": "complete",
               "open_interest": 5000}
    chk("a snapshot with no retrieval timestamp is not eligible",
        not oi_readiness(T, no_recv, now)["ready"])
    good = {"oi_trade_date": T, "dataset_status": "complete",
            "open_interest": 5000, "oi_received_at": now.isoformat(),
            "revision_id": "rev-7"}
    chk("an explicitly session-dated complete snapshot IS eligible",
        oi_readiness(T, good, now)["ready"],
        oi_readiness(T, good, now)["reasons"])
    chk("readiness never consults the wall clock when data is present",
        oi_readiness(T, good, datetime(2026, 7, 25, 3, 0,
                                       tzinfo=BT.ET))["ready"])

    # ── evaluation
    print("\n-- follow-through evaluation --")
    base = groups[0]
    base["oi_before"] = 1000
    ev = evaluate(base, {"oi_trade_date": T, "dataset_status": "complete",
                         "open_interest": 1760,
                         "oi_received_at": now.isoformat()}, now)
    chk("delta and ratio use contract quantity, not premium",
        ev["delta_oi"] == 760 and abs(ev["follow_through_ratio"]
                                      - 760 / 900.0) < 1e-9, ev)
    chk("strong follow-through above the calibrated cut",
        ev["state"] == STRONG, ev["state"])
    chk("the threshold version travels with the result",
        ev["threshold_version"] == THRESHOLD_VERSION)
    chk("the measure is never called direction confirmed",
        ev["measure"] == MEASURE_LABEL and "CONFIRM" not in ev["state"])
    small = evaluate(base, {"oi_trade_date": T, "dataset_status": "complete",
                            "open_interest": 1180,
                            "oi_received_at": now.isoformat()}, now)
    chk("a smaller rise is partial", small["state"] == PARTIAL, small["state"])
    flat = evaluate(base, {"oi_trade_date": T, "dataset_status": "complete",
                           "open_interest": 1010,
                           "oi_received_at": now.isoformat()}, now)
    chk("no material rise is NO NET FOLLOW-THROUGH",
        flat["state"] == NO_FOLLOW, flat["state"])
    drop = evaluate(base, {"oi_trade_date": T, "dataset_status": "complete",
                           "open_interest": 200,
                           "oi_received_at": now.isoformat()}, now)
    chk("a fall in OI floors the ratio at zero, not negative",
        drop["positive_delta_oi"] == 0 and drop["follow_through_ratio"] == 0.0,
        drop)
    chk("the negative delta is still reported", drop["delta_oi"] == -800)
    huge = evaluate(base, {"oi_trade_date": T, "dataset_status": "complete",
                           "open_interest": 5000,
                           "oi_received_at": now.isoformat()}, now)
    chk("a ratio over 100% is capped for display",
        huge["follow_through_ratio"] == 1.0)
    chk("...but preserved uncapped internally",
        huge["follow_through_ratio_uncapped"] > 1.0,
        huge["follow_through_ratio_uncapped"])
    chk("pending while the vendor is silent",
        evaluate(base, {}, now)["state"] == OI_PENDING)
    chk("delayed after the SLA", evaluate(base, {}, late)["state"] == OI_DELAYED)
    chk("a delayed contract has no ratio",
        evaluate(base, {}, late).get("follow_through_ratio") is None)

    nodate = dict(base, oi_before_trade_date=None)
    chk("unknown prior-OI date is NOT EVALUABLE",
        evaluate(nodate, good, now)["state"] == NOT_EVALUABLE)
    adj = dict(base, contract="O:AAPL1260821C00300000")
    chk("an adjusted series is NOT EVALUABLE",
        evaluate(adj, good, now)["state"] == NOT_EVALUABLE,
        evaluate(adj, good, now)["state_reasons"])
    expired = dict(base, expiry="2026-07-17")
    chk("a contract expired before the session is NOT EVALUABLE",
        evaluate(expired, good, now)["state"] == NOT_EVALUABLE)
    unclear = dict(base, structure=STRUCT_VERTICAL,
                   structure_confidence="low")
    ue = evaluate(unclear, good, now)
    chk("a linked leg is STRUCTURE UNCLEAR, not a directional result",
        ue["state"] == STRUCTURE_UNCLEAR, ue["state"])
    chk("structure-unclear still reports the measured numbers",
        ue["delta_oi"] is not None)

    # ── ranking
    print("\n-- ranking --")
    evs = [dict(ev, ticker="AAA", contract="O:AAA1"),
           dict(ev, ticker="AAA", contract="O:AAA2"),
           dict(ev, ticker="AAA", contract="O:AAA3"),
           dict(small, ticker="BBB", contract="O:BBB1"),
           dict(evaluate(base, {}, now), ticker="CCC", contract="O:CCC1")]
    ranked = rank(evs, limit=10)
    chk("pending contracts are not ranked",
        all(r["state"] in RANKABLE for r in ranked), [r["state"] for r in ranked])
    chk("at most two contracts per ticker",
        sum(1 for r in ranked if r["ticker"] == "AAA") == 2,
        [r["ticker"] for r in ranked])
    chk("ranks are 1-based and dense",
        [r["rank"] for r in ranked] == list(range(1, len(ranked) + 1)))

    # ── calibration
    print("\n-- threshold calibration --")
    hist = []
    for i in range(120):
        ratio = (i % 20) / 20.0
        hist.append({"date": "2026-0%d-%02d" % (1 + i // 60, 1 + i % 28),
                     "ratio": ratio, "outcome": ratio > 0.5})
    cal = calibrate(hist)
    chk("calibration reports both halves",
        cal["ok"] and cal["n_train"] and cal["n_test"], cal)
    chk("out-of-sample rates are monotonic across buckets",
        cal["monotonic"], cal["out_of_sample"])
    chk("thin history refuses to produce a threshold",
        not calibrate(hist[:5])["ok"])
    chk("a refused calibration falls back to the stored version",
        calibrate(hist[:5])["version"] == THRESHOLD_VERSION)

    # ── display
    print("\n-- display language --")
    ln = lines_for(ev)
    chk("data date and retrieval time are separate lines",
        "OI verified" in ln["oi"] and "EOD OI:" in ln["oi_change"], ln)
    chk("the OI line shows before, after and delta",
        "1,000 → 1,760" in ln["oi_change"] and "+760" in ln["oi_change"],
        ln["oi_change"])
    chk("follow-through is stated as a count and a percentage",
        "760 of 900 observed contracts" in ln["follow_through"],
        ln["follow_through"])
    chk("direction is a separate claim from OI",
        ln["direction"].startswith("Direction:")
        and "OI" not in ln["direction"], ln["direction"])
    chk("structure is stated", ln["structure"].startswith("Structure:"))
    pend = lines_for(evaluate(base, {}, now))
    chk("pending shows an as-of and the latest vendor OI date",
        "OI pending as of" in pend["oi"] and "Latest vendor OI" in
        pend["oi_latest"], pend)
    chk("no display string claims direction was confirmed",
        not any("DIRECTION CONFIRMED" in v.upper() for v in ln.values()))

    print("\n%d/%d checks passed" % (ran[0] - len(fails), ran[0]))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(self_test())
