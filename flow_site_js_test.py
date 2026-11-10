"""Options Flow site logic (docs/index.html), run in Node.

Extracts the real functions from the page and feeds them rows shaped like
uoa_latest.json, so the tier pills, Opening Buyers count, break-even
direction and move-since-print colouring are checked against published
field names. Skipped when Node is not installed."""

import json
import os
import shutil
import subprocess
import tempfile

import pytest

HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "index.html")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def _extract(src, name):
    """Source of `function name(...) {...}` with brace matching that skips
    string literals and // comments."""
    start = src.index(f"function {name}(")
    i = src.index("{", start)
    depth, q = 0, None
    while True:
        ch = src[i]
        if q:
            if ch == "\\":
                i += 2
                continue
            if ch == q:
                q = None
        elif ch in "\"'`":
            q = ch
        elif ch == "/" and src[i + 1] == "/":
            i = src.index("\n", i)
            continue
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1


def _run(names, body):
    with open(HTML, encoding="utf-8") as f:
        src = f.read()
    js = "\n".join(_extract(src, n) for n in names) + "\n" + body
    # a UTF-8 file, not `node -e`: the Windows command line mangles ▲/▼

    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as f:
        f.write(js)
    try:
        out = subprocess.run(
            [NODE, f.name], capture_output=True, encoding="utf-8", check=True, timeout=30
        )
    finally:
        os.unlink(f.name)
    return json.loads(out.stdout)


def _row(**kw):
    r = {
        "ticker": "TST",
        "type": "call",
        "premium": 500000,
        "dte": 30,
        "earnings_days": None,
        "liquidity": "B",
        "opening": "likely_open",
        "repeat_count": 0,
        "golden": False,
        "flow_side": "call_buyer",
        "direction": "bullish",
        "cap_class": "smid",
    }
    r.update(kw)
    return r


def test_classify_tier_reads_real_fields():
    rows = {
        "conviction": _row(golden=True),
        "caution": _row(liquidity="D"),
        "earnings": _row(dte=5, earnings_days=3),
        "earnings_after_expiry": _row(dte=5, earnings_days=9, golden=True),
        "seller_not_conviction": _row(flow_side="call_seller", direction="income", golden=True),
        "not_opening": _row(opening="likely_close", golden=True),
    }
    got = _run(
        ["classifyTier"],
        "const rows=" + json.dumps(rows) + ";const o={};"
        "for(const k in rows){o[k]=classifyTier(rows[k]).label;}"
        "console.log(JSON.stringify(o));",
    )
    assert got["conviction"] == "A+ Conviction"
    assert got["caution"] == "Caution"
    assert got["earnings"] == "Earnings Bet"
    assert got["earnings_after_expiry"] == "A+ Conviction"
    assert got["seller_not_conviction"] != "A+ Conviction"
    assert got["not_opening"] != "A+ Conviction"


def test_opening_buyers_counts_only_opening_buyer_rows():
    rows = [
        _row(),
        _row(flow_side="put_buyer", direction="bearish"),
        _row(flow_side="call_seller", direction="income"),
        _row(flow_side="mixed", direction="hedge"),
        _row(flow_side="unknown", direction="hedge"),
        _row(opening="likely_close"),
    ]
    got = _run(
        ["isOpeningBuyer"],
        "console.log(JSON.stringify(" + json.dumps(rows) + ".filter(isOpeningBuyer).length));",
    )
    assert got == 2


def test_break_even_shows_the_stocks_required_direction():
    rows = [
        {"type": "call", "be_distance_pct": 4.2, "break_even": 104.2},
        {"type": "put", "be_distance_pct": 4.2, "break_even": 95.8},
        {"type": "put", "be_distance_pct": -1.5, "break_even": 95.8},
        {"type": "call", "be_distance_pct": None},
    ]
    got = _run(
        ["uoaBeHtml"],
        "console.log(JSON.stringify(" + json.dumps(rows) + ".map(uoaBeHtml)));",
    )
    assert "▲ +4.2%" in got[0] and "RISE" in got[0]
    assert "▼ −4.2%" in got[1] and "FALL" in got[1]
    assert ">past<" in got[2] and "below" in got[2]
    assert got[3] == "&mdash;"


def test_move_since_print_is_direction_aware_and_labels_scan_fallback():
    got = _run(
        ["uoaMoveRef", "uoaMoveCls"],
        "console.log(JSON.stringify({"
        "printRef: uoaMoveRef({spot_at_print: 50, spot: 55}),"
        "scanRef: uoaMoveRef({spot_at_print: null, spot: 55}),"
        "none: uoaMoveRef({}),"
        "bullUp: uoaMoveCls({direction:'bullish'}, 2),"
        "bearDown: uoaMoveCls({direction:'bearish'}, -2),"
        "bearUp: uoaMoveCls({direction:'bearish'}, 2),"
        "hedge: uoaMoveCls({direction:'hedge'}, 2)}));",
    )
    assert got["printRef"] == {"px": 50, "basis": "print"}
    assert got["scanRef"] == {"px": 55, "basis": "scan"}
    assert got["none"] is None
    assert got["bullUp"] == "uoa-delta-up"
    assert got["bearDown"] == "uoa-delta-up"  # a fall is good for put flow
    assert got["bearUp"] == "uoa-delta-down"
    assert got["hedge"] == "uoa-delta-flat"
