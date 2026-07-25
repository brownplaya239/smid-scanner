#!/usr/bin/env python3
"""report_v4_validate.py — the gate the v4 package must clear to ship.

v4's promise is specific: a rating only when the event gate permits it, a
consensus and a target only when a feed sourced them, a 52-week band that
reconciles to price and EPS, a withheld datum labelled rather than blank,
and a real appendix behind the six pages. This validator turns each of
those promises into a check that BLOCKS on failure, and proves every
check bites by mutating a good package until each one fails
(report_v4_mutation).

It reuses report_v3_validate's chk() envelope and status/severity
vocabulary so a v4 result reads in the same tooling as a v3 one. The
checks themselves are v4's: the event/rating consistency and the
split-magnitude reconciliation are the two the spec calls out by name.

    report(view, snap, core_pdf, appendix_pdf, estimates) -> dict
"""

import datetime as dt
import json
import re

import report_v3_validate as V3
import report_v4_event as EV
import research_snapshot as rs

chk = V3.chk
PASS, FAIL, WARN_S, SKIP = V3.PASS, V3.FAIL, V3.WARN_S, V3.SKIP
FATAL, ERROR, WARN, INFO = V3.FATAL, V3.ERROR, V3.WARN, V3.INFO

OBSERVED = "OBSERVED"
DERIVED = "DERIVED"

# Ratios that betray a split-adjustment mismatch: a price adjusted for a
# split while its EPS was not (or the reverse) shows up as a trailing P/E
# off from price/EPS by one of these factors.
_SPLIT_FACTORS = (2, 3, 4, 5, 6, 7, 8, 10, 15, 20)
_SPLIT_TOL = 0.06                    # 6% around a clean split factor


def _fv(x):
    return rs.fv(x)


def _near(a, b, rel=0.02):
    if a is None or b is None:
        return False
    if b == 0:
        return abs(a) < 1e-9
    return abs(a - b) / abs(b) <= rel


def _looks_like_split(ratio):
    """True if `ratio` sits within tolerance of a whole split factor or its
    inverse, but is not ~1. That is the signature of price and EPS being
    adjusted on different bases."""
    if ratio is None or _near(ratio, 1.0, _SPLIT_TOL):
        return None
    for f in _SPLIT_FACTORS:
        for cand in (f, 1.0 / f):
            if abs(ratio - cand) / cand <= _SPLIT_TOL:
                return cand
    return None


# ── model-level checks ──────────────────────────────────────────────────

