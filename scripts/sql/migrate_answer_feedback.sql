-- Migration: create answer_feedback table (plan.md §12.4)
-- Run this once in the Supabase SQL Editor for your project.
--
-- Each row is one thumbs up/down (+ optional comment) a reader leaves on an
-- AI answer. Unlike ask_history, user_id is NULLABLE: feedback is accepted
-- from signed-out readers too, so there is no "owner" row-per-user
-- invariant to enforce with RLS the way ask_history does.

CREATE TABLE IF NOT EXISTS public.answer_feedback (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         TEXT,
    question_hash   TEXT        NOT NULL,
    verdict         TEXT        NOT NULL CHECK (verdict IN ('helpful', 'not_helpful')),
    comment         TEXT        NOT NULL DEFAULT '' CHECK (char_length(comment) <= 500),
    mode            TEXT        NOT NULL DEFAULT 'balanced',
    language        TEXT        NOT NULL DEFAULT 'en',
    fallback        BOOLEAN     NOT NULL DEFAULT false,
    safety_class    TEXT        NOT NULL DEFAULT 'ok',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for the devtools feedback-digest view (recent-first scan).
CREATE INDEX IF NOT EXISTS answer_feedback_created_idx
    ON public.answer_feedback (created_at DESC);

-- Enable Row Level Security
ALTER TABLE public.answer_feedback ENABLE ROW LEVEL SECURITY;

-- RLS policy: anyone (anonymous or signed-in) may INSERT feedback -- this is
-- a public write-only form, not a per-user-owned resource. There is
-- deliberately no SELECT policy for the anon/authenticated roles: only the
-- backend's secret-key client (which bypasses RLS) reads this table, via the
-- devtools feedback-digest route.
--
-- DROP + CREATE (not CREATE ... IF NOT EXISTS) because Postgres has no
-- IF NOT EXISTS form for CREATE POLICY, unlike CREATE TABLE/CREATE INDEX
-- above -- without the DROP, re-running this file after a first
-- successful run fails with 42710 "policy already exists" on this
-- statement alone, even though the table/index/RLS-enable above are all
-- already idempotent.
DROP POLICY IF EXISTS "anyone_can_submit_feedback" ON public.answer_feedback;
CREATE POLICY "anyone_can_submit_feedback"
    ON public.answer_feedback
    FOR INSERT
    WITH CHECK (true);
