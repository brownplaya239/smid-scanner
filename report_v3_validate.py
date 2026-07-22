#!/usr/bin/env python3
"""report_v3_validate.py — the checks that can fail a v3.1 package.

v3 returned a list of "problems". That was enough to say something was
wrong and not enough to say what was expected, what was seen, how bad it
was, or which evidence settled it. v3.1 returns one structured object per
check and a manifest naming the exact bytes it read.

    {check_id, status, severity, expected, observed, threshold,
     evidence_refs, detail}

Two properties matter more than the individual rules.

First, the validator must be able to fail the artifacts actually
delivered. It hashes the PDF bytes and the serialised evidence payload it
was handed, records those hashes in the manifest, and `verify_delivered`
re-reads the files from disk afterwards. A file edited after validation
no longer matches its own manifest.

Second, the page and the evidence package are computed independently, so
comparing them is a real cross-check. `report_v3_evidence` formats every
calculated figure from the unrounded value; the renderer formats it from
the snapshot. PDF_MATCHES_EVIDENCE fails when those two disagree. Do not
"simplify" that by having one read the other.

Severity decides the verdict: any FATAL or ERROR failure means ok=false.
WARN records something a reader should know without blocking delivery.
"""

import hashlib
import io
import json
import os
import re

import report_v3_evidence as EV
import report_v3_model as M
import research_snapshot as rs

VALIDATOR_VERSION = "3.4"

FATAL, ERROR, WARN, INFO = "FATAL", "ERROR", "WARN", "INFO"
PASS, FAIL, WARN_S, SKIP = "PASS", "FAIL", "WARN", "SKIP"

MIN_BODY_PT = 9.0
MAX_CORE_PAGES = 4
OWNERSHIP_MAX_AGE_DAYS = 730          # a 13G from 3 years ago is history
CORE_NEWS_SHOWN = 3

INTENT_RX = re.compile(
    r"\b(institution(?:s|al)?\s+(?:are\s+)?(?:accumulat|buy|sell|distribut|"
    r"buil[dt]|unload|exit)\w*"
    r"|smart\s+money"
    r"|(?:institutional|quiet|stealth)\s+(?:accumulation|distribution)"
    r"|being\s+accumulated|under\s+accumulation"
    r"|whales?\s+(?:are\s+)?(?:buy|sell)\w*)", re.I)

GLYPH_RX = re.compile(
    "[\ufffd\u0000-\u0008\u000b\u000c\u000e-\u001f]"
    "|\u00e2\u20ac[\u2122\u201c\u201d]"
    "|\u00c3[\u00a9\u00a8\u00bc\u00b1]")

ENTITIES = ("&amp;", "&#39;", "&quot;", "&gt;", "&lt;", "&nbsp;", "&#x27;")

# Every check this validator knows how to run. The list is hashed into
# the manifest so a reader can tell whether the package was checked by
# the same rule set they are reading about.
CHECK_IDS = [
    "BASELINE_PUBLISHED", "LADDER_ORDER", "LADDER_VS_SPOT",
    "LADDER_RENDER_ORDER", "STRUCTURAL_BOUNDARY_LABELLED",
    "TIMESTAMP_ZONE", "TIMESTAMP_CONSISTENCY",
    "STALE_CATALYST", "STALE_CATALYST_NO_INFERENCE",
    "EXIT_VS_HORIZON", "EXIT_DOCUMENTED", "INVALIDATION_WITHOUT_ENTRY",
    "FILING_ACCESSIONS", "OWNERSHIP_COVERAGE", "OWNERSHIP_FILING_AGE",
    "MISSING_VS_NEGATIVE", "UNAVAILABLE_VS_NOT_INGESTED",
    "CALIBRATION_LANGUAGE", "POPULATION_ARITHMETIC",
    "COORDINATION_CONSISTENCY", "MARKET_CAP_BASIS",
    "CALC_OPERAND_COMPLETENESS", "REC_INPUT_TRACEABILITY",
    "PE_OPERANDS_SHOWN",
    "NEWS_COUNT_RECONCILE", "PDF_MATCHES_EVIDENCE",
    "PAGE_COUNT", "BLANK_PAGE", "NEARLY_BLANK_PAGE", "TYPE_SIZE",
    "HTML_ENTITIES", "GLYPH_INTEGRITY", "CONTENT_BOUNDS",
    "INSTITUTIONAL_INTENT", "RAW_SOCIAL_CONTAINMENT",
    "PDF_METADATA", "PDF_LANGUAGE", "PDF_BOOKMARKS", "PDF_LINKS",
    "PDF_TAGGED", "ARTIFACT_HASHES",
    # v3.2 numerical integrity
    "BAR_CARDINALITY", "BAR_RANGE_PRESENT", "BENCHMARK_OPERANDS",
    "PARTIAL_SESSION_NOT_A_CLOSE", "LEVEL_WORDING_BASIS",
    "GUIDANCE_PRECISION", "GUIDANCE_PDF_MATCH",
    "COVERAGE_CONSISTENCY", "DISPLAY_COUNT_SCOPE",
    "CALC_REPRODUCIBLE", "HASH_COVERAGE",
    "RECORD_HASH_RECOMPUTE", "BENCHMARK_DATE_ALIGNMENT",
    "PDF_SOURCE_COVERAGE_MATCH", "APPENDIX_COUNT_SCOPE",
    # v3.3.1 editorial precision
    "DISPLAY_COUNT_UNSCOPED", "SOCIAL_SAMPLE_DESCRIPTION",
    "CLOSE_EXTREMA_LABELLED",
    # v3.4 setup layer
    "METRIC_FORMULA_TRACEABLE", "METRIC_SOURCE_DATED",
    "MARKET_DATA_FRESHNESS", "CHART_WINDOW_CARDINALITY",
    "PARTIAL_BAR_EXCLUDED", "INTERPRETIVE_PHRASE",
    "SNAPSHOT_REFS_RESOLVE",
]

# Vocabulary the restored setup layer must not reach for. The first group
# asserts a cause the tape cannot show; the second dresses a guess as a
# measured quantity. Both were in the legacy report this layer draws
# from, and neither comes back without a validated rule behind it.
INTERPRETIVE_RX = re.compile(
    r"\b(dead[- ]cat\s+bounce"
    r"|(?:severe|major|serious|huge)\s+red\s+flag"
    r"|bull\s+trap|bear\s+trap"
    r"|(?:model|our)\s+confidence\s*[:=]?\s*\d"
    r"|confidence\s+score"
    r"|\d{1,3}\s*%\s+(?:probability|chance|odds)\s+of"
    r"|probability\s+of\s+(?:upside|downside|a\s+move)"
    r"|(?:base|bull|bear)[- ]case\s+probability"
    r"|price\s+target\s+of\s+\$"
    r"|(?:screaming|table[- ]pounding)\s+(?:buy|sell))", re.I)

# A completed session older than this is not "the tape" any more.
MAX_MARKET_DATA_AGE_DAYS = 5

# A basis naming one of these is a third-party figure rather than a
# filing or a bar we hold, so it has to say when it was struck.
VENDOR_WORDS = ("vendor", "profile", "provider", "estimate", "settled",
                "as of", "holdings")
DATE_RX = re.compile(r"(20\d{2}-\d{2}-\d{2}|as of \d)")
# A vendor that publishes no date can still be shown, provided the basis
# says so. Silence is what the check refuses, not the absence of a date.
UNDATED_RX = re.compile(r"no as-of date|undated", re.I)

# The one calculation that is supposed to read the still-forming session.
PARTIAL_OK = ("CALC-intraday_last",)


def _sha(b):
    if isinstance(b, str):
        b = b.encode("utf-8")
    return hashlib.sha256(b).hexdigest()


