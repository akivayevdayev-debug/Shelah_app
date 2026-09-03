-- Migration: close the unambiguous findings from a live Supabase Advisor
-- scan (plan.md §30, found 2026-08-21).
--
-- Run this once in the Supabase SQL Editor for your project. Every
-- statement here is idempotent / safe to re-run.
--
-- public.rls_auto_enable() is handled in its own file,
-- scripts/sql/rls_auto_enable_setup.sql, now that its definition is known
-- (plan.md §30.4) -- run that file too.
--
-- Deliberately NOT included here (see plan.md §30 for why each is
-- excluded from this migration):
--   - Grants on user_preferences / study_bookmarks / user_memories /
--     ask_history -- these depend on the still-open question of whether
--     Supabase Third-Party Auth for Clerk is actually enabled (plan.md
--     §21, Prompt 34's "1b"). Revoking client-role access now could
--     silently break the intended auth.uid()-scoped RLS path if that
--     turns out to already be configured and working.
--   - Grants on community_knowledge / ai_usage_log -- already correct by
--     design: community_knowledge is a deliberately public-read reference
--     corpus (scripts/sql/rag_identity_cache_setup.sql); ai_usage_log has
--     RLS enabled with zero policies, which already defaults to
--     deny-for-everyone-but-service-role (plan.md §21.2.3).

-- 1. Function search_path hardening (Supabase linter: function_search_path_mutable).
-- Both functions already fully schema-qualify every non-builtin reference
-- they make (public.ai_usage_log; NEW/OLD row fields), and every bare
-- identifier they call (pg_advisory_xact_lock, hashtextextended,
-- gen_random_uuid, date_trunc, now()) is a pg_catalog builtin, which
-- Postgres always resolves regardless of search_path. An empty
-- search_path is therefore safe and is the strictest available fix --
-- it removes any chance of a same-named object earlier in a caller's
-- search_path being resolved instead.
ALTER FUNCTION public.set_updated_at_timestamp()
    SET search_path = '';

ALTER FUNCTION public.check_and_reserve_user_budget(TEXT, TEXT, NUMERIC, NUMERIC)
    SET search_path = '';

-- 2. Move pg_trgm out of the public schema (Supabase linter: extension_in_public).
-- Safe: GIN index opclass bindings (community_knowledge_topic_trgm_idx,
-- community_knowledge_content_trgm_idx in scripts/sql/rag_identity_cache_setup.sql)
-- are bound by OID at index-build time, not by schema search path, so
-- existing indexes keep working unchanged. The app's own use of these
-- indexes is via plain ILIKE through PostgREST -- ILIKE itself is a
-- pg_catalog operator, not part of pg_trgm -- so no caller needs schema
-- USAGE on `extensions` for existing functionality; granted anyway,
-- matching Supabase's own documented remediation, in case anything ever
-- calls similarity()/% directly.
CREATE SCHEMA IF NOT EXISTS extensions;
GRANT USAGE ON SCHEMA extensions TO postgres, anon, authenticated, service_role;
ALTER EXTENSION pg_trgm SET SCHEMA extensions;

-- 3. Close answer_feedback's anon/authenticated SELECT grant (Supabase
-- linter: pg_graphql_anon_table_exposed / pg_graphql_authenticated_table_exposed).
-- scripts/migrate_answer_feedback.sql already documents the intent --
-- "only the backend's secret-key client... can read" -- but never
-- revoked the table-level SELECT grant those roles get by default in a
-- new Supabase project, which is what made the table (and its column
-- names) discoverable via the GraphQL/PostgREST schema even though RLS
-- itself already blocks every row read for both roles (no SELECT policy
-- exists). This closes the grant to match the documented intent exactly.
REVOKE SELECT ON public.answer_feedback FROM anon, authenticated;

-- 4. rls_policy_always_true on answer_feedback's INSERT policy is
-- intentional, not a finding: `anyone_can_submit_feedback` is meant to
-- accept feedback from anonymous and signed-in visitors alike (same
-- pattern as accept_legal() in backend/routes_user.py). No change.
