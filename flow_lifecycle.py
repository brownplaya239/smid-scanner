"""
flow_lifecycle.py — did the flagged flow actually STICK?

Tracks every contract that reaches a daily Top-list and follows its
open interest forward, so the pane can mark a position OPEN / PARTIAL /
CLOSED instead of leaving the reader to guess whether the "whale" is
still there.

WHAT OI CAN AND CANNOT TELL US (the honesty spine of this module):
  * OI settles OVERNIGHT at the OCC. It is NOT live. A contract flagged
    today carries yesterday's settled OI all day — today's flow shows up
    in tomorrow's number. So a same-day verdict is IMPOSSIBLE and every
    day-0 row is PENDING by construction, never guessed.
  * OI is NET across ALL market participants. If the original buyer sold
    5,000 and a different fund bought 5,000, OI is unchanged. A decline
    therefore means "positions in this contract were net reduced" — it
    can NEVER be attributed to the specific trader we flagged.
  * Exercise/assignment also removes OI and is indistinguishable from a
    close.
  * OI necessarily goes to zero at expiry: contracts past expiry are
    marked EXPIRED, never UNWOUND.

Verdicts (thresholds mirror uoa_alpha.oi_status so the pane and the
ledger never disagree):
  first settlement after the flag, retained = dOI / flag-day volume
    >= 50%  OPEN            flow opened positions that stuck
    15-50%  PARTIAL         some stuck, the rest closed intraday
    <  15%  CLOSED          day-trade / not a new position
  subsequent sessions, versus the position's PEAK OI
    >= 80% of peak  HELD
    30-80%          TRIMMED
    <  30%          UNWOUND

WHEN AN OI READ COUNTS (2026-11 accuracy audit):
  * The flag date is the Top list's own ET session date, and the flow
    volume is that session's volume — frozen; a later re-flag never swaps
    in another day's volume.
  * OI is read only on trading sessions. A read on session T shows the
    OI settled after the previous session, recorded as `asof`. Weekend /
    holiday runs read nothing (the vendor's number has not moved).
  * The vendor states no OI date, so an un-updated number is detected by
    value: a first read EXACTLY equal to the pre-flag baseline is treated
    as "vendor not updated yet" and retried next session — never turned
    into a permanent CLOSED 0%.
  * A first read reflecting a later settlement than the flag day (a retry,
    or a missed night) is kept but marked late (first_read_lag sessions);
    the published open/closed rates use on-time reads only. With no usable
    read within FIRST_READ_MAX_LAG sessions the contract is NO_DATA — no
    verdict.

  data/flow_lifecycle.json          tracking state (repo-committed)
  docs/reports/flow_lifecycle.json  compact lookup for the Top pane

    python flow_lifecycle.py            # nightly: register + verdict
    python flow_lifecycle.py --dry-run  # print, write nothing
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytz

import exchange_calendar as EC

ET = pytz.timezone("America/New_York")
_BASE = os.path.dirname(os.path.abspath(__file__))
LATEST = os.path.join(_BASE, "docs", "reports", "uoa_latest.json")
TOPLIST = os.path.join(_BASE, "docs", "reports", "uoa_top25_daily.json")
STATE = os.path.join(_BASE, "data", "flow_lifecycle.json")
OUT = os.path.join(_BASE, "docs", "reports", "flow_lifecycle.json")

CFG = {
    "open_pct": 50,  # >= this share of flow volume stuck -> OPEN
    "partial_pct": 15,  # >= this -> PARTIAL, below -> CLOSED
    "held_frac": 0.80,  # >= this share of peak OI -> HELD
    "trimmed_frac": 0.30,  # >= this -> TRIMMED, below -> UNWOUND
    "retain_days": 45,  # drop tracked contracts older than this
    "publish_window_days": 45,  # lookup window (matches archive depth)
    "min_stats_n": 30,  # gate for published aggregate stats
    "max_reads": 2500,  # per-night single-contract OI reads (API budget)
    "read_workers": 8,
    "first_read_max_lag": 2,  # sessions a first read may trail the flag day
}
SCHEMA = 2  # state written by this version (see migrate)
DERIVED = ("retained_pct", "first_verdict", "current_oi", "pct_of_peak")


def _load(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _et_today():
    return datetime.now(ET).strftime("%Y-%m-%d")


def key_of(ticker, expiry, typ, strike):
    """Same identity the Top pane uses, so the frontend joins directly."""
    return "%s|%s|%s|%g" % (ticker, expiry, typ, float(strike))


# ── 1. register today's Top-list contracts with their flag baseline ─────


def _et_date_of(iso):
    try:
        return (
            datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ET).strftime("%Y-%m-%d")
        )
    except Exception:
        return None


def register(state, top=None, snap=None):
    """Baseline for the contracts on the Top list's session D: OI as
    settled at the PRIOR close (uoa_latest.open_interest during D) + D's
    cumulative flow volume. The flag date is the list's own et_date (not
    the run date), and the volume is frozen to D — the verdict compares
    D's settlement to D's flow, never a later day's."""
    top = top if top is not None else (_load(TOPLIST) or {})
    snap = snap if snap is not None else (_load(LATEST) or {})
    flag_date = top.get("et_date")
    if not flag_date or not EC.is_session(flag_date):
        return 0
    if _et_date_of(snap.get("generated") or "") != flag_date:
        # the batch snapshot is from another session: its OI / volume are
        # not D's baseline, so register nothing rather than mix days
        print("  lifecycle: latest batch is not from %s — nothing registered" % flag_date)
        return 0
    by_key = {}
    for r in snap.get("rows") or []:
        if r.get("contract") and r.get("expiry") and r.get("type") and r.get("strike") is not None:
            by_key[key_of(r["ticker"], r["expiry"], r["type"], r["strike"])] = r
    added = 0
    for row in top.get("rows") or []:
        k = key_of(row["ticker"], row["expiry"], row["type"], row["strike"])
        src = by_key.get(k)
        if not src:
            continue  # not in the latest batch — no fresh OI read
        c = state["contracts"].get(k)
        if c is None:
            state["contracts"][k] = {
                "occ": src.get("contract"),
                "ticker": row["ticker"],
                "type": row["type"],
                "strike": row["strike"],
                "expiry": row["expiry"],
                "flag_date": flag_date,
                "baseline_oi": src.get("open_interest"),
                "flow_vol": src.get("volume"),
                "side": row.get("side"),
                "liquidity": row.get("liquidity"),
                "best_rank": row.get("rank"),
                "peak_oi": src.get("open_interest"),
                "series": [],
                "status": "PENDING",
            }
            added += 1
        else:
            # Same session re-run: the cumulative volume can only grow.
            # A LATER day's re-flag keeps the ORIGINAL baseline and volume
            # (the question is whether the FIRST position stuck).
            if c.get("flag_date") == flag_date:
                c["flow_vol"] = max(c.get("flow_vol") or 0, src.get("volume") or 0)
            if row.get("rank") and row["rank"] < (c.get("best_rank") or 999):
                c["best_rank"] = row["rank"]
    return added


