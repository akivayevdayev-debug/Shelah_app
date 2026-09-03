-- Migration: replace ad-hoc/untracked RLS policies on user_preferences with
-- the canonical, correctly-typed policy set. Idempotent -- safe to re-run.
--
-- Root cause (found 2026-08-21/22 while live-verifying plan.md §29.6's fix):
-- scripts/migrate_user_preferences_user_id_to_text.sql fixed the COLUMN
-- type (user_id uuid -> text), but that migration's own strategy was to
-- discover whatever RLS policies were live and recreate them VERBATIM --
-- including a policy named "Users can manage their own preferences" that is
-- in NO tracked SQL file in this repo (plan.md §30's 2026-08-21 update
-- already flagged this policy as a previously-unknown live artifact, likely
-- created via the Supabase dashboard's own wizard back when the column was
-- still `uuid`). Preserving it verbatim carried its stale `::uuid`-typed
-- comparison forward unchanged into the new `text` schema.
--
-- Live confirmation (2026-08-22): a direct read of user_preferences via
-- both the service-role and anon keys with a fabricated (non-existent)
-- user_id succeeds -- proving the COLUMN itself is `text`. But the actual
-- authenticated app path (GET /api/user/preferences, using
-- backend/routes_user.py's RLS-scoped client with a real signed-in Clerk
-- session, real user_id "user_3DJ2PONd1x9zBnfVRlfiGhMzQPr") reproducibly
-- still 500s with the exact same `22P02 invalid input syntax for type
-- uuid` error, across 3 separate requests over 4+ minutes -- ruling out
-- schema-cache propagation lag. Because RLS-protected tables are enforced
-- as security barriers, ALL applicable PERMISSIVE policies for a role must
-- be evaluated (Postgres cannot skip an erroring policy just because
-- another one would also apply), so a single stale policy with a bad cast
-- is enough to break every authenticated request that touches a real row,
-- even though unauthenticated/service-role reads (which never hit RLS, or
-- hit a different policy path) look fine.
--
-- Fix: rather than trying to guess-and-repair that untracked policy's
-- exact clause, drop every non-canonical policy on user_preferences and
-- (re)create the canonical four from scripts/sql/bookmarks_and_preferences_setup.sql
-- (auth.uid()::text = user_id -- text-vs-text, correct regardless of
-- whatever the column type used to be). This also self-heals if
-- bookmarks_and_preferences_setup.sql's own canonical policies were never
-- actually applied to this project in the first place, which the observed
-- behavior suggests may be the case.

BEGIN;

DO $$
DECLARE
    pol RECORD;
BEGIN
    FOR pol IN
        SELECT policyname FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'user_preferences'
          AND policyname NOT IN (
              'user_preferences_select_own',
              'user_preferences_insert_own',
              'user_preferences_update_own',
              'user_preferences_delete_own'
          )
    LOOP
        RAISE NOTICE 'Dropping non-canonical policy: %', pol.policyname;
        EXECUTE format('DROP POLICY %I ON public.user_preferences', pol.policyname);
    END LOOP;
END $$;

drop policy if exists user_preferences_select_own on public.user_preferences;
create policy user_preferences_select_own
on public.user_preferences
for select
using (auth.uid()::text = user_id);

drop policy if exists user_preferences_insert_own on public.user_preferences;
create policy user_preferences_insert_own
on public.user_preferences
for insert
with check (auth.uid()::text = user_id);

drop policy if exists user_preferences_update_own on public.user_preferences;
create policy user_preferences_update_own
on public.user_preferences
for update
using (auth.uid()::text = user_id)
with check (auth.uid()::text = user_id);

drop policy if exists user_preferences_delete_own on public.user_preferences;
create policy user_preferences_delete_own
on public.user_preferences
for delete
using (auth.uid()::text = user_id);

COMMIT;

-- After running: verify only the four canonical policies remain --
--   SELECT policyname, cmd, qual, with_check FROM pg_policies
--   WHERE schemaname = 'public' AND tablename = 'user_preferences';
-- every qual/with_check should read `(auth.uid() = user_id)` with auth.uid()
-- cast to text (`(auth.uid())::text = user_id` or equivalent), never a cast
-- applied to user_id itself.
