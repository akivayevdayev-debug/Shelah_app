-- Migration: add the missing DEFAULT now() to user_preferences.updated_at.
-- Run this once in the Supabase SQL Editor for your project. Idempotent --
-- safe to re-run (SET DEFAULT is not additive, it just re-sets the same
-- value each time).
--
-- Provenance (plan.md §21, Prompt 34, found 2026-08-31): after the
-- auth.uid()-to-auth.jwt() fix (scripts/migrate_rls_use_jwt_sub_not_auth_uid.sql)
-- resolved RLS itself, scripts/verify_rls.py's next live run passed cleanly
-- for study_bookmarks and user_memories but failed differently for
-- user_preferences: `23502 null value in column "updated_at" violates
-- not-null constraint` on INSERT. scripts/sql/bookmarks_and_preferences_setup.sql
-- has always declared `updated_at timestamptz not null default now()` for
-- this table, and study_bookmarks'/user_memories' equivalent columns clearly
-- have a working default live (their INSERTs, built the same way by the same
-- script, succeeded) -- so this is schema drift specific to this one column
-- on this one table, not a script bug: the live column is NOT NULL but has
-- no DEFAULT.
--
-- This never surfaced through the real app because backend/routes_user.py's
-- PUT /api/user/preferences (line ~219) always sets updated_at explicitly on
-- every write -- only scripts/verify_rls.py's intentionally-minimal direct
-- INSERT (which omits it, to test RLS itself rather than app plumbing)
-- exposed the missing default.

BEGIN;

ALTER TABLE public.user_preferences
    ALTER COLUMN updated_at SET DEFAULT now();

COMMIT;

-- After running: verify the default is live --
--   SELECT column_name, column_default FROM information_schema.columns
--   WHERE table_schema = 'public' AND table_name = 'user_preferences'
--     AND column_name = 'updated_at';
-- column_default should read `now()`.
