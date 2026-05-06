# Breakout Scanner — Setup Guide

## What this does
- Runs at **market open (9:31 AM ET)** and **market close (3:55 PM ET)** every weekday
- Pulls live price, volume, float, and market cap data from Yahoo Finance
- Pre-filters against hard criteria (mkt cap <$10B, float <150M, above 20MA, vol >1.5x, green on day)
- Sends top candidates to Claude for full 7-criteria analysis
- Delivers a formatted report to your Telegram

---

## Step 1 — Install dependencies

```bash
pip install yfinance anthropic requests python-telegram-bot pytz
```

---

## Step 2 — Create your Telegram bot

1. Open Telegram → search **@BotFather** → send `/newbot`
2. Follow the prompts, copy the **bot token** (looks like `123456:ABC-DEF...`)
3. Open Telegram → search **@userinfobot** → send any message → copy your **Chat ID**

---

## Step 3 — Fill in scanner.py

Open `scanner.py` and set these three values at the top:

```python
ANTHROPIC_API_KEY = "sk-ant-YOUR_KEY_HERE"
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
TELEGRAM_CHAT_ID   = "YOUR_CHAT_ID_HERE"
```

---

## Step 4 — Test it manually

```bash
python scanner.py
```

You should see scan output in the terminal and a message arrive in Telegram within ~30 seconds.

---

## Step 5 — Schedule it

### macOS / Linux (cron)

Open your crontab:
```bash
crontab -e
```

Add these two lines (adjust the path to match where scanner.py lives):
```
# Market Open — 9:31 AM ET Mon-Fri
31 9 * * 1-5 cd /path/to/breakout_scanner && /usr/bin/python3 scanner.py >> scanner.log 2>&1

# Market Close — 3:55 PM ET Mon-Fri
55 15 * * 1-5 cd /path/to/breakout_scanner && /usr/bin/python3 scanner.py >> scanner.log 2>&1
```

> ⚠️ If your machine is not in ET, adjust the hours accordingly.
> Check your timezone offset from ET and add/subtract hours.

Find your Python path with: `which python3`

### Windows (Task Scheduler)

1. Open **Task Scheduler** → Create Basic Task
2. Set trigger: **Daily**, repeat at **9:31 AM** (and separately **3:55 PM**)
3. Set action: **Start a program**
   - Program: `C:\Python311\python.exe` (your Python path)
   - Arguments: `C:\path\to\scanner.py`
4. Check "Run only when user is logged on" or set up a service account
5. Repeat setup for the 3:55 PM run

### Cloud (always-on, recommended)

Run on a small cloud VM so it fires even when your laptop is closed:

**Option A — AWS EC2 / Google Cloud / DigitalOcean ($4-6/mo)**
```bash
# On the server, same crontab setup as above
```

**Option B — GitHub Actions (free)**
Create `.github/workflows/scanner.yml`:
```yaml
name: Breakout Scanner
on:
  schedule:
    - cron: '31 13 * * 1-5'   # 9:31 AM ET = 13:31 UTC
    - cron: '55 19 * * 1-5'   # 3:55 PM ET = 19:55 UTC
jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - run: pip install yfinance anthropic requests pytz
      - run: python scanner.py
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
```

Then store your keys in GitHub → Settings → Secrets → Actions.
Change `scanner.py` to read from `os.environ` instead of hardcoded values.

---

## Customizing the universe

Edit the `UNIVERSE` list in `scanner.py` to add/remove tickers.
The scanner pre-filters down to the top 20 by volume ratio before sending to Claude,
so a larger universe is fine — it just takes a bit longer to fetch.

---

## Costs

| Service | Cost |
|---|---|
| Yahoo Finance (yfinance) | Free |
| Anthropic API (Claude) | ~$0.01–0.03 per scan |
| Telegram Bot API | Free |
| **Total per day (2 scans)** | **~$0.02–0.06/day** |

---

## Log file

The cron job writes to `scanner.log` in the same directory.
Check it if something goes wrong:
```bash
tail -50 scanner.log
```
