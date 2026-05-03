-- Sh'elah user-scoped tables: bookmarks and preferences
-- Run in Supabase SQL editor to create tables + RLS

create table if not exists public.user_preferences (
    user_id text primary key,
    prefs text,
    shelf text,
    notes text,
    reading_state text,
    updated_at timestamptz not null default now()
);

create table if not exists public.study_bookmarks (
    id text primary key,
    user_id text not null,
    ref text,
    label text,
    segment_text text,
    ai_summary text,
    notes text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- Indexes for user_id lookups
create index if not exists user_preferences_user_id_idx on public.user_preferences (user_id);
create index if not exists study_bookmarks_user_id_idx on public.study_bookmarks (user_id);
create index if not exists study_bookmarks_created_at_idx on public.study_bookmarks (created_at desc);

-- Enable RLS
alter table public.user_preferences enable row level security;
alter table public.study_bookmarks enable row level security;

-- Force RLS (prevents admin bypass without explicit policy)
alter table public.user_preferences force row level security;
alter table public.study_bookmarks force row level security;

-- RLS Policies for user_preferences
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

-- RLS Policies for study_bookmarks
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

-- RLS Policies for user_memories (if not already set)
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
