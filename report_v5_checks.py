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
                     "expected return", "target price",
                     "margin of safety", "upside/downside",
                     "probability-weighted", "expected value")

_SCENARIO_RX = re.compile(r"\bscenario\w*\b", re.I)


def scenario_language_issues(core_text, mode):
    """§4 (v5.7): historical mode carries NO scenario vocabulary on any
    surface — the whole word family (\\bscenario\\w*\\b), plus the
    forecast-language phrase list."""
    if mode != "historical_range":
        return []
    low = (core_text or "").lower()
    hits = [w for w in BANNED_HISTORICAL if w in low]
    m = _SCENARIO_RX.search(core_text or "")
    if m:
        hits.append("word %r" % m.group(0))
    return hits


def json_scenario_issues(result, mode):
    """§4: the validation JSON itself is a surface. Serialized minus
    the exempt fields (the language checks' own ids/messages and the
    mutation catalogue, which by definition describe the defects) it
    must carry no scenario token in historical mode."""
    if mode != "historical_range":
        return []
    import copy
    import json as _json
    doc = copy.deepcopy({k: v for k, v in (result or {}).items()
                         if k != "mutation_tests"})
    doc["checks"] = [c for c in doc.get("checks") or []
                     if "SCENARIO" not in (c.get("check_id") or "")]
    doc["blocking_failures"] = [b for b in doc.get("blocking_failures")
                                or [] if "SCENARIO" not in str(b)]
    blob = _json.dumps(doc, default=str)
    m = _SCENARIO_RX.search(blob)
    return (["validation JSON carries %r" % m.group(0)] if m else [])


# ── §1 point-in-time integrity ───────────────────────────────────────

def ttm_integrity_issues(multiples):
    """A band that is AVAILABLE must rest on four contiguous, current
    quarters of one named concept — per stream."""
    bad = []
    integ = (multiples or {}).get("ttm_integrity") or {}
    for kind in ("pe", "ps"):
        if not ((multiples or {}).get(kind) or {}).get("available"):
            continue
        rec = integ.get(kind) or {}
        if not rec.get("ok"):
            bad.append("%s: %s" % (kind,
                                   "; ".join(rec.get("reasons")
                                             or ["no integrity record"])))
        if not rec.get("concept"):
            bad.append("%s: no accounting concept recorded" % kind)
    return bad


def balance_sheet_period_issues(fu, bq):
    """§1: instants from different reporting dates must never be netted
    — when they differ, the quality record must have declined the
    pairing."""
    def _p(f):
        return str(f.get("period_end"))[:10] \
            if isinstance(f, dict) and f.get("period_end") else None
    cp, dp = _p((fu or {}).get("cash")), _p((fu or {}).get("debt"))
    if not cp or not dp or cp == dp:
        return []
    from datetime import date
    try:
        gap = abs((date.fromisoformat(cp) - date.fromisoformat(dp)).days)
    except ValueError:
        gap = 9999
    if gap <= 100:
        return []
    if "net_cash" in ((bq or {}).get("metrics_used") or []):
        return ["cash (%s) netted against debt (%s) — %d days apart"
                % (cp, dp, gap)]
    return []


def latest_label_issues(core_text, fu, max_age_days=200, today=None):
    """§1: 'latest' language requires a current fact."""
    t = re.sub(r"\s+", " ", core_text or "")
    if "latest filed quarter" not in t.lower():
        return []
    f = (fu or {}).get("revenue_q")
    pe = str(f.get("period_end"))[:10] \
        if isinstance(f, dict) and f.get("period_end") else None
    if not pe:
        return []
    from datetime import date
    today = today or date.today()
    try:
        age = (today - date.fromisoformat(pe)).days
    except ValueError:
        return []
    if age > max_age_days:
        return ["'latest filed quarter' rendered but the quarter ended "
                "%s (%d days ago)" % (pe, age)]
    return []


_XBRL_TAG_PERIOD_RX = re.compile(
    r"^XBRL-.*:(\w+)-(\d{4}-\d{2}-\d{2})$")


def claim_period_issues(claims, max_gap_days=100):
    """§1: the XBRL facts supporting one claim must share a reporting
    period — with one deliberate exception: two periods of the SAME
    concept are a time comparison (a y/y growth pair), which is the
    claim's whole point, not a mixing error. Only facts of DIFFERENT
    concepts must be contemporaneous."""
    from datetime import date
    bad = []
    for c in claims or []:
        if c.get("claim_type") not in ("fundamental",):
            continue
        newest_by_tag = {}
        for r in (c.get("evidence_refs") or []) \
                + (c.get("counterevidence_refs") or []):
            m = _XBRL_TAG_PERIOD_RX.match(str(r))
            if not m:
                continue
            try:
                d = date.fromisoformat(m.group(2))
            except ValueError:
                continue
            tag = m.group(1)
            if tag not in newest_by_tag or d > newest_by_tag[tag]:
                newest_by_tag[tag] = d
        periods = list(newest_by_tag.values())
        if len(periods) >= 2 \
                and (max(periods) - min(periods)).days > max_gap_days:
            bad.append("%s: evidence periods span %s to %s across "
                       "different concepts"
                       % (c.get("claim_id"), min(periods), max(periods)))
    return bad


