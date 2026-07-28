#!/usr/bin/env python3
"""test_v5_no_ticker_branches.py — the universal-ticker enforcement scan.

Shared v5 production logic must contain no pilot symbols, pilot company
names, or ticker-keyed branches. Pilot identifiers may appear only in
fixtures/tests, assumptions files, documentation, and generated
artifacts. This scan IS one of the spec's validation checks
(NO_TICKER_SPECIFIC_BRANCH / PILOT_SYMBOL_NOT_PRESENT_IN_SHARED_LOGIC).

Docstrings and comments are allowed to NAME a pilot when explaining a
lesson learned (e.g. "the ServiceNow 5:1 split bug") — what is banned
is pilot identifiers in EXECUTABLE code.
"""

import ast
import io
import os
import re
import sys

SHARED_MODULES = (
    "report_v5_multiples.py", "report_v5_scenarios.py",
    "report_v5_archetype.py", "report_v5_capability.py",
    "report_v5_claims.py", "report_v5_grid.py", "report_v5.py",
    "report_v5_run.py",
)

PILOTS = ("NOW", "SG", "HOOD", "SPCX", "MRVL")
COMPANY_NAMES = ("ServiceNow", "Sweetgreen", "Robinhood", "SpaceX",
                 "Marvell")

PASS, FAIL = 0, []


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL.append(name)
        print("  FAIL  %s  %s" % (name, detail))


def executable_strings(path):
    """String literals reachable by execution — docstrings excluded."""
    src = io.open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    doc_lines = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                for ln in range(body[0].lineno,
                                body[0].end_lineno + 1):
                    doc_lines.add(ln)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.lineno not in doc_lines:
                out.append((node.lineno, node.value))
    return out, src


base = os.path.dirname(os.path.abspath(__file__))
for mod in SHARED_MODULES:
    path = os.path.join(base, mod)
    strings, src = executable_strings(path)
    hits = []
    for ln, s in strings:
        for p in PILOTS:
            # a pilot symbol as a discrete token in an executable string
            if re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % p, s):
                hits.append("%s:%d %r" % (mod, ln, s[:50]))
        for n in COMPANY_NAMES:
            if n in s:
                hits.append("%s:%d %r" % (mod, ln, s[:50]))
    check("%s: no pilot identifiers in executable strings" % mod,
          not hits, "; ".join(hits[:4]))

    # comparison branches keyed to a symbol-looking literal
    tree = ast.parse(src)
    branch_hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for comp in node.comparators:
                if isinstance(comp, ast.Constant) \
                        and isinstance(comp.value, str) \
                        and comp.value in PILOTS:
                    branch_hits.append("%s:%d" % (mod, node.lineno))
    check("%s: no comparisons against pilot symbols" % mod,
          not branch_hits, "; ".join(branch_hits))

print("\n%d/%d checks passed" % (PASS, PASS + len(FAIL)))
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
