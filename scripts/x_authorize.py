#!/usr/bin/env python3
"""x_authorize.py — pair @tickerdeskio with your X app and store the keys
as GitHub Actions secrets. Run it yourself, in your own terminal:

    pip install tweepy
    python scripts/x_authorize.py

Works whichever X account owns the developer app (e.g. an app created
under a personal account): X's login page decides which account gets
authorized, so log in there as @tickerdeskio.

Steps it walks through:
  1. asks for the app's API Key and API Key Secret (input hidden)
  2. prints an x.com link — open it while logged in as @tickerdeskio,
     click Authorize, copy the PIN it shows
  3. paste the PIN; it exchanges it for @tickerdeskio's access tokens
  4. checks the tokens really belong to @tickerdeskio
  5. writes X_API_KEY / X_API_SECRET / X_ACCESS_TOKEN / X_ACCESS_SECRET
     into the repo's GitHub secrets via `gh secret set` (values go over
     stdin; nothing is printed or saved to disk)

Requires: the app's permissions set to "Read and write" in the
developer portal BEFORE running, and `gh` logged in.
"""

import getpass
import subprocess
import sys

REPO = "brownplaya239/smid-scanner"
EXPECTED = "tickerdeskio"


def set_secret(name, value):
    r = subprocess.run(["gh", "secret", "set", name, "-R", REPO],
                       input=value, text=True, capture_output=True)
    if r.returncode != 0:
        sys.exit(f"gh secret set {name} failed: {r.stderr.strip()}")
    print(f"  saved {name}")


def main():
    try:
        import tweepy
    except ImportError:
        sys.exit("run: pip install tweepy")
    key = getpass.getpass("App API Key (hidden): ").strip()
    secret = getpass.getpass("App API Key Secret (hidden): ").strip()
    handler = tweepy.OAuth1UserHandler(key, secret, callback="oob")
    url = handler.get_authorization_url()
    print("\n1) Open this link while logged into X as @" + EXPECTED + ":")
    print("   " + url)
    print("2) Click 'Authorize app', then copy the PIN it shows.\n")
    pin = input("PIN: ").strip()
    token, token_secret = handler.get_access_token(pin)

    client = tweepy.Client(consumer_key=key, consumer_secret=secret,
                           access_token=token,
                           access_token_secret=token_secret)
    me = client.get_me(user_auth=True).data
    print(f"\nAuthorized account: @{me.username}")
    if me.username.lower() != EXPECTED:
        sys.exit(f"That's not @{EXPECTED} — nothing saved. Log out of "
                 f"@{me.username} on x.com (or use a private window) and "
                 "run this again.")
    print("Saving GitHub secrets...")
    set_secret("X_API_KEY", key)
    set_secret("X_API_SECRET", secret)
    set_secret("X_ACCESS_TOKEN", token)
    set_secret("X_ACCESS_SECRET", token_secret)
    print("\nDone. @" + EXPECTED + " is paired; the workflow can post.")


if __name__ == "__main__":
    main()
