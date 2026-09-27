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

## `build_siddur.py`

Data prep utility for the siddur (`/siddur`, `/api/siddur/v2/*`):
- Reads Siddur Edot HaMizrach from the public Sefaria-Export bucket
  (schema + Hebrew " Shaliehsaboo Edition" + English Community Translation).
- Checks each version's license (CC0 / Public Domain only).
- Types every line (heading / instruction / conditional / prayer) with
  `backend/siddur_lines.py` and writes `data/siddur/edot-hamizrach/`.

Use when refreshing the siddur text or changing its table of contents.
