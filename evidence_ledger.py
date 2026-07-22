#!/usr/bin/env python3
"""evidence_ledger.py — addressable evidence for every rendered claim.

The v2 brief already refused synthetic data. The remaining gap was
reproducibility: a reader could see "revenue grew 18.5%" and had no way
to get from that sentence back to the two XBRL facts, the filings that
published them, and the arithmetic between. This ledger gives every
underlying observation a stable id, and every rendered claim a list of
`evidence_refs` naming the exact ids it stands on.

Record kinds and their id shapes:
    BAR-2026-07-21                    one daily OHLCV bar
    MKT-<series>                      the bar series header (vendor, span)
    SHR-0001035267-26-000058          cover-page share count
    XBRL-<accn>-<tag>-<period_end>    one tagged financial fact
    F4-<accn>                         one Form 4 filing
    F4-<accn>#3                       one transaction inside it
    OWN-<accn>                        one Schedule 13D/13G filing
    CAT-<accn>                        one catalyst disclosure
    CALC-<slug>                       one calculation: formula + inputs
    REC-<slug>                        one recommendation input
    SOC-<source>:<id>                 one social observation
    NEWS-<hash>                       one news item

A CALC record carries `inputs` as refs to other records, so a reader can
walk the whole derivation without leaving the file.
"""

import hashlib
import json


def _h(s, n=10):
    return hashlib.sha256(str(s).encode("utf-8")).hexdigest()[:n]


HASH_ALGO = "sha256"
HASH_ENCODING = "utf-8"


def content_hash(normalized_text):
    """The ONE hash function. Public `text_hash` and private
    `content_hash` must both call this over the same canonical string, or
    the two exports disagree while each looks internally consistent —
    which is exactly what happened: 13 of 30 public hashes differed from
    their private counterparts while the ledger reported ok:true, because
    verification only ever compared the private file to itself.

    Full 64 characters: a truncated digest saves nothing and weakens the
    only thing the field is for.
    """
    return hashlib.sha256(
        str(normalized_text).encode(HASH_ENCODING)).hexdigest()


