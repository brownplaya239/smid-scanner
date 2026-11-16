-- ============================================================================
-- 2026-11-16_profiles_for_every_account.sql
-- Give every account a profile row, now and for every future signup.
--
-- Run this ONCE in the Supabase SQL editor (Dashboard → SQL → New query →
-- paste → Run). It is idempotent — safe to re-run; a second run creates
-- nothing and just shows the check at the bottom.
--
-- Why: the signup trigger in docs/SUPABASE_SETUP.md (on_auth_user_created →
-- handle_new_user) was never installed on the live database. On 2026-11-16,
-- 22 of 27 accounts had no public.profiles row, including every account
-- created since 2026-07-26. The plan (Free / Pro / Premium) lives on that
-- row, so a new customer who paid stayed on Free, and new users never got
-- the starter watchlist (NVDA, MU, AVGO, MRVL, AAPL).
--
-- What it does:
--   1) makes sure the two columns the trigger writes exist;
--   2) installs handle_new_user() + the on_auth_user_created trigger;
--   3) creates the missing profile rows (plan = the column default, Free);
--   4) seeds the starter watchlist only for those accounts whose watchlist
--      is empty (anyone who already added tickers is left alone);
--   5) shows a one-row check: accounts_without_profile must be 0.
--
-- Works with or without sql/2026-11-10_lock_plans_and_report_usage.sql
-- (PR #8). That script's guard trigger only touches browser requests
-- (roles `authenticated` / `anon`). This script and the signup trigger run
-- as the database owner (handle_new_user is SECURITY DEFINER), so the guard
-- lets them through, and they never set plan fields anyway: new rows get
-- the table default subscription_tier = 'free'.
-- ============================================================================

-- 1) Columns the trigger writes (no-op where they already exist) -------------
alter table public.profiles
  add column if not exists last_seen      timestamptz,
  add column if not exists starter_seeded boolean default false;

-- 2) Signup trigger -----------------------------------------------------------
-- Creates the profile row and seeds the starter watchlist for each new
-- account. Any error is downgraded to a warning: a broken trigger must never
-- block someone from signing up. The site (ensureProfile) and the Stripe
-- webhook (upsert) both create a missing row too, so nothing is lost.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  begin
    insert into public.profiles (id, last_seen)
      values (new.id, now())
      on conflict (id) do nothing;
  exception when others then
    raise warning 'handle_new_user: profile insert failed for %: %',
      new.id, sqlerrm;
    return new;
  end;

  -- Starter watchlist for instant "this is mine" value on first visit.
  begin
    insert into public.watchlists (user_id, ticker) values
      (new.id, 'NVDA'), (new.id, 'MU'), (new.id, 'AVGO'),
      (new.id, 'MRVL'), (new.id, 'AAPL')
      on conflict do nothing;
    update public.profiles set starter_seeded = true where id = new.id;
  exception when others then
    raise warning 'handle_new_user: starter watchlist failed for %: %',
      new.id, sqlerrm;
  end;
  return new;
end;
$$;

-- Trigger functions are not meant to be called directly.
revoke all on function public.handle_new_user() from public, anon, authenticated;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- 3) + 4) Back-fill the accounts that have no profile -------------------------
-- The temp table remembers who this run back-filled, for the check below.
drop table if exists pg_temp.td_profile_backfill;
create temp table td_profile_backfill (
  id     uuid primary key,
  seeded boolean not null
);

with missing as (
  select u.id,
         u.created_at,
         -- seed only if the account has no watchlist rows at all
         not exists (select 1 from public.watchlists w
                     where w.user_id = u.id) as needs_starter
  from auth.users u
  where not exists (select 1 from public.profiles p where p.id = u.id)
),
created as (
  -- created_at = when the account was made, not today, so the site's
  -- first-sign-in welcome toast doesn't fire for old accounts.
  insert into public.profiles (id, created_at, starter_seeded)
  select id, coalesce(created_at, now()), needs_starter
  from missing
  on conflict (id) do nothing
  returning id, starter_seeded
)
insert into td_profile_backfill (id, seeded)
select id, coalesce(starter_seeded, false) from created;

insert into public.watchlists (user_id, ticker)
select b.id, t.ticker
from td_profile_backfill b
cross join (values ('NVDA'), ('MU'), ('AVGO'), ('MRVL'), ('AAPL')) as t(ticker)
where b.seeded
on conflict do nothing;

-- 5) Check (read-only) --------------------------------------------------------
-- Expected: accounts_without_profile = 0 and signup_trigger_installed = true.
-- profiles_created_by_this_run is 22 on the first run (as of 2026-11-16)
-- and 0 on any re-run.
select
  (select count(*) from auth.users)                       as total_accounts,
  (select count(*) from auth.users u
    where not exists (select 1 from public.profiles p
                      where p.id = u.id))                 as accounts_without_profile,
  (select count(*) from td_profile_backfill)              as profiles_created_by_this_run,
  (select count(*) from td_profile_backfill where seeded) as starter_watchlists_seeded_by_this_run,
  exists (select 1 from pg_trigger
          where tgrelid = 'auth.users'::regclass
            and tgname = 'on_auth_user_created'
            and tgenabled <> 'D')                         as signup_trigger_installed;
