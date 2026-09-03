-- Migration: add defensibility-logging metadata to ask_history (plan.md §8.B.6).
-- Run this once in the Supabase SQL Editor for your project, after
-- scripts/migrate_ask_history.sql has already been applied.
--
-- Provenance (plan.md §23.2.1): this file was untracked in git until this
-- pass -- provenance unknown as of 2026-08-21. backend/rag.py writes
-- safety_class/prompt_version on every _store_ask_history() call today,
-- so this migration is load-bearing if the live table lacks these
-- columns, but that has not been independently confirmed from this
-- environment (see scripts/generate_database_doc.py, plan.md §23.2.3).
--
-- Adds the §8.B-AGE safety_class routing outcome and the governing
-- system-prompt version to each stored ask-history row, so a stored
-- answer's safety classification and prompt version are reconstructable
-- during a dispute without retaining the full prompt text itself.
--
-- backend/rag.py::_store_ask_history only includes these two keys in its
-- insert payload — running this migration is required before that code
-- path is deployed, or the insert will fail schema validation and
-- ask-history storage will silently stop working (caught by the existing
-- broad except-and-log-nothing in _store_ask_history).

ALTER TABLE public.ask_history
    ADD COLUMN IF NOT EXISTS safety_class TEXT NOT NULL DEFAULT 'ok',
    ADD COLUMN IF NOT EXISTS prompt_version TEXT;
