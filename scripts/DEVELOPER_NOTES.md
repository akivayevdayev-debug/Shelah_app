# Scripts notes

What each script does and what it needs. [`README.md`](README.md) classifies them by how often you would run them. None embed credentials; auth and database access come from environment variables (see [`docs/ENVIRONMENT.md`](../docs/ENVIRONMENT.md)).

## Live checks (read-only against a real stack)

| Script | Does |
|---|---|
| `verify_integrations.py` | Health-checks the full stack: environment variables, customs JSON, Supabase, Sefaria and Hebcal reachability, a local Flask server, and the Vercel deployment and community endpoints. Run it when triaging a deployment or integration problem |
| `verify_rls.py` | Live Row Level Security acceptance check: as two dedicated test users it inserts sentinel rows into the RLS-protected tables directly through PostgREST and confirms each user can read their own rows and not the other's. Driven by `.github/workflows/rls-verify.yml`, which is manual |
| `verify_answer_feedback_migration.py` | After `sql/migrate_answer_feedback.sql`: confirms anon and authenticated can insert but not select, structurally (`get_schema_snapshot()`) and by trying to read back a probe row |
| `verify_conversations_migration.py` | After `sql/conversations_setup.sql`: confirms the conversation, message and citation tables exist with RLS enabled and forced and the owner-only policies, then round-trips one row of each through the service-role client |
| `verify_library_removals.py` | Re-checks the library leaf report's removals against the public Sefaria-Export bucket and writes `reports/library_leaf_reinstated.json` |
| `redteam_retrieved_context.py` | Opt-in, live red-team harness for indirect prompt injection through retrieved context. It sends attack payloads through the real model to test the defence-in-depth argument in [`docs/AI_SECURITY_REVIEW.md`](../docs/AI_SECURITY_REVIEW.md). It spends model credits; never run it in CI |

## Data builders

| Script | Does |
|---|---|
| `build_siddur.py` | Builds `data/siddur/edot-hamizrach/` from the public Sefaria-Export bucket: the schema, the Hebrew text and the English community translation, each version checked as CC0 or Public Domain, every line typed by `backend/siddur_lines.py`. Re-run when the export or the script's curated table of contents changes, and commit the output |
| `generate_glossary_json.py` | Builds `static/data/glossary.json` from the same lexicon lookup the AI and the reader's word lookup use (`backend.helpers._lookup_hebrew_word_meaning`), accepting a result only if it is a real lexicon hit and otherwise using the curated fallback gloss. A build-time step, not a per-request fan-out |
| `migrate_customs_to_supabase.py` | Seeds Supabase `community_knowledge` from `customs/*.json` with deterministic upserts (`--dry-run`, `--community <lens key>`, `--prune`, `--emit-sql <dir>`) |
| `crawl_library_leaves.py` | Re-crawls the Sefaria library tree and regenerates `reports/library_leaf_remove_fix_report.full.json`. Slow; the output is committed |
| `generate_database_doc.py` | Regenerates `docs/DATABASE.md` from the live Supabase schema once `sql/introspect_schema.sql` has been applied; `--check` diffs the committed file against the live schema. Needs live credentials |

## Maintenance and CI helpers

| Script | Does |
|---|---|
| `recompute_ai_usage_log_cost.py` | One-off backfill that recomputes historical `ai_usage_log.cost_usd` under the current price table. Mutates ledger rows, so it is not wired into any pipeline: run the dry run first and review it |
| `a11y_dark_scan.js` | The dark-theme half of the WCAG 2.1 AA gate (`pa11y-ci` only exercises light). Run by `npm run test:a11y:dark` and by CI |
| `merge_lcov.py` | Folds duplicate per-file records in Node's lcov output into one each, so SonarCloud reads JS coverage correctly (`npm run test:coverage`) |
| `check_prompt_doc_sync.py` | The advisory pre-commit report that compares status claims in `claude_code_prompts.md` with `plan.md`. It never blocks a commit |

## SQL

`sql/` holds the Supabase schema, migrations and policies. Apply them by hand in the Supabase SQL Editor (the CLI's pooler connection times out) and then run the matching `verify_*` script. Notable files: `SUPABASE_RLS_POLICIES.sql`, `bookmarks_and_preferences_setup.sql`, `ai_usage_log_setup.sql`, `check_and_reserve_user_budget.sql`, `conversations_setup.sql`, `rag_identity_cache_setup.sql` (despite its name it creates `community_knowledge` and `user_memories`), `introspect_schema.sql`, and the dated `migrate_*.sql` files. Schema documentation: [`docs/DATABASE.md`](../docs/DATABASE.md).
