-- Migration: close the live Supabase Advisor findings from the scan run
-- 2026-09-03. Run this once, by hand, in the Supabase SQL Editor. Every
-- statement here is idempotent / safe to re-run.
--
-- Findings addressed in this file:
--   1. function_search_path_mutable (WARN, SECURITY)
--   2. auth_rls_initplan            (WARN, PERFORMANCE)
--   3. pg_graphql_anon_table_exposed / pg_graphql_authenticated_table_exposed
--      (WARN, SECURITY)
--
-- NOT in this file (see docs/DATABASE.md's 2026-09-03 subsection and
-- docs/SECURITY.md §10 for the full writeup of every finding, including
-- these):
--   - rls_policy_always_true on answer_feedback.anyone_can_submit_feedback
--     -- intentional design (anonymous feedback), not a DB-level fix; only
--     an app-layer rate-limit *recommendation* is documented.
--   - rls_enabled_no_policy on ai_usage_log -- intentional, same posture
--     as ask_history; no fix needed.
--   - unused_index (9 indexes) -- reviewed, kept; these tables are new and
--     the indexes back real query patterns this app runs.
--   - FORCE ROW SECURITY asymmetry (ai_usage_log/answer_feedback/
--     ask_history/community_knowledge lack it; study_bookmarks/
--     user_memories/user_preferences have it) -- only affects the table
--     OWNER role, and service_role bypasses RLS via BYPASSRLS regardless
--     of FORCE; documented as a low-priority future-cleanup observation,
--     no SQL change here.
--   - community_knowledge -- also flagged by the GraphQL-exposure finding
--     but is intentionally public-read (community_knowledge_read,
--     USING (true), for anon+authenticated). Deliberately untouched.

begin;

-- ============================================================================
-- 1. function_search_path_mutable (WARN, SECURITY)
-- ============================================================================
-- Both functions are confirmed NOT SECURITY DEFINER (plain SQL/plpgsql,
-- caller-rights) -- so this is the standard "an attacker-controlled
-- search_path could make an unqualified identifier resolve to a
-- same-named object in another schema" risk, not the higher-risk
-- "...under elevated privilege" variant a SECURITY DEFINER function would
-- carry. Neither function currently has any unqualified non-builtin
-- reference (get_schema_snapshot fully qualifies information_schema/
-- pg_catalog/pg_policies access; set_updated_at_timestamp only touches
-- NEW/OLD row fields and the builtin now()) -- fixing search_path here is
-- defense-in-depth against future edits reintroducing an unqualified
-- reference, not a fix for a reachable exploit today.
--
-- Note for the operator: public.set_updated_at_timestamp() was already
-- pinned to SET search_path = '' once before, in
-- scripts/sql/migrate_security_hardening.sql (2026-08-21, plan.md §30).
-- It is flagged mutable again in this scan because
-- scripts/sql/rag_identity_cache_setup.sql defines it with a bare
-- `create or replace function ...` (no SET search_path clause) --
-- CREATE OR REPLACE does not preserve a function's prior ALTER FUNCTION
-- ... SET config, so re-running that setup file (or any future edit to
-- it) silently regresses this exact finding. The ALTER FUNCTION below
-- re-applies the fix; if rag_identity_cache_setup.sql is ever re-run
-- after this, this statement will need to be re-run too, or (better)
-- that file's CREATE OR REPLACE should be updated to carry the SET
-- clause inline so this stops regressing. Not changed here --
-- rag_identity_cache_setup.sql is out of scope for this migration.
ALTER FUNCTION public.get_schema_snapshot()
    SET search_path = public, pg_temp;

ALTER FUNCTION public.set_updated_at_timestamp()
    SET search_path = public, pg_temp;

-- ============================================================================
-- 2. auth_rls_initplan (WARN, PERFORMANCE)
-- ============================================================================
-- All 12 live "own row" policies across user_preferences, study_bookmarks,
-- and user_memories call auth.jwt() unwrapped in USING/WITH CHECK, which
-- Postgres re-evaluates once per row instead of once per query. Wrapping
-- the call as (select auth.jwt()) lets the planner treat it as a stable
-- sub-plan evaluated once (Supabase's documented fix for this exact
-- linter finding). Logic is unchanged -- only the auth.jwt() call is
-- wrapped; the `->> 'sub' = user_id` comparison is untouched.
--
-- A 13th policy the Advisor's finding-list named, ask_history's
-- "user_own_history", is NOT included below: it does not exist in the
-- live database. scripts/sql/migrate_ask_history.sql only ever DROPs it
-- (deliberately, 2026-08-31 -- ask_history is read/deleted exclusively
-- through the service-role client, so the policy was dead code and was
-- retired rather than migrated; see docs/DATABASE.md's ask_history entry
-- and docs/SECURITY.md §2). An ALTER POLICY against a nonexistent policy
-- errors, so nothing is emitted for it here -- this finding is already
-- resolved for ask_history by the earlier drop, not by this migration.
ALTER POLICY user_preferences_select_own ON public.user_preferences
    USING (((select auth.jwt()) ->> 'sub') = user_id);

ALTER POLICY user_preferences_insert_own ON public.user_preferences
    WITH CHECK (((select auth.jwt()) ->> 'sub') = user_id);

ALTER POLICY user_preferences_update_own ON public.user_preferences
    USING (((select auth.jwt()) ->> 'sub') = user_id)
    WITH CHECK (((select auth.jwt()) ->> 'sub') = user_id);

ALTER POLICY user_preferences_delete_own ON public.user_preferences
    USING (((select auth.jwt()) ->> 'sub') = user_id);

ALTER POLICY study_bookmarks_select_own ON public.study_bookmarks
    USING (((select auth.jwt()) ->> 'sub') = user_id);