def stale_support_issues(claims):
    """§1: no published claim may rest on stale critical evidence."""
    return [c.get("claim_id") for c in claims or []
            if (c.get("freshness") or {}).get("stale")]


# ── §2 sector compatibility ──────────────────────────────────────────

def claim_sector_issues(cl, adapter):
    pol = ((adapter or {}).get("policy") or {})
    forbidden = set(pol.get("claims_forbidden") or ())
    return [c["claim_id"] for c in (cl or {}).get("claims") or []
            if c.get("claim_id") in forbidden]


def valuation_sector_issues(sc, adapter):
    pol = ((adapter or {}).get("policy") or {})
    allowed = tuple(pol.get("valuation_allowed", ("pe", "ps")))
    if not (sc or {}).get("available"):
        return []
    mk = (sc or {}).get("metric_kind")
    if mk and mk not in allowed:
        return ["method %r used but the %s adapter permits %s"
                % (mk, (adapter or {}).get("key"), list(allowed))]
    return []


def quality_metric_issues(bq, adapter):
    pol = ((adapter or {}).get("policy") or {})
    banned = set(pol.get("quality_metrics_forbidden") or ())
    used = set((bq or {}).get("metrics_used") or [])
    return sorted(used & banned)


def freshness_basis_issues(bq, fu):
    """§1: the quality record's freshness must equal the OLDEST period
    among the facts it actually used — never the newest."""
    used = (bq or {}).get("metrics_used") or []
    if not used:
        return []
    keymap = {"revenue_growth": ("revenue_growth", "revenue_q"),
              "net_margin": ("net_margin",),
              "gross_margin": ("gross_margin",),
              "cash_conversion": ("free_cash_flow",
                                  "operating_cash_flow", "revenue_q"),
              "net_cash": ("cash", "debt")}
    periods = []
    for m in used:
        for k in keymap.get(m, ()):
            f = (fu or {}).get(k)
            if isinstance(f, dict) and f.get("period_end"):
                periods.append(str(f["period_end"])[:10])
    if not periods:
        return []
    oldest = min(periods)
    got = (bq or {}).get("freshness_basis")
    if got != oldest:
        return ["freshness_basis %r but the oldest material period "
                "is %s" % (got, oldest)]
    return []


def issuer_issues(ledger, expected_cik):
    """§1: every evidence record must belong to the intended issuer."""
    lc = (ledger or {}).get("issuer_cik")
    if not lc:
        return ["ledger carries no issuer identifier"]
    if expected_cik and str(lc) != str(expected_cik):
        return ["ledger issuer %s does not match the report's issuer %s"
                % (lc, expected_cik)]
    return []


# ── §5 rendered-layout checks (bbox/occupancy based) ─────────────────

def page_occupancy_issues(occupancy, final_min=0.45, body_min=0.30):
    """occupancy: list of per-page content-height ratios (0..1),
    measured from text AND graphical bounding boxes. §5 (v5.8): a final
    page below 45%% is a layout failure — the repagination chain must
    pull content down or move the binding block up."""
    bad = []
    for i, r in enumerate(occupancy or []):
        last = i == len(occupancy) - 1
        if last and len(occupancy) > 1 and r < final_min:
            bad.append("final page occupancy %.0f%% (min %.0f%%)"
                       % (r * 100, final_min * 100))
        elif not last and r < body_min:
            bad.append("page %d occupancy %.0f%% (min %.0f%%)"
                       % (i + 1, r * 100, body_min * 100))
    return bad


def measure_occupancy(pdf_path):
    """Per-page content-height ratio from text AND graphical (table
    rule / drawing) bounding boxes — top of first element to bottom of
    last, over page height, excluding the running header/footer
    margins. Accepts a path or raw PDF bytes (§5: the repagination
    chain measures candidates before a file is written)."""
    import fitz
    out = []
    doc = fitz.open(stream=pdf_path, filetype="pdf") \
        if isinstance(pdf_path, (bytes, bytearray)) else \
        fitz.open(pdf_path)
    for page in doc:
        h = page.rect.height
        spans = []
        for b in page.get_text("blocks"):
            if (b[4] or "").strip():
                spans.append((b[1], b[3]))
        try:
            for d in page.get_drawings():
                r = d.get("rect")
                if r is not None and r.height < h * 0.9:
                    spans.append((r.y0, r.y1))
        except Exception:
            pass
        try:
            for im in page.get_image_info():
                bb = im.get("bbox")
                if bb:
                    spans.append((bb[1], bb[3]))
        except Exception:
            pass
        body = [(t, b) for t, b in spans if 60 < t < h - 50]
        if not body:
            out.append(0.0)
            continue
        top = min(t for t, _ in body)
        bot = max(b for _, b in body)
        out.append(max(0.0, min(1.0, (bot - top) / (h - 110))))
    return out


