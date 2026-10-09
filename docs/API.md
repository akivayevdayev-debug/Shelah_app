# API Reference

Every route is served from one Vercel deployment. The base URL in production is the project's Vercel URL (for example `https://shelah-app.vercel.app`).

**Status (2026-10-02):** written against the route table in the code (`app.py`, `asgi.py` and every `backend/routes_*.py` blueprint), not against an earlier design. `POST /ask` and `GET /api/async/health` are native FastAPI routes in `asgi.py`; everything else is Flask, reached through the `WSGIMiddleware` mount underneath. Both stacks share one rate limiter, one cache-policy table and one auth module.

## Conventions

**Authentication.** Clerk JWTs, sent as `Authorization: Bearer <token>`. Each route is one of:

| Label | Meaning |
|---|---|
| **Public** | No token needed. |
| **Optional** | `maybe_require_clerk_auth`. With no token the request runs anonymously unless `CLERK_ENFORCE_AUTH` is on, in which case it is a `401`. A token that is sent must verify, or it is a `401` either way. |
| **Clerk** | `require_clerk_auth`. A missing or invalid token is always a `401`, whatever `CLERK_ENFORCE_AUTH` says. |
| **Cron secret** | `Authorization: Bearer <CRON_SECRET>`, compared in constant time. Fails closed (`503`) when `CRON_SECRET` is unset. |
| **Svix signature** | Webhook signature check in place of a JWT. |

`CLERK_ENFORCE_AUTH` defaults to `true` when `VERCEL=1` or `FLASK_ENV=production` and to `false` otherwise (`backend/auth.py`). The production Vercel project sets it to `false` explicitly and on purpose (checked 2026-10-03), so on the live site the **Optional** routes, `/ask` included, run anonymously when no token is sent; they are `401` only where enforcement is on. See `docs/ENVIRONMENT.md` and `docs/SECURITY.md` §6 and §13.

**Errors.** Flask routes return `{"error": "<message>"}`. The native FastAPI routes return `{"detail": "<message>"}`. A `429` always carries the rate-limiter body below, whichever stack answered.

**Request identity.** Every response carries a request id for log correlation. Every non-private cacheable response also carries `X-Deploy-Hash` (diagnostic only).