def validator_code_hash():
    try:
        with io.open(__file__, "rb") as fh:
            return _sha(fh.read())
    except Exception:                                # pragma: no cover
        return None


def schema_hash():
    return _sha(json.dumps({"validator": VALIDATOR_VERSION,
                            "checks": CHECK_IDS}, sort_keys=True))


def chk(check_id, ok, severity, expected, observed, threshold=None,
        refs=None, detail=None, warn_only=False):
    return {"check_id": check_id,
            "status": PASS if ok else (WARN_S if warn_only else FAIL),
            "severity": severity,
            "expected": expected,
            "observed": observed,
            "threshold": threshold,
            "evidence_refs": refs or [],
            "detail": detail}


def skipped(check_id, severity, why):
    return {"check_id": check_id, "status": SKIP, "severity": severity,
            "expected": None, "observed": None, "threshold": None,
            "evidence_refs": [], "detail": why}


# ── semantics ───────────────────────────────────────────────────────────

def check_model(view, snap=None, evidence=None):
    out, snap, ev = [], snap or {}, evidence or {}
    calcs = ev.get("calculations") or {}
    recs = ev.get("records") or {}

    # ── baseline provenance ─────────────────────────────────────────────
    ch = view.get("changed") or {}
    base = ch.get("baseline")
    if ch.get("first_report"):
        out.append(chk("BASELINE_PUBLISHED", True, ERROR,
                       "a published prior package, or an explicit refusal",
                       "no baseline: %s" % (ch.get("refusal") or "none on file"),
                       detail="No change measurement is claimed."))
    else:
        has = bool(base and base.get("core_pdf_sha256"))
        out.append(chk("BASELINE_PUBLISHED", has, ERROR,
                       "baseline carries the sha256 of a published core PDF",
                       "sha256 present" if has else
                       "baseline has no published artifact hash",
                       refs=[base.get("core_pdf_sha256")] if has else [],
                       detail="A preview or draft must never be the thing "
                              "'what changed' is measured from."))

    # ── the ladder ──────────────────────────────────────────────────────
    stages = view.get("recovery") or []
    vals = [s["value"] for s in stages]
    out.append(chk("LADDER_ORDER", vals == sorted(vals), ERROR,
                   "recovery stages ascending by price",
                   ", ".join("%s %.2f" % (s["label"], s["value"])
                             for s in stages) or "no stages",
                   detail="Sorted from the data, never from convention."))
    price = view.get("price")
    bad = [s for s in stages if price is not None and s["value"] <= price]
    out.append(chk("LADDER_VS_SPOT", not bad, ERROR,
                   "every upside stage sits above spot",
                   "%d stage(s) at or below spot %s"
                   % (len(bad), price) if bad else "all above spot",
                   threshold=price))

    ex = view.get("exit") or {}
    structural = ex.get("bound_by") == "documented low"
    out.append(chk("STRUCTURAL_BOUNDARY_LABELLED",
                   bool(ex.get("basis")) and bool(ex.get("label")), ERROR,
                   "the exit level is named and its basis stated",
                   "%s at %s, bound by %s"
                   % (ex.get("label"), ex.get("value"), ex.get("bound_by")),
                   detail="A level derived from a 60-session closing low is a "
                          "structural boundary, not an actionable swing "
                          "stop, and must not be presented as one."))

    # ── time ────────────────────────────────────────────────────────────
    out.append(chk("TIMESTAMP_ZONE", not view.get("quote_tz_warning"), ERROR,
                   "every timestamp carries a zone",
                   view.get("quote_tz_warning") or "all zoned"))
    q, r = view.get("quote_time_utc"), view.get("report_time_utc")
    out.append(chk("TIMESTAMP_CONSISTENCY", not (q and r and q > r), FATAL,
                   "market data is not stamped after the report",
                   "market %s vs report %s" % (q, r), threshold=r))

    # ── catalysts ───────────────────────────────────────────────────────
    cat = view.get("catalysts") or {}
    drv, last = cat.get("current_driver") or {}, cat.get("last_reported") or {}
    age = last.get("age_days")
    stale = age is not None and age > M.CURRENT_DRIVER_DAYS
    out.append(chk("STALE_CATALYST",
                   not (stale and drv.get("grade") == M.DERIVED), ERROR,
                   "a disclosure older than %d days is not the current driver"
                   % M.CURRENT_DRIVER_DAYS,
                   "age %s days, driver grade %s"
                   % (None if age is None else int(age), drv.get("grade")),
                   threshold=M.CURRENT_DRIVER_DAYS))
    # A stale catalyst may not be laundered into an INFERRED driver either.
    # "We think the old filing still explains it" is the same claim with a
    # softer grade attached.
    out.append(chk("STALE_CATALYST_NO_INFERENCE",
                   not (stale and drv.get("grade") == M.INFERRED
                        and drv.get("references_catalyst")), FATAL,
                   "a stale catalyst yields no inferred driver either",
                   "stale=%s, driver grade=%s, references catalyst=%s"
                   % (stale, drv.get("grade"),
                      bool(drv.get("references_catalyst"))),
                   detail="When nothing verifiable explains the tape, the "
                          "report must say so rather than infer a cause."))

    # ── the exit ────────────────────────────────────────────────────────
    am, fl = ex.get("atr_multiple"), ex.get("floor")
    if am is not None and fl:
        out.append(chk("EXIT_VS_HORIZON", am >= fl - 1e-6, ERROR,
                       "exit sits at least %.1f x ATR from spot" % fl,
                       "%.2f x ATR" % am, threshold=fl))
    else:
        out.append(skipped("EXIT_VS_HORIZON", ERROR,
                           "no ATR or no horizon floor for this snapshot"))
    out.append(chk("EXIT_DOCUMENTED",
                   ex.get("value") is None or bool(ex.get("basis")), ERROR,
                   "any exit level states its basis",
                   ex.get("basis") or "no basis given"))
    out.append(chk("INVALIDATION_WITHOUT_ENTRY",
                   not (ex.get("active_entry") is False
                        and ex.get("label") == "Invalidation"), ERROR,
                   "'Invalidation' only with a position on the book",
                   "%s, active entry=%s" % (ex.get("label"),
                                            ex.get("active_entry"))))

    # ── ownership ───────────────────────────────────────────────────────
    own = view.get("ownership") or {}
    rows = own.get("rows") or []
    no_accn = [r for r in rows if not r.get("accession")]
    out.append(chk("FILING_ACCESSIONS", not no_accn, ERROR,
                   "every counted filing carries an accession number",
                   "%d of %d without" % (len(no_accn), len(rows))
                   if rows else "no filings",
                   refs=[r.get("ref") for r in rows if r.get("ref")][:10]))
    n_filer = len([r for r in rows if r.get("filer")])
    n_stake = len([r for r in rows if r.get("stake") is not None])
    complete = bool(rows) and n_filer == len(rows) and n_stake == len(rows)
    out.append(chk("OWNERSHIP_COVERAGE",
                   complete or bool(own.get("interpretation")), ERROR,
                   "filer and stake parsed for every row, or an explicit "
                   "'interpretation unavailable' notice",
                   "filers %d/%d, stakes %d/%d, notice=%s"
                   % (n_filer, len(rows), n_stake, len(rows),
                      bool(own.get("interpretation"))),
                   detail="A count of filings with no filer and no stake is "
                          "not an ownership read."))
    stalest = own.get("oldest_age_days")
    out.append(chk("OWNERSHIP_FILING_AGE",
                   stalest is None or stalest <= OWNERSHIP_MAX_AGE_DAYS,
                   WARN, "filings on record inside %d days"
                   % OWNERSHIP_MAX_AGE_DAYS,
                   "oldest filing on record is %s days old" % stalest,
                   threshold=OWNERSHIP_MAX_AGE_DAYS, warn_only=True))
    shown, admitted = own.get("shown_count"), own.get("n")
    if shown is not None and admitted is not None:
        out.append(chk("NEWS_COUNT_RECONCILE" if False else
                       "OWNERSHIP_SHOWN_RECONCILE", shown <= admitted, ERROR,
                       "rows shown <= rows admitted",
                       "%s shown of %s admitted" % (shown, admitted)))

    # ── coverage semantics ──────────────────────────────────────────────
    op = view.get("options") or {}
    out.append(chk("MISSING_VS_NEGATIVE",
                   bool(op.get("available")) or bool(op.get("note")), ERROR,
                   "an absent feed carries a coverage note",
                   "available=%s, note=%s" % (op.get("available"),
                                              bool(op.get("note"))),
                   detail="Silence must never read as 'no activity'."))
    dispositions = {r.get("disposition") for r in recs.values()}
    knows_diff = (EV.AVAILABLE_NOT_INGESTED in dispositions
                  or EV.SOURCE_UNAVAILABLE in dispositions
                  or not recs)
    out.append(chk("UNAVAILABLE_VS_NOT_INGESTED", knows_diff, ERROR,
                   "public-but-unparsed evidence is labelled "
                   "AVAILABLE_NOT_INGESTED, distinct from SOURCE_UNAVAILABLE",
                   "dispositions in package: %s"
                   % ", ".join(sorted(d for d in dispositions if d))))

    evd = snap.get("evidence") or {}
    claimed = [k for k in ("calibrated_confidence", "probability",
                           "confidence_pct") if evd.get(k) is not None]
    out.append(chk("CALIBRATION_LANGUAGE", not claimed, ERROR,
                   "no self-assessment presented as calibrated",
                   ", ".join(claimed) or "none claimed"))

    # ── populations ─────────────────────────────────────────────────────
    soc = view.get("social") or {}
    c, n, rj = soc.get("n_considered"), soc.get("n_counted"), soc.get("n_rejected")
    okpop = None in (c, n, rj) or c == n + rj
    out.append(chk("POPULATION_ARITHMETIC", okpop, ERROR,
                   "considered == counted + rejected",
                   "%s == %s + %s" % (c, n, rj)))
    co = soc.get("coordination") or {}
    asserts = bool(re.search(r"\b(repeated|echo|coordinat|identical)",
                             str(co.get("label") or ""), re.I)
                   and not re.search(r"\b(no|none|not|zero)\b",
                                     str(co.get("label") or "")[:24], re.I))
    out.append(chk("COORDINATION_CONSISTENCY",
                   not (asserts and not co.get("phrase_groups")), ERROR,
                   "a coordination claim has phrase-group counts behind it",
                   "asserts=%s, groups=%s" % (asserts, co.get("phrase_groups"))))

    # ── calculations ────────────────────────────────────────────────────
    incomplete = [k for k, v in calcs.items()
                  if k.startswith("CALC-") and v.get("operands")
                  and not v.get("operands_complete")]
    rec_incomplete = [k for k, v in calcs.items()
                      if k.startswith("REC-") and v.get("operands")
                      and not v.get("operands_complete")]
    out.append(chk("REC_INPUT_TRACEABILITY", not rec_incomplete, WARN,
                   "every recommendation input resolves its references",
                   ", ".join(sorted(rec_incomplete)) or "all resolve",
                   warn_only=True,
                   detail="A judgement input is not a calculated metric, "
                          "but an unresolvable reference still means the "
                          "reader cannot follow the reasoning."))
    out.append(chk("CALC_OPERAND_COMPLETENESS", not incomplete, ERROR,
                   "every calculated figure resolves all of its operands",
                   "%d incomplete: %s" % (len(incomplete),
                                          ", ".join(sorted(incomplete)[:6]))
                   if incomplete else "all complete",
                   refs=sorted(incomplete)[:10]))

    mc = calcs.get("CALC-market_cap")
    if mc:
        ops = mc.get("operands") or []
        named = {str(o.get("evidence_id")) for o in ops}
        has_px = any("last_close" in n or "intraday_last" in n
                     or n.startswith("BAR-") or n.startswith("INTRADAY-")
                     for n in named)
        has_sh = any(n.startswith("SHR-") or "shares" in n for n in named)
        out.append(chk("MARKET_CAP_BASIS", has_px and has_sh, ERROR,
                       "market cap = observed price x latest filed shares, "
                       "both operands named",
                       "price operand=%s, shares operand=%s"
                       % (has_px, has_sh), refs=sorted(named)))
    else:
        out.append(skipped("MARKET_CAP_BASIS", ERROR,
                           "no market-cap calculation in this package"))

    pe = calcs.get("CALC-pe_trailing")
    if pe:
        ops = [o for o in (pe.get("operands") or []) if o.get("resolved")]
        out.append(chk("PE_OPERANDS_SHOWN", len(ops) >= 2, ERROR,
                       "P/E carries both operands with values",
                       "%d of %d operands resolved"
                       % (len(ops), len(pe.get("operands") or [])),
                       refs=[o.get("evidence_id") for o in
                             (pe.get("operands") or [])]))
    else:
        out.append(skipped("PE_OPERANDS_SHOWN", ERROR,
                           "no trailing P/E in this package"))
    return out