def stranded_validation_summary_issues(page_texts, occupancy,
                                       final_min=0.45):
    """NO_STRANDED_VALIDATION_SUMMARY (§5, v5.8): the final page must
    not be an isolated validation/hash block — the binding table alone
    on a sparse tail page."""
    if not page_texts or len(page_texts) < 2:
        return []
    last = re.sub(r"\s+", " ", page_texts[-1] or "")
    occ_last = (occupancy or [1.0])[-1]
    has_binding = ("Binding record for this package" in last
                   or "sha256" in last.lower())
    headings = re.findall(r"\b(\d{1,2})\.\s+[A-Z]", last)
    only_binding = has_binding and len(set(headings)) <= 1
    if only_binding and occ_last < final_min:
        return ["final page is an isolated validation/hash block at "
                "%.0f%% occupancy (min %.0f%%)"
                % (occ_last * 100, final_min * 100)]
    return []


def sundheim_render_issues(apx_text, sd):
    """§5: every Sundheim answer renders in full — the first words of
    each stored answer must appear in the extracted appendix text
    (wrapped cells reflow but never clip)."""
    t = re.sub(r"\s+", " ", apx_text or "")
    bad = []
    for q in (sd or {}).get("questions") or []:
        probe = re.sub(r"\s+", " ", str(q.get("answer") or ""))[:60]
        if probe and probe not in t:
            bad.append("answer clipped or missing: %r" % probe[:40])
    return bad


def sundheim_header_issues(page_texts, sd=None):
    """§5: a page carrying Sundheim answer rows without the section
    start must repeat the table header. A page counts as carrying rows
    only when at least two DISTINCT substantial answers appear — short
    answers ('not established') recur in ordinary prose and must not
    trip the detector."""
    qs = (sd or {}).get("questions") or []
    answers = [re.sub(r"\s+", " ", str(q.get("answer") or ""))[:40]
               for q in qs if len(str(q.get("answer") or "")) >= 25]
    questions = [re.sub(r"\s+", " ", str(q.get("question") or ""))
                 for q in qs if q.get("question")]
    bad = []
    for i, t in enumerate(page_texts or []):
        norm = re.sub(r"\s+", " ", t or "")
        n_rows = sum(1 for a in set(answers) if a and a in norm)
        # answers alone are not proof of table rows — several answers
        # quote claim texts that legitimately recur in the claims
        # section. A continuation page shows the QUESTION column too.
        n_qs = sum(1 for q in set(questions) if q and q in norm)
        starts_here = "Sundheim decision record" in norm
        if n_rows >= 2 and n_qs >= 1 and not starts_here \
                and "Question" not in norm:
            bad.append("page %d carries Sundheim rows without a "
                       "repeated header" % (i + 1))
    return bad


def stranded_tail_issues(page_texts):
    """§5: no page may open mid-sentence (a stranded continuation).
    A table continuation whose header row repeats on the page is NOT
    stranded — the repeated header is exactly what §5 requires for a
    split table, and extraction order may surface a wrapped cell
    before it."""
    bad = []
    _TABLE_HEADERS = (("Dimension", "Conclusion"),
                      ("Question", "Answer"),
                      ("Metric", "Provenance"), ("Source", "Note"),
                      ("KPI", "Consensus"))
    for i, t in enumerate(page_texts or []):
        lines = [ln.strip() for ln in (t or "").splitlines()
                 if ln.strip()]
        body = [ln for ln in lines
                if not ln.startswith(("Prepared", "Educational"))
                and "Equity Research" not in ln
                and not ln.startswith("Page ")]
        if not (i and body and body[0][:1].islower()):
            continue
        norm = re.sub(r"\s+", " ", t or "")
        if any(all(h in norm for h in pair) for pair in _TABLE_HEADERS):
            continue          # table continuation with repeated header
        bad.append("page %d opens mid-sentence: %r"
                   % (i + 1, body[0][:40]))
    return bad


# ── §6 release provenance ────────────────────────────────────────────

def provenance_issues(result, head_sha=None, tree_sha=None, dirty=None):
    """§6: artifacts must identify the exact clean source state."""
    bad = []
    for k in ("generator_version", "source_commit_sha", "git_tree_sha",
              "generated_at", "report_id"):
        if not (result or {}).get(k):
            bad.append("missing %s" % k)
    if (result or {}).get("dirty_worktree") is not False:
        bad.append("dirty_worktree is not false")
    if head_sha and (result or {}).get("source_commit_sha") != head_sha:
        bad.append("source_commit_sha %r is not the generating commit "
                   "%r" % ((result or {}).get("source_commit_sha"),
                           head_sha))
    if tree_sha and (result or {}).get("git_tree_sha") != tree_sha:
        bad.append("git_tree_sha mismatch")
    return bad


