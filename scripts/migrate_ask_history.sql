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

-- RLS policy: users can only access their own rows.
-- Clerk JWTs are forwarded to Supabase; the `sub` claim is the user_id.
-- Adjust the claim path if your Supabase JWT template uses a different key.
CREATE POLICY "user_own_history"
    ON public.ask_history
    FOR ALL
    USING (
        user_id = (current_setting('request.jwt.claims', true)::jsonb ->> 'sub')
    );
