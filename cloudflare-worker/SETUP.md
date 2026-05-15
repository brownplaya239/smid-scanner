# Discord `/ticker` Slash Command — Setup Guide

End-to-end setup time: ~30 minutes. All free.

## Architecture

```
Discord (/ticker AAPL)
  ↓ HTTPS POST
Cloudflare Worker (validates sig, acks user)
  ↓ workflow_dispatch
GitHub Actions (runs scanner.py --ticker AAPL)
  ↓ webhook POST
Discord #onepager-adhoc channel (PDF arrives ~60-90s later)
```

---

## Step 1: Create the Discord Application (5 min)

1. Go to https://discord.com/developers/applications
2. Click **New Application** → name it `Smid Scanner Bot` → **Create**
3. **General Information tab**:
   - Copy **Application ID** → save as `DISCORD_APP_ID`
   - Copy **Public Key** → save as `DISCORD_PUBLIC_KEY`
4. **Bot tab**:
   - Click **Reset Token** → copy the token → save as `DISCORD_BOT_TOKEN`
   - (You won't need the bot token after registering the slash command — slash commands run via the Worker, not a bot connection)
5. **OAuth2 tab → URL Generator**:
   - Scopes: check `applications.commands` AND `bot`
   - Bot permissions: check `Send Messages` (no other perms needed)
   - Copy the generated URL at the bottom → open in browser → add bot to your server

---

## Step 2: Register the `/ticker` slash command (3 min)

From your local PC (PowerShell):

```powershell
cd C:\smid-scanner\cloudflare-worker
$env:DISCORD_APP_ID="<your app id>"
$env:DISCORD_BOT_TOKEN="<your bot token>"
python register-command.py
```

Expected output: `✅ Slash command registered successfully.`

The command is now visible globally (may take up to 5 min to propagate, usually instant).

---

## Step 3: Create a GitHub fine-grained PAT (3 min)

1. https://github.com/settings/personal-access-tokens/new
2. **Token name**: `smid-scanner-worker`
3. **Expiration**: 1 year (or your preference)
4. **Repository access**: Only select repositories → choose `smid-scanner`
5. **Permissions** → **Repository permissions**:
   - **Actions**: Read and write
   - **Contents**: Read-only
   - **Metadata**: Read-only (auto-selected)
6. **Generate token** → copy → save as `GITHUB_PAT`

---

## Step 4: Deploy the Cloudflare Worker (10 min)

1. https://dash.cloudflare.com → Sign up or log in (free tier is sufficient)
2. **Workers & Pages** → **Create application** → **Create Worker**
3. Name: `smid-scanner-discord-bot` → **Deploy** (deploys a default hello-world)
4. After deploy, click **Edit code**
5. Replace the default code entirely with the contents of `worker.js` (from this folder)
6. **Save and deploy**
7. Click **← Back to worker dashboard**
8. **Settings tab → Variables and Secrets → Add variable** for each of these (mark each as **Secret**):
   - `DISCORD_PUBLIC_KEY` = your Discord app public key
   - `GITHUB_PAT` = your GitHub fine-grained token
   - `GITHUB_REPO` = `brownplaya239/smid-scanner`
9. **Save and deploy** again so the worker picks up the env vars
10. Copy the worker's URL — it looks like `https://smid-scanner-discord-bot.<your-subdomain>.workers.dev`

---

## Step 5: Wire Discord to your Worker (2 min)

1. Discord Developer Portal → your application → **General Information** tab
2. Find **Interactions Endpoint URL** → paste your worker URL
3. Click **Save Changes**
4. Discord will immediately PING your worker. If signature verification works, you'll see a green checkmark. If you see a red error:
   - Check `DISCORD_PUBLIC_KEY` is correct in the Worker
   - Check the worker code deployed cleanly (open Cloudflare Worker logs to see the error)

---

## Step 6: Add GitHub repo secrets (3 min)

The GitHub Action needs to know how to post to Discord:

1. https://github.com/brownplaya239/smid-scanner/settings/secrets/actions
2. **New repository secret** for each (if not already there):
   - `ANTHROPIC_API_KEY`
   - `DISCORD_TICKER_WEBHOOK_URL` = `https://discord.com/api/webhooks/1501978398593908878/g8zDQopt1IVILZ1nLFH6yZY6bIka66vmI2De1VC32oSx329fnQY6mvgesIVJ-UHJHG0A`
   - `DISCORD_WEBHOOK_URL`, `DISCORD_SETUP_WEBHOOK_URL`, `DISCORD_IWM_WEBHOOK_URL` (already set if you've been running scheduled scans)

---

## Step 7: Test (1 min)

In any Discord channel where the bot is installed:

```
/ticker AAPL
```

Expected:
- Within 1-2 seconds: bot responds `🔍 Researching AAPL — Generating one-pager research report...`
- Within 60-90 seconds: a PDF arrives in #onepager-adhoc channel

If the ack works but no PDF arrives:
- Check GitHub Actions tab → did the workflow run? (it should appear under "Ticker One-Pager Lookup")
- If it didn't trigger: check Worker logs for GitHub API errors (PAT scope/expiration issue)
- If it ran but failed: check the workflow logs — common cause is `DISCORD_TICKER_WEBHOOK_URL` not set as a repo secret

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Discord verification fails | Wrong `DISCORD_PUBLIC_KEY` |
| `Invalid signature` in worker logs | Same as above, or worker received a request that wasn't from Discord |
| Worker ack works, no GitHub run | PAT lacks Actions:write or wrong `GITHUB_REPO` value |
| GitHub run succeeds, no PDF | `DISCORD_TICKER_WEBHOOK_URL` missing from repo secrets |
| Slash command doesn't appear | Bot not added to server (re-run OAuth2 install URL), or wait up to 5 min for global propagation |
| `Ed25519` algorithm not supported | Worker runtime is too old — redeploy or use Workers compatibility date `2024-01-01` or later |

---

## Cost & limits

- **Cloudflare Worker free tier**: 100,000 requests/day. You'll use <100/day even with heavy usage.
- **GitHub Actions free tier**: 2,000 minutes/month. Each ticker lookup ≈ 1.5 min. You can run ~1,300/month.
- **Anthropic API**: ~$0.30 per ticker lookup (Sonnet 4.6 + macro + insider data).
- **Discord**: free.

Total monthly cost at 50 ad-hoc lookups: ~$15 in API fees, $0 infrastructure.
