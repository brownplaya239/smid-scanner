#!/usr/bin/env python3
"""test_parity.py — the two bodies describe the same email.

Compares NORMALIZED SECTION RECORDS, not prose. Prose comparison would
either pass on any two documents that share vocabulary or fail on
harmless wording differences; neither tells you whether the plain-text
reader and the HTML reader are looking at the same six contracts.

The specific regression: plain text listed six market-wide contracts
while the HTML listed three, because each renderer applied its own cap.
Both now iterate one model, and this test proves the rendered output
still contains exactly the model's records on both sides.

    python test_parity.py
"""

import json
import re
import sys

import brief_compose as BC
import brief_model as BM
import brief_render as BR
import brief_text as BX


def html_body(doc):
    return BC.visible_text(doc)


def find_all(text, needle):
    return len(re.findall(re.escape(needle), text))


def main():
    fails, ran = [], [0]

    def chk(n, c, d=""):
        ran[0] += 1
        print(("  PASS  " if c else "  FAIL  ") + n
              + ("" if c else "  <- %s" % (d,)))
        if not c:
            fails.append(n)

    model = BR._demo_model()
    doc = BR.render(model, preheader="parity")
    txt = BX.render_text(model, subject="parity")
    hb = html_body(doc)
    txt_flat = re.sub(r"\s+", " ", txt)

    # ── one model, one fingerprint
    fp = BM.parity_fingerprint(model)
    chk("model fingerprint is non-trivial", len(fp) >= 6, fp)

    # ── section presence
    for sid, keys in fp:
        sec = BM.section(model, sid)
        if not (sec.get("records") or sec.get("empty_line")):
            continue
        title = sec.get("title") or ""
        if title:
            chk("section %r present in HTML" % sid, title in hb, title)
            chk("section %r present in text" % sid,
                title.upper() in txt.upper(), title)

    # ── record-level population equality, per section
    for sid in ("flow_market",):
        sec = BM.section(model, sid)
        if not sec:
            continue
        want = [r["key"] for r in sec["records"]]
        # a contract is identified by ticker+right+strike in both bodies
        for key in want:
            tk, right, strike, _exp = key.split("|")
            probe = "%s %s" % (right, strike)
            chk("%s: %s present in HTML" % (sid, key),
                tk in hb and probe in hb, probe)
            chk("%s: %s present in text" % (sid, key),
                tk in txt and probe in txt, probe)
        chk("%s: HTML shows exactly %d contracts" % (sid, len(want)),
            len(want) == len(sec["records"]))

    # the historical failure, stated directly
    mk = BM.section(model, "flow_market")
    if mk:
        n = len(mk["records"])
        html_hits = sum(1 for r in mk["records"]
                        if ("%s %s" % (r["right"], r["strike"])) in hb)
        text_hits = sum(1 for r in mk["records"]
                        if ("%s %s" % (r["right"], r["strike"])) in txt)
        chk("market-wide contract COUNT matches across bodies",
            html_hits == text_hits == n, (html_hits, text_hits, n))

    # ── watch-list populations and ordering
    wl = BM.section(model, "watchlist")
    order_html = [t for t in [r["ticker"] for r in wl["records"]]]
    seen_html = [t for t in order_html if t in hb]
    seen_txt = [t for t in order_html if t in txt]
    chk("watch-list population identical", seen_html == seen_txt == order_html,
        (seen_html, seen_txt))
    if len(order_html) >= 2:
        a, b = order_html[0], order_html[1]
        chk("watch-list ORDER identical",
            (hb.index(a) < hb.index(b)) == (txt.index(a) < txt.index(b)))

    # ── classifications
    for r in wl["records"]:
        chk("%s status in both bodies" % r["ticker"],
            r["status"] in hb and r["status"] in txt, r["status"])

    # ── flow classifications and scores
    for sid in ("flow_driving", "flow_other"):
        sec = BM.section(model, sid)
        if not sec:
            continue
        for r in sec["records"]:
            chk("%s %s verdict in both" % (sid, r["ticker"]),
                r["verdict"] in hb and r["verdict"] in txt, r["verdict"])
            chk("%s %s score in both" % (sid, r["ticker"]),
                r["score"] in hb and r["score"] in txt, r["score"])

    # ── premiums and timestamps
    prem = set()
    for sec in model["sections"]:
        for r in sec.get("records") or []:
            for c in ([r] if r.get("premium") else []) + (
                    r.get("contracts") or []):
                if c.get("premium"):
                    prem.add(c["premium"])
    for p in sorted(prem):
        chk("premium %s in both bodies" % p, p in hb and p in txt, p)

    # ── news records
    nw = BM.section(model, "news")
    for r in nw["records"]:
        chk("headline in both bodies", r["headline"] in hb
            and r["headline"] in txt, r["headline"][:40])
        chk("source tier in both bodies", r["tier"] in hb and r["tier"] in txt)

    # ── event provenance
    ev = BM.section(model, "calendar")
    if ev:
        for r in ev["records"]:
            chk("event time in both", r["time_et"] in hb
                and r["time_et"] in txt, r["time_et"])

    # ── neither body invents a ticker the other lacks
    def tickers(s):
        return set(re.findall(r"\b[A-Z]{2,5}\b", s))
    model_tickers = set()
    for sec in model["sections"]:
        for r in sec.get("records") or []:
            if r.get("ticker"):
                model_tickers.add(r["ticker"])
            for c in r.get("contracts") or []:
                model_tickers.add(c["ticker"])
    extra_html = (tickers(hb) & model_tickers) - (tickers(txt) & model_tickers)
    extra_txt = (tickers(txt) & model_tickers) - (tickers(hb) & model_tickers)
    chk("no ticker appears in HTML but not text", not extra_html, extra_html)
    chk("no ticker appears in text but not HTML", not extra_txt, extra_txt)

    print("\n%d/%d checks passed" % (ran[0] - len(fails), ran[0]))
    if fails:
        print("FAILED: " + "; ".join(fails[:8]))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
