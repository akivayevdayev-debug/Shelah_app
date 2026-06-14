# Service Architecture

This document describes the runtime architecture of the Sh'elah application: how requests flow, what each module owns, how async safety is enforced, and how the system behaves in a serverless environment.

---

## System Overview

```
┌─────────────────────────────────────────────────────────┐
│                        Browser                          │
└────────────────────────────┬────────────────────────────┘
                             │ HTTPS
                             ▼
┌─────────────────────────────────────────────────────────┐
│                    Vercel (serverless)                   │
│   vercel.json: catch-all rewrite → asgi.py              │
└────────────────────────────┬────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────┐
│                 asgi.py  (FastAPI app)                   │
│                                                         │
│  ┌──────────────────────────────────────────────────┐   │
│  │  Async /ask pipeline                             │   │
│  │  auth → rate-limit → RAG → AI → fallback ladder  │   │
│  └──────────────────────────────────────────────────┘   │
│                                                         │
│  ┌──────────────────────────────────────────────────┐   │
│  │  WSGIMiddleware → Flask app (app.py)             │   │
│  │  48 routes: HTML, /api/library, /api/calendar,   │   │
│  │  /api/community, /api/prayers, /api/user,        │   │
│  │  /api/devtools, /api/async/health                │   │
│  └──────────────────────────────────────────────────┘   │
└──────────┬───────────────────┬───────────────────────────┘
           │                   │
           ▼                   ▼
┌──────────────────┐  ┌────────────────────────────────────┐
│    Supabase      │  │         Upstream APIs               │
│  (PostgreSQL)    │  │  Sefaria API   Hebcal API           │
│                  │  │  Anthropic Claude                   │
│ user_memory      │  │  Google Gemini                      │
│ community_       │  │  MyMemory / Google Translate        │
│   knowledge      │  └────────────────────────────────────┘
│ ai_usage_log     │
│ bookmarks        │
│ preferences      │
└──────────────────┘
```

---

## Module Breakdown

### `app.py`

The main Flask application. Responsibilities:

- Registers all route blueprints from `backend/routes_*.py`
- Configures Flask-Limiter (rate limiting), CORS, session handling
- Runs `setup_logging()` at startup so all loggers inherit the JSON formatter
- Exposes the WSGI callable (`app`) consumed by `asgi.py` via `WSGIMiddleware`
- Contains legacy inline route handlers being migrated to blueprints
- Owns in-process module-level state: `DEVTOOLS_STATS` counters, rate-limiter storage, circuit-breaker instances

### `asgi.py`

The FastAPI ASGI entrypoint. Responsibilities:

- Creates a `fastapi_app` instance that Vercel routes all traffic to
- Mounts Flask at `/` via `asgiref.wsgi.WsgiToAsgi` (or `starlette.middleware.wsgi.WSGIMiddleware`)
- Owns the **async `/ask` pipeline** as a native FastAPI route — this allows true async I/O for the latency-sensitive AI path without blocking the event loop
- Exposes `GET /api/async/health` as a FastAPI-native health endpoint

### `backend/auth.py`

Clerk JWT verification. Fetches the JWKS from the Clerk issuer URL and verifies token signatures, expiry, audience, and issuer claims. Caches the JWKS to avoid redundant fetches. Returns a `UserContext` dataclass with `user_id`, `email`, and permission scopes.

### `backend/rag.py`

Retrieval-augmented generation context assembly. Takes a user question and assembles the full context payload for the AI prompt: Sefaria source texts, community customs, user memory fragments, wiki/Halachipedia entries, and the community lens. Returns a `RAGContext` object consumed by `claude.build_prompt()`.

### `backend/claude.py`

AI call layer. Responsibilities:

- Builds structured prompts via `build_prompt()`
- Calls Google Gemini (primary) with `asyncio.to_thread` / async httpx
- Falls back to Anthropic Claude on Gemini error or timeout
- Parses and validates the structured JSON response from the model
- Applies the "Scholarly Librarian" system prompt — defaults to providing sources for borderline halachic queries rather than refusing
- Exposes `validate_user_query()` which gates only hateful/illegal/empty inputs

### `backend/sefaria.py`

Sefaria REST API client. Maintains a `TOPIC_REFS` mapping of 100+ halachic topics to Sefaria reference strings. `find_refs_for_question()` does keyword matching to select relevant refs; `get_sources()` fetches them from the Sefaria API with a 10-second timeout.

### `backend/sefaria_library.py`

Sefaria library tree and text browsing. Powers the `/api/library/index` and `/api/library/text/<ref>` endpoints, fetching the library table of contents and individual texts.

### `backend/search.py`

Full-text search integration. Calls the Sefaria search API and normalizes results for the UI.

### `backend/calendar_service.py`

Jewish calendar service. Fetches parasha, holidays, Daf Yomi, and Mishna Yomit from Hebcal. Combines with `zmanim_engine.py` output to produce the daily calendar payload.

### `backend/zmanim_engine.py`

Halachic time calculation engine. Accepts latitude, longitude, and date; returns the full set of zmanim (Alos, sunrise, Sof Zman Krias Shema, Sof Zman Tefilla, Chatzos, Mincha Gedola, Mincha Ketana, Plag HaMincha, sunset/Shkia, Tzeis Hakochavim).

