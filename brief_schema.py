#!/usr/bin/env python3
"""brief_schema.py — the contract the brief must satisfy to be sent.

One envelope, one validator, one place that says what a valid brief looks
like. Everything the subscriber receives — subject, preheader, HTML, plain
text, MIME — is derived from a model that has passed through here, so a
claim cannot exist in one artefact and not the others.

The validator returns a RESULT PER CHECK rather than a bare pass/fail, so
the run can publish what was verified instead of asserting that something
was. `ok` is the conjunction; a False anywhere blocks the send.

    python brief_schema.py --self-test
"""

import hashlib
import json
import re
import sys

SCHEMA = "tickerdesk_daily_brief/v1"

REQUIRED_TOP = ("schema", "meta", "sections", "validation", "artifact_hashes")
REQUIRED_META = ("subject", "preheader", "as_of", "session", "preview")

# Price provenance. A number with no session basis is not a price, it is a
# float that used to be one.
SESSION_BASES = ("pre_market", "prior_close", "live", "flow_spot")

# Regime: internal enums on the left, the ONLY strings a reader ever sees
# on the right. The email previously said "TRANSITION" in the headline and
# "mixed" in changed_from — the same state under two names, in one message.
REGIME_DISPLAY = {
    "risk_on": "RISK-ON",
    "risk-on": "RISK-ON",
    "RISK-ON": "RISK-ON",
    "risk_off": "RISK-OFF",
    "risk-off": "RISK-OFF",
    "RISK-OFF": "RISK-OFF",
    "mixed": "TRANSITION",
    "transition": "TRANSITION",
    "TRANSITION": "TRANSITION",
    "balanced": "BALANCED",
    "BALANCED": "BALANCED",
}
DISPLAY_LABELS = ("RISK-ON", "RISK-OFF", "TRANSITION", "BALANCED")


def regime_display(label):
    """The display label for any internal spelling. Unknown values are
    returned upper-cased rather than silently mapped to something wrong."""
    if not label:
        return ""
    return REGIME_DISPLAY.get(str(label),
                              REGIME_DISPLAY.get(str(label).lower(),
                                                 str(label).upper()))


def content_hash(s):
    if isinstance(s, str):
        s = s.encode("utf-8")
    return hashlib.sha256(s).hexdigest()


def record_key(*parts):
    """A stable identity for a record. Headline prefixes were used before,
    so two stories sharing an opening clause collided and an edited
    headline became a different record."""
    raw = "|".join(str(p or "") for p in parts)
    return content_hash(raw)[:16]


def artifact_hashes(html="", text="", eml=b"", model=None):
    """Hashes of the things actually delivered, plus the model they came
    from, so a mismatch between the .eml and the standalone HTML is
    detectable after the fact."""
    out = {"html": content_hash(html) if html else "",
           "text": content_hash(text) if text else "",
           "eml": content_hash(eml) if eml else ""}
    if model is not None:
        skinny = {"schema": model.get("schema"),
                  "meta": model.get("meta"),
                  "sections": model.get("sections")}
        out["model"] = content_hash(
            json.dumps(skinny, sort_keys=True, default=str))
    return out


# ── validation ──────────────────────────────────────────────────────────

def _r(name, ok, detail=""):
    return {"check": name, "ok": bool(ok), "detail": str(detail)}


