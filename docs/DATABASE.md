# Sh'elah — Database Schema Reference

> ⚠️ **Interim manual reconstruction (2026-08-21), not yet machine-generated.**
> This file was rewritten by hand against the repo's own `scripts/sql/*.sql` /
> `scripts/migrate_*.sql` files (plan.md §23.1's audit, cross-checked against
> every table name/column read or written by `backend/*.py`), because the
> previous version was wrong in almost every particular — wrong tenant key
> (`clerk_id` instead of `user_id`), a `rag_identity_cache` table that
> doesn't exist, a `bookmarks` table that's actually `study_bookmarks`, a
> `queries` analytics table that was pure fiction — and that drift already
> shipped a real production bug (a legal-consent write silently rejected by
> PostgREST; see plan.md §23.1). This rewrite is believed accurate as of
> 2026-08-21 but has **not** been confirmed against the live Supabase project
> — this environment has no direct Postgres/`information_schema` access.
> **Replace this file with the real generated output** by running
> `scripts/sql/introspect_schema.sql` once in the Supabase SQL editor, then
> `python scripts/generate_database_doc.py` (plan.md §23.2.3) — that is the
> version of this doc that cannot drift silently again, because a
> `--check` mode can diff it against live schema on demand.

This document describes the Supabase (Postgres) `public`-schema tables used
by Sh'elah. The application authenticates users via **Clerk** JWTs and
stores the Clerk user ID (`user_id`) as the tenant key on every user-scoped
table below — there is no `clerk_id` column anywhere in this schema.

> **Secret-key access only**: the backend always uses `SUPABASE_SECRET_KEY`,
> which bypasses RLS. The policies listed per-table below are
> defense-in-depth — see `plan.md` §21 for the open question of whether
> `auth.uid()` actually resolves a Clerk JWT in this project at all (Supabase
> Third-Party Auth for Clerk must be enabled in the dashboard, and that
> setting is versioned nowhere in this repo) — and support direct
> Supabase-dashboard access.

---

## Tables

### `user_preferences`

Per-user application settings and legal-consent/age-attestation records.
Base table: [`scripts/sql/bookmarks_and_preferences_setup.sql`](../scripts/sql/bookmarks_and_preferences_setup.sql).
Additive columns: [`scripts/migrate_user_preferences_legal_consent.sql`](../scripts/migrate_user_preferences_legal_consent.sql), [`scripts/migrate_user_preferences_legal_version.sql`](../scripts/migrate_user_preferences_legal_version.sql), [`scripts/migrate_user_preferences_user_id_to_text.sql`](../scripts/migrate_user_preferences_user_id_to_text.sql) (normalizes `user_id` to `text` — idempotent if already so).

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `user_id` | `text` | NOT NULL | — | Clerk user ID — primary key |
| `prefs` | `text` | YES | — | Serialized UI/app preferences |
| `shelf` | `text` | YES | — | Serialized bookshelf state |
| `notes` | `text` | YES | — | Free-text user notes |
| `reading_state` | `text` | YES | — | Serialized reader position/state |
| `updated_at` | `timestamptz` | NOT NULL | `now()` | Last update timestamp |
| `legal_accepted` | `boolean` | NOT NULL | `false` | Terms + Privacy acceptance |
| `legal_accepted_at` | `timestamptz` | YES | — | UTC timestamp of acceptance |
| `age_attested` | `boolean` | NOT NULL | `false` | plan.md §8.B-AGE.6 13+/16+ attestation |
| `age_attested_at` | `timestamptz` | YES | — | UTC timestamp of attestation |
| `legal_terms_version` | `text` | YES | — | Terms version accepted (plan.md §8.A.1/§8.D.2) |
| `legal_privacy_version` | `text` | YES | — | Privacy Policy version accepted |

**Primary key**: `user_id` · **Upsert conflict target**: `user_id`

**RLS**: enabled + forced. Users may `SELECT`/`INSERT`/`UPDATE`/`DELETE` only their own row (`auth.uid()::text = user_id`).

