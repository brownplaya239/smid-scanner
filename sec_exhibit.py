#!/usr/bin/env python3
"""sec_exhibit.py — read the earnings press release, not just the XBRL.

Guidance and non-GAAP figures are never XBRL-tagged. They live in the
EX-99.1 exhibit attached to the Item 2.02 8-K, which is public, stable
and addressable — so reporting them as "unavailable" was a statement
about our parser, not about the evidence.

The document is a minefield and the traps are all silent:

  * Guidance appears BEFORE the reported figures in document order. A
    first-match regex for "Non-GAAP gross margin" returns next quarter's
    guide (58.25%-59.25%) instead of the quarter just reported (58.9%).
  * The guided GAAP gross-margin low end can be byte-identical to the
    reported GAAP gross margin, so the wrong match looks right.
  * "+/-" is entity-encoded as "+&#47;-", so a regex for `\\+/-` over raw
    HTML matches nothing at all.
  * Numbers and their "%" sign sit in separate <td> cells, every value
    carries a trailing &#160;, and empty colspan cells pad between
    columns — so positional indexing breaks.
  * Three columns run side by side: current quarter, prior quarter,
    year-ago. "The last number on the line" is the year-ago figure.
  * Labels carry footnote markers, "(a)", and negatives are shown in
    parentheses. An em dash means zero, not missing.

So: segment the document by its section headings first, walk <tr> rows
rather than regexing flat text, assert the column header date matches the
period we think we are reading, and check the published arithmetic closes
before emitting anything. If a guard fails we return nothing and say why,
because a plausible wrong number is worse than an admitted gap.
"""

import html
import re

DISPOSITION_OK = "ADMITTED"
DISPOSITION_BLOCKED = "AVAILABLE_NOT_INGESTED"

RESULTS_ITEM = "2.02"
TAG_RX = re.compile(r"<[^>]+>")
ROW_RX = re.compile(r"<tr\b.*?</tr>", re.I | re.S)
CELL_RX = re.compile(r"<t[dh]\b.*?</t[dh]>", re.I | re.S)
HDR_RX = re.compile(r"<TYPE>([^\s<]+)\s*<SEQUENCE>\d+\s*<FILENAME>([^\s<]+)",
                    re.I)
DATE_RX = re.compile(r"(January|February|March|April|May|June|July|August|"
                     r"September|October|November|December)\s*(\d{1,2}),?\s*"
                     r"(\d{4})", re.I)


def _text(fragment):
    """Cell text with entities resolved and layout artefacts removed."""
    s = TAG_RX.sub(" ", fragment)
    s = html.unescape(html.unescape(s))
    return re.sub(r"\s+", " ", s.replace(" ", " ")).strip()


def rows(html_text):
    """Every table row as a list of non-empty cell strings."""
    out = []
    for r in ROW_RX.findall(html_text):
        cells = [_text(c) for c in CELL_RX.findall(r)]
        cells = [c for c in cells if c not in ("", "%", "$")]
        if cells:
            out.append(cells)
    return out


def _num(s):
    """A published figure. Parentheses mean negative, an em dash means
    zero, and a tilde means the issuer said 'approximately'."""
    if s is None:
        return None
    t = str(s).strip().replace(",", "").replace("~", "").replace("$", "")
    t = t.replace("—", "0").replace("–", "0").rstrip("%").strip()
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def _label(s):
    return re.sub(r"\s*\([a-z]\)\s*$", "", str(s or "")).strip().lower()


# ── locating the exhibit ────────────────────────────────────────────────

def find_results_8k(acc):
    """The most recent 8-K carrying Item 2.02, from an acceptance map."""
    best = None
    for accn, meta in (acc or {}).items():
        if (meta.get("form") or "").upper() != "8-K":
            continue
        if RESULTS_ITEM not in str(meta.get("items") or ""):
            continue
        if best is None or (meta.get("accepted") or "") > (best[1].get(
                "accepted") or ""):
            best = (accn, meta)
    return best


def exhibit_url(cik, accession, fetch_text):
    """Walk from the filing to its EX-99.1.

    `acc[...]["url"]` points at the 8-K cover page, which contains none
    of the numbers — it only says an exhibit is being furnished. The
    index-headers file is the one place EDGAR exposes document TYPEs;
    index.json carries only icon names."""
    bare = str(accession).replace("-", "")
    base = ("https://www.sec.gov/Archives/edgar/data/%s/%s"
            % (int(cik), bare))
    try:
        hdr = fetch_text("%s/%s-index-headers.html" % (base, accession))
    except Exception as e:
        return None, "index-headers unreadable: %s" % e
    for typ, fn in HDR_RX.findall(html.unescape(hdr)):
        if typ.upper().startswith("EX-99"):
            return "%s/%s" % (base, fn), None
    return None, "no EX-99 exhibit listed in the filing index"


