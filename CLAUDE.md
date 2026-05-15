# SMID Scanner

Breakout stock scanner — runs twice daily at market open (9:31 AM ET) and close (3:55 PM ET).

## Pipeline

1. **Fetch** — pulls 3-month OHLCV + fundamentals for ~35 tickers via yfinance
2. **Pre-filter** — hard cuts: mkt cap $0–10B, float 0–150M, above 20MA, vol ≥1.5x avg, green on day
3. **Claude analysis** — sends top 20 candidates to claude-opus-4-7 for 7-criteria scoring
4. **Telegram** — posts formatted report to configured chat

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in .env with your 3 credentials (see below)
python scanner.py
```

## Credentials (`.env`)

- `ANTHROPIC_API_KEY` — from https://console.anthropic.com/keys
- `TELEGRAM_BOT_TOKEN` — from @BotFather on Telegram (`/newbot`)
- `TELEGRAM_CHAT_ID` — message your bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` and find `"chat": {"id": ...}`

## Customization

- **Add/remove tickers**: Edit the `UNIVERSE` list in `scanner.py`
- **Tighten filters**: Adjust thresholds in `pre_filter()` (e.g. raise `vol_ratio` minimum)
- **Change model**: Update `model=` in `run_claude_analysis()`

## Scheduling (Windows)

Use Task Scheduler with two triggers (weekdays only):
- 9:31 AM ET → `python C:\smid-scanner\scanner.py`
- 3:55 PM ET → same

Or use GitHub Actions — add the 3 secrets to repo settings and the workflow handles scheduling automatically.