**Used by**: `POST /api/accept-legal` (upserts consent/attestation/version columns), `GET`/`PUT /api/user/preferences`.

---

### `study_bookmarks`

User-saved texts, references, and AI answers. Base table: [`scripts/sql/bookmarks_and_preferences_setup.sql`](../scripts/sql/bookmarks_and_preferences_setup.sql).

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `id` | `text` | NOT NULL | — | Bookmark primary key |
| `user_id` | `text` | NOT NULL | — | Owning user (Clerk ID) |
| `ref` | `text` | YES | — | Sefaria reference string |
| `label` | `text` | YES | — | Display label |
| `segment_text` | `text` | YES | — | Saved text/segment snippet |
| `ai_summary` | `text` | YES | — | AI-generated summary (semantic bookmarks) |
| `notes` | `text` | YES | — | Free-text user notes |
| `created_at` | `timestamptz` | NOT NULL | `now()` | Creation timestamp |
| `updated_at` | `timestamptz` | NOT NULL | `now()` | Last update timestamp |

**Primary key**: `id` · **Indexes**: `(user_id)`, `(created_at DESC)`

**RLS**: enabled + forced. Users may `SELECT`/`INSERT`/`UPDATE`/`DELETE` only their own rows (`auth.uid()::text = user_id`).

---

### `community_knowledge`

Shared reference knowledge base (not user-scoped). Base table: [`scripts/sql/rag_identity_cache_setup.sql`](../scripts/sql/rag_identity_cache_setup.sql). Populated by `scripts/migrate_customs_to_supabase.py`.

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `id` | `text` | NOT NULL | — | Deterministic content hash — primary key |
| `community_name` | `text` | NOT NULL | — | Community/tradition key (e.g. `sefardic`) |
| `topic` | `text` | NOT NULL | — | Halakhic topic |
| `halakhic_source` | `text` | NOT NULL | — | Source citation |
| `content` | `text` | NOT NULL | — | Reference text |
| `created_at` | `timestamptz` | NOT NULL | `now()` | Creation timestamp |
| `updated_at` | `timestamptz` | NOT NULL | `now()` | Last update timestamp (trigger) |

**Primary key**: `id` · **Indexes**: `(community_name, topic)`, trigram GIN on `topic`/`content` (`pg_trgm`, for fuzzy retrieval).

**RLS**: enabled. Public `SELECT` for `anon`/`authenticated` (`using (true)`) — this table has no per-user data, it's a shared corpus; writes are server-side only (no insert/update/delete policy granted to those roles).

---

### `user_memories`

Server-generated per-user interaction-summary memory, used to build ask-time identity context. Base table: [`scripts/sql/rag_identity_cache_setup.sql`](../scripts/sql/rag_identity_cache_setup.sql) (note: the *file* is named for a `rag_identity_cache` table that this migration never actually creates — it creates `community_knowledge` and this table instead; the filename itself is one of the doc-drift artifacts plan.md §23.1 found).

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `id` | `uuid` | NOT NULL | `gen_random_uuid()` | Row ID |
| `user_id` | `text` | NOT NULL | — | Owning user (Clerk ID) |
| `summary` | `text` | NOT NULL | — | AI-generated interaction summary |
| `created_at` | `timestamptz` | NOT NULL | `now()` | Creation timestamp |
| `updated_at` | `timestamptz` | NOT NULL | `now()` | Last update timestamp (trigger) |

**Primary key**: `id` · **Index**: `(user_id, created_at DESC)`

