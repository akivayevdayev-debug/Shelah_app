-- Migration: replace auth.uid() with (auth.jwt() ->> 'sub') in every RLS
-- policy on user_preferences, study_bookmarks, user_memories.
-- Run this once in the Supabase SQL Editor for your project. Idempotent --
-- safe to re-run.
--
-- Root cause (plan.md §21, Prompt 34, found 2026-08-31 during scripts/verify_rls.py's
-- live acceptance run): Supabase's built-in auth.uid() is defined to pull
-- the JWT's `sub` claim and cast it directly to `uuid` -- correct for
-- Supabase's own native auth (UUID user ids), but Clerk's `sub` claims are
-- never UUID-shaped (format `user_XXXXXXXXXXXXXXXXXXXXXXXXXXX`). Calling
-- auth.uid() for a Clerk-authenticated request throws
-- `22P02 invalid input syntax for type uuid: "user_..."` INSIDE auth.uid()
-- itself, before a policy's own `(auth.uid())::text = user_id` cast ever
-- gets a chance to run -- that outer cast only wraps auth.uid()'s
-- already-computed return value, it cannot protect against an exception
-- thrown while computing it. This is a different, deeper failure mode than
-- plan.md §21.1 originally anticipated (auth.uid() resolving NULL and
-- silently returning zero rows if Third-Party Auth were misconfigured) --
-- it means NO Clerk-authenticated request can ever satisfy any policy that
-- calls auth.uid(), REGARDLESS of whether Third-Party Auth is enabled or
-- how correctly every other layer (column types, JWT verification, policy
-- authorship) is configured. Confirmed live: three consecutive verify_rls.py
-- attempts against all three tables, after independently ruling out
-- migration-not-applied, PostgREST schema-cache staleness, wrong project,
-- and stale/non-canonical policies (a `pg_policies` dump showed only the
-- four clean canonical auth.uid()-based policies per table, nothing else).
--
-- Fix: (auth.jwt() ->> 'sub') reads the raw JWT claim as text via plain
-- JSON extraction -- no internal uuid cast, works for any issuer's subject
-- format. This is the same idiom scripts/migrate_ask_history.sql's now-
-- dropped ask_history policy used (current_setting('request.jwt.claims',
-- true)::jsonb ->> 'sub') -- that policy was treated as "the odd one out"
-- earlier in this project's history because it didn't match the
-- auth.uid()-based "canonical" pattern on the other tables; it turns out
-- to have been the only Clerk-compatible idiom in the codebase, and the
-- "canonical" one was the actual bug.
--
-- Same drop-and-recreate technique as the sibling migrations in this
-- directory. Table/policy names are exact matches confirmed live via
-- pg_policies (2026-08-31) -- all four canonical policies on all three
-- tables, nothing else, so this replaces the full known set rather than
-- discovering unknowns at run time.

BEGIN;

-- user_preferences
drop policy if exists user_preferences_select_own on public.user_preferences;
create policy user_preferences_select_own
on public.user_preferences
for select
using ((auth.jwt() ->> 'sub') = user_id);

drop policy if exists user_preferences_insert_own on public.user_preferences;
create policy user_preferences_insert_own
on public.user_preferences
for insert
with check ((auth.jwt() ->> 'sub') = user_id);

drop policy if exists user_preferences_update_own on public.user_preferences;
create policy user_preferences_update_own
on public.user_preferences
for update
using ((auth.jwt() ->> 'sub') = user_id)
with check ((auth.jwt() ->> 'sub') = user_id);

drop policy if exists user_preferences_delete_own on public.user_preferences;
create policy user_preferences_delete_own
on public.user_preferences
for delete
using ((auth.jwt() ->> 'sub') = user_id);

-- study_bookmarks
drop policy if exists study_bookmarks_select_own on public.study_bookmarks;
create policy study_bookmarks_select_own
on public.study_bookmarks
for select
using ((auth.jwt() ->> 'sub') = user_id);

drop policy if exists study_bookmarks_insert_own on public.study_bookmarks;
create policy study_bookmarks_insert_own
on public.study_bookmarks
for insert
with check ((auth.jwt() ->> 'sub') = user_id);

drop policy if exists study_bookmarks_update_own on public.study_bookmarks;
create policy study_bookmarks_update_own
on public.study_bookmarks
for update
using ((auth.jwt() ->> 'sub') = user_id)
with check ((auth.jwt() ->> 'sub') = user_id);

drop policy if exists study_bookmarks_delete_own on public.study_bookmarks;
create policy study_bookmarks_delete_own
on public.study_bookmarks
for delete
using ((auth.jwt() ->> 'sub') = user_id);

-- user_memories
drop policy if exists user_memories_select_own on public.user_memories;
create policy user_memories_select_own
on public.user_memories
for select
using ((auth.jwt() ->> 'sub') = user_id);

drop policy if exists user_memories_insert_own on public.user_memories;
create policy user_memories_insert_own
on public.user_memories
for insert
with check ((auth.jwt() ->> 'sub') = user_id);

drop policy if exists user_memories_update_own on public.user_memories;
create policy user_memories_update_own
on public.user_memories
for update
using ((auth.jwt() ->> 'sub') = user_id)
with check ((auth.jwt() ->> 'sub') = user_id);

drop policy if exists user_memories_delete_own on public.user_memories;
create policy user_memories_delete_own
on public.user_memories
for delete
using ((auth.jwt() ->> 'sub') = user_id);

COMMIT;

-- After running: verify all twelve policies now read (auth.jwt() ->> 'sub')
-- instead of auth.uid() --
--   SELECT tablename, policyname, cmd, qual, with_check FROM pg_policies
--   WHERE schemaname = 'public'
--     AND tablename IN ('user_preferences', 'study_bookmarks', 'user_memories');
-- every qual/with_check should read `((auth.jwt() -> 'sub'::text) = user_id)`
-- or equivalent, with no reference to auth.uid() anywhere.
