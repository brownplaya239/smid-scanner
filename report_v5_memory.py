#!/usr/bin/env python3
"""report_v5_memory.py — ResearchState + ChangeSet (v5.5 phase F).

Every admitted v5 run appends a versioned ResearchState to
data/research_state/<TICKER>.jsonl: the ratings, assessments, claims,
scenarios, valuation range, catalysts, invalidations, evidence refs and
the EXACT artifact hashes of the report it describes. The next run
diffs against the latest prior state to produce a deterministic
ChangeSet, and the report renders "What changed since the prior
report" — or "Initial underwriting" when no admitted prior exists.

Never compare against a prior whose identity and hashes are unknown:
each state line carries its own integrity hash (sha256 of the line
minus the hash field), verified before any comparison. A tampered or
truncated line downgrades to "prior not verifiable — treated as
initial underwriting" rather than a silent wrong diff.
"""

import hashlib
import json
import os
from datetime import datetime, timezone

_BASE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(_BASE, "data", "research_state")
SCHEMA = "v5-research-state/1"


def _line_hash(rec):
    body = {k: v for k, v in rec.items() if k != "state_hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True,
                                     default=str).encode()).hexdigest()


def build_state(ticker, view5, result, prior_id=None):
    """The persisted snapshot of everything a future diff needs."""
    v4 = view5.get("v4") or {}
    sc = view5.get("scenarios") or {}
    cl = view5.get("claims") or {}
    asx = view5.get("assessment") or {}
    arts = result.get("artifacts") or {}
    as_of = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = {r["leg"]: r["price"] for r in sc.get("rows") or []}
    rec = {
        "schema": SCHEMA,
        "ticker": ticker.upper(),
        "report_id": "%s-%s" % (ticker.upper(), as_of),
        "report_version": "v5",
        "as_of": as_of,
        "prior_report_id": prior_id,
        "archetype": (view5.get("archetype") or {}).get("archetype"),
        "event_state": (v4.get("event") or {}).get("state"),
        "fundamental_rating": ((v4.get("ratings") or {}).get(
            "fundamental") or {}).get("band"),
        "tactical_rating": ((v4.get("ratings") or {}).get(
            "tactical") or {}).get("band"),
        "business_quality": (asx.get("business_quality")
                             or {}).get("level"),
        "investment_attractiveness": (asx.get(
            "investment_attractiveness") or {}).get("level"),
        "claims": [{"claim_id": c["claim_id"], "status": c["status"],
                    "direction": c["direction"],
                    "confidence": c["confidence"]}
                   for c in cl.get("claims") or []],
        "rejected_claims": [r["claim_id"]
                            for r in cl.get("rejected") or []],
        "scenario_prices": rows,
        "valuation_range": ([rows.get("bear"), rows.get("bull")]
                            if rows else None),
        "spot": sc.get("spot"),
        "weighted_value": (sc.get("weighted") or {}).get("price"),
        "catalysts": [c.get("next_checkpoint")
                      for c in (cl.get("claims") or [])[:1]
                      if c.get("next_checkpoint")],
        "invalidation_conditions": [c["breaks_if"]
                                    for c in cl.get("claims") or []],
        "confidence": None,
        "consensus_snapshot": _consensus(view5.get("estimates")),
        "guidance_snapshot": _guidance(view5),
        "core_pdf_hash": _hash_of(arts, ".pdf", "_appendix"),
        "appendix_pdf_hash": _hash_of(arts, "_appendix.pdf"),
        "source_ledger_hash": None,
    }
    rec["state_hash"] = _line_hash(rec)
    return rec


def _hash_of(arts, suffix, exclude=None):
    for name, meta in (arts or {}).items():
        if name.endswith(suffix) and (not exclude
                                      or exclude not in name):
            return (meta or {}).get("sha256")
    return None


def _consensus(est):
    rec = (est or {}).get("recommendation") or {}
    return {"band": rec.get("band"), "as_of": rec.get("as_of")} \
        if rec else None


def _guidance(view5):
    exp = view5.get("expectations") or {}
    out = []
    for k in exp.get("kpis") or []:
        if k.get("company_guidance"):
            out.append({"metric": k["metric"],
                        **k["company_guidance"]})
    return out or None


