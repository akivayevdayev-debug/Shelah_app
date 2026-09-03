-- Migration: add caller-attribution columns to ai_usage_log (security audit
-- P1 "No ceiling on cumulative AI spend").
-- Run this once in the Supabase SQL Editor for your project.
--
-- Provenance (plan.md §23.2.1): this file was untracked in git until this
-- pass -- provenance unknown as of 2026-08-21. Application code
-- (backend/cost_meter.py) has depended on these columns for some time and
-- the test suite covers the code path, but neither confirms whether/when
-- this migration was actually run against the live Supabase project (no
-- direct Postgres/information_schema access from this environment --
-- see scripts/generate_database_doc.py, plan.md §23.2.3).
--
-- backend/cost_meter.py::record_llm_call now tags every inserted row with
-- user_id (authenticated Clerk sub) or client_key (an "ip:<addr>" fallback
-- for anonymous callers), and check_user_budget_and_enforce() queries this
-- table by those columns before every /ask model call to enforce
-- PER_USER_DAILY_BUDGET_USD. Until this migration runs, the insert/select
-- against these columns fails and is caught by the existing broad
-- except-and-log-nothing handlers — the budget check silently fails open
-- (no cap enforced) rather than breaking /ask.

ALTER TABLE public.ai_usage_log
    ADD COLUMN IF NOT EXISTS user_id TEXT,
    ADD COLUMN IF NOT EXISTS client_key TEXT;

-- Supports the "sum today's cost for this caller" query pattern (WHERE
-- user_id = ? / client_key = ? AND created_at >= start_of_day).
CREATE INDEX IF NOT EXISTS ai_usage_log_user_id_created_at_idx
    ON public.ai_usage_log (user_id, created_at);
CREATE INDEX IF NOT EXISTS ai_usage_log_client_key_created_at_idx
    ON public.ai_usage_log (client_key, created_at);
