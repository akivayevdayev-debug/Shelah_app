# API Reference

All routes are served from the single Vercel deployment. The base URL in production is your Vercel project URL (e.g. `https://shelah-app.vercel.app`).

Authentication is handled via Clerk JWTs. Pass the token in the `Authorization: Bearer <token>` header. When `CLERK_ENFORCE_AUTH=false` (default), unauthenticated requests to most endpoints succeed with reduced personalisation. Endpoints marked **Auth required** return `401` without a valid token regardless of the flag.

---

## Core

### `GET /`

Returns the main SPA HTML shell.

- **Auth required:** No
- **Response:** HTML (`text/html`)
- **Errors:** None expected

---

### `GET /terms`

Returns the Terms of Service HTML page.

- **Auth required:** No
- **Response:** HTML (`text/html`)

---

### `GET /privacy`

Returns the Privacy Policy HTML page.

- **Auth required:** No
- **Response:** HTML (`text/html`)

---

### `GET /favicon.svg`

Returns the application favicon.

- **Auth required:** No
- **Response:** SVG image (`image/svg+xml`)

---

### `GET /static/manifest.webmanifest`

Returns the PWA web app manifest.

- **Auth required:** No
- **Response:** JSON (`application/manifest+json`)

---

### `GET /static/service-worker.js`

Returns the PWA service worker script.

- **Auth required:** No
- **Response:** JavaScript (`application/javascript`)

---

## Legal Pages

Routes defined in `backend/routes_legal.py` (plan.md §8.A). All four render a Jinja template with the same `clerk_publishable_key`/`clerk_enforce_auth` context as every other page, and none require auth.

### `GET /ai-disclosure`

Returns the AI Disclosure HTML page.

- **Auth required:** No
- **Response:** HTML (`text/html`)

---

### `GET /acceptable-use`

Returns the Acceptable Use Policy HTML page.

- **Auth required:** No
- **Response:** HTML (`text/html`)

---

### `GET /dmca`

Returns the DMCA / copyright-takedown HTML page.

- **Auth required:** No
- **Response:** HTML (`text/html`)

---

### `GET /licenses`

Returns the third-party licenses/attributions HTML page.

- **Auth required:** No
- **Response:** HTML (`text/html`)

---

## Site Pages

Routes defined in `backend/routes_pages.py` (plan.md §12.1, §12.2, §12.5.1).

### `GET /about`

Returns the About page HTML.

- **Auth required:** No
- **Response:** HTML (`text/html`)

---

### `GET /help`

Returns the Help page HTML.

- **Auth required:** No
- **Response:** HTML (`text/html`)

---

### `GET /glossary`

Returns the glossary page HTML, populated from `static/data/glossary.json` (passed to the template as `glossary_entries`). If that file is missing or fails to parse, the page renders with an empty glossary rather than erroring.

- **Auth required:** No
- **Response:** HTML (`text/html`)

---

### `GET /robots.txt`

Returns the crawler-directives file: allows `/`, disallows `/api/` and `/devtools/`, and points crawlers at `/sitemap.xml`.

- **Auth required:** No
- **Response:** Plain text (`text/plain`)

---

### `GET /sitemap.xml`

Returns an XML sitemap covering only stable public routes (home, `/about`, `/help`, `/glossary`, `/terms`, `/privacy`, the four Legal Pages routes above, `/accessibility`) with `changefreq`/`priority` hints. Deliberately excludes per-ref library pages, `/ask` (personalized/dynamic), and any `/api/`/`/devtools/` route.

- **Auth required:** No
- **Response:** XML (`application/xml`)

---

### `GET /llms.txt`

