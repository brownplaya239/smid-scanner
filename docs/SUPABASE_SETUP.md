# TickerDesk — Supabase Setup

One-time setup to power user accounts, watchlists, and trade journal.
Takes ~5 minutes.

## 0c. (Phase 6) Cloudflare Worker secrets for Stripe Checkout

The worker now exposes `/stripe/checkout` (frontend calls this to start
a subscription) and `/stripe/webhook` (Stripe calls this to update
`profiles.subscription_tier` when events fire). Both need worker secrets
set via Wrangler:

```bash
cd cloudflare-worker
npx wrangler secret put STRIPE_SECRET_KEY         # sk_live_... or sk_test_...
npx wrangler secret put STRIPE_PRO_PRICE_ID       # price_... for $29/mo Pro
npx wrangler secret put STRIPE_PREMIUM_PRICE_ID   # price_... for $99/mo Premium
npx wrangler secret put STRIPE_WEBHOOK_SECRET     # whsec_... from webhook endpoint
npx wrangler secret put SUPABASE_URL              # already used by daily_brief — same value
npx wrangler secret put SUPABASE_SERVICE_KEY      # so the webhook can update profiles
```

To wire it up end-to-end:

1. **Create the products in Stripe** at https://dashboard.stripe.com/products:
   - "TickerDesk Pro" — recurring $29/mo
   - "TickerDesk Premium" — recurring $99/mo
   Copy each `price_...` ID (NOT the product ID) into the worker secrets above.

2. **Register the webhook** at https://dashboard.stripe.com/webhooks:
   - Endpoint URL: `https://api.tickerdesk.io/stripe/webhook`
     (the legacy `…workers.dev/stripe/webhook` still hits the same worker,
     so an already-registered Stripe webhook keeps working — update it in
     the Stripe dashboard at your convenience, not urgently)
   - Listen for events:
     - `checkout.session.completed`
     - `customer.subscription.created`
     - `customer.subscription.updated`
     - `customer.subscription.deleted`
   - Copy the **Signing secret** (`whsec_...`) into the `STRIPE_WEBHOOK_SECRET` worker secret.

3. **Deploy the worker** with `npx wrangler deploy`.

4. **Test**: on tickerdesk.io click any "Upgrade to Pro" CTA → should redirect to Stripe hosted checkout. Complete the test card flow (`4242 4242 4242 4242`, any future date, any CVC). On success, redirected back to tickerdesk.io with `?subscribed=1&plan=pro`. The webhook fires within seconds and sets `profiles.subscription_tier = 'pro'`.

Until secrets are set, clicking Upgrade falls back to the email-signup
intent capture (current behavior pre-Stripe) so the user's interest is
still recorded.

## 0b. (Phase 5) GitHub Actions secrets for Web Push notifications

The `Push Alerts` workflow needs 3 more secrets on top of the Daily Brief
ones. Same place: **Settings → Secrets and variables → Actions** on GitHub.

| Secret name             | Value                                            |
|-------------------------|--------------------------------------------------|
| `VAPID_PUBLIC_KEY`      | The public half of your VAPID key pair. The matching value also lives in the client (`docs/index.html`) — they must match. |
| `VAPID_PRIVATE_KEY`     | The private half. Used to SIGN every push payload. Keep this only in Actions secrets — never in client code, never in chat. |
| `VAPID_SUBJECT`         | A `mailto:` URL the push service can contact if our pushes misbehave. Use `mailto:brief@tickerdesk.io` or similar. |

Generate a fresh pair (DO NOT use anything that's appeared in chat for
production) with this one-liner — run in any terminal with Python +
cryptography installed:

```bash
python -c "
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
import base64
priv = ec.generate_private_key(ec.SECP256R1())
pub = priv.public_key().public_bytes(
    encoding=serialization.Encoding.X962,
    format=serialization.PublicFormat.UncompressedPoint)
print('VAPID_PUBLIC_KEY  =', base64.urlsafe_b64encode(pub).decode().rstrip('='))
priv_int = priv.private_numbers().private_value
print('VAPID_PRIVATE_KEY =', base64.urlsafe_b64encode(
    priv_int.to_bytes(32, 'big')).decode().rstrip('='))
"
```

After adding the 3 secrets, ALSO update the `VAPID_PUBLIC_KEY` constant
in `docs/index.html` (search for `VAPID_PUBLIC_KEY =`) to the new value.
The browser-side public key MUST match the server-side public key, or
push subscription registration fails silently.

