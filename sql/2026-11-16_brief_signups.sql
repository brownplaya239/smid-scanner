-- 2026-11-16 — Daily Brief sign-ups that actually subscribe people.
--
-- Run ONCE in the Supabase SQL editor, BEFORE merging the PR that adds
-- this file (merging deploys the worker, which calls these functions).
-- Safe to run again: every statement is idempotent.
--
-- What it does:
--   1. Adds the double opt-in columns the worker and daily_brief.py use,
--      including a one-time, expiring confirmation token (stored hashed).
--      Also adds email_log.email_norm (lower-cased, trimmed address) so the
--      brief job can check "already sent today?" with an exact match.
--   2. Adds a small table of recent sign-up attempts (per network, hashed)
--      so the anti-abuse limit is shared by every copy of the worker.
--   3. Adds four functions the worker calls with the service key:
--        brief_signup_claim   — record a sign-up and decide, atomically,
--                               whether THIS request may send the
--                               confirmation email (only one ever wins)
--        brief_signup_release — undo a claim when the email failed to send
--        brief_confirm        — use a confirmation token (once, unexpired)
--        brief_unsubscribe    — stop the Daily Brief for an address on BOTH
--                               paths: the email list and any account
--   4. Removes the public INSERT policy. The site's forms go through the
--      worker, so the public (anon) and signed-in (authenticated) roles
--      need no access. RLS stays ON with no policies = no public access.

-- 1. Columns ---------------------------------------------------------------
alter table public.email_signups
  add column if not exists confirmed_at       timestamptz,  -- clicked the confirm link
  add column if not exists unsubscribed_at    timestamptz,  -- clicked unsubscribe
  add column if not exists confirm_sent_at    timestamptz,  -- last confirm email (throttle)
  add column if not exists confirm_token_hash text,         -- sha256 hex of the live token
  add column if not exists confirm_expires_at timestamptz;  -- token stops working after this

-- The brief job reads exactly this set every morning.
create index if not exists email_signups_active
  on public.email_signups (email)
  where confirmed_at is not null and unsubscribed_at is null;
create index if not exists email_signups_lower_email
  on public.email_signups (lower(email));
create unique index if not exists email_signups_confirm_token
  on public.email_signups (confirm_token_hash)
  where confirm_token_hash is not null;

-- The brief job's "already sent today?" check matches the delivery log by
-- address with an exact comparison. A generated column fills itself in
-- for every existing row too (older account rows kept the address's
-- original capitals), and the index keeps the lookup fast.
alter table public.email_log
  add column if not exists email_norm text generated always as (lower(btrim(email))) stored;
create index if not exists email_log_norm_day
  on public.email_log (email_norm, kind, brief_date);

-- 2. Shared anti-abuse log -------------------------------------------------
-- One row per sign-up attempt that reached the database. ip_hash is an
-- HMAC of the visitor's IP made by the worker; raw IPs are never stored.
create table if not exists public.brief_signup_attempts (
  id      bigint generated always as identity primary key,
  ip_hash text not null,
  at      timestamptz not null default now()
);
create index if not exists brief_signup_attempts_ip_at
  on public.brief_signup_attempts (ip_hash, at);
create index if not exists brief_signup_attempts_at
  on public.brief_signup_attempts (at);
-- Only attempts that were actually allowed to send an email count toward
-- the site-wide limit; repeats for a pending or subscribed address don't.
-- (Rows from an earlier run of this file default to counting.)
alter table public.brief_signup_attempts
  add column if not exists counted_global boolean not null default true;
create index if not exists brief_signup_attempts_global_at
  on public.brief_signup_attempts (at) where counted_global;
alter table public.brief_signup_attempts enable row level security;
revoke all on table public.brief_signup_attempts from anon, authenticated;