# ── the rendered artefact ───────────────────────────────────────────────

def _is_furniture(blk, page_h, band=0.085):
    y0, y1 = blk["bbox"][1], blk["bbox"][3]
    if y1 < page_h * band or y0 > page_h * (1 - band):
        return True
    txt = "".join(sp["text"] for ln in blk.get("lines", [])
                  for sp in ln.get("spans", []))
    return ("Educational research" in txt or txt.strip().startswith("Page ")
            or "DEMO DATA" in txt or "SYNTHETIC" in txt)


def _spans(d):
    for blk in d.get("blocks", []):
        for ln in blk.get("lines", []):
            for sp in ln.get("spans", []):
                yield sp


def check_pdf(pdf_bytes, snap=None, core=True, evidence=None, view=None):
    out = []
    try:
        import fitz
    except Exception:                                # pragma: no cover
        return [skipped(c, ERROR, "PyMuPDF unavailable")
                for c in ("PAGE_COUNT", "BLANK_PAGE", "TYPE_SIZE")]
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    n = doc.page_count
    if core:
        out.append(chk("PAGE_COUNT", n <= MAX_CORE_PAGES, ERROR,
                       "core brief at most %d pages" % MAX_CORE_PAGES,
                       "%d pages" % n, threshold=MAX_CORE_PAGES))

    smallest, texts, blanks, thin, ents, glyphs, oob = 99.0, [], [], [], [], [], []
    for pno in range(n):
        page = doc[pno]
        where = "page%d" % (pno + 1)
        txt = page.get_text()
        texts.append(txt)
        ph, pw = page.rect.height, page.rect.width
        d = page.get_text("dict")
        if not txt.strip():
            blanks.append(where)
            continue
        body = [b for b in d.get("blocks", []) if b.get("lines")
                and not _is_furniture(b, ph)]
        if body:
            usable = ph * (1 - 2 * 0.085)
            used = (max(b["bbox"][3] for b in body)
                    - min(b["bbox"][1] for b in body)) / usable
            if used < 0.60 and pno < n - 1:
                thin.append("%s (%.0f%%)" % (where, used * 100))
        for sp in _spans(d):
            if sp.get("text", "").strip():
                smallest = min(smallest, round(sp.get("size", 99), 1))
        for e in ENTITIES:
            if e in txt:
                ents.append("%s:%r" % (where, e))
        g = GLYPH_RX.search(txt)
        if g:
            glyphs.append("%s:%r" % (where, g.group(0)))
        for blk in d.get("blocks", []):
            bb = blk.get("bbox") or [0, 0, 0, 0]
            if bb[2] > pw - 2 or bb[0] < -1 or bb[3] > ph + 1:
                oob.append("%s x=%.0f/%.0f" % (where, bb[2], pw))
                break

    joined = "\n".join(texts)
    out += [
        chk("BLANK_PAGE", not blanks, FATAL, "no page without text",
            ", ".join(blanks) or "none"),
        chk("NEARLY_BLANK_PAGE", not thin, ERROR,
            "no non-final page below 60% of the frame",
            ", ".join(thin) or "none", threshold=0.60),
        chk("TYPE_SIZE", smallest >= MIN_BODY_PT - 0.05, ERROR,
            "no type below %.1fpt" % MIN_BODY_PT,
            "smallest %.1fpt" % smallest if smallest < 99 else "no text",
            threshold=MIN_BODY_PT),
        chk("HTML_ENTITIES", not ents, ERROR, "no HTML entity on the page",
            ", ".join(ents) or "none"),
        chk("GLYPH_INTEGRITY", not glyphs, ERROR,
            "no unrenderable or mis-decoded character",
            ", ".join(glyphs) or "none"),
        chk("CONTENT_BOUNDS", not oob, ERROR,
            "no block past the page edge", ", ".join(oob) or "none"),
    ]

    m = INTENT_RX.search(joined)
    out.append(chk("INSTITUTIONAL_INTENT", not m, FATAL,
                   "no claim to know why an institution traded",
                   repr(m.group(0)) if m else "none"))

    if core and snap:
        hit = None
        for rec in ((snap.get("sentiment") or {}).get("sample_records") or []):
            e = (rec.get("excerpt") or "").strip()
            if len(e) >= 25 and e[:40] in joined:
                hit = e[:48]
                break
        out.append(chk("RAW_SOCIAL_CONTAINMENT", not hit, ERROR,
                       "no raw post text in the core brief",
                       repr(hit) if hit else "none"))

    # ── document properties ─────────────────────────────────────────────
    md = doc.metadata or {}
    missing = [k for k in ("title", "author", "subject") if not md.get(k)]
    out.append(chk("PDF_METADATA", not missing, ERROR,
                   "title, author and subject all set",
                   "missing: %s" % ", ".join(missing) if missing
                   else "title=%r author=%r" % (md.get("title"),
                                                md.get("author"))))
    lang = None
    try:
        lang = doc.xref_get_key(doc.pdf_catalog(), "Lang")[1]
    except Exception:
        pass
    out.append(chk("PDF_LANGUAGE", bool(lang and "en" in str(lang).lower()),
                   ERROR, "catalog /Lang declares a language",
                   repr(lang) if lang else "absent"))
    toc = doc.get_toc() or []
    out.append(chk("PDF_BOOKMARKS", len(toc) >= (4 if core else 1),
                   ERROR if core else WARN,
                   "one bookmark per section",
                   "%d bookmarks" % len(toc), threshold=4 if core else 1))
    nlinks = sum(len(doc[i].get_links() or []) for i in range(n))
    out.append(chk("PDF_LINKS", nlinks > 0, ERROR,
                   "filings and news are clickable",
                   "%d link annotations" % nlinks, threshold=1))
    marked = None
    try:
        marked = doc.xref_get_key(doc.pdf_catalog(), "MarkInfo")[1]
    except Exception:
        pass
    # reportlab emits no marked content, so a structure tree cannot be
    # built honestly. This is reported as a known limitation, not passed
    # off as compliance and not silently ignored.
    out.append(chk("PDF_TAGGED", False, WARN,
                   "tagged PDF with a structure tree",
                   "untagged: %s" % (marked or "no /MarkInfo"),
                   warn_only=True,
                   detail="reportlab does not emit marked content, so real "
                          "PDF/UA tagging is not achievable from this "
                          "renderer. Declaring /MarkInfo without MCIDs "
                          "would be a false claim."))
    doc.close()
    return out


