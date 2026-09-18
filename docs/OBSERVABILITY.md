# Observability — Logging, Cost Metering & Health

## Structured JSON Logging

Every log line is a compact single-line JSON object, parseable by Vercel log drains, Datadog, Papertrail, or any JSON-aware aggregator.

### Log fields (always present)

| Field | Type | Description |
|---|---|---|
| `timestamp` | ISO 8601 | UTC time, millisecond precision |
| `level` | string | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `logger` | string | Logger name (e.g. `backend.claude`) |
| `message` | string | Human-readable message |
| `module` | string | Python module name |
| `function` | string | Function name |
| `line` | int | Source line number |
| `request_id` | string | Per-request trace ID (empty if not set) |
| `exception` | string | Formatted traceback (only on exc_info records) |

### How to use

```python
from backend.logging_setup import get_logger, bind_request_id

logger = get_logger(__name__)

# At request entry — accepts X-Request-ID header or generates a UUID fragment:
request_id = bind_request_id(request.headers.get("X-Request-ID"))

logger.info("Processing question", extra={"question_len": len(q)})
```

### Setup (called once at startup)

```python
from backend.logging_setup import setup_logging
setup_logging()   # reads LOG_LEVEL env var (default: INFO)
```

Noisy third-party loggers (`httpx`, `hpack`, `anthropic`) are suppressed to WARNING automatically.

---

## request_id Propagation

`request_id` lives in a `contextvars.ContextVar` — it flows automatically across Flask threads and `asyncio` tasks spawned via `asyncio.to_thread`. To read the current ID from any layer:

```python
from backend.logging_setup import get_request_id
rid = get_request_id()  # "" if not set
```

---

## Cost Metering (`backend/cost_meter.py`)

Records token usage and estimated USD cost for every outbound AI call, then writes to Supabase `ai_usage_log` via `asyncio.to_thread` (fire-and-forget, never blocks the event loop).

### Record a call

```python
from backend.cost_meter import record_llm_call
from backend.logging_setup import get_request_id

await record_llm_call(
    provider="anthropic",
    model="claude-sonnet-4-6",
    input_tokens=response.usage.input_tokens,
    output_tokens=response.usage.output_tokens,
    route="/ask",
    request_id=get_request_id(),
)
```

### `ai_usage_log` table schema

| Column | Type | Description |
|---|---|---|
| `provider` | text | `anthropic`, `gemini`, `google-translate`, `mymemory` |
| `model` | text | Full model ID |
| `input_tokens` | int | Input token count from API response `usage` block |
| `output_tokens` | int | Output token count |
| `cost_usd` | numeric(12,8) | Estimated USD cost |
| `route` | text | Request path that triggered this call |
| `request_id` | text | Trace ID from logging context |
| `created_at` | timestamptz | UTC insertion time |

Prices are defined in `cost_meter._PRICE_PER_M` (USD per 1M tokens). Update when providers change pricing.

---

## Circuit Breaker (`backend/health_check.py`)

| Constant | Default | Meaning |
|---|---|---|
| `FAIL_THRESHOLD` | 3 | Consecutive failures before circuit opens |
| `RECOVERY_INTERVAL` | 120s | Seconds before half-open probe |
| `REQUEST_TIMEOUT` | 5s | Per-probe timeout |

**Services tracked:** `sefaria`, `hebcal`, `gemini`, `claude`

```python
from backend.health_check import health

if not health.is_healthy("sefaria"):
    return {"error": "Sefaria temporarily unavailable."}, 503
health.record_success("sefaria")
```

**Serverless note:** State is per-instance. Each cold start re-probes failing services on first request — intentional trade-off vs. adding Supabase latency to every request.

---

## Devtools Endpoints

| Endpoint | Auth | Description |
|---|---|---|
| `GET /api/devtools/reliability` | None | Circuit-breaker states + upstream health |
| `GET /api/devtools/stats` | None | `DEVTOOLS_STATS` in-process counters |
| `GET /api/async/health` | None | FastAPI liveness: `{"status":"ok","async":true}` |

---

## Sentry Project Setup

Two Sentry projects, one org (`o4511830797975553`). Full rationale for every decision below lives in `plan.md` §17 — this section is the durable operational copy.

### The two projects

