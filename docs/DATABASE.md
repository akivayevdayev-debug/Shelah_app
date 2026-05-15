# Sh'elah — Database Schema Reference

This document describes the Supabase (Postgres) schema used by Sh'elah.  All tables use Supabase's built-in `auth.uid()` for row-level security where applicable, but the application authenticates users via **Clerk** JWTs and stores the Clerk user ID (`clerk_id`) as the primary tenant key.

> **Service-role access only**: The backend always uses the `SUPABASE_SERVICE_ROLE_KEY`, which bypasses RLS. RLS policies are still defined as a defense-in-depth measure and for direct Supabase dashboard access.

---

## Tables

### `user_preferences`

Stores per-user application settings and legal-acceptance records.

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `clerk_id` | `text` | NOT NULL | — | Clerk user ID — primary key |
| `legal_accepted` | `boolean` | YES | `false` | Whether the user has accepted Terms + Privacy |
| `legal_accepted_at` | `timestamptz` | YES | — | UTC timestamp of acceptance |
| `community_lens` | `text` | YES | — | Selected community customs key (e.g. `sefardic`, `ashkenaz`) |
| `preferences` | `jsonb` | YES | `'{}'` | Arbitrary user settings (UI language, display options, etc.) |
| `created_at` | `timestamptz` | YES | `now()` | Row creation timestamp |
| `updated_at` | `timestamptz` | YES | `now()` | Last update timestamp (via trigger) |

**Primary Key**: `clerk_id`  
**Upsert conflict target**: `clerk_id`

**RLS**: Users may read and update only their own row (`clerk_id = auth.uid()`). The service role can read/write all rows.

**Used by**:
- `POST /api/accept-legal` — upserts `legal_accepted`, `legal_accepted_at`
- `GET /api/ask` — reads `community_lens`, `preferences`

---

### `rag_identity_cache`

Caches the AI-generated identity context paragraph for each user, avoiding re-generation on every request.  Keyed by `clerk_id`.

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `clerk_id` | `text` | NOT NULL | — | Clerk user ID — primary key |
| `identity_text` | `text` | YES | — | Cached identity/context paragraph fed into AI prompt |
| `generated_at` | `timestamptz` | YES | `now()` | When the cache entry was last regenerated |
| `version` | `integer` | YES | `1` | Cache version — bump to invalidate |

**Primary Key**: `clerk_id`  
**TTL**: Soft — regenerated when `generated_at` is older than a configurable threshold (currently 24 h).

**Setup SQL**: [`scripts/sql/rag_identity_cache_setup.sql`](../scripts/sql/rag_identity_cache_setup.sql)

---

### `bookmarks`

Stores user-saved texts, references, and AI answers.

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `id` | `uuid` | NOT NULL | `gen_random_uuid()` | Bookmark primary key |
| `clerk_id` | `text` | NOT NULL | — | Owning user (Clerk ID) |
| `type` | `text` | NOT NULL | — | `text` \| `question` \| `prayer` |
| `reference` | `text` | YES | — | Sefaria reference string (e.g. `Berakhot.2a`) |
| `title` | `text` | YES | — | Display title |
| `content_he` | `text` | YES | — | Hebrew text snippet |
| `content_en` | `text` | YES | — | English text snippet |
| `metadata` | `jsonb` | YES | `'{}'` | Extra data (AI answer, source list, etc.) |
| `created_at` | `timestamptz` | YES | `now()` | Creation timestamp |

**Primary Key**: `id`  
**Index**: `(clerk_id, created_at DESC)` for per-user sorted queries.

**RLS**: Users may read, insert, and delete only their own rows (`clerk_id = auth.uid()`).

**Setup SQL**: [`scripts/sql/bookmarks_and_preferences_setup.sql`](../scripts/sql/bookmarks_and_preferences_setup.sql)

---

### `queries` *(optional analytics)*

Log of AI questions submitted via `/api/ask`.  May be disabled in production via `ENABLE_QUERY_LOG=false`.

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `id` | `uuid` | NOT NULL | `gen_random_uuid()` | Row ID |
| `clerk_id` | `text` | YES | — | User (nullable for anonymous queries) |
| `question` | `text` | NOT NULL | — | User's question text |
| `community_lens` | `text` | YES | — | Community tradition used |
| `answer_lang` | `text` | YES | — | Response language |
| `model_used` | `text` | YES | — | AI model that generated the answer |
| `response_ms` | `integer` | YES | — | End-to-end response time in milliseconds |
| `fallback` | `boolean` | YES | `false` | Whether the fallback (Claude) model was used |
| `created_at` | `timestamptz` | YES | `now()` | Timestamp |

**Primary Key**: `id`  
**RLS**: No user-facing read access. Backend service role only.

---

## RLS Policy Reference

Full Supabase RLS policy SQL is in [`scripts/sql/SUPABASE_RLS_POLICIES.sql`](../scripts/sql/SUPABASE_RLS_POLICIES.sql).

Key pattern used throughout:

```sql
-- Users read their own row
CREATE POLICY "user_read_own" ON public.user_preferences
  FOR SELECT USING (clerk_id = auth.uid()::text);

-- Users update their own row
CREATE POLICY "user_update_own" ON public.user_preferences
  FOR UPDATE USING (clerk_id = auth.uid()::text);
```

> Note: `auth.uid()` returns the Supabase/JWT user ID. Because Sh'elah uses Clerk JWTs, the Supabase project must be configured to accept Clerk-signed tokens (or you must use the service role key on the backend and never expose user-token-based access to Supabase directly from the client).

---

## Migrations

Schema changes are applied manually via the Supabase SQL editor or the Supabase CLI:

```bash
supabase db push          # apply local migrations
supabase db diff          # preview pending changes
```

Migration files are tracked in `scripts/sql/`.

---

## Data Retention

| Table | Retention Policy |
|---|---|
| `user_preferences` | Retained until user deletes account |
| `rag_identity_cache` | Soft-expired after 24 h; hard-deleted on account deletion |
| `bookmarks` | Retained until user deletes; exported on GDPR request |
| `queries` | 90-day rolling window (if analytics enabled) |

Users may request full data export or deletion by contacting support (see Privacy Policy at `/privacy`).
