# TickerDesk — Operations & Service Linkages

A single source of truth for every service this platform depends on,
what's automated, what's manual, and what to do when something breaks.

## Service inventory

| Service | What it does | Auto-managed? | Required action when changing |
|---|---|---|---|
| **GitHub** (brownplaya239/smid-scanner) | Source of truth, GH Actions runs all batch jobs | ✅ Fully | None |
| **GitHub Pages** | Hosts the static dashboard at tickerdesk.io | ✅ Fully | `git push master` → auto-deploys in ~60 sec |
| **Cloudflare Worker** (smid-scanner-discord-bot) | Polygon/EDGAR proxy + Stripe checkout + push send | ✅ Auto-deploys via `deploy_worker.yml` | `git push master` if cloudflare-worker/ changes |
| **Cloudflare DNS** (tickerdesk.io) | Domain + SSL + Resend DNS records | ❌ Manual | Edit at dash.cloudflare.com |
| **Supabase** (uaeojibmhxbwkhpvmjwy) | Auth + Postgres + RLS for user data | ❌ Manual SQL | Run new SQL in dashboard SQL editor |
| **Resend** (tickerdesk.io domain) | Transactional email (magic links + Daily Brief) | ❌ Manual | Update via resend.com dashboard |
| **Stripe** | Subscription billing (Pro $29 / Premium $99) | ❌ Manual | Update products/webhook at dashboard.stripe.com |
| **Google Cloud Console** | OAuth client for "Sign in with Google" | ❌ Manual | Update consent screen at console.cloud.google.com |
| **Polygon.io** | Market data (stocks + options + news) | ❌ Manual subscription | API key in worker secrets |
| **Anthropic API** | Claude Opus for report generation, Haiku for filing summaries | ❌ Manual | API key in worker secrets |

## Required secrets — where each one lives

### GitHub Actions secrets (Settings → Secrets and variables → Actions)

| Name | Purpose | How to rotate |
|---|---|---|
| `POLYGON_API_KEY` | Market data for batch scans | New key in polygon.io dashboard |
| `ANTHROPIC_API_KEY` | Claude for report generation | console.anthropic.com → Keys |
| `PAT` | Trigger GH workflows from worker | github.com → Settings → Tokens → fine-grained |
| `SUPABASE_URL` | Daily brief + push alerts + snapshot | `https://uaeojibmhxbwkhpvmjwy.supabase.co` (static) |
| `SUPABASE_SERVICE_KEY` | Bypass RLS for batch writes | Supabase → Settings → API Keys → Legacy → service_role |
| `RESEND_API_KEY` | Daily brief delivery | resend.com → API Keys → Create |
| `FROM_EMAIL` | Daily brief sender | `TickerDesk <brief@tickerdesk.io>` (static) |
| `VAPID_PUBLIC_KEY` | Web push verification | Generate pair locally with Python+cryptography |
| `VAPID_PRIVATE_KEY` | Web push signing | (same pair as public) |
| `VAPID_SUBJECT` | Push notification subject | `mailto:brief@tickerdesk.io` (static) |
| `CLOUDFLARE_API_TOKEN` | Auto-deploy worker on push | dash.cloudflare.com/profile/api-tokens → "Edit Workers" template |

### Cloudflare Worker secrets (set via wrangler or dashboard)

| Name | Purpose | How to rotate |
|---|---|---|
| `POLYGON_API_KEY` | Same key, scoped to worker | polygon.io dashboard |
| `ANTHROPIC_API_KEY` | Same key, scoped to worker | console.anthropic.com |
| `PAT` | Same GH PAT | github.com tokens |
| `STRIPE_SECRET_KEY` | Charge cards | dashboard.stripe.com/apikeys |
| `STRIPE_PRO_PRICE_ID` | Map "Pro" → price | dashboard.stripe.com/products → click product → click price row → API ID (`price_...`, NOT `prod_...`) |
| `STRIPE_PREMIUM_PRICE_ID` | Map "Premium" → price | (same path) |
| `STRIPE_WEBHOOK_SECRET` | Verify webhook authenticity | dashboard.stripe.com/webhooks → endpoint → reveal signing secret |
| `SUPABASE_URL` | For Stripe webhook → Supabase update | static |
| `SUPABASE_SERVICE_KEY` | For Stripe webhook → update profiles.subscription_tier | Supabase Legacy API Keys |

## Workflow trigger graph

```
GitHub push → Pages auto-deploy → tickerdesk.io live (~60s)
GitHub push → Worker auto-deploy (if cloudflare-worker/** changed) → worker.dev live
Cron 5:33 PM ET → Momentum Scans → commits reports/swing_report.json + others → Pages auto-deploy
                  └─ on completion → Push Alerts (workflow_run trigger)
                  └─ on completion → Daily Snapshot (workflow_run trigger)
Cron 4:17 PM ET → Setup Builder (scanner.yml) → commits setup PDFs
Cron 11:07/11:37/3:07 PM ET → Breakout Scanner → commits scanner PDFs
Cron 9:47/11:07/12:17/1:37/2:37/3:47 PM ET → Unusual Options Activity → commits uoa_latest.json
                  └─ on completion → Push Alerts
Cron 8:47 AM ET → Daily Brief → reads users + watchlists from Supabase, sends Resend email
On-demand → Ticker One-Pager Lookup, Alt-Data Intelligence Lookup (user button click triggers via worker → GH API)
```

