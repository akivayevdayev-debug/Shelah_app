-- plan.md §23.2.3 (Prompt 36): read-only schema-introspection RPC backing
-- scripts/generate_database_doc.py.
--
-- Supabase's PostgREST layer only exposes the `public` schema as REST
-- resources by default -- `information_schema`/`pg_catalog` are not
-- reachable via .table(), which is why every prior script in this repo
-- (scripts/recompute_ai_usage_log_cost.py, scripts/migrate_customs_to_
-- supabase.py) only ever touches application tables. This function
-- follows the same pattern already established by
-- scripts/sql/check_and_reserve_user_budget.sql -- a server-side SQL
-- function invoked via `.rpc()` -- so a live-schema doc generator does
-- not need a new credential type (a direct Postgres connection string)
-- that no other tooling in this repo uses.
--
-- Run this once in the Supabase SQL Editor for your project before running
-- `python scripts/generate_database_doc.py`.

CREATE OR REPLACE FUNCTION public.get_schema_snapshot()
RETURNS jsonb
LANGUAGE sql
STABLE
AS $$
    SELECT jsonb_build_object(
        'tables', COALESCE((
            SELECT jsonb_agg(t ORDER BY t->>'table_name')
            FROM (
                SELECT jsonb_build_object(
                    'table_name', c.relname,
                    'rls_enabled', c.relrowsecurity,
                    'rls_forced', c.relforcerowsecurity,
                    'columns', COALESCE((
                        SELECT jsonb_agg(
                            jsonb_build_object(
                                'name', col.column_name,
                                'type', col.data_type,
                                'nullable', col.is_nullable = 'YES',
                                'default', col.column_default
                            )
                            ORDER BY col.ordinal_position
                        )
                        FROM information_schema.columns col
                        WHERE col.table_schema = 'public'
                          AND col.table_name = c.relname
                    ), '[]'::jsonb),
                    'indexes', COALESCE((
                        SELECT jsonb_agg(
                            jsonb_build_object('name', i.indexname, 'def', i.indexdef)
                            ORDER BY i.indexname
                        )
                        FROM pg_indexes i
                        WHERE i.schemaname = 'public'
                          AND i.tablename = c.relname
                    ), '[]'::jsonb),
                    'policies', COALESCE((
                        SELECT jsonb_agg(
                            jsonb_build_object(
                                'name', p.policyname,
                                'cmd', p.cmd,
                                'roles', p.roles,
                                'using', p.qual,
                                'with_check', p.with_check
                            )
                            ORDER BY p.policyname
                        )
                        FROM pg_policies p
                        WHERE p.schemaname = 'public'
                          AND p.tablename = c.relname
                    ), '[]'::jsonb)
                ) AS t
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relkind = 'r'  -- ordinary tables only, not views/sequences
            ) sub
        ), '[]'::jsonb)
    );
$$;

-- Service-role-only, matching check_and_reserve_user_budget.sql's posture:
-- this is schema metadata, not user data, but it is still an operational
-- introspection surface with no reason to be reachable by anon/authenticated
-- PostgREST callers.
REVOKE ALL ON FUNCTION public.get_schema_snapshot() FROM PUBLIC;
