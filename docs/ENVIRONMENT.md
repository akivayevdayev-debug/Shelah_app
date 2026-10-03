# Environment variables

Every variable the code actually reads, checked against the `os.environ.get(...)` / `os.getenv(...)` calls in `backend/`, `app.py`, and `asgi.py`. Start from [`.env.example`](../.env.example) for local dev, but don't ship its defaults to production (`CLERK_ENFORCE_AUTH=false` especially, and set `DAILY_BUDGET_USD` / `CRON_SECRET` explicitly).

## Required

No working default in code; the app misbehaves (or a whole feature silently no-ops) without these.

| Variable | Description |
|---|---|
| `FLASK_SECRET_KEY` | Random secret for session signing — generate with `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` | At least one AI provider — Gemini is checked first, Anthropic is the fallback |
| `CLERK_PUBLISHABLE_KEY` | Clerk Dashboard → API Keys (accepts `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` as a fallback name) |
| `CLERK_JWT_ISSUER` | Clerk Dashboard → API Keys → "Frontend API URL", e.g. `https://xxx.clerk.accounts.dev` |
| `SUPABASE_URL` | Supabase project URL, e.g. `https://xyz.supabase.co` |
| `SUPABASE_PUBLISHABLE_KEY` | Supabase publishable key (`sb_publishable_...`, safe to expose in browser) |
| `SUPABASE_SECRET_KEY` | Supabase secret key (`sb_secret_...`) — **never expose to client** |

## Optional

Every value below already has a working default in code. Set one only to override it.