def adapter_governance_issues(cl, sc, adapter):
    # a post-routing reclassification (pre-revenue -> new-listing) is
    # legitimate: analysis ran under the pre-routing policy, which the
    # adapter records
    keys = {(adapter or {}).get("key"),
            (adapter or {}).get("reclassified_from")}
    keys.discard(None)
    bad = []
    if (cl or {}).get("adapter_key") not in keys:
        bad.append("argument builder ran without the adapter policy "
                   "(%r vs %r)" % ((cl or {}).get("adapter_key"),
                                   sorted(keys)))
    vp = ((sc or {}).get("valuation_policy") or {})
    if vp.get("adapter") not in keys:
        bad.append("valuation selection ran without the adapter policy "
                   "(%r vs %r)" % (vp.get("adapter"), sorted(keys)))
    return bad


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


# ── v5.8 §1: classification integrity ────────────────────────────────

_PRE_REVENUE_ADMITTED_BASES = ("no revenue concept in any taxonomy",
                               "development-stage condition")
_DATA_GAP_MARKERS = ("not parsed", "no filed quarters", "fetch",
                     "unavailable", "missing")


def classification_present_issues(classification):
    c = classification or {}
    out = []
    for f in ("business_stage", "accounting_regime", "listing_type",
              "classification_confidence"):
        if not c.get(f):
            out.append("classification field %r absent" % f)
    if c.get("business_stage") not in ("OPERATING", "PRE_REVENUE",
                                       "UNKNOWN"):
        out.append("business_stage %r not a defined stage"
                   % c.get("business_stage"))
    return out


def missing_data_pre_revenue_issues(classification):
    """§1: PRE_REVENUE may never be a synonym for 'we could not parse
    revenue'. The basis must be one of the admitted-evidence rules and
    must not read like a data-availability statement."""
    c = classification or {}
    if c.get("business_stage") != "PRE_REVENUE":
        return []
    basis = (c.get("business_stage_basis") or "").lower()
    out = []
    if not any(m in basis for m in _PRE_REVENUE_ADMITTED_BASES):
        out.append("PRE_REVENUE basis is not an admitted-evidence "
                   "rule: %r" % basis[:100])
    if any(m in basis for m in _DATA_GAP_MARKERS):
        out.append("PRE_REVENUE basis cites a data gap (%r) — missing "
                   "data never implies pre-revenue" % basis[:100])
    return out


def pre_revenue_evidence_issues(classification, ledger):
    """§1: PRE_REVENUE requires resolvable admitted evidence."""
    c = classification or {}
    if c.get("business_stage") != "PRE_REVENUE":
        return []
    refs = c.get("classification_evidence_refs") or []
    if not refs:
        return ["PRE_REVENUE carries no classification evidence refs"]
    known = (ledger or {}).get("ids") or {}
    return ["classification ref %r unresolved in ledger" % r
            for r in refs if r not in known]


def classification_refs_issues(classification, ledger):
    known = (ledger or {}).get("ids") or {}
    return ["classification ref %r unresolved in ledger" % r
            for r in (classification or {}).get(
                "classification_evidence_refs") or []
            if r not in known]


def expected_routing_issues(expected, classification, adapter):
    """§1/§6: expected-vs-actual routing for registered fixtures.
    Returns [(field, expected, actual), ...]; empty when no expectation
    is registered or everything matches."""
    if not expected:
        return []
    out = []
    pairs = (("expected_adapter", (adapter or {}).get("key")),
             ("expected_business_stage",
              (classification or {}).get("business_stage")),
             ("expected_accounting_regime",
              (classification or {}).get("accounting_regime")))
    for field, actual in pairs:
        want = expected.get(field)
        if want and actual != want:
            out.append((field, want, actual))
    return out


# ── v5.8 §3: per-cell grid TTM validation ────────────────────────────

_GRID_TTM_METRICS = ("revenue", "net_income", "eps", "ocf")


def grid_ttm_cell_issues(grid):
    """EVERY_DISPLAYED_TTM_CELL_VALID: a populated top-level TTM value
    must be backed by a cell that passed its own four-quarter test."""
    ttm = (grid or {}).get("ttm") or {}
    cells = ttm.get("cells") or {}
    out = []
    for m in _GRID_TTM_METRICS:
        v = ttm.get(m)
        c = cells.get(m) or {}
        if v is not None and not c.get("ok"):
            out.append("TTM %s is populated (%.4g) but its cell failed "
                       "validation: %s"
                       % (m, v, "; ".join(c.get("reasons") or
                                          ["no cell record"])))
    # derived rows must come only from validated bases
    if ttm.get("net_margin") is not None and not (
            (cells.get("revenue") or {}).get("ok")
            and (cells.get("net_income") or {}).get("ok")):
        out.append("TTM net margin derived without validated revenue "
                   "and net-income cells")
    if ttm.get("fcf") is not None and not (
            (cells.get("ocf") or {}).get("ok")
            and (cells.get("capex") or {}).get("ok")):
        out.append("TTM FCF derived without validated OCF and capex "
                   "cells")
    return out