# ── page/evidence agreement ─────────────────────────────────────────────

def check_agreement(pdf_bytes, evidence, view, snap=None):
    """The page and the evidence package were formatted independently.
    Where they disagree about a number, one of them is wrong."""
    out = []
    try:
        import fitz
    except Exception:                                # pragma: no cover
        return [skipped("PDF_MATCHES_EVIDENCE", ERROR, "PyMuPDF unavailable")]
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    txt = "\n".join(doc[i].get_text() for i in range(doc.page_count))
    lines = [l.strip() for l in txt.splitlines() if l.strip()]
    doc.close()

    calcs = (evidence or {}).get("calculations") or {}
    checked, disagree = [], []
    for cid, c in sorted(calcs.items()):
        shown = c.get("result_displayed")
        slug = cid[5:] if cid.startswith("CALC-") else cid
        # A figure the report deliberately withholds is not a figure the
        # page is claiming. rel_volume is suppressed mid-session because a
        # partial day cannot be compared with full-session averages.
        if c.get("displayed") is False:
            continue
        if not shown or slug not in EV.DISPLAY:
            continue
        checked.append(cid)
        if shown not in txt:
            disagree.append("%s: evidence says %r, not found on the page"
                            % (cid, shown))
    out.append(chk("PDF_MATCHES_EVIDENCE", not disagree, ERROR,
                   "every displayed calculated figure appears on the page "
                   "exactly as the evidence package renders it",
                   "%d of %d disagree: %s"
                   % (len(disagree), len(checked), "; ".join(disagree[:4]))
                   if disagree else "%d figures agree" % len(checked),
                   refs=checked))

    # "nearest first" has to be true of the rendered rows, not just the
    # model. A correct list rendered in the wrong order is still wrong.
    # The rows the renderer drew, not the whole ladder. view["recovery"]
    # is every rung the model produced; the table shows the moving-average
    # subset. Checking the former against the latter reports levels as
    # "missing" that were never meant to be there.
    stages = view.get("levels_shown") or []
    if len(stages) >= 2:
        # Scan only the rows under the "Upside confirmation" heading.
        # A table cell extracts as its own line, so label and value never
        # share one; and scanning the whole document finds the setup-
        # metrics panel first, which lists the same levels in a different
        # order for a different reason. Neither is a ladder defect.
        head = next((i for i, l in enumerate(lines)
                     if l.strip().lower().startswith("moving-average levels")),
                    None)
        scope = lines[head:] if head is not None else lines
        pos, order_ok = [], True
        for st_ in stages:
            key = "%.2f" % st_["value"]
            idx = next((i for i, l in enumerate(scope) if key in l), None)
            pos.append(idx)
        seen = [p for p in pos if p is not None]
        order_ok = seen == sorted(seen) and len(seen) == len(stages)
        out.append(chk("LADDER_RENDER_ORDER", order_ok, ERROR,
                       "rendered level rows ascend by price, matching "
                       "the model",
                       "model %s -> row positions %s"
                       % ([round(x["value"], 2) for x in stages], pos)))
    elif (view.get("levels") or {}).get("upside_confirmation"):
        # The model produced levels but the renderer recorded none, which
        # means the row list stopped being written. Skipping here would
        # silently disable this check the next time the layout moves.
        out.append(chk("LADDER_RENDER_ORDER", False, ERROR,
                       "the renderer records the level rows it drew",
                       "model has %d upside level(s), renderer recorded none"
                       % len(view["levels"]["upside_confirmation"])))
    else:
        out.append(skipped("LADDER_RENDER_ORDER", ERROR,
                           "fewer than two level rows were rendered"))

    # Counts stated in the document must describe the document. The
    # brief writes "N of M items ... are shown here"; N has to equal the
    # rows actually rendered, and N may never exceed M.
    news = ((snap or {}).get("sentiment") or {}).get("news") or []
    admitted_real = len(news)
    rendered = len(re.findall(r"[·|]\s*20\d\d-\d\d-\d\d \d\d:\d\d ET", txt))
    m_pair = re.search(r"(\d+)\s+of\s+(\d+)\s+items", txt)
    m_adm = re.search(r"(\d+)\s+(?:\w+\s+){0,3}?admitted", txt)
    stated_shown = int(m_pair.group(1)) if m_pair else None
    stated_admitted = (int(m_pair.group(2)) if m_pair
                       else (int(m_adm.group(1)) if m_adm else None))
    problems = []
    if stated_shown is not None and rendered and stated_shown != rendered:
        problems.append("document says %d shown, %d rows rendered"
                        % (stated_shown, rendered))
    if stated_admitted is not None and rendered > stated_admitted:
        problems.append("%d rows rendered against %d stated admitted"
                        % (rendered, stated_admitted))
    if stated_admitted is not None and admitted_real             and stated_admitted > admitted_real:
        problems.append("document claims %d admitted, package holds %d"
                        % (stated_admitted, admitted_real))
    out.append(chk("NEWS_COUNT_RECONCILE", not problems, ERROR,
                   "shown == rendered rows, and shown <= admitted",
                   "; ".join(problems) if problems else
                   "shown=%s rendered=%d admitted=%s"
                   % (stated_shown, rendered, stated_admitted),
                   threshold=admitted_real))
    return out


