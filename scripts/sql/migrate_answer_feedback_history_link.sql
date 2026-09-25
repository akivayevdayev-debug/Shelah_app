-- Migration: link answer feedback to the stored answer it rates
-- (deep-link Phase 6). Run this once in the Supabase SQL Editor, after
-- scripts/sql/migrate_answer_feedback.sql (the table) and
-- scripts/migrate_ask_history.sql (the table it points at).
--
-- backend/routes_feedback.py fills these for feedback on a stored answer:
--   history_id  -- the rated ask_history row. The owner's client sends its
--                  id (accepted only if it is the caller's own row); a
--                  visitor on a shared link sends the share token, which
--                  the server resolves to the row id. The token itself is
--                  never stored -- the owner can revoke it.
--   shared_view -- true when a visitor rated it through a shared link.
--
-- ON DELETE SET NULL: deleting an answer from history (or the whole account)
-- keeps the anonymous verdict but drops the link to the deleted answer.
--
-- Until this runs, feedback is still saved, just without the link (the
-- route retries the insert without these two columns on 42703/PGRST204).
--
-- Idempotent; no policy or grant changes -- answer_feedback keeps its
-- existing insert-only RLS policy.

ALTER TABLE public.answer_feedback
    ADD COLUMN IF NOT EXISTS history_id UUID
        REFERENCES public.ask_history (id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS shared_view BOOLEAN NOT NULL DEFAULT false;

-- Feedback for one answer (the devtools digest, and the ON DELETE SET NULL
-- scan when an answer is deleted).
CREATE INDEX IF NOT EXISTS answer_feedback_history_id_idx
    ON public.answer_feedback (history_id)
    WHERE history_id IS NOT NULL;
