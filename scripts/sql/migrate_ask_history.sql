-- Migration: create ask_history table for per-user ask history
-- Run this once in the Supabase SQL Editor for your project.
--
-- Each row stores one completed AI ask interaction for a signed-in user.
-- The user_id column holds the Clerk `sub` claim (e.g. "user_abc123").
-- RLS ensures users can only read/delete their own rows.

CREATE TABLE IF NOT EXISTS public.ask_history (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         TEXT        NOT NULL,
    question        TEXT        NOT NULL,
    answer          TEXT        NOT NULL DEFAULT '',
    sources         JSONB       NOT NULL DEFAULT '[]'::jsonb,
    ai_cited_sources TEXT[]     NOT NULL DEFAULT '{}',
    community       TEXT        NOT NULL DEFAULT 'All',
    mode            TEXT        NOT NULL DEFAULT 'balanced',
    language        TEXT        NOT NULL DEFAULT 'en',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for fast per-user history lookups ordered by recency
CREATE INDEX IF NOT EXISTS ask_history_user_idx
    ON public.ask_history (user_id, created_at DESC);

-- Enable Row Level Security
ALTER TABLE public.ask_history ENABLE ROW LEVEL SECURITY;

-- Service-role-only by design, decided 2026-08-31 (plan.md §21, STEP 6a):
-- backend/routes_user.py's GET/DELETE /api/user/history handlers both read
-- this table through the service-role client (_get_supabase_client()) with
-- a hand-written .eq("user_id", ...) filter, never through the RLS-gated
-- user-scoped client -- so a per-user policy here is written but never
-- actually exercised by any code path. This table previously had a
-- "user_own_history" policy using current_setting('request.jwt.claims',
-- true)::jsonb ->> 'sub' instead of auth.uid(), which was also the only
-- table in this schema using that idiom instead of the other three's
-- auth.uid()::text = user_id -- dropped rather than migrated, since fixing
-- the idiom on a policy nothing evaluates would still leave the table
-- without a real RLS backstop. No SELECT/INSERT/UPDATE/DELETE grants to
-- anon/authenticated exist on this table by default in a new Supabase
-- project, so dropping the (dead) policy does not newly expose it -- same
-- posture already documented for ai_usage_log
-- (scripts/sql/ai_usage_log_setup.sql). If a user-scoped read/write path
-- for ask_history is ever added, re-add an auth.uid()::text = user_id
-- policy at that time, matching the other three tables' idiom.
DROP POLICY IF EXISTS "user_own_history" ON public.ask_history;