def validate_model(model):
    """Structural validation of the envelope and every record in it."""
    res = []
    m = model or {}
    res.append(_r("schema.version", m.get("schema") == SCHEMA,
                  m.get("schema")))
    missing = [k for k in REQUIRED_TOP if k not in m]
    res.append(_r("schema.top_level_keys", not missing,
                  "missing %s" % missing if missing else "all present"))

    meta = m.get("meta") or {}
    mm = [k for k in REQUIRED_META if k not in meta]
    res.append(_r("schema.meta_keys", not mm,
                  "missing %s" % mm if mm else "all present"))
    res.append(_r("meta.subject_present", bool((meta.get("subject") or "").strip()),
                  meta.get("subject")))
    res.append(_r("meta.preheader_present",
                  bool((meta.get("preheader") or "").strip()),
                  meta.get("preheader")))

    secs = m.get("sections") or []
    res.append(_r("schema.sections_present", bool(secs), len(secs)))
    bad_keys = []
    for s in secs:
        for k in ("id", "kind", "records"):
            if k not in s:
                bad_keys.append("%s.%s" % (s.get("id", "?"), k))
        for r in s.get("records") or []:
            if not r.get("key"):
                bad_keys.append("%s record without key" % s.get("id"))
    res.append(_r("schema.section_shape", not bad_keys, bad_keys[:4]))

    # record keys unique within a section
    dupes = []
    for s in secs:
        seen = set()
        for r in s.get("records") or []:
            k = r.get("key")
            if k in seen:
                dupes.append("%s/%s" % (s["id"], k))
            seen.add(k)
    res.append(_r("schema.record_keys_unique", not dupes, dupes[:4]))

    res.extend(validate_calendar(model))
    res.extend(validate_prices(model))
    res.extend(validate_flow_counts(model))
    res.extend(validate_regime_vocabulary(model))
    res.extend(validate_news(model))
    return res


def validate_calendar(model):
    """A corrected event must carry its whole provenance, and must never
    link to the record it corrected."""
    prob = []
    for s in (model or {}).get("sections") or []:
        if s.get("kind") != "event":
            continue
        for e in s.get("records") or []:
            if not e.get("corrected"):
                continue
            who = e.get("title") or e.get("key")
            for f in ("corrected_title", "corrected_start",
                      "correction_authority", "correction_timestamp",
                      "correction_reason"):
                if not (e.get(f) or "").strip():
                    prob.append("%s: correction missing %s" % (who, f))
            if not (e.get("vendor_title") or "").strip():
                prob.append("%s: vendor record not preserved" % who)
            url = (e.get("url") or "").strip()
            if url and url == (e.get("vendor_url") or "").strip():
                prob.append("%s: corrected row links to the vendor page it "
                            "corrects" % who)
            if url and url != (e.get("correction_source_url") or "").strip():
                prob.append("%s: link is not the correction's source" % who)
    return [_r("calendar.correction_provenance", not prob,
               prob[:3] or "clean")]


def validate_prices(model):
    bad, checked = [], 0
    for s in (model or {}).get("sections") or []:
        for r in s.get("records") or []:
            p = r.get("price")
            if not isinstance(p, dict):
                continue
            checked += 1
            who = "%s/%s" % (s["id"], r.get("ticker") or r.get("key"))
            if p.get("value") is None:
                # an unavailable price must say so and why
                if not (p.get("unavailable_reason") or "").strip():
                    bad.append("%s: no value and no reason" % who)
                continue
            if p.get("session_basis") not in SESSION_BASES:
                bad.append("%s: session_basis %r not one of %s"
                           % (who, p.get("session_basis"), SESSION_BASES))
            if not (p.get("as_of") or "").strip():
                bad.append("%s: priced but not timestamped" % who)
            if not (p.get("source") or "").strip():
                bad.append("%s: no price source" % who)
            if not (p.get("stale_after") or "").strip():
                bad.append("%s: no staleness horizon" % who)
    return [_r("prices.timestamped_and_sourced", not bad,
               "%d checked; %s" % (checked, bad[:3] if bad else "clean"))]


def validate_flow_counts(model):
    """The counts a reader can add up must add up."""
    prob = []
    for s in (model or {}).get("sections") or []:
        if s.get("kind") != "flow_group":
            continue
        for r in s.get("records") or []:
            who = "%s/%s" % (s["id"], r.get("ticker"))
            tot = r.get("contract_count_total")
            disp = r.get("contract_count_displayed")
            om = r.get("contract_count_omitted")
            conf = r.get("confirmed_count")
            pend = r.get("pending_count")
            unres = r.get("unresolved_count")
            if None in (tot, disp, om, conf, pend, unres):
                prob.append("%s: missing count fields" % who)
                continue
            if conf + pend + unres != tot:
                prob.append("%s: confirmed+pending+unresolved (%d+%d+%d) "
                            "!= total %d" % (who, conf, pend, unres, tot))
            if disp + om != tot:
                prob.append("%s: displayed+omitted (%d+%d) != total %d"
                            % (who, disp, om, tot))
            if len(r.get("contracts") or []) != disp:
                prob.append("%s: %d contracts rendered but displayed=%d"
                            % (who, len(r.get("contracts") or []), disp))
            if om and not (r.get("omitted_line") or "").strip():
                prob.append("%s: %d contracts omitted with no disclosure"
                            % (who, om))
    return [_r("flow.population_reconciles", not prob, prob[:3] or "clean")]


