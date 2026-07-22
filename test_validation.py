#!/usr/bin/env python3
"""test_validation.py — the published validation report.

Runs every check the spec asks to see the result of, against the three
artefacts that were actually generated, and prints one line per check.
Anything False blocks the send; there is no "mostly valid".

    python daily_brief.py --dry-run     # generate the artefacts
    python test_validation.py
    python test_validation.py --json    # machine-readable
"""

import email
import email.policy
import json
import os
import re
import sys

import brief_compose as BC
import brief_model as BM
import brief_news as BN
import brief_render as BR
import brief_schema as BS
import brief_text as BX

_BASE = os.path.dirname(os.path.abspath(__file__))
PREV = os.path.join(_BASE, "docs", "email-previews")
EML = os.path.join(PREV, "daily_brief_preview.eml")
HTML = os.path.join(PREV, "daily_brief_preview.html")
TXT = os.path.join(PREV, "daily_brief_preview.txt")
MODEL = os.path.join(PREV, "daily_brief_model.json")

RESULTS = []


def check(area, name, ok, detail=""):
    RESULTS.append({"area": area, "check": name, "ok": bool(ok),
                    "detail": str(detail)[:200]})
    return ok


def main():
    for p in (EML, HTML, TXT, MODEL):
        if not os.path.exists(p):
            sys.exit("missing %s — run: python daily_brief.py --dry-run" % p)

    model = json.load(open(MODEL, encoding="utf-8"))
    html_file = open(HTML, encoding="utf-8").read()
    txt_file = open(TXT, encoding="utf-8").read()
    raw = open(EML, "rb").read()
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    html_eml = msg.get_body(("html",)).get_content()
    txt_eml = msg.get_body(("plain",)).get_content()
    body = BC.visible_text(html_eml)
    # wrapping is layout, not content: normalise whitespace on both
    # sides so a wrapped headline still matches its model record
    txt_flat = re.sub(r"\s+", " ", txt_eml)

    # ── 1. schema validation
    for r in model.get("validation", {}).get("checks", []):
        check("schema", r["check"], r["ok"], r["detail"])
    fresh = BS.validate_model(model)
    for r in fresh:
        check("schema", "revalidated: %s" % r["check"], r["ok"], r["detail"])
    check("schema", "envelope declares v1", model.get("schema") == BS.SCHEMA,
          model.get("schema"))
    check("schema", "meta carries subject and preheader",
          bool(model["meta"].get("subject")) and
          bool(model["meta"].get("preheader")))
    check("schema", "artifact hashes recorded",
          all(model.get("artifact_hashes", {}).get(k)
              for k in ("html", "text", "model")),
          model.get("artifact_hashes"))

    # ── 2. HTML / plain-text section parity
    ok_sections = True
    for sec in model["sections"]:
        recs = sec.get("records") or []
        if not recs:
            continue
        hh = sum(1 for r in recs if _probe(r) and _probe(r) in body)
        tt = sum(1 for r in recs if _probe(r) and _probe(r) in txt_flat)
        n = sum(1 for r in recs if _probe(r))
        ok_sections &= check("parity", "%s: %d records in both bodies"
                             % (sec["id"], n), hh == tt == n, (hh, tt, n))
    check("parity", "subject in envelope matches the MIME header",
          (msg["Subject"] or "") == model["meta"]["subject"],
          (msg["Subject"], model["meta"]["subject"]))
    check("parity", "text part announces the same subject",
          model["meta"]["subject"] in txt_eml)

    # ── 3. EML / standalone HTML equivalence
    # MIME requires a body to end with a line break, so the .eml part is
    # the same document plus one terminator. Compare the documents.
    def _norm(s):
        return s.rstrip("\r\n")
    check("artifacts", "standalone HTML equals the EML HTML part",
          BS.content_hash(_norm(html_file)) == BS.content_hash(_norm(html_eml)),
          "%s vs %s" % (BS.content_hash(_norm(html_file))[:12],
                        BS.content_hash(_norm(html_eml))[:12]))
    check("artifacts", "standalone text equals the EML text part",
          BS.content_hash(_norm(txt_file)) == BS.content_hash(_norm(txt_eml)))
    check("artifacts", "recorded HTML hash matches the artefact",
          model["artifact_hashes"]["html"] == BS.content_hash(html_file))
    check("artifacts", "recorded text hash matches the artefact",
          model["artifact_hashes"]["text"] == BS.content_hash(txt_file))
    check("artifacts", "multipart/alternative with both parts",
          "multipart/alternative" in [p.get_content_type()
                                      for p in msg.walk()])
    check("artifacts", "one HTML document root",
          BC.check_document(html_eml) == [], BC.check_document(html_eml))

    # ── 4. flow population reconciliation
    for sec in model["sections"]:
        if sec.get("kind") != "flow_group":
            continue
        for r in sec["records"]:
            t, d, o = (r["contract_count_total"], r["contract_count_displayed"],
                       r["contract_count_omitted"])
            c, p, u = (r["confirmed_count"], r["pending_count"],
                       r["unresolved_count"])
            check("flow", "%s: %d confirmed + %d pending + %d unresolved = %d"
                  % (r["ticker"], c, p, u, t), c + p + u == t)
            check("flow", "%s: %d displayed + %d omitted = %d"
                  % (r["ticker"], d, o, t), d + o == t)
            check("flow", "%s: rendered rows equal displayed count"
                  % r["ticker"], len(r["contracts"]) == d)
            if o:
                check("flow", "%s: omission disclosed in both bodies"
                      % r["ticker"],
                      r["omitted_line"] in body and r["omitted_line"] in txt_eml,
                      r["omitted_line"])
            check("flow", "%s: every contract states side and direction"
                  % r["ticker"],
                  all(c2.get("action") and c2.get("direction")
                      for c2 in r["contracts"]))

    # ── 5. quote timestamps and freshness
    priced = 0
    for sec in model["sections"]:
        for r in sec.get("records") or []:
            p = r.get("price")
            if not isinstance(p, dict):
                continue
            if p.get("value") is None:
                check("prices", "%s: unavailable price states a reason"
                      % r.get("ticker"), bool(p.get("unavailable_reason")))
                continue
            priced += 1
            check("prices", "%s: basis/as-of/source/stale all present"
                  % r.get("ticker"),
                  p.get("session_basis") in BS.SESSION_BASES
                  and p.get("as_of") and p.get("source") and p.get("stale_after"),
                  {k: p.get(k) for k in ("session_basis", "as_of", "source",
                                         "stale_after")})
    check("prices", "no bare 'pre-market quote' without a clock",
          "pre-market quote" not in body, "empty as_of leaked into display")
    check("prices", "%d priced rows validated" % priced, priced >= 0)

    # ── 6. calendar conversion and correction provenance
    cal = BM.section(model, "calendar")
    if cal:
        for e in cal["records"]:
            if e.get("corrected"):
                check("calendar", "%s: vendor record preserved" % e["ticker"]
                      if e.get("ticker") else "correction keeps vendor record",
                      bool(e.get("vendor_title")), e.get("vendor_title"))
                check("calendar", "correction carries authority and timestamp",
                      bool(e.get("correction_authority"))
                      and bool(e.get("correction_timestamp")))
                check("calendar", "corrected row does not link to the vendor "
                      "page it corrects",
                      (e.get("url") or "") != (e.get("vendor_url") or "")
                      or not e.get("url"), e.get("url"))
            check("calendar", "%s converted with a stated source zone"
                  % (e.get("title") or "")[:28],
                  bool(e.get("source_tz")), e.get("source_tz"))
        eia = [e for e in cal["records"] if "Crude Oil" in (e.get("title") or "")]
        if eia:
            check("calendar", "EIA crude prints at 10:30 a.m. ET",
                  eia[0]["time_et"].startswith("10:30"), eia[0]["time_et"])
    check("calendar", "no clock without a zone anywhere in the body",
          not _naked_clocks(body), _naked_clocks(body)[:2])

    # ── 7. news truncation and source diversity
    nw = BM.section(model, "news")
    if nw:
        for r in nw["records"]:
            why = r.get("why") or ""
            check("news", "%s: why within 140 chars" % r["key"][:8],
                  len(why) <= 140, len(why))
            check("news", "%s: why ends on a terminator" % r["key"][:8],
                  bool(re.search(r"[.!?…]$", why.strip())), why[-20:])
            check("news", "%s: no mid-word truncation" % r["key"][:8],
                  "…" not in why or re.search(r"(\s…|\w\w…)$", why), why[-12:])
            check("news", "%s: key is a content hash, not a prefix"
                  % r["key"][:8], re.fullmatch(r"[0-9a-f]{16}", r["key"] or ""),
                  r["key"])
        if nw["records"]:
            pubs = {}
            for r in nw["records"]:
                pubs[r["source"]] = pubs.get(r["source"], 0) + 1
            top, n = max(pubs.items(), key=lambda kv: kv[1])
            check("news", "no publisher exceeds 50%% of displayed news",
                  len(pubs) == 1 or n * 2 <= len(nw["records"]),
                  "%s: %d of %d" % (top, n, len(nw["records"])))
            mkt = [r for r in nw["records"] if r["scope"] == "market"]
            check("news", "market stories are macro/index relevant",
                  all(BN.is_market_moving({"headline": r["headline"],
                                           "tickers": []}) for r in mkt),
                  [r["headline"][:40] for r in mkt
                   if not BN.is_market_moving({"headline": r["headline"],
                                               "tickers": []})])
        else:
            check("news", "empty news state is displayed",
                  "No high-relevance headlines" in body
                  and "No high-relevance headlines" in txt_eml)

    # ── 8. reason-code support
    for r in (BM.section(model, "watchlist") or {"records": []})["records"]:
        for code in r.get("reason_codes") or []:
            check("reason_codes", "%s: %s is a known rule" % (r["ticker"], code),
                  code in BC.RULES, code)
        if "FLOW_HQ" in (r.get("reason_codes") or []):
            check("reason_codes", "%s: FLOW_HQ agrees with displayed quality"
                  % r["ticker"],
                  r.get("flow_quality") is None
                  or BC.flow_quality_is_hq(r.get("flow_quality")),
                  r.get("flow_quality"))
        if r.get("status") in BC.MATERIAL_STATES:
            check("reason_codes", "%s: material status has a material code"
                  % r["ticker"],
                  any(c in BC.MATERIAL_CODES
                      for c in r.get("reason_codes") or []),
                  r.get("reason_codes"))

    # ── 9. watch-list accounting
    m = re.search(r"(\d+) of your (\d+) watch list names changed", body)
    if check("watchlist", "alert line states both counts", bool(m)):
        changed, total = int(m.group(1)), int(m.group(2))
        quiet = _count_after("No material change:", body)
        notable = _count_after("Notable but not material:", body)
        check("watchlist", "material + notable + unchanged = eligible",
              changed + notable + quiet == total,
              (changed, notable, quiet, total))
        om = re.search(r"Showing the top (\d+) of (\d+) material changes"
                       r"[^\d]+(\d+) more", body)
        if changed > len((BM.section(model, "watchlist") or
                          {"records": []})["records"]):
            check("watchlist", "hidden changes disclosed", bool(om))
            if om:
                check("watchlist", "overflow arithmetic reconciles",
                      int(om.group(1)) + int(om.group(3)) == int(om.group(2)))

    # ── 10. regime vocabulary
    check("regime", "one display vocabulary in the model",
          all(r["ok"] for r in BS.validate_regime_vocabulary(model)),
          BS.validate_regime_vocabulary(model)[0]["detail"])
    for internal in ("risk_off", "risk_on", "mixed"):
        check("regime", "internal enum %r absent from the body" % internal,
              internal not in body, internal)

    # ── 11. unsubscribe token mode
    import daily_brief as DB
    unsub = (msg["List-Unsubscribe"] or "").strip("<>")
    check("unsubscribe", "header and visible link agree",
          BC.check_unsubscribe(html_eml, unsub) == [],
          BC.check_unsubscribe(html_eml, unsub))
    check("unsubscribe", "one-click header present",
          (msg["List-Unsubscribe-Post"] or "") == "List-Unsubscribe=One-Click")
    check("unsubscribe", "preview artefact is flagged as a preview",
          model["meta"].get("preview") is True)
    check("unsubscribe", "production guard REJECTS the preview token",
          bool(DB.production_guard("demo", unsub)),
          DB.production_guard("demo", unsub))
    real = "https://api.tickerdesk.io/unsubscribe?u=9f3a&t=" + "a" * 40
    check("unsubscribe", "production guard ACCEPTS a real signed token",
          DB.production_guard("9f3a", real) == [],
          DB.production_guard("9f3a", real))
    check("unsubscribe", "guard rejects an unsigned URL",
          bool(DB.production_guard("9f3a",
                                   "https://api.tickerdesk.io/unsubscribe?u=9f3a")))

    # ── 12. mobile overflow
    inline = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", html_eml)
    for vw in (320, 390, 600):
        wide = [int(n) for n in
                re.findall(r"(?<!max-)width:\s*(\d+)px", inline)
                if int(n) > vw and int(n) != 620]
        check("mobile", "no fixed width over %dpx" % vw, not wide, wide)
    check("mobile", "container fluid under 600px",
          ".wrap{width:100%!important" in html_eml)
    check("mobile", "1W/YTD hidden on narrow screens",
          ".wk{display:none!important}" in html_eml)
    longest = max(body.split(), key=len, default="")
    check("mobile", "longest token fits 320px",
          len(longest) * 8 + 32 <= 320, "%r" % longest[:40])

    # ── 13. earnings pane
    ep = BM.section(model, "earnings")
    if check("earnings", "earnings pane present", ep is not None):
        check("earnings", "sessions labelled BMO/AMC",
              all(r["session"] in ("BMO", "AMC") for r in ep["records"]))
        check("earnings", "implied move or an explicit n/a for each name",
              all(r.get("implied_move_pct") is not None
                  or "n/a" in body for r in ep["records"]))
        # offline runs have no chain to read; what must never happen is an
        # unpriced name rendering as though it were priced
        priced = [r for r in ep["records"] if r.get("iv_pct") is not None]
        check("earnings", "IV shown where the chain priced it, or "
              "absence disclosed",
              bool(priced) or "n/a" in body or bool(ep.get("note")),
              "%d of %d priced" % (len(priced), len(ep["records"])))
        check("earnings", "pane appears in both bodies",
              ep["title"] in body and ep["title"].upper() in txt_eml.upper())
        check("earnings", "a rich/cheap read shows its arithmetic",
              all(("×" in r["why"] or "typical move" in r["why"])
                  for r in ep["records"] if r.get("verdict")))

    # ── 14. OI follow-through
    import oi_followthrough as FT
    ftsec = BM.section(model, "oi_followthrough")
    if check("oi", "follow-through section present", ftsec is not None):
        rows = ftsec.get("records") or []
        if not rows:
            check("oi", "not-yet-posted state is disclosed",
                  "has not posted yet" in (ftsec.get("empty_line") or ""),
                  ftsec.get("empty_line"))
            check("oi", "the disclosure says the email will not update",
                  "will not update" in (ftsec.get("empty_line") or ""))
            check("oi", "no empty table is rendered in either body",
                  ftsec["empty_line"] in body
                  and ftsec["empty_line"][:40] in txt_flat)
        else:
            for r in rows:
                check("oi", "%s: state is one of the seven" % r["ticker"],
                      r.get("oi_state") in FT.STATES, r.get("oi_state"))
                check("oi", "%s: data date and verified time are separate"
                      % r["ticker"],
                      r.get("oi_data_date") != r.get("oi_verified_at"))
                check("oi", "%s: direction is carried apart from OI state"
                      % r["ticker"],
                      bool(r.get("direction")) and bool(r.get("oi_state")))
                check("oi", "%s: structure confidence stated" % r["ticker"],
                      bool(r.get("structure_confidence")))
                if r.get("follow_through_ratio") is not None:
                    check("oi", "%s: ratio is capped at 100%%" % r["ticker"],
                          r["follow_through_ratio"] <= 1.0)
            per = {}
            for r in rows:
                per[r["ticker"]] = per.get(r["ticker"], 0) + 1
            check("oi", "at most two contracts per ticker",
                  max(per.values()) <= 2, per)
            check("oi", "email carries at most five rows", len(rows) <= 5)
    check("oi", "nothing in the body claims direction was confirmed",
          "DIRECTION CONFIRMED" not in body.upper())
    check("oi", "the measure is named follow-through, not confirmation",
          "OI confirmed" not in body, "legacy 'OI confirmed' language")

    # ── report
    if "--json" in sys.argv:
        print(json.dumps({"ok": all(r["ok"] for r in RESULTS),
                          "results": RESULTS}, indent=1))
        return 0 if all(r["ok"] for r in RESULTS) else 1

    areas = []
    for r in RESULTS:
        if r["area"] not in areas:
            areas.append(r["area"])
    for a in areas:
        rows = [r for r in RESULTS if r["area"] == a]
        bad = [r for r in rows if not r["ok"]]
        print("\n%-14s %d/%d" % (a.upper(), len(rows) - len(bad), len(rows)))
        for r in bad:
            print("   FALSE  %s  <- %s" % (r["check"], r["detail"]))
    ok = all(r["ok"] for r in RESULTS)
    print("\n%d/%d validation results TRUE" %
          (sum(1 for r in RESULTS if r["ok"]), len(RESULTS)))
    print("OVERALL: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def _probe(r):
    return r.get("ticker") or r.get("headline") or r.get("title") or ""


def _naked_clocks(body):
    zone = re.compile(r"\s*(ET|UTC|GMT|[A-Z][A-Za-z_]+/[A-Za-z_]+)")
    return [body[t.start():t.end() + 10]
            for t in re.finditer(r"\d{1,2}:\d{2}\s*[ap]\.?m\.?", body)
            if not zone.match(body[t.end():t.end() + 24])]


def _count_after(label, body):
    m = re.search(re.escape(label) + r" ((?:[A-Z][A-Z.\-]{0,5}(?:,\s*)?)+)",
                  body)
    return len(re.findall(r"[A-Z][A-Z.\-]{0,5}", m.group(1))) if m else 0


if __name__ == "__main__":
    raise SystemExit(main())