### `backend/customs.py`

Community customs loader. Reads from `customs/*.json` — 14 community datasets — and matches the user's community lens to the correct dataset. Used by RAG and the `/api/community/customs` endpoint.

### `backend/data_service.py`

`ShelahEngine` — the top-level orchestrator for the synchronous `/ask` path (called from Flask). Coordinates: query validation → Sefaria source collection → customs lookup → user memory fetch → RAG assembly → AI call → response serialization.

### `backend/logging_setup.py`

Structured JSON logging. `setup_logging()` installs a `JSONFormatter` on the root logger. Every log line is a JSON object. `get_logger(__name__)` returns a module-scoped logger; `bind_request_id()` sets the `request_id` context variable for the current request.

### `backend/health_check.py`

Circuit-breaker implementation for four external dependencies: Sefaria, Hebcal, Gemini, Claude. Opens after 3 consecutive failures; half-opens after 120 seconds. State is exposed via `/api/devtools/reliability`.

### `backend/cost_meter.py`

LLM cost metering. `record_llm_call()` is an async function that writes a row to the `ai_usage_log` Supabase table after every AI response.

### `backend/routes_*.py`

Blueprint modules that own specific API surface areas. Each file registers its routes with Flask and imports only the service modules it needs, keeping `app.py` from growing further.

---

## The `/ask` Pipeline

The async pipeline in `asgi.py` executes these steps in order for every `POST /ask` request:

```
1. Auth check
   └── backend/auth.py → verify Clerk JWT if Authorization header present
       → UserContext (anonymous if no token, when CLERK_ENFORCE_AUTH=false)

2. Rate limit
   └── Flask-Limiter (RATE_LIMIT_PER_MIN per IP/user)
       → 429 Too Many Requests if exceeded

3. Input validation
   └── backend/claude.py → validate_user_query()
       → 400 Bad Request for empty, hateful, or injection-pattern queries

4. Sefaria source collection
   └── backend/sefaria.py → find_refs_for_question() → get_sources()
       → list of {ref, text_he, text_en, url} dicts
       → asyncio.to_thread (blocking HTTP → thread pool)

5. RAG context assembly
   └── backend/rag.py → build RAGContext
       ├── Sefaria sources (step 4)
       ├── Community customs (backend/customs.py)
       ├── User memory fragments (Supabase user_memory)
       ├── Wiki / Halachipedia snippets
       └── Community lens string

6. AI synthesis
   └── backend/claude.py → build_prompt() → call Gemini (async httpx)
       → on error/timeout: fallback to Anthropic Claude (async httpx)
       → parse structured JSON response

7. Fallback ladder
   └── If both AI providers fail:
       → return cached similar answer (if available)
       → else return graceful degradation message with raw Sefaria sources

8. Response
   └── {answer, sources, customs, wiki, meta, confidence}
       → also calls backend/cost_meter.py → record_llm_call() (fire-and-forget)
```

---

## Async Safety Rules

Sh'elah runs on a single-threaded asyncio event loop (uvicorn). These rules are mandatory:

1. **No blocking I/O on the event loop.** All `requests` library calls, file reads, and CPU-bound work must go through `asyncio.to_thread()`.

2. **Shared `httpx.AsyncClient`.** A single `httpx.AsyncClient` instance is created at module level in `backend/claude.py` and reused across requests. Do not create per-request clients.

3. **Flask routes are synchronous** — they run in a thread pool via `WSGIMiddleware`. Inside Flask route handlers, regular blocking I/O is fine; do not mix `asyncio.run()` inside Flask handlers.

4. **FastAPI routes are async** — use `async def` and `await` throughout. Use `asyncio.to_thread()` for any call into synchronous library code (e.g., `pyluach`, Supabase SDK sync methods).

5. **Contextvars for request_id.** The `request_id` is stored in a `contextvars.ContextVar` and propagates automatically across `await` boundaries within one request. Do not pass it as a function argument.

---

## Serverless Considerations

Vercel runs `asgi.py` as a serverless function. Key implications:

### Per-instance state

The following are module-level (process-local) and **not shared across Vercel instances**:

- `DEVTOOLS_STATS` counters in `app.py` — instance-local only; use `/api/devtools/stats` for a single-instance snapshot
- Flask-Limiter in-memory storage — use `RATELIMIT_STORAGE_URI=redis://…` to share rate limit state across instances
- Circuit-breaker state in `backend/health_check.py` — instance-local; each instance maintains its own open/closed state

### Supabase as the persistence layer

All cross-instance state (user preferences, bookmarks, AI usage logs, community knowledge) lives in Supabase. Always use Supabase for anything that must survive a cold start or be visible to all instances.

### Cold starts

A cold start initializes the Flask app, loads all blueprint modules, and sets up logging. The JWKS cache for Clerk is empty on cold start and populated on the first authenticated request. Keep module-level initialization fast — no blocking network calls at import time.

### Timeouts

Vercel serverless functions have a maximum execution time (typically 10–30 seconds depending on plan). The `AI_MODEL_TIMEOUT_SECONDS` environment variable (default: 8s) ensures AI calls complete within budget. Sefaria calls use a 10-second timeout; Hebcal uses 5 seconds.