# ── 2. current OI for tracked contracts needing a verdict ───────────────


def _lag(flag_date, asof):
    """Sessions between the flag session and the settlement a read shows
    (0 = the read reflects exactly the flag day's trading)."""
    return len(EC.sessions_between(flag_date, asof)) - 1


def _needs_oi(c, today, asof):
    """today is a session; asof = the settlement its OI reflects."""
    if c.get("no_data"):
        return False
    if c.get("expiry") and c["expiry"] < today:
        return False  # expired: handled without a fetch
    fd = c.get("flag_date")
    if not fd or asof < fd:
        return False  # the flag day has not settled yet
    ser = c.get("series") or []
    if ser:
        return ser[-1].get("date") != today  # already read today?
    if today in (c.get("stale_reads") or []):
        return False
    return _lag(fd, asof) <= CFG["first_read_max_lag"]


def _read_contract_oi(c):
    import polygon_data as pg

    snap = pg.option_contract(c["ticker"], c["occ"])
    if not snap:
        return None
    oi = snap.get("open_interest")
    if oi is None:
        oi = (snap.get("details") or {}).get("open_interest")
    return oi


def fetch_oi(state, today, read=None):
    """Read settled OI for every contract that needs it — one cheap
    single-contract snapshot each, first reads before follow-ups (their
    window is short). Only on trading sessions. Returns (updated, stale,
    missed). CI-only in practice (needs POLYGON_API_KEY)."""
    if not EC.is_session(today):
        return 0, 0, 0
    asof = EC.previous_session(today).isoformat()
    need = [c for c in state["contracts"].values() if c.get("occ") and _needs_oi(c, today, asof)]
    need.sort(key=lambda c: (bool(c.get("series")), c.get("flag_date") or ""))
    need = need[: CFG["max_reads"]]
    read = read or _read_contract_oi

    def _safe(c):
        try:
            return c, read(c)
        except Exception:
            return c, None

    with ThreadPoolExecutor(max_workers=CFG["read_workers"]) as ex:
        results = list(ex.map(_safe, need))
    updated = stale = missed = 0
    for c, oi in results:
        if oi is None:
            missed += 1
            continue
        oi = int(oi)
        first = not c.get("series")
        if first and oi == c.get("baseline_oi"):
            # exactly the pre-flag number: the vendor has not applied the
            # flag day's settlement yet — retry, never a CLOSED verdict
            c.setdefault("stale_reads", []).append(today)
            stale += 1
            continue
        if first:
            c["first_read_lag"] = _lag(c["flag_date"], asof)
        c.setdefault("series", []).append({"date": today, "asof": asof, "oi": oi})
        c["series"] = c["series"][-12:]
        c["peak_oi"] = max(c.get("peak_oi") or 0, oi)
        updated += 1
    # Tonight was the last session whose read could still count as this
    # contract's first: with nothing usable by now there is no verdict.
    for c in state["contracts"].values():
        fd = c.get("flag_date")
        if (
            fd
            and not c.get("series")
            and not c.get("no_data")
            and fd <= asof
            and not (c.get("expiry") and c["expiry"] < today)
            and _lag(fd, asof) >= CFG["first_read_max_lag"]
        ):
            c["no_data"] = True
    return updated, stale, missed