def check_view(view, snap=None, estimates=None):
    """The v4 invariants that live in the view, before a pixel is drawn."""
    out = []
    snap = snap or {}
    ev = view.get("event") or {}
    ratings = view.get("ratings") or {}
    fr = ratings.get("fundamental") or {}
    tr = ratings.get("tactical") or {}
    tg = ratings.get("target") or {}
    state = ev.get("state")

    # 1. No rating when the gate forbids one.
    if not ev.get("rating_allowed"):
        bad = [k for k, r in (("fundamental", fr), ("target", tg))
               if r.get("available")]
        out.append(chk("EVENT_RATING_CONSISTENCY", not bad, ERROR,
                       "no rating/target while rating_allowed is False",
                       "emitted: %s" % (", ".join(bad) or "none"),
                       detail="Event state %s does not permit a rating; the "
                              "view must withhold it." % state))
    else:
        out.append(chk("EVENT_RATING_CONSISTENCY", True, ERROR,
                       "rating permitted", "state %s" % state))

    # 2. DATA HOLD must carry the flash, so build_core renders it instead of
    #    a report.
    if state == EV.DATA_HOLD:
        out.append(chk("FLASH_ON_DATA_HOLD", bool(view.get("flash")), ERROR,
                       "flash present in DATA HOLD",
                       "present" if view.get("flash") else "MISSING"))

    # 3. Earnings are never "next" after they have been released.
    if state in (EV.RELEASED_PRE_CALL, EV.POST_CALL_UNVERIFIED,
                 EV.POST_CALL_VERIFIED):
        pending = ev.get("next_earnings_is_pending")
        out.append(chk("NO_NEXT_AFTER_RELEASE", not pending, ERROR,
                       "next-earnings not pending after release",
                       "pending=%s" % pending))

    # 4/5. A consensus or target, if shown, is a dated vendor observation —
    #      never our opinion, never undated.
    if fr.get("available"):
        ok = (fr.get("grade") == OBSERVED and fr.get("as_of")
              and fr.get("n_analysts"))
        out.append(chk("CONSENSUS_SOURCED", bool(ok), ERROR,
                       "consensus is OBSERVED, dated, analyst-counted",
                       "grade=%s as_of=%s n=%s" % (fr.get("grade"),
                       fr.get("as_of"), fr.get("n_analysts"))))
    if tg.get("available"):
        ok = (tg.get("grade") == OBSERVED and tg.get("as_of")
              and tg.get("mean") is not None)
        out.append(chk("TARGET_SOURCED", bool(ok), ERROR,
                       "target is OBSERVED, dated, has a mean",
                       "grade=%s as_of=%s mean=%s" % (tg.get("grade"),
                       tg.get("as_of"), tg.get("mean"))))

    # 6. Every withheld datum is labelled with a reason, never left blank.
    val = view.get("valuation") or {}
    fin = view.get("financials") or {}
    withheld = [("target", tg), ("forward_consensus",
                fin.get("forward_consensus") or {}),
                ("saas_kpis", fin.get("saas_kpis") or {}),
                ("peers", val.get("peers") or {}),
                ("historical_band", val.get("historical_band") or {}),
                ("scenarios", val.get("scenarios") or {}),
                ("target_bridge", val.get("target_bridge") or {}),
                ("variant", view.get("variant") or {})]
    unlabelled = [k for k, m in withheld
                  if m.get("available") is False and not m.get("reason")]
    out.append(chk("WITHHELD_LABELLED", not unlabelled, ERROR,
                   "every withheld datum carries a reason",
                   "unlabelled: %s" % (", ".join(unlabelled) or "none")))

    # 7. Split-magnitude: the trailing multiple must reconcile to price/EPS.
    price = view.get("price")
    eps = _fv((snap.get("fundamentals") or {}).get("eps_ttm"))
    pe_t = _fv(val.get("pe_trailing"))
    if price and eps and eps > 0 and pe_t:
        implied = price / eps
        ratio = pe_t / implied if implied else None
        split = _looks_like_split(ratio)
        out.append(chk("SPLIT_MAGNITUDE", split is None, ERROR,
                       "trailing P/E reconciles to price / EPS",
                       "pe=%.2f, price/eps=%.2f, ratio=%.3f%s"
                       % (pe_t, implied, ratio or 0,
                          ", ~%gx split gap" % split if split else ""),
                       detail="A trailing P/E off from price/EPS by a whole "
                              "split factor means price and EPS were adjusted "
                              "on different bases."))

    # 8. Valuation is not circular: no bull/base/bear price equals the
    #    52-week price range (which would mean it was the range divided by
    #    EPS and multiplied back — the v4.0 tautology). Scenarios are
    #    withheld on this tier, so this passes; it fails the moment a
    #    price-range-derived scenario is reintroduced.
    lv = snap.get("levels") or {}
    hi52 = _fv(lv.get("resistance_major")) or _fv(lv.get("hi52"))
    lo52 = _fv(lv.get("support_major")) or _fv(lv.get("lo52"))
    sc = val.get("forward_scenarios") or val.get("scenarios") or {}
    circular = False
    if sc.get("available") and hi52 and lo52:
        bull = ((sc.get("bull") or {}).get("price"))
        bear = ((sc.get("bear") or {}).get("price"))
        circular = _near(bull, hi52) and _near(bear, lo52)
    out.append(chk("VALUATION_NON_CIRCULAR", not circular, ERROR,
                   "no scenario price is the 52-week price range / EPS x EPS",
                   "circular price-range scenarios present" if circular
                   else "no circular scenarios",
                   detail="Dividing the 52-week price range by current EPS "
                          "makes the implied prices identical to that range."))

    # 9. The variant is our synthesis: DERIVED, never dressed as observed.
    var = view.get("variant") or {}
    if var.get("available"):
        out.append(chk("VARIANT_IS_DERIVED", var.get("grade") == DERIVED,
                       WARN, "variant grade is DERIVED",
                       "grade=%s" % var.get("grade"), warn_only=True))

    # 10a. A full report must not ship when the primary release exhibit is
    #      filed but unparsed — its guidance and operating KPIs are the read.
    #      The event gate turns this into DATA HOLD; this is the backstop.
    ex = snap.get("exhibit") or {}
    released = state in (EV.RELEASED_PRE_CALL, EV.CALL_IN_PROGRESS,
                         EV.POST_CALL_UNVERIFIED, EV.POST_CALL_VERIFIED)
    if released and not view.get("flash"):
        ok = ex.get("disposition") != "AVAILABLE_NOT_INGESTED"
        out.append(chk("PRIMARY_RELEASE_INGESTED", ok, ERROR,
                       "a full post-release report ingested the release "
                       "exhibit (or is a DATA HOLD flash)",
                       "exhibit disposition: %s"
                       % (ex.get("disposition") or "none"),
                       detail="Shipping GAAP figures while the release's "
                              "guidance and KPIs are unparsed is an "
                              "incomplete quarter dressed as complete."))

    # 10b. KPI completeness: a release that states subscription revenue is a
    #      subscription business, and its cRPO and RPO are core to the read —
    #      if we parsed one but not the others, the ingestion is partial.
    kp = (view.get("financials") or {}).get("saas_kpis") or {}
    if kp.get("available"):
        keys = {r.get("key") for r in (kp.get("rows") or [])}
        if "subscription_revenue" in keys:
            ok = "crpo" in keys and "rpo" in keys
            out.append(chk("REQUIRED_KPI_COMPLETENESS", ok, WARN,
                           "a subscription release carries cRPO and RPO",
                           "parsed: %s" % ", ".join(sorted(k for k in keys
                                                            if k)),
                           warn_only=True))

    # 10. Metric-period consistency: everything grouped as the latest quarter
    #     must share the revenue quarter's period_end. This is the check that
    #     would have caught Q1 cash flow printed beside Q2 revenue.
    fu = snap.get("fundamentals") or {}
    ref = (fu.get("revenue_q") or {}).get("period_end")
    if ref:
        bad = []
        for k in _QUARTER_FLOW:
            f = fu.get(k)
            pe = f.get("period_end") if isinstance(f, dict) else None
            if pe and pe != ref:
                bad.append("%s@%s" % (k, pe))
        out.append(chk("METRIC_PERIOD_CONSISTENCY", not bad, ERROR,
                       "latest-quarter flow metrics all end %s" % ref,
                       "mismatched: %s" % (", ".join(bad) or "none"),
                       detail="A section labelled 'latest quarter' may hold "
                              "only metrics from that quarter."))

    return out