def grid_ttm_endpoint_issues(grid):
    """TTM_GRID_ENDPOINTS_ARE_CONSISTENT: all populated cells share the
    one displayed column date."""
    ttm = (grid or {}).get("ttm") or {}
    cells = ttm.get("cells") or {}
    thru = ttm.get("through")
    out = []
    populated = [m for m in _GRID_TTM_METRICS if ttm.get(m) is not None]
    if populated and not thru:
        out.append("TTM cells populated (%s) with no column "
                   "through-date" % ", ".join(populated))
    for m in populated:
        c = cells.get(m) or {}
        if c.get("through") and thru and c["through"] != thru:
            out.append("TTM %s ends %s but the column is dated %s"
                       % (m, c["through"], thru))
    if thru and not populated and ttm.get("eps") is None \
            and ttm.get("fcf") is None:
        out.append("column through-date %s shown with no populated "
                   "cell" % thru)
    return out


def grid_ttm_footnote_issues(grid, core_text):
    """TTM_FOOTNOTE_MATCHES_RENDERED_VALUES +
    SUPPRESSED_TTM_HAS_NO_POPULATED_CELL: the note and the cells tell
    one story — a metric named suppressed has no populated value, and
    populated values render in the PDF."""
    ttm = (grid or {}).get("ttm") or {}
    gaps = " ".join((grid or {}).get("gaps") or [])
    out = []
    for s in ttm.get("suppressed") or []:
        m = s.get("metric")
        label = s.get("label") or m
        if m in _GRID_TTM_METRICS and ttm.get(m) is not None:
            out.append("metric %s is listed suppressed but its TTM "
                       "value is populated" % m)
        if m and ("TTM suppressed" in gaps) \
                and m not in gaps and (label or "") not in gaps:
            out.append("suppression note does not name metric %s" % m)
    if (ttm.get("suppressed") and (grid or {}).get("years")
            and core_text and "TTM suppressed" not in core_text):
        out.append("cells are suppressed but the rendered core carries "
                   "no TTM-suppression note")
    return out


def grid_ttm_pdf_issues(grid, core_text, fmt_money=None):
    """PDF_TTM_VALUES_MATCH_VALIDATION_MODEL: every populated money TTM
    cell's formatted value appears in the extracted core text."""
    if not (grid or {}).get("years"):
        return []
    ttm = (grid or {}).get("ttm") or {}
    t = re.sub(r"\s+", " ", core_text or "")
    out = []

    def _fmt(v):                      # mirrors the renderer's $M cell
        return (fmt_money or (lambda x: "{:,.0f}".format(x / 1e6)))(v)

    for m in ("revenue", "net_income", "ocf"):
        v = ttm.get(m)
        if v is None:
            continue
        if _fmt(v) not in t:
            out.append("TTM %s (%s) not found in the rendered core"
                       % (m, _fmt(v)))
    return out


def bank_revenue_concept_issues(grid, adapter):
    """BANK_REVENUE_CONCEPT_IS_SECTOR_APPROPRIATE: a bank/insurer grid
    whose revenue stream populated must have used one of the sector's
    net-revenue concepts."""
    ad = adapter or {}
    if ad.get("key") != "bank_insurer":
        return []
    allowed = (ad.get("policy") or {}).get("revenue_concepts") or ()
    if not allowed:
        return []
    ttm = (grid or {}).get("ttm") or {}
    used = ttm.get("revenue_concept")
    if ttm.get("revenue") is not None and used not in allowed:
        return ["bank grid revenue uses concept %r — not in the "
                "sector-appropriate set %s" % (used, list(allowed))]
    return []


# ── v5.8 §2: same-period balance-sheet rule ──────────────────────────

BS_PAIR_MAX_GAP_DAYS = 100
BS_NOT_ASSESSED_MSG = ("Not assessed: comparable cash and debt facts "
                       "for the same reporting date were not admitted.")
# newest admitted instant a "latest"/current-position wording may cite
BS_LATEST_MAX_AGE_DAYS = 400

_REF_DATE_RX = re.compile(r"-(\d{4}-\d{2}-\d{2})$")


def _fact_period(fact):
    if isinstance(fact, dict) and fact.get("period_end"):
        return str(fact["period_end"])[:10]
    return None


def balance_sheet_pairing(fu):
    """The ONE pairing decision every surface consults: cash and debt
    may be presented together only when their reporting dates match
    within BS_PAIR_MAX_GAP_DAYS."""
    from datetime import date
    cash_f, debt_f = (fu or {}).get("cash"), (fu or {}).get("debt")

    def _v(f):
        return f.get("v") if isinstance(f, dict) else None

    cp, dp = _fact_period(cash_f), _fact_period(debt_f)
    gap = None
    if cp and dp:
        gap = abs((date.fromisoformat(cp) - date.fromisoformat(dp)
                   ).days)
    ok = not (
        _v(cash_f) is not None and _v(debt_f) is not None
        and cp and dp and gap is not None
        and gap > BS_PAIR_MAX_GAP_DAYS)
    return {"cash_value": _v(cash_f), "debt_value": _v(debt_f),
            "cash_period": cp, "debt_period": dp,
            "gap_days": gap, "ok": ok,
            "message": None if ok else BS_NOT_ASSESSED_MSG}