Returns an [llms.txt](https://llmstxt.org/)-style plain-text summary of the site for LLM crawlers/agents, listing the same stable public routes as `/sitemap.xml` (iterating the same list, so the two can't silently drift apart).

- **Auth required:** No
- **Response:** Plain text (`text/plain`)

---

## Ask Pipeline

### `POST /ask`

Submit a halachic question and receive an AI-synthesised answer with source citations. This endpoint is handled by the async FastAPI pipeline in `asgi.py` (Vercel routes it there via the catch-all rewrite). The synchronous Flask version at the same path is mounted underneath and used as a fallback.

- **Auth required:** No (auth enriches the response with user memory and personalised community lens)
- **Content-Type:** `application/json`

**Request body:**

```json
{
  "question": "string — the user's halachic question (required)",
  "mode": "string — 'balanced' | 'strict' | 'lenient' (optional, default: 'balanced')",
  "community": "string — community lens, e.g. 'ashkenaz' | 'sefardic' | 'yemenite' (optional)",
  "language": "string — 'en' | 'he' (optional, default: 'en')"
}
```

**Response (200):**

```json
{
  "answer": "string — AI-generated halachic answer",
  "sources": [
    {
      "ref": "string — Sefaria reference, e.g. 'Shulchan Arukh, Orach Chayim 318:1'",
      "text_he": "string — Hebrew source text",
      "text_en": "string — English source text",
      "url": "string — Sefaria URL"
    }
  ],
  "customs": [
    {
      "community": "string — community name",
      "ruling": "string — community-specific ruling or custom"
    }
  ],
  "wiki": [
    {
      "title": "string — article title",
      "snippet": "string — relevant excerpt",
      "url": "string — source URL"
    }
  ],
  "meta": {
    "model": "string — AI model used",
    "provider": "string — 'gemini' | 'claude'",
    "community_lens": "string — effective community lens applied",
    "request_id": "string — UUID for log correlation"
  },
  "confidence": "number — 0.0–1.0 model confidence signal"
}
```

**Request body (additional field, Turnstile only):**

```json
{
  "turnstile_token": "string — Cloudflare Turnstile response token (optional; only read once an anonymous caller has crossed the hourly threshold below — see Rate limiting & abuse mitigation)"
}
```

**Errors:**

| Status | Meaning |
|---|---|
| `400` | Empty question, question too long, or detected injection/hateful pattern |
| `403` | Turnstile verification required or failed (anonymous callers past the hourly threshold — only when `TURNSTILE_ENABLED=true`) |
| `429` | Rate limit exceeded — per-minute bucket or, for authenticated callers, the daily `/ask` quota |
| `503` | Both AI providers unavailable (circuit breakers open) |

---

## Rate limiting & abuse mitigation

`POST /ask` is the only route with a request-body-level abuse gate (Turnstile); every route is covered by the identity-aware rate limiter below. **Corrects an earlier version of this document**, which claimed a `429` on `/ask` driven by a `RATE_LIMIT_PER_MIN` env var — that var never existed in this codebase; the real mechanism is `backend/rate_limit.py`'s unified ASGI middleware, described here.

### Rate limiting (`backend/rate_limit.py`, `plan.md` §16.3-L2 / §16.6 Phase 9c)

Every route is classified into a policy class (`llm` for `/ask`, `cheap` for everything else) with its own per-minute request budget. The bucket key is **identity-aware**: a Clerk `sub` when the caller is authenticated, the trusted-IP key (`X-Vercel-Forwarded-For` → `X-Forwarded-For` → `X-Real-IP` → socket address, never a spoofable header alone) otherwise — an authenticated caller and an anonymous caller behind the same IP always land in separate buckets. Authenticated callers on the `llm` class get a higher per-minute allowance than anonymous callers, **plus** an independent daily quota; anonymous callers have no daily cap, only the tighter per-minute IP bucket.

**429 response body:**

```json
{
  "error": "Rate limit exceeded. Please wait before sending another request.",
  "code": "rate_limited"
}
```

**`Retry-After` header:** seconds until the caller may retry — `60` (the per-minute window) for an ordinary per-minute rejection, or `86400` (`_DAILY_WINDOW_SECONDS`) when an authenticated caller's *daily* `/ask` quota is what rejected the request. Always present on a `429`.

**Fail-open / fail-closed posture:** the `llm` class (i.e. `/ask`) fails **closed** on a store outage — an unmetered `/ask` during an outage is a budget hole. Every other route class fails **open** — a reader should not be blocked by a transient Redis blip.

### Turnstile (`backend/turnstile.py`, `plan.md` §16.4 / §16.6 Phase 9c)

Anonymous `/ask` traffic past a per-IP hourly request threshold is challenged with Cloudflare Turnstile (chosen over Vercel BotID — see `docs/SECURITY.md` §8 for why). Authenticated callers never see this gate; they already have Clerk signup plus the identity-aware daily quota above. Below the threshold, or whenever `TURNSTILE_ENABLED` is unset (the default), this is a true no-op with zero overhead.

**403 response body (challenge owed and no valid token supplied):**

```json
{
  "error": "Verification required before continuing.",
  "code": "turnstile_required"
}
```

Supply a solved token in the request body's `turnstile_token` field (above) to pass the gate.

### Env var matrix

| Variable | Default | Purpose |
|---|---|---|
| `RATELIMIT_ENABLED` | `true` | Kill switch for the entire rate-limit middleware. |
| `RATE_LIMIT_REDIS_URL` | unset | Upstash Redis (`rediss://`) shared store; falls back to a per-process in-memory store with a loud startup warning when unset — not a real cross-instance limit on Vercel Fluid's multiple concurrent instances. |
| `TURNSTILE_ENABLED` | `false` | Kill switch for the anonymous-`/ask` Turnstile gate; every function in `backend/turnstile.py` is a true no-op when this is unset. |
| `TURNSTILE_SECRET_KEY` | unset | Cloudflare Turnstile server secret. Required once `TURNSTILE_ENABLED=true` — if the flag is on and this is empty, every challenged request is rejected (fails closed). |
| `TURNSTILE_SITE_KEY` | unset | Cloudflare Turnstile public site key (not a secret; for the frontend widget). |
| `TURNSTILE_ANON_HOURLY_THRESHOLD` | `5` | Anonymous requests per IP per trailing hour before a Turnstile challenge is owed. |
| `PER_USER_DAILY_BUDGET_USD` | `2.00` | Per-authenticated-caller daily AI spend ceiling — a good-faith guardrail, not an anti-abuse control (Clerk signup is frictionless). Set `0` to disable. |
| `DAILY_BUDGET_USD` | unset (breaker inert) | Global cross-caller daily spend ceiling (`backend/cost_meter.py`) — the real ceiling against a multi-account attacker, independent of any per-user cap above. |

---

## Library

### `GET /api/library/index`

Returns the Sefaria library table of contents tree.

- **Auth required:** No
- **Response:** JSON array of category nodes, each with `title`, `heTitle`, `contents` children

**Errors:**

| Status | Meaning |
|---|---|
| `502` | Sefaria API unavailable |

---

### `GET /api/library/text/<ref>`

Fetch a specific Sefaria text by reference string.

- **Auth required:** No
- **Path parameter:** `ref` — URL-encoded Sefaria reference, e.g. `Berakhot.2a` or `Shulchan%20Arukh%2C%20Orach%20Chayim%201%3A1`

**Response (200):**

```json
{
  "ref": "string — canonical reference",
  "heRef": "string — Hebrew reference",
  "text": ["string — English text segments"],
  "he": ["string — Hebrew text segments"],
  "sectionRef": "string",
  "url": "string — Sefaria URL"
}
```

**Errors:**

| Status | Meaning |
|---|---|
| `404` | Reference not found in Sefaria |
| `502` | Sefaria API unavailable |

---

### `GET /api/library/search`

Full-text search across the Sefaria library.

- **Auth required:** No
- **Query parameters:**
  - `q` (required) — search query string
  - `size` (optional, default `10`) — number of results
  - `page` (optional, default `1`) — result page

**Response (200):**

```json
{
  "hits": [
    {
      "ref": "string",
      "heRef": "string",
      "text": "string — matched snippet",
      "score": "number"
    }
  ],
  "total": "number — total matching results"
}
```

**Errors:**

| Status | Meaning |
|---|---|
| `400` | Missing or empty `q` parameter |
| `502` | Sefaria search API unavailable |

---

## Calendar

### `GET /api/calendar/zmanim`

Returns halachic prayer times for a given location and date.

- **Auth required:** No
- **Query parameters:**
  - `lat` (required) — latitude as decimal, e.g. `40.7128`
  - `lon` (required) — longitude as decimal, e.g. `-74.0060`
  - `date` (optional) — ISO 8601 date string, e.g. `2026-06-11`; defaults to today

**Response (200):**

```json
{
  "date": "string — ISO date",
  "location": {"lat": "number", "lon": "number"},
  "zmanim": {
    "alos": "string — HH:MM",
    "sunrise": "string — HH:MM",
    "sof_zman_shma_gra": "string — HH:MM",
    "sof_zman_tefilla_gra": "string — HH:MM",
    "chatzos": "string — HH:MM",
    "mincha_gedola": "string — HH:MM",
    "mincha_ketana": "string — HH:MM",
    "plag_hamincha": "string — HH:MM",
    "shkia": "string — HH:MM",
    "tzeis": "string — HH:MM"
  }
}
```

**Errors:**

| Status | Meaning |
|---|---|
| `400` | Missing `lat` or `lon` parameter |
| `502` | Hebcal API unavailable |

---

### `GET /api/calendar/today`

Returns the full daily calendar payload: Hebrew date, parasha, holidays, Daf Yomi, Mishna Yomit, and zmanim (location optional).

- **Auth required:** No
- **Query parameters:**
  - `lat` (optional) — latitude for zmanim
  - `lon` (optional) — longitude for zmanim

**Response (200):**

```json
{
  "hebrew_date": "string — e.g. '11 Sivan 5786'",
  "parasha": "string — weekly Torah portion",
  "holidays": ["string — holiday names if applicable"],
  "daf_yomi": "string — e.g. 'Gittin 45'",
  "mishna_yomit": "string — e.g. 'Bava Kamma 3:1'",
  "zmanim": {}
}
```

---

### `GET /api/calendar/parasha`

Returns the current week's parasha information.

- **Auth required:** No

**Response (200):**

```json
{
  "parasha": "string — English name",
  "parasha_he": "string — Hebrew name",
  "book": "string — Torah book",
  "summary": "string — brief description"
}
```

---

## Community

### `GET /api/community/customs`

Returns the customs and halachic profile for a specific community tradition.

- **Auth required:** No
- **Query parameters:**
  - `community` (required) — community identifier, e.g. `ashkenaz`, `sefardic`, `yemenite`, `moroccan`, `persian`, `syrian`, `bukharian`, `iraqi`, `ethiopian`, `georgian`, `greek`, `mountain-jewish`, `turkish-ottoman`

**Response (200):**

```json
{
  "identity": {
    "id": "string",
    "display_name": "string",
    "hebrew_name": "string",
    "region": "string"
  },
  "halacha_index": [
    {
      "topic": "string",
      "ruling": "string",
      "sources": ["string"]
    }
  ],
  "minhagim": ["string — notable customs"]
}
```

**Errors:**

| Status | Meaning |
|---|---|
| `400` | Missing `community` parameter |
| `404` | Unknown community identifier |

---

### `POST /api/community/knowledge`

Submit a community knowledge contribution (e.g. a local minhag or tradition).

- **Auth required:** Yes

**Request body:**

```json
{
  "community": "string — community identifier",
  "topic": "string — halachic topic",
  "content": "string — the knowledge contribution",
  "source": "string — optional source citation"
}
```

**Response (201):**

```json
{
  "id": "string — UUID of created record",
  "status": "pending"
}
```

**Errors:**

| Status | Meaning |
|---|---|
| `400` | Missing required fields |
| `401` | Not authenticated |

---

### `GET /api/community/timeline`

Returns the community knowledge timeline — recent accepted contributions.

- **Auth required:** No

**Response (200):**

```json
{
  "items": [
    {
      "id": "string",
      "community": "string",
      "topic": "string",
      "content": "string",
      "created_at": "string — ISO 8601"
    }
  ]
}
```

---

## Prayers

### `GET /api/prayers/shacharit`

Returns the morning prayer service structure.

- **Auth required:** No
- **Query parameters:**
  - `community` (optional) — community nusach variant

**Response (200):**

```json
{
  "service": "shacharit",
  "nusach": "string — e.g. 'ashkenaz' | 'sefard' | 'edot-hamizrach'",
  "sections": [
    {
      "name": "string — e.g. 'Birkhot HaShachar'",
      "name_he": "string",
      "components": [
        {
          "title": "string",
          "text_he": "string — Hebrew prayer text",
          "text_en": "string — English translation",
          "rubric": "string — instruction or rubric note"
        }
      ]
    }
  ]
}
```

---

### `GET /api/prayers/mincha`

Returns the afternoon prayer service structure. Same response shape as `/api/prayers/shacharit` with `"service": "mincha"`.

- **Auth required:** No
- **Query parameters:**
  - `community` (optional)

---

### `GET /api/prayers/maariv`

Returns the evening prayer service structure. Same response shape as `/api/prayers/shacharit` with `"service": "maariv"`.

- **Auth required:** No
- **Query parameters:**
  - `community` (optional)

---

## Feedback

### `POST /api/feedback`

Submit a thumbs up/down (and optional short comment) on an AI answer. Writes one row to the `answer_feedback` table (`backend/routes_feedback.py`, plan.md §12.4). Accepted from signed-out readers too — `user_id` is nullable. The table is write-only from the client's perspective: its RLS policy grants `INSERT` to everyone but has no `SELECT` policy for the `anon`/`authenticated` roles, so submitted feedback can only be read back via the backend's own service-role Supabase client.

- **Auth required:** No (optional — if no bearer token is sent, the request proceeds anonymously unless `CLERK_ENFORCE_AUTH=true`, in which case a missing token returns `401`. If a bearer token *is* sent, it must verify: an invalid/expired token returns `401` regardless of `CLERK_ENFORCE_AUTH`. A verified token's `sub` claim is attached as `user_id`.)

**Request body:**

```json
{
  "verdict": "string — required, 'helpful' | 'not_helpful'",
  "question": "string — required, the original question text (hashed server-side with SHA-256 into the stored `question_hash`; the raw question text itself is not persisted)",
  "comment": "string — optional, sanitised (control/hidden characters stripped) and truncated to 500 chars; stored as '' if omitted",
  "mode": "string — optional, answer mode e.g. 'balanced' (default 'balanced' if omitted)",
  "language": "string — optional, e.g. 'en' (default 'en' if omitted)",
  "fallback": "boolean — optional, whether the answer came from the fallback model (default false)",
  "safety_class": "string — optional (default 'ok' if omitted)"
}
```

**Response (200):**

```json
{
  "success": true
}
```

**Errors:**

| Status | Meaning |
|---|---|
| `400` | `verdict` missing or not one of `helpful`/`not_helpful` |
| `400` | `question` missing |
| `401` | No token sent and `CLERK_ENFORCE_AUTH=true`, or a token was sent but failed verification (invalid/expired) |
| `503` | Supabase not configured |
| `500` | Insert into `answer_feedback` failed |

---

## User

### `GET /api/user/profile`

Returns the authenticated user's profile and preferences.

- **Auth required:** Yes

**Response (200):**

```json
{
  "user_id": "string — Clerk user ID",
  "email": "string",
  "community": "string — selected community lens",
  "preferences": {
    "font_size": "number",
    "theme": "string — 'light' | 'dark'",
    "language": "string — 'en' | 'he'"
  },
  "legal_accepted_at": "string — ISO 8601 or null"
}
```

**Errors:**

| Status | Meaning |
|---|---|
| `401` | Not authenticated |

---

### `GET /api/user/bookmarks`

Returns the authenticated user's saved bookmarks.

- **Auth required:** Yes

**Response (200):**

```json
{
  "bookmarks": [
    {
      "id": "string — UUID",
      "ref": "string — Sefaria reference",
      "title": "string",
      "note": "string — optional user note",
      "created_at": "string — ISO 8601"
    }
  ]
}
```

**Errors:**

| Status | Meaning |
|---|---|
| `401` | Not authenticated |

---

### `POST /api/user/preferences`

Update the authenticated user's preferences.

- **Auth required:** Yes

**Request body (all fields optional):**

```json
{
  "community": "string — community lens identifier",
  "font_size": "number",
  "theme": "string — 'light' | 'dark'",
  "language": "string — 'en' | 'he'"
}
```

**Response (200):**

```json
{
  "status": "updated",
  "preferences": {}
}
```

**Errors:**

| Status | Meaning |
|---|---|
| `400` | Invalid preference values |
| `401` | Not authenticated |

---

## Privacy

Routes defined in `backend/routes_privacy.py` (plan.md §8.D) — the self-serve GDPR/CCPA data-subject-request (DSR) flow: "download my data" and "delete my account + data". Both operate over the same six user-scoped Supabase tables (`_USER_DATA_TABLES`), each filtered/deleted with an explicit `.eq("user_id", ...)`:

| Export key | Table |
|---|---|
| `preferences` | `user_preferences` |
| `bookmarks` | `study_bookmarks` |
| `ask_history` | `ask_history` |
| `memories` | `user_memories` |
| `ai_usage_log` | `ai_usage_log` |
| `feedback` | `answer_feedback` |

Both endpoints use the service-role Supabase client (not the RLS-scoped, JWT-derived client used elsewhere) — see the module docstring in `backend/routes_privacy.py` for why: the frontend sends a plain Clerk session token, not a Supabase-compatible JWT, so gating a DSR endpoint behind the RLS-scoped client would 403 for exactly the users this feature exists to serve.

### `GET /api/user/data-export`

plan.md §8.D.1: returns a single JSON export of every row across the six tables above that belongs to the signed-in user. Fetches each table independently and paginates (`.range()`, 1000 rows/page, up to 200 pages) so a high-volume table (`ai_usage_log`, `ask_history`) isn't silently truncated by PostgREST's default per-request row cap. A failure reading one table does not fail the whole export — that table's array comes back empty and its `export_key` is listed in `partial_errors` instead.

- **Auth required:** Yes

**Response (200):**

```json
{
  "user_id": "string — Clerk user ID",
  "exported_at": "string — ISO 8601, UTC",
  "data": {
    "preferences": ["object — raw user_preferences row(s)"],
    "bookmarks": ["object — raw study_bookmarks row(s)"],
    "ask_history": ["object — raw ask_history row(s)"],
    "memories": ["object — raw user_memories row(s)"],
    "ai_usage_log": ["object — raw ai_usage_log row(s)"],
    "feedback": ["object — raw answer_feedback row(s)"]
  },
  "partial_errors": {
    "<export_key>": "string — present only if that table failed to read; other tables still export normally"
  }
}
```

**Errors:**

| Status | Meaning |
|---|---|
| `401` | Not authenticated / missing user identity in token claims |
| `503` | Supabase not configured |

---

### `POST /api/user/delete-account`

plan.md §8.D.1: irreversible self-serve "delete my account + data". Deletes rows from all six tables above, then — only if every table delete succeeded — deletes the Clerk identity itself via Clerk's Backend API (`DELETE https://api.clerk.com/v1/users/{id}`, requires `CLERK_SECRET_KEY`). Table deletes are naturally idempotent (deleting zero remaining rows is not an error), so a retried call after a partial prior failure is safe.

If any table delete fails, the Clerk account is deliberately **left intact** (not deleted) and the response says so — the Clerk identity is the user's only way to authenticate and retry, so deleting it while Supabase rows remain would orphan that data permanently with no self-serve recovery path.

- **Auth required:** Yes

**Request body:**

```json
{
  "confirmation": "string — required, must be the literal string \"DELETE\""
}
```

**Response (200 — full success):**

```json
{
  "ok": true,
  "deleted_tables": {
    "user_preferences": true,
    "study_bookmarks": true,
    "ask_history": true,
    "user_memories": true,
    "ai_usage_log": true,
    "answer_feedback": true
  },
  "clerk_deleted": true
}
```

**Response (207 — partial; a table delete failed, Clerk account untouched):**

```json
{
  "ok": false,
  "deleted_tables": { "...": "boolean per table, as above" },
  "clerk_deleted": false,
  "table_errors": { "<table_name>": "string — generic error message" },
  "clerk_skipped": "Clerk account was not deleted because one or more data tables failed to delete. Please retry."
}
```

**Response (207 — all tables deleted, Clerk delete itself failed, e.g. `CLERK_SECRET_KEY` unset):**

```json
{
  "ok": false,
  "deleted_tables": { "...": true },
  "clerk_deleted": false,
  "clerk_error": "string"
}
```

**Errors:**

| Status | Meaning |
|---|---|
| `400` | `confirmation` missing or not exactly `"DELETE"` |
| `401` | Not authenticated / missing user identity in token claims |
| `503` | Supabase not configured |
| `207` | Partial success — see response shapes above (Supabase and/or Clerk delete did not fully complete) |

---

## Webhooks

### `POST /api/webhooks/clerk`

Defined in `backend/routes_webhooks.py` (plan.md §39.2). Completeness backstop for account deletion: `POST /api/user/delete-account` above is the only code path in this app that deletes Supabase rows for a user, but a Clerk identity can also be deleted through doors this app doesn't control — Clerk's hosted `<UserProfile />` self-service delete UI, or an operator removing a user from the Clerk Dashboard. Neither path calls this app, so neither triggers cleanup without this webhook. On a `user.deleted` event, cascades the same delete across the same six tables listed under **Privacy** above, keyed on the event payload's `data.id` (there is no live Clerk JWT to authenticate with — the identity may already be gone by the time this fires).

- **Auth required:** No Clerk JWT — authentication is [Svix](https://www.svix.com/) HMAC-SHA256 signature verification instead. The request must carry `svix-id`, `svix-timestamp`, and `svix-signature` headers; the signature is computed over `"{svix-id}.{svix-timestamp}.{raw body}"` using `CLERK_WEBHOOK_SIGNING_SECRET` (base64-decoded after stripping a `whsec_` prefix) and must match one of the space-delimited `v1,<base64>` values in `svix-signature`. `svix-timestamp` must also be within 300 seconds of the server's clock, in either direction. Verified by hand against the documented Svix scheme rather than adding the `svix` package as a dependency.

**Request body:** a Clerk webhook event payload (JSON), e.g.:

```json
{
  "type": "string — Clerk event type, e.g. 'user.deleted'",
  "data": {
    "id": "string — Clerk user ID"
  }
}
```

Only `type == "user.deleted"` triggers the delete cascade. Any other event type (including Clerk's own webhook-setup test payload) is acknowledged with `200` and skipped, so Clerk doesn't retry it forever.

**Response (200 — cascade succeeded):**

```json
{
  "ok": true,
  "deleted_tables": {
    "user_preferences": true,
    "study_bookmarks": true,
    "ask_history": true,
    "user_memories": true,
    "ai_usage_log": true,
    "answer_feedback": true
  }
}
```

Or, when the event type is not `user.deleted`:

```json
{
  "ok": true,
  "skipped": "string — the event type that was ignored, or 'unrecognized_payload'"
}
```

**Response (207 — one or more table deletes failed):**

```json
{
  "ok": false,
  "deleted_tables": { "...": "boolean per table" },
  "table_errors": { "<table_name>": "string — generic error message" }
}
```

**Errors:**

| Status | Meaning |
|---|---|
| `400` | Verified payload is missing `data.id` |
| `401` | Missing/invalid Svix signature or stale timestamp |
| `503` | `CLERK_WEBHOOK_SIGNING_SECRET` not configured |
| `503` | Supabase not configured |
| `207` | Partial success — one or more table deletes failed (see `table_errors`) |

Idempotent by construction: Svix delivers at-least-once, and a replayed event just deletes zero remaining rows (still a success) rather than erroring.

---

## Devtools

### `GET /api/devtools/reliability`

Returns the current circuit-breaker state for all external dependencies.

- **Auth required:** No (informational — no sensitive data)

**Response (200):**

```json
{
  "services": {
    "sefaria": {
      "state": "string — 'closed' | 'open' | 'half-open'",
      "failures": "number",
      "last_checked": "string — ISO 8601"
    },
    "hebcal": {},
    "gemini": {},
    "claude": {}
  }
}
```

---

### `GET /api/devtools/stats`

Returns in-process counters for the current Vercel instance. Values reset on cold start and are not aggregated across instances.

- **Auth required:** No

**Response (200):**

```json
{
  "instance_id": "string",
  "uptime_seconds": "number",
  "counters": {
    "ask_total": "number",
    "ask_gemini_success": "number",
    "ask_claude_fallback": "number",
    "ask_error": "number",
    "sefaria_cache_hit": "number",
    "sefaria_cache_miss": "number"
  }
}
```

---

### `GET /api/devtools/retention-enforce`

Defined in `backend/routes_privacy.py` (plan.md §8.D.5). Scheduled job (Vercel Cron — see `vercel.json`) that actually deletes data past the retention windows documented in `templates/privacy.html` §3, rather than leaving that a policy-only promise: `ask_history` and `ai_usage_log` rows older than 90 days (by `created_at`) are hard-deleted. Also sweeps abandoned atomic AI-spend budget reservations (`ai_usage_log` rows with `reserved=true` past their `reservation_expires_at`) into the same daily run, via `expire_stale_budget_reservations()`, rather than standing up a separate job.

- **Auth required:** Yes — gated by `CRON_SECRET`, not a Clerk JWT. Same pattern as `routes_devtools.budget_check()`: fails closed (`503`) if `CRON_SECRET` is unset, so a misconfigured deployment can't be triggered by an unauthenticated caller. Pass `Authorization: Bearer <CRON_SECRET>`.

**Response (200 — fully succeeded):**

```json
{
  "ok": true,
  "ts": "string — ISO 8601, UTC",
  "ask_history": { "deleted": "number", "cutoff": "string — ISO 8601" },
  "ai_usage_log": { "deleted": "number", "cutoff": "string — ISO 8601" },
  "budget_reservations": { "deleted": "number" }
}
```

**Response (500 — one or more steps failed; `ok` is `false` and the failing step's key holds an `error` string instead of `deleted`/`cutoff`):**

```json
{
  "ok": false,
  "ts": "string — ISO 8601, UTC",
  "ask_history": { "error": "string" },
  "ai_usage_log": { "deleted": 0, "cutoff": "string" },
  "budget_reservations": { "deleted": 0, "error": "string" }
}
```

**Errors:**

| Status | Meaning |
|---|---|
| `401` | Missing/incorrect `Authorization: Bearer <CRON_SECRET>` |
| `503` | `CRON_SECRET` not configured, or Supabase not configured |
| `500` | One or more retention/reservation-sweep steps failed (see per-key `error`) |

---

### `GET /api/async/health`

FastAPI-native health endpoint. Returns `200` immediately if the ASGI process is alive. Does not probe external services.

- **Auth required:** No

**Response (200):**

```json
{
  "status": "ok",
  "runtime": "fastapi"
}
```
