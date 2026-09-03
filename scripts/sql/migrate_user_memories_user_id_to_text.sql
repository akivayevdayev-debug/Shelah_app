-- Migration: normalize user_memories.user_id to TEXT (Clerk user IDs).
-- Run this once in the Supabase SQL Editor for your project.
--
-- Provenance (plan.md §21, Prompt 34, found 2026-08-31): scripts/verify_rls.py's
-- first live run against the real project failed INSERT on user_memories
-- with `22P02 invalid input syntax for type uuid: "user_3Ih9j..."` -- the
-- exact same failure scripts/migrate_user_preferences_user_id_to_text.sql
-- (2026-08-21) predicted for user_preferences if that column was ever
-- created/left as `uuid`, now empirically confirmed live for this table
-- too. backend/rag.py's _fetch_user_memory_summaries/_store_user_memory_summary
-- query/insert user_memories.user_id with the raw Clerk `sub` claim (format
-- `user_XXXXXXXX...`), which is not valid `uuid` syntax -- every real
-- personalization read/write silently fails this way. This is plan.md
-- §21.1's "dangerous one": these calls have NO user-visible symptom (the
-- write failure is swallowed via _capture_backend_error, kept non-fatal by
-- design), so this exact bug could have been silently degrading
-- personalization for every real user with nothing surfacing it before
-- this live test. scripts/sql/rag_identity_cache_setup.sql already
-- declares this column as `text`; this migration brings a drifted live
-- schema in line with that tracked definition.
--
-- Same technique as migrate_user_preferences_user_id_to_text.sql, for the
-- same reason: Postgres refuses ALTER COLUMN TYPE while any policy's
-- USING/WITH CHECK expression references the column, and won't do the
-- drop-alter-recreate dance for you. This discovers whatever policies
-- actually exist on the table at run time, drops them, performs the type
-- change, then recreates each one verbatim from what Postgres itself
-- reported -- safe regardless of which policies are currently live. This
-- matters more here than on any other table: plan.md §30.5 already found
-- this table's live policies had drifted into a contradictory state across
-- two tracked SQL files before that was resolved 2026-08-31 -- discovering
-- live policies at run time rather than assuming either tracked file is
-- authoritative is exactly the caution that finding calls for.
--
-- Idempotent: if the column is already `text`, `ALTER COLUMN ... TYPE text`
-- is a no-op and this is always safe to re-run.

BEGIN;

CREATE TEMP TABLE _um_saved_policies ON COMMIT DROP AS
SELECT policyname, permissive, cmd, roles, qual, with_check
FROM pg_policies
WHERE schemaname = 'public' AND tablename = 'user_memories';

DO $$
DECLARE
    pol RECORD;
BEGIN
    FOR pol IN SELECT policyname FROM _um_saved_policies LOOP
        EXECUTE format('DROP POLICY %I ON public.user_memories', pol.policyname);
    END LOOP;
END $$;

ALTER TABLE public.user_memories
    ALTER COLUMN user_id TYPE text USING user_id::text;

DO $$
DECLARE
    pol RECORD;
BEGIN
    FOR pol IN SELECT * FROM _um_saved_policies LOOP
        EXECUTE format(
            'CREATE POLICY %I ON public.user_memories AS %s FOR %s TO %s%s%s',
            pol.policyname,
            pol.permissive,
            pol.cmd,
            array_to_string(pol.roles, ', '),
            CASE WHEN pol.qual IS NOT NULL THEN format(' USING (%s)', pol.qual) ELSE '' END,
            CASE WHEN pol.with_check IS NOT NULL THEN format(' WITH CHECK (%s)', pol.with_check) ELSE '' END
        );
    END LOOP;
END $$;

COMMIT;

-- After running: verify nothing was lost --
--   SELECT policyname, cmd, qual, with_check FROM pg_policies
--   WHERE schemaname = 'public' AND tablename = 'user_memories';
-- should list the exact same policies (by name and clause) as before.
