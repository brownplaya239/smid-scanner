# TickerDesk — Supabase Setup

One-time setup to power user accounts, watchlists, and trade journal.
Takes ~5 minutes.

## 1. Create the Supabase project

1. Go to https://supabase.com
2. Sign up (or use Google OAuth)
3. Create new project
   - Name: `tickerdesk`
   - Database password: any strong password (you won't need it day-to-day)
   - Region: pick the one closest to most users (US East for now)
4. Wait ~90 seconds for provisioning

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
