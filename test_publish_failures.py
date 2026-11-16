"""A report that fails to publish must not count as a finished scan
(PR #22 re-check, 2026-11-16).

- scanner.py / setup_builder.py: an archive failure now exits non-zero, so
  scanner.yml does not record the slot as done. A scan with no candidates
  still finishes cleanly (that is a real, empty result).
- scripts/publish_reports.sh: when every git push attempt fails, the job
  now fails instead of exiting 0. scanner.yml records completion only
  after a successful publish.

The scan pipelines run with every data / Claude call replaced by stubs; no
network, no API key (a placeholder is set for the import only)."""

import os
import shutil
import subprocess
from unittest import mock

import pytest

import report_archive

_BASE = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture(scope="module")
def mods():
    key = os.environ.get("ANTHROPIC_API_KEY") or "placeholder-not-used-by-these-tests"
    with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": key}):
        import scanner
        import setup_builder
    return scanner, setup_builder


def _stub_scanner(monkeypatch, sc, results):
    monkeypatch.setattr(sc, "IWM_MODE", False)
    monkeypatch.setattr(sc, "USER_ID", "")
    monkeypatch.setattr(sc, "get_dynamic_universe", lambda: ["AAA"])
    monkeypatch.setattr(sc, "fetch_yfinance_data", lambda u: ([], {}, None))
    monkeypatch.setattr(sc, "pre_filter", lambda raw: [])
    monkeypatch.setattr(sc, "enrich_with_earnings", lambda c, t: c)
    monkeypatch.setattr(sc, "add_rs_line_new_high", lambda c, h, s: c)
    monkeypatch.setattr(sc, "get_sector_leaders", lambda s: [])
    monkeypatch.setattr(
        sc,
        "fetch_macro_context",
        lambda: {"regime": "NEUTRAL", "vix": 15, "iwm_spy_trend": 0.0},
    )
    monkeypatch.setattr(sc, "enrich_candidates_with_insiders", lambda c, days_back: c)
    monkeypatch.setattr(sc, "run_claude_analysis", lambda *a, **k: list(results))
    monkeypatch.setattr(sc, "generate_pdf", lambda *a, **k: b"%PDF-stub")


def _stub_setup(monkeypatch, sb, results):
    monkeypatch.setattr(sb, "IWM_MODE", False)
    monkeypatch.setattr(sb, "get_universe", lambda: ["AAA"])
    monkeypatch.setattr(sb, "fetch_setup_data", lambda u: ([], {}))
    monkeypatch.setattr(sb, "score_setups", lambda raw: [])
    monkeypatch.setattr(
        sb,
        "fetch_macro_context",
        lambda: {"regime": "NEUTRAL", "vix": 15, "iwm_spy_trend": 0.0},
    )
    monkeypatch.setattr(sb, "enrich_candidates_with_insiders", lambda c, days_back: c)
    monkeypatch.setattr(sb, "run_claude_setup_analysis", lambda *a, **k: list(results))
    monkeypatch.setattr(sb, "generate_setup_pdf", lambda *a, **k: b"%PDF-stub")


def _archive_raises(monkeypatch):
    def boom(pdf_bytes, filename):
        raise OSError("disk full")

    monkeypatch.setattr(report_archive, "archive", boom)


def _archive_records(monkeypatch):
    calls = []
    monkeypatch.setattr(report_archive, "archive", lambda b, f: calls.append(f))
    return calls


ONE = [{"ticker": "AAA", "score": "A", "grade": "A"}]


def test_breakout_scan_fails_when_the_archive_fails(mods, monkeypatch):
    sc, _ = mods
    _stub_scanner(monkeypatch, sc, ONE)
    _archive_raises(monkeypatch)
    with pytest.raises(OSError, match="disk full"):
        sc.run_scan()


def test_breakout_scan_archives_when_it_works(mods, monkeypatch):
    sc, _ = mods
    _stub_scanner(monkeypatch, sc, ONE)
    calls = _archive_records(monkeypatch)
    sc.run_scan()
    assert len(calls) == 1 and calls[0].startswith("smid_scanner_")


def test_empty_breakout_scan_still_succeeds(mods, monkeypatch):
    sc, _ = mods
    _stub_scanner(monkeypatch, sc, [])
    _archive_raises(monkeypatch)  # never reached: nothing to archive
    sc.run_scan()


def test_setup_builder_fails_when_the_archive_fails(mods, monkeypatch):
    _, sb = mods
    _stub_setup(monkeypatch, sb, ONE)
    _archive_raises(monkeypatch)
    with pytest.raises(OSError, match="disk full"):
        sb.run_setup_builder()


def test_setup_builder_archives_when_it_works(mods, monkeypatch):
    _, sb = mods
    _stub_setup(monkeypatch, sb, ONE)
    calls = _archive_records(monkeypatch)
    sb.run_setup_builder()
    assert len(calls) == 1 and calls[0].startswith("smid_setup_")


def test_empty_setup_builder_still_succeeds(mods, monkeypatch):
    _, sb = mods
    _stub_setup(monkeypatch, sb, [])
    _archive_raises(monkeypatch)
    sb.run_setup_builder()


# ── scripts/publish_reports.sh ─────────────────────────────────────────
# Runs the real script in a scratch clone whose origin rejects every push.
# `uv` and `sleep` are stubbed on PATH (no R2, no 30 s of back-off).

