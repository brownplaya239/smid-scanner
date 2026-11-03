# SMID Scanner (TickerDesk)

The code behind [tickerdesk.io](https://tickerdesk.io): small- and mid-cap
breakout scanners, setup builders, momentum screens, options-flow scans,
on-demand research reports and the daily brief. Scheduled GitHub Actions
workflows run the scripts, publish the report PDFs to the site (GitHub Pages,
served from `docs/`) and keep the data in Cloudflare R2.

`CLAUDE.md` is the detailed guide: report types, schedules, files, credentials
and known gotchas. `CONTRIBUTING.md` explains the commit and pull request format.

## Quick start

You need [uv](https://docs.astral.sh/uv/). It installs Python 3.14 and every
package at the exact versions in `uv.lock`.

```bash
uv sync                          # create .venv/ with Python 3.14 and all packages
cp .env.example .env             # then fill in your own API keys
uv run scanner.py                # SMID breakout scan
uv run scanner.py --ticker NVDA  # one-page lookup for a single ticker
uv run momentum_scanner.py       # QM Monthly + Stockbee Weekly
uv run pytest                    # run the tests
```

`uv run <file>.py` runs any script with the project's Python and packages; no
need to activate a virtual environment. Most scripts need API keys in `.env`
(see `CLAUDE.md`, "Credentials").

To get the public report data the site shows (no token needed):

```bash
uv run scripts/r2_sync.py pull-public
```

## Checks

These run automatically on every pull request (`.github/workflows/ci.yml`):

```bash
uv run ruff check .              # lint
uv run ruff format .             # format (CI runs it with --check)
uv run pyrefly check             # type check
uv run pytest -n auto -m "not network and not data"   # tests, as CI runs them
```

Tests marked `network` need internet access and tests marked `data` need data
that is not in git (an R2 pull or cached report snapshots); CI leaves both out.

## Git hooks

With [Bun](https://bun.sh) installed, `bun install` sets up the git hooks
(lefthook): commit messages are checked, staged Python files are fixed and
formatted with Ruff before each commit, and the tests run before each push.