def load_prior(ticker):
    """Latest verified prior state, or (None, reason)."""
    path = os.path.join(STATE_DIR, "%s.jsonl" % ticker.upper())
    if not os.path.exists(path):
        return None, "no prior admitted report"
    last = None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                last = line
    if not last:
        return None, "state file empty"
    try:
        rec = json.loads(last)
    except Exception:
        return None, "prior state unreadable — treated as initial " \
                     "underwriting"
    if rec.get("state_hash") != _line_hash(rec):
        return None, "prior state failed integrity verification — " \
                     "treated as initial underwriting"
    if not rec.get("core_pdf_hash"):
        return None, "prior state carries no artifact hash — treated " \
                     "as initial underwriting"
    return rec, None


def append_state(rec):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, "%s.jsonl" % rec["ticker"])
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
    return path


def changeset(prior, cur):
    """Deterministic diff; every material change carries a reason."""
    if not prior:
        return {"initial_underwriting": True, "changes": []}
    ch = []

    def field(name, label, reason_tpl):
        a, b = prior.get(name), cur.get(name)
        if a != b:
            ch.append({"category": label, "from": a, "to": b,
                       "reason": reason_tpl,
                       "evidence_refs": ["STATE-%s" % prior["report_id"],
                                         "STATE-%s" % cur["report_id"]]})

    field("event_state", "event_state_change",
          "event gate re-resolved on newer filings/calendar")
    field("fundamental_rating", "rating_change",
          "consensus snapshot moved between runs")
    field("tactical_rating", "rating_change",
          "price vs moving-average structure changed")
    field("business_quality", "assessment_change",
          "filed quality dimensions re-scored on newer facts")
    field("investment_attractiveness", "assessment_change",
          "price-relative inputs (gap/asymmetry/confidence) moved")
    field("archetype", "archetype_change",
          "evidence capabilities changed")

    pc = {c["claim_id"]: c for c in prior.get("claims") or []}
    cc = {c["claim_id"]: c for c in cur.get("claims") or []}
    for cid in sorted(set(cc) - set(pc)):
        ch.append({"category": "claim_added", "to": cid,
                   "reason": "newly cleared the publication gate",
                   "evidence_refs": []})
    for cid in sorted(set(pc) - set(cc)):
        ch.append({"category": "claim_removed", "from": cid,
                   "reason": "no longer clears the publication gate "
                             "(rejected or unsupported this run)",
                   "evidence_refs": []})
    for cid in sorted(set(pc) & set(cc)):
        if pc[cid]["status"] != cc[cid]["status"]:
            ch.append({"category": "claim_status_change",
                       "from": "%s:%s" % (cid, pc[cid]["status"]),
                       "to": "%s:%s" % (cid, cc[cid]["status"]),
                       "reason": "gate/counterevidence re-evaluated on "
                                 "newer facts", "evidence_refs": []})

    a, b = prior.get("scenario_prices") or {}, \
        cur.get("scenario_prices") or {}
    for leg in ("bear", "base", "bull"):
        if a.get(leg) is not None and b.get(leg) is not None \
                and abs(a[leg] - b[leg]) > 0.005 * max(abs(a[leg]), 1):
            ch.append({"category": "scenario_change",
                       "from": "%s $%.2f" % (leg, a[leg]),
                       "to": "%s $%.2f" % (leg, b[leg]),
                       "reason": "band percentiles and/or trailing "
                                 "metric moved with newer sessions/"
                                 "filings", "evidence_refs": []})

    pg, cg = prior.get("consensus_snapshot"), cur.get(
        "consensus_snapshot")
    if pg != cg:
        ch.append({"category": "consensus_change", "from": pg, "to": cg,
                   "reason": "vendor consensus snapshot moved",
                   "evidence_refs": []})
    return {"initial_underwriting": False,
            "prior_report_id": prior["report_id"],
            "prior_as_of": prior["as_of"],
            "prior_core_pdf_hash": prior.get("core_pdf_hash"),
            "changes": ch}
