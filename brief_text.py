#!/usr/bin/env python3
"""brief_text.py — the text/plain half of the message.

Renders the same brief_model sections as brief_render, in the same order,
with the same records. Not a stripped copy of the HTML: tag-stripping
drops every link, because the HTML carries its URLs in href attributes, so
a plain-text reader would see the words "Open your desk" with no way to
open anything.

Because both halves iterate one model, they cannot disagree about how many
contracts or headlines the email contains — which they previously did.

    python brief_text.py --self-test
"""

import sys
import textwrap

import brief_model as BM

WIDTH = 74


def _rule(ch="-"):
    return ch * WIDTH


def _head(title):
    return "\n%s\n%s" % (title.upper(), _rule())


def _wrap(s, indent=""):
    """Wrap onto continuation lines without ever splitting a token.

    textwrap breaks long words by default, which cut URLs and ticker
    symbols in half — an unusable link and an unrecognisable symbol. A
    line that runs long is better than a token that is destroyed.
    """
    if not s:
        return ""
    return "\n".join(textwrap.wrap(
        str(s), WIDTH, initial_indent=indent, subsequent_indent=indent + "  ",
        break_long_words=False, break_on_hyphens=False))


def _pct(v, places=2):
    if v is None:
        return "n/a"
    if abs(round(v, places)) < 10 ** -places / 2:
        return "%.*f%%" % (places, 0.0)
    return "%+.*f%%" % (places, v)


# ── sections ────────────────────────────────────────────────────────────

def t_market(sec, site, out):
    reg = sec.get("regime") or {}
    out += [_head(sec["title"]), sec.get("sub") or "", "",
            reg.get("label") or "MARKET", _wrap(reg.get("why") or ""), ""]
    out.append("%-6s %10s %9s %9s %9s %9s"
               % ("", "Last", "1D", "1W", "YTD", "vs 20d"))
    for r in sec["records"]:
        out.append("%-6s %10.2f %9s %9s %9s %9s"
                   % (r["ticker"], r["last"] or 0, _pct(r["d1"]),
                      _pct(r["w1"]), _pct(r["ytd"]), _pct(r["vs20"], 1)))
        out.append("       %s" % r["url"])
    if sec.get("ma_summary"):
        out += ["", sec["ma_summary"]]
    ev = sec.get("event") or {}
    if ev.get("title"):
        out += ["", "NEXT: %s · %s · %s" % (ev.get("title"),
                                            ev.get("time_et") or "",
                                            ev.get("status") or "")]
    out += ["", _wrap(sec.get("alert_line") or "")]
    if sec.get("mixed_sessions"):
        out.append(_wrap("Quotes span more than one session; each value is "
                         "labelled with its own as-of."))


def t_watch(sec, site, out):
    out += [_head(sec["title"]), sec.get("sub") or "", ""]
    if not sec["records"]:
        out.append("No material changes across your watch list.")
        return
    for x in sec["records"]:
        head = "[%s] %s %s" % (x["status"], x["ticker"], x["price"]["text"])
        out.append(head)
        if x.get("status_basis"):
            out.append(_wrap(x["status_basis"], "  "))
        out.append(_wrap("; ".join(x["reasons"]), "  "))
        if x.get("technical"):
            out.append(_wrap(x["technical"], "  "))
        for k, lbl in (("next_confirmation", "Next confirmation"),
                       ("invalidation", "Invalidation")):
            if x.get(k):
                out.append(_wrap("%s: %s" % (lbl, x[k]), "  "))
        meta = []
        if x.get("flow_quality"):
            meta.append("Flow quality %s" % x["flow_quality"])
        if x.get("evidence"):
            meta.append("Evidence %s" % x["evidence"])
        if x.get("edge"):
            meta.append(x["edge"])
        if x.get("reason_codes"):
            meta.append("Rules: %s" % ", ".join(x["reason_codes"]))
        if meta:
            out.append(_wrap(" · ".join(meta), "  "))
        out.append("  %s" % x["url"])
        out.append("")
    for key in ("overflow_line", "notable_line", "quiet_line"):
        if sec.get(key):
            out.append(_wrap(sec[key]))
            if key == "overflow_line":
                out.append("  %s" % sec["overflow_url"])