# ── segmentation ────────────────────────────────────────────────────────

OUTLOOK_RX = re.compile(r"Outlook\s+for\s+the\b|Financial\s+Outlook\b", re.I)
RECON_RX = re.compile(r"Reconciliations?\s+from\s+GAAP\s+to\s+Non-GAAP", re.I)


def segment(doc):
    """Split into (reported, guidance). Everything from the LAST
    'Outlook for the' heading onward is guidance; the reconciliation that
    precedes it is the quarter just reported. Getting this wrong is the
    single most dangerous failure mode in this document."""
    outlooks = [m.start() for m in OUTLOOK_RX.finditer(doc)]
    recons = [m.start() for m in RECON_RX.finditer(doc)]
    if not recons:
        return None, None, "no GAAP-to-non-GAAP reconciliation found"
    rec_start = recons[0]
    tail = [o for o in outlooks if o > rec_start]
    if tail:
        return doc[rec_start:tail[0]], doc[tail[0]:], None
    return doc[rec_start:], None, "no outlook section found"


def column_period(section):
    """The date at the head of the leftmost data column. Asserting this
    is the highest-value guard in the whole parser: it turns a silent
    column mis-map into a loud failure."""
    for r in rows(section)[:6]:
        for c in r:
            m = DATE_RX.search(c)
            if m:
                return "%s %s, %s" % (m.group(1).title(), m.group(2),
                                      m.group(3))
    return None


# ── extraction ──────────────────────────────────────────────────────────

REPORTED_WANT = {
    "non-gaap gross margin": ("non_gaap_gross_margin", "%"),
    "gaap gross margin": ("gaap_gross_margin", "%"),
    "non-gaap operating margin": ("non_gaap_operating_margin", "%"),
    "gaap operating margin": ("gaap_operating_margin", "%"),
    "non-gaap diluted net income per share": ("non_gaap_eps", "USD/share"),
    "gaap diluted net income per share": ("gaap_eps", "USD/share"),
    "non-gaap net income": ("non_gaap_net_income", "USD_M"),
    "gaap net income": ("gaap_net_income", "USD_M"),
}

GUIDANCE_WANT = {
    "gaap net revenue": ("revenue", "USD_M"),
    "non-gaap gross margin": ("non_gaap_gross_margin", "%"),
    "gaap gross margin": ("gaap_gross_margin", "%"),
    "non-gaap diluted net income per share": ("non_gaap_eps", "USD/share"),
    "gaap diluted net income per share": ("gaap_eps", "USD/share"),
    "total non-gaap operating expenses": ("non_gaap_opex", "USD_M"),
    "total gaap operating expenses": ("gaap_opex", "USD_M"),
}

RANGE_RX = re.compile(r"([-+]?[\d.,]+)\s*(?:%|)\s*(?:-|to|–)\s*"
                      r"([-+]?[\d.,]+)\s*%?")
PM_RX = re.compile(r"([-+]?\$?[\d.,]+)\s*(?:\+/-|\+-|±)\s*(\$?[\d.,]+)(%?)")


def parse_reported(section):
    """Leftmost numeric column of each wanted row."""
    out = {}
    for r in rows(section):
        key = _label(r[0])
        want = REPORTED_WANT.get(key)
        if not want or len(r) < 2:
            continue
        name, unit = want
        if name in out:
            continue                     # first occurrence is the header block
        v = _num(r[1])
        if v is not None:
            out[name] = {"value": v, "unit": unit, "label": r[0],
                         "columns": [_num(c) for c in r[1:4]]}
    return out