BASH = shutil.which("bash")
GIT = shutil.which("git")
needs_bash = pytest.mark.skipif(BASH is None or GIT is None, reason="bash/git not installed")


def _scratch_env():
    """os.environ minus every GIT_* variable. Git hooks (lefthook's
    pre-push runs these tests) export GIT_DIR and friends, which would
    point every git command here at the REAL repository instead of the
    scratch one."""
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}


def _git(cwd, *args):
    subprocess.run([GIT, *args], cwd=cwd, env=_scratch_env(), check=True, capture_output=True)


def _publish(tmp_path, reject):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name in ("uv", "sleep"):
        p = stubs / name
        p.write_text("#!/bin/sh\nexit 0\n", newline="\n")
        p.chmod(0o755)

    _git(tmp_path, "init", "-q", "--bare", "-b", "master", str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "config", "user.name", "t")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "core.autocrlf", "false")
    # every path the script stages exists on master (git add fails on a
    # missing pathspec)
    (work / "docs").mkdir()
    for name in ("sitemap.xml", "transparency.html", "some-landing.html"):
        (work / "docs" / name).write_text("x\n")
    _git(work, "add", "docs")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "checkout", "-q", "-B", "master")
    _git(work, "push", "-q", "origin", "HEAD:master")
    if reject:
        # from here on, origin refuses every push
        hook = origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho rejected >&2\nexit 1\n", newline="\n")
        hook.chmod(0o755)

    (work / "scripts").mkdir()
    script = (
        open(os.path.join(_BASE, "scripts", "publish_reports.sh"), encoding="utf-8")
        .read()
        .replace("\r\n", "\n")
    )
    (work / "scripts" / "publish_reports.sh").write_text(script, newline="\n")
    (work / "docs" / "reports").mkdir()
    (work / "docs" / "reports" / "smid_scanner_2026-11-16_1000.pdf").write_bytes(b"%PDF-stub")

    # belt and braces: the script must see the scratch clone, nothing else
    top = subprocess.run(
        [GIT, "rev-parse", "--show-toplevel"],
        cwd=work,
        env=_scratch_env(),
        check=True,
        capture_output=True,
        encoding="utf-8",
    ).stdout.strip()
    assert work.samefile(top), top

    env = _scratch_env()
    env["PATH"] = str(stubs) + os.pathsep + env.get("PATH", "")
    return subprocess.run(
        [BASH, "scripts/publish_reports.sh"],
        check=False,
        cwd=work,
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=120,
    )


@needs_bash
def test_publish_fails_when_every_push_is_rejected(tmp_path):
    r = _publish(tmp_path, reject=True)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "did not publish" in r.stdout


@needs_bash
def test_publish_succeeds_when_the_push_lands(tmp_path):
    r = _publish(tmp_path, reject=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Reports published to site" in r.stdout


def test_scanner_workflow_records_only_after_a_successful_publish():
    with open(os.path.join(_BASE, ".github", "workflows", "scanner.yml"), encoding="utf-8") as f:
        wf = f.read()
    rec = wf[wf.index("- name: Record completed scans") :]
    cond = rec[: rec.index("run:")]
    assert "steps.publish.outcome == 'success'" in cond
    # the record step only marks universes whose scan process exited 0
    assert "SMID_OK" in rec and "IWM_OK" in rec


# --- the real write boundary: report_archive.save_report swallows the OSError
# and returns None; archive() must turn that into an error, not a "done".


def _unwritable_reports_dir(monkeypatch, tmp_path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("a file where the reports folder should be")
    monkeypatch.setattr(report_archive, "REPORTS_DIR", str(blocker / "reports"))
    rebuilt = []
    monkeypatch.setattr(report_archive, "rebuild_manifest", lambda: rebuilt.append(1))
    return rebuilt


def test_archive_raises_when_the_pdf_cannot_be_written(monkeypatch, tmp_path):
    rebuilt = _unwritable_reports_dir(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="could not write report PDF"):
        report_archive.archive(b"%PDF-stub", "smid_scanner_2026-11-17_1000.pdf")
    assert rebuilt == []  # no manifest entry for a PDF that doesn't exist


def test_archive_still_writes_and_rebuilds_when_it_works(monkeypatch, tmp_path):
    monkeypatch.setattr(report_archive, "REPORTS_DIR", str(tmp_path / "reports"))
    rebuilt = []
    monkeypatch.setattr(report_archive, "rebuild_manifest", lambda: rebuilt.append(1))
    report_archive.archive(b"%PDF-stub", "smid_scanner_2026-11-17_1000.pdf")
    assert (tmp_path / "reports" / "smid_scanner_2026-11-17_1000.pdf").read_bytes() == b"%PDF-stub"
    assert rebuilt == [1]


def test_breakout_scan_fails_on_a_real_pdf_write_failure(mods, monkeypatch, tmp_path):
    sc, _ = mods
    _stub_scanner(monkeypatch, sc, ONE)
    _unwritable_reports_dir(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="could not write report PDF"):
        sc.run_scan()


def test_setup_builder_fails_on_a_real_pdf_write_failure(mods, monkeypatch, tmp_path):
    _, sb = mods
    _stub_setup(monkeypatch, sb, ONE)
    _unwritable_reports_dir(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="could not write report PDF"):
        sb.run_setup_builder()