## "Something broke" runbook

### Site doesn't load
1. Check `https://tickerdesk.io` HTTP 200
2. Check GH Actions for failed pages-build-deployment
3. Check Cloudflare DNS records: `nslookup tickerdesk.io 8.8.8.8`

### Daily Brief didn't fire
1. Check GH Actions → Daily Brief workflow run history
2. Look at logs for swing/uoa/earnings load counts
3. Check Supabase: `select * from email_log where kind='daily_brief' order by sent_at desc limit 5;`

### Push notifications not arriving
1. Check `Notification.permission` in browser console (must be "granted")
2. Check Windows: Action Center for entries; Focus Assist must be Off
3. Check GH Actions → Push Alerts run history
4. Check Supabase: `select * from push_log order by sent_at desc limit 5;`

### Stripe checkout fails
1. Try the worker endpoint directly: `curl -X POST .../stripe/checkout -d '{"plan":"pro","user_id":"<your_id>"}'`
2. Expect `{ok:true, url:"https://checkout.stripe.com/..."}` — anything else means a secret is wrong
3. Common: STRIPE_PRO_PRICE_ID must start with `price_` not `prod_`
4. Common: STRIPE_WEBHOOK_SECRET must be from THE webhook destination (not the API key)

### Magic-link email not arriving
1. Check Resend dashboard → emails log → look for the most recent send to that address
2. If domain is "Pending" verification, magic links fail silently
3. Sender email in Supabase SMTP settings must match a verified Resend domain

## Service health quick check (run any time)

```bash
# From repo root:

echo "=== Site ===" && curl -s -o /dev/null -w "HTTP %{http_code}\n" https://tickerdesk.io

echo "=== Worker ===" && curl -s -o /dev/null -w "HTTP %{http_code}\n" \
  "https://smid-scanner-discord-bot.sumeetsancheti97.workers.dev/?quotes=SPY"

echo "=== Recent workflow runs ===" && gh run list --repo brownplaya239/smid-scanner --limit 8

echo "=== Latest commits ===" && git log --oneline -5

echo "=== DNS health ===" && \
  nslookup -type=TXT resend._domainkey.tickerdesk.io 8.8.8.8 | grep -E "p=|text =" | head -1
```

## Improvement ideas (backlog)

| Idea | Benefit | Lift |
|---|---|---|
| Cloudflare API token → auto-deploy worker | Eliminates manual `wrangler deploy` | ✅ DONE (this commit) |
| Single setup script `bin/setup.sh` that handles all wrangler secret commands | Onboarding new env (eg staging) in one shot | Medium |
| Supabase migrations as `.sql` files in `supabase/migrations/` | Track DB schema changes in git | Medium |
| GH Issue templates for "report a bug" / "data quality concern" | Channel user feedback | Low |
| Slack/Discord webhook for failed workflow runs | Faster failure detection | Low |
| Sentry integration in worker | Catch worker exceptions in production | Medium |
| End-to-end Playwright tests | Detect dashboard regressions | High |

## Service interlinkages — what depends on what

```
User signs in via Google
  → Google Cloud OAuth client
  → redirects to Supabase /auth/v1/callback
  → Supabase creates row in auth.users
  → handle_new_user trigger inserts into public.profiles + seeds watchlist

User flips Daily Brief toggle on tickerdesk.io
  → JS client updates profiles.daily_brief_enabled via Supabase REST
  → Daily Brief cron next morning reads daily_brief_enabled=true → sends Resend email

User clicks Upgrade to Pro
  → JS client POSTs to worker /stripe/checkout
  → Worker creates Checkout Session via Stripe API
  → Browser redirects to Stripe-hosted checkout
  → User completes card form
  → Stripe POSTs to worker /stripe/webhook
  → Worker validates signature with STRIPE_WEBHOOK_SECRET
  → Worker PATCHes profiles.subscription_tier via Supabase REST with service_role
  → Client refreshes plan state → user has Pro access

Worker fetches news for ticker drilldown
  → Worker hits Polygon News API with POLYGON_API_KEY
  → Returns to client with 5-min edge cache

Momentum scan runs at 5:33 PM ET
  → momentum.yml runs swing_report.py + others with POLYGON_API_KEY
  → commits report JSONs to docs/reports/
  → triggers pages-build-deployment (site updates)
  → triggers Push Alerts workflow (workflow_run)
  → triggers Daily Snapshot workflow (workflow_run)
  → Push Alerts reads new swing data, finds upgrades for each user's watchlist,
    POSTs signed push to each subscribed device
  → Daily Snapshot upserts per-ticker state into signal_snapshots table
```
