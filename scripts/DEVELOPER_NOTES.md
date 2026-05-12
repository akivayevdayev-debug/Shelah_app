# Scripts Notes

> Sync status (2026-05-12): Updated to reflect May 2026 changes — service worker v8, AI reliability fixes, and local source links.

## `verify_integrations.py`

Operational health checker for the full stack:
- Environment variable validation.
- Customs JSON validation.
- Supabase connectivity.
- Sefaria and Hebcal API reachability.
- Local Flask detection.
- Vercel deployment and community endpoint checks.

Use when triaging deployment/integration issues.

## `clerk_supabase_rls.py`

Auth bridge utility for Clerk + Supabase RLS:
- Extract/verify Clerk JWT.
- Build JWT-authenticated Supabase client.
- Query user-scoped preference rows safely.

Use when debugging user-specific auth/data access behavior.

## `fetch_sefardic_siddur.py`

Data prep utility:
- Pulls Siddur content from Sefaria.
- Normalizes Hebrew/English content.
- Builds prayer payloads used by runtime prayer endpoints.

Use when refreshing or regenerating prayer source data.
