# Sh'elah Developer Notes

> Sync status (2026-05-12): Updated to reflect May 2026 improvements — AI reliability fix (no-cache for fallback responses), local source links, quick-settings panel repositioning, Gemini model update, and service worker v8.

This guide is for coders who need a fast, practical map of what each part of the codebase does.
It is organized by runtime flow first, then file-by-file notes.

## 1) Runtime Flow (High Level)

1. Browser loads `/` -> Flask renders `templates/index.html` from `app.py`.
2. Frontend JS in `templates/index.html` manages UI state, layouts, and user actions.
3. Frontend calls Flask APIs (`/api/*`) for texts, communities, prayers, zmanim, holidays, etc.
4. Flask route handlers in `app.py` delegate to service modules in `backend/` (`backend/data_service.py`, `backend/sefaria_library.py`, `backend/zmanim_engine.py`, etc.).
5. External sources used by backend:
   - Sefaria API: texts, links, calendars.
   - Hebcal API: Hebrew date conversion, holiday/candle-lighting context.
   - Supabase: user preferences and optional todos.
   - Clerk: auth/session identity when enabled.

## 2) Calendar, Zmanim, Hebcal (Where Each Part Lives)

- `backend/calendar_service.py`
  - Pyluach-first date conversion and calendar helper engine.
  - Hebcal cross-checking for consistency.
  - Parasha/holiday support helpers.

- `backend/zmanim_engine.py`
  - Main daily zmanim computation logic.
  - Timezone resolution from lat/lon.
  - Omer/day logic and monthly event generation for FullCalendar.
  - Uses Hebcal for candle-lighting and calendar enrichment.

- `app.py`
  - Route entrypoints for calendar/time features:
    - `/api/zmanim`
    - `/api/zmanim/month`
    - `/api/holidays`
    - `/api/parasha`
  - Coordinates request params, fallback behavior, and response shape.

- `templates/index.html`
  - Frontend rendering for Today panel and calendar modal.
  - Trigger points for fetching zmanim/holiday/parasha payloads.

## 3) Backend File Notes

- `app.py`
  - Main Flask app and API contract.
  - Handles: auth wiring, Supabase preferences CRUD, text/prayer/community APIs, diagnostics, static assets.
  - Important route groups:
    - Health/devtools: `/api/stack/health`, `/api/devtools/*`
    - Reader data: `/api/library/*`, `/api/text/*`
    - Prayer/customs: `/api/prayers/*`, `/api/community/*`
    - Calendar/time: `/api/zmanim*`, `/api/holidays`, `/api/parasha`
  - Includes graceful fallback for missing optional Supabase `todos` table.

- `backend/data_service.py`
  - `ShelahEngine` service facade that keeps route handlers thin.
  - Aggregates calls to Sefaria, customs, wiki/halachipedia search, and zmanim engine.

- `backend/sefaria.py`
  - Curated `TOPIC_REFS` map + lookup helpers for matching user questions to canonical references.

- `backend/sefaria_library.py`
  - Structured Sefaria client wrapper:
    - library index
    - popular texts
    - text retrieval/flattening (Hebrew + English)
    - linked refs and graph-friendly data
  - Includes in-memory caching.

- `backend/customs.py`
  - Loads and normalizes community customs JSON files from `customs/`.
  - Performs exact/fuzzy matching for minhag responses.

- `backend/search.py`
  - External enrichment connectors:
    - Wikipedia summary fetch
    - Halachipedia search/extract
    - Hebcal daily learning helper

- `backend/calendar_service.py`
  - Pyluach calendar conversion and validation support.

- `backend/zmanim_engine.py`
  - Zmanim computation and month-event generation.

- `backend/claude.py`
  - Prompt assembly and Anthropic call wrapper.
  - Formatting helpers for source/custom/wiki blocks.

## 4) Frontend File Notes

- `templates/index.html`
  - Single-page app shell (HTML + major JS controller logic).
  - Contains:
    - top navigation + search
    - left navigator and right Today panel
    - reader settings and reader renderer
    - prayer/community modals
    - API fetch/update logic and local UI state
  - Key state object: `appState` (reader layout, font size, sidebars, language, filters, etc.).

- `static/style.css`
  - Custom style layer over Tailwind utility classes.
  - Major sections:
    - Hebrew typography and font face (`Ezra SIL`)
    - reader layout behavior
    - side-by-side/interleaved spacing
    - responsive mobile drawer behavior
    - shul mode and scaling

- `static/service-worker.js`
  - Offline cache strategy for core assets.

- `static/offline.html`
  - Fallback page displayed when navigation fails offline.

## 5) Data and Content Files

- `customs/*.json`
  - Community-specific minhag datasets used by `backend/customs.py` and `/api/community/*`.

- `sefardic_prayers.json`
  - Prayer text source data used in prayer endpoints.

- `.agents/`
  - Agent/skill metadata for Copilot tooling; not core runtime logic for Flask APIs.

## 6) Utility Scripts

- `scripts/verify_integrations.py`
  - End-to-end service health checker (env, Supabase, Sefaria, Hebcal, local Flask, Vercel).

- `scripts/clerk_supabase_rls.py`
  - Clerk JWT verification and Supabase RLS helper utilities.

- `scripts/fetch_sefardic_siddur.py`
  - Fetches Siddur text from Sefaria and prepares prayer data payloads.

## 7) Config + Deployment

- `requirements.txt`
  - Python dependency lock input for backend runtime.

- `vercel.json`
  - Vercel deployment behavior and route/build settings.

- `README.md`
  - Project setup and usage guidance.

## 8) Quick Debug Entry Points

- Full integration check:
  - `python3 scripts/verify_integrations.py`

- Compile sanity check:
  - `python3 -m py_compile app.py backend/calendar_service.py backend/claude.py backend/customs.py backend/data_service.py backend/search.py backend/sefaria.py backend/sefaria_library.py backend/zmanim_engine.py`

- High-signal production checks:
  - `/api/stack/health`
  - `/api/library/popular`
  - `/api/text/Genesis 1`
  - `/api/zmanim`
  - `/api/parasha`

## 9) Notes for Future Maintainers

- Keep route responses backward-compatible with existing frontend JS expectations.
- Prefer graceful degradation when external services fail (Hebcal/Sefaria/Supabase).
- For typography changes, verify both pointed and unpointed Hebrew in bilingual and interleaved layouts.
- If adding Supabase tables, ensure missing-table behavior does not break anonymous readers.