def _contract_text(c, indent="  ", show_ticker=True):
    head = "%s%s%s %s %s · %s · %s" % (
        indent, (c["ticker"] + " ") if show_ticker else "",
        c["right"], c["strike"], c["expiry"], c["action"], c["direction"])
    bits = [b for b in (c.get("premium"),
                        ("spot %s" % c["spot"]) if c.get("spot") is not None
                        else "", "sweep" if c.get("sweep") else "") if b]
    if bits:
        head += " · " + " · ".join(str(b) for b in bits)
    sub = []
    if c.get("flow_at"):
        sub.append("printed %s" % c["flow_at"])
    oi = c.get("oi_state") or ""
    if c.get("oi_as_of"):
        oi += " as of %s" % c["oi_as_of"]
    if oi:
        sub.append(oi)
    lines = [head]
    if sub:
        lines.append(indent + "  " + " · ".join(sub))
    return lines


def t_flow_group(sec, site, out):
    out += [_head(sec["title"]), sec.get("sub") or "", ""]
    for r in sec["records"]:
        out.append("  %s — %s (%s)" % (r["ticker"], r["verdict"], r["score"]))
        out.append(_wrap(r["explain"], "    "))
        for c in r["contracts"]:
            out += _contract_text(c, "    ", show_ticker=False)
        if r.get("omitted_line"):
            out.append("    (%s)" % r["omitted_line"])
        out.append("    %s" % r["url"])
        out.append("")


def t_flow_flat(sec, site, out):
    out += [_head(sec["title"]), sec.get("sub") or "", ""]
    for c in sec["records"]:
        out += _contract_text(c, "  ")
        out.append("    %s" % c["url"])


def t_link(sec, site, out):
    r = sec["records"][0]
    out += ["", "  %s: %s" % (r["text"], r["url"])]


def t_event(sec, site, out):
    out += [_head(sec["title"]), sec.get("sub") or "", ""]
    for e in sec["records"]:
        # A fixed-width slice silently deleted the end of long event
        # titles; the complete title now wraps onto continuation lines.
        head = "  %-15s %s" % (e["time_et"], e["title"])
        if len(head) <= WIDTH:
            out.append("%s  %s" % (head, e["status"]))
        else:
            out.append(_wrap("%s %s" % (e["time_et"], e["title"]), "  "))
            out.append("                  %s" % e["status"])
        prov = []
        if e.get("source_time") and e.get("source_tz"):
            prov.append("from %s %s" % (e["source_time"], e["source_tz"]))
        if e.get("venue"):
            prov.append(e["venue"])
        if prov:
            out.append("                  %s" % " · ".join(prov))
        if e.get("url"):
            out.append("                  %s" % e["url"])


def t_news(sec, site, out):
    out += [_head(sec["title"]), sec.get("sub") or "", ""]
    if not sec["records"]:
        out.append(_wrap(sec.get("empty_line") or ""))
        return
    scope = None
    for it in sec["records"]:
        if it["scope"] != scope:
            scope = it["scope"]
            out.append("  %s" % ("Market" if scope == "market"
                                 else "Your names"))
        out.append(_wrap(it["headline"], "    "))
        tick = (" · " + ", ".join(it["tickers"])) if it.get("tickers") else ""
        out.append("      %s · %s · %s%s" % (it["source"], it["published_et"],
                                             it["tier"], tick))
        out.append(_wrap(it["why"], "      "))
        if it.get("url"):
            out.append("      %s" % it["url"])
        out.append("")


def t_prose(sec, site, out):
    out += [_head(sec["title"]), sec.get("sub") or "", ""]
    for r in sec["records"]:
        out.append(_wrap(r["text"]))


