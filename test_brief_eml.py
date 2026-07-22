#!/usr/bin/env python3
"""test_brief_eml.py — validate the artefact that actually gets delivered.

Every other suite in this repo tests a function. This one opens the .eml,
parses the MIME, and asserts against the two bodies a subscriber receives.
That distinction matters: the double-wrapped document bug lived entirely
in the gap between "the renderer returns a valid document" and "the file
we looked at contains one".

    python daily_brief.py --dry-run      # produce the artefacts
    python test_brief_eml.py
"""

import email
import email.policy
import os
import re
import sys

import brief_compose as BC

_BASE = os.path.dirname(os.path.abspath(__file__))
PREV = os.path.join(_BASE, "docs", "email-previews")
EML = os.path.join(PREV, "daily_brief_preview.eml")
SITE = "https://tickerdesk.io"


def load():
    with open(EML, "rb") as f:
        return email.message_from_binary_file(f, policy=email.policy.default)


def main():
    if not os.path.exists(EML):
        sys.exit("no .eml — run: python daily_brief.py --dry-run")
    m = load()
    fails, ran = [], [0]

    def chk(n, c, d=""):
        ran[0] += 1
        print(("  PASS  " if c else "  FAIL  ") + n
              + ("" if c else "  <- %s" % (d,)))
        if not c:
            fails.append(n)

    html = m.get_body(("html",))
    text = m.get_body(("plain",))
    html_s = html.get_content() if html else ""
    text_s = text.get_content() if text else ""
    body = BC.visible_text(html_s)
    subject = m["Subject"] or ""
    types = [p.get_content_type() for p in m.walk()]

    # ── MIME envelope
    chk("multipart/alternative", "multipart/alternative" in types, types)
    chk("carries an HTML part", bool(html_s))
    chk("carries a plain-text part", bool(text_s.strip()))
    chk("both parts are UTF-8",
        (html.get_content_charset() or "").lower() == "utf-8"
        and (text.get_content_charset() or "").lower() == "utf-8",
        (html.get_content_charset(), text.get_content_charset()))
    chk("Reply-To present", bool(m["Reply-To"]), m["Reply-To"])
    chk("List-Unsubscribe present", bool(m["List-Unsubscribe"]))
    chk("List-Unsubscribe-Post is one-click",
        (m["List-Unsubscribe-Post"] or "") == "List-Unsubscribe=One-Click",
        m["List-Unsubscribe-Post"])

    # ── P0 document validity, against the delivered part
    probs = BC.check_document(html_s)
    chk("exactly one doctype/html/head/body in the DELIVERED part",
        probs == [], probs)
    chk('html carries lang="en"', 'lang="en"' in html_s[:400])

    # ── P0 unsubscribe: header and visible link are the same endpoint
    unsub = (m["List-Unsubscribe"] or "").strip("<>")
    chk("unsubscribe is a signed subscriber endpoint",
        "/unsubscribe?" in unsub and "u=" in unsub and "t=" in unsub, unsub)
    chk("unsubscribe never points at the watch list",
        "#watchlist" not in unsub, unsub)
    chk("visible link matches the header",
        BC.check_unsubscribe(html_s, unsub) == [],
        BC.check_unsubscribe(html_s, unsub))

    # ── P0 time safety
    # the calendar renders "<time> <title>", so the anchor sits BEFORE the
    # event name -- look backwards from it
    eia = re.search(r"([\d:]+\s*[ap]\.m\.\s*ET)\s+Crude Oil Inventories",
                    body)
    if eia:
        chk("EIA crude prints at its published 10:30 ET, not 2:30pm",
            eia.group(1).startswith("10:30"), eia.group(1))
    else:
        chk("EIA crude prints at its published time (not scheduled today)",
            True)
    # every clock reading must be followed by its zone. Checked by scanning
    # forward rather than with a lookahead, which backtracks over the
    # trailing period in "p.m." and reports a false positive.
    # A source timestamp is displayed in its OWN zone on purpose -- that is
    # the provenance line ("from 2:30pm UTC"). What must never appear is a
    # clock with no zone at all.
    _ZONE = re.compile(r"\s*(ET|UTC|GMT|[A-Z][A-Za-z_]+/[A-Za-z_]+)")
    naked = [t for t in re.finditer(r"\d{1,2}:\d{2}\s*[ap]\.?m\.?", body)
             if not _ZONE.match(body[t.end():t.end() + 24])]
    chk("no clock time appears without its zone", not naked,
        [body[t.start():t.end() + 12] for t in naked[:3]])

    # ── P0 subject / preheader
    chk("subject <= 65 chars", len(subject) <= 65, len(subject))
    chk("subject makes no unsupported claim",
        BC.check_subject_supported(subject, body,
                                   ("watchlist", "flow", "calendar",
                                    "news", "discovery")) == [],
        BC.check_subject_supported(subject, body,
                                   ("watchlist", "flow", "calendar",
                                    "news", "discovery")))
    m_pre = re.search(r'mso-hide:all[^>]*>(.*?)</div>', html_s, re.S)
    pre = re.sub(r"<[^>]+>", "", m_pre.group(1)) if m_pre else ""
    chk("preheader <= 140 chars", len(pre) <= 140, len(pre))
    chk("preheader ends on a whole token",
        BC.check_preheader(pre) == [], BC.check_preheader(pre))

    # ── P0 watch-list arithmetic, read off the rendered words
    am = re.search(r"(\d+) of your (\d+) watch list names changed", body)
    chk("alert line states both counts", bool(am), body[:120])
    if am:
        changed, total = int(am.group(1)), int(am.group(2))
        shown = len(re.findall(
            r"(TRIGGER REACHED|REVIEW NOW|MIXED SETUP|MATERIAL "
            r"STRENGTHENING|MATERIAL WEAKENING|MONITOR)\b", body))
        om = re.search(r"Showing the top (\d+) of (\d+) material changes"
                       r"[^\d]+(\d+) more", body)
        if changed > shown:
            chk("hidden changes are stated, not dropped", bool(om),
                "no overflow line for %d changed vs %d shown"
                % (changed, shown))
            if om:
                chk("overflow arithmetic reconciles",
                    int(om.group(1)) + int(om.group(3)) == int(om.group(2))
                    == changed,
                    om.group(0))
        else:
            chk("hidden changes are stated, not dropped", True)
        # visible_text collapses newlines, so bound the capture at the next
        # section rather than running to the end of the document
        def _names(label):
            m = re.search(re.escape(label)
                          + r" ((?:[A-Z][A-Z.\-]{0,5}(?:,\s*)?)+)", body)
            return len(re.findall(r"[A-Z][A-Z.\-]{0,5}", m.group(1))) \
                if m else 0
        # three buckets now: MATERIAL, NOTABLE, and unchanged. They must
        # still add up to the eligible watch list.
        quiet = _names("No material change:")
        notable = _names("Notable but not material:")
        chk("material + notable + unchanged = eligible",
            changed + notable + quiet == total,
            (changed, notable, quiet, total))
        chk("notable names are not counted as material",
            notable == 0 or "changed materially" not in
            (re.search(r"Notable but not material:[^.]*", body)
             or re.match("", "")).group(0))

    # ── P0 no row without a reason
    for mm in re.finditer(r"(MONITOR|MIXED SETUP|MATERIAL \w+|REVIEW NOW)\s+"
                          r"([A-Z][A-Z.\-]{0,5})\s*(\$[\d,.]+)?\s*(.{0,60})",
                          body):
        tail = (mm.group(4) or "").strip()
        chk("%s row states a reason" % mm.group(2),
            len(tail) > 3, "%r" % tail)

    # ── P0 OI state machine
    chk("no contract is UNCONFIRMED without a reading",
        "UNCONFIRMED" not in body or "OI PENDING" in body
        or "open interest" in body,
        "UNCONFIRMED used with no OI language")
    for lab in ("OI PENDING", "CONFIRMED", "UNCONFIRMED"):
        if lab in body:
            chk("%r is explained where it appears" % lab,
                "open interest" in body.lower(), lab)

    # ── P1 flow budget
    words = len(body.split())
    fm = re.search(r"Options flow(.*?)(Macro calendar|Top news|"
                   r"Market discovery|Weekly lens|Open your desk)", body,
                   re.S)
    flow_words = len(fm.group(1).split()) if fm else 0
    pct = (100.0 * flow_words / words) if words else 0
    chk("options flow <= 35%% of visible text (was 64%%)", pct <= 35,
        "%.1f%% (%d of %d words)" % (pct, flow_words, words))
    chk("flow names both populations explicitly",
        "Market-wide" in body and "Your watch list" in body)
    chk("one View-all-flow link",
        body.count("View all flow") <= 1, body.count("View all flow"))
    chk("no mechanical 'of N print(s)' phrasing survives",
        "print(s)" not in body, "print(s) still rendered")

    # ── P1 presentation
    chk("no negative zero", BC.check_no_negative_zero(body) == [],
        BC.check_no_negative_zero(body))
    chk("mobile hides 1W/YTD from the compact table",
        ".wk{display:none!important}" in html_s)
    chk("mobile restores 1W/YTD on a second line",
        ".mo{display:block!important}" in html_s
        and 'class="mo"' in html_s)
    chk("container is fluid under 600px",
        ".wrap{width:100%!important" in html_s
        and "max-width:100%!important" in html_s)

    # Horizontal overflow, checked structurally rather than by eye. Three
    # things can push a table email sideways: a fixed width wider than the
    # viewport that nothing overrides, an unbreakable text token, and a
    # nowrap cell holding something long.
    # scan element styles only: the <style> block holds the breakpoint
    # itself (max-width:600px), which is a query, not a box
    inline = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", html_s)
    for vw in (320, 390, 600):
        fixed = [int(n) for n in
                 re.findall(r"(?<!max-)width:\s*(\d+)px", inline)]
        # .wrap's 620px is the only wide fixed width, and the media query
        # overrides both width and max-width below 600px
        offenders = [w for w in fixed if w > vw and w != 620]
        chk("no fixed width exceeds %dpx" % vw, not offenders, offenders)
    chk("the one wide container is overridden below 600px",
        "620px" in html_s and ".wrap{width:100%!important" in html_s)
    longest = max((t for t in re.findall(r"[^\s<>]+", body)), key=len,
                  default="")
    # ~8px per char at 15px in the stack's default face, plus 32px padding
    chk("longest unbreakable token fits 320px",
        len(longest) * 8 + 32 <= 320, "%r (%d chars)" % (longest,
                                                         len(longest)))
    nowrap = re.findall(r'white-space:nowrap">([^<]{0,80})', html_s)
    worst = max(nowrap, key=len) if nowrap else ""
    chk("no nowrap cell is wide enough to force a scroll",
        len(worst) * 8 + 32 <= 320, "%r (%d chars)" % (worst, len(worst)))
    chk("no fixed-width images", "<img" not in html_s or "max-width" in html_s)

    # ── P1 plain-text parity
    urls = ["%s/#desk" % SITE, "%s/#settings" % SITE, unsub]
    chk("plain text carries every core URL",
        BC.check_plain_text(text_s, urls) == [],
        BC.check_plain_text(text_s, urls))
    chk("plain text carries ticker links", "/#ticker=" in text_s)
    chk("plain text has no markup",
        "<td" not in text_s and "font-family" not in text_s)
    for token in ("Market in 30 seconds", "Your watch list"):
        chk("plain text mirrors %r" % token,
            token.lower() in text_s.lower(), token)

    # ── size
    kb = os.path.getsize(EML) / 1024.0
    chk("under the Gmail 102KB clip limit", kb < 102, "%.0f KB" % kb)

    print("\n%d/%d checks passed" % (ran[0] - len(fails), ran[0]))
    if fails:
        print("FAILED: " + "; ".join(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
