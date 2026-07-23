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

import json

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
    if state in (EV.RESULTS_RELEASED, EV.POST_CALL_VERIFIED):
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

    # 8. The band and its scenarios reconcile to the same inputs.
    band = val.get("historical_band") or {}
    if band.get("available"):
        pn = band.get("pe_now")
        implied = (price / eps) if (price and eps and eps > 0) else None
        out.append(chk("BAND_RECONCILES", _near(pn, implied, 0.03), ERROR,
                       "band pe_now == price / EPS",
                       "pe_now=%s price/eps=%s" % (pn, round(implied, 2)
                       if implied else None)))
        sc = val.get("scenarios") or {}
        if sc.get("available"):
            ok = (_near(sc["bull"]["price"], band.get("hi52"))
                  and _near(sc["bear"]["price"], band.get("lo52")))
            out.append(chk("SCENARIOS_ARE_THE_RANGE", ok, ERROR,
                           "bull/bear prices are the 52-week range",
                           "bull=%s hi52=%s bear=%s lo52=%s"
                           % (sc["bull"]["price"], band.get("hi52"),
                              sc["bear"]["price"], band.get("lo52"))))

    # 9. The variant is our synthesis: DERIVED, never dressed as observed.
    var = view.get("variant") or {}
    if var.get("available"):
        out.append(chk("VARIANT_IS_DERIVED", var.get("grade") == DERIVED,
                       WARN, "variant grade is DERIVED",
                       "grade=%s" % var.get("grade"), warn_only=True))

    return out


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

    want = 1 if is_flash else 6
    out.append(chk("CORE_PAGE_COUNT", n == want, ERROR,
                   "%d core pages" % want, "%d" % n,
                   detail="Flash is one page; the full report is exactly six."
                   if not is_flash else None))

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
    return out


# ── the whole report ────────────────────────────────────────────────────

def report(view, snap, core_pdf, appendix_pdf=None, estimates=None,
           run_mutation=True):
    checks = []
    checks += check_view(view, snap, estimates)
    checks += check_pdfs(core_pdf, appendix_pdf, view)

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
