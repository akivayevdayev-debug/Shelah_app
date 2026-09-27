# `scripts/` index

Operational one-offs and setup utilities. None embed credentials — all auth/DB
access goes through environment variables (`.env`). See
[`DEVELOPER_NOTES.md`](DEVELOPER_NOTES.md) for what each Python script actually
does; this file just classifies how often you'd run them.

## Repeatable (safe to re-run anytime)

- **`verify_integrations.py`** — health-checks the full stack (env vars, customs
  JSON, Supabase, Sefaria/Hebcal, local Flask, Vercel). Run when triaging a
  deployment or integration issue.
- **`verify_answer_feedback_migration.py`** — live acceptance check for the
  `answer_feedback` RLS migration (`sql/migrate_answer_feedback.sql`):
  confirms against the real database that anon/authenticated can insert but
  cannot select, both structurally (via the `get_schema_snapshot()` RPC) and
  empirically (insert a probe row, then try to read it back with the anon
  client). Run after applying that migration in the Supabase SQL editor, or
  anytime you want to re-verify the policy is still in effect.
- **`crawl_library_leaves.py`** — re-crawls the Sefaria library tree and
  regenerates the leaf remove/fix report. Re-run only when that report
  (`reports/library_leaf_remove_fix_report.full.json`, read at runtime by
  `backend/sefaria_library.py`) needs refreshing — it's slow (probes every
  leaf) and the output is committed, so this isn't part of any normal workflow.
  It needs www.sefaria.org. A complex-schema work (siddur, machzor, haggadah)
  is probed at the first leaf of its schema, the ref the library opens it at;
  the April 2026 report predates that and removed 441 such works on bare-title
  400s.
- **`verify_library_removals.py`** — re-checks that report's removals against
  the public Sefaria-Export bucket (no sefaria.org access needed) and writes
  `reports/library_leaf_reinstated.json`: the removed works that have a
  complex schema and real text there. The backend takes those back out of the
  removals for that report run only, so re-running the crawler supersedes it.
  Re-run after regenerating the report only if the new crawl can't reach
  sefaria.org's per-title index.

## One-time (setup / migration)

- **`migrate_customs_to_supabase.py`** — seeds `community_knowledge` from the
  `customs/*.json` files. Supports `--dry-run` and `--community <name>`. Run
  once per environment, or after a customs-data change you want pushed.
- **`build_siddur.py`** — rebuilds the checked-in siddur
  (`data/siddur/edot-hamizrach/`) from the public Sefaria-Export bucket:
  curated table of contents plus typed lines per service. Refuses any text
  version not licensed CC0 or Public Domain. Re-run when Sefaria's export or
  the curation in the script changes, and commit the output.
- **`migrate_ask_history.sql`** — run once in the Supabase SQL editor to create
  the `ask_history` table + RLS policy.
- **`sql/SUPABASE_RLS_POLICIES.sql`**, **`sql/bookmarks_and_preferences_setup.sql`**,
  **`sql/rag_identity_cache_setup.sql`** — one-time Supabase schema/RLS setup,
  run directly in the SQL editor when provisioning a new environment.
