# Service architecture

**Status (2026-10-02):** a Flask + FastAPI hybrid on Vercel (one region, `iad1`). The AI path, the conversation routes, rate limiting, per-user and global cost gates, the circuit breakers and the CDN cache tiers are all implemented and covered by tests (about 4,700 pytest tests at 99.8% backend coverage, an 85% floor enforced in CI). This document describes how requests flow, what each module owns, how async safety is enforced, and how the system behaves in a serverless environment. Route-by-route detail is in [API.md](API.md); the data model is in [DATABASE.md](DATABASE.md); configuration is in [ENVIRONMENT.md](ENVIRONMENT.md).

---

## 1. System overview

```text
Browser (SPA shell + ES modules + service worker)
        │ HTTPS
        ▼
Vercel (iad1)          vercel.json: functions api/index.py, maxDuration 90 s,
        │              two crons, /static/ Cache-Control
        ▼
api/index.py ── re-exports asgi.app
        │
        ▼
asgi.py  (FastAPI)
  middleware, outermost first:
    request_id_middleware   body cap (256 KiB), x-request-id, security headers,
                            Cache-Control tier, request_complete log line
    RateLimitMiddleware     one limiter for native AND Flask routes
  native routes:
    POST /ask               the async answer pipeline (section 4)
    GET  /api/async/health
  mount "/"  ──► WSGIMiddleware(app.py Flask)
                   15 blueprints (section 3), the SPA shell, pages, 404
        │
        ├── Supabase (Postgres + RLS)   user data, history, conversations, usage log
        ├── Upstash Redis               rate-limit counters, shared caches
        ├── Gemini (primary) / Claude (fallback)
        ├── Sefaria, Hebcal, Nominatim, Wikipedia/Halachipedia, MyMemory/Google Translate
        ├── Clerk (JWT verification, JWKS; Backend API for account deletion)
        └── Cloudflare Turnstile (siteverify), Sentry
```

Everything that is not `POST /ask` or `GET /api/async/health` is a Flask route reached through the WSGI mount, run on a worker thread. `app.py` also still defines a synchronous `POST /ask`, but in production the native FastAPI route matches first, so it is unreachable there; `tests/test_ask_transport_parity.py` pins the two to the same payload shape so they cannot drift.

---

## 2. Process-level modules

### `asgi.py`

The ASGI entry point and the only place the two stacks meet.

