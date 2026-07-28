#!/usr/bin/env python3
"""test_v5_no_ticker_branches.py — the universal-ticker enforcement scan.

Shared v5 production logic must contain no pilot symbols, pilot company
names, or ticker-keyed branches. Pilot identifiers may appear only in
fixtures/tests, assumptions files, documentation, and generated
artifacts. This scan IS one of the spec's validation checks
(NO_TICKER_SPECIFIC_BRANCH / PILOT_SYMBOL_NOT_PRESENT_IN_SHARED_LOGIC):
scan() is imported by the validator and run on every package, and this
script wraps it as the standalone test.

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
    "report_v5_run.py", "report_v5_expectations.py",
    "report_v5_assessment.py", "report_v5_memory.py",
    "report_v5_framework.py", "report_v5_adapters.py",
    "report_v5_ledger.py", "report_v5_appendix.py",
    "report_v5_checks.py",
)

PILOTS = ("NOW", "SG", "HOOD", "SPCX", "MRVL")
COMPANY_NAMES = ("ServiceNow", "Sweetgreen", "Robinhood", "SpaceX",
                 "Marvell")


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


def scan_module(path, mod):
    """-> (string_hits, branch_hits) for one shared module."""
    strings, src = executable_strings(path)
    hits = []
    for ln, s in strings:
        for p in PILOTS:
            if re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % p, s):
                hits.append("%s:%d %r" % (mod, ln, s[:50]))
        for n in COMPANY_NAMES:
            if n in s:
                hits.append("%s:%d %r" % (mod, ln, s[:50]))
    tree = ast.parse(src)
    branch_hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for comp in node.comparators:
                if isinstance(comp, ast.Constant) \
                        and isinstance(comp.value, str) \
                        and comp.value in PILOTS:
                    branch_hits.append("%s:%d" % (mod, node.lineno))
    return hits, branch_hits


def scan(base=None):
    """-> list of all violations across shared modules (empty = clean).
    Called by the validator as NO_TICKER_SPECIFIC_BRANCH."""
    base = base or os.path.dirname(os.path.abspath(__file__))
    bad = []
    for mod in SHARED_MODULES:
        path = os.path.join(base, mod)
        if not os.path.exists(path):
            continue
        hits, branch_hits = scan_module(path, mod)
        bad += hits + ["branch %s" % b for b in branch_hits]
    return bad


def main():
    PASS, FAIL = 0, []

    def check(name, ok, detail=""):
        nonlocal PASS
        if ok:
            PASS += 1
            print("  PASS  %s" % name)
        else:
            FAIL.append(name)
            print("  FAIL  %s  %s" % (name, detail))

    base = os.path.dirname(os.path.abspath(__file__))
    for mod in SHARED_MODULES:
        path = os.path.join(base, mod)
        if not os.path.exists(path):
            continue
        hits, branch_hits = scan_module(path, mod)
        check("%s: no pilot identifiers in executable strings" % mod,
              not hits, "; ".join(hits[:4]))
        check("%s: no comparisons against pilot symbols" % mod,
              not branch_hits, "; ".join(branch_hits))
    print("\n%d/%d checks passed" % (PASS, PASS + len(FAIL)))
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