| Project | Sentry platform | Env var | Where it's read | Secrecy |
|---|---|---|---|---|
| Backend | **Python → FastAPI** (FastAPI is *under* Python in Sentry's picker, not a separate platform) | `SENTRY_DSN` | `backend/logging_setup.py` | Server secret — never commit, never log. |
| Frontend | **Browser JavaScript** (not React — no React surfaces exist yet) | `SENTRY_DSN_BROWSER` | `app.py` → `templates/index.html` → `static/js/sentry-init.js` | Public by design (a browser DSN can only *send* events), but still env-only — never hardcoded in a template or committed file. |

**Why FastAPI, not Flask, for the backend project.** `asgi.py:559`'s `fastapi_app` is the actual process entrypoint Vercel invokes in production; the Flask app is mounted *inside* it via `WSGIMiddleware` at `asgi.py:556`. Sentry's FastAPI/Starlette integration sits at that outer ASGI boundary, so it observes every request — including ones later routed to the WSGI-mounted Flask app — where a hypothetical "Flask" project selection would only see requests that happen to terminate in Flask's own WSGI handling. `python3 app.py` (bare Flask, no ASGI layer — see the D1 finding in `plan.md` §16.1) is a local-only run mode; production is FastAPI.

### Environment → env var matrix

| Vercel environment | `SENTRY_DSN` | `SENTRY_DSN_BROWSER` |
|---|---|---|
| Production | ✅ set | ✅ set |
| Preview | ✅ set (same project — `environment` tag distinguishes preview from prod via `VERCEL_ENV`) | ✅ set |
| Development (local) | unset by default | unset by default |

Both are true no-ops when unset: no SDK init, no CDN script tag, no network call, no console warning. `environment` and `release` are read identically on both sides (`VERCEL_ENV` and `VERCEL_GIT_COMMIT_SHA`) so a browser error and a backend error from the same deploy carry matching tags — that's the whole point of running two projects instead of one.

### The 5,000 events/month quota — why filtering is a prerequisite, not polish

Sentry's free Developer plan caps at **5,000 events/month across both projects** — roughly 170/day. That is trivially exhausted by:
- One looping component emitting an unbounded stream of the same client error.
- `POST /api/client-errors` (`backend/routes_devtools.py`) — before hardening, this was unauthenticated, unrate-limited, and forwarded an 8 KB attacker-controlled `stack` straight into a Sentry `capture_exception` call. ~5,000 forged requests would have blinded error monitoring for the rest of the month without ever touching real application logic. It's now same-origin-gated, caps `stack` at 2,000 chars, and never forwards the (currently spoofable — see `plan.md` §16.1 D2) client IP into the Sentry context.
- A backend DSN leak: unlike the browser DSN, a leaked `SENTRY_DSN` lets an attacker POST forged events directly to Sentry's ingest with no application involved at all — this is why it's a server secret, never written into `plan.md`, README, or any committed file.

Client-side mitigations (`static/js/sentry-init.js`) are defense in depth, not the primary control — they reduce volume before an event is even sent, but the dashboard settings below are what actually enforce the ceiling.

### Required dashboard settings (manual — not code, must be set before launch)

1. **Per-key rate limits** (Settings → Client Keys → Rate Limits) on both projects. 5K/month ≈ 170/day — set both keys well under that so one bad deploy can't burn the whole month. Tighter on the browser key (it's public).
2. **Spike Protection** — enabled on both projects, backstop for whatever the rate limits miss.
3. **Inbound filters** (browser project especially): browser-extension errors, localhost, known crawlers, legacy browsers. These drop server-side *before* counting against quota — the client-side noise filtering in `sentry-init.js` still costs an event if it doesn't run (e.g. `beforeSend` bypassed by a bug), so both layers matter.
4. **Data scrubbing**: Data Scrubber + default scrubbers + **Scrub IP Addresses** enabled. Doubly required here: `send_default_pii=False`/`sendDefaultPii: false` are set in both SDK inits, but Data Scrubber is the server-side backstop, and IP scrubbing is worth it twice over — once for privacy (§8.D), and once because until `plan.md` §16.1 D2 lands, the IP the backend would attach is attacker-spoofable anyway (trusts `CF-Connecting-IP` first, which Vercel doesn't set).
5. **One alert rule**: volume spike → email. Per-issue alerts at this quota teach you to ignore Sentry rather than act on it.
6. **Source maps: deferred.** No frontend build step exists yet — the browser bundle is loaded unminified-enough vanilla JS from `static/js/`, so raw stack traces are still readable. Revisit once `plan.md` §7g / Prompt 11 gives the frontend a bundler; do not pretend this is configured before then.

### What's intentionally off

Per `plan.md` §17.5: Profiling, Sentry Logs (duplicate of the structured JSON logging above), Session Replay (privacy — halachic questions are frequently medical/marital/mental-health/abuse-adjacent), the User Feedback widget (§12 plans a first-party one so responses land in Supabase, not a third-party quota), and Cron Monitoring (this repo has no cron jobs and must not acquire any per §14.6). Uptime Monitoring is on, 5-minute interval — a free Sentry check still costs a real invocation on your end, so it must not tempt the interval back down.

---

## Vercel Log Drain

1. Vercel dashboard → Project → Storage → Log Drain → Add
2. Set drain URL to your aggregator's ingest endpoint
3. Select **JSON** format — output maps directly, no parser needed

The `request_id` field enables cross-request tracing when correlating drain events with deployment logs.
