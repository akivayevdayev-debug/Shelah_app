-- Migration: add legal-document *version* columns to user_preferences.
-- Run this once in the Supabase SQL Editor for your project.
--
-- Provenance (plan.md §23.2.1): this file was untracked in git until this
-- pass -- provenance unknown as of 2026-08-21. Same load-bearing/
-- unconfirmed-against-live-schema caveat as
-- scripts/migrate_user_preferences_legal_consent.sql above (see
-- scripts/generate_database_doc.py, plan.md §23.2.3).
--
-- plan.md §8.A.1/§8.D.2: click-through consent must record which version
-- of the Terms of Service and Privacy Policy a user accepted, not just
-- that they accepted *something*, so a material document revision can be
-- detected and re-prompted. backend/routes_user.py::accept_legal() now
-- stores legal_terms_version / legal_privacy_version alongside the
-- existing legal_accepted / legal_accepted_at / age_attested /
-- age_attested_at columns from migrate_user_preferences_legal_consent.sql.
-- This migration is additive and idempotent (safe to run whether or not
-- the columns already exist from a prior out-of-band change).

ALTER TABLE public.user_preferences
    ADD COLUMN IF NOT EXISTS legal_terms_version TEXT,
    ADD COLUMN IF NOT EXISTS legal_privacy_version TEXT;