# ── 3. classify ────────────────────────────────────────────────────────


def _verdict(c):
    """Status from the OI series alone (expiry is applied by classify)."""
    ser = c.get("series") or []
    if not ser:
        return "NO_DATA" if c.get("no_data") else "PENDING"
    base = c.get("baseline_oi")
    vol = c.get("flow_vol") or 0
    if base is None or vol <= 0:
        return "UNKNOWN"
    first = ser[0]["oi"]
    retained = round(100.0 * (first - base) / vol)
    c["retained_pct"] = retained
    if retained >= CFG["open_pct"]:
        verdict = "OPEN"
    elif retained >= CFG["partial_pct"]:
        verdict = "PARTIAL"
    else:
        verdict = "CLOSED"
    c["first_verdict"] = verdict
    cur = ser[-1]["oi"]
    c["current_oi"] = cur
    if verdict == "CLOSED" or len(ser) == 1:
        return verdict
    peak = c.get("peak_oi") or first or 1
    frac = cur / peak if peak else 0
    c["pct_of_peak"] = round(100 * frac)
    if cur <= (base or 0):
        return "UNWOUND"
    if frac >= CFG["held_frac"]:
        return "HELD"
    if frac >= CFG["trimmed_frac"]:
        return "TRIMMED"
    return "UNWOUND"


def classify(state, today):
    for c in state["contracts"].values():
        for k in DERIVED:
            c.pop(k, None)
        st = _verdict(c)
        # expiry dominates: OI necessarily decays to 0 at expiration, so
        # an expired contract is never reported as "unwound"
        if c.get("expiry") and c["expiry"] < today:
            st = (
                "EXPIRED_UNRESOLVED" if st in ("PENDING", "NO_DATA", "UNKNOWN") else "EXPIRED_" + st
            )
        c["status"] = st


def migrate(state):
    """Clean state written before SCHEMA 2 (once): reads taken on
    non-sessions are dropped; a contract registered by a non-session run
    is moved to the session its list belonged to; a first read equal to
    the baseline (vendor not updated) is dropped; a first read that came
    more than FIRST_READ_MAX_LAG sessions late gives NO_DATA, not a
    verdict. Flow volume bumped by an old re-flag cannot be recovered."""
    if state.get("schema") == SCHEMA:
        return 0
    fixed = 0
    for c in state.get("contracts", {}).values():
        before = json.dumps(c.get("series") or []) + str(c.get("flag_date"))
        fd = c.get("flag_date")
        try:
            if fd and not EC.is_session(fd):
                fd = c["flag_date"] = EC.previous_session(fd).isoformat()
            ser = []
            for p in c.get("series") or []:
                if not EC.is_session(p["date"]):
                    continue
                p.setdefault("asof", EC.previous_session(p["date"]).isoformat())
                if fd and p["asof"] >= fd:
                    ser.append(p)
        except EC.CalendarRangeError:
            continue
        while ser and ser[0]["oi"] == c.get("baseline_oi"):
            ser.pop(0)
        if ser and fd:
            lag = _lag(fd, ser[0]["asof"])
            if lag > CFG["first_read_max_lag"]:
                ser, c["no_data"] = [], True
            else:
                c["first_read_lag"] = lag
        c["series"] = ser
        c["peak_oi"] = max([c.get("baseline_oi") or 0] + [p["oi"] for p in ser])
        if json.dumps(ser) + str(c.get("flag_date")) != before:
            fixed += 1
    state["schema"] = SCHEMA
    return fixed


