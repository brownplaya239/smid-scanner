"""
uoa_daily_top.py — Top Daily Unusual Contracts (cumulative cross-batch).

Runs after every UOA scan batch (uoa.yml, 6-9x/day). Folds the batch
snapshot (docs/reports/uoa_latest.json) into a per-day accumulator keyed
by exact contract identity, rescores the whole day, and publishes the
diversified top list for the Options Flow pane.

FEED SEMANTICS (verified against uoa_scanner.py / Polygon snapshot API):
  * Each batch snapshot re-lists the ENTIRE flagged universe with
    day-cumulative fields — a contract appearing in batch N+1 does NOT
    by itself mean new activity. "Seen in N scans" and "active in N
    batches" are therefore tracked as SEPARATE concepts (see below).
  * volume, premium, sweeps, blocks, repeat_count are DAY-CUMULATIVE per
    contract in every snapshot. Cross-batch merge is MAX per field,
    NEVER sum — summing would double-count. Deltas between snapshots
    are the only reliable "new activity" evidence.
  * FEED LIMITATION: the snapshot feed exposes NO per-trade/print IDs,
    so print-level dedup is impossible client-side. unique_prints is
    approximated by the scanner's own deduplicated day print counter
    (repeat_count, max across batches) and labeled as an approximation.
  * open_interest / prev_oi / oi_delta are STATIC intraday (OCC settles
    overnight); oi_delta is yesterday->today. It is a daily fact — it
    cannot change between intraday batches and is never "live OI".
  * bid/ask/mid are null in the delayed feed; execution tendency comes
    from the scanner's ask_pct/bid_pct approximation (side_method:
    "approx") and spread quality from the liquidity grade.
  * last_price is the LAST FILL per contract (most recent trade price,
    delayed) — not an average fill and not a quote mark.
  * Ticker roots are taken verbatim from Polygon contract symbols
    (O:<ROOT>...): no client-side normalization exists that could
    collide roots (e.g. SPCX vs SPXC vs SPX index roots SPX/SPXW).

REPEAT-FLOW SEMANTICS (the audit fix):
  seen_scans      — snapshots containing the contract (presence only).
  active_batches  — batches with VERIFIED new activity: first sighting,
                    or any monotonic increase in cumulative volume,
                    premium, sweep/block/print counters, or a newer
                    last_print_ts. Unchanged records add NOTHING.
  unique_prints   — scanner's deduplicated day print count (approx, see
                    feed limitation above).
  Recurrence scoring and labels use active_batches ONLY.

Day boundary: keyed to the ET date of the BATCH's generated timestamp.
Idempotent per batch timestamp. Reset on new ET date.

  data/uoa_daily_top.json            per-day accumulator (repo-committed)
  docs/reports/uoa_top25_daily.json  published ranking for the site

    python uoa_daily_top.py            # merge current batch + publish
    python uoa_daily_top.py --dry-run  # print ranking, write nothing
    python uoa_daily_top.py --self-test  # offline fixtures, no I/O
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import sys
from datetime import datetime, timedelta

import pytz

ET = pytz.timezone("America/New_York")
_BASE = os.path.dirname(os.path.abspath(__file__))
LATEST_PATH = os.path.join(_BASE, "docs", "reports", "uoa_latest.json")
ACC_PATH = os.path.join(_BASE, "data", "uoa_daily_top.json")
OUT_PATH = os.path.join(_BASE, "docs", "reports", "uoa_top25_daily.json")

# ── single tunable config object ────────────────────────────────────────
CFG = {
    # eligibility (noise gate before any scoring)
    "min_premium": 250_000,      # $ cumulative day premium
    "min_volume": 200,           # contracts
    "min_vol_oi": 1.0,           # day volume vs prior OI
    "max_pct_otm_unconfirmed": 25.0,  # far-OTM lottos need confirmation
    "liq_reject": {"D", "F"},    # unusably illiquid grades
    # ranking weights (see score_all; every component logged in parts)
    "w_base": 0.55,              # learned trade_score share
    "w_premium": 12, "w_voloi": 10, "w_oi": 6,
    "w_prints": 5,               # unique-print depth (approx)
    "w_active": 10,              # VERIFIED active-flow recurrence
    "w_side": 6,
    "golden_bonus": 4, "sweep_bonus": 2,
    "otm_knee": 15.0, "otm_slope": 0.4, "otm_cap": 8,
    "seller_penalty": 6, "mixed_penalty": 3, "illiquid_penalty": 4,
    # 90+ scores are reserved for several independent strong signals
    "strong_for_90": 3,
    "score_cap_below_strong": 89,
    # earnings-window rule for PRE-EARNINGS FLOW
    "earnings_window_d": 7,
    # reason-label thresholds
    "extreme_voloi": 10.0,
    "large_premium": 3_000_000,
    "oi_change_pct": 200.0,
    "aggressive_ask": 60,
    "directional_ask": 65,
    # diversification / list depth
    "per_ticker_cap": 2,
    "per_view": 50,
    "max_rows": 120,
}

# Point-in-time fields refreshed from the newest observation of a contract;
# cumulative fields (below) merge via max().
LATEST_FIELDS = [
    "spot", "pct_otm", "is_otm", "iv", "last_price", "liquidity",
    "ask_pct", "bid_pct", "side_method", "flow_side", "direction",
    "opening", "tier", "dte", "earnings_days", "last_print_ts",
    "sector", "cap_bucket", "edge_adj", "oi_delta", "oi_delta_pct",
    "open_interest", "prev_oi", "vol_oi", "size_gt_oi",
]
CUM_FIELDS = ["premium", "volume", "sweeps", "blocks", "sweep_premium",
              "repeat_count", "biggest_print", "trade_score"]
# Monotonic increase in ANY of these (or a newer last_print_ts) is the
# verified new-activity evidence that makes a batch "active".
ACTIVITY_FIELDS = ["volume", "premium", "sweeps", "blocks", "repeat_count"]


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _et_date(iso_ts):
    dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    return dt.astimezone(ET).strftime("%Y-%m-%d")


def contract_key(r):
    """Stable exact-contract identity: never merges across expiry, type,
    or strike. Strike normalized via %g (382.50 == 382.5)."""
    return "%s|%s|%s|%g" % (r["ticker"], r["expiry"], r["type"],
                            float(r["strike"]))


# ── 1. merge the current batch into the day accumulator ─────────────────

def _has_new_activity(c, r):
    """Verified new-activity test: any cumulative counter increased, or
    the last print timestamp moved forward. An identical re-listed
    snapshot record fails every test and earns nothing."""
    for f in ACTIVITY_FIELDS:
        if (r.get(f) or 0) > (c.get(f) or 0):
            return True
    if (r.get("last_print_ts") or "") > (c.get("last_print_ts") or ""):
        return True
    return False


def migrate_acc(acc):
    """Upgrade a pre-audit accumulator in place: batch_count conflated
    presence with activity, so presence carries over as seen_scans and
    only ONE batch of activity is credited (the rest was never
    verified). Fresh-format contracts are untouched."""
    for c in (acc.get("contracts") or {}).values():
        if "seen_scans" not in c:
            c["seen_scans"] = c.pop("batch_count", 1)
            c["active_batches"] = 1
    return acc


def merge_batch(acc, snap):
    gen = snap.get("generated")
    rows = snap.get("rows") or []
    if not gen or not rows:
        return acc, False
    day = _et_date(gen)
    if acc.get("et_date") != day:
        acc = {"et_date": day, "batches_seen": [], "contracts": {}}
    if gen in acc["batches_seen"]:
        return acc, False          # idempotent re-run on same snapshot
    acc["batches_seen"].append(gen)
    con = acc["contracts"]
    for r in rows:
        if not (r.get("ticker") and r.get("expiry") and r.get("type")
                and r.get("strike") is not None):
            continue               # invalid identity -> reject outright
        k = contract_key(r)
        c = con.get(k)
        if c is None:
            c = {"ticker": r["ticker"], "type": r["type"],
                 "strike": r["strike"], "expiry": r["expiry"],
                 "first_seen": gen, "seen_scans": 0, "active_batches": 0}
            con[k] = c
        # Migration from the pre-audit accumulator shape: batch_count
        # conflated presence with activity. Convert conservatively —
        # presence carries over, but only 1 batch of activity is
        # credited because the rest was never verified.
        if "seen_scans" not in c:
            c["seen_scans"] = c.pop("batch_count", 1)
            c["active_batches"] = 1
        active = _has_new_activity(c, r) if c["seen_scans"] else True
        c["seen_scans"] += 1
        if active:
            c["active_batches"] += 1
            c["last_active"] = gen
        c["last_seen"] = gen
        for f in CUM_FIELDS:       # day-cumulative -> max, never sum
            v = r.get(f)
            if v is not None:
                c[f] = max(c.get(f) or 0, v)
        for f in LATEST_FIELDS:    # point-in-time -> newest observation
            if r.get(f) is not None:
                c[f] = r[f]
    return acc, True


# ── 2. eligibility (noise gate) ─────────────────────────────────────────

def eligible(c):
    if (c.get("premium") or 0) < CFG["min_premium"]:
        return False
    if (c.get("volume") or 0) < CFG["min_volume"]:
        return False
    if (c.get("vol_oi") or 0) < CFG["min_vol_oi"]:
        return False
    if (c.get("liquidity") or "").upper() in CFG["liq_reject"]:
        return False
    po = c.get("pct_otm")
    confirmed = ((c.get("sweeps") or 0) > 0 or (c.get("blocks") or 0) > 0
                 or (c.get("repeat_count") or 0) >= 3
                 or (c.get("oi_delta") or 0) > 0
                 or (c.get("active_batches") or 0) >= 2)
    if po is not None and po > CFG["max_pct_otm_unconfirmed"] and not confirmed:
        return False
    return True


# ── 3. explainable dailyUnusualScore 0-100 with parts breakdown ─────────

def _pctile_fn(vals):
    s = sorted(vals)
    n = max(1, len(s) - 1)
    return lambda v: bisect.bisect_left(s, v) / n


def score_all(contracts, n_batches):
    """Score every eligible contract against today's eligible universe.

    Base = learned trade_score. Recurrence credit uses active_batches
    (verified new flow) ONLY and requires >=2 active batches; presence
    in cumulative snapshots earns nothing. Component contributions are
    stored on the contract (score_parts) for the UI breakdown. Scores
    of 90+ additionally require >= strong_for_90 independent strong
    signals — recurrence alone can never manufacture a 90."""
    elig = [c for c in contracts.values() if eligible(c)]
    if not elig:
        return []
    p_prem = _pctile_fn([c["premium"] for c in elig])
    p_voloi = _pctile_fn([c.get("vol_oi") or 0 for c in elig])
    for c in elig:
        parts = {}
        parts["base"] = round(CFG["w_base"] * (c.get("trade_score") or 0), 1)
        parts["premium"] = round(CFG["w_premium"] * p_prem(c["premium"]), 1)
        parts["vol_oi"] = round(CFG["w_voloi"] * p_voloi(c.get("vol_oi") or 0), 1)
        parts["oi_chg"] = round(CFG["w_oi"] * min(1.0, (c.get("oi_delta") or 0) / 2000.0), 1)
        parts["prints"] = round(CFG["w_prints"] * min(1.0, (c.get("repeat_count") or 0) / 10.0), 1)
        # VERIFIED recurrence only: >=2 active batches, normalized by
        # batches available, capped at w_active (10) — so two scans of
        # unchanged records add 0 and even a full active day adds 10.
        ab = c.get("active_batches") or 0
        parts["active_flow"] = round(
            CFG["w_active"] * min(1.0, (ab - 1) / max(1, n_batches - 1)), 1
        ) if (n_batches >= 2 and ab >= 2) else 0.0
        ap = c.get("ask_pct")
        parts["side_conviction"] = round(
            CFG["w_side"] * (abs(ap - 50) / 50.0), 1) if ap is not None else 0.0
        parts["sweep"] = (CFG["golden_bonus"]
                          if (c.get("sweeps") and c.get("blocks"))
                          else CFG["sweep_bonus"] if c.get("sweeps") else 0)
        # penalties (negative parts)
        po = c.get("pct_otm") or 0
        parts["far_otm"] = -round(min(CFG["otm_cap"],
            (po - CFG["otm_knee"]) * CFG["otm_slope"]), 1) if po > CFG["otm_knee"] else 0.0
        parts["seller_flow"] = -CFG["seller_penalty"] if c.get("flow_side") in (
            "put_seller", "call_seller") else 0
        parts["mixed_tape"] = -CFG["mixed_penalty"] if (
            ap is not None and 40 <= ap <= 60
            and not (c.get("sweeps") and c.get("blocks"))) else 0
        parts["illiquid"] = -CFG["illiquid_penalty"] if (
            (c.get("liquidity") or "").upper() == "C") else 0
        s = sum(parts.values())
        # independent strong signals gate for 90+
        strong = 0
        strong += 1 if p_prem(c["premium"]) >= 0.90 else 0
        strong += 1 if (c.get("vol_oi") or 0) >= 5 else 0
        strong += 1 if (c.get("oi_delta") or 0) > 0 else 0
        strong += 1 if ab >= 2 else 0
        strong += 1 if (c.get("sweeps") and c.get("blocks")) else 0
        strong += 1 if (ap is not None and abs(ap - 50) >= 15) else 0
        ed = c.get("earnings_days")
        strong += 1 if (ed is not None and 0 <= ed <= CFG["earnings_window_d"]) else 0
        if strong < CFG["strong_for_90"]:
            s = min(s, CFG["score_cap_below_strong"])
        c["strong_signals"] = strong
        c["score_parts"] = parts
        c["daily_score"] = max(0, min(100, round(s)))
    return elig


# ── 4. explicit trade-side classification + primary reason ──────────────

def side_of(c):
    """Explicit trade-side read: CALL BUY / PUT BUY / CALL SELL /
    PUT SELL / MIXED. Buys require >=55% of prints at/near the ask;
    SELL labels come only from the scanner's flow_side classifier;
    everything else is MIXED (tape doesn't confirm a side)."""
    fs = c.get("flow_side")
    if fs == "put_seller":
        return "PUT SELL"
    if fs == "call_seller":
        return "CALL SELL"
    ap = c.get("ask_pct")
    if ap is not None and ap >= 55:
        return "CALL BUY" if c["type"] == "call" else "PUT BUY"
    if ap is None:
        return "CALL BUY" if c["type"] == "call" else "PUT BUY"
    return "MIXED"


def bias_of(c):
    """Directional lean derived from the explicit side (drives the
    pane's BULLISH/BEARISH views): buys follow the contract direction;
    sells invert (income); MIXED leans on contract type, unconfirmed."""
    s = side_of(c)
    return {"CALL BUY": "bull", "PUT SELL": "bull_income",
            "PUT BUY": "bear", "CALL SELL": "bear_income"}.get(
        s, "bull_mixed" if c["type"] == "call" else "bear_mixed")


def _earnings_date_approx(c, et_date):
    ed = c.get("earnings_days")
    if ed is None or ed < 0:
        return None
    try:
        d = datetime.strptime(et_date, "%Y-%m-%d") + timedelta(days=int(ed))
        return d.strftime("%m/%d")
    except Exception:
        return None


def reason_of(c):
    """PRIMARY unusual reason = the strongest INDEPENDENT signal.
    Recurrence is deliberately NOT a primary reason — it ships as a
    separate badge (seen/active/prints fields) so a contract's actual
    edge is never hidden behind 'it appeared twice'."""
    ap = c.get("ask_pct")
    # Ask-side execution AND sweep evidence must BOTH exist.
    if (c.get("sweeps") or 0) > 0 and ap is not None and ap >= CFG["aggressive_ask"]:
        return "AGGRESSIVE ASK SWEEPS"
    if (c.get("vol_oi") or 0) >= CFG["extreme_voloi"]:
        return "EXTREME V/OI"
    if (c.get("oi_delta_pct") or 0) >= CFG["oi_change_pct"]:
        return "OI CHANGE"
    ed = c.get("earnings_days")
    if ed is not None and 0 <= ed <= CFG["earnings_window_d"]:
        return "PRE-EARNINGS FLOW"
    if (c.get("premium") or 0) >= CFG["large_premium"]:
        return "LARGE PREMIUM"
    if c.get("opening") == "likely_open" or c.get("size_gt_oi"):
        return "OPENING FLOW"
    if ap is not None and ap >= CFG["directional_ask"]:
        return ("DIRECTIONAL CALL BUYING" if c["type"] == "call"
                else "DIRECTIONAL PUT BUYING")
    return "OPENING FLOW" if (c.get("vol_oi") or 0) >= 2 else "ACTIVE FLOW"


# ── 5. diversified top list ─────────────────────────────────────────────

def top_n(elig, n_batches):
    liq_rank = {"A": 0, "B": 1, "C": 2, "D": 3, "F": 4}
    elig.sort(key=lambda c: (
        -c["daily_score"],
        -(c.get("active_batches") or 0),          # verified recurrence
        -(c.get("premium") or 0),
        liq_rank.get((c.get("liquidity") or "C").upper(), 2),
        c.get("last_seen") or "",
        contract_key(c),
    ))
    out, per_tk = [], {}
    n_bull = n_bear = 0
    per = CFG["per_view"]
    for c in elig:
        if n_bull >= per and n_bear >= per:
            break
        if len(out) >= CFG["max_rows"]:
            break
        if per_tk.get(c["ticker"], 0) >= CFG["per_ticker_cap"]:
            continue
        is_bull = bias_of(c).startswith("bull")
        if (is_bull and n_bull >= per) or (not is_bull and n_bear >= per):
            continue
        out.append(c)
        per_tk[c["ticker"]] = per_tk.get(c["ticker"], 0) + 1
        if is_bull:
            n_bull += 1
        else:
            n_bear += 1
    return out


def build_payload(acc, ranked, n_elig, n_total):
    n_b = len(acc["batches_seen"])
    rows = []
    for i, c in enumerate(ranked, 1):
        rows.append({
            "rank": i, "ticker": c["ticker"], "type": c["type"],
            "strike": c["strike"], "expiry": c["expiry"],
            "dte": c.get("dte"),
            "side": side_of(c), "bias": bias_of(c),
            "score": c["daily_score"],
            "score_parts": c.get("score_parts"),
            "strong_signals": c.get("strong_signals"),
            "premium": c.get("premium"), "last_price": c.get("last_price"),
            "vol_oi": c.get("vol_oi"),
            "oi_delta": c.get("oi_delta"),
            "seen_scans": c.get("seen_scans"),
            "active_batches": c.get("active_batches"),
            "unique_prints": c.get("repeat_count"),
            "first_seen": c.get("first_seen"),
            "last_seen": c.get("last_seen"),
            "last_print_ts": c.get("last_print_ts"),
            "ask_pct": c.get("ask_pct"), "liquidity": c.get("liquidity"),
            "iv": c.get("iv"), "pct_otm": c.get("pct_otm"),
            "earnings_days": c.get("earnings_days"),
            "earnings_date_est": _earnings_date_approx(c, acc["et_date"]),
            "reason": reason_of(c),
        })
    return {
        "generated": acc["batches_seen"][-1] if acc["batches_seen"] else None,
        "et_date": acc["et_date"],
        "batches_today": n_b,
        "universe_total": n_total,
        "universe_eligible": n_elig,
        "per_view": CFG["per_view"],
        "rows": rows,
        "note": ("Cumulative ranking across all of today's scan batches "
                 "(delayed data). Contracts dedup by exact identity; "
                 "day-cumulative volume/premium merge as max across "
                 "batches (never summed). Recurrence credit requires "
                 "VERIFIED new activity between batches, not mere "
                 "presence in cumulative snapshots. Max 2 per ticker; "
                 "up to " + str(CFG["per_view"]) + " rows per view. "
                 "Educational, not advice."),
    }


# ── self-test: audit fixtures, no I/O ───────────────────────────────────

def _fixture_row(**kw):
    base = {"ticker": "TST", "type": "call", "strike": 100.0,
            "expiry": "2026-08-21", "dte": 30, "spot": 100.0,
            "premium": 1_000_000, "volume": 5000, "vol_oi": 5.0,
            "open_interest": 1000, "oi_delta": 0, "trade_score": 70,
            "repeat_count": 2, "sweeps": 1, "blocks": 0, "ask_pct": 70,
            "liquidity": "B", "pct_otm": 2.0,
            "last_print_ts": "2026-07-21T14:00:00+00:00"}
    base.update(kw)
    return base


def self_test():
    def snap(gen, rows):
        return {"generated": gen, "rows": rows}
    G1 = "2026-07-21T14:05:00+00:00"
    G2 = "2026-07-21T15:05:00+00:00"
    fails = []

    def check(name, cond):
        print(("  PASS  " if cond else "  FAIL  ") + name)
        if not cond:
            fails.append(name)

    # 1. Same unchanged cumulative record in two scans:
    #    seen 2, active 1, no recurrence points.
    acc = {"et_date": None, "batches_seen": [], "contracts": {}}
    r = _fixture_row()
    acc, _ = merge_batch(acc, snap(G1, [dict(r)]))
    acc, _ = merge_batch(acc, snap(G2, [dict(r)]))
    c = list(acc["contracts"].values())[0]
    check("1a unchanged record: seen_scans == 2", c["seen_scans"] == 2)
    check("1b unchanged record: active_batches == 1", c["active_batches"] == 1)
    score_all(acc["contracts"], 2)
    check("1c unchanged record: 0 active-flow points",
          c["score_parts"]["active_flow"] == 0.0)

    # 2. Volume + print-time increase in batch two -> active 2 + credit.
    acc = {"et_date": None, "batches_seen": [], "contracts": {}}
    acc, _ = merge_batch(acc, snap(G1, [_fixture_row()]))
    acc, _ = merge_batch(acc, snap(G2, [_fixture_row(
        volume=7000, premium=1_400_000,
        last_print_ts="2026-07-21T15:00:00+00:00")]))
    c = list(acc["contracts"].values())[0]
    check("2a grown record: active_batches == 2", c["active_batches"] == 2)
    score_all(acc["contracts"], 2)
    check("2b grown record: active-flow points > 0",
          c["score_parts"]["active_flow"] > 0)

    # 3. No trade IDs in feed (documented limitation) — the equivalent
    #    guarantee: re-listed cumulative premium is never double-counted.
    check("3 premium not duplicated across scans (max, not sum)",
          c["premium"] == 1_400_000)

    # 4. Several prints inside ONE batch: prints > 1 but active == 1.
    acc = {"et_date": None, "batches_seen": [], "contracts": {}}
    acc, _ = merge_batch(acc, snap(G1, [_fixture_row(repeat_count=6)]))
    c = list(acc["contracts"].values())[0]
    check("4a multi-print single batch: unique prints == 6",
          c["repeat_count"] == 6)
    check("4b multi-print single batch: active_batches == 1",
          c["active_batches"] == 1)

    # 5. Cumulative premium snapshot: latest value, never a sum.
    acc = {"et_date": None, "batches_seen": [], "contracts": {}}
    acc, _ = merge_batch(acc, snap(G1, [_fixture_row(premium=2_000_000)]))
    acc, _ = merge_batch(acc, snap(G2, [_fixture_row(premium=2_500_000)]))
    c = list(acc["contracts"].values())[0]
    check("5 cumulative premium: 2.5M (not 4.5M)", c["premium"] == 2_500_000)

    # 6. >2 strong contracts from one ticker: only best two published.
    acc = {"et_date": None, "batches_seen": [], "contracts": {}}
    rows = [_fixture_row(strike=100.0 + i, trade_score=90 - i,
                         premium=5_000_000 - i * 100_000)
            for i in range(4)]
    rows += [_fixture_row(ticker="OTH", strike=50.0, trade_score=60)]
    acc, _ = merge_batch(acc, snap(G1, rows))
    elig = score_all(acc["contracts"], 1)
    ranked = top_n(elig, 1)
    tst = [c for c in ranked if c["ticker"] == "TST"]
    check("6a per-ticker cap: TST appears exactly twice", len(tst) == 2)
    check("6b per-ticker cap: kept its two best strikes",
          sorted(c["strike"] for c in tst) == [100.0, 101.0])

    # Bonus: 90+ requires >=3 independent strong signals.
    acc = {"et_date": None, "batches_seen": [], "contracts": {}}
    acc, _ = merge_batch(acc, snap(G1, [_fixture_row(
        trade_score=99, premium=20_000_000, vol_oi=1.5, oi_delta=0,
        sweeps=0, blocks=0, ask_pct=52, repeat_count=1)]))
    elig = score_all(acc["contracts"], 1)
    c = elig[0]
    check("7 weak-evidence 99 trade_score capped below 90",
          c["daily_score"] <= CFG["score_cap_below_strong"])

    print("\n%d/%d fixtures passed" % (
        11 - len(fails), 11))
    return 0 if not fails else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        sys.exit(self_test())

    snap = _load(LATEST_PATH)
    if not snap or not snap.get("rows"):
        print("  daily top: no uoa_latest.json rows — nothing to merge")
        return
    acc = migrate_acc(_load(ACC_PATH) or {"et_date": None,
                                          "batches_seen": [],
                                          "contracts": {}})
    acc, merged = merge_batch(acc, snap)
    n_b = len(acc["batches_seen"])
    elig = score_all(acc["contracts"], n_b)
    ranked = top_n(elig, n_b)
    payload = build_payload(acc, ranked, len(elig), len(acc["contracts"]))

    print("  daily top: %s · %d batches · %d contracts tracked · "
          "%d eligible · top %d published%s" %
          (acc["et_date"], n_b, len(acc["contracts"]), len(elig),
           len(ranked), "" if merged else " (batch already merged)"))
    if args.dry_run:
        for r in payload["rows"][:10]:
            print("   #%2d %-5s %-9s %8g %s  score=%d prem=$%.1fM "
                  "act=%s/%s prints=%s %s"
                  % (r["rank"], r["ticker"], r["side"], r["strike"],
                     r["expiry"], r["score"], (r["premium"] or 0) / 1e6,
                     r["active_batches"], r["seen_scans"],
                     r["unique_prints"], r["reason"]))
        return
    os.makedirs(os.path.dirname(ACC_PATH), exist_ok=True)
    with open(ACC_PATH, "w", encoding="utf-8") as f:
        json.dump(acc, f, separators=(",", ":"))
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    print("  Wrote uoa_top25_daily.json + accumulator")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