def parse_guidance(section):
    """The outlook table's rows are two cells: a label and a range."""
    out = {}
    for r in rows(section):
        key = _label(r[0])
        want = GUIDANCE_WANT.get(key)
        if not want or len(r) < 2:
            continue
        name, unit = want
        raw = " ".join(r[1:])
        rec = {"unit": unit, "label": r[0], "raw": raw}
        pm = PM_RX.search(raw)
        rng = RANGE_RX.search(raw)
        if pm:
            mid, tol = _num(pm.group(1)), _num(pm.group(2))
            if mid is not None and tol is not None:
                pct = pm.group(3) == "%" or "%" in raw
                lo = mid * (1 - tol / 100.0) if pct else mid - tol
                hi = mid * (1 + tol / 100.0) if pct else mid + tol
                rec.update({"midpoint": mid, "low": lo, "high": hi,
                            "basis": "midpoint +/- %s%s"
                                     % (tol, "%" if pct else "")})
        elif rng:
            lo, hi = _num(rng.group(1)), _num(rng.group(2))
            if lo is not None and hi is not None:
                rec.update({"low": lo, "high": hi, "midpoint": (lo + hi) / 2.0,
                            "basis": "stated range"})
        else:
            v = _num(raw)
            if v is None:
                continue
            rec.update({"midpoint": v, "low": v, "high": v,
                        "basis": "single figure"})
        if "midpoint" in rec and name not in out:
            out[name] = rec
    return out


def arithmetic_ok(guide):
    """The issuer publishes a reconciliation that must close. If it does
    not, our column mapping is wrong and every number here is suspect."""
    g, n = guide.get("gaap_eps"), guide.get("non_gaap_eps")
    if g and n and g.get("midpoint") is not None \
            and n.get("midpoint") is not None:
        if n["midpoint"] <= g["midpoint"]:
            return False, ("guided non-GAAP EPS (%.2f) is not above guided "
                           "GAAP EPS (%.2f); the columns are mis-mapped"
                           % (n["midpoint"], g["midpoint"]))
    gm, ngm = guide.get("gaap_gross_margin"), guide.get("non_gaap_gross_margin")
    if gm and ngm and gm.get("midpoint") and ngm.get("midpoint"):
        if ngm["midpoint"] <= gm["midpoint"]:
            return False, ("guided non-GAAP gross margin is not above the "
                           "GAAP figure; the columns are mis-mapped")
    return True, None


ORDINAL = {"first": 1, "second": 2, "third": 3, "fourth": 4}
PERIOD_RX = re.compile(r"(first|second|third|fourth)\s+quarter\s+of\s+"
                       r"fiscal(?:\s+year)?\s+(\d{4})", re.I)


def _period_label(section):
    """A short label for the guided period.

    Taking the first 120 characters of the outlook section put the entire
    heading — "Outlook for the Second Quarter of Fiscal Year 2027
    Reconciliations from GAAP to Non-GAAP (Unaudited)" — into a slot meant
    for a date the reader can check against."""
    m = PERIOD_RX.search(_text(section[:2000]))
    if m:
        return "Q%d FY%s results" % (ORDINAL[m.group(1).lower()], m.group(2))
    return "next reported quarter"


def ingest(cik, acc, fetch_text, report_time=None):
    """Return an exhibit record, or a record explaining why not.

    Never raises: a parse failure becomes AVAILABLE_NOT_INGESTED with the
    reason attached, which is a true statement about our software rather
    than a false one about the company."""
    base = {"schema": "sec_exhibit/v1", "disposition": DISPOSITION_BLOCKED,
            "reported": {}, "guidance": {}, "reason": None, "url": None,
            "accession": None, "accepted": None, "period_label": None}
    hit = find_results_8k(acc)
    if not hit:
        base["reason"] = "no 8-K carrying Item 2.02 in the acceptance window"
        return base
    accn, meta = hit
    base.update({"accession": accn, "accepted": meta.get("accepted")})
    if report_time and (meta.get("accepted") or "") > str(report_time):
        base["reason"] = ("the most recent results 8-K was accepted after "
                          "this report's point-in-time gate")
        return base
    url, err = exhibit_url(cik, accn, fetch_text)
    base["url"] = url
    if not url:
        base["reason"] = err
        return base
    try:
        doc = fetch_text(url)
    except Exception as e:
        base["reason"] = "exhibit fetch failed: %s" % e
        return base
    rep_sec, gui_sec, why = segment(doc)
    if rep_sec is None:
        base["reason"] = why
        return base
    base["period_label"] = column_period(rep_sec)
    base["reported"] = parse_reported(rep_sec)
    base["guidance"] = parse_guidance(gui_sec) if gui_sec else {}
    if not base["reported"] and not base["guidance"]:
        base["reason"] = ("the exhibit was fetched but no recognised "
                          "reconciliation row matched; the layout differs "
                          "from the one this parser handles")
        return base
    ok, why = arithmetic_ok(base["guidance"])
    if not ok:
        base.update({"reported": {}, "guidance": {}, "reason": why})
        return base
    base["disposition"] = DISPOSITION_OK
    base["guidance_period"] = _period_label(gui_sec) if gui_sec else None
    return base
