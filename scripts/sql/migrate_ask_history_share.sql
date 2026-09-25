-- Migration: public share links for stored AI answers (deep-link Phase 4).
-- Run this once in the Supabase SQL Editor (the CLI pooler times out).
-- Idempotent: safe to run again.
--
-- Prerequisite: scripts/sql/migrate_ask_history_safety_metadata.sql (adds
-- ask_history.safety_class, which the public answer payload returns so a
-- shared referral answer keeps its referral banner).
--
-- What it does:
--   * share_token      -- the unguessable token in a public link (/?a=<token>).
--                         NULL while the answer is private.
--   * is_public        -- true while the link works. Revoking sets it false and
--                         clears share_token, so the old link 404s for good;
--                         sharing again mints a new token.
--   * shared_at        -- when the current link was created.
--   * share_revoked_at -- when the owner last stopped sharing.
--
-- Access model is unchanged: ask_history keeps RLS enabled with NO policies
-- and no anon/authenticated grants -- it stays service-role only. The public
-- read happens server-side in backend/routes_answer_share.py, which selects
-- only the answer columns (never user_id or the row id).
--
-- Until this runs, the share endpoints answer 503 {"code":"share_unavailable"}
-- and the Copy link button falls back to the owner-only ?chat= link.

ALTER TABLE public.ask_history
    ADD COLUMN IF NOT EXISTS share_token TEXT,
    ADD COLUMN IF NOT EXISTS is_public BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS shared_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS share_revoked_at TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS ask_history_share_token_key
    ON public.ask_history (share_token) WHERE share_token IS NOT NULL;

ALTER TABLE public.ask_history DROP CONSTRAINT IF EXISTS ask_history_public_needs_token;
ALTER TABLE public.ask_history ADD CONSTRAINT ask_history_public_needs_token
    CHECK (NOT is_public OR share_token IS NOT NULL);
