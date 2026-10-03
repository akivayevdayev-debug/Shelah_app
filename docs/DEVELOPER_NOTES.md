# Sh'elah developer notes

A practical map for someone about to change the code: what runs where, which file owns what, how to run and test it, and where to look first when something breaks. It is deliberately short on detail that lives better elsewhere; the deep references are linked below.

| You want | Read |
|---|---|
| Every HTTP route, request and response shape | [`API.md`](API.md) |
| How `/ask` works, stage by stage, and the process layout | [`SERVICE_ARCHITECTURE.md`](SERVICE_ARCHITECTURE.md) |
| The browser side: modules, bridges, state, routing, theming, service worker | [`FRONTEND.md`](FRONTEND.md) |
| Tables, columns, RLS | [`DATABASE.md`](DATABASE.md) |
| Environment variables | [`ENVIRONMENT.md`](ENVIRONMENT.md) |
| The AI tool registry | [`AI_TOOLS.md`](AI_TOOLS.md) |
| Security model, rate limits, headers, open items | [`SECURITY.md`](SECURITY.md) |
| Operations: incidents, alerts, monitoring | [`RUNBOOKS.md`](RUNBOOKS.md), [`OBSERVABILITY.md`](OBSERVABILITY.md) |
| House rules for UI, motion, accessibility, backend | [`../.agents/ENGINEERING_RULES.md`](../.agents/ENGINEERING_RULES.md) |
| Why a past choice was made | [`../DECISIONS.md`](../DECISIONS.md) |

## 1. Runtime flow