def validate_regime_vocabulary(model):
    """One vocabulary. Any internal enum reaching a display field is a bug
    the reader experiences as two different market calls."""
    leaks = []
    internal = {"risk_on", "risk_off", "mixed", "transition", "balanced"}
    for s in (model or {}).get("sections") or []:
        for field in ("regime", "weekly_regime"):
            reg = s.get(field) or {}
            if not isinstance(reg, dict):
                continue
            for k in ("label", "changed_from", "display", "prior_display"):
                v = reg.get(k)
                if isinstance(v, str) and v in internal:
                    leaks.append("%s.%s.%s=%r" % (s["id"], field, k, v))
                if k in ("label", "display", "prior_display") and v and \
                        v not in DISPLAY_LABELS and v not in internal:
                    leaks.append("%s.%s.%s=%r not a display label"
                                 % (s["id"], field, k, v))
    return [_r("regime.single_vocabulary", not leaks, leaks[:4] or "clean")]


def validate_news(model):
    prob = []
    sec = None
    for s in (model or {}).get("sections") or []:
        if s.get("id") == "news":
            sec = s
    if sec is None:
        return [_r("news.section_present", False, "no news section")]
    recs = sec.get("records") or []
    for r in recs:
        why = r.get("why") or ""
        if len(why) > 140:
            prob.append("%s: why is %d chars" % (r.get("key"), len(why)))
        # a truncated sentence must announce itself
        if why and not re.search(r"[.!?…]$", why.strip()):
            prob.append("%s: why ends mid-sentence: %r" % (r.get("key"),
                                                           why[-24:]))
        if re.search(r"\w…$", why) and not re.search(r"\s…$|\w\w…$", why):
            prob.append("%s: ellipsis mid-word" % r.get("key"))
    if recs:
        pubs = {}
        for r in recs:
            pubs[r.get("source")] = pubs.get(r.get("source"), 0) + 1
        top, n = max(pubs.items(), key=lambda kv: kv[1])
        if n * 2 > len(recs) and len(pubs) > 1:
            prob.append("%s supplies %d of %d displayed stories"
                        % (top, n, len(recs)))
    return [_r("news.formatting_and_diversity", not prob, prob[:3] or "clean")]


def summarise(results):
    ok = all(r["ok"] for r in results)
    return {"ok": ok,
            "passed": sum(1 for r in results if r["ok"]),
            "failed": sum(1 for r in results if not r["ok"]),
            "checks": results}


def failures(results):
    return ["%s: %s" % (r["check"], r["detail"])
            for r in results if not r["ok"]]


# ── self-test ───────────────────────────────────────────────────────────

def _min_model(**over):
    m = {
        "schema": SCHEMA,
        "meta": {"subject": "S", "preheader": "P", "as_of": "2026-07-22",
                 "session": "Pre-Market Brief", "preview": True},
        "sections": [
            {"id": "news", "kind": "news", "records": [
                {"key": "a1", "source": "Reuters", "why": "Rates in doubt."},
                {"key": "a2", "source": "Fed", "why": "Committee split."}]},
            {"id": "flow_driving", "kind": "flow_group", "records": [
                {"key": "MU", "ticker": "MU", "contract_count_total": 3,
                 "contract_count_displayed": 2, "contract_count_omitted": 1,
                 "confirmed_count": 1, "pending_count": 2,
                 "unresolved_count": 0, "omitted_line": "showing 2 of 3",
                 "contracts": [{"key": "c1"}, {"key": "c2"}]}]},
            {"id": "watchlist", "kind": "watch", "records": [
                {"key": "MU", "ticker": "MU",
                 "price": {"value": 118.2, "session_basis": "pre_market",
                           "as_of": "2026-07-22 07:20 ET",
                           "source": "worker/quote",
                           "stale_after": "2026-07-22 09:30 ET"}}],
             "regime": {}},
        ],
        "validation": {}, "artifact_hashes": {},
    }
    m.update(over)
    return m


