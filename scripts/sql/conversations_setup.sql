-- Sh'elah multi-turn AI conversation schema: conversations, messages, citations
-- Run in Supabase SQL editor to create tables + RLS.
--
-- Design source: Claude.ai/Gemini chat+history research verdict (2026-09-23),
-- adapted to this codebase's established Clerk+Supabase RLS idiom -- see
-- scripts/sql/bookmarks_and_preferences_setup.sql for the pattern this
-- follows: (auth.jwt() ->> 'sub') = user_id, NOT auth.uid() (Clerk's `sub`
-- claim, e.g. "user_abc123", is never UUID-shaped, so auth.uid() throws
-- 22P02 before any outer cast runs -- see that file's header comment and
-- scripts/migrate_rls_use_jwt_sub_not_auth_uid.sql).
--
-- This is a NEW, separate schema from public.ask_history (see
-- migrate_ask_history.sql). ask_history remains exactly as-is: it is the
-- backing store for the single-shot Q&A history feature already shipped
-- (GET/DELETE /api/user/history, the ?chat=<id> deep link). Existing rows
-- and links keep working unchanged. All *new* AI interactions instead create
-- rows here; ask_history simply stops growing once the app cuts over.
-- Unifying the two into one sidebar list, if wanted, is a later UI decision
-- (spec step 3) -- it does not require migrating old rows into this schema.
--
-- messages/citations have no user_id column of their own -- their RLS
-- policies join back to conversations.user_id, same join-through-parent
-- idiom needed anywhere a child table's owner is defined by its parent.

create table if not exists public.conversations (
    id                    uuid        primary key default gen_random_uuid(),
    user_id               text        not null,
    title                 text        not null default '',
    title_is_custom       boolean     not null default false,
    minhag                text,
    pinned_at             timestamptz,
    created_at            timestamptz not null default now(),
    updated_at            timestamptz not null default now(),
    deleted_at            timestamptz,
    share_id              text        unique,
    share_snapshot_msg_id uuid,
    shared_at             timestamptz
);

create table if not exists public.messages (
    id              uuid        primary key default gen_random_uuid(),
    conversation_id uuid        not null references public.conversations(id) on delete cascade,
    role            text        not null check (role in ('user', 'assistant')),
    content         text        not null default '',
    status          text        not null default 'complete'
                        check (status in ('streaming', 'complete', 'stopped', 'error')),
    superseded_by   uuid        references public.messages(id) on delete set null,
    created_at      timestamptz not null default now()
);

create table if not exists public.citations (
    id          uuid    primary key default gen_random_uuid(),
    message_id  uuid    not null references public.messages(id) on delete cascade,
    ordinal     int     not null,
    source_ref  text,
    excerpt_he  text,
    excerpt_en  text,
    url         text
);

-- Indexes for the sidebar list (per-user, most-recently-updated, excluding
-- soft-deleted rows) and for hydrating one thread's turns/citations in order.
create index if not exists conversations_user_updated_idx
    on public.conversations (user_id, updated_at desc)
    where deleted_at is null;
create index if not exists conversations_share_id_idx
    on public.conversations (share_id)
    where share_id is not null;
create index if not exists messages_conversation_created_idx
    on public.messages (conversation_id, created_at);
create index if not exists citations_message_ordinal_idx
    on public.citations (message_id, ordinal);

-- Enable + force RLS (force prevents admin-role bypass without an explicit policy).
alter table public.conversations enable row level security;
alter table public.messages      enable row level security;
alter table public.citations     enable row level security;
alter table public.conversations force row level security;
alter table public.messages      force row level security;
alter table public.citations     force row level security;

-- conversations: owner-only, same four-policy shape as study_bookmarks.
drop policy if exists conversations_select_own on public.conversations;
create policy conversations_select_own
on public.conversations
for select
using ((auth.jwt() ->> 'sub') = user_id);

drop policy if exists conversations_insert_own on public.conversations;
create policy conversations_insert_own
on public.conversations
for insert
with check ((auth.jwt() ->> 'sub') = user_id);

drop policy if exists conversations_update_own on public.conversations;
create policy conversations_update_own
on public.conversations
for update
using ((auth.jwt() ->> 'sub') = user_id)
with check ((auth.jwt() ->> 'sub') = user_id);

drop policy if exists conversations_delete_own on public.conversations;
create policy conversations_delete_own
on public.conversations
for delete
using ((auth.jwt() ->> 'sub') = user_id);

-- messages: owner determined by the parent conversation's user_id.
drop policy if exists messages_select_own on public.messages;
create policy messages_select_own
on public.messages
for select
using (exists (
    select 1 from public.conversations c
    where c.id = messages.conversation_id
    and (auth.jwt() ->> 'sub') = c.user_id
));

drop policy if exists messages_insert_own on public.messages;
create policy messages_insert_own
on public.messages
for insert
with check (exists (
    select 1 from public.conversations c
    where c.id = messages.conversation_id
    and (auth.jwt() ->> 'sub') = c.user_id
));

drop policy if exists messages_update_own on public.messages;
create policy messages_update_own
on public.messages
for update
using (exists (
    select 1 from public.conversations c
    where c.id = messages.conversation_id
    and (auth.jwt() ->> 'sub') = c.user_id
))
with check (exists (
    select 1 from public.conversations c
    where c.id = messages.conversation_id
    and (auth.jwt() ->> 'sub') = c.user_id
));

drop policy if exists messages_delete_own on public.messages;
create policy messages_delete_own
on public.messages
for delete
using (exists (
    select 1 from public.conversations c
    where c.id = messages.conversation_id
    and (auth.jwt() ->> 'sub') = c.user_id
));

-- citations: owner determined by the grandparent conversation's user_id.
drop policy if exists citations_select_own on public.citations;
create policy citations_select_own
on public.citations
for select
using (exists (
    select 1 from public.messages m
    join public.conversations c on c.id = m.conversation_id
    where m.id = citations.message_id
    and (auth.jwt() ->> 'sub') = c.user_id
));

drop policy if exists citations_insert_own on public.citations;
create policy citations_insert_own
on public.citations
for insert
with check (exists (
    select 1 from public.messages m
    join public.conversations c on c.id = m.conversation_id
    where m.id = citations.message_id
    and (auth.jwt() ->> 'sub') = c.user_id
));

drop policy if exists citations_delete_own on public.citations;
create policy citations_delete_own
on public.citations
for delete
using (exists (
    select 1 from public.messages m
    join public.conversations c on c.id = m.conversation_id
    where m.id = citations.message_id
    and (auth.jwt() ->> 'sub') = c.user_id
));

-- No anon/authenticated grants are added here beyond what Supabase's default
-- `authenticated` role already has -- RLS above is the actual gate, matching
-- the other user-scoped tables in this schema (see SUPABASE_RLS_POLICIES.sql).
--
-- Public share pages (/s/{shareId}) are NOT served through client-side RLS:
-- the spec is explicit that shares must never expose the raw conversation
-- id, so the /s/ route reads through the backend's existing service-role
-- client (_get_supabase_client()), filtered by share_id, same access
-- pattern already used for ask_history. Anonymous visitors never get a
-- Supabase client of their own, so no anon policy is defined here.
