#!/usr/bin/env python3
"""test_v5_scenarios.py — slice-2 proof: scenario arithmetic recomputes,
ASM is labelled with a basis, expired assumptions drop loudly, and
probabilities cannot exist without a user file."""

import json
import os
import sys
import tempfile

import report_v5_scenarios as S

PASS, FAIL = 0, []


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL.append(name)
        print("  FAIL  %s  %s" % (name, detail))


BAND = {"pe": {"available": True, "kind": "pe", "window_years": 3,
               "window_start": "2023-07-01", "window_end": "2026-07-25",
               "coverage": 1.0, "p25": 20.0, "p50": 25.0, "p75": 30.0,
               "current": 25.0},
        "ps": {"available": False, "reason": "x"}}

# ── 1. arithmetic: every price = multiple x metric, recomputable ─────
rec = S.build("TEST", BAND, spot=100.0)
metric = 100.0 / 25.0                                   # 4.0
check("metric derived from the band's own current multiple",
      abs(rec["rows"][0]["metric"]["value"] - metric) < 1e-9,
      rec["rows"][0]["metric"])
ok = all(abs(r["price"] - r["multiple"]["value"]
             * r["metric"]["value"]) < 0.01 for r in rec["rows"])
check("every scenario price recomputes from its own row", ok,
      json.dumps(rec["arithmetic"]))
check("bear/base/bull = P25/P50/P75",
      [r["multiple"]["value"] for r in rec["rows"]] == [20.0, 25.0, 30.0])
check("no probabilities without a user file", rec["weighted"] is None)
check("default cells are DER with a stated basis",
      all(r["multiple"]["grade"] == "DER" and r["multiple"]["basis"]
          for r in rec["rows"]))

# ── 2. withheld when no band survived ────────────────────────────────
none_rec = S.build("TEST", {"pe": {"available": False, "reason": "thin"},
                            "ps": {"available": False, "reason": "thin"}},
                   spot=100.0)
check("no band -> withheld with both reasons",
      none_rec["available"] is False and "thin" in none_rec["reason"])

# ── 3. assumptions contract ──────────────────────────────────────────
tmp = tempfile.mkdtemp()
S.ASSUMPTIONS_DIR = tmp


def write_asm(doc):
    with open(os.path.join(tmp, "TEST.json"), "w") as f:
        json.dump(doc, f)


GOOD = {"schema": "v5-assumptions/1", "as_of": "2026-07-01",
        "expires": "2026-12-31", "source": "user", "currency": "USD",
        "units": "per-share", "fiscal_basis": "FY-Dec",
        "fields": {"base_multiple": {"value": 17.0, "basis": "my view"},
                   "probabilities": {"bear": 0.2, "base": 0.5,
                                     "bull": 0.3}}}
write_asm(GOOD)
asm, note = S.load_assumptions("TEST", today="2026-07-28")
check("valid assumptions load", asm is not None and note is None,
      str(note))
rec2 = S.build("TEST", BAND, spot=100.0, assumptions=asm, note=note)
base = [r for r in rec2["rows"] if r["leg"] == "base"][0]
check("override renders ASM with basis + as-of",
      base["multiple"]["grade"] == "ASM"
      and "my view" in base["multiple"]["basis"]
      and "2026-07-01" in base["multiple"]["basis"],
      base["multiple"])
check("un-overridden legs stay DER",
      [r for r in rec2["rows"] if r["leg"] == "bear"
       ][0]["multiple"]["grade"] == "DER")
check("probabilities only via file, and weighted price recomputes",
      rec2["weighted"] is not None
      and abs(rec2["weighted"]["price"]
              - (0.2 * 80 + 0.5 * 68 + 0.3 * 120)) < 0.01,
      str(rec2["weighted"]))

# ── phase E: EV, asymmetry, annualized ───────────────────────────────
w2 = rec2["weighted"]
check("EV contributions recompute (sum == price)",
      abs(sum(w2["expected_value_contribution"].values())
          - w2["price"]) < 0.02, str(w2["expected_value_contribution"]))
check("expected return derived from EV vs spot",
      abs(w2["expected_return_pct"]
          - round(100.0 * (w2["price"] / 100.0 - 1), 1)) < 0.11)
check("no horizon -> no annualized figure",
      w2["annualized_return_pct"] is None)
check("weighted carries the uncertainty caveat",
      "uncertainty" in (w2.get("caveat") or ""))
asym = rec2["asymmetry"]
check("asymmetry block: bear/bull vs spot with ratio",
      asym["downside_to_bear_pct"] == -20.0
      and asym["upside_to_bull_pct"] == 20.0
      and asym["up_down_ratio"] == 1.0, str(asym))

hz = json.loads(json.dumps(GOOD))
hz["fields"]["horizon_years"] = {"value": 3, "basis": "underwriting"}
write_asm(hz)
asm_h, _ = S.load_assumptions("TEST", today="2026-07-28")
rec_h = S.build("TEST", BAND, spot=100.0, assumptions=asm_h)
wh = rec_h["weighted"]
check("horizon -> annualized return computed",
      wh["annualized_return_pct"] is not None
      and abs(wh["annualized_return_pct"]
              - 100.0 * ((wh["price"] / 100.0) ** (1 / 3.0) - 1)) < 0.11,
      str(wh["annualized_return_pct"]))

write_asm({**GOOD, "expires": "2026-07-27"})
asm3, note3 = S.load_assumptions("TEST", today="2026-07-28")
check("expired assumptions dropped with a loud note",
      asm3 is None and "expired" in (note3 or ""), str(note3))

write_asm({**GOOD, "schema": "v9-wrong"})
asm4, note4 = S.load_assumptions("TEST", today="2026-07-28")
check("wrong schema version dropped with a note",
      asm4 is None and "schema" in (note4 or ""), str(note4))

bad = dict(GOOD)
bad["fields"] = {"base_multiple": {"value": 17.0}}       # no basis
write_asm(bad)
asm5, note5 = S.load_assumptions("TEST", today="2026-07-28")
check("assumption without a basis rejects the file",
      asm5 is None and "basis" in (note5 or ""), str(note5))

bad_p = json.loads(json.dumps(GOOD))
bad_p["fields"]["probabilities"] = {"bear": 0.5, "base": 0.6}
write_asm(bad_p)
asm6, _ = S.load_assumptions("TEST", today="2026-07-28")
rec6 = S.build("TEST", BAND, spot=100.0, assumptions=asm6)
check("malformed probabilities ignored, noted, never weighted",
      rec6["weighted"] is None
      and "probabilities ignored" in (rec6["assumptions_note"] or ""),
      str(rec6["assumptions_note"]))

print("\n%d/%d checks passed" % (PASS, PASS + len(FAIL)))
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
