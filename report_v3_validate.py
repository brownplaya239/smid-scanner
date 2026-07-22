#!/usr/bin/env python3
"""report_v3_validate.py — the checks that can fail a v3 brief.

A validator that only ever returns "ok" has proved nothing, so every
rule here corresponds to a defect we actually shipped at least once:
a ladder that told the reader to reclaim a level they were already
above, a seven-week-old filing presented as today's driver, a page that
was half white space, a filing count with nothing to look the filings up
by, and message-board text sitting in the middle of a decision page.

Two entry points:

    validate_model(view, snap)   semantics — runs before any rendering
    validate_pdf(pdf_bytes, ...) the artefact — runs after

Both return a list of problem dicts. Empty means clean.
"""

import re

import report_v3_model as M

MIN_BODY_PT = 9.0
MAX_CORE_PAGES = 4

# Language that claims to know why an institution traded. We can observe
# that a filing exists and that volume expanded; we cannot observe
# intent, and phrasing that implies we can is the single easiest way for
# a research note to overstate its evidence.
INTENT_RX = re.compile(
    r"\b(institution(?:s|al)?\s+(?:are\s+)?(?:accumulat|buy|sell|distribut|"
    r"buil[dt]|unload|exit)\w*"
    r"|smart\s+money"
    r"|(?:institutional|quiet|stealth)\s+(?:accumulation|distribution)"
    r"|being\s+accumulated"
    r"|under\s+accumulation"
    r"|whales?\s+(?:are\s+)?(?:buy|sell)\w*)", re.I)

# Corrupted text arrives in three shapes: the replacement character a
# font substitution leaves behind, raw control bytes, and the mojibake
# family that appears when UTF-8 is decoded as cp1252.
GLYPH_RX = re.compile(
    "[\ufffd\u0000-\u0008\u000b\u000c\u000e-\u001f]"
    "|\u00e2\u20ac[\u2122\u201c\u201d]"
    "|\u00c3[\u00a9\u00a8\u00bc\u00b1]")

ENTITIES = ("&amp;", "&#39;", "&quot;", "&gt;", "&lt;", "&nbsp;", "&#x27;")


def _p(code, detail, where=""):
    return {"code": code, "detail": detail, "where": where}


# ── semantics ───────────────────────────────────────────────────────────

