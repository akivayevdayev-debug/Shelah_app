-- Sh'elah AI usage/cost ledger — base table (plan.md §23.2.1 / Prompt 36).
--
-- This table's CREATE TABLE was never committed anywhere in this repo —
-- only later migrations that ALTER it (migrate_ai_usage_log_add_user_
-- columns.sql, scripts/sql/check_and_reserve_user_budget.sql). Run THIS
-- file FIRST, before either of those, in a project where the table does
-- not already exist.
--
-- Columns match exactly what backend/cost_meter.py writes/reads and what
-- scripts/recompute_ai_usage_log_cost.py looks up by `id` — derived from
-- application code, not introspected from a live table, since no live
-- table existed to introspect (this file is the reason why).
--
-- Service-role-only, same as the two migrations that extend it: no RLS,
-- no anon/authenticated grants. The app only ever accesses this table via
-- the server-side SUPABASE_SECRET_KEY client.

create table if not exists public.ai_usage_log (
    id uuid primary key default gen_random_uuid(),
    provider text,
    model text,
    input_tokens integer,
    output_tokens integer,
    cost_usd numeric not null default 0,
    route text,
    request_id text,
    created_at timestamptz not null default now()
);

-- Supports the "sum today's cost" queries in check_user_budget_and_enforce
-- / _fetch_today_usage_cost_for_key, and the recompute script's full scan.
create index if not exists ai_usage_log_created_at_idx
    on public.ai_usage_log (created_at);

-- No RLS: this table is written and read exclusively by the server-side
-- Supabase client (SUPABASE_SECRET_KEY), never from the browser — same
-- posture documented on scripts/sql/check_and_reserve_user_budget.sql's
-- function-level REVOKE ALL FROM PUBLIC.