**RLS**: enabled, governed by `scripts/sql/SUPABASE_RLS_POLICIES.sql`'s owner-scoped policies (`auth.uid()::text = user_id`) — the same idiom as `user_preferences`/`study_bookmarks` above. `backend/rag.py::_store_user_memory_summary`/`_fetch_user_memory_summaries` read/write through the request-scoped, RLS-gated client (`app.py::_get_user_scoped_supabase_client`) first, not the service-role client — corrected 2026-08-31 (plan.md §21/§30.5); the previous text here describing this table as service-role-only was stale and did not match the code.
>
> **Resolved doc-drift, recorded for history:** `rag_identity_cache_setup.sql` (this table's base-table file) originally also shipped a fully-locked-down pair of policies (`user_memories_block_client_select`/`_write`, `using (false)` for `anon`/`authenticated`) from when this table was service-role-only by design. `SUPABASE_RLS_POLICIES.sql` later added owner-scoped policies under different names without removing the old ones; since Postgres OR's multiple `PERMISSIVE` policies together per command, the owner-scoped policies granted access whenever both had actually been applied to the live project — but a project where only the older file had run would have this table fully locked for every real user, contradicting what this doc said. The block-all policies were dropped in the same pass that corrected this paragraph; `SUPABASE_RLS_POLICIES.sql` is now the single source of truth for this table's RLS.

---

### `ask_history`

Per-user record of completed `/ask` interactions, including defensibility-logging metadata. Base table: [`scripts/migrate_ask_history.sql`](../scripts/migrate_ask_history.sql). Additive columns: [`scripts/migrate_ask_history_safety_metadata.sql`](../scripts/migrate_ask_history_safety_metadata.sql).

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `id` | `uuid` | NOT NULL | `gen_random_uuid()` | Row ID |
| `user_id` | `text` | NOT NULL | — | Clerk `sub` claim |
| `question` | `text` | NOT NULL | — | User's question (truncated to 2000 chars by the app) |
| `answer` | `text` | NOT NULL | `''` | AI answer (truncated to 10000 chars by the app) |
| `sources` | `jsonb` | NOT NULL | `'[]'` | Source list |
| `ai_cited_sources` | `text[]` | NOT NULL | `'{}'` | AI-cited source subset |
| `community` | `text` | NOT NULL | `'All'` | Community lens used |
| `mode` | `text` | NOT NULL | `'balanced'` | Answer mode |
| `language` | `text` | NOT NULL | `'en'` | Response language |
| `safety_class` | `text` | NOT NULL | `'ok'` | plan.md §8.B-AGE safety-routing outcome |
| `prompt_version` | `text` | YES | — | Governing system-prompt version |
| `created_at` | `timestamptz` | NOT NULL | `now()` | Creation timestamp |

**Primary key**: `id` · **Index**: `(user_id, created_at DESC)`

**RLS**: enabled, zero policies — service-role-only by design, decided 2026-08-31 (plan.md §21 STEP 6a). `backend/routes_user.py`'s GET/DELETE `/api/user/history` handlers only ever read this table through the service-role client (`_get_supabase_client()`, `app.py:910`, which uses `SUPABASE_SECRET_KEY` specifically to bypass RLS) with a hand-written `.eq("user_id", ...)` filter, so a user-scoped RLS policy is never actually exercised by any code path. Same posture as `ai_usage_log` below.

**Correction (2026-09-03):** the previous `user_own_history` policy was written up as "dropped rather than migrated on 2026-08-31," but a live `pg_policies` query via `npx supabase db query --linked` on 2026-09-03 found it was still present in production, unwrapped, and still flagged by the Advisor's `auth_rls_initplan` lint — the DROP had been documented but never actually executed live. Verified via CLI query that this policy was genuinely never consulted by any code path (confirmed above), then dropped for real: `DROP POLICY IF EXISTS "user_own_history" ON public.ask_history;` run directly via `npx supabase db query --linked`, 2026-09-03. Re-ran `npx supabase db advisors --linked` immediately after: `ask_history` now shows the same `rls_enabled_no_policy` (INFO, by-design) posture as `ai_usage_log`, and the `auth_rls_initplan` finding for it is gone. See `docs/SECURITY.md` §2 for the full history.

**Retention**: 90-day rolling window, enforced by the `retention_enforce` cron.

---

### `ai_usage_log`

Per-call AI cost ledger and atomic per-caller daily-budget reservation bookkeeping. Base table: [`scripts/sql/ai_usage_log_setup.sql`](../scripts/sql/ai_usage_log_setup.sql) (reconstructed 2026-08-20 — this table's original `CREATE TABLE` was never committed anywhere). Additive columns: [`scripts/migrate_ai_usage_log_add_user_columns.sql`](../scripts/migrate_ai_usage_log_add_user_columns.sql), [`scripts/sql/check_and_reserve_user_budget.sql`](../scripts/sql/check_and_reserve_user_budget.sql) (reservation columns + the `check_and_reserve_user_budget()` function below).

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `id` | `uuid` | NOT NULL | `gen_random_uuid()` | Row ID |
| `provider` | `text` | YES | — | e.g. `anthropic`, `gemini` |
| `model` | `text` | YES | — | Model identifier (renamed from the misleadingly-named `provider`-holds-a-model-name convention, plan.md §20a) |
| `input_tokens` | `integer` | YES | — | Input token count |
| `output_tokens` | `integer` | YES | — | Output token count |
| `cost_usd` | `numeric` | NOT NULL | `0` | Computed cost |
| `route` | `text` | YES | — | Originating route |
| `request_id` | `text` | YES | — | Request-ID contextvar, for correlation |
| `user_id` | `text` | YES | — | Authenticated caller (Clerk `sub`) |
| `client_key` | `text` | YES | — | `ip:<addr>` fallback key for anonymous callers |
| `reserved` | `boolean` | NOT NULL | `false` | Row is an in-flight budget reservation, not a settled call |
| `reservation_id` | `uuid` | YES | — | Reservation identity (unique when set) |
| `reservation_expires_at` | `timestamptz` | YES | — | TTL for abandoned reservations (10 min) |
| `created_at` | `timestamptz` | NOT NULL | `now()` | Creation timestamp |

**Primary key**: `id` · **Indexes**: `(created_at)`, `(user_id, created_at)`, `(client_key, created_at)`, unique on `(reservation_id)` where set, `(reserved, reservation_expires_at)` where `reserved`.

**RLS**: **none** — deliberate. This table is written and read exclusively by the server-side `SUPABASE_SECRET_KEY` client, never from the browser.

**Function**: `public.check_and_reserve_user_budget(key_column, key_value, threshold_usd, reservation_usd)` — atomic check-and-reserve for the per-caller daily spend ceiling (plan.md §20.2 Phase 20b), `pg_advisory_xact_lock`-serialized per key, `REVOKE ALL FROM PUBLIC` (service-role/RPC-only).

**Retention**: 90-day rolling window, enforced by the `retention_enforce` cron; abandoned reservations past their 10-minute TTL are swept by the same cron (`expire_stale_budget_reservations()`).

---

### `answer_feedback`

Write-only thumbs up/down (plus an optional short comment) a reader leaves on an AI answer, accepted from signed-in and signed-out readers alike — `user_id` is nullable, so unlike `ask_history` there is no owner-per-row invariant to enforce with RLS. Written by `POST /api/feedback` (`backend/routes_feedback.py`); read by `GET /api/devtools/feedback-digest` (`backend/routes_devtools.py`), which is auth-gated (`@require_clerk_auth`) because comments are free-text supplied by readers and not meant for public display. Base table: [`scripts/sql/migrate_answer_feedback.sql`](../scripts/sql/migrate_answer_feedback.sql). Grant hardening: [`scripts/sql/migrate_security_hardening.sql`](../scripts/sql/migrate_security_hardening.sql).

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `id` | `uuid` | NOT NULL | `gen_random_uuid()` | Row ID |
| `user_id` | `text` | YES | — | Clerk `sub` claim; null for signed-out feedback |
| `question_hash` | `text` | NOT NULL | — | SHA-256 of the question text |
| `verdict` | `text` | NOT NULL | — | `helpful` or `not_helpful` (`CHECK` constraint) |
| `comment` | `text` | NOT NULL | `''` | Optional free-text comment, capped at 500 chars |
| `mode` | `text` | NOT NULL | `'balanced'` | Answer mode the feedback refers to |
| `language` | `text` | NOT NULL | `'en'` | Response language |
| `fallback` | `boolean` | NOT NULL | `false` | Whether the answer was a fallback response |
| `safety_class` | `text` | NOT NULL | `'ok'` | plan.md §8.B-AGE safety-routing outcome |
| `created_at` | `timestamptz` | NOT NULL | `now()` | Creation timestamp |

**Primary key**: `id` · **Index**: `(created_at DESC)`

**RLS**: enabled. Policy `anyone_can_submit_feedback` (`FOR INSERT`, `WITH CHECK (true)`) lets both `anon` and `authenticated` insert rows — a deliberate public write-only form, not a per-user-owned resource. There is no `SELECT` policy for either role; only the backend's service-role client (which bypasses RLS) reads this table. The default `SELECT` grant `anon`/`authenticated` get on a new Supabase table was separately revoked (`migrate_security_hardening.sql`) to close a Supabase-linter-flagged schema-exposure gap, since a missing policy alone still let the table (and its column names) be discovered via GraphQL/PostgREST even with every row read blocked.

**Retention**: no dedicated rolling-window cron (not part of `retention_enforce`'s scope); retained until account deletion. Included in both `/api/user/data-export` and `/api/user/delete-account` (`backend/routes_privacy.py`'s `_USER_DATA_TABLES`, added 2026-08-27 per plan.md §39.1 — see `docs/SECURITY.md` §9).

---

## Introspection function

`public.get_schema_snapshot()` — [`scripts/sql/introspect_schema.sql`](../scripts/sql/introspect_schema.sql) — the read-only RPC this generated-doc pipeline depends on. `REVOKE ALL FROM PUBLIC`, same posture as `check_and_reserve_user_budget()`.

---

## Migrations

There is no `supabase/` CLI migration directory in this repo (a CLI-migration
workflow adoption was considered and deliberately deferred — plan.md
§23.2.3, to avoid taking on that workflow-migration risk before the schema
was even *recorded*). Schema changes are hand-pasted `.sql` files, run once
in the Supabase SQL editor: base tables/functions live in `scripts/sql/`,
additive `ALTER`s live in `scripts/migrate_*.sql`. There is no
migration-tracking table, ordering convention, or rollback SQL — this doc
plus each file's own header comment (provenance, dependencies) is the
closest thing to one.

---

## Data Retention

| Table | Retention policy |
|---|---|
| `user_preferences` | Retained until account deletion (DSR) |
| `study_bookmarks` | Retained until account deletion; exported on GDPR request |
| `community_knowledge` | Not user data — retained indefinitely (reference corpus) |
| `user_memories` | Retained until account deletion; not directly user-readable |
| `ask_history` | 90-day rolling window, `retention_enforce` cron |
| `ai_usage_log` | 90-day rolling window, `retention_enforce` cron; abandoned reservations swept on a 10-minute TTL |
| `answer_feedback` | Retained until account deletion; not covered by `retention_enforce` |

Users may request full data export or deletion via `/api/user/data-export` and `/api/user/delete-account` (see `docs/PRIVACY_OPERATIONS.md`).

---

## Live Supabase Advisor scan — 2026-09-03

A Supabase Advisor scan was run directly against the live project on
2026-09-03. This subsection records every finding and its resolution.
Two are real bugs with a **fix written but not yet applied** — the
operator must run the migration below by hand; everything else was
either already correct by design or reviewed and deliberately left as
is. See `docs/SECURITY.md` §10 for the security-focused summary of the
same scan.

### Live fixes — pending operator execution

**`scripts/sql/migrate_search_path_and_rls_perf_fixes.sql`** (new file,
this pass) contains every statement below. Run it once, by hand, in the
Supabase SQL editor; it is idempotent.

1. **`function_search_path_mutable`** (WARN, SECURITY) — `public.get_schema_snapshot()`
   and `public.set_updated_at_timestamp()` both had a role-mutable
   `search_path`. Neither is `SECURITY DEFINER` (confirmed by reading
   their live definitions in `scripts/sql/introspect_schema.sql` /
   `scripts/sql/rag_identity_cache_setup.sql`), so this is the
   ordinary object-hijacking risk, not the higher-risk elevated-privilege
   variant. Fixed with `ALTER FUNCTION ... SET search_path = public,
   pg_temp;` for both. Note: `set_updated_at_timestamp()` was already
   fixed once, in `scripts/sql/migrate_security_hardening.sql`
   (2026-08-21, `SET search_path = ''`) — it regressed because
   `scripts/sql/rag_identity_cache_setup.sql` defines the function with a
   bare `create or replace function ...` and `CREATE OR REPLACE` does not
   preserve a function's prior `ALTER FUNCTION ... SET` config. If that
   setup file is ever re-run, this finding will resurface until its
   `CREATE OR REPLACE` itself carries the `SET` clause (not changed in
   this pass — out of scope).

2. **`auth_rls_initplan`** (WARN, PERFORMANCE) — 12 live "own row" RLS
   policies (4 each on `user_preferences`, `study_bookmarks`,
   `user_memories`) called `auth.jwt()` unwrapped in `USING`/`WITH CHECK`,
   forcing a per-row re-evaluation instead of once per query. Fixed by
   wrapping every call as `(select auth.jwt())` — same logic, planner
   evaluates it once. `scripts/sql/SUPABASE_RLS_POLICIES.sql` (the
   canonical policy source-of-truth) was updated in the same pass so it
   stays in sync with the live fix.
   - The Advisor's finding also named a 13th policy, `user_own_history`
     on `ask_history`. At the time this migration file was authored it
     was believed already dropped (2026-08-31) and so was intentionally
     excluded — an `ALTER POLICY` against a nonexistent policy would
     just error. A live `pg_policies` check on 2026-09-03 found that
     belief was wrong: the policy was still present in production,
     unwrapped. Since it's confirmed dead code (see the `ask_history`
     entry above), it was dropped for real via
     `npx supabase db query --linked` on 2026-09-03 rather than wrapped —
     removing the object resolves this finding the same way the drop was
     always meant to. See the `ask_history` entry above for the full
     correction.

3. **`pg_graphql_anon_table_exposed` / `pg_graphql_authenticated_table_exposed`**
   (WARN, SECURITY) — `ai_usage_log`, `ask_history`, `study_bookmarks`,
   `user_memories`, `user_preferences` were all visible in the GraphQL
   schema to `anon`/`authenticated` even though this app's tables are
   meant to be reached only through the Flask/FastAPI backend.
   `community_knowledge` is also flagged but is **deliberately
   public-read** (`community_knowledge_read`, `USING (true)`, per its
   entry above) — left untouched.
   - **`ai_usage_log`, `ask_history`: `REVOKE SELECT ... FROM anon,
     authenticated;`** — safe unconditionally. Both are zero-policy,
     service-role-only tables by design (deny-all for both roles
     regardless of this grant); direct code read confirms every access
     path for both (`backend/cost_meter.py` for `ai_usage_log`;
     `backend/routes_user.py`'s history endpoints and
     `backend/rag.py::_store_ask_history` for `ask_history`) uses the
     service-role client exclusively, never the user-scoped client.
   - **`study_bookmarks`, `user_memories`, `user_preferences`: `REVOKE
     SELECT ... FROM anon;` only — `authenticated` was deliberately left
     alone.** These three have real owner-scoped RLS policies (fixed in
     item 2 above) meant for an `authenticated`-role client. Revoking
     `anon` is unconditionally safe (`auth.jwt()` is always null for an
     anon session, so the policy already blocks it). The prior research
     phase's frontend-direct-access audit found no *browser-side*
     Supabase session anywhere and proposed also revoking
     `authenticated` on this basis — but that audit didn't check which
     Postgres role the **backend's own** RLS-scoped client authenticates
     as. Direct code read for this pass found it does matter here:
     `backend/routes_user.py`'s `user_preferences` and
     `semantic_bookmarks` handlers, and `backend/rag.py`'s
     `_fetch_user_memory_summaries` / `_store_user_memory_summary`, all
     call `app.py::_get_user_scoped_supabase_client()` — a client built
     from `SUPABASE_PUBLISHABLE_KEY` forwarding the caller's Clerk bearer
     token, i.e. it authenticates as the Postgres `authenticated` role —
     as their **primary, not fallback**, path. `STRICT_SUPABASE_RLS` is a
     hardcoded `True` literal in `app.py` (`docs/SECURITY.md` §2), so the
     `_get_supabase_client()` (service-role) fallback each of these sites
     also has can never actually fire. Since Postgres requires a
     table-level `GRANT` before RLS is even consulted, revoking `SELECT`
     from `authenticated` on these three tables would not narrow row
     access — the RLS policies already do that correctly — it would break
     `GET`/`PUT /api/user/preferences`, the semantic-bookmarks route, and
     both RAG memory read/write paths outright. That's a production
     regression, not hardening, so it was skipped. The residual GraphQL
     schema-visibility exposure to `authenticated` for these three tables
     is accepted: fully closing it would mean moving their backend access
     onto the service-role client with an app-level ownership check
     instead (the pattern `ask_history` already uses) — a larger,
     separately-scoped change, not done here.

### Reviewed, no fix needed / already correct by design

4. **`rls_policy_always_true`** (WARN, SECURITY) on
   `answer_feedback.anyone_can_submit_feedback` (`INSERT`, `WITH CHECK
   true`) — **intentional, confirmed by design, not a gap.**
   `backend/routes_feedback.py:32` decorates `POST /api/feedback` with
   `@maybe_require_clerk_auth` (not a hard-require decorator); the module
   docstring (`routes_feedback.py:4-7`) states feedback is accepted from
   signed-out readers too, matching `accept_legal()`'s optional-auth
   pattern. `user_id` in the stored record is nullable
   (`routes_feedback.py:49`). No DB policy change — that would break the
   feature. `/api/feedback` already has a **dedicated, tighter-than-
   default** rate-limit policy (`backend/rate_limit.py:90,118` — the
   `"feedback"` class, 10 requests/60s, IP-keyed, fail-open — versus the
   120/min `"cheap"` default unmatched routes get), so the FUTURE
   recommendation this finding would otherwise carry (add a dedicated
   stricter policy, reusing the `"webhook"` 15-requests/60s pattern) does
   **not** apply here — that mitigation already exists. No further action.

5. **`rls_enabled_no_policy`** (INFO) on `ai_usage_log` — same
   intentional service-role-only pattern already documented above for
   `ask_history`; no fix needed. (Note: this table's entry above says
   "RLS: none — deliberate", describing its zero-*policy* posture; the
   live Advisor scan confirms RLS is actually *enabled* on the table
   with zero policies attached, which is precisely what makes this an
   INFO-level `rls_enabled_no_policy` finding rather than a no-RLS gap —
   the wording above is being flagged here for a future doc pass, not
   corrected in this one.)

6. **`unused_index`** (INFO, 9 indexes across `ask_history`,
   `community_knowledge`, `study_bookmarks`, `ai_usage_log`,
   `answer_feedback`, `user_memories`, `user_preferences`) — reviewed,
   **kept**. These are recently-added tables with thin production
   traffic so far; the indexes back the exact `WHERE user_id = ...` /
   `WHERE created_at ...` query patterns this app runs (see each table's
   **Indexes** line above). No action.

7. **`FORCE ROW SECURITY` asymmetry** (observed directly via a live
   `pg_class` query, not a formal Advisor finding) — `ai_usage_log`,
   `answer_feedback`, `ask_history`, `community_knowledge` have
   `relforcerowsecurity = false`; `study_bookmarks`, `user_memories`,
   `user_preferences` have it `= true` (matching
   `scripts/sql/SUPABASE_RLS_POLICIES.sql`'s explicit `force row level
   security` statements for the latter three). `FORCE` only changes
   whether RLS applies to the table **owner** role — `service_role`
   bypasses RLS entirely via `BYPASSRLS` regardless of this setting — so
   this is a low-priority inconsistency, not a live security gap. Noted
   for future cleanup consideration; no SQL change in this pass.