**Rate limits and caching.** Each route belongs to a rate-limit class (see [Rate limiting](#rate-limiting--abuse-mitigation)) and a `Cache-Control` tier (see [Cache tiers](#cache-tiers)). Both are listed per route group below.

**Safe to share.** Nothing here depends on a secret except where a section says so.

## Route index

| Group | Routes |
|---|---|
| [Pages](#pages) | `/`, `/settings`, `/profile`, `/terms`, `/privacy`, `/accessibility`, `/about`, `/help`, `/glossary`, `/ai-disclosure`, `/acceptable-use`, `/dmca`, `/licenses`, `/robots.txt`, `/sitemap.xml`, `/llms.txt`, `/manifest.webmanifest`, `/favicon.ico`, `/service-worker.js` |
| [Deep-link paths](#deep-link-paths) | `/text/…`, `/prayer/…`, `/siddur[/…]`, `/community/…`, `/calendar/…`, `/answer/…`, `/a/…`, `/chat/…`, `/history[/…]`, `/signin` |
| [Ask](#ask-pipeline) | `POST /ask` |
| [Conversations](#conversations) | `/api/conversations` (+ `/<id>`, `/<id>/messages`, `/<id>/restore`, `/<id>/ask`, `/<id>/share`) |
| [History and sharing](#answer-history-and-sharing) | `/api/user/history` (+ `/<id>`, `/<id>/share`), `/api/public/answer/<token>`; [conversation sharing](#conversation-sharing) and [device identity](#device-identity) |
| [Library and texts](#library-and-texts) | `/api/library/*`, `/api/texts-index`, `/api/text/<ref>` (+ `/links`, `/graph`), `/api/sidebar/<ref>`, `/api/search/suggest`, `/api/word/meaning`, `/api/export/chapter`, `/api/diagnostics/sefaria` |
| [Prayers and siddur](#prayers-and-siddur) | `/api/prayers/list`, `/api/prayer/<name>`, `/api/siddur/v2/*`, `/api/siddur/full/*`, `/api/siddur/section-refs/*` |
| [Calendar and location](#calendar-and-location) | `/set_location`, `/api/geocode`, `/api/zmanim` (+ `/month`, `/days`), `/api/daily-study`, `/api/holidays`, `/api/parasha` |
| [Communities](#communities) | `/api/communities`, `/api/communities/list`, `/api/community/<name>` (+ `/timeline`) |
| [Feedback](#feedback) | `POST /api/feedback` |
| [Account](#account) | `/api/auth/me`, `/api/user/preferences`, `/api/preferences`, `/api/bookmarks/semantic`, `/api/accept-legal` |
| [Privacy](#privacy) | `GET /api/user/data-export`, `POST /api/user/delete-account` |
| [Webhooks](#webhooks) | `POST /api/webhooks/clerk` |
| [Operations](#operations-and-health) | `/api/async/health`, `/api/health`, `/api/stack/health`, `/api/devtools/*`, `/api/client-errors` |

---

## Pages

HTML pages are Jinja templates sharing one context (`clerk_publishable_key`, `clerk_enforce_auth`). None require auth; all are **Public**.

| Route | Page |
|---|---|
| `GET /`, `GET /settings`, `GET /profile` | The single-page app shell (`templates/index.html`). The three paths serve the same shell; the client router decides the view. |
| `GET /terms`, `GET /privacy`, `GET /accessibility` | Terms of Service, Privacy Policy, accessibility statement (`app.py`). |
| `GET /ai-disclosure`, `GET /acceptable-use`, `GET /dmca`, `GET /licenses` | AI Disclosure, Acceptable Use Policy, DMCA / copyright takedown, third-party licences and attributions (`backend/routes_legal.py`). |
| `GET /about`, `GET /help` | About and Help (`backend/routes_pages.py`). |
| `GET /glossary` | Glossary, populated from `static/data/glossary.json` and passed to the template as `glossary_entries`. A missing or unparseable file renders an empty glossary instead of an error. |
| `GET /manifest.webmanifest` | The PWA manifest. |
| `GET /favicon.ico` | The favicon. |
| `GET /service-worker.js` | The PWA service worker, served from the site root so its scope covers the whole app. It serves `/api/*` stale-while-revalidate. |

### `GET /robots.txt`

Plain text. Allows `/`, disallows `/api/` and `/devtools/`, and points crawlers at `/sitemap.xml`.

### `GET /sitemap.xml`

`application/xml`. The stable public routes (home, `/about`, `/help`, `/glossary`, `/terms`, `/privacy`, the four legal pages above, `/accessibility`) plus the library's content pages at their canonical paths: every Tanakh chapter (`/text/Genesis.1` … `/text/II_Chronicles.36`, 929 URLs, mirroring the reader's chapter grid), the fixed prayer services (`/prayer/Weekday_Shacharit`, …) and the community pages (`/community/Ashkenaz`, …). Built from static data only (no Sefaria call), cached per process, with `changefreq` and `priority` hints. Private views (`/answer/`, `/a/`, `/history`), `/ask` and every `/api/` and `/devtools/` route are excluded.

### `GET /llms.txt`

`text/plain; charset=utf-8`, in the [llms.txt](https://llmstxt.org/) style: an H1, a short summary of the site for LLM crawlers, then sections of `- [title](url): note` Markdown links (a bare URL is not a link, and Lighthouse's agentic-browsing audit fails a file with none). It links the same stable public routes as the sitemap by iterating the same list (`_SITEMAP_PATHS`, with a title and note for each in `_LLMS_PAGES`), so the two cannot drift apart; adds one entry point each into the Tanakh reader and the siddur; and links `/sitemap.xml` for the ~950 library pages it leaves out. Every URL is on the canonical host `https://shelah.org`, as are the sitemap's and robots.txt's and the pages' own canonical tags (`SITE_BASE_URL` in `backend/page_meta.py`, `_SITE_BASE_URL` in `backend/routes_pages.py` is the same value).

---

## Deep-link paths

`backend/routes_spa_paths.py`. The client router (`static/js/router.js`) keeps the main view in the URL path, so these routes serve the **same shell as `/`** with a per-URL `<title>`, `og:url` and canonical link (`backend/page_meta.py`). A cold open or refresh on any of them paints the app instead of a 404. All are `GET`, **Public**.

| Path | View |
|---|---|
| `/text/<ref>` | A text in the reader, e.g. `/text/Genesis.1`. |
| `/prayer/<name>` | A prayer service. |
| `/siddur`, `/siddur/<rite>[/<service>[/<section>]]` | The siddur reader. `/siddur` answers `308` to the default rite. Every siddur page is known in advance (`backend/siddur_data.py`), so an unknown rite, or a segment that is neither a service, a section nor a tail word, is a real `404`. |
| `/community/<name>` | A community's customs. |
| `/calendar/<YYYY-MM-DD>` | The calendar on a day. |
| `/answer/<id>` | One of the caller's own stored answers (private; loaded through `GET /api/user/history/<id>`). |
| `/a/<token>` | A publicly shared answer (loaded through `GET /api/public/answer/<token>`). |
| `/chat/<id>` | A conversation. |
| `/history`, `/history/<rest>` | The answer history. |
| `/signin` | The sign-in overlay (`/profile` and `/settings` are served by `app.py`, above). |

After the view comes an optional **tail** of overlay and AI keys, all path segments: `chat/<id>`, a community slug, an answer mode (`balanced`, `practical`, `sources`, `strict`), `mini` / `overlay` / `full`, `calendar/<day>`, and `signin` / `profile` / `settings`. Examples: `/chat/new/sefardic/strict`, `/text/Genesis.1/chat/<id>/mini`. The server checks the tail against the router's own grammar, so a segment that fits nowhere is a real `404` rather than a soft-404 shell. The view value itself is not validated, with one exception: a text the reader has already found does not exist (`sefaria_library.is_known_missing_text`, never a Sefaria request here) is served with a real `404` status and kept out of search results.

- A trailing slash (`/text/Genesis.1/`, `/history/`) answers `308` to the bare path, query string kept.
- Legacy query links (`/?text=…`, `/?prayer=…`, `/?community=…`) answer `308` to their path form, other query keys kept, so a shared or bookmarked old link reaches its canonical, indexable URL.
- One person's answers, history, conversations and the sign-in overlay (`/answer/…`, `/a/…`, `/chat/…`, `/history…`, `/signin`) are served with `X-Robots-Tag: noindex, nofollow`, as is `/` when a private query key is present.
- No `vercel.json` rewrite is involved; Vercel's implicit routing sends every path to the one ASGI function, and a catch-all rewrite there once 404'd production.

---

## Ask pipeline

### `POST /ask`

Submit a halachic or Torah-study question and receive an AI-synthesised answer with source citations. Handled by the async FastAPI route in `asgi.py`. A synchronous Flask implementation of `/ask` still exists in `app.py`, but the native route shadows it and it is not reachable in production.

- **Auth:** Optional. Required whenever `CLERK_ENFORCE_AUTH` is on (the code's default on Vercel, but deliberately set to `false` on the production project, so anonymous asking works there). Auth enriches the answer with the caller's memory summaries and records it in the caller's history. A signed-out caller's answer is recorded too, under their **device**: the first signed-out `/ask` that succeeds sets a `shelah_device` cookie (see [Device identity](#device-identity)), and the returned `history_id` opens at `/answer/<id>` in that browser only.
- **Rate-limit class:** `llm` (fails closed).
- **Cache:** `private, no-store`.
- **Content-Type:** `application/json`

**Request body:**

```json
{
  "question": "string, the user's question (required)",
  "mode": "string, 'balanced' | 'practical' | 'sources' | 'strict' (optional, default 'balanced'; an unknown value falls back to 'balanced')",
  "community": "string, community lens such as 'Ashkenaz' or 'Sefardic' (optional, default 'All'; names and aliases are canonicalised)",
  "language": "string, 'en' | 'he' (optional, default 'en')",
  "turnstile_token": "string, Cloudflare Turnstile response token (optional; only read once an anonymous caller has crossed the hourly threshold, see Rate limiting)"
}
```

The question is sanitised before use (hidden and control characters removed, whitespace collapsed) and cut to 1,200 characters. A question that is empty after sanitising is a `400`.

**Response (200):**

```json
{
  "answer": "string, the answer in markdown, claims tied to sources with [n] markers",
  "confidence": "number | null, the model's confidence signal",
  "wiki": [ { "title": "…", "snippet": "…", "url": "…" } ],
  "customs": [ { "…": "community customs relevant to the question" } ],
  "sources": [
    {
      "ref": "string, Sefaria reference, e.g. 'Shulchan Arukh, Orach Chayim 318:1'",
      "title": "string",
      "lines": [ { "he": "string", "en": "string" } ]
    }
  ],
  "ai_cited_sources": [ "string, one per source the model cites, written 'Ref — one-line note'" ],
  "history_id": "string (uuid) | null, the stored ask_history row (the caller's account, or their device when signed out), for the /answer/<id> deep link and the Share button; null when nothing was stored (a cached answer, the model-failure fallback)",
  "meta": {
    "mode": "string",
    "language": "string",
    "community_lens": "string, the lens actually applied",
    "source_count": "number",
    "custom_count": "number",
    "knowledge_count": "number",
    "memory_count": "number",
    "identity_aware": "boolean",
    "generated_at": "number, unix seconds",
    "fallback": "boolean, true when the Claude fallback or a no-model fallback answered",
    "structured": "boolean",
    "is_prohibited": "boolean",
    "input_sanitized": "boolean, true when the question was altered by sanitising",
    "security": "object",
    "safety_class": "string, 'ok' unless the answer was classed otherwise",
    "rabbinic_disclaimer": "string",
    "async": true
  }
}
```

`sources` is trimmed for transfer (at most 8 entries, 3 lines each, 280 characters per line). Four cases return a `200` with a smaller `meta` and no model call:

| Case | Signal |
|---|---|
| Strict mode, no primary source matched with enough confidence | `meta.strict_blocked: true`, `meta.fallback: true`, `confidence: 0.2` |
| The global daily cost breaker is tripped | `meta.breaker_tripped: true`, `meta.fallback: true`, `confidence: 0.0`; a previously cached answer for the same question is served instead when one exists, with `meta.cached: true` |
| A prayer-service keyword (Shacharit, Mincha, Maariv, Kiddush, Havdalah) | A short pointer to the prayer sections, `confidence: 0.85`, `sources` naming Sefaria Liturgy |
| A question that is plainly not about Jewish law or learning (literature, trivia, coding, and the like) | A short refusal naming the subject, `confidence: 0`, no sources or customs; `meta.security.input.blocked: true`, `reasons: ["off_topic_subject"]` and `refusal_subject`. The keyword gate answers before any retrieval, so it costs no Sefaria, Supabase or model call; a question that gets past it can still be refused by the model's own `out_of_scope` verdict (the same refusal, with the model's answer, sources and customs dropped). The check is per question, so an off-topic question is refused even in the middle of a run of Torah questions, and the same applies to a follow-up in a conversation. |

**Errors:**

| Status | Meaning |
|---|---|
| `400` | `No valid question provided` |
| `401` | Authentication required (`CLERK_ENFORCE_AUTH` on and no valid token) |
| `402` | Daily AI usage limit reached for this account (per-user budget, `PER_USER_DAILY_BUDGET_USD`); try again after midnight UTC |
| `403` | Turnstile verification required or failed (anonymous callers past the hourly threshold; only when `TURNSTILE_ENABLED=true`) |
| `429` | Rate limit exceeded, either the per-minute bucket or the signed-in daily quota |
| `500` | An internal error occurred (reported to Sentry) |

When the primary provider (Gemini) fails the answer comes from the Claude fallback and `meta.fallback` is `true`. When both providers fail the answer is built from the retrieved sources alone, without a model summary.

**Live progress stream (opt-in).** A client that sends `Accept: application/x-ndjson` gets the same answer preceded by one line per pipeline step, so the UI can say what is happening while it waits. `POST /api/conversations/<id>/ask` behaves identically. Any other `Accept` header gets the single JSON response above, unchanged. Implementation: `backend/ask_progress.py` (server), `static/js/ask-progress.js` (client).

The body is newline-delimited JSON, one object per line:

```json
{"type":"stage","stage":"sources","state":"start"}
{"type":"stage","stage":"sources","state":"done"}
{"type":"ping"}
{"type":"result","payload":{ ...the JSON body above... }}
```

- `stage` is one of `sources`, `commentary`, `customs`, `times`, `thinking`; `state` is `start` or `done`. Steps overlap, and a step that fans out into several lookups reports one `start` and one `done`.
- `ping` is a keep-alive sent after 10 s of silence; clients ignore it.
- The last line is always a terminal `result` (the normal response body in `payload`; the conversation route sends `status` and `body`) or `error` (`status` plus `detail`). A stream that ends without one was interrupted.
- The HTTP status stays real. Streaming starts only after the pre-flight checks (validation, auth, Turnstile, budget) pass, so a refusal is still an ordinary `400`/`401`/`402`/`403`/`429`; an answer that never reports a step (the prayer shortcut) comes back as plain JSON. After streaming starts, failures arrive as an `error` line.
- A client that disconnects mid-stream does not cancel the answer: it still finishes, is recorded, and is charged as on the plain path.

**Source markers.** `answer` ties a claim to a source with a numbered marker, `[n]` (the 1-based position in `ai_cited_sources`), placed after the claim instead of naming the source in the prose: `"Kindling is forbidden on Shabbat.[1][2]"`. The server finalises the list in `backend/citation_markers.py` before it is returned or stored (empty entries dropped, a source named twice kept once, capped at 10, the way the conversation UI caps its citations) and renumbers every marker to match, so a number always points at the right entry; a marker whose source did not survive is removed rather than left dangling. Each marker sits at the end of the excerpt it backs (`place_markers`): a run of consecutive single sentences resting on the same source carries one marker, after the last of them ("Kindling is forbidden. Cooking too.[1][2]" rather than a `[1]` after every sentence), and a source cited again after a passage from a different source is shown again. A marker group is ascending and sits flush against the text it follows; code fences are left alone, and applying it twice changes nothing. Only digits count (`[1]`, `[1, 2]` and `[1-3]` are markers; `[2a]` and `[the Rema]` stay text). Answers stored before markers existed simply have none. When an earlier turn is shown to the model as history its markers are stripped, since their numbers belong to another answer's list.

The client (`static/js/citation-markers.js`, `citation-popover.js`) renders each marker as a small numbered chip, shows the sources once, in the sources area under the answer (it drops the duplicate trailing "Sources" list the server also puts in the answer's markdown), and opens a card on hover, click, tap or Enter with the reference, the model's one-line note, the first lines of the text, "Open in reader" and "Show in sources". A source the reader cannot open (not on Sefaria) shows plain text with a search link instead of a dead one.

**Which sources are pulled.** Sources come from what the question actually names or matches (`backend/sefaria.py: find_refs_for_question`); a question that matches nothing gets **no** Sefaria sources, not a stock fallback such as Orach Chayim 1 or Human Dispositions 1, and the answer then says so (an unmatched question may still reach the web-context tier, which carries its own warning). Community customs are included only when the custom is about the question's subject, not merely the same community, and a ref that failed to load (an `unavailable` stand-in) or has no text is dropped from the sources the model and the reader see (`backend/ask_payloads.py: is_usable_primary_source`).

**Reference spellings.** `GET /api/text/<ref>` answers an unresolvable reference with `200` and `{"error", "error_type": "not_found"}`. Before trying the reference as written it tries Sefaria's title for a transliterated Mishneh Torah reference (`Hilchot Shabbat 2:1` and `Rambam, Hilchot Shabbat 2:1` resolve as `Mishneh Torah, Sabbath 2:1`; table in `backend/ref_aliases.py`), which is how the model tends to write them.

**Outbound links.** Sources that Sefaria carries are opened in the site's own reader and are not linked to sefaria.org. Talmud and Tanakh references also link to AlHaTorah (`shas.alhatorah.org/Full/<Tractate>/<daf>`, the Bavli only, and `mg.alhatorah.org/Full/<Book>/<chapter>.<verse>`), with the tractate or book name normalised to the spelling each site accepts (`static/js/source-cards.js`: `alhatorahTractate`, `alhatorahBook`). Responsa and other works not on Sefaria link to a HebrewBooks search.

---

## Conversations

`backend/routes_conversations.py`. Multi-turn threads, separate from the single-shot answer history. Stored in the `conversations`, `messages` and `citations` tables, read through the caller's RLS-scoped Supabase client (service-role fallback only when `STRICT_SUPABASE_RLS` is off), and **every route also filters on the caller's own verified `sub` at the application layer**, so a guessed or foreign id is a `404`, never a leak. All routes are **Clerk**. Class: `cheap`, except `/ask` (see below). Cache: `private, no-store`.

| Route | Purpose |
|---|---|
| `POST /api/conversations` | Start a thread. Body: `minhag` (optional string, the community lock, cut to 80 characters) and `from_history_id` (optional, one of the caller's own `ask_history` ids). With `from_history_id` the stored question and answer become the first turn, read from the stored row and never from the request, and the thread locks to the community that answer used. Returns `201` with the conversation, plus `messages` when seeded. |
| `GET /api/conversations` | The sidebar list: pinned first, then most recently updated, soft-deleted threads excluded. Query: `limit` (1–50, default 20). Returns `{"items": [{id, title, title_is_custom, minhag, pinned_at, created_at, updated_at}]}`. |
| `GET /api/conversations/<id>` | One thread with every live turn, each with its `citations` embedded (`id, ordinal, source_ref, excerpt_he, excerpt_en, url`). Turns superseded by a retry are excluded. |
| `POST /api/conversations/<id>/messages` | Append one turn. Body: `role` (`user` or `assistant`), `status` (`streaming`, `complete`, `stopped`, `error`; default `complete`), `content`, and an optional `citations` list. Bumps the thread's `updated_at`. Returns `201` with the message. |
| `PATCH /api/conversations/<id>` | Rename (`title`) and/or pin (`pinned`: boolean). The community lock is deliberately not editable; switching community means a new thread. `400` when neither field is sent. |
| `DELETE /api/conversations/<id>` | Soft delete (sets `deleted_at`), so the client can offer Undo. Returns `{"ok": true}`. |
| `POST /api/conversations/<id>/restore` | Undo a soft delete. Owner-only and idempotent: restoring a thread that is not deleted returns it unchanged. |
| `POST /api/conversations/<id>/ask` | Ask a follow-up with real multi-turn context. |
| `GET /api/conversations/<id>/share` | The owner's share state: `{"shared": true, "share_token": "…", "path": "/a/<token>"}`, or `{"shared": false}`. See [Conversation sharing](#conversation-sharing). |
| `POST /api/conversations/<id>/share` | Make the conversation public as it stands now. |
| `DELETE /api/conversations/<id>/share` | Stop sharing. |

### `POST /api/conversations/<id>/ask`

- **Rate-limit class:** `llm` (fails closed), carved out ahead of the `cheap` prefix by a route pattern, so a thread answer is metered exactly like `/ask`.
- **Body:** `question` (required unless retrying), `mode`, `language`, and optionally `retry_of`.
- The thread's prior complete turns go into the prompt. The community comes from the conversation row, never from the request body (the minhag lock).
- `retry_of`: the id of a `status: "error"` assistant message in this thread. The failed turn is re-answered without writing a new user turn, the body's `question` is ignored, and the error row is then superseded so `GET` stops returning it. The `201` body gains `superseded_message_id`.
- Streams like `POST /ask` when the client sends `Accept: application/x-ndjson` (the terminal line carries `status` and `body`).
- Applies the same two cost gates as `/ask`, checked before the user turn is written, so a refused ask leaves the thread untouched.

**Response (201):**

```json
{
  "user_message": "the stored user turn (absent content change on a retry, which writes none)",
  "assistant_message": "the stored assistant turn, with its citations",
  "conversation": "the conversation header after this turn (the title may just have been derived)",
  "superseded_message_id": "present only on a retry"
}
```

**Errors:** `400` (no valid question, invalid `retry_of`), `401`, `402` `{"code": "daily_budget_exhausted"}`, `404` (unknown or foreign conversation), `429`, `503` `{"code": "ai_paused"}` with a `Retry-After` to the next UTC midnight when the global breaker is tripped. There is no cached-answer fallback here: a thread's answer depends on the thread's own history.

---

## Answer history and sharing

### History (`backend/routes_user.py`)

Stored in `ask_history`, which is **service-role only** (RLS on, no policies); every route filters on the caller's owner id: the verified `sub` of a signed-in caller, and/or the `device:` owner a signed-out caller's cookie names (see [Device identity](#device-identity)). The list route is **Clerk** only; the per-entry routes (`GET`/`DELETE /api/user/history/<id>` and the share routes below) take **Clerk or a device cookie**. `cheap`; `private, no-store`.

| Route | Purpose |
|---|---|
| `GET /api/user/history` | One page of the caller's answers, newest first. Query: `limit` (1–50, default 20), `cursor` (the previous page's `next_cursor`; an invalid one is a `400`), `q` (case-insensitive substring of the question, up to 200 characters). Returns `{"items": [...], "next_cursor": "string \| null"}`. |
| `GET /api/user/history/<id>` | One entry, so `/answer/<id>` can hydrate it. A signed-in caller reads their account's entries and any saved under their device; a signed-out caller with a device cookie reads that device's. A malformed id, a missing id and someone else's id all answer the same `404`; no account and no device cookie is a `401`. |
| `DELETE /api/user/history/<id>` | Delete one entry (same owners as the read). Returns `{"ok": true}`. |

An entry holds `id, question, answer, sources, ai_cited_sources, community, mode, language, created_at`. Entries older than 90 days are removed by the retention job.

### Sharing (`backend/routes_answer_share.py`)

The owner can mint a public link for one stored answer, signed in or on the device that asked it; anyone holding `/a/<token>` can read it without signing in. The owner's own address-bar link (`/answer/<id>`, `/chat/<id>`) stays private: it opens only for the account, or the device, that saved it, and a signed-out answer's link needs that device's cookie. The token is unguessable (`secrets.token_urlsafe(16)`); the public read selects only answer columns and never `user_id` or the row id. These need the columns from `scripts/sql/migrate_ask_history_share.sql`; until it has been applied they answer `503 {"code": "share_unavailable"}`.

| Route | Auth | Purpose |
|---|---|---|
| `GET /api/user/history/<id>/share` | Clerk or device | The owner's share state for the answer: `{"shared": true, "share_token": "…", "path": "/a/<token>"}`, or `{"shared": false}`. |
| `POST /api/user/history/<id>/share` | Clerk or device | Make the answer public. Idempotent: an already-shared answer keeps its token (`200`), otherwise a new one is minted (`201`). The write is a compare-and-set, so two concurrent shares cannot overwrite a link someone has already copied. |
| `DELETE /api/user/history/<id>/share` | Clerk or device | Stop sharing. The token is cleared and the old link `404`s for good; sharing again mints a new one. The owner's own `/answer/<id>` link is untouched. Returns `{"shared": false}`. |
| `GET /api/public/answer/<token>` | Public | The shared answer (or, for a conversation token, the shared conversation, below): `question, answer, sources, ai_cited_sources, community, mode, language, created_at`, `meta` (`safety_class`, `mode`, `language`) and `public: true`. A malformed, unknown, revoked or private token all answer the same `404`, so the response never says whether a token once existed. Always `noindex`. Rate-limit class `fanout` (per IP). |


### Conversation sharing

`backend/routes_conversation_share.py`. A whole multi-turn conversation can be shared with the **same `/a/<token>` link shape** as an answer: `GET /api/public/answer/<token>` looks for a stored answer with that token first and, finding none, for a live conversation. One link shape, one router key and one public viewer cover both. The owner routes are **Clerk** (a conversation always belongs to an account), use the caller's RLS-scoped client and also filter on the verified `sub`; no migration is needed (`conversations.share_id`, `share_snapshot_msg_id`, `shared_at` come from `scripts/sql/conversations_setup.sql`).

| Route | Purpose |
|---|---|
| `GET /api/conversations/<id>/share` | `{"shared": true, "share_token": "…", "path": "/a/<token>"}` or `{"shared": false}`. `404` for an unknown or foreign conversation. |
| `POST /api/conversations/<id>/share` | Share the chat **as it stands now**. The first share mints an unguessable token (`201`); sharing again keeps the token and moves the snapshot to the newest finished turn (`200`), so a link already sent shows the newer turns only when the owner chooses to. A first share is a compare-and-set, so two concurrent shares cannot overwrite a copied link. A conversation with no finished turn is `409 {"code": "empty_conversation"}`. |
| `DELETE /api/conversations/<id>/share` | Stop sharing: the token, snapshot and `shared_at` are cleared, the old link `404`s for good, and sharing again mints a new token. The owner's `/chat/<id>` is untouched. Returns `{"shared": false}`. |

A shared conversation is a **snapshot**: turns added after the share are not public until the owner shares again. The public payload from `GET /api/public/answer/<token>` is `{"conversation": true, "title", "minhag", "created_at", "shared_at", "messages": [{"role", "content", "created_at", "citations": [{"ordinal", "source_ref", "excerpt_he", "excerpt_en", "url"}]}], "public": true}`: only turns up to and including the pinned message, finished and not superseded, oldest first (at most 200), and never a user id or any row id. Same `404`, `noindex` and `fanout` rate limit as an answer token.

### Device identity

`backend/device_identity.py`. A signed-out visitor has no account, so their answers are tied to their **device** instead, which is what makes the private `/answer/<id>` link work when signed out.

- The first successful signed-out `/ask` (a request without a bearer token) sets the cookie `shelah_device`: 32 random URL-safe characters, `HttpOnly`, `SameSite=Lax`, `Path=/`, one year, `Secure` in production or whenever the request came over HTTPS. A signed-in answer never sets or reads it.
- The answer is saved in `ask_history.user_id` as `device:<sha256 of the cookie>`. Only the hash is stored, and the `device:` prefix cannot collide with a Clerk `sub` (`user_…`), so no migration is needed. The cookie is the secret: it is never sent anywhere but this site and carries no profile or tracking id.
- `GET`/`DELETE /api/user/history/<id>` and the answer share routes accept the account and the device together (`require_history_owner`), so signing in on the same browser keeps the signed-out answers reachable. `GET /api/user/history` (the list) stays account-only; there is no signed-out history list.
- `SameSite=Lax` is also what keeps the state-changing device routes (share, revoke, delete) safe from CSRF: a cross-site request does not carry the cookie.
- Signed-out answers fall under the same 90-day retention as signed-in ones.

---

## Rate limiting & abuse mitigation

Every route is covered by the identity-aware rate limiter (`backend/rate_limit.py`, an ASGI middleware on the FastAPI app, so it fronts both native and Flask routes). `/static/*` is exempt. `POST /ask` and `POST /api/conversations/<id>/ask` additionally carry the Turnstile gate for anonymous callers (`/ask` only).

### Rate limiting (`backend/rate_limit.py`)

Each route is classified by path prefix (first match wins; a route pattern for `/api/conversations/<id>/ask` is checked before the prefixes) and everything unlisted is `cheap`. The bucket key is **identity-aware**: the Clerk `sub` when the caller is authenticated, the trusted-IP key (`X-Vercel-Forwarded-For`, then `X-Forwarded-For`, then `X-Real-IP`, then the socket address; never a spoofable header alone) otherwise, so a signed-in caller and an anonymous caller behind the same IP land in separate buckets.

| Class | Per minute | On store outage | Routes |
|---|---|---|---|
| `llm` | 20 anonymous, 40 signed in, plus 200/day signed in | fails **closed** | `/ask`, `/api/conversations/<id>/ask` |
| `heavy` | 10 | open | `/api/export/chapter`, `/api/siddur/full/*` |
| `fanout` | 30 | open | `/api/library/search`, `/api/text/*`, `/api/word/meaning`, `/api/geocode`, `/api/public/answer/*` |
| `sidebar` | 90 | open | `/api/sidebar/*` |
| `feedback` | 10 | open | `/api/feedback` |
| `telemetry` | 10 | open | `/api/client-errors` |
| `account` | 5 (keyed by Clerk id) | open | `/api/user/delete-account`, `/api/user/data-export` |
| `webhook` | 15 (per IP) | open | `/api/webhooks/clerk` |
| `cheap` | 120 | open | everything else, including `/api/conversations*` CRUD and `/api/siddur/v2/*` |

Anonymous callers have no daily cap, only the tighter per-minute IP bucket. A signed-in caller's daily quota is an independent counter.

**429 response body:**

```json
{
  "error": "Rate limit exceeded. Please wait before sending another request.",
  "code": "rate_limited"
}
```

**`Retry-After`:** seconds until the caller may retry, always present on a `429`: `60` (the window) for a per-minute rejection, or `86400` when a signed-in caller's *daily* quota is what rejected the request.

**Store outage posture:** the `llm` class fails **closed**, because an unmetered `/ask` during an outage is a budget hole. Every other class fails **open**, so a reader is not blocked by a transient Redis blip. Redis failures open a 20-second circuit breaker and are reported once per cooldown rather than once per request.

An earlier version of this document named a `RATE_LIMIT_PER_MIN` variable; that variable never existed. The mechanism is this middleware. A Vercel WAF rate-limit layer sits in front of all of it (`docs/SECURITY.md` §7).

### Turnstile (`backend/turnstile.py`)

Anonymous `/ask` traffic past a per-IP hourly request threshold is challenged with Cloudflare Turnstile (chosen over Vercel BotID; `docs/SECURITY.md` §8 has the reasoning). Signed-in callers never see this gate; they already have Clerk signup plus the daily quota above. Below the threshold, or whenever `TURNSTILE_ENABLED` is unset (the default), this is a true no-op.

**403 response body (a challenge is owed and no valid token was supplied):**

```json
{
  "error": "Verification required before continuing.",
  "code": "turnstile_required"
}
```

Supply a solved token in the request body's `turnstile_token` field to pass the gate.

### Env var matrix

| Variable | Default | Purpose |
|---|---|---|
| `RATELIMIT_ENABLED` | `true` | Kill switch for the whole rate-limit middleware. |
| `RATE_LIMIT_REDIS_URL` | unset | Upstash Redis (`rediss://`) shared store. Falls back to a per-process in-memory store with a loud startup warning when unset, which is not a real cross-instance limit on Vercel Fluid. |
| `TURNSTILE_ENABLED` | `false` | Kill switch for the anonymous-`/ask` Turnstile gate; every function in `backend/turnstile.py` is a no-op when unset. |
| `TURNSTILE_SECRET_KEY` | unset | Cloudflare Turnstile server secret. Required once `TURNSTILE_ENABLED=true`; with the flag on and this empty, every challenged request is rejected (fails closed). |
| `TURNSTILE_SITE_KEY` | unset | Cloudflare Turnstile public site key (not a secret; for the frontend widget). |
| `TURNSTILE_ANON_HOURLY_THRESHOLD` | `5` | Anonymous requests per IP per trailing hour before a challenge is owed. |
| `PER_USER_DAILY_BUDGET_USD` | `2.00` | Per-signed-in-caller daily AI spend ceiling, a good-faith guardrail rather than an anti-abuse control. `0` disables it. |
| `DAILY_BUDGET_USD` | unset (breaker inert) | Global cross-caller daily spend ceiling (`backend/cost_meter.py`), the real ceiling against a multi-account attacker. |

---

## Library and texts

Texts come from Sefaria through `backend/sefaria_library.py`, behind a circuit breaker, a memory-plus-disk TTL cache and a block detector. All routes here are **Public** `GET`s unless noted.

### `GET /api/library/index`

The Sefaria library table of contents, with removed texts pruned and fix refs applied. Cache: immutable tier.

### `GET /api/library/category/<category>`

Every book in one library category. Cache: immutable tier.

### `GET /api/library/leaf-refs`

Section references for one work, to power the section-grid selectors. Query: `title` (the index title) and `max` (default 140, 1–800). Returns `{"title", "refs": [...], "sections": [...]}`; `sections` groups large works into labelled ranges (Talmud daf ranges, Shulchan Arukh topics) where the index carries them. An empty `title` returns empty lists. Cache: immutable tier.

### `GET /api/library/popular`

Curated popular texts per category. Cache: immutable tier.

### `GET /api/texts-index`

The complete browsable index (prayers, communities, Sefaria texts) for the top menu. Cache: immutable tier.

### `GET /api/text/<ref>`

One text, Hebrew and English with metadata. `<ref>` is a Sefaria reference, URL-encoded as needed (`Berakhot.2a`, `Shulchan%20Arukh%2C%20Orach%20Chayim%201%3A1`). Class `fanout`. Cache: immutable tier.

- **Query:** `autotranslate` (default on; `0`, `false`, `no` or `off` disables it). Lines with no English are machine-translated unless disabled.

**Response (200):**

```json
{
  "ref": "string, the resolved reference",
  "title": "string",
  "heTitle": "string",
  "heRef": "string, Sefaria's Hebrew spelling (may be empty)",
  "he": ["string, Hebrew lines"],
  "en": ["string, English lines"],
  "lines": [ { "he": "…", "en": "…" } ],
  "sections": [],
  "sectionNames": [],
  "next": "string | null",
  "prev": "string | null",
  "categories": [],
  "authors": [],
  "era": "string"
}
```

An unresolvable reference answers `200` with `{"error", "error_type": "not_found", "ref", "he": [], "en": []}` (and is not cached). While Sefaria is actively blocking requests the answer is `503` with `error_type: "sefaria_blocked"`. A request with no reference (`/api/text/`) is a `400`.

### `GET /api/text/<ref>/links`

Linked commentaries and parallel texts for a reference, as `{category: [item]}`. Class `fanout`. Cache: immutable tier.

### `GET /api/text/<ref>/graph`

A lightweight source graph around a reference: `{"ref", "nodes": [{id, label, kind, category?}], "edges": [{source, target, label}]}`, with at most 14 links per category. Class `fanout`. Cache: immutable tier.

### `GET /api/sidebar/<ref>`

Slim commentary data for the reader's sidebar, in two cached stages, so the client can load it for the verse being read before anything is selected (`static/js/commentary-preload.js`). Logic in `backend/sidebar_bundle.py`.

- **Path parameter:** `ref`, a URL-encoded passage reference such as `Genesis%201%3A1` (at most 200 characters).
- **Query:** `stage`, `links` (default) or `texts`.
- **Rate-limit class:** `sidebar` (90 per minute, fails open). A reader moving through a chapter makes about two requests per verse.

**`stage=links` response (200):** the commentary, targum and midrash refs only, in the shape `GET /api/text/<ref>/links` uses (`{category: [item]}`), without the rest of Sefaria's `/related` payload (about a third of the size for Genesis 1:1). Ordered commentary, then targum, then midrash, de-duplicated, capped at 800 refs.

```json
{
  "ref": "Genesis 1:1",
  "links": {
    "Commentary": [
      { "ref": "Rashi on Genesis 1:1:1", "heRef": "...", "collectiveTitle": { "en": "Rashi", "he": "..." } }
    ],
    "Targum": [ { "ref": "Onkelos Genesis 1:1" } ]
  }
}
```

**`stage=texts` response (200):** the text of the first few passages (3 per commentator) of the four most-read commentators present on that verse (priority list `COMMENTARY_PRIORITY`), fetched in parallel and keyed by the commentary's own ref. The first three passages carry a machine-generated English translation and `"translation_attempted": true`; the rest are untranslated and are translated on demand by the client. Passages over 60 KB, and anything past a 160 KB bundle total, are left out; stragglers still running after 3 s are left out and retried on the next request (a partial result is cached for 60 s instead of the usual 30 min).

```json
{
  "ref": "Genesis 1:1",
  "texts": {
    "Rashi on Genesis 1:1:1": {
      "ref": "...", "title": "...", "heTitle": "...", "heRef": "...",
      "lines": [ { "he": "...", "en": "..." } ],
      "translation_attempted": true
    }
  }
}
```

Both stages are cached per ref server-side, and concurrent requests for one ref share a single upstream fan-out.

| Status | Meaning |
|---|---|
| `400` | Missing or overlong `ref`, or an unknown `stage` |
| `429` | Rate limit exceeded (`Retry-After: 60`); the client stops speculative requests for 30 s |

### `GET /api/library/search`

Full-text search across the library, with removed texts filtered out. Class `fanout`. Cache: corpus-derived tier.

- **Query:** `q` (required for results; empty returns `[]`), `size` (default 10, 1–50), plus the optional metadata filters read by `_extract_search_metadata_filters`.
- **Response (200):** a JSON array of hits, each with `ref`, `heRef`, `text` (the matched title or label), `categories`, `path`, `authors` and `era`. Matches come from Sefaria's name completion and the library catalogue, not a body-text index.

### `GET /api/search/suggest`

Omnibox suggestions: popular Torah aliases, communities, prayer books, text hits, and an "Ask Sh'elah" option. Cache: corpus-derived tier.

- **Query:** `q` (empty returns `[]`), `size` (default 8, 1–20).
- **Response (200):** an array, best first, of `{"type": "text" | "prayer" | "community" | "ask", "label", "label_he", "value", "subtitle", "subtitle_he", "score"}`.

### `GET /api/word/meaning`

A best-effort meaning for a highlighted Hebrew or English word. Class `fanout`. Cache: corpus-derived tier.

- **Query:** `word` (required; `400` when missing) and `lang` (`en` or `he`; a Hebrew source word always answers in English).
- **Response (200):** the primary meaning, any alternatives, the `source`, `lang`, `status: "ok"`, and `machine_translated`, which is `true` when the definition came from an online translation fallback so the client never presents it as a curated entry.

### `POST /api/export/chapter`

Export a chapter's lines as a file. **Optional** auth. Class `heavy`.

**Request body:**

```json
{
  "title": "string",
  "ref": "string",
  "format": "'txt' | 'docx' | 'pdf' (default 'txt')",
  "lines": [ { "segment": "…", "he": "…", "en": "…" } ]
}
```

**Response (200):** the file as an attachment (`text/plain`, a Word document or a PDF), named from the title. **Errors:** `400` for an unsupported format or no usable lines.

### `GET /api/diagnostics/sefaria`

A live availability probe of the upstream Sefaria API (the v3 and v2 endpoints, plus any cached block information). `200` when Sefaria is available overall, `503` otherwise.

---

## Prayers and siddur

### Siddur v2 (`backend/routes_siddur.py`)

The in-app siddur reads these. Every response is a pure function of its URL (the text is checked-in data in `backend/siddur_data.py`, and the day guidance is computed from the date and the Israel flag alone in `backend/siddur_day.py`), so all three are publicly cacheable. Class `cheap`. **Public**.

| Route | Response |
|---|---|
| `GET /api/siddur/v2/toc/<rite>` | The curated table of contents for a rite, including a `version`. `404` for an unknown rite. Hour-long cache tier, because it is fetched without a version key (it is what carries the version). |
| `GET /api/siddur/v2/service/<rite>/<service>` | One whole service of typed lines. The client passes the toc's `version` as `?v=`, which the server ignores but which gives each data version its own cache key. `404` for an unknown rite or service. Immutable tier. |
| `GET /api/siddur/v2/day?date=YYYY-MM-DD&il=0\|1` | The day's prayer guidance for that date, with `il=1` for Israel. `400` for a malformed date, an `il` other than `0` or `1`, or a date outside 1900-01-01 … 2099-12-31. Immutable tier. |

### Prayer books and legacy services (`backend/routes_prayers.py`)

| Route | Response |
|---|---|
| `GET /api/prayers/list` | `[{"name", "title", "source": "legacy-service" \| "sefaria-liturgy"}]`: the quick services plus Sefaria's Liturgy books. The Sefaria index the checked-in siddur is built from is left out, since the siddur reader already serves it. Corpus-derived tier. |
| `GET /api/prayer/<name>` | A preview of one prayer book or service, in English and Hebrew. `404` when the name maps to no reference. Immutable tier. |
| `GET /api/siddur/full/<name>` | The full prayer text, fetched from Sefaria in parallel and combined: `{"prayer", "lines", "sources"}`. `404` when unmapped or when nothing could be fetched. Class `heavy`. Immutable tier. |
| `GET /api/siddur/section-refs/<name>` | `{"prayer", "sources": [refs]}`, the references `/api/siddur/full/<name>` would fetch, without fetching them. Kept for old bookmarks and API consumers; nothing in the client calls it. Immutable tier. |

---

## Calendar and location

`backend/routes_calendar.py`, backed by `backend/zmanim_engine.py` and Hebcal. All **Public**; none make an AI call.

A location is a `lat`/`lon` pair, validated as numeric and in range (`-90…90`, `-180…180`). A parameter that is present but invalid is a `400`; an absent one falls back to the session location (below) or the default engine location.

### `POST /set_location`

Store the caller's location in the Flask session cookie (`SameSite=Lax`, `HttpOnly`, `Secure` in production). Body: `{"lat": number, "lon": number}`. Returns `{"status": "success", "lat", "lon"}`. Only same-origin requests are accepted (`403` otherwise); `400` for invalid coordinates. Cache: `private, no-store`.

### `GET /api/geocode`

Server-side proxy for the "search by city" box (the page's CSP does not allow calling Nominatim directly, and Nominatim requires an identifying User-Agent). Query: `q` (required) and `lang` (`he` or `en`). Returns `{"results": [{"lat", "lon", "display_name"}]}` with at most one result, or an empty `results` when nothing matched. Errors: `400` (no `q`), `502` (lookup failed), `503` (circuit open). Class `fanout`. Corpus-derived tier.

### `GET /api/zmanim`

Halachic times for a location. Query: `lat`, `lon` and `community` (default `standard`). Returns the engine's times for that day. Same-origin callers with explicit coordinates also have the location remembered in the session.

### `GET /api/zmanim/month`

The month's zmanim as calendar events, same location rules as `/api/zmanim`.

Both routes above fall back to the **session** location when `lat`/`lon` are absent, which makes that response user-specific; the cache layer forces it to `private` (`g.cache_tier_force_private`), so one user's location can never be served from the CDN to another.

### `GET /api/zmanim/days`

Clock times (dawn, sunset, nightfall, candle lighting, havdalah) for specific dates, as ISO timestamps in the location's own timezone or `null` where they do not exist. `lat` and `lon` are **required** (no session fallback, no session write), and `dates` is 1–16 comma-separated `YYYY-MM-DD` values between 1900 and 2200. Returns one entry per date. Errors: `400` for missing coordinates or a bad `dates`. Because it is a pure function of its URL it is publicly cacheable.

### `GET /api/daily-study`

Today's daily refs (Daf Yomi, Rambam and related), straight from Sefaria's daily-study data. It never touches the session, which keeps the response cacheable.

### `GET /api/holidays`

Jewish holiday events for the calendar, from Hebcal. Query: `year` (default the current year, 1583–3000). Returns a JSON array of calendar events: `title` (emoji-prefixed), `start`, `allDay`, `display`, `category`, `color`, `textColor` and `detail`. If Hebcal is down or its circuit is open, a fallback chain answers instead of an error.

### `GET /api/parasha`

The current weekly portion. Returns `{"title", "heTitle", "ref", "source"}`, where `source` is `sefaria-calendars`, `calendar-fallback` or `default-fallback` depending on what answered.

---

## Communities

`backend/routes_community.py`, reading the checked-in `customs/*.json` files. All **Public** `GET`s.

| Route | Response |
|---|---|
| `GET /api/communities/list` | `[{"name": "Ashkenaz"}, …]`, the supported community names, sorted. Corpus-derived tier. |
| `GET /api/communities` | Alias of the list above, kept for older clients. |
| `GET /api/community/<name>` | One community's customs: `name`, `requested_name`, `heritage_id`, `primary_origin`, `customs` (keyed `category_topic`, each with `category`, `topic` (original casing), `topic_he`, `ruling`, `ruling_he`, `common_practices`, `source`, and, where the file has them, `source_url`, `references`, `variants` and `confidence`; the `_he` fields are hand-written Hebrew, or an empty string), `major_authorities` (short names from the source registry), `distinctive_customs` (each with `name`, `name_he`, `description`, `description_he`, `when`, `source`, `source_url`, `confidence`), `disputes`, `gaps`, and the full `raw_data`. Reviewer-only fields (`review_notes`, `needs_rabbinic_review`) are stripped from every part of the response, `raw_data` included. The name accepts aliases and is canonicalised; an unknown name is a `404`, an unreadable file a `500`. |
| `GET /api/community/<name>/timeline` | `{"name", "events": [{"title", "description", "approx_period"}]}`, at most 30 events built from the community file's origin and history fields. |

---

## Feedback

### `POST /api/feedback`

Submit a thumbs up or down (and an optional short comment) on an AI answer. Writes one row to the `answer_feedback` table (`backend/routes_feedback.py`). Accepted from signed-out readers too, since `user_id` is nullable. The table is write-only from the client's side: its RLS policy grants `INSERT` to everyone and has no `SELECT` policy for `anon` or `authenticated`, so submitted feedback can only be read back through the backend's own service-role client (see `GET /api/devtools/feedback-digest`).

- **Auth:** Optional. A verified token's `sub` is attached as `user_id`.
- **Rate-limit class:** `feedback`.

**Request body:**

```json
{
  "verdict": "string, required, 'helpful' | 'not_helpful'",
  "question": "string, required, the original question (hashed server-side with SHA-256 into question_hash; the raw text is not persisted)",
  "comment": "string, optional, sanitised and truncated to 500 characters; stored as '' if omitted",
  "mode": "string, optional (default 'balanced')",
  "language": "string, optional (default 'en')",
  "fallback": "boolean, optional, whether the answer came from the fallback model (default false)",
  "safety_class": "string, optional (default 'ok')"
}
```

**Response (200):** `{"success": true}`

| Status | Meaning |
|---|---|
| `400` | `verdict` missing or not `helpful`/`not_helpful`, or `question` missing |
| `401` | No token and `CLERK_ENFORCE_AUTH` on, or a token that failed verification |
| `500` | The insert into `answer_feedback` failed |
| `503` | Supabase not configured |

---

## Account

`backend/routes_user.py`.

### `GET /api/auth/me`

**Public.** `{"authenticated": false}` with no token; `{"authenticated": true, "user_id", "session_id"}` for a valid one; `401 {"authenticated": false}` for a token that does not verify.

### `GET /api/user/preferences`, `PUT /api/user/preferences`

**Clerk.** Cross-device sync of the caller's UI state, stored in `user_preferences` through the RLS-scoped client (service-role fallback only when `STRICT_SUPABASE_RLS` is off; in strict mode with no Supabase session the answer is `403`). `GET /api/preferences` and `PUT /api/preferences` are an alias kept for older clients.

- `GET` returns `{"prefs", "shelf", "notes", "reading_state", "updated_at"}`, each `null` when nothing is stored.
- `PUT` takes `{"prefs": object (required), "shelf": object, "notes": object, "reading_state": object}` (the last three default to `{}`) and returns `{"ok": true, "updated_at"}`. A non-object value is a `400`.
- `500` when the sync fails; `503` when Supabase is not configured.

### `GET /api/bookmarks/semantic`, `POST /api/bookmarks/semantic`

**Clerk.** Semantic bookmarks with notes and an AI summary, stored in `study_bookmarks`.

- `GET` returns `{"items": [{id, ref, label, segment_text, ai_summary, notes, created_at}]}`, newest first, up to 50.
- `POST` takes `ref`, `label`, `segment_text`, `notes` and an optional `ai_summary` (a reference or a text segment is required, else `400`). When `segment_text` is given and no summary is, one is generated with Gemini. Returns `{"ok": true, "item", "summary_generated": boolean, "summary_error": string}`.

### `POST /api/accept-legal`

**Optional.** Records acceptance of the terms and privacy policy versions and the age attestation. **Currently dormant:** no client code calls it, but the route and its `user_preferences` columns remain. Anonymous callers get `{"success": true, "stored": "client"}`; signed-in callers get `{"success": true, "stored": "server"}` after a best-effort write.

---

## Privacy

`backend/routes_privacy.py`: the self-serve GDPR/CCPA data-subject-request flow, "download my data" and "delete my account and data". Both operate over the same seven user-scoped Supabase tables (`_USER_DATA_TABLES`), each filtered or deleted with an explicit `.eq("user_id", ...)`. Both are **Clerk**, class `account`, `private, no-store`.

| Export key | Table |
|---|---|
| `preferences` | `user_preferences` |
| `bookmarks` | `study_bookmarks` |
| `ask_history` | `ask_history` |
| `memories` | `user_memories` |
| `ai_usage_log` | `ai_usage_log` |
| `feedback` | `answer_feedback` |
| `ai_conversations` | `conversations` (the export embeds each conversation's `messages`, each with its `citations`; delete cascades to both) |

Both endpoints use the service-role Supabase client rather than the RLS-scoped, JWT-derived one: the frontend sends a plain Clerk session token, not a Supabase-compatible JWT, so gating a DSR endpoint behind the RLS-scoped client would `403` for exactly the users the feature exists to serve. Every query is still filtered to the caller's own verified `sub`.

### `GET /api/user/data-export`

One JSON export of every row across the seven tables that belongs to the signed-in user. Each table is fetched independently and paginated (`.range()`, 1,000 rows per page, up to 200 pages), so a high-volume table (`ai_usage_log`, `ask_history`) is not silently truncated by PostgREST's default row cap. A failure reading one table does not fail the export: that table's array comes back empty and its export key is listed in `partial_errors`.

**Response (200):**

```json
{
  "user_id": "string, Clerk user ID",
  "exported_at": "string, ISO 8601, UTC",
  "data": {
    "preferences": ["raw user_preferences rows"],
    "bookmarks": ["raw study_bookmarks rows"],
    "ask_history": ["raw ask_history rows"],
    "memories": ["raw user_memories rows"],
    "ai_usage_log": ["raw ai_usage_log rows"],
    "feedback": ["raw answer_feedback rows"],
    "ai_conversations": ["raw conversations rows, each with `messages` (oldest first, each with numbered `citations`)"]
  },
  "partial_errors": {
    "<export_key>": "string, present only if that table failed to read"
  }
}
```

| Status | Meaning |
|---|---|
| `401` | Not authenticated, or no user identity in the token claims |
| `503` | Supabase not configured |

### `POST /api/user/delete-account`

Irreversible. Deletes the caller's rows from all seven tables above, then, only if every table delete succeeded, deletes the Clerk identity through Clerk's Backend API (`DELETE https://api.clerk.com/v1/users/{id}`, needs `CLERK_SECRET_KEY`; a `404` from Clerk counts as already deleted). Table deletes are naturally idempotent, so a retry after a partial failure is safe.

If any table delete fails, the Clerk account is deliberately **left intact** and the response says so: the Clerk identity is the user's only way to authenticate and retry, so deleting it while rows remain would orphan that data with no self-serve recovery.

**Request body:** `{"confirmation": "DELETE"}` (the literal string, required).

**Response (200, full success):**

```json
{
  "ok": true,
  "deleted_tables": {
    "user_preferences": true,
    "study_bookmarks": true,
    "ask_history": true,
    "user_memories": true,
    "ai_usage_log": true,
    "answer_feedback": true,
    "conversations": true
  },
  "clerk_deleted": true
}
```

**Response (207, a table delete failed; Clerk account untouched):**

```json
{
  "ok": false,
  "deleted_tables": { "<table>": "boolean per table, as above" },
  "clerk_deleted": false,
  "table_errors": { "<table_name>": "string, generic error message" },
  "clerk_skipped": "Clerk account was not deleted because one or more data tables failed to delete. Please retry."
}
```

**Response (207, every table deleted but the Clerk delete failed, for example `CLERK_SECRET_KEY` is unset):** the same shape with `"ok": false`, `"clerk_deleted": false` and `"clerk_error": "string"`.

| Status | Meaning |
|---|---|
| `400` | `confirmation` missing or not exactly `"DELETE"` |
| `401` | Not authenticated, or no user identity in the token claims |
| `503` | Supabase not configured |
| `207` | Partial success, see above |

---

## Webhooks

### `POST /api/webhooks/clerk`

`backend/routes_webhooks.py`. A completeness backstop for account deletion: `POST /api/user/delete-account` is the only code path in this app that deletes a user's Supabase rows, but a Clerk identity can also be deleted through doors the app does not control (Clerk's hosted `<UserProfile />` self-service delete, or an operator removing a user in the Clerk Dashboard). Neither calls this app, so neither would trigger cleanup without this webhook. On a `user.deleted` event it cascades the same delete across the same seven tables listed under [Privacy](#privacy), keyed on the payload's `data.id`; there is no live Clerk JWT to authenticate with, because the identity may already be gone.

- **Auth:** Svix signature. The request must carry `svix-id`, `svix-timestamp` and `svix-signature`. The signature is HMAC-SHA256 over `"{svix-id}.{svix-timestamp}.{raw body}"` with `CLERK_WEBHOOK_SIGNING_SECRET` (base64-decoded after stripping a `whsec_` prefix) and must match one of the space-delimited `v1,<base64>` values in `svix-signature`. `svix-timestamp` must be within 300 seconds of the server's clock in either direction. Verified by hand against the documented Svix scheme rather than adding the `svix` package as a dependency.
- **Rate-limit class:** `webhook` (15 per minute per IP).

**Request body:** a Clerk webhook event, for example:

```json
{
  "type": "user.deleted",
  "data": { "id": "string, Clerk user ID" }
}
```

Only `type == "user.deleted"` triggers the cascade. Any other event type (including Clerk's own setup test payload) is acknowledged with `200` and skipped, so Clerk does not retry it forever.

**Response (200, cascade succeeded):**

```json
{
  "ok": true,
  "deleted_tables": {
    "user_preferences": true,
    "study_bookmarks": true,
    "ask_history": true,
    "user_memories": true,
    "ai_usage_log": true,
    "answer_feedback": true,
    "conversations": true
  }
}
```

**Response (200, other event types):** `{"ok": true, "skipped": "<event type or 'unrecognized_payload'>"}`.

**Response (207, one or more table deletes failed):** `{"ok": false, "deleted_tables": {...}, "table_errors": {"<table>": "generic message"}}`.

| Status | Meaning |
|---|---|
| `400` | The verified payload has no `data.id` |
| `401` | Missing or invalid Svix signature, or a stale timestamp |
| `503` | `CLERK_WEBHOOK_SIGNING_SECRET` or Supabase not configured |
| `207` | Partial success, see `table_errors` |

Idempotent by construction: Svix delivers at least once, and a replayed event just deletes zero remaining rows, which is still a success.

---

## Operations and health

Nothing here is meant for end users. Gating matters because several of these reveal configuration or user data, so most are **Clerk** or **Cron secret**; the exceptions are listed explicitly.

| Route | Auth | Purpose |
|---|---|---|
| `GET /api/async/health` | Public | Liveness of the ASGI process. Returns `{"ok": true, "runtime": "fastapi", "flask_mounted": true, "ts": <unix seconds>}` immediately; probes nothing external. |
| `GET /api/stack/health` and its alias `GET /api/health` | Clerk | Runtime readiness: rate-limit store and per-class policy, cost-breaker configuration, Clerk and Supabase configuration state, external API circuit-breaker summary, in-process counters. Cheap: no outbound calls. Reveals configuration, hence the gate. |
| `GET /api/devtools/heartbeat` | Public | A low-noise diagnostic for the hidden devtools inspector (Alt+Shift+I), which any visitor can open. Returns `{"ok", "ts", "elapsed_ms", "checks": {"library_popular_ready", "library_popular_ms"}, "stats"}`. Configuration-presence booleans feed the `ok` rollup but are deliberately left out of the body. The inspector polls it every 15 s, and only while the panel is open. |
| `GET /api/devtools/reliability` | Clerk | In-process counters (`stats`) for this instance. Values reset on a cold start and are not aggregated across instances. |
| `GET /api/devtools/rls-audit` | Clerk | The caller's own RLS posture: strict-mode flag, the user-scoped tables, whether a Supabase token was present, and, for a signed-in caller with one, a service-role versus user-scoped row-count comparison per table (`observed`). That comparison catches the silent failure where an unresolved `auth.uid()` makes every policy evaluate false. It does not test cross-user isolation; `scripts/verify_rls.py` does. |
| `GET /api/devtools/feedback-digest` | Clerk | Recent `answer_feedback` rows, newest first: `{"count", "helpful", "not_helpful", "rows"}`. Query `limit` (default 50, max 200; a bad value falls back to the default). `503` without Supabase. |
| `POST /api/devtools/segment-report` | Optional | A reader's "this segment looks wrong" report. Fields are stringified and length-capped; logged and counted, not stored. Returns `{"ok": true, "logged": true}`. |
| `POST /api/client-errors` | Public, same-origin only | Frontend error boundary reports (`message`, `url`, `stack` cut to 2,000 characters, `component`). Forwarded to Sentry with the user agent but no IP. `403` for a cross-origin caller. Class `telemetry`. |
| `GET /api/devtools/budget-check` | Cron secret | The daily AI-spend guardrail (`backend/cost_meter.py`), run by Vercel Cron at 13:00 UTC. A no-op until `DAILY_BUDGET_USD` is set. `401` for a wrong secret, `503` when `CRON_SECRET` is unset. |
| `GET /api/devtools/retention-enforce` | Cron secret | The retention job, run by Vercel Cron at 14:00 UTC. See below. |

`vercel.json` carries exactly these two crons. Vercel sends `CRON_SECRET` as a bearer token on cron-triggered requests once the variable is set on the project.

### `GET /api/devtools/retention-enforce`

`backend/routes_privacy.py`. Deletes data past the retention windows documented in `templates/privacy.html` §3, so that policy is enforced rather than promised: `ask_history` and `ai_usage_log` rows older than 90 days (by `created_at`) are hard-deleted. It also sweeps abandoned atomic AI-spend budget reservations (`ai_usage_log` rows with `reserved=true` past their `reservation_expires_at`) in the same run, through `expire_stale_budget_reservations()`, rather than standing up a separate job.

**Response (200, fully succeeded):**

```json
{
  "ok": true,
  "ts": "string, ISO 8601, UTC",
  "ask_history": { "deleted": "number", "cutoff": "string, ISO 8601" },
  "ai_usage_log": { "deleted": "number", "cutoff": "string, ISO 8601" },
  "budget_reservations": { "deleted": "number" }
}
```

**Response (500, one or more steps failed):** `ok` is `false` and the failing step's key holds an `error` string in place of `deleted` and `cutoff`:

```json
{
  "ok": false,
  "ts": "string, ISO 8601, UTC",
  "ask_history": { "error": "string" },
  "ai_usage_log": { "deleted": 0, "cutoff": "string" },
  "budget_reservations": { "deleted": 0, "error": "string" }
}
```

| Status | Meaning |
|---|---|
| `401` | Missing or incorrect `Authorization: Bearer <CRON_SECRET>` |
| `503` | `CRON_SECRET` not configured, or Supabase not configured |
| `500` | One or more steps failed (see the per-key `error`) |

---

## Cache tiers

`backend/cache_policy.py` is the single source of truth for `Cache-Control`, consulted from both the Flask `after_request` hook and the ASGI request middleware. Only `GET` is ever promoted to a public tier, and any error response (status 400 or above) from a public-tier route is downgraded to `private, no-store`, so a transient upstream failure cannot sit in the CDN.

| Tier | Header | Routes |
|---|---|---|
| Immutable | `public, s-maxage=86400, stale-while-revalidate=604800` | `/api/library/index`, `/api/library/leaf-refs`, `/api/library/popular`, `/api/library/category/*`, `/api/texts-index`, `/api/text/*`, `/api/prayer/*`, `/api/siddur/full/*`, `/api/siddur/section-refs/*`, `/api/siddur/v2/day`, `/api/siddur/v2/service/*` |
| Deterministic by date | `public, s-maxage=3600, stale-while-revalidate=86400` | `/api/zmanim`, `/api/zmanim/month`, `/api/zmanim/days`, `/api/daily-study`, `/api/holidays`, `/api/parasha` |
| Corpus-derived | `public, s-maxage=3600, stale-while-revalidate=86400` | `/api/communities/list`, `/api/communities`, `/api/prayers/list`, `/api/word/meaning`, `/api/library/search`, `/api/search/suggest`, `/api/geocode`, `/api/community/*`, `/api/siddur/v2/toc/*` |
| Private | `private, no-store` | `/ask`, `/set_location`, and every other `/api/*` route; an unclassified route is never accidentally made public |

`/api/zmanim` and `/api/zmanim/month` drop to `private` when they fall back to the session location. `docs/VERCEL_COST_OPTIMIZATION.md` explains why the tiers exist and what they save.
