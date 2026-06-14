# Sh'elah — Jewish Learning & Halachic AI Assistant

**Sh'elah** (שאלה — "question") is a full-stack web application for Jewish text study, halachic inquiry, and daily practice. It integrates the Sefaria text library, community customs datasets, prayer resources, zmanim, and a multi-model AI layer (Google Gemini primary, Anthropic Claude fallback) to answer halachic questions with source citations in the user's community tradition.

---

## Table of Contents

1. [What is Sh'elah](#what-is-shelah)
2. [Architecture Overview](#architecture-overview)
3. [Quick Start](#quick-start)
4. [Environment Variables](#environment-variables)
5. [Testing](#testing)
6. [Deployment](#deployment)
7. [Directory Structure](#directory-structure)

---

## What is Sh'elah

Sh'elah is a Jewish learning, halacha, calendar, and prayers application. It helps users:

- Ask halachic questions and receive AI-synthesized answers with citations from Talmud, Rishonim, Acharonim, Shulchan Aruch, and responsa literature
- Get community-aware guidance across 14 tradition datasets (Sefardic, Ashkenaz, Yemenite, Moroccan, Persian, Syrian, Bukharian, Iraqi, Ethiopian, Georgian, Greek/Romaniote, Mountain Jewish, Turkish/Ottoman, and more)
- Browse the full Sefaria text library with bilingual (Hebrew + English) rendering
- View halachic prayer times (zmanim) by GPS coordinates
- Follow the Jewish calendar — Parasha, holidays, Daf Yomi, Mishna Yomit from live Hebcal feed
- Browse prayer services (Shacharit, Mincha, Maariv) with community-specific nusach awareness
- Manage bookmarks and per-user preferences via Clerk authentication + Supabase storage

Sh'elah provides **educational information only** — it is not a posek (halachic decisor). Users requiring binding halachic decisions should consult a qualified rabbi.

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
| `backend/routes_*.py` | Blueprint modules for library, calendar, community, prayers, user, devtools |

---

## Quick Start

### Prerequisites

- Python 3.11+
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

### Required

| Variable | Description |
|---|---|
| `FLASK_SECRET_KEY` | Random secret for session signing — generate with `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `FLASK_ENV` | `development` or `production` |
| `CLERK_JWT_ISSUER` | Clerk JWT issuer URL, e.g. `https://xxx.clerk.accounts.dev` |
| `CLERK_AUDIENCE` | Audience claim expected in Clerk JWTs (usually your app URL or leave blank) |
| `CLERK_ENFORCE_AUTH` | `true` to require auth on all `/api/*` routes; `false` for permissive mode |
| `SUPABASE_URL` | Supabase project URL, e.g. `https://xyz.supabase.co` |
| `SUPABASE_ANON_KEY` | Supabase public anon key (safe to expose in browser) |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase service role key — **never expose to client** |
| `ANTHROPIC_API_KEY` | Anthropic Claude API key (used as AI fallback) |
| `GEMINI_API_KEY` | Google Gemini API key (primary AI) |

### Optional

| Variable | Default | Description |
|---|---|---|
| `GOOGLE_TRANSLATE_API_KEY` | — | Enables Google Translate for response translation |
| `SEFARIA_API` | `https://www.sefaria.org/api` | Sefaria API base URL |
| `HEBCAL_API` | `https://www.hebcal.com/api` | Hebcal API base URL |
| `LOG_LEVEL` | `INFO` | Root log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `RATE_LIMIT_PER_MIN` | `20` | Requests per minute cap on the `/ask` endpoint |
| `GOOGLE_MODEL` | `gemini-flash-3` | Gemini model name override |
| `ANTHROPIC_MODEL` | `claude-haiku-4-5` | Claude model name override |
| `AI_MODEL_TIMEOUT_SECONDS` | `8` | Per-request AI timeout in seconds |
| `AI_MAX_INPUT_CHARS` | — | Max sanitized user query size |
| `AI_MAX_PROMPT_CHARS` | — | Max prompt payload to AI |
| `AI_MAX_RESPONSE_WORDS` | — | Max words returned to UI |
| `RATELIMIT_STORAGE_URI` | `memory://` | Rate limiter backend (e.g. `redis://…`) |
| `PORT` | `5001` | Server port for plain Flask mode |

---

## Testing

```bash
# Install dev dependencies
pip install -r requirements-dev.txt

# Run test suite
pytest
```

Tests live in the project root and `tests/` subdirectory. The test suite covers the backend service modules; the Flask routes are exercised via Flask's test client.

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
