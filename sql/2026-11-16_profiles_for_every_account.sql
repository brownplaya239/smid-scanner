-- ============================================================================
-- 2026-11-16_profiles_for_every_account.sql
-- Give every account a profile row, now and for every future signup, and
-- make Stripe plan changes save one at a time per account.
--
-- Run this in the Supabase SQL editor (Dashboard → SQL → New query → paste →
-- Run) BEFORE the matching worker change goes live (the worker calls the
-- functions created below), and once more a few minutes after it goes live.
-- It is idempotent — safe to re-run, including over an earlier version of
-- this file; a second run creates nothing and just shows the check at the
-- bottom.
--
-- Why: the signup trigger in docs/SUPABASE_SETUP.md (on_auth_user_created →
-- handle_new_user) was never installed on the live database. On 2026-11-16,
-- 22 of 27 accounts had no public.profiles row, including every account
-- created since 2026-07-26. The plan (Free / Pro / Premium) lives on that
-- row, so a new customer who paid stayed on Free, and new users never got
-- the starter watchlist (NVDA, MU, AVGO, MRVL, AAPL).
--
-- What it does:
--   1) makes sure the columns this script writes exist;
--   2) records which accounts have NO profile yet (before step 4 creates
--      them): only those, plus later signups whose starter step failed, are
--      ever given starters by this script (starter_repair_allowlist);
--   3) installs handle_new_user() + the on_auth_user_created trigger;
--   4) creates the missing profile rows (plan = the column default, Free);
--   5) adds the starters for allow-listed accounts whose watchlist is still
--      empty. Accounts that already had a profile are never touched, so a
--      watchlist someone emptied on purpose stays empty;
--   6) installs the Stripe sync: stripe_customers (every Stripe customer
--      each account has had), stripe_reconcile_lease (one Stripe sync per
--      account at a time), stripe_webhook_events (events already saved),
--      and the three functions the worker calls;
--   7) shows a one-row check: accounts_without_profile must be 0.
--
-- Works with or without sql/2026-11-10_lock_plans_and_report_usage.sql
-- (PR #8), in either order. That script's guard trigger only touches browser
-- requests (roles `authenticated` / `anon`). This script, the signup trigger
-- and the Stripe functions run as the database owner (SECURITY DEFINER), so
-- the guard lets them through. td_apply_stripe_state locks the profile row
-- FIRST (FOR UPDATE), the same row PR #8's td_redeem_promo locks, and only
-- then the lease row, so the two never deadlock and a Stripe save and a
-- promo redemption for one account run one after the other.
-- ============================================================================

-- 1) Columns (no-op where they already exist) --------------------------------
alter table public.profiles
  add column if not exists last_seen               timestamptz,
  add column if not exists starter_seeded          boolean default false,
  add column if not exists subscription_expires_at timestamptz,
  add column if not exists stripe_customer_id      text;

-- 2) Who may get starters -----------------------------------------------------
-- Audited list of the only accounts this script (or a later re-run) adds
-- starter tickers to. Browsers have no access (RLS on, no grants).
--   no_profile_at_migration - had no profile row when this script ran (so
--                             the missing trigger never seeded them);
--   signup_seed_failed      - signed up after the trigger was installed,
--                             but its starter step failed (the trigger
--                             writes this row);
--   created_after_trigger   - signed up after the trigger was installed and
--                             still not marked starter_seeded (catches a
--                             signup whose trigger failed before it could
--                             write the row above).
create table if not exists public.starter_repair_allowlist (
  user_id  uuid primary key references auth.users(id) on delete cascade,
  reason   text not null,
  added_at timestamptz not null default now()
);
alter table public.starter_repair_allowlist enable row level security;
revoke all on public.starter_repair_allowlist from public, anon, authenticated;

-- When this script first installed the signup trigger (first run only).
create table if not exists public.td_migration_marks (
  name text primary key,
  at   timestamptz not null default now()
);
alter table public.td_migration_marks enable row level security;
revoke all on public.td_migration_marks from public, anon, authenticated;

-- Computed BEFORE step 4 creates the missing profiles.
insert into public.starter_repair_allowlist (user_id, reason)
select u.id, 'no_profile_at_migration'
from auth.users u
where not exists (select 1 from public.profiles p where p.id = u.id)
on conflict (user_id) do nothing;

