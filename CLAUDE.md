# SMID Scanner — Project Guide

Institutional-grade SMID-cap research system. Six automated report types plus
on-demand ad-hoc ticker research, delivered to Discord and a GitHub Pages site.

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

The scanner/setup-builder reports use Claude for analysis (Sonnet 4.6 for the
scanner, Opus 4.7 for the setup builder). The momentum scans are pure
quantitative screens — no Claude.

---

## Schedule (`.github/workflows/`)

All weekdays ET. Workflows auto-handle EDT/EST via dual cron lines.

| Time ET | Workflow | Runs |
|---|---|---|
| 10:00, 11:30 AM, 3:00 PM | `scanner.yml` | SMID + IWM scanners (sequential) |
| 4:15 PM | `scanner.yml` | SMID + IWM setup builders (sequential) |
| 4:30 PM | `momentum.yml` | QM Monthly + Stockbee Weekly |
| on demand | `ticker-lookup.yml` | Ad-hoc single-ticker lookup (`workflow_dispatch`) |

`scanner.yml` time-routes via UTC: the 4:15 PM window runs setup builders, all
others run scanners.

---

## Files

| File | Role |
|---|---|
| `scanner.py` | Breakout scanner (SMID + IWM modes) + `--ticker` ad-hoc lookup |
| `setup_builder.py` | EOD VCP setup builder (SMID + IWM modes) |
| `momentum_scanner.py` | QM Monthly + Stockbee Weekly screens (one run does both) |
| `macro_context.py` | SPY/IWM/VIX/sector regime overlay |
| `insider_activity.py` | SEC EDGAR Form 4 scan — 60/90d signal + 12-mo transaction log |
| `institutional_data.py` | yfinance holders + smart-money detection + SEC 13D/13G filings |
| `volume_intelligence.py` | O'Neill A/D rating + monthly flow + significant-volume bars |
| `report_archive.py` | Saves PDFs to `docs/reports/`, rebuilds `manifest.json` |
| `scripts/publish_reports.sh` | Race-safe commit+push of archived PDFs (used by all workflows) |
| `IWM_holdings.csv` | iShares Russell 2000 ETF constituents (universe source for IWM mode) |
| `docs/index.html` | GitHub Pages site — 7 tabs, manifest-driven, inline PDF viewer |
| `cloudflare-worker/worker.js` | Cloudflare Worker — web form → triggers `ticker-lookup.yml` |

---

## The website

GitHub Pages, served from `docs/` on `master`:
**https://brownplaya239.github.io/smid-scanner/**

- Tabs for each report type, each listing timestamped PDFs from `docs/reports/manifest.json`
- Ad-Hoc tab: a form that POSTs to the Cloudflare Worker, which triggers the
  GitHub workflow; the resulting PDF is polled for and embedded inline
- Every workflow run archives its PDF to `docs/reports/` and commits it back
  via `scripts/publish_reports.sh`

---

## Credentials

`.env` (gitignored) for local runs; **GitHub repo secrets** for the workflows;
the Cloudflare Worker has its own separate secret.

GitHub repo secrets (Settings → Secrets and variables → Actions):
- `ANTHROPIC_API_KEY`
- `DISCORD_WEBHOOK_URL` (SMID scanner channel)
- `DISCORD_SETUP_WEBHOOK_URL` (SMID setup channel)
- `DISCORD_IWM_WEBHOOK_URL` (#iwm-names)
- `DISCORD_TICKER_WEBHOOK_URL` (#onepager-adhoc)
- `DISCORD_MOMENTUM_WEBHOOK_URL` (optional — momentum scans; site-only if unset)

Cloudflare Worker secret (dashboard → Worker → Settings → Variables):
- `PAT` — GitHub PAT with `repo` + `workflow` scope (triggers `ticker-lookup.yml`)

> GitHub secrets and Cloudflare Worker variables are **separate stores** — a
> secret added to GitHub is not visible to the Worker, and vice versa.

---

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in real values
python scanner.py             # SMID breakout scan
python scanner.py --iwm       # IWM breakout scan
python scanner.py --ticker NVDA   # ad-hoc one-pager
python setup_builder.py       # SMID EOD VCP watchlist
python momentum_scanner.py    # QM Monthly + Stockbee Weekly
```

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
  rebases with retry, regenerating the manifest on conflict.
- **fpdf2 is latin-1 only.** All text rendered to PDF must go through `_safe()`
  — it strips/replaces em-dashes, smart quotes, and non-latin-1 chars.
- **Claude returns only the JSON fields it's asked for.** yfinance fundamentals
  must be re-merged into the results by ticker after the Claude call.
- **Claude can hallucinate ticker symbols and earnings dates.** The input
  ticker is force-preserved over Claude's output; earnings dates come from
  yfinance's verified calendar, not Claude's training data.
- **Invalid tickers abort early.** `--ticker` validates price history before
  any pipeline work and produces a clean "TICKER NOT FOUND" PDF.
- **`max_tokens` and streaming.** Opus 4.7 caps non-streaming at ~21K tokens;
  the setup builder uses 20K. Going higher requires streaming.

---

## Models

- Scanner: `claude-sonnet-4-6` (frequent, lighter analysis)
- Setup builder: `claude-opus-4-7` (deeper "Wharton HF analyst" reasoning)
- `temperature` is not supported on Opus 4.7 — do not pass it.
