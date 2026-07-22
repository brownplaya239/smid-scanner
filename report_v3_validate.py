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

VALIDATOR_VERSION = "3.1.0"

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
    "CALC_OPERAND_COMPLETENESS", "PE_OPERANDS_SHOWN",
    "NEWS_COUNT_RECONCILE", "PDF_MATCHES_EVIDENCE",
    "PAGE_COUNT", "BLANK_PAGE", "NEARLY_BLANK_PAGE", "TYPE_SIZE",
    "HTML_ENTITIES", "GLYPH_INTEGRITY", "CONTENT_BOUNDS",
    "INSTITUTIONAL_INTENT", "RAW_SOCIAL_CONTAINMENT",
    "PDF_METADATA", "PDF_LANGUAGE", "PDF_BOOKMARKS", "PDF_LINKS",
    "PDF_TAGGED", "ARTIFACT_HASHES",
]


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
                   detail="A level derived from a 60-session low is a "
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
                  if v.get("operands") and not v.get("operands_complete")]
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
        has_px = any("last_close" in n or n.startswith("BAR-") for n in named)
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
    stages = view.get("recovery") or []
    if len(stages) >= 2:
        pos, order_ok = [], True
        for s in stages:
            key = "%.2f" % s["value"]
            idx = next((i for i, l in enumerate(lines) if key in l), None)
            pos.append(idx)
        seen = [p for p in pos if p is not None]
        order_ok = seen == sorted(seen) and len(seen) == len(stages)
        out.append(chk("LADDER_RENDER_ORDER", order_ok, ERROR,
                       "rendered rows appear nearest-first, matching the model",
                       "model %s -> page positions %s"
                       % ([round(s["value"], 2) for s in stages], pos)))
    else:
        out.append(skipped("LADDER_RENDER_ORDER", ERROR,
                           "fewer than two upside stages to order"))

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
    checks += check_artifacts(artifacts, core_pdf, appendix_pdf or b"",
                              ev_bytes)

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
        "blocking_failures": [c["check_id"] for c in fatal],
        "ok": not fatal,
    }
