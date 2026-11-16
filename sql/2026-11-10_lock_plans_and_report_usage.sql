-- ============================================================================
-- 2026-11-10_lock_plans_and_report_usage.sql
-- Stop signed-in users from changing their own plan or their report usage,
-- and give the worker two database functions that do the plan/usage checks
-- in one locked step.
--
-- Run this ONCE in the Supabase SQL editor (Dashboard → SQL → New query →
-- paste → Run), BEFORE merging the matching worker + site change (PR #8).
-- It is idempotent: safe to re-run.
--
-- Safe to run first: everything here is additive or only takes rights away
-- from browsers. The site and worker that are live today keep working:
--   * the live worker uses the service key, which none of this restricts;
--   * the live site's "log this report" insert into report_generations now
--     fails, which that code already ignores ("non-blocking");
--   * the live site's old promo box (it wrote the plan itself) now fails
--     with "Plan fields can only be changed by TickerDesk billing.";
--   * daily-brief settings, last_seen, display_name etc. still save.
-- Between running this and merging, reports are not counted at all (the old
-- worker doesn't count them and browsers no longer can), so merge right
-- after. sql/2026-11-16_report_usage_duplicates.sql checks for any rows
-- counted twice if the order was ever reversed.
--
-- Before this:
--   * policy pr_own_write let a browser PATCH its own profiles row, including
--     subscription_tier, so anyone could make themselves Premium; it also let
--     a browser DELETE its row and re-create it as Free, wiping its promo
--     history (and its Stripe customer link) so a code could be used again;
--   * policy rg_own let a browser insert AND delete its own
--     report_generations rows, which are what the report caps count;
--   * the worker checked the cap and recorded usage in separate steps, so
--     ten requests sent at once could all pass the check.
-- After this:
--   * only the service role (Stripe webhook, worker, CI) and the database
--     owner can change plan fields, and browsers cannot delete profiles;
--   * browsers can read their report rows and update only `status`;
--   * td_reserve_report() checks the cap and records the report in one
--     locked step, per user (the worker calls it before starting a report);
--   * td_redeem_promo() applies a promo code in one locked step, records
--     every code each account has used (promo_redemptions), and refuses
--     while a paid plan is active.
-- ============================================================================

-- 1) Make sure every plan column exists (no-op where it already does) --------
alter table public.profiles
  add column if not exists subscription_expires_at timestamptz,
  add column if not exists promo_code_redeemed     text,
  add column if not exists promo_code_redeemed_at  timestamptz,
  add column if not exists stripe_customer_id      text,
  add column if not exists beta_cohort_lock        numeric;

-- 2) Guard trigger on profiles ------------------------------------------------
-- Browser requests run as the `authenticated` (or `anon`) role. The function
-- is deliberately NOT security definer, so current_user is the caller's role.
-- (Inside the SECURITY DEFINER functions below, current_user is the owner,
-- so their writes pass.)
--   INSERT from a browser: plan fields are forced to Free defaults.
--   UPDATE from a browser: any change to a plan field is rejected.
create or replace function public.td_guard_profile_plan_fields()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if current_user not in ('authenticated', 'anon') then
    return new;
  end if;

  if tg_op = 'INSERT' then
    new.subscription_tier       := 'free';
    new.subscription_expires_at := null;
    new.promo_code_redeemed     := null;
    new.promo_code_redeemed_at  := null;
    new.stripe_customer_id      := null;
    new.beta_cohort_lock        := null;
    return new;
  end if;

  if new.subscription_tier       is distinct from old.subscription_tier
  or new.subscription_expires_at is distinct from old.subscription_expires_at
  or new.promo_code_redeemed     is distinct from old.promo_code_redeemed
  or new.promo_code_redeemed_at  is distinct from old.promo_code_redeemed_at
  or new.stripe_customer_id      is distinct from old.stripe_customer_id
  or new.beta_cohort_lock        is distinct from old.beta_cohort_lock then
    raise exception 'Plan fields can only be changed by TickerDesk billing.'
      using errcode = '42501';
  end if;
  return new;
end;
$$;

drop trigger if exists td_guard_profile_plan_fields on public.profiles;
create trigger td_guard_profile_plan_fields
  before insert or update on public.profiles
  for each row execute function public.td_guard_profile_plan_fields();

-- 3) Browsers can no longer delete their profile ------------------------------
-- Deleting and re-creating the row reset it to Free with no promo history,
-- so the same promo code could be redeemed again, and the Stripe customer
-- link was lost. The table privilege is removed, so this holds whatever the
-- row policies say (pr_own_write is `for all`). Browsers keep SELECT (their
-- own row, via pr_own_read), INSERT (ensureProfile; the guard makes it Free)
-- and UPDATE (non-plan fields only, via the guard).
revoke delete, truncate on public.profiles from anon, authenticated;
grant select, insert, update on public.profiles to authenticated;

