"""
register-command.py — One-time registration of /ticker slash command with Discord.

Run this ONCE after creating your Discord application. Updating the command schema
(e.g., adding a new option) requires re-running this script.

Required env vars (or hardcode below):
  DISCORD_APP_ID    — from Developer Portal → General Information → Application ID
  DISCORD_BOT_TOKEN — from Developer Portal → Bot → Token (click Reset Token if needed)
"""

import os
import sys
import json
import requests

APP_ID    = os.environ.get("DISCORD_APP_ID", "").strip()
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()

if not APP_ID or not BOT_TOKEN:
    print("Set DISCORD_APP_ID and DISCORD_BOT_TOKEN env vars first:")
    print("  $env:DISCORD_APP_ID='1234567890'")
    print("  $env:DISCORD_BOT_TOKEN='MTIzNDU2Nzg5MA...'")
    print("  python register-command.py")
    sys.exit(1)

command = {
    "name":        "ticker",
    "description": "Get a one-pager research report on any ticker",
    "options": [{
        "name":        "symbol",
        "description": "Stock symbol (e.g. AAPL, BKSY, IONQ)",
        "type":        3,        # STRING
        "required":    True,
    }]
}

url = f"https://discord.com/api/v10/applications/{APP_ID}/commands"
resp = requests.post(
    url,
    headers={
        "Authorization": f"Bot {BOT_TOKEN}",
        "Content-Type":  "application/json",
    },
    data=json.dumps(command),
    timeout=15,
)

print(f"Status: {resp.status_code}")
print(json.dumps(resp.json(), indent=2))

if resp.status_code in (200, 201):
    print("\n✅ Slash command registered successfully.")
    print("   Type /ticker in Discord to use it.")
else:
    print("\n❌ Registration failed. Common causes:")
    print("   - Wrong bot token (regenerate in Developer Portal)")
    print("   - Wrong application ID")
    print("   - Bot not added to your server (use OAuth2 install URL)")
