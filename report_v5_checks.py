#!/usr/bin/env python3
"""report_v5_checks.py — pure semantic-check functions (v5.6 §15).

Each function takes rendered artifacts (page texts, claim objects,
model records) and returns the list of violations — empty means PASS.
Keeping them pure lets the mutation harness (§16) prove each check
detects its intended defect without regenerating a PDF: the harness
mutates the input, calls the same function the validator calls, and
records the observed failure.
"""

import re

# ── presentation (§13) ───────────────────────────────────────────────

ORPHAN_MIN_CHARS = 100


def orphan_pages(page_texts):
    """Pages whose extracted text is too thin to justify the page.
    The final chart-bearing page of a NEW_LISTING report still carries
    a heading + level table, so a genuine orphan is text-starved."""
    bad = []
    for i, t in enumerate(page_texts):
        txt = (t or "").strip()
        if len(txt) < ORPHAN_MIN_CHARS:
            bad.append("page %d: %d chars" % (i + 1, len(txt)))
    return bad


def orphan_bullets(page_texts):
    """A page whose entire content is a single stranded bullet."""
    bad = []
    for i, t in enumerate(page_texts):
        lines = [ln.strip() for ln in (t or "").splitlines()
                 if ln.strip()]
        # ignore running header/footer lines (short, no bullet)
        body = [ln for ln in lines if len(ln) > 3]
        bullets = [ln for ln in body if ln.startswith(("•", "-", "*"))
                   or ln.startswith("•")]
        if bullets and len(bullets) == 1 and len(body) <= 3 \
                and sum(len(ln) for ln in body) < 220:
            bad.append("page %d: single stranded bullet" % (i + 1))
    return bad


_RAW_ID_RX = re.compile(r"\b[a-z][a-z0-9]+(?:_[a-z0-9]+)+\b")
# tokens that legitimately appear (schema names in provenance notes are
# moved to the appendix; the core must carry none)
_RAW_ID_ALLOW = {"v5_inputs"}


_LABEL_PREFIXES = (("next_q_", "Next-quarter "),
                   ("next_fy_", "Next fiscal-year "),
                   ("fy_", "Full-year "), ("q_", "Quarterly "))


def human_metric_label(key):
    """'next_q_subscription_revenue' -> 'Next-quarter subscription
    revenue' — the reader-facing form of a schema key (§13)."""
    s = str(key or "")
    for pre, rep in _LABEL_PREFIXES:
        if s.startswith(pre):
            s = rep + s[len(pre):]
            break
    s = s.replace("_", " ").strip()
    return s[:1].upper() + s[1:] if s else s


def raw_identifiers(text):
    """Raw snake_case schema identifiers in reader-facing core text."""
    return sorted({t for t in _RAW_ID_RX.findall(text or "")
                   if t not in _RAW_ID_ALLOW})


# ── appendix binding (§1) ────────────────────────────────────────────

def _norm(s):
    return re.sub(r"[–—-]", "-", s or "")


def appendix_version_issues(apx_text):
    t = _norm(apx_text)
    bad = []
    if "Equity Research v5 - Appendix" not in t:
        bad.append("v5 appendix header missing")
    if re.search(r"Equity Research v4", t):
        bad.append("v4 metadata present in appendix")
    return bad


def appendix_report_id_issues(apx_text, report_id):
    if report_id and report_id in (apx_text or ""):
        return []
    return ["report ID %r not found in appendix" % report_id]


def appendix_hash_issues(apx_text, core_hash):
    if core_hash and core_hash in (apx_text or ""):
        return []
    return ["core PDF sha256 not recorded in appendix"]


def ledger_hash_issues(apx_text, ledger_hash):
    if ledger_hash and ledger_hash in (apx_text or ""):
        return []
    return ["source-ledger hash not recorded in appendix"]


