-- Sh'elah RAG + identity-aware memory tables
-- Run in Supabase SQL editor before executing scripts/migrate_customs_to_supabase.py

create extension if not exists pg_trgm;

create table if not exists public.community_knowledge (
    id text primary key,
    community_name text not null,
    topic text not null,
    halakhic_source text not null,
    content text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.user_memories (
    id uuid primary key default gen_random_uuid(),
    user_id text not null,
    summary text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create or replace function public.set_updated_at_timestamp()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists community_knowledge_set_updated_at on public.community_knowledge;
create trigger community_knowledge_set_updated_at
before update on public.community_knowledge
for each row execute function public.set_updated_at_timestamp();

drop trigger if exists user_memories_set_updated_at on public.user_memories;
create trigger user_memories_set_updated_at
before update on public.user_memories
for each row execute function public.set_updated_at_timestamp();

create index if not exists community_knowledge_community_topic_idx
on public.community_knowledge (community_name, topic);

create index if not exists community_knowledge_topic_trgm_idx
on public.community_knowledge using gin (topic gin_trgm_ops);

create index if not exists community_knowledge_content_trgm_idx
on public.community_knowledge using gin (content gin_trgm_ops);

create index if not exists user_memories_user_created_idx
on public.user_memories (user_id, created_at desc);

alter table public.community_knowledge enable row level security;
alter table public.user_memories enable row level security;

-- Public read for reference knowledge; writes should stay server-side.
drop policy if exists community_knowledge_read on public.community_knowledge;
create policy community_knowledge_read
on public.community_knowledge
for select
to anon, authenticated
using (true);

-- Block client-side direct access to user_memories; server uses service role.
drop policy if exists user_memories_block_client_select on public.user_memories;
create policy user_memories_block_client_select
on public.user_memories
for select
to anon, authenticated
using (false);

drop policy if exists user_memories_block_client_write on public.user_memories;
create policy user_memories_block_client_write
on public.user_memories
for all
to anon, authenticated
using (false)
with check (false);