class Ledger(object):
    """Append-only evidence store keyed by stable ids."""

    SECTIONS = ("market_bars", "benchmark_bars", "shares_outstanding",
                "xbrl_facts",
                "form4_records", "ownership_filings",
                "technical_calculations", "catalyst_records",
                "recommendation_inputs", "social_records", "news_records")

    def __init__(self, ticker, report_time):
        self.ticker = ticker
        self.report_time = report_time
        self._store = {k: {} for k in self.SECTIONS}
        self._index = {}          # id -> section
        self.coverage = {}
        # Distinct populations, kept apart. "61 transactions from 1,501
        # filings" mixed two different things: the whole EDGAR index and
        # the parsed rows. Every displayed count statement is generated
        # from this structure, so prose cannot drift from the arrays.
        self.counts = {}
        self._audit = {}          # private immutable snapshot

    # ── populations ─────────────────────────────────────────────────────
    def population(self, domain, **fields):
        """Record one domain's populations. Field names are the shared
        vocabulary: records_fetched, source_filings_scanned,
        records_parsed, records_in_window, records_admitted,
        records_displayed."""
        self.counts.setdefault(domain, {}).update(
            {k: v for k, v in fields.items() if v is not None})
        return self.counts[domain]

    def statement(self, domain, *pairs):
        """Build a displayed count sentence FROM the recorded populations,
        so the sentence and the arrays cannot disagree."""
        c = self.counts.get(domain) or {}
        bits = []
        for label, key in pairs:
            v = c.get(key)
            if v is not None:
                bits.append("%s %s" % (format(v, ","), label))
        return " · ".join(bits)

    # ── private audit snapshot ──────────────────────────────────────────
    def audit_record(self, evidence_id, normalized_text, hash_input,
                     content_hash, norm_version, source_meta=None,
                     retrieval_meta=None):
        """The immutable material a hash was computed from.

        The public export carries hashes but not the exact bytes hashed,
        so a reader cannot independently reproduce them. This snapshot
        holds the normalized text and the literal hash input; it is NOT
        part of the public bundle.
        """
        self._audit[evidence_id] = {
            "evidence_id": evidence_id,
            "normalized_text": normalized_text,
            "hash_input": hash_input,
            "content_hash": content_hash,
            "normalization_version": norm_version,
            "source": source_meta or {},
            "retrieval": retrieval_meta or {},
        }
        return evidence_id

    def verify_hashes(self):
        """Recompute every private hash AND compare it to the public
        record's text_hash.

        Comparing the private file only to itself is a tautology: it
        passed while 13 of 30 public hashes disagreed. Both directions
        are checked here, and the public/private comparison is the one
        that actually matters.
        """
        out = {"algorithm": HASH_ALGO, "encoding": HASH_ENCODING,
               "digest_chars": 64, "checked": 0, "recompute_mismatched": [],
               "public_private_mismatched": [], "public_missing": [],
               "ok": True}
        pub = self._store.get("social_records") or {}
        for eid, rec in self._audit.items():
            out["checked"] += 1
            got = content_hash(rec["hash_input"])
            if got != rec["content_hash"]:
                out["ok"] = False
                out["recompute_mismatched"].append(
                    {"evidence_id": eid, "stored": rec["content_hash"],
                     "recomputed": got})
            p = pub.get(eid)
            if p is None:
                out["public_missing"].append(eid)
                out["ok"] = False
            elif p.get("text_hash") != rec["content_hash"]:
                out["ok"] = False
                out["public_private_mismatched"].append(
                    {"evidence_id": eid, "public": p.get("text_hash"),
                     "private": rec["content_hash"]})
        out["mismatched"] = (out["recompute_mismatched"]
                             + out["public_private_mismatched"])
        return out

    def audit_snapshot(self):
        return {
            "schema": "evidence_audit_snapshot/v1",
            "ticker": self.ticker,
            "report_time": self.report_time,
            "note": ("PRIVATE. Contains the exact normalized text each "
                     "content hash was computed from. Retained so hashes "
                     "in the public export can be independently "
                     "reproduced; not for distribution."),
            "records": list(self._audit.values()),
        }

    # ── writing ─────────────────────────────────────────────────────────
    def add(self, section, rec_id, rec):
        if section not in self._store:
            raise KeyError("unknown evidence section %r" % section)
        rec = dict(rec)
        rec["id"] = rec_id
        self._store[section][rec_id] = rec
        self._index[rec_id] = section
        return rec_id

    def bar(self, date, o, h, l, c, v):
        return self.add("market_bars", "BAR-%s" % date,
                        {"date": date, "open": o, "high": h, "low": l,
                         "close": c, "volume": v})

    def calc(self, slug, formula, inputs, output, unit=None, note=None):
        """A calculation is evidence only if its inputs are themselves
        addressable, so `inputs` must be a list of existing ids (or a
        compact range string over ids that exist)."""
        return self.add("technical_calculations", "CALC-%s" % slug,
                        {"formula": formula, "inputs": inputs,
                         "output": output, "unit": unit, "note": note})

    def rec_input(self, slug, name, value, refs, rationale=None):
        return self.add("recommendation_inputs", "REC-%s" % slug,
                        {"name": name, "value": value,
                         "evidence_refs": list(refs or []),
                         "rationale": rationale})

    # ── reading / validation ────────────────────────────────────────────
    def has(self, rec_id):
        return rec_id in self._index

    def missing(self, refs):
        """Refs that name nothing in the ledger. A range like
        'BAR-2026-06-23..BAR-2026-07-21' resolves if both ends exist."""
        out = []
        for r in refs or []:
            if ".." in str(r):
                a, b = str(r).split("..", 1)
                if not (self.has(a.strip()) and self.has(b.strip())):
                    out.append(r)
            elif not self.has(r):
                out.append(r)
        return out

    def ids(self):
        return set(self._index)

    def count_statements(self):
        """The exact sentences the report displays, built from `counts`."""
        s = {}
        f = self.counts.get("form4") or {}
        if f:
            s["form4"] = self.statement(
                "form4",
                ("Form 4 filings on record", "source_filings_in_index"),
                ("inside the %d-day analysis window"
                 % (f.get("window_days") or 0), "source_filings_scanned"),
                ("transactions parsed", "records_parsed"),
                ("open-market sales", "open_market_sales"))
        if self.counts.get("ownership"):
            s["ownership"] = self.statement(
                "ownership",
                ("Schedule 13D/G records parsed", "records_parsed"),
                ("inside the analysis window", "records_in_window"),
                ("displayed", "records_displayed"))
        if self.counts.get("market"):
            s["market"] = self.statement(
                "market",
                ("issuer daily sessions", "issuer_sessions"),
                ("benchmark reference series", "benchmark_references"))
        if self.counts.get("social"):
            s["social"] = self.statement(
                "social",
                ("posts fetched", "records_fetched"),
                ("records parsed", "records_parsed"),
                ("records admitted", "records_admitted"),
                ("rejected", "records_rejected"),
                ("shown in core", "shown_core"),
                ("shown in appendix", "shown_appendix"))
        if self.counts.get("news"):
            s["news"] = self.statement(
                "news",
                ("items fetched", "records_fetched"),
                ("records admitted after article-level relevance",
                 "records_admitted"),
                ("rejected", "records_rejected"),
                ("shown in core", "shown_core"),
                ("shown in appendix", "shown_appendix"))
        return s

    def reconcile(self):
        """Invariants over the populations. Any failure blocks export."""
        v = []
        c = self.counts

        def _le(dom, a, b):
            x, y = (c.get(dom) or {}).get(a), (c.get(dom) or {}).get(b)
            if x is not None and y is not None and x > y:
                v.append("%s: %s (%d) exceeds %s (%d)" % (dom, a, x, b, y))

        _le("form4", "records_in_window", "records_parsed")
        _le("form4", "open_market_sales", "records_in_window")
        _le("ownership", "records_in_window", "records_parsed")
        _le("ownership", "records_displayed", "records_in_window")
        _le("social", "records_admitted", "records_parsed")
        _le("social", "records_displayed", "records_admitted")
        _le("news", "records_admitted", "records_fetched")
        so = c.get("social") or {}
        if None not in (so.get("records_parsed"), so.get("records_admitted"),
                        so.get("records_rejected")):
            if so["records_parsed"] != so["records_admitted"] + \
                    so["records_rejected"]:
                v.append("social: considered %d != admitted %d + rejected %d"
                         % (so["records_parsed"], so["records_admitted"],
                            so["records_rejected"]))
        # ledger arrays must match the populations they claim to hold
        mk = c.get("market") or {}
        if mk.get("issuer_sessions") is not None:
            stored = len([r for r in self._store["market_bars"].values()
                          if r.get("id", "").startswith("BAR-")])
            if stored != mk["issuer_sessions"]:
                v.append("market: %d issuer bars stored but issuer_sessions "
                         "reports %d" % (stored, mk["issuer_sessions"]))
        return v

    def count(self, section):
        return len(self._store.get(section) or {})

    def to_dict(self, extra=None):
        d = {
            "schema": "evidence_ledger/v2",
            "schema_history": {
                "v1": "flat sections, array lengths only",
                "v2": ("adds `counts` (named populations per domain), "
                       "`count_statements` (display strings generated "
                       "from those populations) and a private audit "
                       "snapshot alongside; v1 readers can ignore both"),
            },
            "ticker": self.ticker,
            "report_time": self.report_time,
            # the shared vocabulary, carried in the export so the PDF and
            # the JSON cannot drift into two dialects
            "terminology": {
                "earliest verified public disclosure":
                    "the company's own release of results; the catalyst",
                "later periodic filing":
                    "a 10-Q/10-K that follows it and cannot replace it",
                "analysis window": "the dated span a population was drawn from",
                "records parsed": "rows extracted from the source documents",
                "records admitted": "rows that survived validation",
                "shown in core": "rows printed in the four-page brief",
                "shown in appendix": "rows printed in the appendix PDF",
                "benchmark reference":
                    "an external series used for comparison, never counted "
                    "as issuer data",
                "observational context":
                    "descriptive only; not an independent trade signal",
            },
            "source_coverage": self.coverage,
            "counts": self.counts,
            "count_statements": self.count_statements(),
            "hash_verification": self.verify_hashes(),
            # Array lengths are a storage fact, NOT an analysis
            # population. form4_records mixes transaction rows with the
            # source-filing objects they came from, so it is named for
            # what it is rather than passed off as a transaction count.
            "record_counts": {
                ("form4_evidence_objects" if k == "form4_records" else k):
                    len(v) for k, v in self._store.items()},
            "record_count_note": (
                "form4_evidence_objects counts BOTH parsed transaction "
                "rows (F4TXN-*) and the source filings they came from "
                "(F4-*). Quote `counts.form4` for analysis populations."),
        }
        for k in self.SECTIONS:
            d[k] = list(self._store[k].values())
        if extra:
            d.update(extra)
        return d

    def dump(self, path, extra=None):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(extra), f, indent=2, default=str)
        return path


