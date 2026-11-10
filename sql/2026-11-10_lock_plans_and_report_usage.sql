-- ============================================================================
-- 2026-11-10_lock_plans_and_report_usage.sql
-- Stop signed-in users from changing their own plan or their report usage.
--
-- Run this ONCE in the Supabase SQL editor (Dashboard → SQL → New query →
-- paste → Run), AFTER the matching worker + site change is live (merge the
-- PR, wait ~2 minutes for both deploys). It is idempotent — safe to re-run.
--
-- Before this:
--   * policy pr_own_write let a browser PATCH its own profiles row, including
--     subscription_tier, so anyone could make themselves Premium;
--   * policy rg_own let a browser insert AND delete its own
--     report_generations rows, which are what the report caps count, so a
--     user could delete their usage and keep running AI reports.
-- After this:
--   * only the service role (Stripe webhook, promo endpoint, CI) and the
--     database owner can change plan fields;
--   * browsers can read their report rows and update only `status`;
--     the worker records each report itself.
-- Everything else a browser legitimately writes on its profile
-- (daily_brief_enabled, last_seen, display_name, ...) keeps working.
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

-- 3) report_generations: browsers read, and may only update `status` --------
revoke insert, update, delete on public.report_generations from anon, authenticated;
grant select on public.report_generations to authenticated;
grant update (status) on public.report_generations to authenticated;

-- ============================================================================
-- 4) Check (read-only): who is on a paid plan right now, and why.
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
