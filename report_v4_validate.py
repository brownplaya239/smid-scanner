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
_SPLIT_TOL = 0.06  # 6% around a clean split factor


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
        bad = [k for k, r in (("fundamental", fr), ("target", tg)) if r.get("available")]
        out.append(
            chk(
                "EVENT_RATING_CONSISTENCY",
                not bad,
                ERROR,
                "no rating/target while rating_allowed is False",
                "emitted: %s" % (", ".join(bad) or "none"),
                detail="Event state %s does not permit a rating; the "
                "view must withhold it." % state,
            )
        )
    else:
        out.append(
            chk("EVENT_RATING_CONSISTENCY", True, ERROR, "rating permitted", "state %s" % state)
        )

    # 2. DATA HOLD must carry the flash, so build_core renders it instead of
    #    a report.
    if state == EV.DATA_HOLD:
        out.append(
            chk(
                "FLASH_ON_DATA_HOLD",
                bool(view.get("flash")),
                ERROR,
                "flash present in DATA HOLD",
                "present" if view.get("flash") else "MISSING",
            )
        )

    # 3. Earnings are never "next" after they have been released.
    if state in (EV.RELEASED_PRE_CALL, EV.POST_CALL_UNVERIFIED, EV.POST_CALL_VERIFIED):
        pending = ev.get("next_earnings_is_pending")
        out.append(
            chk(
                "NO_NEXT_AFTER_RELEASE",
                not pending,
                ERROR,
                "next-earnings not pending after release",
                "pending=%s" % pending,
            )
        )

    # 4/5. A consensus or target, if shown, is a dated vendor observation —
    #      never our opinion, never undated.
    if fr.get("available"):
        ok = fr.get("grade") == OBSERVED and fr.get("as_of") and fr.get("n_analysts")
        out.append(
            chk(
                "CONSENSUS_SOURCED",
                bool(ok),
                ERROR,
                "consensus is OBSERVED, dated, analyst-counted",
                "grade=%s as_of=%s n=%s" % (fr.get("grade"), fr.get("as_of"), fr.get("n_analysts")),
            )
        )
    if tg.get("available"):
        ok = tg.get("grade") == OBSERVED and tg.get("as_of") and tg.get("mean") is not None
        out.append(
            chk(
                "TARGET_SOURCED",
                bool(ok),
                ERROR,
                "target is OBSERVED, dated, has a mean",
                "grade=%s as_of=%s mean=%s" % (tg.get("grade"), tg.get("as_of"), tg.get("mean")),
            )
        )

    # 6. Every withheld datum is labelled with a reason, never left blank.
    val = view.get("valuation") or {}
    fin = view.get("financials") or {}
    withheld = [
        ("target", tg),
        ("forward_consensus", fin.get("forward_consensus") or {}),
        ("saas_kpis", fin.get("saas_kpis") or {}),
        ("peers", val.get("peers") or {}),
        ("historical_band", val.get("historical_band") or {}),
        ("scenarios", val.get("scenarios") or {}),
        ("target_bridge", val.get("target_bridge") or {}),
        ("variant", view.get("variant") or {}),
    ]
    unlabelled = [k for k, m in withheld if m.get("available") is False and not m.get("reason")]
    out.append(
        chk(
            "WITHHELD_LABELLED",
            not unlabelled,
            ERROR,
            "every withheld datum carries a reason",
            "unlabelled: %s" % (", ".join(unlabelled) or "none"),
        )
    )

    # 7. Split-magnitude: the trailing multiple must reconcile to price/EPS.
    price = view.get("price")
    eps = _fv((snap.get("fundamentals") or {}).get("eps_ttm"))
    pe_t = _fv(val.get("pe_trailing"))
    if price and eps and eps > 0 and pe_t:
        implied = price / eps
        ratio = pe_t / implied if implied else None
        split = _looks_like_split(ratio)
        out.append(
            chk(
                "SPLIT_MAGNITUDE",
                split is None,
                ERROR,
                "trailing P/E reconciles to price / EPS",
                "pe=%.2f, price/eps=%.2f, ratio=%.3f%s"
                % (pe_t, implied, ratio or 0, ", ~%gx split gap" % split if split else ""),
                detail="A trailing P/E off from price/EPS by a whole "
                "split factor means price and EPS were adjusted "
                "on different bases.",
            )
        )

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
        bull = (sc.get("bull") or {}).get("price")
        bear = (sc.get("bear") or {}).get("price")
        circular = _near(bull, hi52) and _near(bear, lo52)
    out.append(
        chk(
            "VALUATION_NON_CIRCULAR",
            not circular,
            ERROR,
            "no scenario price is the 52-week price range / EPS x EPS",
            "circular price-range scenarios present" if circular else "no circular scenarios",
            detail="Dividing the 52-week price range by current EPS "
            "makes the implied prices identical to that range.",
        )
    )

    # 9. The variant is our synthesis: DERIVED, never dressed as observed.
    var = view.get("variant") or {}
    if var.get("available"):
        out.append(
            chk(
                "VARIANT_IS_DERIVED",
                var.get("grade") == DERIVED,
                WARN,
                "variant grade is DERIVED",
                "grade=%s" % var.get("grade"),
                warn_only=True,
            )
        )

    # 10a. A full report must not ship when the primary release exhibit is
    #      filed but unparsed — its guidance and operating KPIs are the read.
    #      The event gate turns this into DATA HOLD; this is the backstop.
    ex = snap.get("exhibit") or {}
    released = state in (
        EV.RELEASED_PRE_CALL,
        EV.CALL_IN_PROGRESS,
        EV.POST_CALL_UNVERIFIED,
        EV.POST_CALL_VERIFIED,
    )
    if released and not view.get("flash"):
        # Mirror the gate's freshness window: inside HOLD_WINDOW_DAYS an
        # unparsed exhibit must have produced a DATA HOLD (so reaching
        # here un-ingested is a real failure); beyond it the gate
        # deliberately proceeds with the gap labelled, and this backstop
        # must not re-block what the gate chose to allow.
        age = EV._release_age_days(ex, dt.datetime.now(dt.UTC))
        fresh = age is not None and age <= EV.HOLD_WINDOW_DAYS
        ok = ex.get("disposition") != "AVAILABLE_NOT_INGESTED" or not fresh
        out.append(
            chk(
                "PRIMARY_RELEASE_INGESTED",
                ok,
                ERROR,
                "a fresh post-release report ingested the release "
                "exhibit (or held); a stale unparsed exhibit is a "
                "labelled gap",
                "exhibit disposition: %s, release age: %s day(s)"
                % (ex.get("disposition") or "none", "unknown" if age is None else age),
                detail="Shipping GAAP figures while a FRESH "
                "release's guidance and KPIs are unparsed "
                "is an incomplete quarter dressed as "
                "complete.",
            )
        )

    # 10b. KPI completeness: a release that states subscription revenue is a
    #      subscription business, and its cRPO and RPO are core to the read —
    #      if we parsed one but not the others, the ingestion is partial.
    kp = (view.get("financials") or {}).get("saas_kpis") or {}
    if kp.get("available"):
        keys = {r.get("key") for r in (kp.get("rows") or [])}
        if "subscription_revenue" in keys:
            ok = "crpo" in keys and "rpo" in keys
            out.append(
                chk(
                    "REQUIRED_KPI_COMPLETENESS",
                    ok,
                    WARN,
                    "a subscription release carries cRPO and RPO",
                    "parsed: %s" % ", ".join(sorted(k for k in keys if k)),
                    warn_only=True,
                )
            )

    # 10. Metric-period consistency: everything grouped as the latest quarter
    #     must share the revenue quarter's period_end. This is the check that
    #     would have caught Q1 cash flow printed beside Q2 revenue.
    fu = snap.get("fundamentals") or {}
    _rev = fu.get("revenue_q") or {}
    # Anchor only on a revenue quarter that actually carries a value, and
    # judge only metrics that do: a value-less fact renders "n/a" and
    # asserts no period, so its stray period_end (HOOD's retired 2021
    # revenue concept) must not fail a table that never printed it.
    ref = _rev.get("period_end") if rs.fv(_rev) is not None else None
    if ref:
        bad = []
        for k in _QUARTER_FLOW:
            f = fu.get(k)
            pe = f.get("period_end") if isinstance(f, dict) and rs.fv(f) is not None else None
            if pe and pe != ref:
                bad.append("%s@%s" % (k, pe))
        out.append(
            chk(
                "METRIC_PERIOD_CONSISTENCY",
                not bad,
                ERROR,
                "latest-quarter flow metrics all end %s" % ref,
                "mismatched: %s" % (", ".join(bad) or "none"),
                detail="A section labelled 'latest quarter' may hold "
                "only metrics from that quarter.",
            )
        )

    # 11. Listing-date integrity. Ticker symbols are recycled: bars dated
    #     before a security's listing date belong to a DIFFERENT company,
    #     and splicing them on silently corrupts the 200-day average, the
    #     52-week range and RSI while every other check still passes. This
    #     is the check that would have caught SPCX carrying 435 sessions of
    #     a prior issuer's $22 tape under a $161 IPO.
    th = view.get("trading_history") or {}
    ld = th.get("listing_date")
    lv = snap.get("levels") or {}
    if ld:
        first = th.get("first_session")
        clean = (not first) or str(first)[:10] >= str(ld)[:10]
        out.append(
            chk(
                "LISTING_DATE_INTEGRITY",
                clean,
                ERROR,
                "no bar predates the %s listing" % ld,
                "earliest session %s" % (first or "not recorded"),
                detail="Symbols are reused. A bar dated before the "
                "listing date belongs to a different security "
                "and must be dropped, not averaged in.",
            )
        )
        # A short history must be DECLARED, not inferred by the reader from
        # a level table with holes in it.
        if not th.get("full_history"):
            out.append(
                chk(
                    "SHORT_HISTORY_DECLARED",
                    bool(th.get("sessions")),
                    ERROR,
                    "session count published for a short history",
                    "sessions=%s" % th.get("sessions"),
                    detail="With under a year of trading the report "
                    "must state the history length, because "
                    "the 200-day average and 52-week range "
                    "do not exist.",
                )
            )

    return out


