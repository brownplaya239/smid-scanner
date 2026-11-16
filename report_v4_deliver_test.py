"""User-requested v4 reports must never fall back to the public archive.

The privacy policy promises a report a user asks for is stored privately.
report_v4_run._deliver retries the private upload, then fails the job
rather than publishing the user's report publicly. Public runs (no user
id) still go to the public archive.
"""

import pytest

import report_archive
import report_v4_run


@pytest.fixture
def parts(tmp_path):
    out = []
    for name in ("research_X_core.pdf", "research_X_appendix.pdf"):
        p = tmp_path / name
        p.write_bytes(b"%PDF fake")
        out.append((str(p), name, name.endswith("core.pdf")))
    return out


@pytest.fixture
def calls(monkeypatch):
    log = {"public": [], "private": []}
    monkeypatch.setattr(report_archive, "archive", lambda blob, name: log["public"].append(name))
    return log


def _uploader(calls, results):
    seq = iter(results)

    def fake(blob, name, user_id, ticker, kind, link=True):
        ok = next(seq)
        calls["private"].append((name, ok))
        return ok

    return fake


def test_user_report_never_archived_publicly_when_upload_keeps_failing(parts, calls, monkeypatch):
    monkeypatch.setattr(report_archive, "upload_user_report", _uploader(calls, [False] * 10))
    with pytest.raises(SystemExit, match="NOT published publicly"):
        report_v4_run._deliver(parts, "user-1", "X", attempts=3, wait=0)
    assert calls["public"] == []
    assert len(calls["private"]) == 3


def test_user_report_retries_then_succeeds_privately(parts, calls, monkeypatch):
    monkeypatch.setattr(report_archive, "upload_user_report", _uploader(calls, [False, True, True]))
    report_v4_run._deliver(parts, "user-1", "X", attempts=3, wait=0)
    assert calls["public"] == []
    assert [ok for _, ok in calls["private"]] == [False, True, True]


def test_public_run_without_user_still_archives_publicly(parts, calls, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no private upload without a user id")

    monkeypatch.setattr(report_archive, "upload_user_report", boom)
    report_v4_run._deliver(parts, "", "X")
    assert calls["public"] == ["research_X_core.pdf", "research_X_appendix.pdf"]
