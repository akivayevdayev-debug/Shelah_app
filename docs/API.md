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

**Errors:**

| Status | Meaning |
|---|---|
| `400` | Empty question, question too long, or detected injection/hateful pattern |
| `429` | Rate limit exceeded (`RATE_LIMIT_PER_MIN`) |
| `503` | Both AI providers unavailable (circuit breakers open) |

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
