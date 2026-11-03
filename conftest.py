"""Shared pytest fixtures.

Many test files here began as scripts that ran a long list of named checks and
printed PASS/FAIL for each. The `check` fixture keeps that style inside pytest:
every check is reported on its own (as a pytest "subtest"), and a failing check
does not stop the ones after it.
"""

import pytest

_NO_DETAIL = object()


@pytest.fixture
def check(subtests):
    """check(name, ok, detail) -> one named sub-check; `ok` must be truthy."""

    def _check(name, ok, detail=_NO_DETAIL):
        with subtests.test(msg=name):
            assert ok, name if detail is _NO_DETAIL else f"{name}: {detail}"
        return bool(ok)

    return _check