-- 3) Signup trigger -----------------------------------------------------------
-- Creates the profile row and seeds the starter watchlist for each new
-- account. Any error is downgraded to a warning: a broken trigger must never
-- block someone from signing up. starter_seeded is false until the starters
-- are in, then true; a failure also puts the account on the allowlist, so
-- the next run of this script adds its starters.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  begin
    insert into public.profiles (id, last_seen, starter_seeded)
      values (new.id, now(), false)
      on conflict (id) do nothing;
  exception when others then
    raise warning 'handle_new_user: profile insert failed for %: %',
      new.id, sqlerrm;
    begin
      insert into public.starter_repair_allowlist (user_id, reason)
        values (new.id, 'signup_seed_failed') on conflict (user_id) do nothing;
    exception when others then null;
    end;
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
    begin
      update public.profiles set starter_seeded = false where id = new.id;
      insert into public.starter_repair_allowlist (user_id, reason)
        values (new.id, 'signup_seed_failed') on conflict (user_id) do nothing;
    exception when others then null;
    end;
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

insert into public.td_migration_marks (name) values ('starter_trigger_installed')
on conflict (name) do nothing;

-- 4) Back-fill the accounts that have no profile ------------------------------
-- The temp tables remember what this run did, for the check at the bottom.
drop table if exists pg_temp.td_profile_backfill;
create temp table td_profile_backfill (id uuid primary key);
drop table if exists pg_temp.td_starter_repair;
create temp table td_starter_repair (id uuid primary key);

with created as (
  -- created_at = when the account was made, not today, so the site's
  -- first-sign-in welcome toast doesn't fire for old accounts.
  insert into public.profiles (id, created_at, starter_seeded)
  select u.id, coalesce(u.created_at, now()), false
  from auth.users u
  where not exists (select 1 from public.profiles p where p.id = u.id)
  on conflict (id) do nothing
  returning id
)
insert into td_profile_backfill (id)
select id from created;

-- 5) Starter watchlists, allow-listed accounts only ---------------------------
-- Signups since the trigger went in that are still not marked seeded.
insert into public.starter_repair_allowlist (user_id, reason)
select p.id, 'created_after_trigger'
from public.profiles p
join auth.users u on u.id = p.id
join public.td_migration_marks m on m.name = 'starter_trigger_installed'
where u.created_at > m.at
  and p.starter_seeded is not true
on conflict (user_id) do nothing;

-- Seed allow-listed accounts not yet seeded whose watchlist is empty (an
-- account that added its own tickers meanwhile is left alone). Only
-- plan-free columns are written.
with targets as (
  select p.id
  from public.profiles p
  join public.starter_repair_allowlist a on a.user_id = p.id
  where p.starter_seeded is not true
    and not exists (select 1 from public.watchlists w where w.user_id = p.id)
),
seeded as (
  insert into public.watchlists (user_id, ticker)
  select t.id, s.ticker
  from targets t
  cross join (values ('NVDA'), ('MU'), ('AVGO'), ('MRVL'), ('AAPL')) as s(ticker)
  on conflict do nothing
  returning user_id
)
insert into td_starter_repair (id)
select distinct user_id from seeded;

-- Marked only for the accounts whose starters were actually inserted above.
update public.profiles p
   set starter_seeded = true
  from td_starter_repair r
 where p.id = r.id;

-- 6) Stripe -> plan -----------------------------------------------------------
-- No browser access to any of these (RLS on, no policies, no grants).

-- Every Stripe event the worker has saved, so an exact duplicate is skipped.
create table if not exists public.stripe_webhook_events (
  event_id     text primary key,
  event_type   text,
  user_id      uuid,
  tier         text,
  applied      boolean,
  processed_at timestamptz not null default now()
);
alter table public.stripe_webhook_events enable row level security;
revoke all on public.stripe_webhook_events from public, anon, authenticated;
grant select, insert on public.stripe_webhook_events to service_role;