def appendix_method_issues(apx_text, band_available, metric_kind=None):
    """The appendix methodology must match what the core actually did:
    never deny a band the core renders; affirm the same metric kind."""
    t = (apx_text or "").lower()
    bad = []
    denial = re.search(r"band[^.]{0,80}(not produced|deliberately not|"
                       r"were not produced)", t)
    if band_available:
        if denial:
            bad.append("appendix denies a band the core renders")
        if "historical multiple band" not in t \
                and "multiple band" not in t:
            bad.append("appendix never describes the band methodology")
        if metric_kind and metric_kind.lower() not in t:
            bad.append("metric kind %r absent from appendix"
                       % metric_kind)
    else:
        if "core renders a historical multiple band" in t:
            bad.append("appendix affirms a band the core withheld")
    return bad


# ── claims / evidence (§8) ───────────────────────────────────────────

def variant_wording_issues(core_text, variant_available):
    """'Our variant' is banned outright; 'Variant perception:' as an
    affirmative claim requires a sourced expectations gap."""
    t = core_text or ""
    bad = []
    if "Our variant" in t:
        bad.append("'Our variant' rendered")
    if not variant_available:
        for m in re.finditer(r"Variant perception[:\s]", t):
            ctx = t[m.start():m.start() + 160].lower()
            if not any(k in ctx for k in ("not established", "no sourced",
                                          "unavailable", "withheld",
                                          "not claimed", "no variant")):
                bad.append("affirmative variant wording without a "
                           "sourced expectations gap")
                break
    return bad


def historical_expected_return_issues(core_text, mode, weighted):
    """In historical mode nothing may present the range as a return."""
    if mode != "historical_range":
        return []
    bad = []
    if weighted:
        bad.append("probability-weighted value on a historical range")
    if "expected return" in (core_text or "").lower():
        bad.append("'expected return' rendered in historical mode")
    return bad


def event_state_issues(event, today=None):
    """§10: a report generated inside the pre-event window must not
    carry a post-call state."""
    from datetime import date, datetime, timedelta
    ev = event or {}
    state = ev.get("state") or ""
    nxt = ev.get("next_event_date") or (ev.get("catalyst") or {}).get(
        "next_event_date")
    if not nxt or not state.startswith("POST-CALL"):
        return []
    try:
        d = datetime.strptime(str(nxt)[:10], "%Y-%m-%d").date()
    except ValueError:
        return []
    today = today or date.today()
    if timedelta(0) <= (d - today) <= timedelta(days=3):
        return ["state %s with next event %s inside the pre-event "
                "window" % (state, nxt)]
    return []


def stage_order_issues(stages):
    """§12: monitoring stages must progress monotonically by level.
    Upward stages (reclaim/close above) and downward stages (close
    below) are separate ladders — an invalidation line beneath the
    price is not part of the reclaim progression."""
    ups, downs = [], []
    for s in stages or []:
        cond = str(s.get("condition") or "").lower()
        m = re.search(r"\$([\d,]+\.?\d*)", cond)
        if not m:
            continue
        lvl = float(m.group(1).replace(",", ""))
        (downs if "below" in cond else ups).append(lvl)
    bad = []
    if ups != sorted(ups):
        bad.append("upward stage thresholds not ascending: %s" % ups)
    if downs != sorted(downs, reverse=True):
        bad.append("downward stage thresholds not descending: %s"
                   % downs)
    return bad


OVERFLOW_MAX_CHARS = 5500


def overflow_pages(page_texts, limit=OVERFLOW_MAX_CHARS):
    return ["page %d: %d chars" % (i + 1, len(t or ""))
            for i, t in enumerate(page_texts) if len(t or "") > limit]


BANNED_HISTORICAL = ("bear case", "base case", "bull case",
                     "bear-case", "base-case", "bull-case",
                     "bear scenario", "base scenario", "bull scenario",
                     "scenario table", "scenario price",
                     "expected return", "target price",
                     "margin of safety", "upside/downside")


def scenario_language_issues(core_text, mode):
    """§2: historical mode renders no scenario/forecast vocabulary."""
    if mode != "historical_range":
        return []
    low = (core_text or "").lower()
    return [w for w in BANNED_HISTORICAL if w in low]


