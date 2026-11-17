// A small stand-in for the parts of the live Supabase database that the
// sql/ scripts touch: auth.users, the anon / authenticated / service_role
// roles, the tables as they were before 2026-11-16, and their row-security
// rules (docs/SUPABASE_SETUP.md). Used by profiles_sql_check.mjs and by
// stripe_webhook_profile_test.py's PGlite mode.
export const BASE_SCHEMA = `
create role anon nologin; create role authenticated nologin;
create role service_role nologin bypassrls;
create schema auth;
create table auth.users (id uuid primary key, email text, created_at timestamptz default now());
create function auth.uid() returns uuid language sql stable as
  $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
grant usage on schema public, auth to anon, authenticated, service_role;
grant execute on function auth.uid() to anon, authenticated, service_role;
-- Supabase's default privileges: new tables / functions are open to the API roles.
alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
alter default privileges in schema public grant execute on functions to anon, authenticated, service_role;
create table public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  display_name text,
  subscription_tier text default 'free' check (subscription_tier in ('free','pro','premium','beta')),
  daily_brief_enabled boolean, created_at timestamptz default now());
create table public.watchlists (user_id uuid references auth.users(id) on delete cascade,
  ticker text not null, added_at timestamptz default now(), primary key (user_id, ticker));
create table public.report_generations (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users(id) on delete cascade,
  ticker text not null, report_type text not null,
  status text default 'queued' check (status in ('queued','running','done','failed')),
  cost_units numeric default 1, created_at timestamptz default now());
alter table public.profiles enable row level security;
alter table public.watchlists enable row level security;
alter table public.report_generations enable row level security;
create policy pr_own_read on public.profiles for select using (auth.uid() = id);
create policy pr_own_write on public.profiles for all using (auth.uid() = id) with check (auth.uid() = id);
create policy wl_own_write on public.watchlists for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy rg_own on public.report_generations for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
alter table public.profiles
  add column subscription_expires_at timestamptz, add column promo_code_redeemed text,
  add column promo_code_redeemed_at timestamptz, add column stripe_customer_id text;
`;
