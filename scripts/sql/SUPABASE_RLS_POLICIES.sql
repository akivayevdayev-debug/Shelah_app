-- Supabase RLS baseline for user-scoped Shelah tables.
-- Apply in Supabase SQL editor after verifying actual column names.
--
-- 2026-08-31 (plan.md §21, Prompt 34): policies below use
-- (auth.jwt() ->> 'sub') = user_id, not auth.uid()::text = user_id.
-- auth.uid() casts the JWT `sub` claim to `uuid` internally -- correct for
-- Supabase's own auth, but Clerk's `sub` (format `user_XXXX...`) is never
-- UUID-shaped, so calling auth.uid() for a Clerk-authenticated request
-- throws `22P02 invalid input syntax for type uuid` INSIDE auth.uid()
-- itself, before any outer cast in the policy runs. Confirmed live via
-- scripts/verify_rls.py; see scripts/sql/migrate_rls_use_jwt_sub_not_auth_uid.sql
-- for the fix applied to an already-provisioned project.
--
-- 2026-09-03 (live Supabase Advisor scan, auth_rls_initplan finding):
-- every auth.jwt() call below is wrapped as (select auth.jwt()) so
-- Postgres evaluates it once per query instead of once per row -- same
-- logic, just planner-friendly. This file is the source of truth this
-- app's policies should match; the actual ALTER POLICY statements an
-- operator runs against the live project live in
-- scripts/sql/migrate_search_path_and_rls_perf_fixes.sql (also covers
-- two search_path fixes and some GRAPHQL-exposure REVOKEs from the same
-- scan). See docs/DATABASE.md's 2026-09-03 subsection for the full
-- writeup.

begin;

alter table if exists public.user_preferences enable row level security;
alter table if exists public.user_memories enable row level security;
alter table if exists public.study_bookmarks enable row level security;

-- Optional hardening: force RLS for table owners as well.
alter table if exists public.user_preferences force row level security;
alter table if exists public.user_memories force row level security;
alter table if exists public.study_bookmarks force row level security;

-- user_preferences
drop policy if exists user_preferences_select_own on public.user_preferences;
create policy user_preferences_select_own
on public.user_preferences
for select
using (((select auth.jwt()) ->> 'sub') = user_id);

drop policy if exists user_preferences_insert_own on public.user_preferences;
create policy user_preferences_insert_own
on public.user_preferences
for insert
with check (((select auth.jwt()) ->> 'sub') = user_id);

drop policy if exists user_preferences_update_own on public.user_preferences;
create policy user_preferences_update_own
on public.user_preferences
for update
using (((select auth.jwt()) ->> 'sub') = user_id)
with check (((select auth.jwt()) ->> 'sub') = user_id);

drop policy if exists user_preferences_delete_own on public.user_preferences;
create policy user_preferences_delete_own
on public.user_preferences
for delete
using (((select auth.jwt()) ->> 'sub') = user_id);

-- user_memories
drop policy if exists user_memories_select_own on public.user_memories;
create policy user_memories_select_own
on public.user_memories
for select
using (((select auth.jwt()) ->> 'sub') = user_id);

drop policy if exists user_memories_insert_own on public.user_memories;
create policy user_memories_insert_own
on public.user_memories
for insert
with check (((select auth.jwt()) ->> 'sub') = user_id);

drop policy if exists user_memories_update_own on public.user_memories;
create policy user_memories_update_own
on public.user_memories
for update
using (((select auth.jwt()) ->> 'sub') = user_id)
with check (((select auth.jwt()) ->> 'sub') = user_id);

drop policy if exists user_memories_delete_own on public.user_memories;
create policy user_memories_delete_own
on public.user_memories
for delete
using (((select auth.jwt()) ->> 'sub') = user_id);

-- study_bookmarks
drop policy if exists study_bookmarks_select_own on public.study_bookmarks;
create policy study_bookmarks_select_own
on public.study_bookmarks
for select
using (((select auth.jwt()) ->> 'sub') = user_id);

drop policy if exists study_bookmarks_insert_own on public.study_bookmarks;
create policy study_bookmarks_insert_own
on public.study_bookmarks
for insert
with check (((select auth.jwt()) ->> 'sub') = user_id);

drop policy if exists study_bookmarks_update_own on public.study_bookmarks;
create policy study_bookmarks_update_own
on public.study_bookmarks
for update
using (((select auth.jwt()) ->> 'sub') = user_id)
with check (((select auth.jwt()) ->> 'sub') = user_id);

drop policy if exists study_bookmarks_delete_own on public.study_bookmarks;
create policy study_bookmarks_delete_own
on public.study_bookmarks
for delete
using (((select auth.jwt()) ->> 'sub') = user_id);

commit;