# Flow metrics that must all belong to the same reported quarter. eps_ttm
# (trailing) and cash/debt (balance-sheet instants) are period-different by
# nature and excluded.
_QUARTER_FLOW = (
    "revenue_q",
    "net_income_q",
    "gross_profit",
    "operating_cash_flow",
    "free_cash_flow",
    "capex",
    "gross_margin",
    "net_margin",
)


# ── pdf-level checks ────────────────────────────────────────────────────

# The entities v4 uses in source, plus v3's set — any that leak into the
# rendered text unescaped are a bug. Glyph integrity reuses v3's proven
# regex, which catches the replacement char and UTF-8 mojibake alike.
_ENTITIES = tuple(
    sorted(
        {
            "&amp;",
            "&mdash;",
            "&ndash;",
            "&bull;",
            "&middot;",
            "&divide;",
            "&minus;",
            "&lt;",
            "&gt;",
            "&nbsp;",
        }
        | set(V3.ENTITIES)
    )
)


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
        return [chk("CORE_RENDERS", False, ERROR, "core is a valid PDF", "open failed: %s" % e)]

    # The analyst take, when it ships, is one dedicated page.
    extra = 1 if (not is_flash and ((view or {}).get("analyst_take") or {}).get("available")) else 0
    ok_n = (n == 1) if is_flash else (5 + extra <= n <= 6 + extra)
    out.append(
        chk(
            "CORE_PAGE_COUNT",
            ok_n,
            ERROR,
            "1 flash page" if is_flash else "%d or %d core pages" % (5 + extra, 6 + extra),
            "%d" % n,
            detail=None
            if is_flash
            else "Six with valuation, five when valuation is omitted rather "
            "than padded, plus one page when the analyst take ships; never more.",
        )
    )

    lit = [e for e in _ENTITIES if e in txt]
    out.append(
        chk(
            "HTML_ENTITIES",
            not lit,
            ERROR,
            "no literal HTML entities in the rendered text",
            "found: %s" % (", ".join(lit) or "none"),
        )
    )
    g = V3.GLYPH_RX.search(txt)
    out.append(
        chk(
            "GLYPH_INTEGRITY",
            not g,
            ERROR,
            "no unrenderable or mis-decoded character",
            ("found %r" % g.group(0)) if g else "clean",
        )
    )

    if not is_flash:
        an, _ = (0, "")
        try:
            an, atxt = _pdf_pages_text(appendix or b"")
        except Exception:
            atxt = ""
        out.append(
            chk(
                "APPENDIX_PRESENT",
                bool(appendix) and an >= 2,
                ERROR,
                "appendix present, >= 2 pages",
                "%d pages" % an,
                detail="The methodology appendix is a required, separate document.",
            )
        )
        if atxt:
            alit = [e for e in _ENTITIES if e in atxt]
            out.append(
                chk(
                    "HTML_ENTITIES_APPENDIX",
                    not alit,
                    ERROR,
                    "no literal HTML entities in the appendix",
                    "found: %s" % (", ".join(alit) or "none"),
                )
            )
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
                out.append(
                    chk(
                        "APPENDIX_TABLE_PAGINATION",
                        last_len >= 300,
                        WARN,
                        "final appendix page carries real content, not an orphaned table tail",
                        "last page %d chars" % last_len,
                        warn_only=True,
                    )
                )
    return out


