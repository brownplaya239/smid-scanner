"""landing_pages.py builds the SEO landing pages and /transparency from the
published report JSON. These tests build them from fixture JSON shaped like
the live files and check that every quoted number comes from the data, that a
missing source drops its stat instead of showing an old one, and that the
scan cadence matches the worker that actually fires the scans."""

import importlib
import json
import os
import re

import pytest

import landing_pages

ROOT = os.path.dirname(os.path.abspath(__file__))

EDGE = {
    "generated": "2026-11-16T19:17:37+00:00",
    "total_signals": 33235,
    "matured_5d": 20402,
    "oldest_signal_ts": "2026-05-18T18:44:44+00:00",
    "rich_overall": {
        "5": {
            "n": 20402,
            "status": "active",
            "ev": -0.23,
            "win_rate": 48,
            "avg_win": 6.99,
            "avg_loss": -6.91,
            "profit_factor": 0.94,
            "ci95": [-0.51, 0.05],
        },
        "10": {"n": 19337, "status": "active", "ev": -0.6, "ci95": [-1.04, -0.17]},
        "20": {"n": 17090, "status": "active", "ev": -1.02, "ci95": [-1.62, -0.42]},
    },
    "ic": {"n": 20402, "ic_spearman": 0.009},
    "score_deciles": {"status": "active", "top_minus_bottom": 0.22},
    "calibration": {
        "80-100": {"status": "accruing"},
        "65-79": {"drift": -6},
        "55-64": {"drift": -3},
    },
    "by_type": {
        "golden_sweep": {
            "rich": {
                "n": 2468,
                "status": "active",
                "ev": -0.48,
                "win_rate": 47,
                "profit_factor": 0.88,
                "ci95": [-1.13, 0.17],
            }
        }
    },
    "by_tag": {
        "Into ERN": {
            "rich": {
                "n": 4758,
                "status": "active",
                "ev": 0.07,
                "win_rate": 48,
                "ci95": [-0.44, 0.59],
            }
        },
        "Repeat": {"rich": {"n": 12, "status": "accruing"}},
    },
    "oi_confirmation": {"checked": 1106, "confirm_rate": 41},
}
RESEARCH = {
    "attribution": {
        "books": {
            "put_buy": {
                "status": "active",
                "n": 6913,
                "hit": 54.0,
                "dir_ev": 0.314,
                "flow_alpha": -0.004,
                "flow_alpha_ci95": [-0.41, 0.402],
            }
        }
    }
}
HLAB = {
    "generated": "2026-11-14T03:27:49+00:00",
    "status": "active",
    "n_tested": 192,
    "survivors": [{}, {}, {}],
    "graveyard": 189,
    "multiple_testing": {"bh_survivors": 1},
}
SCANS = {"overall": {"status": "active", "n": 1484, "win_rate": 43, "ev": -1.81, "hold_d": 5}}
LEVELS = {"sessions": 339}
TOP25 = {"universe_total": 645}

FULL = {
    "uoa_edge.json": EDGE,
    "uoa_research.json": RESEARCH,
    "hypothesis_lab.json": HLAB,
    "scan_outcomes.json": SCANS,
    "level_stats.json": LEVELS,
    "uoa_top25_daily.json": TOP25,
}


@pytest.fixture
def build(tmp_path, monkeypatch):
    """build(files) -> {page name: html} generated from those report JSONs."""

    def _build(files):
        rep, out = tmp_path / "reports", tmp_path / "out"
        rep.mkdir(exist_ok=True)
        out.mkdir(exist_ok=True)
        for name, obj in files.items():
            (rep / name).write_text(json.dumps(obj), encoding="utf-8")
        monkeypatch.setenv("TD_REPORTS_DIR", str(rep))
        monkeypatch.setenv("TD_SITE_OUT", str(out))
        importlib.reload(landing_pages)
        landing_pages.main()
        return {p.stem: p.read_text(encoding="utf-8") for p in out.iterdir() if p.suffix == ".html"}

    yield _build
    monkeypatch.delenv("TD_REPORTS_DIR", raising=False)
    monkeypatch.delenv("TD_SITE_OUT", raising=False)
    importlib.reload(landing_pages)