def _money(v):
    if v is None:
        return None
    a = abs(v)
    if a >= 1e9:
        return "$%.2fB" % (v / 1e9)
    if a >= 1e6:
        return "$%.1fM" % (v / 1e6)
    return "$%.2f" % v


def serialized_bs_issues(snap, view5, dashboard_rows=None):
    """SERIALIZED_BALANCE_SHEET_FACTS_SHARE_PERIOD: when the pairing is
    incompatible, NO serialized surface may join the two formatted
    values — framework conclusions, Sundheim answers, claims, quality
    reasons or dashboard rows."""
    fu = (snap or {}).get("fundamentals") or {}
    pair = balance_sheet_pairing(fu)
    if pair["ok"]:
        return []
    c_s, d_s = _money(pair["cash_value"]), _money(pair["debt_value"])
    if not (c_s and d_s):
        return []
    out = []

    def _scan(label, s):
        if isinstance(s, str) and c_s in s and d_s in s:
            out.append("%s joins cash %s and debt %s from different "
                       "reporting dates (%s vs %s)"
                       % (label, c_s, d_s, pair["cash_period"],
                          pair["debt_period"]))

    fw = (view5 or {}).get("framework") or {}
    for k, dim in (fw.get("dimensions") or {}).items():
        _scan("framework dimension %r" % k,
              (dim or {}).get("conclusion"))
    for q in ((view5 or {}).get("sundheim") or {}).get(
            "questions") or []:
        _scan("Sundheim answer %r" % (q.get("id")
                                      or str(q.get("question"))[:30]),
              q.get("answer"))
    cl = (view5 or {}).get("claims") or {}
    for c in _published_claims(cl)[0]:
        _scan("claim %r" % c.get("claim_id"), c.get("claim"))
    bq = ((view5 or {}).get("assessment") or {}).get(
        "business_quality") or {}
    for r in bq.get("reasons") or []:
        _scan("business-quality reason", r)
    for row in dashboard_rows or []:
        if isinstance(row, (list, tuple)) and len(row) > 1:
            _scan("dashboard row %r" % row[0], str(row[1]))
    return out


def bs_value_period_issues(dashboard_rows, framework):
    """EVERY_BALANCE_SHEET_VALUE_HAS_PERIOD: a displayed cash or debt
    value carries its own reporting date."""
    out = []
    for row in dashboard_rows or []:
        if not (isinstance(row, (list, tuple)) and len(row) >= 2):
            continue
        label, val = str(row[0]), str(row[1])
        if "/" in label and "cash" in label.lower() \
                and "$" in val and "as of" not in val \
                and "Not assessed" not in val:
            out.append("dashboard row %r shows balance-sheet values "
                       "without their reporting dates" % label)
    dim = ((framework or {}).get("dimensions") or {}).get(
        "balance_sheet") or {}
    concl = dim.get("conclusion") or ""
    if dim.get("status") == "PARTIAL" and "$" in concl \
            and "as of" not in concl:
        out.append("balance-sheet conclusion cites values without "
                   "their reporting dates: %r" % concl[:80])
    return out


def framework_bs_freshness_issues(framework, today=None):
    """FRAMEWORK_CONCLUSIONS_RESPECT_FRESHNESS +
    LATEST_LABEL_MATCHES_EACH_SUPPORTING_FACT (framework side): a dim
    graded above NOT_ASSESSED must not join instants from different
    periods, and any 'latest' wording requires every supporting fact to
    be current."""
    from datetime import date, timedelta
    today = today or date.today()
    out = []
    for k, dim in ((framework or {}).get("dimensions") or {}).items():
        if (dim or {}).get("status") == "NOT_ASSESSED":
            continue
        dates = []
        for r in dim.get("evidence_refs") or []:
            m = _REF_DATE_RX.search(str(r))
            if m:
                dates.append(m.group(1))
        if len(set(dates)) > 1:
            ds = sorted(set(dates))
            gap = (date.fromisoformat(ds[-1])
                   - date.fromisoformat(ds[0])).days
            joined = "$" in (dim.get("conclusion") or "") \
                and " vs " in (dim.get("conclusion") or "")
            if gap > BS_PAIR_MAX_GAP_DAYS and joined:
                out.append("dimension %r joins facts dated %s and %s "
                           "(%d days apart)" % (k, ds[0], ds[-1], gap))
        if "latest" in (dim.get("conclusion") or "").lower() and dates:
            oldest = min(dates)
            if date.fromisoformat(oldest) < today - timedelta(
                    days=BS_LATEST_MAX_AGE_DAYS):
                out.append("dimension %r says 'latest' but cites a "
                           "fact dated %s" % (k, oldest))
    return out