# ── rendered-document checks (the meaning a reader gets) ────────────────

_DATE_RX = re.compile(r"(\d{4}-\d{2}-\d{2})")
_NEXT_RX = re.compile(r"[Nn]ext[^.\n]{0,80}?(\d{4}-\d{2}-\d{2})")
# Engineering detail that must never reach a client-facing document.
_INTERNAL_RX = re.compile(
    r"FINNHUB_API_KEY|[A-Z_]*API_KEY|os\.environ|ENV_KEY|Traceback|"
    r"not configured for this run",
    re.IGNORECASE,
)


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
    hits = sorted({m.group(0) for m in _INTERNAL_RX.finditer(full)})
    out.append(
        chk(
            "INTERNAL_CONFIG_NOT_EXPOSED",
            not hits,
            ERROR,
            "no internal config or engineering detail in the document",
            "found: %s" % (", ".join(hits) or "none"),
        )
    )

    # No 'next' event dated before the report was generated.
    past = []
    if prepared:
        for m in _NEXT_RX.finditer(core_text or ""):
            d = _as_date(m.group(1))
            if d and d < prepared:
                past.append(m.group(1))
    out.append(
        chk(
            "NO_PAST_NEXT_EVENT_RENDERED",
            not past,
            ERROR,
            "no 'next' event earlier than the prepared date (%s)" % prepared,
            "past next-dates: %s" % (", ".join(sorted(set(past))) or "none"),
            detail="A 'next' event in the past means the event state "
            "was not advanced after the release.",
        )
    )

    # The text roundtrips cleanly: real content, no mojibake.
    body = (core_text or "").strip()
    g = V3.GLYPH_RX.search(full)
    out.append(
        chk(
            "PDF_TEXT_ROUNDTRIP",
            len(body) > 800 and not g,
            ERROR,
            "core text extracts cleanly with real content",
            "len=%d%s" % (len(body), ", mojibake %r" % g.group(0) if g else ""),
        )
    )
    return out


