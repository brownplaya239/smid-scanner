-- ============================================================================
-- 2026-11-16_news_alert_throttle.sql
-- One small table that stops the worker's ops alerts (starting with "the
-- news wire is silent") from posting more than once an hour, even when two
-- worker runs overlap.
--
-- Run this ONCE in the Supabase SQL editor (Dashboard -> SQL -> New query ->
-- paste -> Run). It is idempotent: safe to re-run.
--
-- How it is used: the worker asks "may I alert?" with a single conditional
-- UPDATE (PATCH ...?key=eq.news_stall&last_alert_at=lt.<now - 1 hour>).
-- Postgres lets exactly one overlapping run win that update, and only the
-- winner posts. If this table is missing, the worker just logs the alert
-- and never posts.
-- ============================================================================

create table if not exists public.ops_alert_throttle (
  key           text primary key,
  last_alert_at timestamptz not null default 'epoch'
);

-- Only the worker (service-role key, which bypasses RLS) touches this table.
-- RLS on with no policies keeps the public anon key out entirely.
alter table public.ops_alert_throttle enable row level security;
revoke all on public.ops_alert_throttle from anon, authenticated;

-- The row the news-wire alert claims. Existing rows are left alone.
insert into public.ops_alert_throttle (key)
values ('news_stall')
on conflict (key) do nothing;
