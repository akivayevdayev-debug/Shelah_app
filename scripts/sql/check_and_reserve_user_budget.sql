-- plan.md §20.2 Phase 20b (Prompt 33b): atomic check-and-reserve for the
-- per-caller daily AI-spend ceiling.
--
-- Replaces the read-then-decide race in backend/cost_meter.py::
-- check_user_budget_and_enforce (plan.md §20.1-C2: the old code reads
-- today's total, compares, and never writes -- N concurrent /ask requests
-- from one caller near the cap all read the same pre-spend total and all
-- pass) with a single Postgres statement, so concurrent callers for the
-- SAME key see each other's reservations instead of racing on a stale read.
--
-- Run this once in the Supabase SQL Editor for your project, after
-- scripts/migrate_ai_usage_log_add_user_columns.sql (this migration adds
-- three more columns to the same table).

-- Reservation bookkeeping columns, additive.
ALTER TABLE public.ai_usage_log
    ADD COLUMN IF NOT EXISTS reserved BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS reservation_id UUID,
    ADD COLUMN IF NOT EXISTS reservation_expires_at TIMESTAMPTZ;

-- Supports the reserve-then-settle UPDATE (by reservation_id) and the TTL
-- sweep (WHERE reserved AND reservation_expires_at < now()).
CREATE UNIQUE INDEX IF NOT EXISTS ai_usage_log_reservation_id_idx
    ON public.ai_usage_log (reservation_id)
    WHERE reservation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ai_usage_log_reserved_expiry_idx
    ON public.ai_usage_log (reserved, reservation_expires_at)
    WHERE reserved;

-- Reservation lifecycle (plan.md §20.2 Phase 20b STEP 2):
--
--   1. RESERVE -- check_and_reserve_user_budget() below inserts a
--      placeholder row (cost_usd = the caller's worst-case single-call
--      estimate, reserved = true, reservation_expires_at = now() + 10
--      minutes) inside the SAME statement that computed today's running
--      total, so a concurrent caller's read cannot observe the
--      pre-reservation total.
--
--   2. SETTLE -- backend/cost_meter.py::record_llm_call(), once the real
--      call completes, UPDATEs that row in place with the real
--      provider/model/token counts/cost and clears `reserved` -- it does
--      NOT insert a second row (a second row would double-count the spend
--      Prompt 33a's price-table fix made real).
--
--   3. EXPIRE -- if the caller's process dies between reserve and settle
--      (crash, cold-start eviction, request timeout), the row is left with
--      reserved = true past its reservation_expires_at. backend/cost_meter.
--      py::expire_stale_budget_reservations() -- folded into the existing
--      retention-enforce cron (backend/routes_privacy.py::retention_enforce)
--      rather than a new scheduled job -- deletes these (not settles them:
--      no real spend happened, so the reserved amount must be released,
--      not recorded). A 10-minute TTL is far longer than
--      backend/claude.py's AI_TOTAL_BUDGET_SECONDS request timeout, so a
--      stale reservation is unambiguously abandoned, never a slow
--      in-flight request.
--
-- Concurrency: pg_advisory_xact_lock() below serializes concurrent callers
-- for the SAME budget key only (hashed from key_column+key_value) -- calls
-- for different keys never block each other. The lock is transaction-
-- scoped: Postgres releases it automatically on commit/rollback, so it is
-- never held across the network round trip back to the caller.
CREATE OR REPLACE FUNCTION public.check_and_reserve_user_budget(
    p_key_column TEXT,
    p_key_value TEXT,
    p_threshold_usd NUMERIC,
    p_reservation_usd NUMERIC
)
RETURNS TABLE (allowed BOOLEAN, total_usd NUMERIC, reservation_id UUID)
LANGUAGE plpgsql
AS $$
DECLARE
    v_total NUMERIC;
    v_reservation_id UUID;
    v_start_of_day TIMESTAMPTZ := date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc';
BEGIN
    IF p_key_column NOT IN ('user_id', 'client_key') THEN
        RAISE EXCEPTION 'check_and_reserve_user_budget: invalid key_column %', p_key_column;
    END IF;

    -- Serializes concurrent callers for this exact key so the SELECT+INSERT
    -- below cannot race (plan.md §20.1-C2).
    PERFORM pg_advisory_xact_lock(hashtextextended(p_key_column || ':' || p_key_value, 0));

    IF p_key_column = 'user_id' THEN
        SELECT COALESCE(SUM(cost_usd), 0) INTO v_total
        FROM public.ai_usage_log
        WHERE user_id = p_key_value AND created_at >= v_start_of_day;
    ELSE
        SELECT COALESCE(SUM(cost_usd), 0) INTO v_total
        FROM public.ai_usage_log
        WHERE client_key = p_key_value AND created_at >= v_start_of_day;
    END IF;

    IF v_total + p_reservation_usd > p_threshold_usd THEN
        RETURN QUERY SELECT false, v_total, NULL::UUID;
        RETURN;
    END IF;

    v_reservation_id := gen_random_uuid();
    INSERT INTO public.ai_usage_log (
        provider, model, input_tokens, output_tokens, cost_usd,
        route, request_id, user_id, client_key, created_at,
        reserved, reservation_id, reservation_expires_at
    ) VALUES (
        'reservation', 'reservation', 0, 0, p_reservation_usd,
        'budget-reservation', '',
        CASE WHEN p_key_column = 'user_id' THEN p_key_value ELSE NULL END,
        CASE WHEN p_key_column = 'client_key' THEN p_key_value ELSE NULL END,
        now(),
        true, v_reservation_id, now() + INTERVAL '10 minutes'
    );

    RETURN QUERY SELECT true, v_total + p_reservation_usd, v_reservation_id;
END;
$$;

-- service-role-only: invoked exclusively via the server-side Supabase
-- client (SUPABASE_SECRET_KEY), never from the browser. No GRANT to
-- anon/authenticated -- matches ai_usage_log's own
-- no-RLS-because-service-role-only posture (plan.md §21.2.3).
REVOKE ALL ON FUNCTION public.check_and_reserve_user_budget(TEXT, TEXT, NUMERIC, NUMERIC) FROM PUBLIC;