def stale_rendered_support_issues(framework, today=None):
    """STALE_EVIDENCE_CANNOT_SUPPORT_ANY_RENDERED_CONCLUSION: a graded
    dimension quoting dollar figures must rest on at least one current
    fact."""
    from datetime import date, timedelta
    today = today or date.today()
    out = []
    for k, dim in ((framework or {}).get("dimensions") or {}).items():
        if (dim or {}).get("status") == "NOT_ASSESSED":
            continue
        if "$" not in (dim.get("conclusion") or ""):
            continue
        dates = []
        for r in dim.get("evidence_refs") or []:
            m = _REF_DATE_RX.search(str(r))
            if m:
                dates.append(m.group(1))
        if dates and max(dates) < (today - timedelta(
                days=BS_LATEST_MAX_AGE_DAYS)).isoformat():
            out.append("dimension %r quotes figures whose newest "
                       "supporting fact is dated %s"
                       % (k, max(dates)))
    return out


def dashboard_bs_issues(snap, dashboard_rows):
    """DASHBOARD_CONCLUSIONS_RESPECT_FRESHNESS: an incompatible pairing
    renders the canonical not-assessed sentence, never the two values."""
    fu = (snap or {}).get("fundamentals") or {}
    pair = balance_sheet_pairing(fu)
    out = []
    for row in dashboard_rows or []:
        if not (isinstance(row, (list, tuple)) and len(row) >= 2):
            continue
        label, val = str(row[0]), str(row[1])
        if "cash" in label.lower() and "/" in label:
            if not pair["ok"] and "Not assessed" not in val:
                out.append("dashboard renders %r despite cash %s / "
                           "debt %s reporting dates"
                           % (val[:60], pair["cash_period"],
                              pair["debt_period"]))
    return out


# ── v5.8 §4: reproducible ledger checks ──────────────────────────────

_PCT_RX = re.compile(r"(-?\d+(?:\.\d+)?)%")
_DOLLAR_RX = re.compile(r"\$(-?\d+(?:\.\d+)?)([BM])\b")


def ledger_kind_issues(ledger):
    """LEDGER_MERGE_PRESERVES_RICHEST_RECORD (structural half): no
    registered record may end up with a null kind — a sparse first
    sighting can no longer block enrichment."""
    out = []
    for rid, rec in ((ledger or {}).get("ids") or {}).items():
        if not isinstance(rec, dict) or rec.get("kind") is None:
            out.append("record %r has kind null" % rid)
    return out[:20]



def _published_claims(claims_obj):
    """The published claims of a v5-claims/2 object, plus explicit
    schema issues. The v5.8 review found five validators iterating a
    key the builder never wrote ('published'), silently inspecting
    ZERO claims — so an unrecognized shape now returns loud issues,
    never a quiet empty list."""
    cl = claims_obj or {}
    pub = cl.get("claims")
    if isinstance(pub, list):
        return pub, []
    issues = []
    for k, v in cl.items():
        if k != "rejected" and isinstance(v, list) and v                 and isinstance(v[0], dict) and "claim" in v[0]:
            issues.append("claims object carries %d claim(s) under "
                          "unrecognized key %r — zero claims would "
                          "have been inspected" % (len(v), k))
    if not issues and cl:
        issues.append("claims object has no 'claims' list "
                      "(schema %r) — zero claims inspected"
                      % cl.get("schema"))
    return [], issues


def calc_record_issues(claims, ledger):
    """CALC_RECORD_HAS_FORMULA_AND_INPUTS: every CALC record cited by a
    published claim carries its formula and ordered inputs."""
    known = (ledger or {}).get("ids") or {}
    out = []
    seen = set()
    pub, out2 = _published_claims(claims)
    out.extend(out2)
    for c in pub:
        for r in (c.get("evidence_refs") or []) + (
                c.get("counterevidence_refs") or []):
            if not (isinstance(r, str) and r.startswith("CALC-")) \
                    or r in seen:
                continue
            seen.add(r)
            rec = known.get(r)
            if not isinstance(rec, dict):
                out.append("CALC ref %r unregistered" % r)
                continue
            missing = [f for f in ("kind", "metric", "calculation")
                       if not rec.get(f)]
            if not rec.get("input_evidence_ids"):
                missing.append("input_evidence_ids")
            if missing:
                out.append("CALC record %r missing %s"
                           % (r, ", ".join(missing)))
    return out


