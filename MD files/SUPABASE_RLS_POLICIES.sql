-- Supabase RLS baseline for user-scoped Shelah tables.
-- Apply in Supabase SQL editor after verifying actual column names.

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

-- user_memories
drop policy if exists user_memories_select_own on public.user_memories;
create policy user_memories_select_own
on public.user_memories
for select
using (auth.uid()::text = user_id);

drop policy if exists user_memories_insert_own on public.user_memories;
create policy user_memories_insert_own
on public.user_memories
for insert
with check (auth.uid()::text = user_id);

drop policy if exists user_memories_update_own on public.user_memories;
create policy user_memories_update_own
on public.user_memories
for update
using (auth.uid()::text = user_id)
with check (auth.uid()::text = user_id);

drop policy if exists user_memories_delete_own on public.user_memories;
create policy user_memories_delete_own
on public.user_memories
for delete
using (auth.uid()::text = user_id);

-- study_bookmarks
drop policy if exists study_bookmarks_select_own on public.study_bookmarks;
create policy study_bookmarks_select_own
on public.study_bookmarks
for select
using (auth.uid()::text = user_id);

drop policy if exists study_bookmarks_insert_own on public.study_bookmarks;
create policy study_bookmarks_insert_own
on public.study_bookmarks
for insert
with check (auth.uid()::text = user_id);

drop policy if exists study_bookmarks_update_own on public.study_bookmarks;
create policy study_bookmarks_update_own
on public.study_bookmarks
for update
using (auth.uid()::text = user_id)
with check (auth.uid()::text = user_id);

drop policy if exists study_bookmarks_delete_own on public.study_bookmarks;
create policy study_bookmarks_delete_own
on public.study_bookmarks
for delete
using (auth.uid()::text = user_id);

commit;