def prune(state, today):
    cut = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=CFG["retain_days"])).strftime(
        "%Y-%m-%d"
    )
    for k in [k for k, c in state["contracts"].items() if (c.get("flag_date") or "") < cut]:
        del state["contracts"][k]


# ── 4. publish ─────────────────────────────────────────────────────────


def publish(state, today):
    # Covers the live Top list AND the archived sessions the date
    # toggle can reach, so looking back at an old list shows which of
    # those positions actually stuck. Compact array encoding keeps the
    # whole window ~40% the size of per-key objects:
    #   [status, retained_pct, pct_of_peak, baseline_oi, current_oi,
    #    flag_date, first_read_lag, stale_reads]
    cutoff = (
        datetime.strptime(today, "%Y-%m-%d") - timedelta(days=CFG["publish_window_days"])
    ).strftime("%Y-%m-%d")
    lookup = {}
    for k, c in state["contracts"].items():
        if (c.get("flag_date") or "") < cutoff:
            continue
        lookup[k] = [
            c.get("status", "PENDING"),
            c.get("retained_pct"),
            c.get("pct_of_peak"),
            c.get("baseline_oi"),
            c.get("current_oi"),
            c.get("flag_date"),
            c.get("first_read_lag"),
            len(c.get("stale_reads") or []),
        ]
    # gated aggregate: how often does flagged flow actually stick? Only
    # first reads that show exactly the flag day's settlement count — a
    # late read also contains later days' trading.
    resolved = [
        c
        for c in state["contracts"].values()
        if c.get("first_verdict") and c.get("first_read_lag") == 0
    ]
    late = sum(
        1 for c in state["contracts"].values() if c.get("first_verdict") and c.get("first_read_lag")
    )
    no_data = sum(1 for c in state["contracts"].values() if c.get("no_data"))
    stats = {
        "status": "accruing",
        "n": len(resolved),
        "activates_at": CFG["min_stats_n"],
        "late_excluded": late,
        "no_data": no_data,
    }
    if len(resolved) >= CFG["min_stats_n"]:
        cnt = Counter(c["first_verdict"] for c in resolved)
        n = len(resolved)
        by_side = {}
        for s in {c.get("side") for c in resolved if c.get("side")}:
            sub = [c for c in resolved if c.get("side") == s]
            if len(sub) >= CFG["min_stats_n"]:
                by_side[s] = round(
                    100 * sum(1 for c in sub if c["first_verdict"] == "OPEN") / len(sub)
                )
        stats = {
            "status": "active",
            "n": n,
            "open_pct": round(100 * cnt["OPEN"] / n),
            "partial_pct": round(100 * cnt["PARTIAL"] / n),
            "closed_pct": round(100 * cnt["CLOSED"] / n),
            "open_rate_by_side": by_side,
            "late_excluded": late,
            "no_data": no_data,
        }
    payload = {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "et_date": today,
        "tracked_total": len(state["contracts"]),
        "encoding": [
            "status",
            "retained_pct",
            "pct_of_peak",
            "baseline_oi",
            "current_oi",
            "flag_date",
            "first_read_lag",
            "stale_reads",
        ],
        "contracts": lookup,
        "stats": stats,
        "thresholds": {k: CFG[k] for k in ("open_pct", "partial_pct", "held_frac", "trimmed_frac")},
        "note": (
            "Open interest settles OVERNIGHT at the OCC — a "
            "contract flagged today gets its verdict tomorrow, so "
            "same-day rows read PENDING by construction. OI is read "
            "only on trading sessions; a read that still equals the "
            "pre-flag OI is treated as not-yet-updated and retried, and "
            "with no usable read within two sessions the contract gets "
            "no verdict (NO_DATA). Open/closed rates use reads of the "
            "flag day's own settlement only. OI is NET "
            "across ALL participants: a decline means positions in "
            "the contract were net reduced and can never be "
            "attributed to the specific trader flagged. "
            "Exercise/assignment is indistinguishable from a close. "
            "Contracts past expiry are marked EXPIRED, never "
            "UNWOUND. Educational, not advice."
        ),
    }
    return payload


