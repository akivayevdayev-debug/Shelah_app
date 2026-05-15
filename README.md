# Sh'elah — Jewish Learning & Halachic AI Assistant

**Sh'elah** (שאלה — "question") is a full-stack web application for Jewish text study, halachic inquiry, and daily practice. It integrates the Sefaria text library, community customs datasets, prayer resources, zmanim, and a multi-model AI layer (Google Gemini primary, Anthropic Claude fallback) to answer halachic questions with source citations in the user's community tradition.

---

## Table of Contents

1. [Features](#features)
2. [Tech Stack](#tech-stack)
3. [Quick Start](#quick-start)
4. [Environment Variables](#environment-variables)
5. [Project Structure](#project-structure)
6. [API Reference](#api-reference)
7. [Authentication & Authorization](#authentication--authorization)
8. [Database](#database)
9. [Logging & Observability](#logging--observability)
10. [Resilience & Health Checks](#resilience--health-checks)
11. [Legal & Compliance](#legal--compliance)
12. [Deployment](#deployment)
13. [Security](#security)

---

## Features

- **AI Halachic Assistant** — answers questions with citations from Talmud, Rishonim, Acharonim, Shulchan Aruch, and responsa literature
- **Community-Aware** — 14 traditioned customs datasets (Sefardic, Ashkenaz, Yemenite, Moroccan, Persian, Syrian, Bukharian, Iraqi, Ethiopian, Georgian, Greek/Romaniote, Mountain Jewish, Turkish/Ottoman, and more)
- **Sefaria Integration** — full-text search, bilingual (Hebrew + English) source rendering, deep links
- **Prayer Reader** — browse Siddurim with community-specific nusach awareness
- **Zmanim** — halachic prayer times by GPS coordinates (via Hebcal)
- **Jewish Calendar** — Parasha, holidays, Daf Yomi, Mishna Yomit from live Hebcal feed
- **Text Reader** — explore the full Sefaria library with a clean bilingual reader
- **User Accounts** — Clerk-based authentication with Supabase-persisted preferences and bookmarks
- **Progressive Web App** — offline-capable via Service Worker + web manifest

---

## Tech Stack

| Layer | Technology |
|---|---|
| Web framework | Flask 3.x (WSGI) + ASGI adapter (`asgi.py`) |
| Frontend | Single-page HTML/Tailwind CSS/DaisyUI (CDN) |
| AI — primary | Google Gemini (`gemini-flash-3` or env override) |
| AI — fallback | Anthropic Claude (`claude-haiku-4-5` or env override) |
| Authentication | Clerk (JWT + JWKS verification) |
| Database | Supabase (Postgres via REST + RLS) |
| Rate limiting | Flask-Limiter |
| Async HTTP | `httpx.AsyncClient` (AI layer), `requests` (health checks) |
| Hebrew calendar | `pyluach` |
| Async entrypoint | `uvicorn` + `asgiref` |
| Hosting | Vercel (serverless) or any WSGI/ASGI host |

---

## Quick Start

### Prerequisites

- Python 3.11+
- A Clerk account (auth)
- A Supabase project (database)
- A Google AI / Gemini API key

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

Copy the example env file and fill in your keys:

```bash
cp .env.example .env
# Edit .env with your actual credentials
```

### Run (development)

```bash
python3 app.py
```

Default URL: http://127.0.0.1:5001

### Run (async ASGI mode)

```bash
uvicorn asgi:app --host 127.0.0.1 --port 5001 --reload
```

---

## Environment Variables

### Required

| Variable | Description |
|---|---|
| `FLASK_SECRET_KEY` | Random secret for session signing (use `python3 -c "import secrets; print(secrets.token_hex(32))"`) |
| `GOOGLE_API_KEY` | Gemini API key |
| `CLERK_PUBLISHABLE_KEY` | Clerk publishable key |
| `CLERK_JWT_ISSUER` | Clerk JWT issuer URL (e.g. `https://xxx.clerk.accounts.dev`) |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase service role key (server-side only, never expose to client) |

### Optional — AI

| Variable | Default | Description |
|---|---|---|
| `GOOGLE_MODEL` | `gemini-flash-3` | Gemini model name |
| `ANTHROPIC_API_KEY` | — | Enables Claude fallback |
| `ANTHROPIC_MODEL` | `claude-haiku-4-5` | Claude model name |
| `AI_MODEL_TIMEOUT_SECONDS` | `8` | Per-request AI timeout |
| `AI_MAX_INPUT_CHARS` | — | Max sanitized user query size |
| `AI_MAX_PROMPT_CHARS` | — | Max prompt payload to AI |
| `AI_MAX_RESPONSE_WORDS` | — | Max words returned to UI |

### Optional — Infrastructure

| Variable | Default | Description |
|---|---|---|
| `SUPABASE_PUBLISHABLE_KEY` | — | Supabase anon key (client-safe) |
| `CLERK_ENFORCE_AUTH` | `false` | Require authentication for all /api routes |
| `RATE_LIMIT_DEFAULT` | — | Global API rate limit (comma-separated list) |
| `RATE_LIMIT_ASK` | `20 per minute` | `/api/ask` rate limit |
| `RATELIMIT_STORAGE_URI` | `memory://` | Rate limiter backend (e.g. `redis://…`) |
| `LOG_LEVEL` | `INFO` | Root log level (DEBUG/INFO/WARNING/ERROR) |
| `PORT` | `5001` | Server port |

---

## Project Structure

```
app.py                  Main Flask entrypoint — all routes and middleware
asgi.py                 ASGI adapter (FastAPI + mounted Flask)
requirements.txt        Python dependencies

backend/
  claude.py             AI layer — Gemini primary, Claude fallback, prompt templates
  data_service.py       ShelahEngine — orchestrates Sefaria, customs, AI, cache
  sefaria.py            Sefaria REST API client
  sefaria_library.py    Sefaria library tree + text browsing
  search.py             Full-text search integration
  calendar_service.py   Jewish calendar, Daf Yomi, zmanim via Hebcal
  zmanim_engine.py      Halachic time calculation engine
  customs.py            Community customs loader and matcher
  logging_setup.py      Structured JSON logging (JSONFormatter + setup_logging)
  health_check.py       Circuit-breaker health checks for external APIs

templates/
  index.html            Single-page app shell (all UI, inline JS)
  terms.html            Terms of Service page
  privacy.html          Privacy Policy page

static/
  style.css             CSS variables and global typography
  css/                  Feature-specific stylesheets (ai.css, calendar.css, …)
  js/                   Client-side modules (main.js, ai-service.js, …)
  service-worker.js     PWA offline support
  manifest.webmanifest  PWA manifest

customs/                14 community customs JSON datasets
docs/                   Architecture and implementation documentation
scripts/                Utility scripts (migrations, verification, audits)
scripts/sql/            Supabase schema and RLS policy SQL files
```

---

## API Reference

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `GET` | `/` | optional | Main SPA |
| `GET` | `/terms` | — | Terms of Service |
| `GET` | `/privacy` | — | Privacy Policy |
| `POST` | `/api/ask` | optional | AI halachic Q&A |
| `GET` | `/api/calendar` | — | Jewish calendar + zmanim |
| `GET` | `/api/sefaria/text/<ref>` | — | Fetch Sefaria text by reference |
| `GET` | `/api/sefaria/search` | — | Full-text search |
| `GET` | `/api/library` | — | Sefaria library tree |
| `GET` | `/api/customs` | — | List available customs |
| `POST` | `/api/accept-legal` | optional | Record legal acceptance |
| `GET` | `/api/stack/health` | — | System health (incl. external API circuit state) |
| `GET` | `/api/devtools/heartbeat` | — | Detailed diagnostics |

---

## Authentication & Authorization

Authentication uses **Clerk** JWT tokens verified server-side via JWKS. The `CLERK_ENFORCE_AUTH` flag controls whether unauthenticated requests to `/api/*` routes are rejected (default: permissive — most features work without login).

User IDs (`clerk_id`) are stored in Supabase for preferences, bookmarks, and legal-acceptance records. RLS policies ensure users can only access their own rows.

See [`scripts/sql/SUPABASE_RLS_POLICIES.sql`](scripts/sql/SUPABASE_RLS_POLICIES.sql) for full policy definitions.

---

## Database

See [`docs/DATABASE.md`](docs/DATABASE.md) for full schema documentation.

Core Supabase tables:

| Table | Purpose |
|---|---|
| `user_preferences` | Per-user settings, legal acceptance, community lens |
| `rag_identity_cache` | Cached user identity context for AI prompts |
| `bookmarks` | User-saved texts and references |
| `queries` | Query log (optional analytics) |

---

## Logging & Observability

All application logs are emitted as **structured JSON** via `backend/logging_setup.py`. Each record includes:

```json
{
  "timestamp": "2026-05-14T12:00:00.123+00:00",
  "level": "INFO",
  "logger": "backend.data_service",
  "message": "AI response generated",
  "module": "data_service",
  "function": "ask",
  "line": 412
}
```

`setup_logging()` is called at Flask startup and configures the root logger, so all libraries (including Flask's own `app.logger`) inherit the JSON formatter automatically.

The `LOG_LEVEL` environment variable controls verbosity (`DEBUG` for local development, `INFO` / `WARNING` in production).

---

## Resilience & Health Checks

`backend/health_check.py` implements a **circuit-breaker** pattern for four external APIs:

| Service | Probe |
|---|---|
| Sefaria | `GET /api/texts/Berakhot.2a` |
| Hebcal | `GET /api/holidays` |
| Gemini | `GET /v1beta/models` |
| Claude | `GET /v1/models` |

A circuit opens after **3 consecutive failures** and half-opens after **120 seconds**, at which point the next request triggers a live probe. Circuit state is exposed in `/api/stack/health` under `external_apis`.

Usage in service code:

```python
from backend.health_check import health

if not health.is_healthy('sefaria'):
    return {'error': 'Sefaria is temporarily unavailable.'}
```

---

## Legal & Compliance

- **Terms of Service**: `/terms` — covers acceptable use, disclaimer of religious advice, limitation of liability
- **Privacy Policy**: `/privacy` — covers data collected, retention, GDPR/CCPA rights, third-party processors
- **Legal acceptance modal**: shown once on first visit; stores acceptance in `localStorage` and (for authenticated users) in Supabase `user_preferences`
- Sh'elah provides **educational information only** — it is not a posek (halachic decisor). Users requiring binding halachic decisions should consult a qualified rabbi.

---

## Deployment

### Vercel

```bash
vercel deploy
```

Configuration in `vercel.json`. All environment variables must be set in the Vercel project dashboard.

### Self-Hosted (WSGI)

```bash
gunicorn app:app --bind 0.0.0.0:5001 --workers 4
```

### Self-Hosted (ASGI)

```bash
uvicorn asgi:app --host 0.0.0.0 --port 5001 --workers 4
```

---

## Security

- **No secrets in source** — all credentials via environment variables
- **Input sanitization** — user queries stripped and length-capped before AI prompts
- **Rate limiting** — Flask-Limiter on all write endpoints
- **JWKS verification** — Clerk JWTs verified against live JWKS endpoint with caching
- **Supabase RLS** — row-level security on all user tables
- **Content policy** — AI system prompt explicitly forbids political advocacy and off-topic content

Pre-commit security hooks (Bandit + Gitleaks):

```bash
pip install pre-commit
pre-commit install
pre-commit run --all-files
```