# ── artifact identity ───────────────────────────────────────────────────

# -- v3.2: numbers must be reproducible from what we shipped -------------

# A level described one way and computed another. The report says "the
# lowest close"; if the formula reads min(low) the sentence is false even
# though the number is arithmetically correct.
BASIS_WORDS = [
    (re.compile(r"min\(close", re.I), "lowest close", "lowest intraday low"),
    (re.compile(r"max\(close", re.I), "highest close", "highest intraday high"),
    (re.compile(r"min\(low", re.I), "lowest intraday low", "lowest close"),
    (re.compile(r"max\(high", re.I), "highest intraday high", "highest close"),
]


def check_numerics(evidence, view, snap=None, pdf_text=""):
    out, ev = [], evidence or {}
    calcs = ev.get("calculations") or {}
    recs = ev.get("records") or {}

    # cardinality: the window a formula declares must be the window the
    # package delivers
    short = []
    for cid, c in sorted(calcs.items()):
        d, g = c.get("window_declared"), c.get("window_delivered")
        if d and g is not None and d != g:
            short.append("%s declares %d, delivers %d" % (cid, d, g))
    out.append(chk("BAR_CARDINALITY", not short, FATAL,
                   "delivered sessions equal the declared window",
                   "; ".join(short) if short else
                   "%d windowed calculations agree"
                   % len([c for c in calcs.values()
                          if c.get("window_declared")]),
                   refs=[x.split()[0] for x in short]))

    # a declared range whose endpoints are not in the package at all
    missing = []
    for cid, c in sorted(calcs.items()):
        for o in (c.get("operands") or []):
            r = str(o.get("evidence_id") or "")
            if ".." not in r:
                continue
            lo, hi = r.split("..", 1)
            if lo not in recs or hi not in recs:
                missing.append("%s: %s" % (cid, r))
    out.append(chk("BAR_RANGE_PRESENT", not missing, FATAL,
                   "both endpoints of every declared range are delivered",
                   "; ".join(missing[:4]) if missing else "all present"))

    # benchmark legs must be real observations, not nulls
    bench_bad = []
    for cid, c in sorted(calcs.items()):
        if "rs_" not in cid:
            continue
        if not c.get("benchmark_sessions_delivered"):
            bench_bad.append("%s has no embedded benchmark window" % cid)
    nulls = [k for k, v in recs.items()
             if v.get("evidence_type") == "benchmark_bar"
             and v.get("value") is None]
    if nulls:
        bench_bad.append("%d benchmark records carry null values" % len(nulls))
    out.append(chk("BENCHMARK_OPERANDS", not bench_bad, FATAL,
                   "relative strength cites an embedded, non-null benchmark",
                   "; ".join(bench_bad) if bench_bad else
                   "%d benchmark sessions embedded"
                   % len([1 for v in recs.values()
                          if v.get("evidence_type") == "benchmark_bar"]),
                   detail="A null placeholder must set resolved=false, not "
                          "pass as a resolved operand."))

    # an open session is not a close
    intraday = [k for k, v in recs.items()
                if v.get("evidence_type") == "intraday_observation"]
    bad_name = []
    if intraday:
        if "CALC-last_close" in calcs:
            bad_name.append("CALC-last_close published while the session "
                            "is open")
        for cid, c in calcs.items():
            for o in (c.get("operands") or []):
                if str(o.get("evidence_id", "")).startswith("INTRADAY-") \
                        and "close" in str(c.get("formula") or "").lower():
                    bad_name.append("%s calls an open-session observation a "
                                    "close" % cid)
    out.append(chk("PARTIAL_SESSION_NOT_A_CLOSE", not bad_name, FATAL,
                   "no incomplete session is presented as a close",
                   "; ".join(sorted(set(bad_name))) if bad_name else
                   ("open session carried as %s" % intraday[0]) if intraday
                   else "session closed; no intraday record"))

    # the words and the formula must describe the same basis
    mismatch = []
    for cid, c in sorted(calcs.items()):
        f = str(c.get("formula") or "")
        for rx, phrase, anti in BASIS_WORDS:
            if not rx.search(f):
                continue
            if pdf_text and anti in pdf_text and phrase not in pdf_text:
                mismatch.append("%s computes the %s; the page says %r"
                                % (cid, phrase, anti))
            break
    out.append(chk("LEVEL_WORDING_BASIS", not mismatch, FATAL,
                   "the page describes the basis the formula uses",
                   "; ".join(mismatch) if mismatch else "wording matches"))

    # guidance must keep the precision the issuer published
    trunc = []
    for k, v in sorted(recs.items()):
        if v.get("evidence_type") != "exhibit_guidance":
            continue
        val = v.get("value") or {}
        disp = v.get("display")
        if not disp or not pdf_text:
            continue
        for part in [x.strip() for x in str(disp).split(" - ")]:
            body = part.lstrip("$").rstrip("%MB")
            if "." not in body:
                continue
            short = body[:body.index(".") + 3].rstrip("0").rstrip(".")
            short_token = part.replace(body, short)
            # Deliberately NOT "and the exact form is absent". A page that
            # prints $2.565B in the table and $2.56B in the prose has shown
            # the reader two different numbers for one guide.
            if short != body and short_token in pdf_text:
                trunc.append("%s: page shows %s, issuer stated %s"
                             % (k, short_token, part))
    out.append(chk("GUIDANCE_PRECISION", not trunc, FATAL,
                   "guidance is displayed at the precision the issuer stated",
                   "; ".join(trunc[:4]) if trunc else
                   "issuer precision preserved"))

    # every guidance value the package holds should appear on the page
    if pdf_text:
        absent = []
        for k, v in sorted(recs.items()):
            if v.get("evidence_type") != "exhibit_guidance":
                continue
            disp = v.get("display")
            if not disp:
                continue
            # Compare the package's own rendering, which is produced from
            # a documented rule rather than by reading the renderer.
            parts = [x.strip() for x in str(disp).split(" - ")]
            if not all(pt in pdf_text for pt in parts):
                absent.append("%s (%s)" % (k, disp))
        out.append(chk("GUIDANCE_PDF_MATCH", not absent, ERROR,
                       "each guidance line in the package appears on the page",
                       "%d absent: %s" % (len(absent), "; ".join(absent[:4]))
                       if absent else "all guidance rendered", refs=absent))

    # coverage must not contradict what the report shows
    cov = ev.get("source_coverage") or {}
    contra = []
    ex_ok = any(str(v.get("evidence_type", "")).startswith("exhibit_")
                and v.get("disposition") == EV.ADMITTED
                for v in recs.values())
    for key, txt in cov.items():
        t = str(txt or "").lower()
        if ex_ok and "non_gaap" in key and ("not available" in t
                                            or "unavailable" in t):
            contra.append("%s says unavailable while exhibit figures are "
                          "admitted" % key)
    out.append(chk("COVERAGE_CONSISTENCY", not contra, FATAL,
                   "source coverage agrees with the evidence admitted",
                   "; ".join(contra) if contra else
                   "%d coverage lines consistent" % len(cov)))

    # a displayed count must say which artifact it counts
    pops = ev.get("populations") or {}
    vague = [d for d, pp in pops.items()
             if pp.get("legacy_records_displayed") is not None
             and pp.get("shown_core") is None
             and pp.get("shown_appendix") is None]
    out.append(chk("DISPLAY_COUNT_SCOPE", not vague, ERROR,
                   "every population reports shown_core / shown_appendix "
                   "rather than a bare displayed count",
                   ", ".join(sorted(vague)) if vague else
                   "%d populations scoped" % len(pops)))

    # the arithmetic has to redo from the delivered records
    # "17 of 22" invited the reader to assume five figures had quietly
    # failed. Numeric results that must reproduce are counted separately
    # from narrative inputs that cannot, and the exempt ones are named.
    cov = ev.get("calculation_coverage") or {}
    failed = cov.get("failed_detail") or []
    exempt = cov.get("exempt_detail") or []
    out.append(chk("CALC_REPRODUCIBLE", not failed, FATAL,
                   "every numeric calculation redoes from delivered evidence",
                   ("numeric_reproduced=%s numeric_failed=%s "
                    "nonnumeric_exempt=%s (%s)"
                    % (cov.get("numeric_reproduced"),
                       cov.get("numeric_failed"),
                       cov.get("nonnumeric_exempt"),
                       ", ".join(x["calculation_id"] for x in exempt)))
                   if cov else "no coverage report in the package",
                   refs=[x.get("calculation_id") for x in failed],
                   detail="; ".join("%s: %s" % (x["calculation_id"],
                                                x.get("note"))
                                    for x in failed[:3]) or None))
    # The appendix renders source coverage from its own copy of prov.
    # When only the evidence package was corrected, the two artifacts
    # disagreed about whether non-GAAP was available.
    if pdf_text:
        cov_txt = ev.get("source_coverage") or {}
        clash = []
        ex_ok = any(str(v.get("evidence_type", "")).startswith("exhibit_")
                    and v.get("disposition") == EV.ADMITTED
                    for v in recs.values())
        if ex_ok and "non-GAAP measures are not XBRL-tagged" in pdf_text:
            clash.append("a rendered page still calls non-GAAP unavailable "
                         "while the package marks it ADMITTED")
        out.append(chk("PDF_SOURCE_COVERAGE_MATCH", not clash, FATAL,
                       "rendered source-coverage states match evidence.json",
                       "; ".join(clash) if clash else
                       "%d coverage lines agree across artifacts"
                       % len(cov_txt)))

        # a count printed in an artifact must name the artifact it counts
        vague_txt = re.search(r"(\d+)\s+records?\s+displayed", pdf_text)
        out.append(chk("APPENDIX_COUNT_SCOPE", not vague_txt, ERROR,
                       "counts in the rendered artifacts carry a scope",
                       ("a rendered count says %r with no artifact named"
                        % vague_txt.group(0)) if vague_txt else
                       "no unscoped display counts rendered"))

    hv = ev.get("hash_verification") or {}
    out.append(chk("HASH_COVERAGE", (hv.get("coverage_pct") or 0) >= 99.9,
                   ERROR, "every record carries a canonical record_hash",
                   "%s%% of %s records" % (hv.get("coverage_pct"),
                                           hv.get("records_total")),
                   threshold=100,
                   detail="hash version %s" % hv.get("hash_version")))

    # Presence is not verification. Recompute every hash from the file
    # itself: v3.2 shipped 497 hashes, all present, none reproducible.
    rc = EV.verify_record_hashes(ev)
    out.append(chk("RECORD_HASH_RECOMPUTE", rc["all_match"], FATAL,
                   "every record_hash recomputes from the delivered record",
                   "%d of %d match%s" % (rc["matched"], rc["total"],
                                         "" if rc["all_match"] else
                                         "; mismatched: %s"
                                         % ", ".join(rc["mismatched"][:6])),
                   refs=rc["mismatched"][:20],
                   detail=EV.HASH_CANONICALIZATION))

    # Relative strength is only meaningful if both legs cover the same
    # sessions. The vendor series ran a day ahead at both ends.
    bench_dates = sorted(k[4:] for k, v in recs.items()
                         if v.get("evidence_type") == "benchmark_bar")
    align_note, aligned = "no benchmark window in this package", True
    for cid, c in calcs.items():
        if "rs_" not in cid:
            continue
        rng = [str(o.get("evidence_id")) for o in (c.get("operands") or [])]
        iss = [r for r in rng if r.startswith("BAR-")]
        ben = [r for r in rng if r.startswith("SPY-")]
        if not iss or not ben:
            aligned, align_note = False, "%s cites no paired windows" % cid
            break
        i_lo, i_hi = iss[0].split("..")
        b_lo, b_hi = ben[0].split("..")
        if i_lo[4:] != b_lo[4:] or i_hi[4:] != b_hi[4:]:
            aligned = False
            align_note = ("%s spans %s..%s while the benchmark spans %s..%s"
                          % (cid, i_lo[4:], i_hi[4:], b_lo[4:], b_hi[4:]))
            break
        align_note = ("both legs span %s..%s across %d benchmark sessions"
                      % (i_lo[4:], i_hi[4:], len(bench_dates)))
    out.append(chk("BENCHMARK_DATE_ALIGNMENT", aligned, FATAL,
                   "issuer and benchmark legs cover identical sessions",
                   align_note,
                   detail="A return difference measured over two different "
                          "spans is not a relative-strength figure."))
    return out


