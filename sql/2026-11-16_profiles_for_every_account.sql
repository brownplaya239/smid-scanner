-- ============================================================================
-- 2026-11-16_profiles_for_every_account.sql
-- Give every account a profile row, now and for every future signup, and
-- make Stripe plan changes land in the right order.
--
-- Run this in the Supabase SQL editor (Dashboard → SQL → New query → paste →
-- Run) BEFORE the matching worker change goes live (the worker calls
-- td_apply_stripe_state, created below). It is idempotent — safe to re-run;
-- a second run creates nothing and just shows the check at the bottom.
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
--   2) installs handle_new_user() + the on_auth_user_created trigger;
--   3) creates the missing profile rows (plan = the column default, Free);
--   4) repairs starter watchlists: any account not yet marked starter_seeded
--      whose watchlist is EMPTY gets the five starters (this also catches
--      accounts whose profile the site created before this script ran);
--      a non-empty watchlist and the plan fields are never touched;
--   5) installs td_apply_stripe_state() and two small billing tables
--      (stripe_webhook_events, stripe_sync_state), which the Stripe webhook
--      uses so an older Stripe state can never replace a newer one;
--   6) shows a one-row check: accounts_without_profile must be 0.
--
-- Works with or without sql/2026-11-10_lock_plans_and_report_usage.sql
-- (PR #8), in either order. That script's guard trigger only touches browser
-- requests (roles `authenticated` / `anon`). This script, the signup trigger
-- and td_apply_stripe_state run as the database owner (SECURITY DEFINER), so
-- the guard lets them through. td_apply_stripe_state locks the profile row
-- (FOR UPDATE) the same way PR #8's td_redeem_promo does, so a Stripe update
-- and a promo redemption for one account run one after the other.
-- ============================================================================

-- 1) Columns (no-op where they already exist) --------------------------------
alter table public.profiles
  add column if not exists last_seen               timestamptz,
  add column if not exists starter_seeded          boolean default false,
  add column if not exists subscription_expires_at timestamptz,
  add column if not exists stripe_customer_id      text;

-- 2) Signup trigger -----------------------------------------------------------
-- Creates the profile row and seeds the starter watchlist for each new
-- account. Any error is downgraded to a warning: a broken trigger must never
-- block someone from signing up. The site (ensureProfile) and the Stripe
-- webhook both create a missing row too, and step 4 of this script (re-run
-- any time) repairs a starter watchlist the trigger failed to add.
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
  -- starter_seeded is set only after the insert succeeded.
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

-- 3) Back-fill the accounts that have no profile ------------------------------
-- The temp tables remember what this run did, for the check at the bottom.
drop table if exists pg_temp.td_profile_backfill;
create temp table td_profile_backfill (id uuid primary key);
drop table if exists pg_temp.td_starter_repair;
create temp table td_starter_repair (id uuid primary key);

with created as (
  -- created_at = when the account was made, not today, so the site's
  -- first-sign-in welcome toast doesn't fire for old accounts.
  -- starter_seeded stays false: step 4 decides about the watchlist.
  insert into public.profiles (id, created_at, starter_seeded)
  select u.id, coalesce(u.created_at, now()), false
  from auth.users u
  where not exists (select 1 from public.profiles p where p.id = u.id)
  on conflict (id) do nothing
  returning id
)
insert into td_profile_backfill (id)
select id from created;

-- 4) Repair starter watchlists ------------------------------------------------
-- Separate from step 3 on purpose: an account can have a profile and still
-- be missing its starters (the site created the profile before this script
-- ran, or the trigger's seeding step failed). Seed every account that is
-- not marked starter_seeded AND has no watchlist rows at all. Accounts with
-- any ticker are left alone. Only plan-free columns are written.
with targets as (
  select p.id
  from public.profiles p
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

-- 5) Stripe → plan, in order -------------------------------------------------
-- Every Stripe event the worker accepts, so an exact duplicate is skipped.
-- No browser access at all (RLS on, no policies, no grants).
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

-- When the Stripe state last saved for each account was read from Stripe.
-- A separate table, not a profiles column, because browsers may update
-- their own profile row: a user who could set this to the far future could
-- freeze a cancelled paid plan. Browsers have no access here.
create table if not exists public.stripe_sync_state (
  user_id     uuid primary key references auth.users(id) on delete cascade,
  state_at    timestamptz not null,
  tier        text not null,
  customer_id text,
  updated_at  timestamptz not null default now()
);
alter table public.stripe_sync_state enable row level security;
revoke all on public.stripe_sync_state from public, anon, authenticated;
grant select on public.stripe_sync_state to service_role;

-- td_apply_stripe_state: save the plan the worker just read from the Stripe
-- API (never from an event payload). Called with the service key only.
--   * creates a Free profile row first if the account has none;
--   * locks the profile row (FOR UPDATE), like PR #8's td_redeem_promo, so a
--     Stripe update and a promo redemption never interleave;
--   * p_state_at is when the worker asked Stripe. If stripe_sync_state
--     already holds a state read at or after that moment, nothing changes
--     (applied=false, reason 'stale'): an older Stripe state can never
--     replace a newer one, whatever order the webhooks arrive or finish in;
--   * a paid tier sets the plan and clears any promo end date (Stripe now
--     owns the plan; otherwise it would read as Free when the promo date
--     passed, while still being charged);
--   * 'free' only removes a plan Stripe could have granted (pro / premium
--     with no end date). A live promo or a hand-granted beta plan is kept;
--   * records p_event_id in stripe_webhook_events.
-- Returns {applied, tier, reason}.
create or replace function public.td_apply_stripe_state(
  p_user       uuid,
  p_tier       text,
  p_customer   text,
  p_state_at   timestamptz,
  p_event_id   text default null,
  p_event_type text default null
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_tier     text;
  v_expires  timestamptz;
  v_state_at timestamptz;
  v_applied  boolean := true;
  v_reason   text := null;
begin
  if p_user is null or p_state_at is null
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

  select state_at into v_state_at
  from public.stripe_sync_state
  where user_id = p_user;

  if v_state_at is not null and v_state_at >= p_state_at then
    v_applied := false;
    v_reason  := 'stale';
  elsif p_tier <> 'free' then
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

  if v_applied then
    insert into public.stripe_sync_state (user_id, state_at, tier, customer_id)
    values (p_user, p_state_at, p_tier, p_customer)
    on conflict (user_id) do update
      set state_at    = excluded.state_at,
          tier        = excluded.tier,
          customer_id = excluded.customer_id,
          updated_at  = now();
  end if;

  if p_event_id is not null then
    insert into public.stripe_webhook_events
      (event_id, event_type, user_id, tier, applied)
    values (p_event_id, p_event_type, p_user, p_tier, v_applied)
    on conflict (event_id) do nothing;
  end if;

  return jsonb_build_object('applied', v_applied,
    'tier', coalesce(v_tier, 'free'), 'reason', v_reason);
end;
$$;

revoke all on function public.td_apply_stripe_state(uuid, text, text, timestamptz, text, text)
  from public, anon, authenticated;
grant execute on function public.td_apply_stripe_state(uuid, text, text, timestamptz, text, text)
  to service_role;

-- 6) Check (read-only) --------------------------------------------------------
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
  exists (select 1 from pg_trigger
          where tgrelid = 'auth.users'::regclass
            and tgname = 'on_auth_user_created'
            and tgenabled <> 'D')                         as signup_trigger_installed,
  to_regprocedure('public.td_apply_stripe_state(uuid, text, text, timestamptz, text, text)')
    is not null                                           as stripe_sync_installed;
