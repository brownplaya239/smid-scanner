-- ============================================================================
-- 2026-11-16_report_usage_duplicates.sql   (read-only check)
-- Finds reports that were counted twice while the report-usage change
-- (PR #8) was rolling out.
--
-- How a double count happens: an old copy of the site (a tab opened before
-- the deploy) wrote its own report_generations row just before asking the
-- worker for a report, and the new worker then recorded the same report
-- again. Running sql/2026-11-10_lock_plans_and_report_usage.sql BEFORE the
-- merge stops old pages from writing rows, so this should return nothing.
--
-- Run in the Supabase SQL editor after merging. Expected: 0 rows.
-- Each row is a pair: the same account, ticker and report type, recorded
-- twice within 2 minutes. `earlier_id` is the likely browser-written copy.
-- (Someone really running the same report twice inside 2 minutes would
-- also show up, so look before deleting.)
-- ============================================================================
select later.user_id,
       later.ticker,
       later.report_type,
       earlier.id         as earlier_id,
       earlier.created_at as earlier_at,
       later.id           as later_id,
       later.created_at   as later_at
from public.report_generations later
join public.report_generations earlier
  on  earlier.user_id     = later.user_id
  and earlier.ticker      = later.ticker
  and earlier.report_type = later.report_type
  and earlier.created_at  <  later.created_at
  and earlier.created_at  >= later.created_at - interval '2 minutes'
where later.created_at >= timestamptz '2026-11-01'
order by later.created_at desc;

-- To remove the extra copies after checking the list above, run:
--
-- delete from public.report_generations r
-- where r.created_at >= timestamptz '2026-11-01'
--   and exists (select 1 from public.report_generations l
--               where l.user_id = r.user_id and l.ticker = r.ticker
--                 and l.report_type = r.report_type
--                 and l.created_at > r.created_at
--                 and l.created_at <= r.created_at + interval '2 minutes');