def t_discovery(sec, site, out):
    out += [_head(sec["title"]), sec.get("sub") or "", ""]
    for x in sec["records"]:
        out.append("  #%d %s" % (x["rank"], x["ticker"]))
        meta = [x[k] for k in ("contract", "side_label", "premium",
                               "oi_state") if x.get(k)]
        if meta:
            out.append(_wrap(" · ".join(meta), "     "))
        out.append(_wrap(x["why"], "     "))
        out.append("     %s" % x["url"])


def t_followthrough(sec, site, out):
    out += [_head(sec["title"]), sec.get("sub") or "", ""]
    if not sec["records"]:
        out.append(_wrap(sec.get("empty_line") or ""))
        if sec.get("desk_line"):
            out += ["", _wrap(sec["desk_line"]), "  %s" % sec["desk_url"]]
        return
    for r in sec["records"]:
        ratio = r.get("follow_through_ratio")
        out.append("  #%s %s %s %s %s"
                   % (r.get("rank"), r["ticker"],
                      (r.get("right") or "").upper(), r.get("strike"),
                      r.get("expiry")))
        out.append(_wrap("Direction: %s, inferred from %s"
                         % (r.get("direction") or "unresolved",
                            (r.get("action") or "the tape").replace("_", " ")),
                         "     "))
        out.append(_wrap("Structure: %s (%s confidence)"
                         % (r.get("structure") or "",
                            r.get("structure_confidence") or ""), "     "))
        out.append(_wrap("Flow: %s" % (r.get("flow_at") or "-"), "     "))
        out.append(_wrap("Observed: %s contracts · %s"
                         % (_int(r.get("observed_contracts")),
                            _prem(r.get("premium"))), "     "))
        out.append(_wrap("%s EOD OI: %s -> %s · dOI %s"
                         % (r.get("oi_data_date") or "-",
                            _int(r.get("oi_before")), _int(r.get("oi_after")),
                            _int(r.get("delta_oi"), signed=True)), "     "))
        if ratio is not None:
            out.append(_wrap("Follow-through: %s of %s observed contracts "
                             "· %d%%"
                             % (_int(r.get("delta_oi") if
                                     (r.get("delta_oi") or 0) > 0 else 0),
                                _int(r.get("observed_contracts")),
                                round(ratio * 100)), "     "))
        out.append(_wrap("OI status: %s · verified %s"
                         % (r.get("oi_state") or "",
                            r.get("oi_verified_at") or "-"), "     "))
        out.append("     %s" % r["url"])
        out.append("")
    if sec.get("desk_line"):
        out += [_wrap(sec["desk_line"]), "  %s" % sec["desk_url"]]


def _int(n, signed=False):
    if n is None:
        return "-"
    return ("%+d" % int(n)) if signed else "{:,}".format(int(n))


def _prem(p):
    if p is None:
        return "-"
    p = float(p)
    return ("$%.1fM" % (p / 1e6)) if p >= 1e6 else "$%dK" % round(p / 1e3)


def t_earnings(sec, site, out):
    out += [_head(sec["title"]), sec.get("sub") or "", ""]
    out.append("  %-7s %-5s %9s %8s %9s  %s"
               % ("", "When", "Implied", "IV", "Typical", "Read"))
    for r in sec["records"]:
        star = "*" if r.get("on_watchlist") else " "
        imp = ("±%.1f%%" % r["implied_move_pct"])             if r.get("implied_move_pct") is not None else "n/a"
        iv = ("%.0f%%" % r["iv_pct"]) if r.get("iv_pct") is not None else "n/a"
        real = ("±%.1f%%" % r["realized_med_pct"])             if r.get("realized_med_pct") is not None else "n/a"
        out.append("%s %-7s %-5s %9s %8s %9s  %s"
                   % (star, r["ticker"], r["session"], imp, iv, real,
                      r.get("verdict") or "-"))
        if r.get("why"):
            out.append(_wrap(r["why"], "      "))
        out.append("      %s" % r["url"])
    if sec.get("note"):
        out += ["", _wrap(sec["note"])]