-- 3. Functions ---------------------------------------------------------------
-- Returns one of:
--   'send'               this request won the claim; send the email now
--   'pending'            a confirmation went out < 15 minutes ago; send nothing
--   'already_subscribed' confirmed and not unsubscribed; send nothing
--   'rate_limited'       too many attempts (per network, or site-wide)
--   'invalid'            bad input
-- Order matters, so that cheap repeats can't use up the site-wide limit:
--   1. per-network limit (the worker hashes an IPv4 address, or an IPv6
--      /64, so rotating addresses inside one network doesn't help);
--   2. the address's own state: a repeat for an address that is pending
--      or already subscribed returns here, sends nothing, and takes no
--      site-wide slot;
--   3. only then the site-wide limit, counted under one global lock so
--      simultaneous requests from different networks can't overshoot it.
-- Locks are always taken in this order (network, address, global), so
-- they can't deadlock, and all are released when the call ends.
create or replace function public.brief_signup_claim(
  p_email text, p_source text, p_ip_hash text, p_token_hash text)
returns text
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_email   text := lower(btrim(coalesce(p_email, '')));
  v_ip      text := coalesce(nullif(btrim(p_ip_hash), ''), '?');
  v_row     public.email_signups%rowtype;
  v_found   boolean;
  v_attempt bigint;
  v_n       int;
begin
  if length(v_email) > 254 or v_email !~ '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$' then
    return 'invalid';
  end if;
  if coalesce(p_token_hash, '') !~ '^[0-9a-f]{64}$' then
    return 'invalid';
  end if;

  -- 1. Per-network limit: 5 attempts per 10 minutes.
  perform pg_advisory_xact_lock(hashtextextended('brief-ip:' || v_ip, 0));
  delete from public.brief_signup_attempts where at < now() - interval '1 day';
  select count(*) into v_n from public.brief_signup_attempts
   where ip_hash = v_ip and at > now() - interval '10 minutes';
  if v_n >= 5 then return 'rate_limited'; end if;
  insert into public.brief_signup_attempts (ip_hash, counted_global)
  values (v_ip, false)
  returning id into v_attempt;

  -- 2. The address's state. The lock makes simultaneous requests for one
  -- address take turns, so only the first can see an expired cooldown.
  perform pg_advisory_xact_lock(hashtextextended('brief-email:' || v_email, 0));
  select * into v_row from public.email_signups
   where lower(email) = v_email
   order by (confirmed_at is not null and unsubscribed_at is null) desc, (email = v_email) desc
   limit 1
   for update;
  v_found := found;
  if v_found and v_row.confirmed_at is not null and v_row.unsubscribed_at is null then
    return 'already_subscribed';
  end if;
  if v_found and v_row.confirm_sent_at is not null
     and v_row.confirm_sent_at > now() - interval '15 minutes' then
    return 'pending';
  end if;

  -- 3. Site-wide limit: 300 confirmation emails per hour.
  perform pg_advisory_xact_lock(hashtextextended('brief-global', 0));
  select count(*) into v_n from public.brief_signup_attempts
   where counted_global and at > now() - interval '1 hour';
  if v_n >= 300 then return 'rate_limited'; end if;
  update public.brief_signup_attempts set counted_global = true where id = v_attempt;

  if not v_found then
    insert into public.email_signups (email, source)
    values (v_email, left(coalesce(nullif(p_source, ''), 'site'), 40))
    on conflict do nothing;
    select * into v_row from public.email_signups
     where lower(email) = v_email
     order by (email = v_email) desc
     limit 1
     for update;
  end if;
  update public.email_signups
     set confirm_sent_at    = now(),
         confirm_token_hash = p_token_hash,
         confirm_expires_at = now() + interval '7 days'
   where email = v_row.email;
  return 'send';
end;
$$;

-- The confirmation email failed to send: free the address so the person
-- can try again straight away. Only the request holding this token can.
create or replace function public.brief_signup_release(p_email text, p_token_hash text)
returns void
language sql
security definer
set search_path = public, pg_temp
as $$
  update public.email_signups
     set confirm_sent_at = null, confirm_token_hash = null, confirm_expires_at = null
   where lower(email) = lower(btrim(coalesce(p_email, '')))
     and confirm_token_hash = p_token_hash;
$$;

-- Uses a confirmation token: works once, within 7 days, and only if no
-- newer confirmation or an unsubscribe has replaced it. Returns the
-- confirmed address, or null.
create or replace function public.brief_confirm(p_token_hash text)
returns text
language sql
security definer
set search_path = public, pg_temp
as $$
  update public.email_signups
     set confirmed_at = now(), unsubscribed_at = null,
         confirm_token_hash = null, confirm_expires_at = null
   where confirm_token_hash = p_token_hash
     and confirm_expires_at > now()
  returning email;
$$;

-- Stops the Daily Brief for one email address on BOTH paths.
-- Pass the account id (from a signed account link: the address is looked
-- up here, never taken from the link) OR the address (from a signed
-- email-list link). Turns off every account with that address and marks
-- every email-list row for it unsubscribed, also killing any pending
-- confirmation link. Signing up again needs a fresh confirmation; an
-- account holder can switch the brief back on in Watchlist settings.
create or replace function public.brief_unsubscribe(
  p_user_id uuid default null, p_email text default null)
returns text
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_email text;
begin
  if p_user_id is not null then
    update public.profiles set daily_brief_enabled = false where id = p_user_id;
    select lower(btrim(email)) into v_email from auth.users where id = p_user_id;
  else
    v_email := lower(btrim(coalesce(p_email, '')));
  end if;
  if coalesce(v_email, '') = '' then
    return 'ok';
  end if;
  update public.email_signups
     set unsubscribed_at    = coalesce(unsubscribed_at, now()),
         confirm_token_hash = null,
         confirm_expires_at = null
   where lower(email) = v_email;
  update public.profiles set daily_brief_enabled = false
   where id in (select id from auth.users where lower(btrim(email)) = v_email);
  return 'ok';
end;
$$;

-- Only the service role (the worker) may call these.
revoke all on function public.brief_signup_claim(text, text, text, text) from public, anon, authenticated;
revoke all on function public.brief_signup_release(text, text) from public, anon, authenticated;
revoke all on function public.brief_confirm(text) from public, anon, authenticated;
revoke all on function public.brief_unsubscribe(uuid, text) from public, anon, authenticated;
grant execute on function public.brief_signup_claim(text, text, text, text) to service_role;
grant execute on function public.brief_signup_release(text, text) to service_role;
grant execute on function public.brief_confirm(text) to service_role;
grant execute on function public.brief_unsubscribe(uuid, text) to service_role;

-- 4. Lock the table to the service role ----------------------------------
alter table public.email_signups enable row level security;
drop policy if exists "es_insert_any" on public.email_signups;
revoke all on table public.email_signups from anon, authenticated;

-- Checks:
--   no policies left (should return 0 rows):
--     select policyname from pg_policies
--     where schemaname = 'public' and tablename = 'email_signups';
--   the delivery log has its normalized column (should return 1 row):
--     select column_name from information_schema.columns
--     where table_schema = 'public' and table_name = 'email_log'
--       and column_name = 'email_norm';
--   the four functions exist (should return 4 rows):
--     select proname from pg_proc
--     where proname in ('brief_signup_claim', 'brief_signup_release',
--                       'brief_confirm', 'brief_unsubscribe');
