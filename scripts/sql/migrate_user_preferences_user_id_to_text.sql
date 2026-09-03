-- Migration: normalize user_preferences.user_id to TEXT (Clerk user IDs).
-- Run this once in the Supabase SQL Editor for your project.
--
-- Provenance (plan.md §23.2.1): this file was untracked in git until this
-- pass -- provenance unknown as of 2026-08-21. Whether the live
-- user_preferences.user_id column is already `text` (this migration is
-- then a no-op) or still needed has not been independently confirmed from
-- this environment (see scripts/generate_database_doc.py, plan.md
-- §23.2.3). It is idempotent either way, so re-running it is always safe.
--
-- backend/routes_user.py::_user_preferences_get_response /
-- _user_preferences_put_response query/upsert user_preferences.user_id with
-- the raw Clerk `sub` claim (format `user_XXXXXXXX...`).
-- scripts/sql/bookmarks_and_preferences_setup.sql already defines this
-- column as `text`, matching every other Clerk-keyed table in this schema
-- (study_bookmarks.user_id, ask_history.user_id, ai_usage_log.user_id --
-- see scripts/migrate_ai_usage_log_add_user_columns.sql). If this
-- project's user_preferences.user_id was instead created (or later
-- changed, out-of-band via the dashboard) as `uuid`, every
-- GET/PUT /api/user/preferences call fails with
-- `22P02 invalid input syntax for type uuid` on the Clerk id literal.
--
-- Update (2026-08-21, plan.md §30.5): a first run of this file failed with
-- `0A000: cannot alter type of a column used in a policy definition`,
-- naming a live policy -- "Users can manage their own preferences" -- that
-- exists on the real project but is in NO tracked SQL file in this repo
-- (neither scripts/sql/SUPABASE_RLS_POLICIES.sql's four named policies,
-- nor anything else here). Postgres refuses to change a column's type
-- while any policy's USING/WITH CHECK expression references it, and won't
-- do the drop-alter-recreate dance for you. Rather than guess at that
-- policy's exact definition (or any others that may also be live and
-- equally untracked), this version discovers whatever policies actually
-- exist on the table at run time, drops them, performs the type change,
-- then recreates each one verbatim from what Postgres itself reported --
-- so this is safe regardless of which policies are currently live or
-- what named this repo does or doesn't know about.

BEGIN;

CREATE TEMP TABLE _up_saved_policies ON COMMIT DROP AS
SELECT policyname, permissive, cmd, roles, qual, with_check
FROM pg_policies
WHERE schemaname = 'public' AND tablename = 'user_preferences';

DO $$
DECLARE
    pol RECORD;
BEGIN
    FOR pol IN SELECT policyname FROM _up_saved_policies LOOP
        EXECUTE format('DROP POLICY %I ON public.user_preferences', pol.policyname);
    END LOOP;
END $$;

ALTER TABLE public.user_preferences
    ALTER COLUMN user_id TYPE text USING user_id::text;

DO $$
DECLARE
    pol RECORD;
BEGIN
    FOR pol IN SELECT * FROM _up_saved_policies LOOP
        EXECUTE format(
            'CREATE POLICY %I ON public.user_preferences AS %s FOR %s TO %s%s%s',
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
--   WHERE schemaname = 'public' AND tablename = 'user_preferences';
-- should list the exact same policies (by name and clause) as before.