def cross_verify_exports(public_path, private_path):
    """Reopen the two FINISHED files and compare them on disk.

    In-memory agreement is not the claim being made: the claim is that
    the bundle a reader receives is self-consistent. So this reads both
    written artefacts back and recomputes every hash from the private
    snapshot's own bytes.
    """
    with open(public_path, encoding="utf-8") as fh:
        pub = json.load(fh)
    with open(private_path, encoding="utf-8") as fh:
        prv = json.load(fh)
    pubrec = {r["id"]: r for r in (pub.get("social_records") or [])}
    out = {"public_file": public_path, "private_file": private_path,
           "public_records": len(pubrec),
           "private_records": len(prv.get("records") or []),
           "compared": 0, "recompute_mismatched": [],
           "public_private_mismatched": [], "missing_in_public": [],
           "ok": True}
    for rec in prv.get("records") or []:
        eid = rec.get("evidence_id")
        out["compared"] += 1
        got = content_hash(rec.get("hash_input"))
        if got != rec.get("content_hash"):
            out["ok"] = False
            out["recompute_mismatched"].append(eid)
        p = pubrec.get(eid)
        if p is None:
            out["ok"] = False
            out["missing_in_public"].append(eid)
        elif p.get("text_hash") != rec.get("content_hash"):
            out["ok"] = False
            out["public_private_mismatched"].append(
                {"evidence_id": eid, "public": p.get("text_hash"),
                 "private": rec.get("content_hash")})
    return out


def excerpt(text, limit=110):
    """Truncate at a WORD boundary and mark it with a true ellipsis.
    Excerpts ending "...Buy the Dip i" read as data corruption; the
    untruncated text stays in the private snapshot."""
    t = " ".join(str(text or "").split())
    if len(t) <= limit:
        return t
    cut = t[:limit]
    sp = cut.rfind(" ")
    if sp > limit * 0.5:
        cut = cut[:sp]
    return cut.rstrip(" ,.;:-—") + "…"


def news_id(url):
    return "NEWS-" + _h(url)


def social_id(record_id):
    return "SOC-" + str(record_id)


def xbrl_id(accn, tag, period_end):
    return "XBRL-%s-%s-%s" % (accn, tag, period_end)