1. A browser requests `/` (or any deep-link path such as `/library/...`, `/siddur/...`, `/conversations/...`). Flask renders `templates/index.html`; `backend/page_meta.py` fills the per-URL `<head>` values and `backend/routes_spa_paths.py` serves the path deep links.
2. The page is a single-page app with no bundler. A classic inline script in `templates/index.html` owns most of the UI; ES modules under `static/js/` own the newer surfaces and meet the inline script through `window.Shelah*` bridges (see [`FRONTEND.md`](FRONTEND.md)).
3. The page calls `/api/*` for texts, the library, prayers and the siddur, zmanim, holidays, the parasha, communities, bookmarks, history and conversations, and `POST /ask` for AI answers.
4. In production the whole app is one Vercel function (`api/index.py` re-exports `asgi.app`). FastAPI serves only `POST /ask` and `GET /api/async/health` natively; every other route is Flask, mounted through `WSGIMiddleware`. Request handling therefore has two worlds:
   - **Async** (`asgi.py`, `backend/ask_pipeline.py`, `backend/claude.py`'s async entry points): must never block the event loop. Use `asyncio.to_thread` or async `httpx`.
   - **Sync** (`app.py` and the `backend/routes_*.py` blueprints): ordinary Flask handlers, run in WSGI worker threads.
5. External services: Sefaria (texts, links, calendars, the library index), Hebcal (Hebrew dates, candle lighting), Wikipedia and Halachipedia (retrieval), Gemini and Claude (answers), Supabase (user data, community knowledge, history, conversations, budgets), Clerk (identity), Cloudflare Turnstile (anonymous `/ask`), Upstash Redis (rate limits), Sentry (errors, relayed to Discord through `sentry-discord-relay/`).

## 2. Backend map

Entry points:

| File | Owns |
|---|---|
| `asgi.py` | The deployed ASGI app: native `/ask`, request-id and cache-policy middleware, `RateLimitMiddleware`, the Flask mount |
| `app.py` | The Flask app: configuration, security headers, blueprint registration, the Sefaria/zmanim helpers the blueprints import, and a sync `/ask` that is not reachable in production but is kept at parity (`tests/test_ask_transport_parity.py`) |
| `api/index.py` | The Vercel entry point |

Request-path modules (all under `backend/`):

| Area | Modules |
|---|---|
| Answering | `ask_pipeline` (agentic loop), `ask_payloads` (response builders shared by both transports), `ask_progress` (NDJSON progress events), `claude` (prompts, providers, circuit breakers), `ai_tools` (the 22-tool registry), `citation_markers` (`[n]` source markers), `retrieval_guard` (prompt-injection screen for retrieved text), `rag` (Supabase community knowledge, user memory, identity context) |
| Cost and abuse | `cost_meter` (price table, spend ledger), `cost_gates` (the same gates for sync routes), `rate_limit` (middleware and the route classes), `turnstile`, `cache` (small TTL and Redis helpers) |
| Sources and text | `sefaria_library` (library, text, links), `sefaria` (topic-to-reference table), `search` (Wikipedia, Halachipedia, Hebcal), `ref_aliases`, `utils/search_provider` (retrieval and corpus matching), `utils/text_engine` (normalisation and answer formatting), `customs` (community minhag JSON), `data_service` (`ShelahEngine` facade) |
| Calendar | `calendar_service` (pyluach-first dates), `zmanim_engine` (zmanim, omer, month events) |
| Siddur | `siddur_data` (reads the checked-in `data/siddur/`), `siddur_lines` (typed lines), `siddur_day` (what changes on a given Hebrew day), `sidebar_bundle` (commentary-sidebar preload) |
| Platform | `auth` (Clerk JWT checks), `cache_policy` (CDN tiers), `helpers` (shared constants and word-meaning utilities), `health_check` (provider circuit breaker), `logging_setup` (JSON logs, request id, PII scrubbing), `module_versions` (content-hashed module URLs), `page_meta` |

Blueprints (registered from `_BLUEPRINTS` in `app.py`; a failed registration is logged as critical):
`routes_library`, `routes_prayers`, `routes_siddur`, `routes_calendar`, `routes_community`, `routes_user`, `routes_conversations`, `routes_answer_share`, `routes_spa_paths`, `routes_devtools`, `routes_legal`, `routes_privacy`, `routes_pages`, `routes_feedback`, `routes_webhooks`. New routes go in a blueprint under `backend/`, not in `app.py`.

Rules that bite:

- **No blocking I/O on the event loop.** It is a correctness rule and a billing rule (see [`VERCEL_COST_OPTIMIZATION.md`](VERCEL_COST_OPTIMIZATION.md)).
- **Every new route needs a rate-limit class and a cache tier.** Unclassified routes default to the strictest treatment, so a missing entry shows up as a `no-store` header or a surprising 429 rather than a leak.
- **Cached routes must be a pure function of the URL.** A route that falls back to the session (for example zmanim without `lat`/`lon`) sets `g.cache_tier_force_private`.
- **Degrade, don't fail.** Retrieval stages have ceilings and an answer is still produced when one is slow; a provider failure falls back to the other provider, then to a source-only answer.
- **Supabase is optional for readers.** Anonymous reading must keep working when a table is missing or Supabase is unreachable.

## 3. Frontend map

`templates/index.html` is the shell and the classic script (about 14,500 lines); `static/js/` holds the 31 ES modules; `static/css/` holds 16 sheets plus the legacy `static/style.css`; `static/service-worker.js` is the offline layer. Everything about how they connect (import map and content-hashed URLs, bridges, state shape, the routing grammar, theme tokens and motion rules, the service worker's per-route strategies, how to add a module) is in [`FRONTEND.md`](FRONTEND.md). Per-directory notes: [`../templates/DEVELOPER_NOTES.md`](../templates/DEVELOPER_NOTES.md), [`../static/DEVELOPER_NOTES.md`](../static/DEVELOPER_NOTES.md).

## 4. Data and content

- `customs/*.json`: thirteen community datasets plus `schema.json` and the aggregate `customs_db.json`. See [`../customs/DEVELOPER_NOTES.md`](../customs/DEVELOPER_NOTES.md).
- `data/siddur/edot-hamizrach/`: the checked-in siddur, built by `scripts/build_siddur.py` from the public Sefaria-Export bucket; only CC0 and Public Domain versions are accepted.
- `static/data/glossary.json`: the glossary, generated by `scripts/generate_glossary_json.py`.
- `reports/`: the library leaf report read at runtime by `backend/sefaria_library.py`.
- `scripts/sql/`: the Supabase schema, migrations and RLS policies, applied by hand in the SQL Editor (not through the CLI; the pooler times out). See [`DATABASE.md`](DATABASE.md).

## 5. Running it locally

Use the project virtualenv (`.venv`); the local Python version is pinned in `.python-version` and CI's main job uses the same one.

| Task | Command |
|---|---|
| Full app including the async `/ask` | `.venv/bin/uvicorn asgi:app --port 5002` (`asgi-dev` in `.claude/launch.json`) |
| Flask only (no native `/ask`) | `.venv/bin/python app.py`, port 5001 (`flask-dev`) |
| Python tests | `.venv/bin/python -m pytest -q -p no:cacheprovider` |
| JS tests | `npm test` |
| JS coverage (as CI runs it) | `npm run test:coverage` |
| Lint | `.venv/bin/ruff check .` |
| Rebuild Tailwind | `npm run build:css` |
| Accessibility scan, both themes | `npm run test:a11y` |
| Stack health, live services | `.venv/bin/python scripts/verify_integrations.py` |

Notes:

- Use `asgi-dev` when you touch `/ask`. The Flask-only server answers `/ask` through the sync copy in `app.py`, which has no per-stage ceilings, no progress stream and no `RateLimitMiddleware` (that is registered once, in `asgi.py`).
- `uvicorn --reload` can miss edits to `templates/index.html`. When a template change does not appear, restart the server fully.
- A local `/ask` returns 429 when Redis is not configured, because the `llm` rate-limit class fails closed. Set `RATE_LIMIT_REDIS_URL` (see `.env.example`) or test through the unit suite.
- `pytest.ini` enforces a coverage floor of 85% on `backend/` and `asyncio_mode = auto`. Raise the floor as coverage grows; do not lower it.
- Dependencies are hash-locked: `requirements.txt` and `requirements-dev.txt` are the inputs, `requirements.lock.txt` and `requirements-dev.lock.txt` are what CI installs. Regenerate the lock with `uv` after changing an input.
- Leave `SENTRY_DSN` unset locally: with it set, local errors are reported to the same Sentry project (and Discord relay) as production.

## 6. Scripts

[`../scripts/README.md`](../scripts/README.md) classifies them by how often you run them; [`../scripts/DEVELOPER_NOTES.md`](../scripts/DEVELOPER_NOTES.md) says what each does. In short: `verify_*` scripts are live acceptance checks, `migrate_customs_to_supabase.py` and `build_siddur.py` rebuild data, `generate_database_doc.py` and `generate_glossary_json.py` generate documents and data, and `a11y_dark_scan.js` and `merge_lcov.py` are CI helpers.

## 7. CI and hooks

- `.github/workflows/ci.yml`: ruff (non-blocking), the pre-commit security scan over all files (bandit and gitleaks, blocking), `pip-audit` (non-blocking), pytest with the coverage floor, Node tests with coverage, the dual-theme accessibility scan, and SonarCloud.
- `.github/workflows/rls-verify.yml`: live Row Level Security acceptance check against the real project. Manual only (`workflow_dispatch`): the weekly schedule was disabled on 2026-09-15 because the test users' Clerk session ids expire, which fails the run without any RLS regression. Run it by hand, with fresh session ids, after touching auth or RLS configuration.
- `.pre-commit-config.yaml`: bandit and gitleaks block the commit; a documentation-sync check prints an advisory report and never blocks.
- Vercel skips builds for commits that touch only documentation (`ignoreCommand` in `vercel.json`).

## 8. Where to look when something is wrong

| Symptom | First stop |
|---|---|
| An answer is slow, empty or source-only | The `/ask` stage table in [`SERVICE_ARCHITECTURE.md`](SERVICE_ARCHITECTURE.md); the structured logs carry the request id, and a retrieval stage that hits its ceiling logs that it is "answering without it"; check the circuit breakers in `backend/health_check.py` |
| 402 or 429 from `/ask` | Per-user budget (`cost_meter`, `check_and_reserve_user_budget.sql`) or rate limit class (`rate_limit.py`) |
| 403 `turnstile_required` | Anonymous caller without a valid Turnstile token (`backend/turnstile.py`) |
| A page shows stale content after a deploy | Module URL hashes (`backend/module_versions.py`), the service worker version (`?swv=`), and the cache tier of the route |
| A route returns `no-store` unexpectedly | `backend/cache_policy.py`: unclassified routes and any status of 400 or above are private |
| Supabase writes silently fail | RLS and column names in [`DATABASE.md`](DATABASE.md); a 42501 on `anon` reads is deliberate (no SELECT for `anon`) |
| Deep-link path renders the wrong view | `backend/routes_spa_paths.py` and `static/js/router.js` must agree on the path grammar |
| Production health | `/api/async/health` is the cheap public check; `/api/health` and `/api/stack/health` are sign-in gated |

## 9. Maintainer notes

- Keep route responses backward compatible with what the deployed frontend and the service worker expect; clients can be several deploys old.
- Prefer graceful degradation when an upstream (Sefaria, Hebcal, Supabase, a model provider) fails.
- For typography, check pointed and unpointed Hebrew in the bilingual and interleaved layouts.
- When adding a Supabase table, handle its absence without breaking anonymous readers, enable RLS in the same migration, and add it to `_USER_DATA_TABLES` in `backend/routes_privacy.py` if it holds user data.
- Do not commit editor or sync-service conflict copies (names like `file 2.py`); compare, then delete the duplicate.