-- Every Stripe customer id ever seen for an account. Each checkout can make
-- a new Stripe customer, so the worker checks all of them, not just one.
create table if not exists public.stripe_customers (
  user_id       uuid not null references auth.users(id) on delete cascade,
  customer_id   text not null check (customer_id <> ''),
  first_seen_at timestamptz not null default now(),
  primary key (user_id, customer_id)
);
alter table public.stripe_customers enable row level security;
revoke all on public.stripe_customers from public, anon, authenticated;
grant select, insert on public.stripe_customers to service_role;

-- Back-fill from the one customer id cached on each profile. Re-running
-- after the new worker is live also picks up any the old worker cached.
insert into public.stripe_customers (user_id, customer_id)
select p.id, p.stripe_customer_id
from public.profiles p
where coalesce(p.stripe_customer_id, '') <> ''
on conflict do nothing;

-- One Stripe sync per account at a time. The worker takes this lease,
-- reads Stripe, saves, and gives it back; a second webhook for the same
-- account meanwhile is told "busy" and Stripe retries it later, when it
-- reads the newer state. A lease the worker never gave back (crash, lost
-- network) simply expires.
create table if not exists public.stripe_reconcile_lease (
  user_id    uuid primary key references auth.users(id) on delete cascade,
  token      uuid not null,
  expires_at timestamptz not null
);
alter table public.stripe_reconcile_lease enable row level security;
revoke all on public.stripe_reconcile_lease from public, anon, authenticated;

-- Replaced by the lease: earlier versions of this file ordered saves by the
-- worker's own clock, which is not when Stripe's state was read.
drop function if exists public.td_apply_stripe_state(uuid, text, text, timestamptz, text, text);
drop table if exists public.stripe_sync_state;

