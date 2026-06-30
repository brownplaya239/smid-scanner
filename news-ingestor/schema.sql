-- news_live — real-time headline tape backing TickerDesk's "Live tape".
-- Written by the Alpaca→Supabase ingestor (index.mjs) with the SERVICE ROLE
-- key; read by browsers with the anon/publishable key over Supabase Realtime.
-- Headlines are public, so anon SELECT is allowed; there is NO write policy for
-- anon (only the service role, which bypasses RLS, may insert/delete).
--
-- Run once in the Supabase SQL editor. Re-runnable (idempotent).

create table if not exists public.news_live (
  id           text primary key,          -- Alpaca/Benzinga article id (dedupe)
  headline     text not null,
  summary      text,
  source       text,                       -- publisher (e.g. "benzinga")
  url          text,
  symbols      text[] default '{}',        -- tagged tickers
  published_at timestamptz,                 -- article publish time
  created_at   timestamptz not null default now()
);

create index if not exists news_live_published_idx
  on public.news_live (published_at desc);

alter table public.news_live enable row level security;

drop policy if exists "news_live anon read" on public.news_live;
create policy "news_live anon read"
  on public.news_live for select
  to anon, authenticated
  using (true);

-- Add to the Realtime publication so subscribed browsers get INSERTs.
do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname = 'supabase_realtime'
      and schemaname = 'public'
      and tablename  = 'news_live'
  ) then
    alter publication supabase_realtime add table public.news_live;
  end if;
end $$;

-- Retention: the ingestor prunes rows older than 3 days hourly. If you prefer
-- the database to do it and pg_cron is enabled, uncomment:
-- select cron.schedule('news_live_prune', '*/30 * * * *',
--   $$delete from public.news_live where published_at < now() - interval '3 days'$$);