def validate_model(view, snap=None):
    out = []
    snap = snap or {}

    # 1. recovery stages must strictly ascend. This is the check that
    #    catches a ladder written from habit rather than from the data.
    stages = view.get("recovery") or []
    vals = [s["value"] for s in stages]
    if vals != sorted(vals):
        out.append(_p("ladder_unordered",
                      "recovery stages are not in ascending price order: %s"
                      % ", ".join("%s %.2f" % (s["label"], s["value"])
                                  for s in stages), "page1"))
    for a, b in zip(stages, stages[1:]):
        if b["value"] <= a["value"]:
            out.append(_p("ladder_unordered",
                          "stage '%s' (%.2f) does not sit above the stage "
                          "before it, '%s' (%.2f)"
                          % (b["label"], b["value"], a["label"], a["value"]),
                          "page1"))
    price = view.get("price")
    for s in stages:
        if price is not None and s["value"] <= price:
            out.append(_p("ladder_below_spot",
                          "'%s' at %.2f is presented as an upside trigger but "
                          "sits at or below spot %.2f"
                          % (s["label"], s["value"], price), "page1"))

    # 2. a metric may not be newer than the source it came from, and a
    #    price-derived number may not be stamped with a filing date.
    q = view.get("quote_time_utc")
    if view.get("quote_tz_warning"):
        out.append(_p("timestamp_no_zone", view["quote_tz_warning"], "page1"))
    if q and view.get("report_time_utc") and q > view["report_time_utc"]:
        out.append(_p("timestamp_mismatch",
                      "market data (%s) is stamped after the report itself "
                      "(%s)" % (q, view["report_time_utc"]), "page1"))

    # 3. a stale filing must not be sold as the current driver
    cat = view.get("catalysts") or {}
    drv = cat.get("current_driver") or {}
    last = cat.get("last_reported") or {}
    if drv.get("grade") == M.DERIVED and last.get("age_days") is not None \
            and last["age_days"] > M.CURRENT_DRIVER_DAYS:
        out.append(_p("stale_catalyst_as_driver",
                      "a disclosure %d days old is presented as the current "
                      "driver (limit %d)"
                      % (last["age_days"], M.CURRENT_DRIVER_DAYS), "page4"))

    # 4. the exit has to be compatible with the holding period
    ex = view.get("exit") or {}
    if ex.get("atr_multiple") is not None and ex.get("floor"):
        # A boundary placed exactly at the floor is compliant; the
        # tolerance is for the round trip through price - k*atr and back.
        if ex["atr_multiple"] < ex["floor"] - 1e-6:
            out.append(_p("exit_horizon_mismatch",
                          "%s sits %.1f x ATR from spot, inside the %.1f x "
                          "floor for a '%s' horizon — normal daily range "
                          "would trigger it"
                          % (ex.get("label"), ex["atr_multiple"], ex["floor"],
                             ex.get("horizon")), "page1"))
    if ex.get("value") is not None and not ex.get("basis"):
        out.append(_p("exit_undocumented",
                      "an exit level is shown with no stated basis", "page1"))
    if ex.get("active_entry") is False and ex.get("label") == "Invalidation":
        out.append(_p("invalidation_without_entry",
                      "'Invalidation' is used with no active entry; with no "
                      "position on the book this is a risk boundary",
                      "page1"))

    # 5. a filing count needs accession numbers behind it
    own = view.get("ownership") or {}
    if own.get("n"):
        missing = [r for r in own.get("rows") or [] if not r.get("accession")]
        if missing:
            out.append(_p("filing_count_without_accession",
                          "%d of %d Schedule 13 filings are counted without an "
                          "accession number" % (len(missing), own["n"]),
                          "page3"))
        if not own.get("filers_parsed") and not own.get("interpretation"):
            out.append(_p("ownership_overclaimed",
                          "filer identities were not parsed but no "
                          "'interpretation unavailable' notice is shown",
                          "page3"))

    # 6. never turn an absent feed into a signal
    op = view.get("options") or {}
    if not op.get("available") and not op.get("note"):
        out.append(_p("missing_read_as_negative",
                      "options section is empty with no coverage note, which "
                      "reads as 'no options activity'", "page3"))

    # 7. self-assessed confidence must not be dressed as calibration
    ev = snap.get("evidence") or {}
    for k in ("calibrated_confidence", "probability", "confidence_pct"):
        if ev.get(k) is not None:
            out.append(_p("uncalibrated_confidence",
                          "'%s' is a self-assessment and cannot be presented "
                          "as calibrated" % k, "page1"))

    # 8. internal arithmetic in the social block must actually add up
    soc = view.get("social") or {}
    cons, cnt, rej = (soc.get("n_considered"), soc.get("n_counted"),
                      soc.get("n_rejected"))
    if None not in (cons, cnt, rej) and cons != cnt + rej:
        out.append(_p("population_mismatch",
                      "%s considered != %s counted + %s rejected"
                      % (cons, cnt, rej), "page4"))
    # A label is only a contradiction when it ASSERTS coordination with
    # no counts behind it. "no repeated-phrase groups detected" beside an
    # empty count is the two agreeing, and flagging it punished the
    # validator's own correct negative finding.
    co = soc.get("coordination") or {}
    asserts = re.search(r"\b(repeated|echo|coordinat|identical)",
                        str(co.get("label") or ""), re.I) and not \
        re.search(r"\b(no|none|not|zero)\b", str(co.get("label") or "")[:24],
                  re.I)
    if asserts and not co.get("phrase_groups"):
        out.append(_p("coordination_contradiction",
                      "a coordination assessment is stated (%r) while the "
                      "phrase-group count is empty" % str(co["label"])[:60],
                      "page4"))
    return out


# ── the rendered artefact ───────────────────────────────────────────────