def test_transparency_quotes_live_numbers_with_as_of_date(build):
    t = build(FULL)["transparency"]
    assert "Numbers as of November 16, 2026" in t
    assert "33,235" in t and "since May 18, 2026" in t
    assert "&minus;0.23%" in t and "48%" in t and "profit factor 0.94" in t
    assert "&minus;0.51% to +0.05%" in t and "coin flip" in t
    # golden sweeps use their own cohort, sign included
    assert re.search(r"Golden sweeps[^<]*</td><td>47%</td><td>&minus;0.48%", t)
    assert "2,468" in t
    assert "0.009" in t
    assert "tested 192" in t and "3 survived" in t and "1 still passes" in t
    assert "&minus;1.81%" in t and "1,484" in t
    assert "&minus;6 to &minus;3 points" in t
    # puts look right only because of drift: the flow adds ~0
    assert "flow itself adds 0.00%" in t and "drifted down anyway" in t
    # the July hand-written figures are gone
    for stale in ("79,245", "July 17", "18,358", "268", "&minus;0.03"):
        assert stale not in t


def test_transparency_drops_stats_whose_source_is_missing(build):
    t = build({})["transparency"]
    assert "Numbers as of" not in t
    assert "The headline" not in t and "By cohort" not in t
    assert "hypothesis lab" not in t and "Momentum lists" not in t
    assert not re.search(r"\d+\.\d+%", t)
    assert "How the grading works" in t


def test_transparency_skips_accruing_cohorts(build):
    t = build(FULL)["transparency"]
    assert "Repeat buyers" not in t  # status accruing in the fixture
    assert "Flow into earnings" in t


def test_golden_sweeps_page_uses_golden_sweep_stats(build):
    g = build(FULL)["golden-sweeps-scanner"]
    assert "2,468</b>golden sweeps graded" in g
    assert "&minus;0.48%" in g and "47%</b>win rate" in g
    assert "not distinguishable from zero" in g
    assert "33,235" not in g  # not the overall flow numbers


def test_golden_sweeps_page_omits_callout_without_cohort(build):
    edge = {k: v for k, v in EDGE.items() if k != "by_type"}
    g = build({**FULL, "uoa_edge.json": edge})["golden-sweeps-scanner"]
    assert 'class="callout"' not in g


def test_levels_page_uses_level_stats_sessions_everywhere(build):
    lv = build(FULL)["spy-qqq-iwm-levels"]
    assert lv.count("339") >= 3  # callout, intro, FAQ (+ JSON-LD copy)
    assert "~250" not in lv and "hundreds of sessions" not in lv


def test_flow_pages_state_real_universe_and_cadence(build):
    pages = build(FULL)
    flow = pages["options-flow-scanner"]
    assert "645 optionable names" in flow
    assert "every 30 minutes from 9:45 AM to 3:45 PM ET" in flow
    for name in ("options-flow-scanner", "golden-sweeps-scanner"):
        assert "six times" not in pages[name] and "~180" not in pages[name]
    assert "16:15" not in pages["vcp-screener"]
    assert "after the close" in pages["vcp-screener"]


def test_cadence_reads_the_worker_slots():
    slots = landing_pages.uoa_slots()
    assert slots and slots[0] == "09:45" and slots[-1] == "15:45"
    assert landing_pages.scan_cadence(["09:45", "10:15", "10:45"]) == (
        "every 30 minutes from 9:45 AM to 10:45 AM ET"
    )
    assert landing_pages.scan_cadence(["09:45"]) is None


def test_committed_transparency_page_is_generated():
    with open(os.path.join(ROOT, "docs", "transparency.html"), encoding="utf-8") as f:
        html = f.read()
    assert "Generated by landing_pages.py" in html
    assert "79,245" not in html