# ── the AI-written analyst take ─────────────────────────────────────────

ANALYST_CHECKS = (
    "ANALYST_FACT_IDS",
    "ANALYST_NO_FREE_NUMBERS",
    "ANALYST_NO_FREE_DIRECTION",
    "ANALYST_NO_ADVICE",
    "ANALYST_NO_FOREIGN_TICKERS",
    "ANALYST_LENGTH_CAPS",
    "ANALYST_RENDER_MATCHES_DATA",
    "ANALYST_JUDGE_PASSED",
)
_EXPECT = {
    "ANALYST_FACT_IDS": "every point cites facts that exist, and its placeholders use only "
    "those facts, in forms they allow",
    "ANALYST_NO_FREE_NUMBERS": "no digit, number word, fraction, size or bound word, unit, "
    "date or month outside placeholders",
    "ANALYST_NO_FREE_DIRECTION": "no direction, movement, comparison, superlative or judgement "
    "word outside placeholders",
    "ANALYST_NO_ADVICE": "no advice, rating, target or position wording",
    "ANALYST_NO_FOREIGN_TICKERS": "no ticker outside this report's data",
    "ANALYST_LENGTH_CAPS": "2-3 sentence summary, 1-3 points per list, every part within its cap",
    "ANALYST_RENDER_MATCHES_DATA": "every printed value, unit and direction word equals what "
    "the facts rebuilt from this view render to",
    "ANALYST_JUDGE_PASSED": "the judge's raw answer holds exactly one verdict per point, "
    "no other ids, and every verdict is supported",
}


