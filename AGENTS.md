# SMID Scanner — Project Guide

Institutional-grade SMID-cap research system. Six automated report types plus
on-demand ad-hoc ticker research, published to a GitHub Pages site.

---

## Report types

| Report | Script | What it does |
|---|---|---|
| SMID Scanner | `scanner.py` | Live breakout scan — Qullamaggie methodology (vol surge + green + near 52W high). Claude-graded A/B/C. |
| IWM Scanner | `scanner.py --iwm` | Same, on the Russell 2000 universe (IWM holdings). |
| SMID Setup Builder | `setup_builder.py` | EOD pre-breakout VCP watchlist (volume dry-up + coiling base). |
| IWM Setup Builder | `setup_builder.py --iwm` | Same, Russell 2000 universe. |
| QM Monthly Gainers | `momentum_scanner.py` | Qullamaggie's biggest 1-month gainers — top 2% by 1-mo gain, ADR%≥5, $100M+ dollar volume. Pure data table. |
| Stockbee Weekly 20% | `momentum_scanner.py` | Every liquid name up 20%+ in the last 5 trading days. Pure data table. |
| Ad-Hoc Lookup | `scanner.py --ticker SYM` | On-demand 5-page one-pager for any ticker (setup + insider + institutional + volume intelligence). |

The scanner/setup-builder reports use Claude for analysis (Sonnet 5.5 for the
scanner, Opus 5.5 for the setup builder). The momentum scans are pure
quantitative screens — no Claude.

---

## Schedule (`.github/workflows/`)