def test_publish_and_pages_build_regenerate_transparency():
    with open(os.path.join(ROOT, "scripts", "publish_reports.sh"), encoding="utf-8") as f:
        assert "docs/transparency.html" in f.read()
    with open(os.path.join(ROOT, ".github", "workflows", "pages.yml"), encoding="utf-8") as f:
        assert "landing_pages.py" in f.read()


XSS = "<img src=x onerror=alert(1)>"


def _poison(obj):
    """Every number in obj replaced by the injection string."""
    if isinstance(obj, dict):
        return {k: _poison(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_poison(v) for v in obj]
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        return XSS
    return obj


def test_injected_win_rate_never_reaches_the_page(build):
    edge = json.loads(json.dumps(EDGE))
    edge["rich_overall"]["5"]["win_rate"] = XSS
    edge["rich_overall"]["5"]["profit_factor"] = XSS
    pages = build({**FULL, "uoa_edge.json": edge})
    t = pages["transparency"]
    assert "<img" not in t and "onerror" not in t
    assert "<span>win rate" not in t  # the stat is omitted, not shown broken
    assert "&minus;0.23%" in t  # the valid numbers still publish
    assert "<img" not in pages["options-flow-scanner"]


def test_every_numeric_field_poisoned_publishes_nothing_unsafe(build):
    files = {name: _poison(obj) for name, obj in FULL.items()}
    pages = build(files)
    for name, html in pages.items():
        assert "<img" not in html and "onerror" not in html, name
        assert "None" not in html and not re.search(r"\bnan\b", html, re.IGNORECASE), name


def test_non_finite_numbers_are_omitted(build):
    edge = json.loads(json.dumps(EDGE))
    edge["rich_overall"]["5"]["win_rate"] = float("inf")
    edge["total_signals"] = float("nan")
    t = build({**FULL, "uoa_edge.json": edge})["transparency"]
    assert "inf%" not in t and "<span>win rate" not in t
    assert not re.search(r"\bnan\b", t, re.IGNORECASE)
    assert "33,235" not in t


@pytest.mark.parametrize(
    ("overall", "present", "absent"),
    [
        (
            {"status": "active", "ev": -1.81},
            ["averaged &minus;1.81%"],
            ["None", "?", "won", "sessions", "graded"],
        ),
        (
            {"status": "active", "ev": -1.81, "win_rate": 43},
            ["won 43% of the time with a &minus;1.81% average"],
            ["None", "sessions", "graded"],
        ),
        (
            {"status": "active", "ev": -1.81, "n": 1484},
            ["averaged &minus;1.81% (1,484 graded)"],
            ["None", "sessions"],
        ),
        (
            {"status": "active", "ev": -1.81, "hold_d": 10, "win_rate": "x"},
            ["averaged &minus;1.81% over the next 10 sessions."],
            ["None", "x%", "graded"],
        ),
    ],
)
def test_partial_momentum_record_renders_only_present_clauses(build, overall, present, absent):
    t = build({**FULL, "scan_outcomes.json": {"overall": overall}})["transparency"]
    li = re.search(r"Momentum lists fade\.</b>([^<]*)</li>", t).group(1)
    for s in present:
        assert s in li, li
    for s in absent:
        assert s not in li, li


def test_partial_attribution_and_oi_omit_missing_fields(build):
    research = json.loads(json.dumps(RESEARCH))
    del research["attribution"]["books"]["put_buy"]["n"]
    edge = json.loads(json.dumps(EDGE))
    edge["oi_confirmation"] = {"confirm_rate": 41}
    pages = build({**FULL, "uoa_research.json": research, "uoa_edge.json": edge})
    t = pages["transparency"]
    assert "Puts bought</b>: 54% beat SPY" in t and "(?" not in t
    for html in pages.values():
        assert "<b></b>alerts OI-checked" not in html
