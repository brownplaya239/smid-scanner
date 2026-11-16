-- 2026-11-16 — Daily Brief sign-ups that actually subscribe people.
--
-- Run ONCE in the Supabase SQL editor, BEFORE merging the PR that adds
-- this file (merging deploys the worker, which writes these columns).
-- Safe to run again: every statement is idempotent.
--
-- What it does:
--   1. Adds the double opt-in columns the worker and daily_brief.py use.
--   2. Removes the public INSERT policy. The site's forms now go through
--      the worker (POST /brief/subscribe), which uses the service key, so
--      the public (anon) and signed-in (authenticated) roles need no
--      access at all. RLS stays ON with no policies = no public reads or
--      writes.

-- 1. Columns ---------------------------------------------------------------
alter table public.email_signups
  add column if not exists confirmed_at    timestamptz,  -- clicked the confirm link
  add column if not exists unsubscribed_at timestamptz,  -- clicked unsubscribe
  add column if not exists confirm_sent_at timestamptz;  -- last confirm email (throttle)

-- The brief job reads exactly this set every morning.
create index if not exists email_signups_active
  on public.email_signups (email)
  where confirmed_at is not null and unsubscribed_at is null;

-- 2. Lock the table to the service role ----------------------------------
alter table public.email_signups enable row level security;
drop policy if exists "es_insert_any" on public.email_signups;
revoke all on table public.email_signups from anon, authenticated;

-- Check (should return 0 rows: no policies left on the table):
--   select policyname from pg_policies
--   where schemaname = 'public' and tablename = 'email_signups';