All weekdays ET. GitHub cron is literal UTC (no DST) and runs hours late
(scanner.yml's 10:07 AM cron starts ~4:20 PM). The Cloudflare worker is the
primary scheduler, on the ET clock: flow scans (`uoa.yml`, every 30 min
9:45 AM-3:45 PM), breakout scans (`scanner.yml` mode=breakout, 10:00, 11:30,
3:00 PM; holidays and half-day afternoons skipped), the 4:45 PM EOD dispatch
(`scanner.yml` + `momentum.yml`) and `JOB_SCHEDULE` (`UOA_SLOTS`,
`BREAKOUT_SLOTS`, `EOD_SLOT` in `cloudflare-worker/worker.js`). GitHub crons
stay as backstops.

| Time ET | Workflow | Runs |
|---|---|---|
| 10:00, 11:30 AM, 3:00 PM | `scanner.yml` | SMID + IWM scanners (sequential) |
| 4:45 PM | `scanner.yml` | SMID + IWM setup builders (sequential) |
| 4:30 PM | `momentum.yml` | QM Monthly + Stockbee Weekly (+ economic calendar) |
| Sun ~10 AM + 7 PM, Mon 8 AM | `economic_calendar.yml` | Rolls the economic calendar to the new week |
| on demand | `ticker-lookup.yml` | Ad-hoc single-ticker lookup (`workflow_dispatch`) |

`scanner.yml` asks `scan_slot.py plan` what to do. Each worker dispatch
names its slot (`slot` input, e.g. `2026-11-17T10:00ET`; setups are
`<date>T16:00ET`); a backstop cron maps to its slot via `CRON_SLOTS`. After
each universe scans cleanly and publishes, `scan_slot.py record` writes a
completion record to R2 `scan_runs/<date>.json`, even when the scan found
nothing. A slot with a record is skipped, so extra triggers add nothing;
`force` overrides. A run that starts after a later slot began serves that
later slot. 45 min after each breakout slot the worker reads the same
record and posts to Slack (`SLACK_ALERT_CHANNEL`) if a universe is missing.
The worker's holiday table must match `exchange_calendar.py` (tested).

---

## Files

| File | Role |
|---|---|
| `scanner.py` | Breakout scanner (SMID + IWM modes) + `--ticker` ad-hoc lookup |
| `setup_builder.py` | EOD VCP setup builder (SMID + IWM modes) |
| `claude_call.py` | Shared Claude call: streaming, effort, refusal fallback, reads only `text` blocks |
| `momentum_scanner.py` | QM Monthly + Stockbee Weekly screens (one run does both) |
| `macro_context.py` | SPY/IWM/VIX/sector regime overlay |
| `insider_activity.py` | SEC EDGAR Form 4 scan — 60/90d signal + 12-mo transaction log |
| `institutional_data.py` | yfinance holders + smart-money detection + SEC 13D/13G filings |
| `volume_intelligence.py` | O'Neill A/D rating + monthly flow + significant-volume bars |
| `report_archive.py` | Saves PDFs to `docs/reports/`, rebuilds `manifest.json` |
| `scripts/publish_reports.sh` | Race-safe commit+push of archived PDFs (used by all workflows) |
| `IWM_holdings.csv` | iShares Russell 2000 ETF constituents (universe source for IWM mode) |
| `trade_desk.py` | AI Trade Desk engine — candidates → empirical Alpha Score → measured family gates → append-only idea ledger (`data/trade_desk_log.jsonl`) → forward grading. Abstains unless a family's own record qualifies. |
| `trade_desk_validation.py` | Walk-forward validation of the Research Rank (Alpha Score) on the frozen signal record, plus the measured regime-conflict cells the engine's filter reads. The flow GATE reads the research registry (`trade_desk_research.py`), not this file. Runs in `uoa.yml`. |
| `docs_dev/AI_TRADE_DESK.md` | Trade Desk methodology + honest findings (read before touching the desk) |
| `uoa_scorecard.py` | Matched-control flow scorecard (2026-09 redesign): controls matched on sector/cap/momentum/vol replace the SPY benchmark; cluster-bootstrap effective n; net-of-cost EV; entry-lag ladder; capacity/book-sim/crowding. One row per stock, ET day and direction (v2, 2026-11); stock-cost model tiered by dollar volume. Nightly via `uoa_scorecard.yml`. Assignments freeze in `data/uoa_matched_v2.json.gz` (keyed by ticker-day, controls stored as symbols) — never reassign; the v1 file `data/uoa_matched.json.gz` is an unread archive. |
| `docs/index.html` | GitHub Pages site — 7 tabs, manifest-driven, inline PDF viewer |
| `docs/analytics.js` | PostHog visitor analytics, off until `TD_POSTHOG_KEY` is set; every page loads it. Events, privacy rules, setup: `docs_dev/ANALYTICS.md` |
| `cloudflare-worker/worker.js` | Cloudflare Worker — web form → triggers `ticker-lookup.yml` |

---

## The website

GitHub Pages, served from `docs/` on `master`, at the custom domain
**https://tickerdesk.io** (the `docs/CNAME` file binds it). The old
`brownplaya239.github.io/smid-scanner` URL 301-redirects here automatically.

- Tabs for each report type, each listing timestamped PDFs from `docs/reports/manifest.json`
- Ad-Hoc tab: a form that POSTs to the Cloudflare Worker, which triggers the
  GitHub workflow; the resulting PDF is polled for and embedded inline
- Every workflow run archives its PDF to `docs/reports/` and commits it back
  via `scripts/publish_reports.sh`
- **Data is in Cloudflare R2, not git** (cutover 2026-09-28; bucket
  `tickerdesk-data`). `data/**` and `docs/reports/*.json` are gitignored.
  Each data workflow runs `uv run scripts/r2_sync.py pull` right after
  installing Python (records `.r2_manifest.json`); `publish_reports.sh` runs
  `push --changed`, which uploads only files that job changed and deletes
  ones it pruned, and FAILS the job if the upload fails. `pages.yml` pulls
  `reports/` into the site build. The worker serves `/reports/*.json` from
  R2. uoa / momentum / uoa_scorecard share the `tickerdesk-data`
  concurrency group because they write the same files (uoa_alpha_cache,
  oi_lifecycle, the ledger). Git-only exceptions (hand-edited or fixtures)
  are listed in `.gitignore` and `r2_sync.GIT_ONLY`. Local dev:
  `uv run scripts/r2_sync.py pull-public` (public reports, no token).

---

## Credentials

`.env` (gitignored) for local runs; **GitHub repo secrets** for the workflows;
the Cloudflare Worker has its own separate secret.

GitHub repo secrets (Settings → Secrets and variables → Actions):
- `ANTHROPIC_API_KEY` — scanner / setup builder / ad-hoc lookup
- `NIMBLE_API_KEY` — alt-data lookup
- `POLYGON_API_KEY` — market data feed

Cloudflare Worker secret (dashboard → Worker → Settings → Variables):
- `PAT` — GitHub PAT with `repo` + `workflow` scope (triggers `ticker-lookup.yml`)

> GitHub secrets and Cloudflare Worker variables are **separate stores** — a
> secret added to GitHub is not visible to the Worker, and vice versa.

---

## Run locally

```bash
uv sync                          # Python 3.14 + the locked packages + dev tools, in .venv/
cp .env.example .env             # fill in real values
uv run scanner.py                # SMID breakout scan
uv run scanner.py --iwm          # IWM breakout scan
uv run scanner.py --ticker NVDA  # ad-hoc one-pager
uv run setup_builder.py          # SMID EOD VCP watchlist
uv run momentum_scanner.py       # QM Monthly + Stockbee Weekly
uv run scripts/r2_sync.py pull-public   # public report JSONs, no token
uv run pytest -n auto -m "not network and not data"   # the CI test set
```

`uv run` always uses the project's `.venv` (no activation needed). Add a
package with `uv add <pkg>` (`uv add --dev <pkg>` for a tool); never edit
`uv.lock` by hand. `bun install` installs the git hooks (see Toolchain).

---

## Toolchain

| Tool | What it does here | Command |
|---|---|---|
| **uv** | Python 3.14 (`.python-version`) and packages: ranges in `pyproject.toml`, exact versions in `uv.lock` | `uv sync`, `uv run <script>.py`, `uv add <pkg>`, `uv lock --upgrade-package <pkg>` |
| **Ruff** | Lint + format, settings in `[tool.ruff]` | `uv run ruff check --fix .`, `uv run ruff format .` |
| **Pyrefly** | Type check, settings in `[tool.pyrefly]` | `uv run pyrefly check` (`--min-severity=warn` shows the warnings) |
| **pytest** + xdist | Tests: every `test_*.py` / `*_test.py` at the root | `uv run pytest -n auto -m "not network and not data"` |
| **lefthook** + commitlint | Git hooks (`lefthook.yml`, `commitlint.config.js`), installed by `bun install` | commit-msg: message format; pre-commit: Ruff on staged `.py`; pre-push: tests |
| **CI** | `.github/workflows/ci.yml` on PRs and pushes to master (not docs/-only) | Ruff lint, Ruff format check, Pyrefly, pytest; all blocking |
| **PR title check** | `.github/workflows/pr-title.yml` | PR title must be a Conventional Commit |
| **release-please** | `.github/workflows/release-please.yml`: release PR + `CHANGELOG.md` + `version.txt` | Merge the release PR to publish a release |
| **Renovate** | `renovate.json`: weekly grouped non-major update PR, weekly `uv.lock` refresh | Python itself is never upgraded automatically |

Rules worth knowing:

- **Workflows** use `astral-sh/setup-uv` (pinned to a full version; there is
  no moving `v10` tag) + `uv sync --locked --no-dev` + `uv run ...`. Each
  sets `UV_NO_DEV=1` so `uv run` never installs the dev tools. `--locked`
  fails the job if `uv.lock` is out of date: after changing dependencies in
  `pyproject.toml`, run `uv lock` and commit `uv.lock` too.
- **Do not bump `version` in `pyproject.toml`** (or let release-please do it):
  `uv.lock` records it, so every `uv sync --locked` would fail.
- **Ruff**: the long `ignore` list in `pyproject.toml` is legacy debt, not
  policy. Remove a code from it once its existing hits are fixed.
- **Pyrefly**: error kinds fail CI and must stay at zero. The noisy inference
  kinds listed in `[tool.pyrefly.errors]` are warnings until type hints exist.
  Silence a reviewed false positive with `# pyrefly: ignore[<kind>]  <reason>`
  on the line above.
- **pytest markers**: `network` (live services) and `data` (needs an R2 pull
  or `.snapcache/` snapshots) are left out in CI. Script-style test files use
  the `check(name, ok, detail)` fixture from `conftest.py`, which reports
  each named check separately; they also still run directly
  (`uv run test_fixtures.py --write`, `uv run test_validation.py --json`).
  `uv run test_v5_mutations.py` regenerates
  `data/mutation_proofs/MUTATIONS.json`; pytest only checks the proofs.
- **Commits**: Conventional Commits, `type(scope): summary`, first line at
  most 100 characters. Types: feat, fix, perf, docs, refactor, test, build,
  ci, chore, revert (style is accepted for commits but not for PR titles).
  Scopes: `scanner`, `brief`, `reports`, `desk`, `site`, `worker`, `data`,
  `repo`, `deps`, `ci`, `main`. PRs are squash-merged, so the PR title is
  the commit on master. The publish bot commits
  `chore(reports): archive reports <time>`, which the changelog hides.
  Plain-English guide: `CONTRIBUTING.md`.
- **Root `package.json`** only holds the hook tools. Do not add
  `"type": "module"`: it would break `scripts/verify_inline_js.js` (CommonJS),
  which the Pages build runs.

---

## Known gotchas (don't re-learn these the hard way)

- **Yahoo rate-limits datacenter IPs.** A single 500-ticker `yf.download` is
  flagged as abuse and returns empty. IWM fetches are chunked into 50-ticker
  batches with retries (`fetch_iwm_data`). Run SMID and IWM **sequentially**,
  never in parallel — concurrent runs starve each other.
- **Intraday volume is incomplete.** A mid-day scan sees ~half a day's volume.
  `_trading_day_fraction()` projects the partial bar to a full-day estimate so
  the relative-volume filter isn't falsely starved.
- **The publish race.** Multiple workflows finishing seconds apart used to
  collide on `manifest.json`. `scripts/publish_reports.sh` commits first, then
  rebases with retry, regenerating the manifest on conflict. If the push
  still fails after 5 tries the job fails (the PDFs would otherwise be lost
  silently), and scanner.yml does not mark the slot done.
- **fpdf2 is latin-1 only.** All text rendered to PDF must go through `_safe()`
  — it strips/replaces em-dashes, smart quotes, and non-latin-1 chars.
- **Claude returns only the JSON fields it's asked for.** yfinance fundamentals
  must be re-merged into the results by ticker after the Claude call.
- **Claude can hallucinate ticker symbols and earnings dates.** The input
  ticker is force-preserved over Claude's output; earnings dates come from
  yfinance's verified calendar, not Claude's training data.
- **Invalid tickers abort early.** `--ticker` validates price history before
  any pipeline work and produces a clean "TICKER NOT FOUND" PDF.
- **`max_tokens`, thinking and streaming.** The current models think before
  answering, and thinking counts toward `max_tokens`, so budgets are sized
  well above the answer and every Python call streams (`claude_call.ask_claude`).
  A reply can start with `thinking` blocks: never read `content[0].text`.

---

## Models

- Scanner + alt-data synthesis: `claude-sonnet-5-5` (effort `medium` / `low`)
- Setup builder: `claude-opus-5-5`, effort `high` (deeper "Wharton HF analyst" reasoning)
- Worker (filing summary, Trade Desk "Ask", screenshot import): `claude-haiku-5-5`,
  thinking disabled (`cloudflare-worker/claude.mjs`)
- Python calls go through `claude_call.py`: streaming, explicit `effort`,
  server-side refusal fallback (`fallbacks="default"`), and `ClaudeNoAnswer` on a
  refusal or empty reply. Haiku 5.5 has no server-side fallback.
- Do not pass `temperature`/`top_p`/`top_k`, an assistant prefill, or
  `thinking: {type: "disabled"}` on Opus/Sonnet 5.5 — all return HTTP 400.