## 0. (Phase 4) GitHub Actions secrets for the Daily Brief job

The `Daily Brief` workflow (`.github/workflows/daily_brief.yml`) runs at
~8:45 AM ET weekdays and emails every user with `daily_brief_enabled =
true`. It needs 4 secrets — add them under **Settings → Secrets and
variables → Actions** on the GitHub repo:

| Secret name             | Value                                                 |
|-------------------------|-------------------------------------------------------|
| `SUPABASE_URL`          | `https://uaeojibmhxbwkhpvmjwy.supabase.co`            |
| `SUPABASE_SERVICE_KEY`  | service_role key (Supabase → Settings → API → `service_role secret`). NEVER expose this in client code — it bypasses RLS. |
| `RESEND_API_KEY`        | `re_…` from resend.com → API Keys (single key fine; same one the Supabase SMTP integration uses) |
| `FROM_EMAIL`            | `TickerDesk <brief@tickerdesk.io>` (sender must be a verified domain in Resend) |

Once added you can test on demand: **Actions → Daily Brief → Run workflow**,
toggle `dry_run = true` and `restrict_email = your-email@example.com`
for a no-send preview. Remove both for the real send.


## 1. Create the Supabase project

1. Go to https://supabase.com
2. Sign up (or use Google OAuth)
3. Create new project
   - Name: `tickerdesk`
   - Database password: any strong password (you won't need it day-to-day)
   - Region: pick the one closest to most users (US East for now)
4. Wait ~90 seconds for provisioning

## 2f. (Phase 8) Run this in SQL Editor for Stripe customer-id cache

```sql
-- Cache the Stripe customer_id on the profile the first time a user
-- completes a checkout. Lets the Billing Portal endpoint look up the
-- customer directly by user_id instead of searching Stripe by email
-- (which 404s for promo / trial users who never paid, and is racy
-- when the same email maps to multiple Stripe customers).
alter table public.profiles
  add column if not exists stripe_customer_id text;
create index if not exists profiles_stripe_customer_idx
  on public.profiles(stripe_customer_id);
```

Run that. Adds 1 nullable column + an index. The worker's
`/stripe/webhook` handler writes the customer id on
`checkout.session.completed` and `customer.subscription.*` events.
`/stripe/portal` reads it first and only falls back to email lookup
if the row is null (and back-fills the row when it does).

## 2e. (Phase 7) Run this in SQL Editor for promo-code expiry

```sql
-- Add an optional expiry timestamp on the user's subscription_tier.
-- Promo codes use this to grant time-limited Premium access (e.g.
-- "TEST69" → premium for 90 days). When NULL, the tier is open-ended
-- (normal paid subscription). When set + < now(), the client treats
-- the user as 'free' for entitlement purposes regardless of the tier
-- value, until a Stripe webhook re-confirms paid status.
alter table public.profiles
  add column if not exists subscription_expires_at timestamptz;
-- Most recent promo redemption tracked separately so we can show the
-- user what they redeemed and when (and prevent redeeming twice).
alter table public.profiles
  add column if not exists promo_code_redeemed text;
alter table public.profiles
  add column if not exists promo_code_redeemed_at timestamptz;
```

Run that. Adds 3 nullable columns. Existing users default to NULL
(no expiry). The promo redemption flow in the frontend looks for these
columns and refuses to re-redeem the same code.

## 2d. (Phase 5) Run this in SQL Editor for Web Push subscriptions

```sql
-- One row per browser/device the user has subscribed for push from.
-- A user can subscribe from multiple devices — same user_id, multiple
-- rows. Endpoint is unique per (browser, device) and is the URL the
-- push service hands us; we sign payloads with our VAPID private key
-- and POST to it.
create table if not exists public.push_subscriptions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users(id) on delete cascade,
  endpoint text not null,
  p256dh text not null,         -- public ECDH key from browser
  auth text not null,           -- ephemeral auth secret from browser
  user_agent text,
  device_label text,            -- "Chrome on Mac" — for UI listing
  alert_types jsonb default '{
    "uoa_watchlist": true,
    "grade_upgrade": true,
    "earnings_imminent": true,
    "new_report": true
  }',
  created_at timestamptz default now(),
  last_used_at timestamptz,     -- bumped on every successful push
  last_failed_at timestamptz,   -- and bumped on every failed push
  fail_count int default 0,     -- if reaches 5, we stop trying & delete
  unique (endpoint)
);
create index if not exists psub_user on public.push_subscriptions(user_id);
alter table public.push_subscriptions enable row level security;
-- Users can read + manage their own subscriptions (for the device list
-- UI under Watchlist → Settings).
create policy "psub_own_read"  on public.push_subscriptions
  for select using (auth.uid() = user_id);
create policy "psub_own_write" on public.push_subscriptions
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- Per-send log — used to debug + measure engagement (open/click).
-- Schema mirrors email_log for consistency.
create table if not exists public.push_log (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users(id) on delete cascade,
  subscription_id uuid references public.push_subscriptions(id) on delete set null,
  alert_type text not null,
  status text not null check (status in ('sent','failed','expired')),
  title text,
  body text,
  payload jsonb,
  error text,
  sent_at timestamptz default now()
);
create index if not exists pl_user_sent on public.push_log(user_id, sent_at desc);
alter table public.push_log enable row level security;
create policy "pl_own_read" on public.push_log
  for select using (auth.uid() = user_id);
```

Run that. Adds 2 tables. The Python push_alerts.py script (run by the
push_alerts.yml workflow) reads `push_subscriptions` via the service
key, sends WebPushes signed with our VAPID private key, and logs each
attempt to `push_log`.

## 2c. (Phase 4) Run this in SQL Editor for Daily Brief opt-in + email log

```sql
-- Daily Brief opt-in lives on profiles (single boolean — keeps the
-- toggle path simple). Alerts table will still hold per-event
-- preferences later (new flow, grade change, etc); the daily brief
-- is the one cross-cutting digest we ship first.
alter table public.profiles
  add column if not exists daily_brief_enabled boolean default false;

-- Email log — every send (success or fail) is recorded so we can
-- (a) avoid duplicate sends within a single brief day, (b) debug
-- delivery, (c) measure open / engagement later. We don't store
-- the HTML body, just metadata + the JSON payload we shaped it from.
create table if not exists public.email_log (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users(id) on delete cascade,
  email text not null,
  kind text not null check (kind in (
    'daily_brief','alert','onboarding','transactional'
  )),
  status text not null check (status in ('sent','failed','skipped')),
  subject text,
  payload jsonb,
  error text,
  sent_at timestamptz default now(),
  -- For daily_brief: which trading day's data the brief covered. Lets
  -- us idempotently retry without spamming someone twice for the same day.
  brief_date date
);
create index if not exists email_log_user_day
  on public.email_log(user_id, kind, brief_date);
alter table public.email_log enable row level security;
-- Users can read their own delivery history (status badge in the UI)
create policy "el_own_read" on public.email_log
  for select using (auth.uid() = user_id);
-- Only service role writes (the daily-brief CI job uses the service key)
```

Run that block. After it succeeds: 1 new column on `profiles`, plus
the `email_log` table. The daily brief CI job will only send to users
who have `daily_brief_enabled = true`; the in-app toggle (Watchlist
tab → Settings) flips it.

## 2b. (Phase 3) Run this in SQL Editor for notes, alerts, report tracking

```sql
-- Notes — private per-ticker (and optionally per-signal) jottings.
-- Separate from trade_journal because not every note is a trade action.
create table if not exists public.notes (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users(id) on delete cascade,
  ticker text not null,
  note text not null,
  related_signal_type text,
  related_signal_id text,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);
create index if not exists notes_user_ticker on public.notes(user_id, ticker);
alter table public.notes enable row level security;
create policy "n_own" on public.notes
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- Alerts — what user wants to be notified about. Email sending deferred;
-- this just persists preferences for now.
create table if not exists public.alerts (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users(id) on delete cascade,
  ticker text,                  -- nullable: alert can apply to all watchlist
  alert_type text not null check (alert_type in (
    'new_flow','grade_change','earnings_imminent','news_sentiment',
    'top_flow','enters_a_tier'
  )),
  enabled boolean default true,
  config jsonb default '{}',
  created_at timestamptz default now()
);
alter table public.alerts enable row level security;
create policy "a_own" on public.alerts
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- Report-generation usage tracking (drives free/Pro credits)
create table if not exists public.report_generations (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users(id) on delete cascade,
  ticker text not null,
  report_type text not null,        -- 'adhoc' | 'altdata' | etc.
  status text default 'queued'
    check (status in ('queued','running','done','failed')),
  cost_units numeric default 1,
  created_at timestamptz default now()
);
create index if not exists rg_user_created
  on public.report_generations(user_id, created_at desc);
alter table public.report_generations enable row level security;
create policy "rg_own" on public.report_generations
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- Daily snapshot per signal per ticker — drives "What's New since last
-- visit." Lightweight: one row per ticker per day with the day's grade,
-- flow bias, and whether the ticker appeared in key modules.
create table if not exists public.signal_snapshots (
  id uuid primary key default gen_random_uuid(),
  snapshot_date date not null,
  ticker text not null,
  trend_grade text,
  flow_bias text,
  flow_score numeric,
  theme text,
  earnings_date date,
  news_sentiment text,
  appeared_in_modules jsonb default '{}',  -- {top_flow:true, qm:true, ...}
  unique (snapshot_date, ticker)
);
-- Snapshots are SHARED across users (same swing data feeds everyone).
-- Public read so the "what's new" widget can compute diffs cheaply.
alter table public.signal_snapshots enable row level security;
create policy "ss_read_any" on public.signal_snapshots
  for select using (true);
-- Only service role can write (server-side ETL)

-- Helper to get user plan (free/pro/premium) with sane fallback.
-- Server-side enforces subscription_expires_at: once the timestamp
-- has passed, the user reads as 'free' regardless of what column
-- subscription_tier still holds. Prevents devtools-bypass where a
-- client could keep the cached "premium" string after expiry — every
-- privileged read must round-trip through this RPC, which honors the
-- expiry date directly. The expires_at column is left intact so the
-- client UI can still show "Your trial ended on Aug 19, 2026" copy.
create or replace function public.get_user_plan()
returns text language sql security definer set search_path = public
as $$
  select case
    when subscription_expires_at is not null
     and subscription_expires_at < now()
    then 'free'
    else coalesce(subscription_tier, 'free')
  end
  from public.profiles
  where id = auth.uid();
$$;
grant execute on function public.get_user_plan() to authenticated;

-- Count report generations in the last 30 days (for monthly credit caps)
create or replace function public.report_count_30d()
returns bigint language sql security definer set search_path = public
as $$
  select count(*)::bigint from public.report_generations
  where user_id = auth.uid()
    and created_at > now() - interval '30 days';
$$;
grant execute on function public.report_count_30d() to authenticated;

-- Total lifetime report generations (for free-tier lifetime cap)
create or replace function public.report_count_lifetime()
returns bigint language sql security definer set search_path = public
as $$
  select count(*)::bigint from public.report_generations
  where user_id = auth.uid();
$$;
grant execute on function public.report_count_lifetime() to authenticated;
```

Run that block as one query. After it succeeds: 4 new tables + 3 helper
RPCs. Free / Pro / Premium logic is now driven by `profiles.subscription_tier`
(default `'free'` for new signups via the existing trigger).

## 2a. (Phase 2) Run this in SQL Editor to add the launch features

If you've already run the initial setup, run this delta to add portfolio
weights, email signups, watcher-count function, and last-seen tracking:

```sql
-- Add weight column for portfolio benchmarking (1.0 = equal weight default)
alter table public.watchlists
  add column if not exists weight numeric default 1
    check (weight >= 0 and weight <= 1000000);

-- Email signups for beta + daily brief (collected before account creation)
create table if not exists public.email_signups (
  email text primary key,
  source text,            -- 'beta', 'daily_brief', 'footer', etc.
  created_at timestamptz default now()
);
alter table public.email_signups enable row level security;
-- Anyone can INSERT (signup form is public); nobody can SELECT (privacy)
create policy "es_insert_any" on public.email_signups
  for insert with check (true);

-- Watcher count RPC — returns {ticker, n} for an array of tickers, with
-- security definer so it can read across ALL rows in watchlists without
-- exposing individual user identities. Counts only, never user_ids.
create or replace function public.watcher_counts(p_tickers text[])
returns table (ticker text, n bigint)
language sql security definer set search_path = public
as $$
  select ticker, count(*)::bigint
  from public.watchlists
  where ticker = any(p_tickers)
  group by ticker;
$$;
grant execute on function public.watcher_counts(text[]) to anon, authenticated;

-- last_seen RPC — atomically read prev last_seen + bump to now()
-- Used by the "What's New Since Last Visit" widget so we can show
-- diffs without losing the marker.
create or replace function public.touch_last_seen()
returns timestamptz
language plpgsql security definer set search_path = public
as $$
declare prev timestamptz;
begin
  select last_seen into prev from public.profiles where id = auth.uid();
  update public.profiles set last_seen = now() where id = auth.uid();
  return prev;
end;
$$;
grant execute on function public.touch_last_seen() to authenticated;
```

## 2. Run the schema SQL

In the project, open **SQL Editor** (left sidebar). Paste the entire
block below and click Run.

```sql
-- ── Watchlists (one row per user-ticker) ──
create table public.watchlists (
  user_id uuid references auth.users(id) on delete cascade,
  ticker text not null check (ticker ~ '^[A-Z\.\-]{1,8}$'),
  added_at timestamptz default now(),
  notes text,
  primary key (user_id, ticker)
);

-- ── Trade Journal (one row per logged action) ──
create table public.trade_journal (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users(id) on delete cascade,
  ticker text not null,
  action text not null check (action in ('buy','sell','watch','signal_acted')),
  ts timestamptz default now(),
  price numeric,
  quantity numeric,
  notes text,
  source text,                 -- e.g. 'uoa signal', 'swing upgrade', 'manual'
  source_data jsonb
);
create index trade_journal_user_ts on public.trade_journal(user_id, ts desc);

-- ── Profile + settings (subscription tier, last_seen for "what's new") ──
create table public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  display_name text,
  subscription_tier text default 'free'
    check (subscription_tier in ('free','pro','premium','beta')),
  beta_cohort_lock numeric,    -- $/mo lifetime price if cohort 1
  last_seen timestamptz,
  starter_seeded boolean default false,
  created_at timestamptz default now()
);

-- ── Row-Level Security: users only see their own rows ──
alter table public.watchlists    enable row level security;
alter table public.trade_journal enable row level security;
alter table public.profiles      enable row level security;

create policy "wl_own_read"   on public.watchlists
  for select using (auth.uid() = user_id);
create policy "wl_own_write"  on public.watchlists
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

create policy "tj_own_read"   on public.trade_journal
  for select using (auth.uid() = user_id);
create policy "tj_own_write"  on public.trade_journal
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

create policy "pr_own_read"   on public.profiles
  for select using (auth.uid() = id);
create policy "pr_own_write"  on public.profiles
  for all using (auth.uid() = id) with check (auth.uid() = id);

-- ── On signup: auto-create profile row + seed starter watchlist ──
create or replace function public.handle_new_user()
returns trigger language plpgsql security definer set search_path = public
as $$
begin
  insert into public.profiles (id, last_seen)
    values (new.id, now())
    on conflict (id) do nothing;
  -- Starter watchlist for instant "this is mine" value on first visit
  insert into public.watchlists (user_id, ticker) values
    (new.id, 'NVDA'), (new.id, 'MU'), (new.id, 'AVGO'),
    (new.id, 'MRVL'), (new.id, 'AAPL')
    on conflict do nothing;
  update public.profiles set starter_seeded = true where id = new.id;
  return new;
end;
$$;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();
```

## 3. Enable auth providers

**Authentication → Providers** (left sidebar):
- **Email** — already on. Default mode is magic link (good).
- **Google** — toggle on. Requires Google Cloud Console OAuth client
  (one-time, ~5 min). Or leave off for now; email magic link alone works.

## 4. Add your domain to the redirect URLs

**Authentication → URL Configuration**:
- Site URL: `https://tickerdesk.io`
- Additional Redirect URLs:
    `https://tickerdesk.io/*`
    `http://localhost:8080/*` (for local testing)
    `https://brownplaya239.github.io/smid-scanner/*` (raw Pages URL too)

## 5. Give me the credentials

**Settings → API**:
- **Project URL** — `https://xxxxxxxxxx.supabase.co`
- **anon public** key — long JWT starting `eyJ...`

Paste both. I'll drop them into the dashboard config and we're live.

These keys ARE safe to put in client-side code. The anon key alone
can't read or write anyone's data because RLS gates every table on
`auth.uid()`. Users only see/edit their own rows. The service-role
key (which we won't use anywhere client-side) is the bypass key —
keep that one in Supabase only.