# -- v3.3.1: the words have to be as precise as the numbers -------------

# A count with no artifact behind it. "5 records displayed" was true of
# the pipeline, the core page and the appendix at different call sites,
# and meant a different number in each.
UNSCOPED_COUNT_RX = re.compile(
    r"\b\d+\s+(?:records?|items?|rows?|posts?)\s+displayed\b", re.I)

# Close-derived extrema that do not say so. A "60-session low" computed
# from closes reads as an intraday low to anyone who trades.
UNLABELLED_EXTREMA_RX = re.compile(
    r"\b(?:60-session|52-week|252-session)\s+(?!closing\b)(?:high|low)\b",
    re.I)


def check_shared_snapshot(snap=None):
    """Run the v2 gate's snapshot checks here too.

    Two renderers read one snapshot. v3 verifies the calculations it
    exports; v2 verifies the refs the snapshot claims. Neither looked at
    the other's territory, so a snapshot could be internally wrong in a
    way that took v2 down while v3 reported every one of its checks
    green — which is exactly what happened: two setup facts published
    citing CALC- ids that were never emitted, v3 PASS 78/78, v2 refusing
    every ticker.

    This calls research_snapshot's own function rather than
    reimplementing it. A second implementation of "do these refs
    resolve" is a second thing to drift; the point of this check is that
    both pipelines agree because they are asking the same code."""
    out = []
    if not snap:
        return [skipped("SNAPSHOT_REFS_RESOLVE", FATAL,
                        "no snapshot handed to the validator")]
    try:
        bad = rs.check_evidence_refs(snap)
    except Exception as e:                            # pragma: no cover
        return [chk("SNAPSHOT_REFS_RESOLVE", False, FATAL,
                    "the shared snapshot check runs",
                    "check_evidence_refs raised: %s" % e)]
    out.append(chk("SNAPSHOT_REFS_RESOLVE", not bad, FATAL,
                   "every published fact cites evidence ids the export "
                   "actually carries (research_snapshot.check_evidence_refs)",
                   "; ".join(bad[:3]) if bad else
                   "all snapshot evidence refs resolve",
                   detail="Shared with the v2 publication gate: a failure "
                          "here would refuse the v2 brief too."))
    return out