-- 4) report_generations: browsers read, and may only update `status` --------
revoke insert, update, delete, truncate on public.report_generations from anon, authenticated;
grant select on public.report_generations to authenticated;
grant update (status) on public.report_generations to authenticated;

-- 5) Promo history: every code each account has redeemed ----------------------
-- Only the latest code used to be remembered (profiles.promo_code_redeemed),
-- so alternating two codes (A, B, A, ...) extended a promo forever. One row
-- per (account, code); the unique key is what refuses a second use.
-- RLS on with no policies, and no browser grants: only the service role and
-- td_redeem_promo() touch it.
create table if not exists public.promo_redemptions (
  user_id     uuid        not null references auth.users(id) on delete cascade,
  code        text        not null,
  tier        text,
  days        integer,
  redeemed_at timestamptz not null default now(),
  constraint promo_redemptions_user_code_key unique (user_id, code)
);
alter table public.promo_redemptions enable row level security;
revoke all on public.promo_redemptions from public, anon, authenticated;
grant select, insert, delete on public.promo_redemptions to service_role;

-- Carry over the codes already redeemed (the old single-code column).
insert into public.promo_redemptions (user_id, code, redeemed_at)
select p.id,
       upper(btrim(p.promo_code_redeemed)),
       coalesce(p.promo_code_redeemed_at, now())
from public.profiles p
join auth.users u on u.id = p.id
where nullif(btrim(p.promo_code_redeemed), '') is not null
on conflict (user_id, code) do nothing;

