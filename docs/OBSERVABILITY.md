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

## Vercel Log Drain

1. Vercel dashboard → Project → Storage → Log Drain → Add
2. Set drain URL to your aggregator's ingest endpoint
3. Select **JSON** format — output maps directly, no parser needed

The `request_id` field enables cross-request tracing when correlating drain events with deployment logs.