def check_setup(evidence, view, snap=None, pdf_text=""):
    """The setup layer restored in v3.4: the metric panel, the chart
    window, and the freshness of the tape behind both.

    These sit apart from check_numerics because they fail for a different
    reason. check_numerics asks whether a published number can be rebuilt
    from the delivered evidence. These ask whether the panel a reader
    scans in five seconds says where each cell came from, how old it is,
    and how many sessions the picture covers."""
    out, ev = [], evidence or {}
    calcs = ev.get("calculations") or {}
    recs = ev.get("records") or {}
    view = view or {}
    metrics = view.get("setup_metrics") or []

    # Every derived cell names a calculation, and that calculation
    # reproduced. A DER tag with nothing behind it is decoration.
    if metrics:
        bad = []
        for m in metrics:
            if m.get("value") == "Unavailable" or m.get("grade") != M.DERIVED:
                continue
            cid = m.get("calc")
            if not cid:
                bad.append("%s cites no calculation" % m["label"])
            elif cid not in calcs:
                bad.append("%s cites %s, absent from the package"
                           % (m["label"], cid))
            elif calcs[cid].get("reproducible") is False:
                bad.append("%s: %s did not reproduce" % (m["label"], cid))
        n_der = len([m for m in metrics if m.get("grade") == M.DERIVED
                     and m.get("value") != "Unavailable"])
        out.append(chk("METRIC_FORMULA_TRACEABLE", not bad, ERROR,
                       "every derived setup metric cites a calculation that "
                       "reproduced from the delivered evidence",
                       "; ".join(bad[:4]) if bad else
                       "%d derived metric(s) traced" % n_der,
                       refs=sorted(set(m["calc"] for m in metrics
                                       if m.get("calc")))))

        # A vendor figure a reader cannot age is not a fact. Anything
        # sourced outside the filings or our own bars carries an as-of
        # date, or says Unavailable and why.
        undated = []
        for m in metrics:
            if m.get("value") == "Unavailable":
                if not m.get("unavailable_reason"):
                    undated.append("%s is unavailable with no reason given"
                                   % m["label"])
                continue
            b = str(m.get("basis") or "")
            if any(w in b.lower() for w in VENDOR_WORDS) \
                    and not DATE_RX.search(b) and not UNDATED_RX.search(b):
                undated.append("%s: vendor basis %r carries no date"
                               % (m["label"], b))
        out.append(chk("METRIC_SOURCE_DATED", not undated, ERROR,
                       "a vendor-sourced metric carries an as-of date or "
                       "states that none is published, and an unavailable "
                       "metric carries a reason",
                       "; ".join(undated[:4]) if undated else
                       "%d metric(s) carry a source date or a reason"
                       % len(metrics)))
    else:
        out.append(skipped("METRIC_FORMULA_TRACEABLE", ERROR,
                           "no setup metric panel in this view"))
        out.append(skipped("METRIC_SOURCE_DATED", ERROR,
                           "no setup metric panel in this view"))

    # Freshness of the newest completed session the package carries.
    sessions = sorted(r["period"]["session"] for r in recs.values()
                      if r.get("evidence_type") == "market_bar"
                      and (r.get("period") or {}).get("session"))
    rt = (snap or {}).get("report_time")
    age = M._days_between(sessions[-1], rt) if (sessions and rt) else None
    out.append(chk("MARKET_DATA_FRESHNESS",
                   age is None or age <= MAX_MARKET_DATA_AGE_DAYS, ERROR,
                   "the newest completed session is no more than %d days "
                   "before the report time" % MAX_MARKET_DATA_AGE_DAYS,
                   ("newest completed session %s is %s days old"
                    % (sessions[-1], age)) if age is not None
                   else "no dated market bar to age",
                   threshold=MAX_MARKET_DATA_AGE_DAYS,
                   refs=["BAR-%s" % sessions[-1]] if sessions else []))

    # The chart reports how many sessions it drew. The package must hold
    # at least that many, and the caption must state the same number.
    cm = view.get("chart") or {}
    drew = cm.get("sessions")
    if drew:
        have, problems = len(sessions), []
        if have and drew > have:
            problems.append("chart drew %d sessions from %d delivered bars"
                            % (drew, have))
        # Anchored on the caption's own wording. A bare "N completed
        # sessions" also appears in the base-tightness sentence on page 1,
        # and matching that reported a 20-session chart.
        m_ = re.search(r"(\d+)\s+completed sessions: candles",
                       pdf_text or "")
        if m_ and int(m_.group(1)) != drew:
            problems.append("caption states %s, chart drew %d"
                            % (m_.group(1), drew))
        out.append(chk("CHART_WINDOW_CARDINALITY", not problems, ERROR,
                       "the chart window, its caption and the delivered bars "
                       "agree",
                       "; ".join(problems) if problems else
                       "%d sessions drawn, stated and delivered" % drew,
                       threshold=drew))
    else:
        out.append(skipped("CHART_WINDOW_CARDINALITY", ERROR,
                           "no chart metadata was recorded for this run"))

    # The open session must not be inside an average.
    intra = set(k for k, r in recs.items()
                if r.get("evidence_type") == "intraday_observation"
                or str(k).startswith("INTRADAY-"))
    leaked = []
    for cid, c in sorted(calcs.items()):
        if cid in PARTIAL_OK:
            continue
        for o in (c.get("operands") or []):
            oid = str(o.get("evidence_id") or "")
            if any(i == oid or i in oid.split("..") for i in intra):
                leaked.append("%s takes %s" % (cid, oid))
    out.append(chk("PARTIAL_BAR_EXCLUDED", not leaked, FATAL,
                   "no average, extreme or window statistic takes the open "
                   "session as an operand",
                   "; ".join(leaked[:4]) if leaked else
                   ("%d intraday record(s), none inside a window calculation"
                    % len(intra)) if intra else "no open session in this run",
                   detail="CALC-intraday_last is the only calculation that "
                          "is supposed to read it."))
    return out