RENDERERS = {
    "index": t_market, "watch": t_watch, "flow_group": t_flow_group,
    "flow_flat": t_flow_flat, "link": t_link, "event": t_event,
    "news": t_news, "prose": t_prose, "discovery": t_discovery,
    "earnings": t_earnings, "followthrough": t_followthrough,
}


def render_text(model, *, subject=None):
    """Subject comes from the envelope unless explicitly
    overridden, so the text part cannot announce a different
    email from the one the headers describe."""
    if subject is None:
        subject = model["meta"].get("subject") or ""
    site = model["meta"]["site"]
    unsub = model["meta"]["unsub"]
    out = [subject or "TickerDesk Brief", _rule("=")]
    for sec in model["sections"]:
        fn = RENDERERS.get(sec["kind"])
        if not fn or not (sec.get("records") or sec.get("empty_line")):
            continue
        fn(sec, site, out)
    out += ["", _rule(),
            "Open your desk: %s/#desk" % site,
            "Email settings:  %s/#settings" % site]
    if unsub:
        out.append("Unsubscribe:     %s" % unsub)
    out += ["", _wrap("TickerDesk · educational research, not investment "
                      "advice. Delayed market data. Reply to this email to "
                      "reach a human.")]
    return "\n".join(l for l in out if l is not None) + "\n"


def urls_in(site, unsub_url):
    return [u for u in ("%s/#desk" % site, "%s/#settings" % site,
                        unsub_url) if u]


def self_test():
    import brief_compose as BC
    import brief_render as BR
    fails, ran = [], [0]

    def chk(n, c, d=""):
        ran[0] += 1
        print(("  PASS  " if c else "  FAIL  ") + n
              + ("" if c else "  <- %s" % (d,)))
        if not c:
            fails.append(n)

    model = BR._demo_model()
    site = model["meta"]["site"]
    unsub = model["meta"]["unsub"]
    txt = render_text(model, subject="Risk-off tape · 2 watchlist changes")

    chk("desk URL written out", "%s/#desk" % site in txt)
    chk("settings URL written out", "%s/#settings" % site in txt)
    chk("unsubscribe URL written out", unsub in txt)
    chk("index ticker links written out", "%s/#ticker=SPY" % site in txt)
    chk("watch-list ticker links written out",
        "%s/#ticker=GEV" % site in txt)
    chk("flow link written out", "%s/#flow" % site in txt)
    chk("news source URL written out", "https://reuters.com/x" in txt)
    chk("passes the plain-text gate",
        BC.check_plain_text(txt, urls_in(site, unsub)) == [],
        BC.check_plain_text(txt, urls_in(site, unsub)))
    chk("negative zero normalised", "-0.0%" not in txt)
    chk("no markup leaks", "<" not in txt and "&nbsp;" not in txt)
    chk("no CSS leaks", "font-family" not in txt and "px;" not in txt)
    chk("contract states side and direction", "PUT BUY · bearish" in txt)
    chk("two-sided print labelled unresolved",
        "TWO-SIDED · unresolved" in txt)
    chk("OI state present", "OI PENDING" in txt)
    chk("flow quality label, not Signal", "Signal " not in txt)
    chk("price basis present", "Jul 21 close" in txt)
    chk("20-day summary present", "their 20-day averages" in txt)
    chk("event provenance present", "from 2:30pm UTC" in txt)
    chk("driving-flow section present", "DRIVING TODAY" in txt.upper())
    chk("sections keep the model's order",
        (txt.upper().index("MARKET IN 30 SECONDS")
         < txt.upper().index("YOUR WATCH LIST")
         < txt.upper().index("DRIVING TODAY")
         < txt.upper().index("TOP NEWS")))
    chk("lines stay inside the wrap width",
        max(len(l) for l in txt.split("\n")) <= WIDTH + 12,
        max(len(l) for l in txt.split("\n")))

    print("\n%d/%d checks passed" % (ran[0] - len(fails), ran[0]))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(self_test())
