# Sh'elah — Jewish Learning & Halachic AI Assistant

*Educational information only — not a substitute for a rabbi's p'sak (a binding halachic ruling) or for legal advice. Always consult a qualified rabbi and/or attorney before relying on anything here for a practical decision.*

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.14](https://img.shields.io/badge/python-3.14-blue.svg)](.github/workflows/ci.yml)
[![Deployed on Vercel](https://img.shields.io/badge/deployed%20on-Vercel-black?logo=vercel&logoColor=white)](https://vercel.com)
[![CI](https://github.com/akivayevdayev-debug/Shelah_app/actions/workflows/ci.yml/badge.svg)](https://github.com/akivayevdayev-debug/Shelah_app/actions/workflows/ci.yml)
[![Quality gate status](https://sonarcloud.io/api/project_badges/measure?project=akivayevdayev-debug_Shelah_app&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=akivayevdayev-debug_Shelah_app)

**Sh'elah** (שאלה — "question") is a full-stack web application for Jewish text study, halachic inquiry, and daily practice. It integrates the Sefaria text library, community customs datasets, prayer resources, zmanim, and a multi-model AI layer (Google Gemini primary, Anthropic Claude fallback) to answer halachic questions with source citations in the user's community tradition.

---

## Table of Contents

1. [What is Sh'elah](#what-is-shelah)
2. [Screenshots & Demo](#screenshots--demo)
3. [Architecture Overview](#architecture-overview)
4. [AI & Safety](#ai--safety)
5. [Quick Start](#quick-start)
6. [Environment Variables](#environment-variables)
7. [Testing](#testing)
8. [Deployment](#deployment)
9. [Directory Structure](#directory-structure)
10. [Contributing](#contributing)
11. [Code of Conduct](#code-of-conduct)
12. [Security](#security)
13. [Credits & Licenses](#credits--licenses)
14. [Further Documentation](#further-documentation)

---

## What is Sh'elah 

Sh'elah is a Jewish learning, halacha, calendar, and prayers application. It helps users:

- Ask halachic questions and receive AI-synthesized answers with citations from Talmud, Rishonim, Acharonim, Shulchan Aruch, and responsa literature — optionally backed by an **agentic tool-use layer** (22 tools spanning texts, live zmanim/calendar computation, and last-resort web search, texts-first by design; opt-in, off by default — see `docs/AI_TOOLS.md`)
- Get **community-lens answers** — guidance aware of 14 tradition datasets (Sefardic, Ashkenaz, Yemenite, Moroccan, Persian, Syrian, Bukharian, Iraqi, Ethiopian, Georgian, Greek/Romaniote, Mountain Jewish, Turkish/Ottoman, and more)
- Browse the full Sefaria text library with **bilingual EN/HE rendering and RTL layout support**
- View halachic prayer times (zmanim) by GPS coordinates
- Follow the Jewish calendar — Parasha, holidays, Daf Yomi, Mishna Yomit from live Hebcal feed
- Browse prayer services (Shacharit, Mincha, Maariv) with community-specific nusach awareness
- Manage bookmarks and per-user preferences via Clerk authentication + Supabase storage
- Install as a **PWA and use core reading features offline**, and switch between **light/dark themes** with full WCAG 2.1 AA contrast support

Sh'elah provides **educational information only** — it is not a posek (halachic decisor). Users requiring binding halachic decisions should consult a qualified rabbi.

---

## Screenshots & Demo

<!-- TODO: capture and add a real screenshot of the Sefaria text reader view — the bilingual EN/HE reading pane in templates/index.html (static/js/reader-ui.js), ideally ~1280px wide, one light-theme and one dark-theme shot. Save as docs/images/screenshot-reader.png (or similar) and update the path below. -->
![Reader view — TODO: add screenshot](docs/images/screenshot-reader.png)

<!-- TODO: capture and add a real screenshot of an AI-synthesized halachic answer with its rendered source citations (the #aiSources panel / source boxes described in claude_code_prompts.md Prompt 5, static/js/ai-service.js). Show a real question, the ruling, and the source box list. Save as docs/images/screenshot-ai-answer.png and update the path below. -->
![AI answer with sources — TODO: add screenshot](docs/images/screenshot-ai-answer.png)

<!-- TODO: capture and add a real screenshot of the zmanim/calendar view — GPS-based halachic times plus the Hebcal-driven Jewish calendar (static/js/zmanim.js). Save as docs/images/screenshot-calendar.png and update the path below. -->
![Zmanim & calendar view — TODO: add screenshot](docs/images/screenshot-calendar.png)

**Live demo:** [shelah.org](https://shelah.org)

---

## Architecture Overview

Sh'elah is a **Flask + FastAPI ASGI hybrid** deployed on Vercel as a serverless function, backed by Supabase (Postgres) for persistence, and consuming Sefaria, Hebcal, and AI APIs upstream.

```
Browser
  |
  v
Vercel (catch-all route → asgi.py)
  |
  v
asgi.py  (FastAPI ASGI app)
  |-- async /ask pipeline (auth → rate-limit → RAG → AI synthesis → response)
  |-- WSGIMiddleware → Flask app (app.py)
        |-- 48 routes: HTML pages, /api/* endpoints
        |-- backend/ modules for every service domain
              |
              |-- Supabase (user_memory, community_knowledge, ai_usage_log,
              |             bookmarks, preferences)
              |
              |-- Sefaria API  (texts, search, library tree)
              |-- Hebcal API   (calendar, zmanim, parasha)
              |-- Anthropic Claude  (AI fallback)
              |-- Google Gemini     (AI primary)
              |-- MyMemory / Google Translate  (translation layer)
```

See **[docs/SERVICE_ARCHITECTURE.md](docs/SERVICE_ARCHITECTURE.md)** for the full post-refactor module breakdown, request-flow details, and how the blueprints below wire together.

### Key components

| File / Module | Role |
|---|---|
| `app.py` | 5 000-line Flask app; owns 48 routes and all middleware setup |
| `asgi.py` | FastAPI ASGI wrapper; owns the async `/ask` pipeline; mounts Flask via `WSGIMiddleware` |
| `backend/auth.py` | Clerk JWT verification (JWKS-based) |
| `backend/rag.py` | Retrieval-augmented generation — assembles context for AI prompts |
| `backend/claude.py` | AI call layer — Gemini primary, Claude fallback, prompt templates, structured output |
| `backend/sefaria.py` | Sefaria REST API client, topic/keyword → reference mapping |
| `backend/sefaria_library.py` | Sefaria library tree + text browsing |
| `backend/search.py` | Full-text search integration |
| `backend/calendar_service.py` | Jewish calendar, Daf Yomi, zmanim via Hebcal |
| `backend/zmanim_engine.py` | Halachic time calculation engine |
| `backend/customs.py` | Community customs loader and matcher |
| `backend/logging_setup.py` | Structured JSON logging (`JSONFormatter` + `setup_logging`) |
| `backend/health_check.py` | Circuit-breaker health checks for external APIs |
| `backend/cost_meter.py` | LLM cost metering — records to `ai_usage_log` Supabase table |
| `backend/routes_*.py` | Blueprint modules for library, calendar, community, prayers, user, legal, devtools |

---

## AI & Safety

The `/ask` endpoint runs a **multi-model RAG pipeline**: **Google Gemini is the primary model**, with **Anthropic Claude as an automatic fallback** if Gemini errors or is unavailable (`backend/claude.py`). Context assembly (`backend/rag.py`) blends live Sefaria API results, the 14-community customs corpus, and per-user memory before either model sees the question.

**Source priority**, enforced on every answer by the system prompt (`backend/claude.py::CORE_SYSTEM_PROMPT`):

1. Direct, chapter-level Sefaria citations (Talmud, Tanakh, Shulchan Aruch, Mishneh Torah, major commentaries)
2. Broader keyword evidence from Sefaria, HebrewBooks, and Halachipedia
3. Acharonim and contemporary poskim (19th–21st century responsa)
4. The model's own internal halakhic knowledge — used only when 1–3 yield nothing, or clearly conflict

General web search (e.g. Wikipedia) is a documented **last resort** — used only when Judaic texts and computed calendar data can't answer a question at all, and is never the sole basis for a halachic ruling. See [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) for the Wikipedia CC-BY-SA attribution this triggers.

**Disclaimers & guardrails:**

- Every `/ask` response carries a persistent, non-dismissible disclaimer ("Please consult with your local Rabbi for a final ruling"), rendered in both the AI modal and reader view. Sh'elah is explicitly **educational only** — it never claims rabbinic authority or that its output is a binding ruling (`NO_IMPERSONATION_DIRECTIVE`, `backend/claude.py`).
- A `safety_class` classifier (`classify_safety()`, `backend/claude.py`) flags medical, mental-health/self-harm, abuse/minor-safety, and dangerous/illegal content for a distinct, more prominent referral variant of the disclaimer.
- **Minimum age: 13+ (16+ in the EU/UK)**, stated via a persistent site-footer notice rather than a blocking gate. See **[docs/AGE_AND_SAFETY_POLICY.md](docs/AGE_AND_SAFETY_POLICY.md)** for the full policy, including the age-appropriate output layer that keeps sensitive topics (e.g. niddah, intimacy) clinical and educational rather than explicit, without reducing scholarly depth or sourcing.
- Full AI usage disclosure — which models are used, how data is handled, and known limitations — is published at **[/ai-disclosure](templates/ai-disclosure.html)** (route registered in `backend/routes_legal.py`).

---

## Quick Start

### Prerequisites

- Python 3.14 (the version CI tests and locks dependencies against, pinned via `.python-version` — earlier 3.12+ interpreters are likely compatible but not verified here)
- A Clerk account (auth)
- A Supabase project (database)
- A Google AI / Gemini API key (primary AI)
- An Anthropic API key (fallback AI — optional but recommended)

### Installation

```bash
git clone <repo-url>
cd Sh\'elah_app

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate      # macOS/Linux
# .venv\Scripts\activate       # Windows

# Install dependencies
pip install -r requirements.txt
```

### Configuration

```bash
cp .env.example .env
# Open .env and fill in all required variables (see table below)
```

**Note:** `.env.example` is a convenient starting template for local development, not a production-ready config file as-is. In particular, its `CLERK_ENFORCE_AUTH=false` default is correct for local dev but must not ship unmodified to production (production runtime auto-defaults it to `true` regardless — see the table below), and its blank `DAILY_BUDGET_USD` / `CRON_SECRET` defaults should be set explicitly before a real deployment (plan.md §24.4).

### Run locally

```bash
# ASGI mode (recommended — matches Vercel runtime):
uvicorn asgi:fastapi_app --reload

# Plain Flask mode (simpler, no async /ask pipeline):
python3 app.py
```

Default URL: `http://127.0.0.1:8000` (uvicorn) or `http://127.0.0.1:5001` (Flask).

---

## Environment Variables

See [`.env.example`](.env.example) for a local-dev starting template — but read the caveat above before trusting its literal default *values* in production. The table below is reconciled directly against what the code actually reads (`os.environ.get(...)` / `os.getenv(...)` across `backend/`, `app.py`, and `asgi.py`), not copied from `.env.example` or from prose elsewhere.

### Required

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

### Optional

Every value below already has a working default in code. Set one only to override it.

| Variable | Default | Description |
|---|---|---|
| `FLASK_ENV` | `development` | `development` or `production` — also drives `CLERK_ENFORCE_AUTH`'s auto-default (below) and whether `VALIDATE_CUSTOMS_AT_STARTUP` runs by default |
| `FLASK_DEBUG` | `false` | Enables Flask's debug/reloader mode — only read in the `python3 app.py` direct-run path (`app.py`'s `__main__` block); has no effect under `uvicorn`/`gunicorn` |
| `PORT` | `5001` | Server port for plain Flask mode |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | Gemini model name override |
| `GOOGLE_API_KEY` | — | Fallback for `GEMINI_API_KEY` (checked second); once resolved, both are normalized to the same value so the Gemini SDK doesn't warn about a mismatch |
| `AI_TOTAL_BUDGET_SECONDS` | `45` | Wall-clock budget (seconds) for a full `/ask` AI synthesis call, shared by the Flask and FastAPI transports — must stay under `vercel.json`'s `functions.maxDuration` (90s) so the platform never kills the request before the graceful-fallback path can run |
| `PER_USER_DAILY_BUDGET_USD` | `2.00` | Per-caller daily AI-spend ceiling, enforced atomically before every `/ask` model call (`backend/cost_meter.py::check_user_budget_and_enforce`) — set to `"0"` to disable |
| `DAILY_BUDGET_USD` | unset (disabled) | Global daily AI-spend guardrail/alert threshold — unset disables the check entirely |
| `RATE_LIMIT_REDIS_URL` | unset (in-process fallback) | Shared store for the unified rate-limit middleware (`backend/rate_limit.py`) — per-process only until pointed at a shared store (e.g. Upstash Redis over `rediss://`); see plan.md §16.1 D3 for the known multi-instance limitation on Vercel Fluid |
| `RATELIMIT_ENABLED` | `true` | Kill switch for the whole rate-limit middleware; read once at import time |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | — | Fallback name for `CLERK_PUBLISHABLE_KEY` |
| `CLERK_AUDIENCE` | — | Audience claim expected in Clerk JWTs — recommended for production; unset skips JWT audience verification entirely |
| `CLERK_ENFORCE_AUTH` | `true` when `VERCEL=1` or `FLASK_ENV=production`, else `false` | Explicitly force auth enforcement on protected `/api/*` routes, overriding the runtime-based auto-default |
| `CLERK_SECRET_KEY` | — | Required for `/api/user/delete-account` to also delete the Clerk identity itself (not just Supabase rows) via Clerk's Backend API (`backend/routes_privacy.py`). Without it, account deletion still wipes Supabase data but reports `clerk_deleted=false` |
| `SEFARIA_API` / `SEFARIA_V3_API` | `https://www.sefaria.org.il/api` / `.../api/v3` | Sefaria API base URL overrides — mainly used to point at a mock endpoint in CI/tests |
| `VALIDATE_CUSTOMS_AT_STARTUP` | unset (off in production runtime, on elsewhere) | Validates the customs corpus at startup; skip in production to avoid billing cold-start CPU |
| `LOG_LEVEL` | `INFO` | Root log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `SENTRY_DSN` / `SENTRY_DSN_BROWSER` | — | Sentry error tracking (server / browser) — true no-op until set |
| `ERROR_LOG_WEBHOOK_URL` | — | Also POST backend error payloads to this URL |
| `CRON_SECRET` | — | Shared secret Vercel Cron sends as a Bearer token to `/api/devtools/budget-check` and `/api/devtools/retention-enforce` (see `vercel.json`); only needed on Vercel |
| `DEPLOY_HASH` | `v8` | Service-worker cache-busting version string (`static/service-worker.js`'s `CACHE_VERSION`) — bump to force clients to drop old caches on deploy |
| `SUPABASE_PREFS_TABLE`, `SUPABASE_COMMUNITY_KNOWLEDGE_TABLE`, `SUPABASE_USER_MEMORIES_TABLE`, `SUPABASE_STUDY_BOOKMARKS_TABLE`, `SUPABASE_ASK_HISTORY_TABLE` | `user_preferences`, `community_knowledge`, `user_memories`, `study_bookmarks`, `ask_history` | Supabase table-name overrides — only needed if your tables are named differently from the defaults |
| `VERCEL`, `VERCEL_ENV`, `VERCEL_GIT_COMMIT_SHA` | — | Set automatically by the Vercel platform (production-runtime detection, Sentry environment/release tagging) — do not set these manually |

**Never commit real values for any of the above** — `.env` is gitignored; use `.env.example`'s blank placeholders as the template.

**Not actually environment variables** (documented here to prevent confusion, since both names surface in `plan.md`/tests):
- `STRICT_SUPABASE_RLS` is a hardcoded `True` literal in `app.py` — RLS enforcement is treated as a fixed security posture, not per-deployment config, so setting an environment variable of this name has no effect. The name only appears in test monkeypatches (`tests/test_routes_user.py`, `tests/test_rag.py`).
- `RATE_LIMIT_ASK` (`"20 per minute"` on `/ask`) and `RATE_LIMIT_DEFAULT` (`"60 per minute"` blanket default) are also literals in `app.py`, deliberately not environment-configurable — rate-limit policy is treated as a code change requiring redeploy either way, not a runtime setting (see the comment at `app.py:762-768`).

---

## Testing

```bash
# Install dev dependencies
pip install -r requirements-dev.txt

# Run the full offline test suite (coverage-gated)
pytest
```

- **Fully offline.** `tests/conftest.py` sets mock credentials and disables auth enforcement — no live Sefaria, Hebcal, Clerk, Supabase, Gemini, or Anthropic access is needed to run the suite.
- **Coverage gate.** `pytest.ini` enforces `--cov-fail-under=85` against `backend/` (the unit-test target — `app.py` is a large Flask migration candidate, not the coverage target itself). Actual measured coverage runs well above the floor (~91% as of the last full backend-refactor pass); the threshold is meant to be ratcheted up as coverage grows, never lowered.
- **Golden-master discipline.** The project rule (see `plan.md` §6.2, "golden master before change") is: before moving or refactoring existing code, first write a characterization test pinning its *current* behavior, confirm it passes against the pre-change code, then make the change and require the same test to still pass — or be deliberately, visibly flipped when the change's whole point is to fix that exact behavior (e.g. `tests/test_cost_meter_pricing.py`'s pricing-gap regression test, or `plan.md` §16's rate-limit characterization test). This is how the multi-phase `app.py` → `backend/` module extraction (`backend/utils/text_engine.py`, `backend/utils/search_provider.py`, and others) shipped without behavioral regressions: every extracted function had a golden-master test asserting byte-identical output before it moved.
- **CI** (`.github/workflows/ci.yml`) runs the same suite with coverage reporting, plus `ruff` lint (non-blocking), `pre-commit run --all-files` (gitleaks + bandit secret/security scanning, blocking), `pip-audit` dependency vulnerability scanning against the hash-locked requirement files (non-blocking), and a `pa11y-ci`-based WCAG 2.1 AA accessibility scan (light + dark theme) against the legal/public pages.

---

## Deployment

### Vercel (production)

`vercel.json` configures a single catch-all route that sends every request to `asgi.py`. All environment variables must be set in the Vercel project dashboard (Settings → Environment Variables).

```bash
# Link project (first time)
vercel link

# Deploy preview
vercel deploy

# Deploy to production
vercel --prod
```

### Self-hosted ASGI

```bash
uvicorn asgi:fastapi_app --host 0.0.0.0 --port 8000 --workers 4
```

### Self-hosted WSGI

```bash
gunicorn app:app --bind 0.0.0.0:5001 --workers 4
```

---

## Directory Structure

```
.
├── app.py                      Flask application — 48 routes, middleware, blueprints
├── asgi.py                     FastAPI ASGI wrapper; mounts Flask; owns async /ask
├── requirements.txt            Production Python dependencies
├── requirements-dev.txt        Dev/test dependencies (pytest, etc.)
├── vercel.json                 Vercel routing config (catch-all → asgi.py)
│
├── backend/
│   ├── auth.py                 Clerk JWT verification (JWKS-based)
│   ├── rag.py                  Retrieval-augmented generation context assembly
│   ├── claude.py               AI layer: Gemini primary, Claude fallback, prompts
│   ├── sefaria.py              Sefaria REST client, topic → ref mapping
│   ├── sefaria_library.py      Sefaria library tree + text browsing
│   ├── search.py               Full-text search integration
│   ├── calendar_service.py     Jewish calendar, Daf Yomi, zmanim (Hebcal)
│   ├── zmanim_engine.py        Halachic time calculation engine
│   ├── customs.py              Community customs loader and matcher
│   ├── data_service.py         ShelahEngine — top-level orchestrator
│   ├── logging_setup.py        Structured JSON logging
│   ├── health_check.py         Circuit-breaker health checks
│   ├── cost_meter.py           LLM cost metering → ai_usage_log
│   ├── routes_library.py       /api/library blueprint
│   ├── routes_calendar.py      /api/calendar blueprint
│   ├── routes_community.py     /api/community blueprint
│   ├── routes_prayers.py       /api/prayers blueprint
│   ├── routes_user.py          /api/user blueprint
│   ├── routes_legal.py         /ai-disclosure, /acceptable-use, /dmca, /licenses blueprint
│   └── routes_devtools.py      /api/devtools blueprint
│
├── templates/
│   ├── index.html              11 200-line SPA shell (ES modules migration in progress)
│   ├── terms.html              Terms of Service page
│   └── privacy.html            Privacy Policy page
│
├── static/
│   ├── style.css               Legacy monolithic CSS (4 086 lines, migrating out)
│   ├── css/
│   │   ├── ai.css              AI panel styles
│   │   ├── calendar.css        Calendar / zmanim styles
│   │   ├── halacha.css         Halacha answer styles
│   │   ├── prayer.css          Prayer reader styles
│   │   ├── reader.css          Text reader styles
│   │   ├── sidebar.css         Sidebar nav styles
│   │   └── typography.css      Typography scale
│   ├── js/
│   │   ├── state.js            Pub/sub store (getState / setState)
│   │   ├── ai-service.js       askAi() function and streaming handler
│   │   ├── reader-ui.js        Reader panel controller
│   │   ├── zmanim.js           Calendar / zmanim UI
│   │   └── main.js             Bootstrap — imports and calls all install*() hooks
│   ├── service-worker.js       PWA offline support
│   └── manifest.webmanifest    PWA manifest
│
├── customs/                    14 community customs JSON datasets
│
├── docs/
│   ├── SERVICE_ARCHITECTURE.md System design and module breakdown
│   ├── API.md                  Full route reference
│   ├── FRONTEND.md             JS module map, theme system, CSS architecture
│   ├── OBSERVABILITY.md        Logging, cost metering, circuit breakers
│   ├── DATABASE.md             Supabase schema documentation
│   ├── DEVELOPER_NOTES.md      Developer notes and conventions
│   └── archive/                Archived historical documents
│
└── scripts/
    ├── sql/                    Supabase schema and RLS policy SQL files
    └── *.py                    Utility scripts (migrations, verification)
```
## Further Documentation

| Doc | Covers |
|---|---|
| [docs/SERVICE_ARCHITECTURE.md](docs/SERVICE_ARCHITECTURE.md) | Post-refactor module layout: `text_engine`, `search_provider`, blueprints, and how requests flow through them |
| [docs/API.md](docs/API.md) | Every route — method, path, params, auth, response shape |
| [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md) | Structured logging, request IDs, Sentry (backend + browser), cost metering, circuit breakers |
| [docs/FRONTEND.md](docs/FRONTEND.md) | Template/static structure, theming, motion rules, JS module map |
| [docs/DATABASE.md](docs/DATABASE.md) | Supabase schema documentation |
| [docs/DEVELOPER_NOTES.md](docs/DEVELOPER_NOTES.md) | Developer notes and conventions |
| [docs/SECURITY.md](docs/SECURITY.md) | Pre-launch security findings report: secrets, RLS, CSP, dependencies, input validation, AuthN/Z, and how to report a vulnerability |
| [docs/PRIVACY_OPERATIONS.md](docs/PRIVACY_OPERATIONS.md) | DSR/account-deletion flow, DPA checklist, GDPR Art. 30 RoPA table, retention job, breach response plan |
| [docs/DPIA.md](docs/DPIA.md) | Data Protection Impact Assessment for the automated religious-guidance AI |
| [docs/RUNBOOKS.md](docs/RUNBOOKS.md) | Deploy checklist, rollback, incident response, uptime monitoring, backups & recovery, spend guardrails |
| [docs/AGE_AND_SAFETY_POLICY.md](docs/AGE_AND_SAFETY_POLICY.md) | Minimum-age policy, age notice implementation, age-appropriate AI output layer |
| [docs/LAUNCH_CHECKLIST.md](docs/LAUNCH_CHECKLIST.md) | plan.md §8.H launch-gate checklist tracked line by line against real evidence |
| [docs/ACCESSIBILITY_AUDIT.md](docs/ACCESSIBILITY_AUDIT.md) | WCAG 2.1 AA color-contrast audit of the design tokens, light and dark theme |
| [docs/CONTENT_QA.md](docs/CONTENT_QA.md) | Religious-accuracy review process and the "not rabbinically supervised" disclosure |

Superseded docs from earlier integration eras (Merkava/Siddur Kol Yaakov, pre-refactor `backend/sefaria.py` API) live in [docs/archive/](docs/archive/), kept for historical reference rather than deleted.

---

## Contributing

This is currently a solo/small project, not yet staffed for a high-volume external-contribution workflow — but issues, bug reports, and small pull requests are welcome. See **[CONTRIBUTING.md](CONTRIBUTING.md)** for how to set up a dev environment, run the test suite, the coding standards this repo enforces (`.agents/ENGINEERING_RULES.md`), and the (currently lightweight) PR process.

## Code of Conduct

This project follows a code of conduct adapted from the [Contributor Covenant](https://www.contributor-covenant.org/), v2.1. By participating, you're expected to uphold it. See **[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)**.

## Security

Found a vulnerability? Please **do not open a public GitHub issue.** Email the address listed in **[docs/SECURITY.md](docs/SECURITY.md)** ("Reporting a vulnerability" section) with details and, if possible, reproduction steps. That document also covers the pre-launch security findings report: secrets/key management, RLS posture, CSP, dependency scanning, and input validation. `docs/SECURITY.md` lives in one of GitHub's three auto-detected security-policy locations (repo root, `docs/`, or `.github/`), so it is also surfaced automatically in this repository's Security tab and in new-issue prompts.

## Credits & Licenses

Sh'elah's own code is [MIT-licensed](LICENSE). It depends on and displays third-party content and libraries under their own separate licenses — Sefaria (mixed CC0/CC-BY/CC-BY-NC/public-domain, varies per text), Hebcal, Wikipedia (CC-BY-SA, share-alike), Halachipedia/HebrewBooks, the SILEOT font, and MIT-licensed open-source libraries (Tailwind CSS, DaisyUI, marked, DOMPurify). **None of this third-party content is relicensed by Sh'elah's MIT license.** Full attribution and per-source license details: **[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)**.

---

## License
* **Source Code:** This project's code is licensed under the [MIT License](LICENSE).
* **Website Content:** All text, branding, and media content on the sh'elah website are Copyright © 2026. All Rights Reserved.