def check_editorial(evidence, view, snap=None, pdf_text="", appendix_text=""):
    """Wording that would mislead a careful reader, even where every
    number behind it is correct."""
    out, ev = [], evidence or {}
    both = "\n".join(t for t in (pdf_text, appendix_text) if t)

    hits = sorted(set(m.group(0) for m in UNSCOPED_COUNT_RX.finditer(both)))
    out.append(chk("DISPLAY_COUNT_UNSCOPED", not hits, ERROR,
                   "every rendered count names the artifact it counts",
                   "; ".join(hits) if hits else
                   "no unscoped display counts in either artifact",
                   detail="Expected form: N evidence records - N admitted - "
                          "N shown in core - N shown in appendix."))

    # The appendix filter removes explicit, abusive and content-free
    # posts. What survives still spans bullish, bearish and neutral, so
    # describing the sample as "neutral" states the wrong property.
    classes = set()
    for r in (ev.get("records") or {}).values():
        if r.get("evidence_type") == "social_post" and r.get("classification"):
            classes.add(str(r["classification"]).lower())
    mixed = len(classes) > 1
    claims_neutral = bool(re.search(r"neutral[, ]+representative|"
                                    r"neutral representative", both, re.I))
    out.append(chk("SOCIAL_SAMPLE_DESCRIPTION", not (mixed and claims_neutral),
                   ERROR,
                   "a multi-classification sample is described as screened, "
                   "not neutral",
                   ("sample spans %s but the page calls it neutral"
                    % ", ".join(sorted(classes))) if (mixed and claims_neutral)
                   else "sample described as %s across %d classification(s)"
                   % ("screened" if "screened" in both.lower() else "unlabelled",
                      len(classes))))

    # A range statistic computed from closes must say "closing".
    bad = []
    for cid, c in sorted((ev.get("calculations") or {}).items()):
        f = str(c.get("formula") or "")
        if re.search(r"(?:min|max)\(close", f, re.I):
            bad.extend(sorted(set(m.group(0)
                                  for m in UNLABELLED_EXTREMA_RX.finditer(
                                      both))))
            break
    bad = sorted(set(bad))
    out.append(chk("CLOSE_EXTREMA_LABELLED", not bad, ERROR,
                   "close-derived extrema are labelled 'closing'",
                   "; ".join(bad) if bad else
                   "all range extrema labelled on a close basis",
                   detail="A 60-session low computed from closes reads as an "
                          "intraday low unless it says otherwise."))

    # The setup layer restored the legacy report's visibility. It does
    # not restore its vocabulary: a "dead-cat bounce" asserts a cause the
    # tape cannot show, and a confidence score or scenario probability is
    # a guess wearing a measurement's clothes. INSTITUTIONAL_INTENT bans
    # the who; this bans the why and the fake precision.
    hits = sorted(set(m.group(0).strip()
                      for m in INTERPRETIVE_RX.finditer(both)))
    out.append(chk("INTERPRETIVE_PHRASE", not hits, ERROR,
                   "no categorical read, confidence score or scenario "
                   "probability appears in either artifact",
                   "; ".join(hits[:4]) if hits else
                   "no unsupported interpretive phrasing",
                   detail="Allowed only behind a validated rule and the "
                          "evidence that supports it."))
    return out


def check_artifacts(artifacts, core, apx, ev_bytes):
    """Hash what we were actually handed and compare it with the manifest
    the package advertises."""
    out = []
    got = {"core_pdf": _sha(core), "appendix_pdf": _sha(apx),
           "evidence_json": _sha(ev_bytes) if ev_bytes is not None else None}
    bad = []
    for k, h in got.items():
        want = ((artifacts or {}).get(k) or {}).get("sha256")
        if h is None or want is None:
            continue
        if h != want:
            bad.append("%s manifest=%s actual=%s" % (k, want[:12], h[:12]))
    out.append(chk("ARTIFACT_HASHES", not bad, FATAL,
                   "validated bytes match the manifest for every artifact",
                   "; ".join(bad) if bad else
                   "%d artifact hashes match" % len(
                       [v for v in got.values() if v]),
                   refs=[v for v in got.values() if v]))
    return out


def verify_delivered(paths, manifest):
    """Re-read the files from disk and compare with the manifest. This is
    what catches a PDF edited after validation ran."""
    out = []
    for key, path in (paths or {}).items():
        want = ((manifest or {}).get(key) or {}).get("sha256")
        if not want:
            continue
        try:
            with open(path, "rb") as fh:
                got = _sha(fh.read())
        except Exception as e:
            out.append(chk("ARTIFACT_HASHES", False, FATAL,
                           "%s readable and matching manifest" % key,
                           "unreadable: %s" % e))
            continue
        out.append(chk("ARTIFACT_HASHES", got == want, FATAL,
                       "%s matches manifest %s" % (key, want[:12]),
                       "on disk %s" % got[:12],
                       detail=None if got == want else
                       "%s was modified after validation" % os.path.basename(
                           path)))
    return out


# ── the report ──────────────────────────────────────────────────────────

def report(view, snap, core_pdf, appendix_pdf=None, evidence=None,
           artifacts=None, prov=None, published_only=True, ev_bytes=None):
    # The caller passes the EXACT bytes it wrote to disk. Re-serialising
    # `evidence` here produced a different string, because build_all adds
    # the evidence file's own entry to the artifacts dict after writing —
    # so the validator hashed an object that was never a file.
    if ev_bytes is None and evidence is not None:
        ev_bytes = json.dumps(evidence, indent=1, default=str,
                              sort_keys=True).encode("utf-8")
    checks = []
    checks += check_model(view, snap, evidence)
    checks += check_pdf(core_pdf, snap, core=True, evidence=evidence,
                        view=view)
    if appendix_pdf:
        # Same rules, different document. Sharing check ids made an
        # appendix result read as a core failure — PDF_LINKS reported
        # zero for a core brief that in fact carried twelve.
        for c in check_pdf(appendix_pdf, snap, core=False):
            c = dict(c, check_id=c["check_id"] + "_APPENDIX",
                     scope="appendix")
            if c["check_id"] in ("PDF_LINKS_APPENDIX",
                                 "PDF_BOOKMARKS_APPENDIX")                     and c["status"] == FAIL:
                c["status"], c["severity"] = WARN_S, WARN
            checks.append(c)
    checks += check_agreement(core_pdf, evidence, view, snap)
    try:
        import fitz
        _d = fitz.open(stream=core_pdf, filetype="pdf")
        _txt = chr(10).join(_d[i].get_text()
                            for i in range(_d.page_count))
        _d.close()
    except Exception:
        _txt = ""
    _atxt = ""
    if appendix_pdf:
        try:
            import fitz as _f
            _ad = _f.open(stream=appendix_pdf, filetype="pdf")
            _atxt = chr(10).join(_ad[i].get_text()
                                 for i in range(_ad.page_count))
            _ad.close()
        except Exception:
            _atxt = ""
    checks += check_numerics(evidence, view, snap, _txt)
    checks += check_setup(evidence, view, snap, _txt)
    checks += check_shared_snapshot(snap)
    checks += check_editorial(evidence, view, snap, _txt, _atxt)
    checks += check_artifacts(artifacts, core_pdf, appendix_pdf or b"",
                              ev_bytes)

    # Prove the gates on the same run that passes them. A validator that
    # has never been shown to fail is a validator nobody has tested.
    try:
        import report_v3_mutation as MUT
        mutation = MUT.summary()
    except Exception as e:                       # pragma: no cover
        mutation = {"all_checks_proven": False, "error": str(e),
                    "note": "mutation suite could not run"}

    fatal = [c for c in checks if c["status"] == FAIL
             and c["severity"] in (FATAL, ERROR)]
    return {
        "schema": "stock_research_brief_validation/v3.1",
        "validator_version": VALIDATOR_VERSION,
        "validator_code_sha256": validator_code_hash(),
        "schema_sha256": schema_hash(),
        "ticker": view.get("ticker"),
        "report_time_utc": view.get("report_time_utc"),
        "artifacts": artifacts or {},
        "validated_bytes": {
            "core_pdf_sha256": _sha(core_pdf),
            "appendix_pdf_sha256": _sha(appendix_pdf) if appendix_pdf else None,
            "evidence_json_sha256": _sha(ev_bytes) if ev_bytes else None,
        },
        "counts": {
            "total": len(checks),
            "pass": len([c for c in checks if c["status"] == PASS]),
            "fail": len([c for c in checks if c["status"] == FAIL]),
            "warn": len([c for c in checks if c["status"] == WARN_S]),
            "skip": len([c for c in checks if c["status"] == SKIP]),
        },
        "checks": checks,
        "mutation_tests": mutation,
        "blocking_failures": ([c["check_id"] for c in fatal]
                              + ([] if mutation.get("all_checks_proven")
                                 else ["MUTATION_SUITE"])),
        # A package is only valid if its checks passed AND those checks
        # were demonstrated to be capable of failing.
        "ok": bool(not fatal and mutation.get("all_checks_proven")),
    }