ALTER POLICY study_bookmarks_insert_own ON public.study_bookmarks
    WITH CHECK (((select auth.jwt()) ->> 'sub') = user_id);

ALTER POLICY study_bookmarks_update_own ON public.study_bookmarks
    USING (((select auth.jwt()) ->> 'sub') = user_id)
    WITH CHECK (((select auth.jwt()) ->> 'sub') = user_id);

ALTER POLICY study_bookmarks_delete_own ON public.study_bookmarks
    USING (((select auth.jwt()) ->> 'sub') = user_id);

ALTER POLICY user_memories_select_own ON public.user_memories
    USING (((select auth.jwt()) ->> 'sub') = user_id);

ALTER POLICY user_memories_insert_own ON public.user_memories
    WITH CHECK (((select auth.jwt()) ->> 'sub') = user_id);

ALTER POLICY user_memories_update_own ON public.user_memories
    USING (((select auth.jwt()) ->> 'sub') = user_id)
    WITH CHECK (((select auth.jwt()) ->> 'sub') = user_id);

ALTER POLICY user_memories_delete_own ON public.user_memories
    USING (((select auth.jwt()) ->> 'sub') = user_id);

-- ============================================================================
-- 3. pg_graphql_anon_table_exposed / pg_graphql_authenticated_table_exposed
--    (WARN, SECURITY)
-- ============================================================================
-- community_knowledge is also flagged by this finding but is deliberately
-- public-read (community_knowledge_read, USING (true)) -- left untouched,
-- per instructions.

-- 3a. ai_usage_log, ask_history: zero-policy, service-role-only tables by
-- design (docs/DATABASE.md already documents both this way). RLS is
-- enabled with no policies on either, so no anon/authenticated session
-- was ever granted a row regardless of this REVOKE -- confirmed by
-- direct code read, not just the standing research doc:
--   - ai_usage_log: every .table("ai_usage_log") call is via the
--     service-role client (backend/cost_meter.py:106,149,273,479,643).
--   - ask_history: every read/delete (backend/routes_user.py
--     get_ask_history:359, delete_ask_history_entry:389) and the one
--     insert (backend/rag.py::_store_ask_history:383/402) all go through
--     app.py::_get_supabase_client() (service-role), never the
--     user-scoped/RLS client. Revoking SELECT closes the GraphQL/
--     PostgREST schema-exposure gap with zero functional risk.
REVOKE SELECT ON public.ai_usage_log FROM anon, authenticated;
REVOKE SELECT ON public.ask_history FROM anon, authenticated;

-- 3b. study_bookmarks, user_memories, user_preferences: these have real
-- per-user "own row" RLS policies (fixed in section 2 above). Revoking
-- SELECT from `anon` is unconditionally safe -- an anon session's
-- auth.jwt() is always null, so `(select auth.jwt()) ->> 'sub' = user_id`
-- can never match, meaning anon was already reading zero rows through
-- RLS; this REVOKE only closes the GraphQL schema-visibility gap, not a
-- row-access gap.
REVOKE SELECT ON public.study_bookmarks FROM anon;
REVOKE SELECT ON public.user_memories FROM anon;
REVOKE SELECT ON public.user_preferences FROM anon;

-- Deliberately NOT revoking SELECT from `authenticated` on these three
-- tables, diverging from the default "frontend_direct_access: false =>
-- revoke authenticated too" rule the prior research phase's frontend
-- audit proposed. That audit only checked for a *browser-side* Supabase
-- session (correctly found none) -- it did not check which Postgres role
-- the *backend's* own RLS-scoped client authenticates as. Direct code
-- read for this migration confirms the backend itself is that
-- `authenticated`-role client for exactly these three tables, as its
-- PRIMARY (not fallback) access path:
--   - backend/routes_user.py::user_preferences (line ~234) and
--     ::semantic_bookmarks (line ~306) both call
--     app.py::_get_user_scoped_supabase_client() first -- which builds a
--     Supabase client using SUPABASE_PUBLISHABLE_KEY and forwards the
--     caller's verified Clerk bearer token, i.e. PostgREST/pg_graphql
--     authenticates this session as the Postgres `authenticated` role,
--     not service_role.
--   - backend/rag.py::_fetch_user_memory_summaries (line ~316) and
--     ::_store_user_memory_summary (line ~424) do the same for
--     user_memories.
--   - app.py's STRICT_SUPABASE_RLS is a hardcoded `True` literal (see
--     docs/SECURITY.md §2), so each call site's
--     `if not supabase and not STRICT_SUPABASE_RLS: supabase =
--     _get_supabase_client()` fallback to the service-role client can
--     never fire -- the user-scoped/`authenticated`-role client is the
--     ONLY live path for these three tables' reads and writes today.
-- Postgres requires a table-level GRANT before RLS is even consulted:
-- revoking SELECT from `authenticated` here would not narrow row access
-- (the policies already do that correctly) -- it would make every one of
-- these calls fail outright with a permission-denied error, breaking
-- GET/PUT /api/user/preferences, the semantic-bookmarks GET/POST route,
-- and both the RAG memory read and write paths in production. That is a
-- real regression, not a hardening step, so it is intentionally skipped.
-- The residual GraphQL-schema-visibility exposure to `authenticated` for
-- these three tables is accepted as-is: the RLS policies (section 2
-- above) already correctly gate every row to its owner for that role.
-- Fully closing this would require moving these three tables' backend
-- access off the publishable-key user-scoped client and onto the
-- service-role client with an app-level ownership check instead (the
-- same pattern ask_history already uses) -- a larger, separately-scoped
-- change, not done in this pass.

commit;
