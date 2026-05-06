# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A breakout stock scanner that runs twice daily (market open 9:31 AM ET and close 3:55 PM ET). It fetches price/volume/float data from Yahoo Finance, applies hard pre-filters, then sends surviving candidates to Claude for 7-criteria momentum scoring, and delivers results to Telegram.

## Running locally

```bash
pip install yfinance anthropic requests python-telegram-bot pytz
python scanner.py
```

Before running locally, fill in the three hardcoded credentials at the top of `scanner.py`:
```python
ANTHROPIC_API_KEY = "sk-ant-..."
TELEGRAM_BOT_TOKEN = "..."
TELEGRAM_CHAT_ID   = "..."
```

## Two versions of the scanner

| File | Credentials | Use case |
|---|---|---|
| `scanner.py` | Hardcoded at top of file | Local / manual runs |
| `scanner_cloud.py` | Read from `os.environ` | Cloud / CI deployment |

**Note:** The GitHub Actions workflow (`.github/workflows/scanner.yml`) currently runs `scanner.py`, but injects secrets as env vars — this works only if `scanner.py` has been updated to read from `os.environ`. For clean cloud deployment, the workflow should point to `scanner_cloud.py` instead.

## Architecture: 4-step pipeline

```
fetch_yfinance_data()  →  pre_filter()  →  run_claude_analysis()  →  send_telegram()
     [1/4]                  [2/4]               [3/4]                    [4/4]
```

1. **Fetch** — pulls 3-month daily OHLCV history + fundamentals (float, market cap) for every ticker in `UNIVERSE`
2. **Pre-filter** — hard cuts: mkt cap $0–10B, float 0–150M, above 20MA, volume ≥1.5× avg, positive on day; sorts by vol ratio, caps at top 20
3. **Claude analysis** — sends surviving candidates to `claude-opus-4-5-20251101` with a structured prompt; Claude scores each against 7 criteria using its knowledge of recent catalysts; returns raw JSON of top 5–7 setups
4. **Telegram** — formats results as Markdown and POSTs to the bot

## The 7 scoring criteria Claude evaluates

1. Market cap < $10B (pre-filtered)
2. Float < 150M shares (pre-filtered)
3. Breaking out of a base at a steep angle
4. Hot theme/narrative (AI, space, defense, biotech, nuclear, reshoring)
5. Recent catalyst (earnings, FDA, contract, news in last 2–4 weeks)
6. Strong price/volume action + above key MAs (pre-filtered)
7. Exceptional setup quality overall

Scores: `"A - Top Setup"` / `"B - Strong Setup"` / `"C - Watch List"`

## Customizing the universe

Edit the `UNIVERSE` list in either scanner file to add/remove tickers. The pre-filter and top-20 cap keep Claude API cost low regardless of universe size.

## GitHub Actions deployment

The workflow triggers on schedule (cron) and supports manual dispatch (`workflow_dispatch`). Required GitHub Secrets: `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

DST caveat: the cron times are hardcoded for EDT (UTC-4). During EST (UTC-5, Nov–Mar), update to `31 14` and `55 20`.