# Flow metrics that must all belong to the same reported quarter. eps_ttm
# (trailing) and cash/debt (balance-sheet instants) are period-different by
# nature and excluded.
_QUARTER_FLOW = ("revenue_q", "net_income_q", "gross_profit",
                 "operating_cash_flow", "free_cash_flow", "capex",
                 "gross_margin", "net_margin")


# ── pdf-level checks ────────────────────────────────────────────────────

# The entities v4 uses in source, plus v3's set — any that leak into the
# rendered text unescaped are a bug. Glyph integrity reuses v3's proven
# regex, which catches the replacement char and UTF-8 mojibake alike.
_ENTITIES = tuple(sorted(set(
    ("&amp;", "&mdash;", "&ndash;", "&bull;", "&middot;", "&divide;",
     "&minus;", "&lt;", "&gt;", "&nbsp;")) | set(V3.ENTITIES)))


def _pdf_pages_text(data):
    import fitz
    d = fitz.open(stream=data, filetype="pdf")
    txt = "\n".join(d[i].get_text() for i in range(d.page_count))
    n = d.page_count
    d.close()
    return n, txt


def check_pdfs(core, appendix, view=None):
    out = []
    is_flash = bool((view or {}).get("flash"))
    try:
        n, txt = _pdf_pages_text(core)
    except Exception as e:
        return [chk("CORE_RENDERS", False, ERROR, "core is a valid PDF",
                    "open failed: %s" % e)]

    ok_n = (n == 1) if is_flash else (5 <= n <= 6)
    out.append(chk("CORE_PAGE_COUNT", ok_n, ERROR,
                   "1 flash page" if is_flash else "5 or 6 core pages", "%d" % n,
                   detail=None if is_flash else
                   "Six with valuation, five when valuation is omitted rather "
                   "than padded; never more."))

    lit = [e for e in _ENTITIES if e in txt]
    out.append(chk("HTML_ENTITIES", not lit, ERROR,
                   "no literal HTML entities in the rendered text",
                   "found: %s" % (", ".join(lit) or "none")))
    g = V3.GLYPH_RX.search(txt)
    out.append(chk("GLYPH_INTEGRITY", not g, ERROR,
                   "no unrenderable or mis-decoded character",
                   ("found %r" % g.group(0)) if g else "clean"))

    if not is_flash:
        an, _ = (0, "")
        try:
            an, atxt = _pdf_pages_text(appendix or b"")
        except Exception:
            atxt = ""
        out.append(chk("APPENDIX_PRESENT", bool(appendix) and an >= 2, ERROR,
                       "appendix present, >= 2 pages", "%d pages" % an,
                       detail="The methodology appendix is a required, "
                              "separate document."))
        if atxt:
            alit = [e for e in _ENTITIES if e in atxt]
            out.append(chk("HTML_ENTITIES_APPENDIX", not alit, ERROR,
                           "no literal HTML entities in the appendix",
                           "found: %s" % (", ".join(alit) or "none")))
        # No orphaned tail: a final appendix page carrying only a scrap of
        # a table looks unfinished. The furniture (header + footer) alone
        # extracts ~100 chars, so 300 means the page has real content.
        if appendix and an > 1:
            try:
                import fitz
                d = fitz.open(stream=appendix, filetype="pdf")
                last_len = len(d[d.page_count - 1].get_text().strip())
                d.close()
            except Exception:
                last_len = None
            if last_len is not None:
                out.append(chk("APPENDIX_TABLE_PAGINATION", last_len >= 300,
                               WARN, "final appendix page carries real "
                               "content, not an orphaned table tail",
                               "last page %d chars" % last_len,
                               warn_only=True))
    return out


