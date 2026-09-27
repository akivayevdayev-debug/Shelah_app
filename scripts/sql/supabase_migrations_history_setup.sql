-- Setup: an empty Supabase CLI migration-history table.
-- Run this once in the Supabase SQL Editor.
--
-- Why: the Supabase dashboard's Database > Migrations page and the Supabase
-- CLI (`supabase migration list`, `db push`, `db pull`) read
--   SELECT version FROM supabase_migrations.schema_migrations ORDER BY version
-- This project applies its schema by hand in the SQL Editor (docs/DATABASE.md
-- "Migrations"), so that table was never created and every such read logged
-- `relation "supabase_migrations.schema_migrations" does not exist` (42P01)
-- in postgres_logs. It was noise, not a failure of the app, which never
-- reads this table.
--
-- This creates the table with the CLI's own base columns. It stays empty:
-- the migrations page shows "no migrations" instead of an error. If the CLI
-- migration workflow is adopted later, the CLI adds any newer columns it
-- needs (it runs ADD COLUMN IF NOT EXISTS itself).
--
-- Not exposed through the API: supabase_migrations is not in the exposed
-- schemas, and nothing is granted to anon or authenticated.
--
-- Idempotent.

CREATE SCHEMA IF NOT EXISTS supabase_migrations;

CREATE TABLE IF NOT EXISTS supabase_migrations.schema_migrations (
    version    TEXT NOT NULL PRIMARY KEY,
    statements TEXT[],
    name       TEXT
);

REVOKE ALL ON SCHEMA supabase_migrations FROM PUBLIC, anon, authenticated;
REVOKE ALL ON supabase_migrations.schema_migrations FROM PUBLIC, anon, authenticated;
