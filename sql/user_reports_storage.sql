-- ============================================================================
-- user_reports_storage.sql
-- Per-user private report history for TickerDesk ad-hoc + alt-data reports.
--
-- Run this ONCE in the Supabase SQL editor (Dashboard → SQL → New query →
-- paste → Run). It is idempotent — safe to re-run.
--
-- After this runs, generated ad-hoc/alt-data reports are uploaded to a
-- PRIVATE Storage bucket scoped by user_id (never the public Pages site),
-- and each user can pull back only their own report history.
-- ============================================================================

-- 1) Private Storage bucket for user-scoped report PDFs -----------------------
insert into storage.buckets (id, name, public)
values ('user-reports', 'user-reports', false)
on conflict (id) do update set public = false;

-- 2) Columns on report_generations so we can link a row to its stored PDF -----
--    (report_generations already exists for rate-limiting; we extend it.)
alter table public.report_generations
  add column if not exists kind          text,      -- 'adhoc' | 'altdata'
  add column if not exists storage_path  text,      -- '<user_id>/<file>.pdf'
  add column if not exists completed_at  timestamptz;

-- Helpful index for "my reports, newest first"
create index if not exists report_generations_user_created_idx
  on public.report_generations (user_id, created_at desc);

-- 3) RLS on report_generations: a user can read ONLY their own rows -----------
alter table public.report_generations enable row level security;

drop policy if exists "own report_generations select" on public.report_generations;
create policy "own report_generations select"
  on public.report_generations
  for select
  using (auth.uid() = user_id);

-- (Inserts/updates from the app already run as the user or via the service
--  role in CI, which bypasses RLS — no extra write policy needed here.)

-- 4) Storage RLS: a user can read ONLY objects under their own <uid>/ prefix --
--    The service_role (CI upload) bypasses RLS, so no insert policy is needed
--    for the pipeline. This SELECT policy is what lets a signed-in user (or a
--    signed URL minted on their behalf) read back their own PDFs.
drop policy if exists "user reads own report objects" on storage.objects;
create policy "user reads own report objects"
  on storage.objects
  for select
  to authenticated
  using (
    bucket_id = 'user-reports'
    and (storage.foldername(name))[1] = auth.uid()::text
  );

-- ============================================================================
-- Done. No secrets here. The pipeline uploads with the existing
-- SUPABASE_SERVICE_KEY (already a CI secret); the worker mints short-lived
-- signed URLs for the client. Nothing user-generated hits the public site.
-- ============================================================================