-- td_acquire_reconcile_lease: take the account's lease, or take it over if
-- the holder's has expired, in one atomic step. True = this token holds it.
create or replace function public.td_acquire_reconcile_lease(
  p_user        uuid,
  p_token       uuid,
  p_ttl_seconds integer default 60
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
  v_rows integer;
begin
  if p_user is null or p_token is null or p_ttl_seconds is null
  or p_ttl_seconds < 1 or p_ttl_seconds > 600 then
    raise exception 'td_acquire_reconcile_lease: invalid arguments' using errcode = '22023';
  end if;
  insert into public.stripe_reconcile_lease as l (user_id, token, expires_at)
  values (p_user, p_token, clock_timestamp() + make_interval(secs => p_ttl_seconds))
  on conflict (user_id) do update
    set token = excluded.token, expires_at = excluded.expires_at
    where l.expires_at <= clock_timestamp();
  get diagnostics v_rows = row_count;
  return v_rows = 1;
end;
$$;

-- td_release_reconcile_lease: give the lease back, only if this token still
-- holds it. True = released.
create or replace function public.td_release_reconcile_lease(
  p_user  uuid,
  p_token uuid
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
  v_rows integer;
begin
  delete from public.stripe_reconcile_lease
   where user_id = p_user and token = p_token;
  get diagnostics v_rows = row_count;
  return v_rows = 1;
end;
$$;

-- td_apply_stripe_state: save the plan the worker just read from the Stripe
-- API (never from an event payload), while it holds the account's lease.
-- Called with the service key only.
--   * creates a Free profile row first if the account has none;
--   * locks the profile row (FOR UPDATE), like PR #8's td_redeem_promo,
--     THEN the lease row;
--   * saves nothing unless p_lease_token still holds an unexpired lease
--     (applied=false, reason 'lease_lost'; the worker asks Stripe to retry);
--   * a paid tier sets the plan and clears any promo end date (Stripe now
--     owns the plan; otherwise it would read as Free when the promo date
--     passed, while still being charged);
--   * 'free' only removes a plan Stripe could have granted (pro / premium
--     with no end date). A live promo or a hand-granted beta plan is kept;
--   * remembers p_customer in stripe_customers and p_event_id in
--     stripe_webhook_events.
-- Returns {applied, tier, reason}.
create or replace function public.td_apply_stripe_state(
  p_user        uuid,
  p_tier        text,
  p_customer    text,
  p_lease_token uuid,
  p_event_id    text default null,
  p_event_type  text default null
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_tier    text;
  v_expires timestamptz;
  v_token   uuid;
  v_until   timestamptz;
  v_reason  text := null;
begin
  if p_user is null or p_lease_token is null
  or p_tier is null or p_tier not in ('free', 'pro', 'premium') then
    raise exception 'td_apply_stripe_state: invalid arguments' using errcode = '22023';
  end if;

  insert into public.profiles (id) values (p_user)
  on conflict (id) do nothing;

  select subscription_tier, subscription_expires_at
    into v_tier, v_expires
  from public.profiles
  where id = p_user
  for update;

  select token, expires_at into v_token, v_until
  from public.stripe_reconcile_lease
  where user_id = p_user
  for update;

  if v_token is null or v_token <> p_lease_token or v_until <= clock_timestamp() then
    return jsonb_build_object('applied', false,
      'tier', coalesce(v_tier, 'free'), 'reason', 'lease_lost');
  end if;

  if p_tier <> 'free' then
    update public.profiles
       set subscription_tier       = p_tier,
           subscription_expires_at = null,
           stripe_customer_id      = coalesce(p_customer, stripe_customer_id)
     where id = p_user;
    v_tier := p_tier;
  elsif coalesce(v_tier, 'free') in ('pro', 'premium') and v_expires is null then
    update public.profiles
       set subscription_tier  = 'free',
           stripe_customer_id = coalesce(p_customer, stripe_customer_id)
     where id = p_user;
    v_tier := 'free';
  else
    -- Stripe says "not paying", and the plan on the row isn't a Stripe
    -- one (Free already, a promo with an end date, or beta): keep it.
    update public.profiles
       set stripe_customer_id = coalesce(p_customer, stripe_customer_id)
     where id = p_user;
    v_reason := 'kept_non_stripe_plan';
  end if;

  if coalesce(p_customer, '') <> '' then
    insert into public.stripe_customers (user_id, customer_id)
    values (p_user, p_customer)
    on conflict do nothing;
  end if;

  if p_event_id is not null then
    insert into public.stripe_webhook_events
      (event_id, event_type, user_id, tier, applied)
    values (p_event_id, p_event_type, p_user, p_tier, true)
    on conflict (event_id) do nothing;
  end if;

  return jsonb_build_object('applied', true,
    'tier', coalesce(v_tier, 'free'), 'reason', v_reason);
end;
$$;

revoke all on function public.td_acquire_reconcile_lease(uuid, uuid, integer)
  from public, anon, authenticated;
grant execute on function public.td_acquire_reconcile_lease(uuid, uuid, integer)
  to service_role;
revoke all on function public.td_release_reconcile_lease(uuid, uuid)
  from public, anon, authenticated;
grant execute on function public.td_release_reconcile_lease(uuid, uuid)
  to service_role;
revoke all on function public.td_apply_stripe_state(uuid, text, text, uuid, text, text)
  from public, anon, authenticated;
grant execute on function public.td_apply_stripe_state(uuid, text, text, uuid, text, text)
  to service_role;

-- 7) Check (read-only) --------------------------------------------------------
-- Expected: accounts_without_profile = 0, signup_trigger_installed = true,
-- stripe_sync_installed = true. profiles_created_by_this_run is about 22 on
-- the first run (as of 2026-11-16) and 0 on any re-run.
select
  (select count(*) from auth.users)                       as total_accounts,
  (select count(*) from auth.users u
    where not exists (select 1 from public.profiles p
                      where p.id = u.id))                 as accounts_without_profile,
  (select count(*) from td_profile_backfill)              as profiles_created_by_this_run,
  (select count(*) from td_starter_repair)                as starter_watchlists_added_by_this_run,
  (select count(*) from public.starter_repair_allowlist)  as starter_allowlist_size,
  (select count(*) from public.stripe_customers)          as stripe_customers_known,
  exists (select 1 from pg_trigger
          where tgrelid = 'auth.users'::regclass
            and tgname = 'on_auth_user_created'
            and tgenabled <> 'D')                         as signup_trigger_installed,
  (to_regprocedure('public.td_apply_stripe_state(uuid, text, text, uuid, text, text)') is not null
   and to_regprocedure('public.td_acquire_reconcile_lease(uuid, uuid, integer)') is not null
   and to_regprocedure('public.td_release_reconcile_lease(uuid, uuid)') is not null)
                                                          as stripe_sync_installed;