def window_label_issues(core_text, actual_years):
    """§2: the rendered history label equals actual_years."""
    yrs = set(re.findall(r"available (\d+\.\d)-year", core_text or ""))
    if not yrs:
        return []
    if actual_years is None:
        return ["label %s but no actual_years recorded" % sorted(yrs)]
    if yrs != {"%.1f" % actual_years}:
        return ["labels %s vs actual %.1f" % (sorted(yrs), actual_years)]
    return []


def ia_underwriting_issues(ia_level, mode):
    """§3: graded attractiveness requires underwritten scenarios."""
    if ia_level in ("PROVISIONAL", "NOT_UNDERWRITTEN", None):
        return []
    if mode == "underwritten":
        return []
    return ["graded level %s without underwritten scenarios (mode %s)"
            % (ia_level, mode)]


def confidence_axes_issues(conf):
    """§11: confidence must decompose into the five named axes."""
    axes = (conf or {}).get("axes") or {}
    want = {"source_integrity", "quantitative_coverage",
            "qualitative_coverage", "expectations_coverage",
            "thesis_completeness"}
    missing = sorted(want - set(axes))
    return (["missing axes: %s" % ", ".join(missing)] if missing
            else [])


VALID_CHECKPOINT_TYPES = ("exact_date", "estimated_date",
                          "unscheduled_event")


def checkpoint_type_issues(claims):
    """§10: checkpoints are typed objects; dated types carry a date."""
    bad = []
    for c in claims or []:
        cp = c.get("next_checkpoint")
        if not isinstance(cp, dict) \
                or cp.get("type") not in VALID_CHECKPOINT_TYPES \
                or (cp.get("type") != "unscheduled_event"
                    and not cp.get("date")):
            bad.append(c.get("claim_id") or "?")
    return bad


SUNDHEIM_REQUIRED_FIELDS = (
    "underwriting_status", "thesis_type", "business_quality",
    "investment_attractiveness", "principal_uncertainty",
    "next_evidence_needed", "reunderwrite_when", "questions",
)
SUNDHEIM_QUESTION_COUNT = 12


def sundheim_issues(sd):
    """§5: the Sundheim decision object must be complete and
    serialized — all stored fields present and all twelve questions
    answered (an honest 'not established' is an answer; a missing
    question is not)."""
    if not isinstance(sd, dict):
        return ["no Sundheim decision object"]
    bad = [k for k in SUNDHEIM_REQUIRED_FIELDS if k not in sd]
    qs = sd.get("questions") or []
    if len(qs) != SUNDHEIM_QUESTION_COUNT:
        bad.append("%d of %d questions present"
                   % (len(qs), SUNDHEIM_QUESTION_COUNT))
    for q in qs:
        if not isinstance(q, dict) or not q.get("question") \
                or not q.get("answer"):
            bad.append("unanswered question object")
            break
    return bad


def framework_issues(framework):
    """§4: all 26 dimensions present with valid statuses."""
    import report_v5_framework as FW
    dims = (framework or {}).get("dimensions") or {}
    return [k for k in FW.TIGER_DIMENSIONS
            if not isinstance(dims.get(k), dict)
            or dims[k].get("status") not in FW.STATUSES]


def full_coverage_issues(archetype, framework):
    """§6: FULL only with coverage on the decision dimensions."""
    if archetype != "FULL":
        return []
    return list(((framework or {}).get("summary") or {})
                .get("missing_for_full") or [])


def adapter_issues(adapter, core_text, archetype):
    """§7: an adapter was selected and its dashboard rendered."""
    ad = adapter or {}
    if not ad.get("key"):
        return ["no adapter selected"]
    if archetype == "NEW_LISTING":
        return []
    if (ad.get("label") or "") not in (core_text or ""):
        return ["adapter %r dashboard not rendered" % ad.get("key")]
    return []


def invalidation_separation_issues(core_text):
    # whitespace-tolerant: PDF extraction can wrap a phrase across
    # lines mid-way ("Tactical\ninvalidation:")
    t = re.sub(r"\s+", " ", core_text or "")
    bad = []
    if "Fundamental invalidation:" not in t:
        bad.append("no fundamental-invalidation line")
    if "Tactical invalidation:" not in t:
        bad.append("no tactical-invalidation line")
    return bad
