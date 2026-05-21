# TickerDesk — Supabase Setup

One-time setup to power user accounts, watchlists, and trade journal.
Takes ~5 minutes.

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

-- Helper to get user plan (free/pro/premium) with sane fallback
create or replace function public.get_user_plan()
returns text language sql security definer set search_path = public
as $$
  select coalesce(subscription_tier, 'free') from public.profiles
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