| Variable | Default | Description |
|---|---|---|
| `FLASK_ENV` | `development` | `development` or `production` — also drives `CLERK_ENFORCE_AUTH`'s auto-default (below) and whether `VALIDATE_CUSTOMS_AT_STARTUP` runs by default |
| `FLASK_DEBUG` | `false` | Enables Flask's debug/reloader mode — only read in the `python3 app.py` direct-run path (`app.py`'s `__main__` block); has no effect under `uvicorn`/`gunicorn` |
| `PORT` | `5001` | Server port for plain Flask mode |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | Gemini model name override |
| `GOOGLE_API_KEY` | — | Fallback for `GEMINI_API_KEY` (checked second); once resolved, both are normalized to the same value so the Gemini SDK doesn't warn about a mismatch |
| `AI_AGENTIC_TOOLS` | `false` | `true` turns on the AI's tool-use loop (`backend/ask_pipeline.py::run_agentic_ask`, 22 tools in `backend/ai_tools.py`). Off, `/ask` answers from the up-front retrieval context only. Each tool round is another metered model call, so don't enable in production until `RATE_LIMIT_REDIS_URL` points at a shared store — see [AI_TOOLS.md](AI_TOOLS.md) |
| `AI_TOTAL_BUDGET_SECONDS` | `45` | Wall-clock budget (seconds) for a full `/ask` AI synthesis call, shared by the Flask and FastAPI transports — must stay under `vercel.json`'s `functions.maxDuration` (90s) so the platform never kills the request before the graceful-fallback path can run |
| `PER_USER_DAILY_BUDGET_USD` | `2.00` | Per-caller daily AI-spend ceiling, enforced atomically before every `/ask` model call (`backend/cost_meter.py::check_user_budget_and_enforce`) — set to `"0"` to disable |
| `DAILY_BUDGET_USD` | unset (disabled) | Global daily AI-spend guardrail/alert threshold — unset disables the check (and `/api/stack/health` then reports `security.cost_breaker.configured: false`). Production runs `10.00` |
| `RATE_LIMIT_REDIS_URL` | unset (in-process fallback) | Shared store for the unified rate-limit middleware (`backend/rate_limit.py`) — per-process only until pointed at a shared store (e.g. Upstash Redis over `rediss://`), which matters on Vercel Fluid where several instances run at once. Production uses Upstash Redis (confirmed 2026-08-26); a malformed URL falls back to the in-process store with a CRITICAL log instead of failing boot |
| `RATELIMIT_ENABLED` | `true` | Kill switch for the whole rate-limit middleware; read once at import time |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | — | Fallback name for `CLERK_PUBLISHABLE_KEY` |
| `CLERK_AUDIENCE` | — | Audience claim expected in Clerk JWTs — recommended for production; unset skips JWT audience verification entirely |
| `CLERK_ENFORCE_AUTH` | `true` when `VERCEL=1` or `FLASK_ENV=production`, else `false` | Explicitly force auth enforcement on protected `/api/*` routes, overriding the runtime-based auto-default. `.env.example` ships `false` for local development. On the production Vercel project it is set explicitly to `false` (checked 2026-10-03 with `vercel env run`), which overrides the `true` default there and lets anonymous callers use `/ask`; removing the variable, or setting it to `true`, turns enforcement on (`docs/SECURITY.md` §13) |
| `CLERK_SECRET_KEY` | — | Required for `/api/user/delete-account` to also delete the Clerk identity itself (not just Supabase rows) via Clerk's Backend API (`backend/routes_privacy.py`). Without it, account deletion still wipes Supabase data but reports `clerk_deleted=false` |
| `SEFARIA_API` / `SEFARIA_V3_API` | `https://www.sefaria.org.il/api` / `.../api/v3` | Sefaria API base URL overrides — mainly used to point at a mock endpoint in CI/tests |
| `VALIDATE_CUSTOMS_AT_STARTUP` | unset (off in production runtime, on elsewhere) | Validates the customs corpus at startup; skip in production to avoid billing cold-start CPU |
| `LOG_LEVEL` | `INFO` | Root log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `SENTRY_DSN` / `SENTRY_DSN_BROWSER` | — | Sentry error tracking (server / browser) — true no-op until set |
| `ERROR_LOG_WEBHOOK_URL` | — | Also POST backend error payloads to this URL |
| `CRON_SECRET` | — | Shared secret Vercel Cron sends as a Bearer token to `/api/devtools/budget-check` and `/api/devtools/retention-enforce` (see `vercel.json`); only needed on Vercel |
| `DEPLOY_HASH` | unset (`static/service-worker.js`'s own `CACHE_VERSION`) | Service-worker cache version base. Every Vercel deploy already gets fresh caches (the commit is appended); set this only to force a change between deploys of the same commit |
| `VIEW_TRANSITIONS` | `false` | `true` animates moves between views (home, a text, a prayer, a community, history) with the View Transitions API, for every visitor; browsers without the API just swap. One browser can opt in or out alone with `localStorage.setItem("shelah.viewTransitions", "on" \| "off")` |
| `SUPABASE_PREFS_TABLE`, `SUPABASE_COMMUNITY_KNOWLEDGE_TABLE`, `SUPABASE_USER_MEMORIES_TABLE`, `SUPABASE_STUDY_BOOKMARKS_TABLE`, `SUPABASE_ASK_HISTORY_TABLE` | `user_preferences`, `community_knowledge`, `user_memories`, `study_bookmarks`, `ask_history` | Supabase table-name overrides — only needed if your tables are named differently from the defaults |
| `VERCEL`, `VERCEL_ENV`, `VERCEL_GIT_COMMIT_SHA` | — | Set automatically by the Vercel platform (production-runtime detection, Sentry environment/release tagging) — do not set these manually |

**Never commit real values for any of the above** — `.env` is gitignored; use `.env.example`'s blank placeholders as the template.

**Not actually environment variables** (documented here to prevent confusion, since these names surface in tests and older commit messages):
- `STRICT_SUPABASE_RLS` is a hardcoded `True` literal in `app.py` — RLS enforcement is treated as a fixed security posture, not per-deployment config, so setting an environment variable of this name has no effect. The name only appears in test monkeypatches (`tests/test_routes_user.py`, `tests/test_rag.py`).
- `RATE_LIMIT_ASK` and `RATE_LIMIT_DEFAULT` belonged to the removed Flask-Limiter and no longer exist. Rate-limit policy is one code table, `_POLICIES` in `backend/rate_limit.py` (for example `/ask` is 20 per minute anonymous, 40 per minute signed in, 200 per day), deliberately not environment-configurable: a policy change is a code change requiring a redeploy. `/api/stack/health` reports the live policy. The only runtime switches are `RATELIMIT_ENABLED` and `RATE_LIMIT_REDIS_URL` above.