-- 6) td_reserve_report: check the cap and record the report, atomically ------
-- Called by the worker (service key) BEFORE it starts a report. A per-user
-- advisory lock makes concurrent calls for the same user run one at a time,
-- so the count each one sees includes the rows the earlier ones inserted.
-- Limits mirror TIER caps in worker.js / PLAN_LIMITS in docs/index.html:
--   free 3 lifetime (and 3 per 30 days), pro 10 / premium 100 / beta 50
--   per rolling 30 days. A promo whose end date has passed counts as Free
--   (the same rule as get_user_plan()). No profile row = Free.
-- Returns {ok:true, id, tier, used, cap} or
--         {ok:false, code:'monthly_cap'|'lifetime_cap', tier, used, cap}.
-- The worker deletes the row again if the report fails to start.
create or replace function public.td_reserve_report(
  p_user        uuid,
  p_ticker      text,
  p_report_type text default 'adhoc'
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_tier     text;
  v_expires  timestamptz;
  v_eff      text;
  v_cap30    integer;
  v_cap_life integer;
  v_used30   bigint;
  v_used_life bigint;
  v_id       uuid;
begin
  if p_user is null
  or p_ticker is null or p_ticker !~ '^[A-Z.-]{1,8}$'
  or p_report_type is null or p_report_type not in ('adhoc', 'altdata') then
    raise exception 'td_reserve_report: invalid arguments' using errcode = '22023';
  end if;

  -- One reservation at a time per user, until this transaction ends.
  perform pg_advisory_xact_lock(
    hashtextextended('td_reserve_report:' || p_user::text, 0));

  select subscription_tier, subscription_expires_at
    into v_tier, v_expires
  from public.profiles
  where id = p_user;

  v_eff := case
    when v_expires is not null and v_expires < now() then 'free'
    else coalesce(v_tier, 'free')
  end;
  if v_eff not in ('free', 'pro', 'premium', 'beta') then
    v_eff := 'free';
  end if;
  v_cap30 := case v_eff
    when 'pro' then 10 when 'premium' then 100 when 'beta' then 50 else 3 end;
  v_cap_life := case when v_eff = 'free' then 3 else 9999 end;

  select count(*) into v_used30
  from public.report_generations
  where user_id = p_user
    and created_at >= now() - interval '30 days';
  if v_used30 >= v_cap30 then
    return jsonb_build_object('ok', false, 'code', 'monthly_cap',
      'tier', v_eff, 'used', v_used30, 'cap', v_cap30);
  end if;

  if v_eff = 'free' then
    select count(*) into v_used_life
    from public.report_generations
    where user_id = p_user;
    if v_used_life >= v_cap_life then
      return jsonb_build_object('ok', false, 'code', 'lifetime_cap',
        'tier', v_eff, 'used', v_used_life, 'cap', v_cap_life);
    end if;
  end if;

  insert into public.report_generations (user_id, ticker, report_type, status)
  values (p_user, p_ticker, p_report_type, 'queued')
  returning id into v_id;

  return jsonb_build_object('ok', true, 'id', v_id, 'tier', v_eff,
    'used', v_used30 + 1, 'cap', v_cap30);
end;
$$;

revoke all on function public.td_reserve_report(uuid, text, text)
  from public, anon, authenticated;
grant execute on function public.td_reserve_report(uuid, text, text)
  to service_role;

-- 7) td_redeem_promo: apply a promo code, atomically --------------------------
-- Called by the worker (service key) after it has checked the code against
-- its private PROMO_CODES secret; p_tier / p_days come from that secret.
--   * creates a Free profile row first if the account has none;
--   * locks the profile row (FOR UPDATE), so a Stripe webhook write and a
--     redemption for the same account run one after the other, never
--     interleaved: whichever commits second sees the first one's result;
--   * refuses a code this account already used (promo_redemptions), an
--     account on a paid plan with no end date (Stripe or hand-granted:
--     a promo would give it an end date and later show it as Free while it
--     is still paying), and a promo below the account's current tier.
-- Returns {ok:true, tier, days, expires} or
--         {ok:false, code:'already_redeemed'|'plan_active'|'higher_tier', tier}.
create or replace function public.td_redeem_promo(
  p_user uuid,
  p_code text,
  p_tier text,
  p_days integer
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_code    text := upper(btrim(p_code));
  v_tier    text;
  v_expires timestamptz;
  v_last    text;
  v_eff     text;
  v_new_exp timestamptz;
  v_rows    integer;
begin
  if p_user is null or v_code is null or v_code = '' or length(v_code) > 40
  or p_tier is null or p_tier not in ('pro', 'premium', 'beta')
  or p_days is null or p_days < 1 or p_days > 730 then
    raise exception 'td_redeem_promo: invalid arguments' using errcode = '22023';
  end if;

  -- Accounts the signup trigger missed have no row yet: give them a Free one.
  insert into public.profiles (id) values (p_user)
  on conflict (id) do nothing;

  select subscription_tier, subscription_expires_at, promo_code_redeemed
    into v_tier, v_expires, v_last
  from public.profiles
  where id = p_user
  for update;

  if upper(btrim(coalesce(v_last, ''))) = v_code
  or exists (select 1 from public.promo_redemptions
             where user_id = p_user and code = v_code) then
    return jsonb_build_object('ok', false, 'code', 'already_redeemed');
  end if;

  v_eff := case
    when v_expires is not null and v_expires < now() then 'free'
    else coalesce(v_tier, 'free')
  end;
  if v_eff <> 'free' and v_expires is null then
    return jsonb_build_object('ok', false, 'code', 'plan_active', 'tier', v_eff);
  end if;
  if (case v_eff when 'pro' then 1 when 'premium' then 2 when 'beta' then 3 else 0 end)
   > (case p_tier when 'pro' then 1 when 'premium' then 2 when 'beta' then 3 else 0 end) then
    return jsonb_build_object('ok', false, 'code', 'higher_tier', 'tier', v_eff);
  end if;

  insert into public.promo_redemptions (user_id, code, tier, days)
  values (p_user, v_code, p_tier, p_days)
  on conflict (user_id, code) do nothing;
  get diagnostics v_rows = row_count;
  if v_rows = 0 then
    return jsonb_build_object('ok', false, 'code', 'already_redeemed');
  end if;

  v_new_exp := now() + make_interval(days => p_days);
  update public.profiles
     set subscription_tier       = p_tier,
         subscription_expires_at = v_new_exp,
         promo_code_redeemed     = v_code,
         promo_code_redeemed_at  = now()
   where id = p_user;

  return jsonb_build_object('ok', true, 'tier', p_tier, 'days', p_days,
    'expires', v_new_exp);
end;
$$;

revoke all on function public.td_redeem_promo(uuid, text, text, integer)
  from public, anon, authenticated;
grant execute on function public.td_redeem_promo(uuid, text, text, integer)
  to service_role;

-- ============================================================================
-- 8) Check (read-only): who is on a paid plan right now, and why.
--    A row with no stripe_customer_id and no promo_code_redeemed was set by
--    hand or by the old self-upgrade hole. TEST69 / BETAFNF rows came from the
--    codes that were visible in the page source.
-- ============================================================================
select p.id,
       u.email,
       p.subscription_tier,
       p.subscription_expires_at,
       p.promo_code_redeemed,
       p.promo_code_redeemed_at,
       p.stripe_customer_id,
       p.created_at
from public.profiles p
join auth.users u on u.id = p.id
where coalesce(p.subscription_tier, 'free') <> 'free'
order by p.created_at desc;
