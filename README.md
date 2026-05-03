# Sh'elah App

> Sync status (2026-04-21): Verified against current implementation (report-driven library filtering, topbar menu icon layering fix, global warm icon tones, and backup template sync).

Sh'elah is a Flask-based Jewish learning and halachic assistant that combines:
- text browsing from Sefaria
- community customs data
- prayer access
- zmanim and holiday context
- AI-assisted Sh'elah synthesis

## Quick Start

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```
If on Mac, use this instead:
```bash
pip install -r requirements.txt --break-system-packages
```
3. Set environment variables (see `.env.example`).
4. Run the app:

```bash
cd ~/(Location_of_file)
python app.py
```
If on Mac, use this instead:
```bash
cd ~/(Location_of_file)
python3 app.py
```
Default local URL: http://127.0.0.1:5001

Optional async ASGI entrypoint (FastAPI + mounted Flask):

```bash
uvicorn asgi:app --host 127.0.0.1 --port 5001 --reload
```

## Project Structure

- `app.py`: Flask entrypoint and API routes
- `asgi.py`: FastAPI async `/ask` route with mounted legacy Flask app
- `backend/`: backend service modules and integrations
- `templates/`: HTML templates
- `static/`: CSS and static assets
- `customs/`: community customs JSON datasets
- `docs/`: implementation and audit docs
- `scripts/`: utility scripts (verification, data fetch, audits)
- `MD files/SUPABASE_RLS_POLICIES.sql`: baseline Supabase `auth.uid()` RLS policies for user-scoped tables

## Environment Variables

- `FLASK_SECRET_KEY`: required for stable sessions
- `ANTHROPIC_API_KEY`: optional, enables Claude-backed responses
- `ANTHROPIC_MODEL`: optional Claude model override (default: `claude-haiku-4-5`)
- `AI_MAX_INPUT_CHARS`: max sanitized user query size passed to AI wrapper
- `AI_MAX_PROMPT_CHARS`: max prompt payload length sent to Claude
- `AI_MAX_RESPONSE_WORDS`: max response words returned to UI
- `RATE_LIMIT_DEFAULT`: optional global API limit string list (comma-separated)
- `RATE_LIMIT_ASK`: `/ask`-specific rate limit
- `RATELIMIT_STORAGE_URI`: limiter backend (`memory://` by default)
- `PORT`: optional override for server port

## Security Scanning (Pre-Commit)

This repository includes `.pre-commit-config.yaml` with:
- Bandit (Python security linting)
- Gitleaks (secret detection)

Setup:

```bash
pip install pre-commit
pre-commit install
pre-commit install --hook-type pre-push
```

Run manually anytime:

```bash
pre-commit run --all-files
```

## Notes for GitHub

This repository is configured to ignore local caches, virtual environments, and `.env` files.
If you publish publicly, ensure your deployment uses environment variables and does not expose secrets.