def validate_pdf(pdf_bytes, snap=None, core=True, page_w=None, margin=None):
    """Open the file we are about to ship and look at it. Everything here
    is measured off the real page geometry, not the intent of the code
    that produced it."""
    out = []
    try:
        import fitz
    except Exception:                                # pragma: no cover
        return [_p("audit_skipped", "PyMuPDF unavailable — page audit "
                                    "could not run")]
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    n = doc.page_count
    if core and n > MAX_CORE_PAGES:
        out.append(_p("core_too_long",
                      "core brief is %d pages; the limit is %d"
                      % (n, MAX_CORE_PAGES)))
    smallest, all_text = 99.0, []
    for pno in range(n):
        page = doc[pno]
        where = "page%d" % (pno + 1)
        txt = page.get_text()
        all_text.append(txt)
        pw, ph = page.rect.width, page.rect.height
        d = page.get_text("dict")

        if not txt.strip():
            out.append(_p("blank_page", "page carries no text", where))
            continue

        # Nearly blank: how far down the page does real content reach?
        # The running header and footer are painted on every page, so
        # counting them made a half-empty page measure as full — this
        # check silently passed the very page it was written for.
        body = [b for b in d.get("blocks", []) if b.get("lines")
                and not _is_furniture(b, ph)]
        if body:
            top = min(b["bbox"][1] for b in body)
            # Denominator is the printable frame, not the sheet: roughly
            # 8.5% of each edge is margin and furniture, and dividing by
            # the full sheet made every page look emptier than it was.
            usable = ph * (1 - 2 * 0.085)
            used = (max(b["bbox"][3] for b in body) - top) / usable
            # The final page of any document ends where the content ends;
            # only a short page with more pages after it is a defect.
            if used < 0.60 and pno < n - 1:
                out.append(_p("nearly_blank_page",
                              "body content fills only %.0f%% of the page "
                              "height" % (used * 100), where))

        for sp in _spans(d):
            if sp["text"].strip():
                smallest = min(smallest, round(sp.get("size", 99), 1))

        for ent in ENTITIES:
            if ent in txt:
                out.append(_p("html_entity",
                              "%r reached the page" % ent, where))
        g = GLYPH_RX.search(txt)
        if g:
            out.append(_p("corrupted_glyph",
                          "unrenderable or mis-decoded character %r"
                          % g.group(0), where))

        # table / content bounds against the real page box
        right = (page_w or pw) - (margin or 0) - 2
        for blk in d.get("blocks", []):
            bb = blk.get("bbox") or [0, 0, 0, 0]
            if bb[2] > right or bb[0] < -1 or bb[3] > ph + 1:
                out.append(_p("content_out_of_bounds",
                              "a block extends to x=%.0f on a %.0f-wide page"
                              % (bb[2], pw), where))
                break
    if smallest < 99.0 and smallest < MIN_BODY_PT - 0.05:
        out.append(_p("type_too_small",
                      "%.1fpt type found; the floor is %.1fpt"
                      % (smallest, MIN_BODY_PT)))
    joined = "\n".join(all_text)

    m = INTENT_RX.search(joined)
    if m:
        out.append(_p("institutional_intent_claim",
                      "the text claims to know why an institution traded: %r"
                      % m.group(0)))

    if core and snap:
        for rec in ((snap.get("sentiment") or {}).get("sample_records") or []):
            ex = (rec.get("excerpt") or "").strip()
            if len(ex) >= 25 and ex[:40] in joined:
                out.append(_p("raw_social_in_core",
                              "a raw post excerpt appears in the core brief: "
                              "%r" % ex[:48]))
                break
    doc.close()
    return out


def _is_furniture(blk, page_h, band=0.085):
    """Running header, footer and watermark. They appear on every page
    and say nothing about whether the page carries content."""
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


def report(view, snap, core_pdf, appendix_pdf=None):
    """The validation report that ships beside the brief."""
    sem = validate_model(view, snap)
    core = validate_pdf(core_pdf, snap, core=True)
    apx = validate_pdf(appendix_pdf, snap, core=False) if appendix_pdf else []
    problems = ([dict(p, stage="model") for p in sem]
                + [dict(p, stage="core_pdf") for p in core]
                + [dict(p, stage="appendix_pdf") for p in apx])
    return {"schema": "stock_research_brief_validation/v3",
            "ticker": view.get("ticker"),
            "report_time_utc": view.get("report_time_utc"),
            "checks_run": ["ladder_order", "ladder_vs_spot",
                           "timestamp_consistency", "stale_catalyst",
                           "exit_vs_horizon", "filing_accessions",
                           "missing_vs_negative", "calibration_language",
                           "population_arithmetic", "coordination_consistency",
                           "page_count", "blank_page", "nearly_blank_page",
                           "type_size", "html_entities", "glyph_integrity",
                           "content_bounds", "institutional_intent",
                           "raw_social_containment"],
            "problems": problems,
            "ok": not problems}