def self_test():
    """Offline fixtures for every classification path, incl. the traps:
    same-day verdicts must stay PENDING, and expiry must never read as
    an unwind."""
    fails = []

    def chk(name, cond):
        print(("  PASS  " if cond else "  FAIL  ") + name)
        if not cond:
            fails.append(name)

    def mk(**kw):
        c = {
            "ticker": "TST",
            "type": "call",
            "strike": 100.0,
            "expiry": "2026-12-18",
            "flag_date": "2026-07-20",
            "baseline_oi": 1000,
            "flow_vol": 10000,
            "peak_oi": 1000,
            "series": [],
            "status": "PENDING",
        }
        c.update(kw)
        return c

    T = "2026-07-21"
    # 1. flagged today -> PENDING regardless of anything else
    s = {"contracts": {"a": mk(flag_date=T, series=[])}}
    classify(s, T)
    chk("1 same-day flag stays PENDING", s["contracts"]["a"]["status"] == "PENDING")

    # 2. OI rose by 60% of flow volume -> OPEN
    s = {"contracts": {"a": mk(series=[{"date": T, "oi": 7000}], peak_oi=7000)}}
    classify(s, T)
    c = s["contracts"]["a"]
    chk("2 retained 60% -> OPEN", c["status"] == "OPEN" and c["retained_pct"] == 60)

    # 3. OI rose by 25% -> PARTIAL
    s = {"contracts": {"a": mk(series=[{"date": T, "oi": 3500}], peak_oi=3500)}}
    classify(s, T)
    chk("3 retained 25% -> PARTIAL", s["contracts"]["a"]["status"] == "PARTIAL")

    # 4. OI barely moved -> CLOSED (day trade)
    s = {"contracts": {"a": mk(series=[{"date": T, "oi": 1050}], peak_oi=1050)}}
    classify(s, T)
    chk("4 retained 0.5% -> CLOSED", s["contracts"]["a"]["status"] == "CLOSED")

    # 5. opened then decayed to 35% of peak -> TRIMMED
    s = {
        "contracts": {
            "a": mk(
                series=[{"date": "2026-07-21", "oi": 7000}, {"date": T, "oi": 2450}], peak_oi=7000
            )
        }
    }
    classify(s, T)
    c = s["contracts"]["a"]
    chk("5 35% of peak -> TRIMMED", c["status"] == "TRIMMED" and c["first_verdict"] == "OPEN")

    # 6. opened then back to baseline -> UNWOUND
    s = {
        "contracts": {
            "a": mk(
                series=[{"date": "2026-07-21", "oi": 7000}, {"date": T, "oi": 950}], peak_oi=7000
            )
        }
    }
    classify(s, T)
    chk("6 back to baseline -> UNWOUND", s["contracts"]["a"]["status"] == "UNWOUND")

    # 7. THE TRAP: an expired contract's OI necessarily goes to 0 —
    #    must read EXPIRED_*, never UNWOUND
    s = {"contracts": {"a": mk(expiry="2026-07-17", status="OPEN", series=[{"date": T, "oi": 0}])}}
    classify(s, T)
    chk(
        "7 expired contract is EXPIRED_*, not UNWOUND",
        s["contracts"]["a"]["status"].startswith("EXPIRED"),
    )

    # 8. expired before any verdict
    s = {"contracts": {"a": mk(expiry="2026-07-17")}}
    classify(s, T)
    chk(
        "8 expired w/o verdict -> EXPIRED_UNRESOLVED",
        s["contracts"]["a"]["status"] == "EXPIRED_UNRESOLVED",
    )

    # 9. no OI read yet -> stays PENDING (never guessed)
    s = {"contracts": {"a": mk(series=[])}}
    classify(s, T)
    chk("9 no OI read -> PENDING", s["contracts"]["a"]["status"] == "PENDING")

    print("\n%d/9 fixtures passed" % (9 - len(fails)))
    return 0 if not fails else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        sys.exit(self_test())
    today = _et_today()
    state = _load(STATE) or {"contracts": {}, "schema": SCHEMA}
    fixed = migrate(state)
    if fixed:
        print("  flow lifecycle: migrated %d contracts to schema %d" % (fixed, SCHEMA))
    added = register(state)
    if not EC.is_session(today):
        print("  flow lifecycle: %s is not a trading session — no OI reads" % today)
    updated, stale, missed = fetch_oi(state, today)
    classify(state, today)
    prune(state, today)
    payload = publish(state, today)
    cnt = Counter(c.get("status") for c in state["contracts"].values())
    print(
        "  flow lifecycle: +%d new · %d OI reads · %d not yet updated by "
        "the vendor (retry) · %d unread · tracking %d · %s"
        % (
            added,
            updated,
            stale,
            missed,
            len(state["contracts"]),
            " ".join("%s=%d" % (k, v) for k, v in sorted(cnt.items())),
        )
    )
    if args.dry_run:
        print(json.dumps(payload["stats"], indent=1))
        return
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"))
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    print("  Wrote flow_lifecycle.json (%d in current top list)" % len(payload["contracts"]))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