def _squash(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _nows(s):
    """Whitespace-free form for comparing page text: PDF extraction wraps
    lines wherever the layout does, so only the characters are compared."""
    return re.sub(r"\s+", "", s or "")


def _pdf_page_texts(data):
    import fitz

    d = fitz.open(stream=data, filetype="pdf")
    out = [d[i].get_text() for i in range(d.page_count)]
    d.close()
    return out


_FURNITURE_FOOTER = "Educational research, not investment advice."
_DEMO_LINES = ("DEMO DATA", "SYNTHETIC — NOT RESEARCH")


def _page_body(page_text, page_no, ticker):
    """The page's text with the running header and footer removed, or None
    when the furniture is not exactly what report_v3._Doc draws."""
    lines = (page_text or "").split("\n")
    while lines and lines[0].strip() in _DEMO_LINES:
        lines.pop(0)
    if len(lines) < 4:
        return None
    head, prep, foot, num = (ln.strip() for ln in lines[:4])
    if not (
        head == "%s — Equity Research v4" % (ticker or "")
        and prep.startswith("Prepared ")
        and foot == _FURNITURE_FOOTER
        and num == "Page %d" % page_no
    ):
        return None
    return "\n".join(lines[4:])


def _expected_body(at):
    import report_v4_analyst as AT

    out = []
    for style, text, ids in AT.page_blocks(at):
        out.append(("• " if style == "point" else "") + text)
        if ids is not None:
            out.append(AT.cite(ids))
    return " ".join(out)


def rendered_take_problems(at, core_pages, ticker):
    """Why the printed core does not show exactly the analyst take ([] =
    it does). Available: exactly one page carries the label, and that
    page's whole text (furniture aside) equals the expected rendering,
    character for character; no unavailable note anywhere. Withheld: the
    unavailable note appears exactly once and nothing of the analyst page
    appears anywhere."""
    import report_v4_analyst as AT

    probs = []
    pages = [_squash(p) for p in core_pages]
    whole = " ".join(pages)
    n_note = whole.count(_squash(AT.UNAVAILABLE_NOTE))
    marks = [_squash(AT.LABEL_MARK), _squash(AT.FOOTNOTE[:60])] + [t for _, t in AT.SECTION_TITLES]
    if at.get("available"):
        if n_note:
            probs.append("the unavailable note is printed although the take shipped")
        label_pages = [i for i, p in enumerate(pages) if marks[0] in p]
        if len(label_pages) != 1:
            probs.append("the analyst label is on %d pages, not exactly 1" % len(label_pages))
            return probs
        i = label_pages[0]
        body = _page_body(core_pages[i], i + 1, ticker)
        if body is None:
            probs.append("page %d: running header/footer not as drawn" % (i + 1))
        else:
            got, exp = _nows(body), _nows(_expected_body(at))
            if got != exp:
                k = next(
                    (j for j in range(min(len(got), len(exp))) if got[j] != exp[j]),
                    min(len(got), len(exp)),
                )
                probs.append(
                    "page %d text differs from the validated take at character %d "
                    "(printed %r, expected %r; %d vs %d characters)"
                    % (i + 1, k, got[k : k + 40], exp[k : k + 40], len(got), len(exp))
                )
        others = [j + 1 for j, p in enumerate(pages) if j != i and any(m in p for m in marks)]
        if others:
            probs.append("analyst-page text also appears on page(s) %s" % others)
    else:
        if n_note != 1:
            probs.append("the unavailable note appears %d times, not exactly once" % n_note)
        found = [m for m in marks if m in whole]
        if found:
            probs.append(
                "analyst-page text is printed although the take was withheld: %s" % "; ".join(found)
            )
    return probs


def check_analyst_take(view, snap=None, core_pages=None):
    """The gate's own pass over the AI section. The facts are REBUILT here
    from the view, independently of the generator; the stored templates
    are re-checked, every token is re-rendered and compared with the
    shipped text, and the judge's record must clear every point. With
    core_pages (one string per PDF page), the printed page is held to the
    exact expected text, or to the one-line note when the take was
    withheld."""
    import report_v4_analyst as AT

    out = []
    at = view.get("analyst_take")
    if at is None or view.get("flash"):
        return out
    if not at.get("available"):
        for cid in ANALYST_CHECKS:
            out.append(
                V3.skipped(
                    cid, ERROR, "analyst take withheld: %s" % (at.get("meta") or {}).get("status")
                )
            )
    else:
        base = {k: v for k, v in view.items() if k != "analyst_take"}
        facts = AT.fact_list(base, snap)
        by_id = {f["fact_id"]: f for f in facts}
        probs = AT.take_problems(at, facts, view.get("ticker"))
        # Re-render every part from the rebuilt facts; the shipped text must
        # be exactly that (a flipped sign, a reversed direction word or a
        # swapped unit in the stored text all fail here).
        for where, p in AT.parts(at):
            try:
                want = AT.render(str(p.get("template") or ""), by_id)
            except Exception as e:
                probs.append(("ANALYST_RENDER_MATCHES_DATA", "%s does not render: %s" % (where, e)))
                continue
            if p.get("text") != want:
                probs.append(
                    (
                        "ANALYST_RENDER_MATCHES_DATA",
                        "%s prints %r; the data renders %r" % (where, p.get("text"), want),
                    )
                )
        probs += [("ANALYST_JUDGE_PASSED", m) for m in AT.judge_record_problems(at)]
        refs = sorted({i for _, p in AT.parts(at) for i in p.get("fact_ids") or []})
        for cid in ANALYST_CHECKS:
            mine = [m for c, m in probs if c == cid]
            out.append(
                chk(cid, not mine, ERROR, _EXPECT[cid], "; ".join(mine[:5]) or "clean", refs=refs)
            )

    if core_pages is not None:
        probs = rendered_take_problems(at, core_pages, view.get("ticker"))
        out.append(
            chk(
                "ANALYST_TAKE_RENDERED",
                not probs,
                ERROR,
                "the analyst page shows exactly the validated take (label, text, fact names "
                "and footnote, nothing else), or the core shows only the 'unavailable' note",
                "; ".join(probs[:3]) or "exact match",
            )
        )
    return out


def analyst_facts_record(view, snap=None):
    """The facts the shipped take cites, with their sources, for the
    validation record (the page footnote points here)."""
    import report_v4_analyst as AT

    at = view.get("analyst_take") or {}
    if not at.get("available"):
        return None
    base = {k: v for k, v in view.items() if k != "analyst_take"}
    cited = {i for _, p in AT.parts(at) for i in p.get("fact_ids") or []}
    return [f for f in AT.fact_list(base, snap) if f["fact_id"] in cited]


# ── the whole report ────────────────────────────────────────────────────


def report(view, snap, core_pdf, appendix_pdf=None, estimates=None, run_mutation=True):
    checks = []
    checks += check_view(view, snap, estimates)
    checks += check_pdfs(core_pdf, appendix_pdf, view)
    if not view.get("flash"):
        try:
            _, ctext = _pdf_pages_text(core_pdf)
            _, atext = _pdf_pages_text(appendix_pdf) if appendix_pdf else (0, "")
            checks += check_rendered(ctext, atext, view, snap)
            checks += check_analyst_take(view, snap, _pdf_page_texts(core_pdf))
        except Exception as e:  # pragma: no cover
            checks.append(
                chk(
                    "RENDERED_CHECKS_RAN", False, ERROR, "rendered-text checks ran", "error: %s" % e
                )
            )

    if run_mutation:
        try:
            import report_v4_mutation as MUT

            mutation = MUT.summary()
        except Exception as e:  # pragma: no cover
            mutation = {"all_checks_proven": False, "error": str(e)}
    else:
        mutation = {"all_checks_proven": None, "note": "not run"}

    fatal = [c for c in checks if c["status"] == FAIL]
    import datetime as _dt

    return {
        "schema": "equity-research-v4-validation/1",
        "validator_code_sha256": V3.validator_code_hash(),
        # Stamped so a reader can tell at a glance whether this record was
        # produced by the same run as the PDFs beside it. `artifacts`,
        # filled in by the runner after both PDFs are written, is the
        # authoritative binding — a timestamp only narrates.
        "generated_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "ticker": view.get("ticker"),
        "event_state": (view.get("event") or {}).get("state"),
        "checks": checks,
        "blocking_failures": [c["check_id"] for c in fatal],
        "mutation_tests": mutation,
        # Status, calls, tokens, estimated cost and the judge's verdicts of
        # the AI section (None when the run did not attempt one).
        "analyst_take": (view.get("analyst_take") or {}).get("meta"),
        # The facts the shipped take cites, each with its source path.
        "analyst_facts": analyst_facts_record(view, snap),
        "ok": bool(not fatal and (mutation.get("all_checks_proven") is not False)),
    }


def validate_paths(core_pdf_path, appendix_pdf_path, view, snap, estimates=None, out_path=None):
    """Convenience: validate PDFs already on disk and optionally write the
    JSON. The runner passes bytes directly via report()."""
    core = open(core_pdf_path, "rb").read()
    apx = open(appendix_pdf_path, "rb").read() if appendix_pdf_path else None
    res = report(view, snap, core, apx, estimates)
    if out_path:
        with open(out_path, "w") as fh:
            json.dump(res, fh, indent=1, default=str, sort_keys=True)
    return res