def self_test():
    fails, ran = [], [0]

    def chk(n, c, d=""):
        ran[0] += 1
        print(("  PASS  " if c else "  FAIL  ") + n
              + ("" if c else "  <- %s" % (d,)))
        if not c:
            fails.append(n)

    good = _min_model()
    res = validate_model(good)
    chk("a well-formed model validates", summarise(res)["ok"], failures(res))

    bad = _min_model(schema="something/else")
    chk("a wrong schema version is caught",
        not summarise(validate_model(bad))["ok"])

    m = _min_model()
    del m["meta"]["preheader"]
    chk("a missing meta key is caught",
        not summarise(validate_model(m))["ok"])

    m = _min_model()
    m["sections"][1]["records"][0]["pending_count"] = 5
    chk("confirmed+pending+unresolved != total is caught",
        not summarise(validate_model(m))["ok"])

    m = _min_model()
    m["sections"][1]["records"][0]["contract_count_omitted"] = 0
    chk("displayed+omitted != total is caught",
        not summarise(validate_model(m))["ok"])

    m = _min_model()
    m["sections"][1]["records"][0]["omitted_line"] = ""
    chk("omitted contracts with no disclosure are caught",
        not summarise(validate_model(m))["ok"])

    m = _min_model()
    m["sections"][2]["records"][0]["price"]["as_of"] = ""
    chk("a priced value with no timestamp is caught",
        not summarise(validate_model(m))["ok"])
    m = _min_model()
    m["sections"][2]["records"][0]["price"]["session_basis"] = "quote"
    chk("an unknown session basis is caught",
        not summarise(validate_model(m))["ok"])
    m = _min_model()
    m["sections"][2]["records"][0]["price"] = {"value": None}
    chk("an unavailable price with no reason is caught",
        not summarise(validate_model(m))["ok"])

    m = _min_model()
    m["sections"][2]["regime"] = {"label": "TRANSITION",
                                  "changed_from": "risk_off"}
    chk("an internal enum leaking into a display field is caught",
        not summarise(validate_model(m))["ok"],
        validate_regime_vocabulary(m))
    m["sections"][2]["regime"] = {"label": "TRANSITION",
                                  "changed_from": "RISK-OFF"}
    chk("display labels on both fields validate",
        summarise(validate_model(m))["ok"], failures(validate_model(m)))

    chk("mixed maps to the transition display label",
        regime_display("mixed") == "TRANSITION")
    chk("risk_off maps to RISK-OFF", regime_display("risk_off") == "RISK-OFF")
    chk("an already-display label is idempotent",
        regime_display("RISK-ON") == "RISK-ON")

    m = _min_model()
    m["sections"][0]["records"][0]["why"] = "x" * 200
    chk("an over-long why is caught",
        not summarise(validate_model(m))["ok"])
    m = _min_model()
    m["sections"][0]["records"][0]["why"] = "Ends abruptly with no stop"
    chk("a why ending mid-sentence is caught",
        not summarise(validate_model(m))["ok"])
    m = _min_model()
    for r in m["sections"][0]["records"]:
        r["source"] = "The Motley Fool"
    m["sections"][0]["records"].append({"key": "a3", "source": "Reuters",
                                        "why": "Third."})
    chk("one publisher over half the stories is caught",
        not summarise(validate_model(m))["ok"],
        validate_news(m))

    m = _min_model()
    m["sections"][0]["records"][1]["key"] = "a1"
    chk("duplicate record keys are caught",
        not summarise(validate_model(m))["ok"])

    h = artifact_hashes(html="<p>x</p>", text="x", eml=b"raw", model=good)
    chk("artifact hashes cover html, text, eml and model",
        all(h[k] for k in ("html", "text", "eml", "model")), h)
    chk("hashes are stable",
        artifact_hashes(html="<p>x</p>")["html"] == h["html"])
    chk("hashes change with content",
        artifact_hashes(html="<p>y</p>")["html"] != h["html"])
    chk("record keys are content-addressed, not prefixes",
        record_key("https://a/1", "Same opening clause here")
        != record_key("https://a/2", "Same opening clause here"))

    print("\n%d/%d checks passed" % (ran[0] - len(fails), ran[0]))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(self_test())
