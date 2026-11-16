"""SQL checks for sql/2026-11-16_profiles_for_every_account.sql.

Runs sql/tests/profiles_sql_check.mjs, which loads the script into a
throwaway in-memory Postgres (PGlite) next to a small copy of our Supabase
tables and PR #8's plan guard, and checks: re-run safety, the starter
watchlist repair, the signup trigger (including a failing seed), browser
access, and td_apply_stripe_state's ordering, promo and lock rules.

Needs Node and PGlite (`bun install --cwd sql/tests`); skipped otherwise,
which is the case in CI."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "sql" / "tests" / "profiles_sql_check.mjs"
PGLITE = ROOT / "sql" / "tests" / "node_modules" / "@electric-sql" / "pglite"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None or not PGLITE.exists(),
    reason="needs node + PGlite (bun install --cwd sql/tests)",
)


def test_profiles_sql_on_pglite(check):
    out = subprocess.run(
        [NODE, str(SCRIPT)],
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=300,
        cwd=ROOT,
    )
    lines = out.stdout.splitlines()
    results = [ln for ln in lines if ln.startswith(("PASS ", "FAIL "))]
    for ln in results:
        check(ln[5:], ln.startswith("PASS "), ln)
    assert results, out.stdout[-2000:] + out.stderr[-2000:]
    assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-2000:]