def xbrl_claim_ref_issues(claims, ledger):
    """XBRL_CLAIM_REF_IS_COMPLETE: every XBRL fact cited by a published
    claim is fully reproducible — accession, taxonomy, concept, value,
    units, period_end, filing URL, issuer CIK."""
    known = (ledger or {}).get("ids") or {}
    out = []
    seen = set()
    pub, out2 = _published_claims(claims)
    out.extend(out2)
    for c in pub:
        for r in (c.get("evidence_refs") or []) + (
                c.get("counterevidence_refs") or []):
            if not (isinstance(r, str) and r.startswith("XBRL-")) \
                    or r in seen:
                continue
            seen.add(r)
            rec = known.get(r)
            if not isinstance(rec, dict):
                out.append("XBRL ref %r unregistered" % r)
                continue
            missing = [f for f in ("accession", "taxonomy", "concept",
                                   "value", "units", "period_end",
                                   "url", "issuer_cik")
                       if rec.get(f) is None]
            if missing:
                out.append("XBRL record %r missing %s"
                           % (r, ", ".join(missing)))
    return out


def all_refs_resolve_issues(view5, classification, ledger):
    """ALL_EVIDENCE_REFS_RESOLVE: every reference on every serialized
    surface resolves in the ledger — claims (both directions),
    framework dimensions and the classification record."""
    known = (ledger or {}).get("ids") or {}
    out = []

    def _chk(label, refs):
        for r in refs or []:
            if not isinstance(r, str) or r not in known:
                out.append("%s ref %r unresolved" % (label, str(r)[:50]))

    cl = (view5 or {}).get("claims") or {}
    pub_cl, sch_iss = _published_claims(cl)
    out.extend(sch_iss)
    for c in pub_cl:
        _chk("claim %s" % c.get("claim_id"), c.get("evidence_refs"))
        _chk("claim %s counter" % c.get("claim_id"),
             c.get("counterevidence_refs"))
    for k, dim in (((view5 or {}).get("framework") or {}).get(
            "dimensions") or {}).items():
        _chk("framework %s" % k, (dim or {}).get("evidence_refs"))
    _chk("classification",
         (classification or {}).get("classification_evidence_refs"))
    return out[:25]


def claim_reproduction_issues(claims, ledger):
    """CLAIM_EVIDENCE_IS_REPRODUCIBLE +
    REPRODUCED_CLAIM_MATCHES_RENDERED_VALUE: a published claim quoting
    a figure must have at least one cited ledger record whose stored
    value reproduces that figure."""
    known = (ledger or {}).get("ids") or {}
    out = []
    pub, out2 = _published_claims(claims)
    out.extend(out2)
    for c in pub:
        text = c.get("claim") or ""
        refs = [r for r in (c.get("evidence_refs") or [])
                if isinstance(r, str)]
        recs = [known.get(r) for r in refs if isinstance(known.get(r),
                                                        dict)]
        if len(recs) != len(refs):
            out.append("claim %r cites unregistered evidence"
                       % c.get("claim_id"))
            continue
        vals = [rec.get("value") for rec in recs
                if isinstance(rec.get("value"), (int, float))]
        # percentages quoted in the claim reproduce from a cited value
        for m in _PCT_RX.finditer(text):
            q = float(m.group(1))
            if not any(abs(v - q) < 0.06 or (
                    v and abs(100.0 * v - q) < 0.06) for v in vals):
                out.append("claim %r quotes %s%% — no cited record "
                           "stores that value"
                           % (c.get("claim_id"), m.group(1)))
                break
        for m in _DOLLAR_RX.finditer(text):
            q = float(m.group(1)) * (1e9 if m.group(2) == "B" else 1e6)
            if not any(v and abs(v - q) / max(abs(q), 1) < 0.006
                       for v in vals):
                out.append("claim %r quotes $%s%s — no cited record "
                           "stores that value"
                           % (c.get("claim_id"), m.group(1),
                              m.group(2)))
                break
    return out


# ── v5.8 review fix: dashboard value currency + sourcing ─────────────

def dashboard_currency_issues(dashboard_rows, core_text=None,
                              today=None):
    """DASHBOARD_VALUES_ARE_CURRENT_AND_SOURCED: every displayed money
    or percentage value on the sector dashboard carries its own
    reporting date, that date is within the recency floor, and (when
    the rendered core text is supplied) the value string actually
    appears on the page — the visible dashboard and the validated model
    can no longer diverge."""
    from datetime import date, timedelta
    today = today or date.today()
    floor = (today - timedelta(days=BS_LATEST_MAX_AGE_DAYS)).isoformat()
    out = []
    for row in dashboard_rows or []:
        if not (isinstance(row, (list, tuple)) and len(row) >= 2):
            continue
        label, val = str(row[0]), str(row[1])
        if "$" not in val and "%" not in val:
            continue                       # absence/staleness rows
        m = re.search(r"\(as of (\d{4}-\d{2}-\d{2})\)", val)
        if not m:
            out.append("dashboard row %r shows %r without its "
                       "reporting date" % (label, val[:40]))
            continue
        if m.group(1) < floor:
            out.append("dashboard row %r dated %s — older than the "
                       "recency floor yet displayed as a value"
                       % (label, m.group(1)))
        if core_text is not None:
            probe = val.split(" (as of")[0]
            if probe and probe not in re.sub(r"\s+", " ", core_text):
                out.append("dashboard row %r value %r not found in the "
                           "rendered core" % (label, probe[:30]))
    return out