# ── rendered-document checks (the meaning a reader gets) ────────────────

_DATE_RX = re.compile(r"(\d{4}-\d{2}-\d{2})")
_NEXT_RX = re.compile(r"[Nn]ext[^.\n]{0,80}?(\d{4}-\d{2}-\d{2})")
# Engineering detail that must never reach a client-facing document.
_INTERNAL_RX = re.compile(
    r"FINNHUB_API_KEY|[A-Z_]*API_KEY|os\.environ|ENV_KEY|Traceback|"
    r"not configured for this run", re.IGNORECASE)


def _as_date(s):
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def check_rendered(core_text, apx_text, view, snap):
    """Checks on the TEXT a reader actually extracts from the PDF, not the
    model objects behind it — the gap the first v4 fell through when the
    validator passed a report whose page 6 still showed a past 'next
    earnings' date."""
    out = []
    full = (core_text or "") + "\n" + (apx_text or "")
    prepared = _as_date(snap.get("report_time"))

    # No engineering detail in the client document.
    hits = sorted(set(m.group(0) for m in _INTERNAL_RX.finditer(full)))
    out.append(chk("INTERNAL_CONFIG_NOT_EXPOSED", not hits, ERROR,
                   "no internal config or engineering detail in the document",
                   "found: %s" % (", ".join(hits) or "none")))

    # No 'next' event dated before the report was generated.
    past = []
    if prepared:
        for m in _NEXT_RX.finditer(core_text or ""):
            d = _as_date(m.group(1))
            if d and d < prepared:
                past.append(m.group(1))
    out.append(chk("NO_PAST_NEXT_EVENT_RENDERED", not past, ERROR,
                   "no 'next' event earlier than the prepared date (%s)"
                   % prepared, "past next-dates: %s"
                   % (", ".join(sorted(set(past))) or "none"),
                   detail="A 'next' event in the past means the event state "
                          "was not advanced after the release."))

    # The text roundtrips cleanly: real content, no mojibake.
    body = (core_text or "").strip()
    g = V3.GLYPH_RX.search(full)
    out.append(chk("PDF_TEXT_ROUNDTRIP", len(body) > 800 and not g, ERROR,
                   "core text extracts cleanly with real content",
                   "len=%d%s" % (len(body),
                                 ", mojibake %r" % g.group(0) if g else "")))
    return out


# ── the whole report ────────────────────────────────────────────────────

def report(view, snap, core_pdf, appendix_pdf=None, estimates=None,
           run_mutation=True):
    checks = []
    checks += check_view(view, snap, estimates)
    checks += check_pdfs(core_pdf, appendix_pdf, view)
    if not view.get("flash"):
        try:
            _, ctext = _pdf_pages_text(core_pdf)
            _, atext = (_pdf_pages_text(appendix_pdf) if appendix_pdf
                        else (0, ""))
            checks += check_rendered(ctext, atext, view, snap)
        except Exception as e:                       # pragma: no cover
            checks.append(chk("RENDERED_CHECKS_RAN", False, ERROR,
                              "rendered-text checks ran", "error: %s" % e))

    if run_mutation:
        try:
            import report_v4_mutation as MUT
            mutation = MUT.summary()
        except Exception as e:                       # pragma: no cover
            mutation = {"all_checks_proven": False, "error": str(e)}
    else:
        mutation = {"all_checks_proven": None, "note": "not run"}

    fatal = [c for c in checks if c["status"] == FAIL]
    return {
        "schema": "equity-research-v4-validation/1",
        "validator_code_sha256": V3.validator_code_hash(),
        "ticker": view.get("ticker"),
        "event_state": (view.get("event") or {}).get("state"),
        "checks": checks,
        "blocking_failures": [c["check_id"] for c in fatal],
        "mutation_tests": mutation,
        "ok": bool(not fatal and (mutation.get("all_checks_proven")
                                  is not False)),
    }


def validate_paths(core_pdf_path, appendix_pdf_path, view, snap,
                   estimates=None, out_path=None):
    """Convenience: validate PDFs already on disk and optionally write the
    JSON. The runner passes bytes directly via report()."""
    core = open(core_pdf_path, "rb").read()
    apx = (open(appendix_pdf_path, "rb").read() if appendix_pdf_path
           else None)
    res = report(view, snap, core, apx, estimates)
    if out_path:
        with open(out_path, "w") as fh:
            json.dump(res, fh, indent=1, default=str, sort_keys=True)
    return res
