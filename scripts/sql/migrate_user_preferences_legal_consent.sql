-- Migration: add legal-consent + age-attestation columns to user_preferences.
-- Run this once in the Supabase SQL Editor for your project.
--
-- Provenance (plan.md §23.2.1): this file was untracked in git until this
-- pass -- provenance unknown as of 2026-08-21. backend/routes_user.py's
-- accept_legal() writes these columns on every call, so this migration is
-- load-bearing if the live table lacks them, but whether/when it was
-- actually applied to the live Supabase project has not been
-- independently confirmed from this environment (see
-- scripts/generate_database_doc.py, plan.md §23.2.3).
--
-- backend/routes_user.py::accept_legal() has stored legal_accepted /
-- legal_accepted_at since before this migration existed, and (plan.md
-- §8.B-AGE.6) now also stores age_attested / age_attested_at when the
-- client sends {"age_attested": true}. scripts/sql/bookmarks_and_preferences_setup.sql
-- does not define any of these four columns on user_preferences — this
-- migration is additive and idempotent (safe to run whether or not the
-- columns already exist from a prior out-of-band change).

ALTER TABLE public.user_preferences
    ADD COLUMN IF NOT EXISTS legal_accepted BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS legal_accepted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS age_attested BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS age_attested_at TIMESTAMPTZ;
