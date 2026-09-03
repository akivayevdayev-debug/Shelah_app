-- Sh'elah -- public.rls_auto_enable(): auto-enable RLS on new tables.
--
-- Provenance (plan.md §30.4): this function existed live on the real
-- Supabase project with NO tracked source file anywhere in this repo
-- until this pass -- surfaced by a Supabase Advisor scan flagging it as a
-- SECURITY DEFINER function executable by anon/authenticated via
-- /rest/v1/rpc/rls_auto_enable. Its actual body (captured verbatim below
-- via `SELECT pg_get_functiondef(...)`, run by the operator 2026-08-21)
-- shows it is an EVENT TRIGGER function: it fires on CREATE TABLE /
-- CREATE TABLE AS / SELECT INTO in the `public` schema and runs
-- `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` on the new table
-- automatically -- a DDL governance guardrail against a table shipping
-- with RLS off by accident, not something meant to be called directly.
--
-- The advisor's "executable by anon/authenticated" warning is very
-- unlikely to be a live risk in practice: a function that RETURNS
-- event_trigger cannot be invoked through a normal call (including a
-- PostgREST RPC call) -- only the event-trigger dispatcher can run it.
-- The REVOKE below closes the warning anyway, as correct hygiene: there
-- is no legitimate reason anon/authenticated need EXECUTE on this.
--
-- Open question this file does NOT resolve: whether a matching
-- `CREATE EVENT TRIGGER ... EXECUTE FUNCTION rls_auto_enable()` actually
-- exists to wire this function up to real DDL events, or whether it's
-- dead code with no trigger pointing at it. Run this to check and add the
-- result to this file's own history next time it's touched:
--   SELECT evtname, evtevent, evtenabled, evttags
--   FROM pg_event_trigger WHERE evtfoid = 'public.rls_auto_enable()'::regprocedure;

CREATE OR REPLACE FUNCTION public.rls_auto_enable()
 RETURNS event_trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
DECLARE
  cmd record;
BEGIN
  FOR cmd IN
    SELECT *
    FROM pg_event_trigger_ddl_commands()
    WHERE command_tag IN ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO')
      AND object_type IN ('table','partitioned table')
  LOOP
     IF cmd.schema_name IS NOT NULL AND cmd.schema_name IN ('public') AND cmd.schema_name NOT IN ('pg_catalog','information_schema') AND cmd.schema_name NOT LIKE 'pg_toast%' AND cmd.schema_name NOT LIKE 'pg_temp%' THEN
      BEGIN
        EXECUTE format('alter table if exists %s enable row level security', cmd.object_identity);
        RAISE LOG 'rls_auto_enable: enabled RLS on %', cmd.object_identity;
      EXCEPTION
        WHEN OTHERS THEN
          RAISE LOG 'rls_auto_enable: failed to enable RLS on %', cmd.object_identity;
      END;
     ELSE
        RAISE LOG 'rls_auto_enable: skip % (either system schema or not in enforced list: %.)', cmd.object_identity, cmd.schema_name;
     END IF;
  END LOOP;
END;
$function$;

REVOKE ALL ON FUNCTION public.rls_auto_enable() FROM PUBLIC, anon, authenticated;