- `request_id_middleware`: rejects bodies over 256 KiB by `Content-Length` before any parsing (Flask's own `MAX_CONTENT_LENGTH` does not cover native routes), binds a `request_id` (an inbound `X-Request-Id` is kept, otherwise one is generated and written back into the ASGI headers so the Flask layer reports the same id), adds the security headers and a `Cache-Control` tier to native responses that lack them, and logs `request_complete` with the duration.
- `RateLimitMiddleware` (`backend/rate_limit.py`): registered first so the id middleware ends up outermost; it rejects with 429 before dispatch to either FastAPI or Flask.
- The `/ask` handler and its helpers (section 4), `GET /api/async/health`, and `fastapi_app.mount("/", WSGIMiddleware(flask_app_module.app))`.
- `import backend.logging_setup` (which runs `sentry_sdk.init()`) must stay ahead of the `FastAPI(...)` call, or Sentry silently stops instrumenting.

### `app.py`

The Flask application: configuration, the request hooks, the SPA shell and a few pages (`/`, `/settings`, `/profile`, `/terms`, `/privacy`, `/accessibility`, `/manifest.webmanifest`, `/favicon.ico`, `/service-worker.js`), the 404 handler, shared helpers that blueprints import (`get_engine`, the Supabase client factories, the ask-time retrieval helpers), and the loop that registers the blueprints. A blueprint that fails to import aborts startup with the module name instead of serving 404s for an unknown subset of routes. In-process counters (`DEVTOOLS_STATS`) live here.

Hooks: `before_request` binds the request id; `after_request` logs completion and applies the security headers and the cache tier. New routes go in a `backend/routes_*.py` blueprint, not `app.py`.

### `api/index.py`

Vercel requires the function entry point under `api/`; this file only re-exports `asgi.app`.

---

## 3. Blueprints (`backend/routes_*.py`)

| Blueprint | Owns |
|---|---|
| `routes_library` | `/api/library/*` (index, search, categories, popular, leaf refs), `/api/texts-index`, `/api/text/*` (text, links, graph), `/api/sidebar/*` (the commentary bundle), `/api/search/suggest`, `/api/word/meaning`, `/api/export/chapter`, `/api/diagnostics/sefaria` |
| `routes_prayers` | `/api/prayers/list`, `/api/prayer/*`, and the first-generation siddur reads (`/api/siddur/full/*`, `/api/siddur/section-refs/*`) |
| `routes_siddur` | `/api/siddur/v2/*` (contents, service and day endpoints over the checked-in siddur) |
| `routes_community` | `/api/communities*`, `/api/community/*` (read from `customs/*.json`) |
| `routes_calendar` | `/api/zmanim*`, `/api/holidays`, `/api/parasha`, `/api/daily-study`, `/api/geocode`, `/set_location` |
| `routes_user` | `/api/auth/me`, `/api/user/preferences` (and its `/api/preferences` alias), ask history (`/api/user/history*`), semantic bookmarks, `/api/accept-legal` |
| `routes_conversations` | `/api/conversations/*`, including the multi-turn `/ask` |
| `routes_answer_share` | Sharing a stored answer (`/api/user/history/<id>/share`) and reading a shared one (`/api/public/answer/<token>`) |
| `routes_spa_paths` | Path deep links (`/text/…`, `/prayer/…`, `/community/…`, `/siddur/…`, `/calendar/…`, `/answer/…`, `/a/…`, `/chat/…`, `/history`, `/signin`) served as the SPA shell, with the 308 redirects and `noindex` rules |
| `routes_devtools` | `/api/stack/health`, `/api/health`, `/api/devtools/*` (heartbeat, reliability, RLS audit, feedback digest, segment report), client-error intake, and the `budget-check` cron target |
| `routes_legal`, `routes_pages` | `/ai-disclosure`, `/acceptable-use`, `/dmca`, `/licenses`; `/about`, `/help`, `/glossary`, `robots.txt`, `sitemap.xml`, `llms.txt` |
| `routes_privacy` | `/api/user/data-export`, `/api/user/delete-account`, and the `retention-enforce` cron target |
| `routes_feedback` | Answer feedback |
| `routes_webhooks` | Clerk webhooks (signature-verified) |

`docs/API.md` lists every route with its auth label and rate-limit class.

---

## 4. The `/ask` pipeline

`POST /ask` in `asgi.py` runs these stages in order. A client that sends `Accept: application/x-ndjson` gets one progress line per step and then the result (the HTTP status is still real: a refusal is an ordinary 4xx; once streaming starts, a failure is an in-stream `error` line).

```text
 0. Rate limit        RateLimitMiddleware (llm class, fail-closed)       → 429
 1. Sanitize          claude.sanitize_user_query                          → 400
 2. Identity          Clerk bearer → user id (CLERK_ENFORCE_AUTH)        → 401
 3. Turnstile         anonymous callers only, once past the hourly
                      threshold and only when TURNSTILE_ENABLED          → 403 turnstile_required
 4. Budget            per-user daily cap, reserved atomically            → 402
 5. Prayer shortcut   a question naming Shacharit/Mincha/Maariv/Kiddush/
                      Havdalah returns a static pointer to the siddur, no model call
 6. Retrieval         six concurrent stages, each under its own ceiling
                      (primary sources 15 s, Halachipedia 6, wiki 3, Supabase
                      community knowledge 5, user memory 3, tool context 3); a stage
                      that times out contributes nothing and the answer proceeds
 7. Strict guard      mode "strict" with no primary source → strict_blocked answer, no model call
 8. Cost breaker      global daily breaker tripped → cached answer (marked) or
                      the "AI answers are paused" payload, no model call
 9. Synthesis         claude.ask_ai_async, or the agentic tool loop when
                      AI_AGENTIC_TOOLS=true (backend/ask_pipeline.run_agentic_ask),
                      under AI_TOTAL_BUDGET_SECONDS (45 s)
10. Persist           user-memory summary, ask_history row (with safety class and
                      PROMPT_VERSION), cost row
11. Fallback          any synthesis failure → halakhic source discovery
                      (get_halakhic_sources), returned as a source-only answer
```

Notes:

- **Providers.** Gemini is primary (`gemini-3.5-flash-lite`, overridable with `GEMINI_MODEL`) through the SDK's async client; Claude Haiku 5.5 is the fallback through async `httpx`. Both providers receive the thread's earlier turns as real conversation turns (not pasted into the prompt), and the Claude request puts the fixed system prompt in a cached block ahead of the per-request context so repeat asks read it from the prompt cache. Every call is gated by that provider's circuit breaker (`backend/health_check.py`): 3 consecutive failures open it, it half-opens after 120 s, and an open circuit skips straight to the next provider. A `security_blocked` result is returned as is, never retried on the other provider.
- **Timeouts.** One request ceiling of 30 s per model call, clamped to the budget that remains (a contextvar deadline); Gemini rejects deadlines under 10 s, so it is not started with less. The 45 s synthesis budget sits under Vercel's 90 s `maxDuration` so the fallback in stage 11 always gets to run.
- **Prompt and answer shape.** The model returns structured JSON (`ruling`, `sources`, `summary`, `practical_steps`, …) and tags each claim with a source number right after the sentence it backs; `backend/citation_markers.py` renumbers the markers against the cleaned source list, drops any that point at nothing, and shows a source once for a run of consecutive sentences that rest on it. A question that asks to learn a whole passage or topic is detected (`question_profile`) and given a larger output budget and a sectioned answer; an answer cut off by the token limit is trimmed back to its last complete sentence instead of being shown half-finished. Retrieved third-party text passes `backend/retrieval_guard.py` before it reaches the prompt (a snippet carrying injection phrases is dropped whole). `claude.PROMPT_VERSION` is stored with each saved answer so the governing prompt is reconstructable.
- **The conversation ask** (`POST /api/conversations/<id>/ask`, in `routes_conversations.py`) runs the same retrieval and synthesis through `app.py`'s sync helpers on a worker thread, adds the thread's prior turns to the prompt, enforces the minhag lock from the conversation row (never the request body), and applies the same two cost gates through `backend/cost_gates.py`. It carries the `llm` rate-limit class through a path pattern.
- **Streaming and cancellation.** A streamed answer is held by a strong reference in `_ASK_STREAM_TASKS`, so a client that disconnects mid-stream does not cancel the history and cost writes that follow.

---

## 5. Supporting modules

| Module | Role |
|---|---|
| `backend/auth.py` | Clerk JWT verification (JWKS cached per process; issuer, optional audience, expiry), `require_clerk_auth` (unconditional) and `maybe_require_clerk_auth` (honors `CLERK_ENFORCE_AUTH`, which defaults on in production) |
| `backend/rate_limit.py` | Fixed-window limiter in Redis with an in-process fallback; route classes `llm`, `heavy`, `fanout`, `sidebar`, `feedback`, `telemetry`, `cheap`, `account`, `webhook`; keyed by Clerk id for `llm`, else client IP; `llm` fails closed, the rest fail open; `/static/` exempt |
| `backend/turnstile.py` | Cloudflare Turnstile gate for anonymous `/ask` |
| `backend/cost_meter.py`, `backend/cost_gates.py` | Per-call cost recording to `ai_usage_log`, the atomic per-user daily reservation (`PER_USER_DAILY_BUDGET_USD`), the global daily budget and breaker (`DAILY_BUDGET_USD`), and the sync/async gate bridge |
| `backend/claude.py` | System prompts, prompt assembly, the Gemini then Claude call ladder, structured-output parsing, input sanitizing and the safety classifiers |
| `backend/ask_pipeline.py`, `backend/ai_tools.py` | The agentic tool-use loop and its tool registry (22 tools); off by default, see [AI_TOOLS.md](AI_TOOLS.md) |
| `backend/rag.py` | Ask-time context assembly: community-knowledge retrieval and scoring from the Supabase `community_knowledge` table, user-memory fetch and store, the ask-history writer, answer prefixes |
| `backend/data_service.py` | `ShelahEngine`, a thin facade over zmanim, daily learning, Halachipedia and wiki summaries and library text for the Flask routes |
| `backend/sefaria.py`, `backend/sefaria_library.py` | Topic-to-reference table; the library, text, links and search client with bounded caches |
| `backend/sidebar_bundle.py` | The commentary sidebar's two-stage, server-cached bundle (slim links, then preloaded text) |
| `backend/search.py`, `backend/utils/search_provider.py` | Wikipedia, Halachipedia and Hebcal connectors; corpus matching and the source-discovery fallback; translation (MyMemory, Google) |
| `backend/customs.py` | Loads and fuzzy-matches the community customs under `customs/` (13 community files, `schema.json`, and the aggregate `customs_db.json`). The JSON files seed the `community_knowledge` table (`scripts/migrate_customs_to_supabase.py`), back `/api/community/*`, and serve the agentic `search_community_customs` tool; the live `/ask` retrieval reads `community_knowledge`, not the files |
| `backend/zmanim_engine.py`, `backend/calendar_service.py` | Zmanim and the Hebrew calendar (pyluach first, Hebcal cross-checks); month events for FullCalendar |
| `backend/siddur_data.py`, `siddur_day.py`, `siddur_lines.py` | The checked-in siddur (`data/siddur/`), what changes on a Hebrew day, and typed lines |
| `backend/cache.py`, `backend/cache_policy.py` | A bounded TTL/LRU cache with an optional Redis tier; the single source of truth for `Cache-Control` tiers |
| `backend/page_meta.py`, `backend/module_versions.py` | Per-URL `<head>` values for the SPA shell; content-hashed ES module URLs and the import map |
| `backend/health_check.py` | Circuit breakers for Sefaria, Hebcal, Gemini and Claude (per process); state shown by `/api/devtools/reliability` |
| `backend/logging_setup.py` | JSON logging, request/user/client contextvars, Sentry init and scrubbing, `log_mitigation` |
| `backend/ref_aliases.py`, `backend/citation_markers.py`, `backend/ask_payloads.py`, `backend/ask_progress.py` | Mishneh Torah spelling aliases; numbered source markers; the response-body builders shared by both ask transports; live progress events |

---

## 6. Async safety rules

The FastAPI side runs on one event loop. These rules are mandatory (`.agents/ENGINEERING_RULES.md`):

1. **No blocking I/O on the event loop.** `requests`, file reads, Supabase's sync client and CPU-heavy work go through `asyncio.to_thread()`; outbound HTTP on the async path uses `httpx.AsyncClient`.
2. **Flask routes are synchronous** and run on the WSGI thread pool, where blocking I/O is fine. Do not call `asyncio.run()` inside a Flask handler except through the documented bridge in `claude.py` and `cost_gates.py`, which handle both the no-loop and running-loop cases.
3. **`request_id` and the identity fields are contextvars**, copied into tasks and threads at creation, so binding them in the middleware reaches everything the request spawns.
4. **One Redis client per event loop** for the limiter, because an async client cannot be shared across loops.
5. **No warmers and no polling** of any route by the client for ordinary visitors (a billing rule; see [VERCEL_COST_OPTIMIZATION.md](VERCEL_COST_OPTIMIZATION.md)).

---

## 7. Serverless behavior

- **Per-instance state.** Process-local and not shared across instances: `DEVTOOLS_STATS`, the circuit-breaker state, the in-process rate-limit fallback (used only when `RATE_LIMIT_REDIS_URL` is unset), and each `TTLCache`'s memory tier. Anything that must be seen by every instance lives in Supabase or Redis.
- **Cold starts.** Import time loads the blueprints and sets up logging; the Clerk JWKS cache is empty until the first authenticated request. Keep import-time work free of network calls; customs validation at startup is off in the production runtime.
- **Time limits.** `maxDuration` is 90 s. Upstream reads (Sefaria, Hebcal, Wikipedia and the rest) use short per-call timeouts, mostly 5 to 12 s, and the retrieval stages in section 4 have their own ceilings; the model budget is in section 4.
- **Scheduled work.** Two crons in `vercel.json`: `budget-check` (13:00 UTC) and `retention-enforce` (14:00 UTC), both bearer-authenticated with `CRON_SECRET`.
- **CDN.** `cache_policy.py` classifies every response into an immutable, date-deterministic, corpus-derived or private tier; a response that depends on a session (a zmanim call that falls back to the session location) is forced private for that request. Details in [VERCEL_COST_OPTIMIZATION.md](VERCEL_COST_OPTIMIZATION.md).
- **Security layers in front.** Vercel WAF, then the rate limiter, Turnstile and the cost gates; see [SECURITY.md](SECURITY.md).
