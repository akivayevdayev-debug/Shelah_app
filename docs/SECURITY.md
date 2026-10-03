# Security — policy & posture

**Status:** living document, last reviewed **2026-10-02**. It records what has
been audited, what was found, what was fixed, how the live deployment is
configured, and what remains an explicit open item for a human decision
(see [§13](#13-open-items)). Dated entries ("confirmed 2026-09-02") are the
last time the operator or an engineering pass verified that fact; they are
not re-verified automatically.

This is an engineering document, not a substitute for a penetration test or
external security review. None has been performed yet; this document and the
fixes it describes are the engineering-side input to one.

See [`docs/LAUNCH_CHECKLIST.md`](LAUNCH_CHECKLIST.md) for how these findings
map onto the launch-gate checklist, [`docs/AI_SECURITY_REVIEW.md`](AI_SECURITY_REVIEW.md)
for the AI-specific threat review, and
[`docs/PRIVACY_OPERATIONS.md`](PRIVACY_OPERATIONS.md) for data-subject
requests and incident handling.

## Reporting a vulnerability

Email **akiva.yevda@gmail.com** (the same contact used across `terms.html`,
`privacy.html`, and `dmca.html`) with details and, if possible, steps to
reproduce. Please do not open a public GitHub issue for a suspected
vulnerability, and do not test against other users' accounts or data. There is
currently no bug-bounty program; reports are reviewed and fixed on a
best-effort basis by a solo maintainer.

**Supported versions.** Sh'elah is a single continuously-deployed service. Only
the code on `main` (the version running in production) is supported; fixes land
there and ship on the next deploy.

**In scope:** the deployed application and everything in this repository.
**Out of scope:** the third-party services it depends on (Clerk, Supabase,
Vercel, Upstash, Cloudflare, Sentry, Sefaria, Anthropic, Google) — report
issues in those to their vendors.

## 1. Secrets & key management

**Historical finding — rotated; git history rewritten.** A Google/Gemini API
key (value not reproduced here) was committed in `test_results.txt` (raw saved
output of a manual model-call test, including the request URL's `?key=` query
parameter) across multiple commits from 2026-05-16 onward and was still
tracked in the working tree at the start of the first security pass.

- **Fixed 2026-08-17:** the file was removed from the tree, and
  `test_results.txt` / `_workspace_backups_and_trash/` were added to
  `.gitignore` so the pattern can't be re-committed.
- **✅ Rotated — confirmed by the operator, 2026-09-02 (Google Cloud
  Console).** The leaked key is revoked at the source; it is not a live
  credential regardless of who holds a copy.
- **Git-history purge — `origin/main` rewritten 2026-09-18.** An earlier
  attempt (2026-09-17) was incomplete — the key had been hard-wrapped across a
  line break in the file, so only the head of the key was replaced — and was
  reverted. The 2026-09-18 pass ran `git filter-repo --replace-text` (regex
  rules that tolerate the line wrap, plus rules for any prefix/suffix fragment
  of at least 12 characters) on a disposable mirror clone, then scanned all
  3,386 objects for the full key (raw, newline-joined, whitespace-joined) and
  every 12-character window of it: 0 hits. Tip trees of every kept ref were
  byte-identical to their pre-rewrite trees; authors, dates and messages are
  unchanged apart from commit hashes quoted inside messages. It was
  force-pushed with `--force-with-lease`, and a fresh mirror clone of `origin`
  re-scanned clean.
  - **Still open — residual exposure 1:** `refs/pull/2/head` through
    `refs/pull/5/head` on GitHub still reach the old, key-bearing commits. A
    push to `main` cannot change pull-request refs; removing them needs a
    request to GitHub Support.
  - **Still open — residual exposure 2:** GitHub kept serving the old commits
    by hash on 2026-09-18 (the repository is public; 0 forks at that time), and
    any pre-existing clone keeps the old history. Anyone with an old clone must
    re-clone or run `git fetch origin && git reset --hard origin/main`.
  - Neither is a live-credential risk, since the key itself is rotated; they
    matter only as hygiene. Neither has been re-checked since 2026-09-18.
- A `gitleaks` 8.30.1 scan over every commit reachable from all refs of the
  rewritten history (497 commits, 2026-09-18) reported 4 findings, all the same
  false positive (`shelah-sw-v2-migrated` / `shelah-sw-v3-migrated`, held in a
  `migrationKey` constant in `templates/index.html` — a client-side
  localStorage migration-flag string, not a credential).
- `.env` is never tracked (`.gitignore` covers `.env`/`.env.*`; only
  `.env.example`, which ships placeholder values, is committed). The
  `gitleaks` and `bandit` pre-commit hooks run locally and again in CI over
  every file on every push (§4).
- `FLASK_SECRET_KEY`: if unset, `app.py` falls back to `os.urandom(32)`
  (cryptographically strong, just non-persistent across restarts) rather than
  a weak static default. Setting it in production is still recommended, for
  session stability rather than because the fallback is insecure.
- Supabase key separation: `SUPABASE_SECRET_KEY` (service role) is read only
  by server-side code and never referenced from any `templates/*.html` or
  `static/js/*.js`; only `SUPABASE_PUBLISHABLE_KEY` / `CLERK_PUBLISHABLE_KEY`
  reach the client.
- A repo-wide sweep for hardcoded credential patterns (Google/AWS/Stripe/
  OpenAI-style key shapes, PEM private-key headers) found nothing beyond the
  one finding above.

## 2. Supabase Row-Level Security

**Status: empirically confirmed enforcing, 2026-09-04; reconfirmed by the
operator 2026-09-16.**

- **Third-Party Auth for Clerk** is enabled on both sides. The Supabase side
  (Authentication → Sign In / Providers → Third-Party Auth) was confirmed
  2026-08-24; the *Clerk-side* half (the dashboard's "Enable the Supabase
  integration" toggle, which injects claims into session tokens) was found
  **Disabled** on Production on 2026-09-04 and activated. Clerk's integration
  toggle injects a `role` claim only, never `aud`, so `aud` / `email` /
  `app_metadata` / `user_metadata` were added to Clerk's Claims template by
  hand — without that, `CLERK_AUDIENCE=authenticated` would have produced
  "Token is missing the 'aud' claim" failures. A Clerk development instance is
  also registered under Third-Party Auth for local development.
- **Live acceptance test:** `scripts/verify_rls.py` (a two-real-user positive
  round trip plus a cross-user negative assertion, run directly against
  Supabase's REST layer so the app's own `.eq("user_id", ...)` filter cannot
  make the negative half trivially true). After the fixes above, the operator
  re-ran it: Layer 1's positive and negative cross-user checks passed on all
  three tables and Layer 2's token diagnostic read `aud='authenticated'`
  ("RLS is enforcing cross-user isolation on every checked table").
- `.github/workflows/rls-verify.yml` runs the same check, **manual dispatch
  only** and with `continue-on-error`: the test users' `RLS_TEST_USER_A/B_
  SESSION_ID` values are Clerk *session* ids that expire, so a scheduled run
  eventually 404s on `/v1/sessions/{id}/tokens`. That failure is stale-token
  noise, not an RLS regression; re-run it by hand with fresh session ids after
  touching auth or RLS configuration.
- The live path sends Clerk's plain default session token
  (`templates/index.html`'s `authHeaders()`), not a `"supabase"`-templated one.
- `STRICT_SUPABASE_RLS` (`app.py`) is a hardcoded `True` literal, enforced
  unconditionally — a security posture, not per-deployment config. A
  regression test asserts `strict_rls` is `True` in the audit response, so
  flipping the literal fails CI.
- **Policies.** `scripts/sql/SUPABASE_RLS_POLICIES.sql` defines
  `FORCE ROW LEVEL SECURITY` plus owner-scoped select/insert/update/delete
  policies (written as `(select auth.jwt()) ->> 'sub'`, evaluated once per
  query) for `user_preferences`, `user_memories` and `study_bookmarks`, and is
  the only file defining a policy on `user_memories` (a stale locked-down
  policy pair in `scripts/sql/rag_identity_cache_setup.sql` was dropped).
  `scripts/sql/conversations_setup.sql` defines the same owner-only pattern for
  `conversations`, `messages` and `citations`.
- **Service-role-only tables.** `ai_usage_log` and `ask_history` have RLS on
  and *no* policies by design: `backend/routes_user.py` and
  `backend/routes_answer_share.py` read `ask_history` only through the
  service-role client with a hand-written `.eq("user_id", ...)` filter, so a
  per-user policy would never be exercised. An old `user_own_history` policy on
  `ask_history` was documented as dropped on 2026-08-31 but was found still
  live on 2026-09-03 and dropped for real that day.
- `answer_feedback` accepts anonymous inserts by design (`WITH CHECK true`;
  anon has no `SELECT`). See §10.
- `community_knowledge` is a shared, global corpus queried by community filter,
  not by `user_id`, so it is deliberately public-read and has no per-user
  policy.
- `GET /api/devtools/rls-audit` (Clerk-authenticated) reports policy presence
  for each RLS-relevant table **and** runs an *observed* check on every call:
  it compares a user-scoped-client row count against a service-role-client row
  count for the caller's own rows. A silent-zero-rows failure would show up as
  a live mismatch on the next real signed-in request, not only in a scheduled
  script.
- The Supabase access token is sourced solely from the
  `Authorization: Bearer` header. A dead cookie-reading fallback (a pre-Clerk
  native-Supabase-Auth leftover; nothing in this app ever sets those cookies)
  was deleted 2026-08-31.
- `scripts/clerk_supabase_rls.py` (which expected a `"supabase"`-templated
  token and had no production importers) and the `CLERK_TOKEN_TEMPLATE` env var
  only it consumed were removed 2026-09-18.
- CI runs the full pytest suite, including the RLS-audit tests, on every push.

## 3. CSP / security headers

`backend/helpers.py::SECURITY_RESPONSE_HEADERS` is the single source of truth,
applied to every route on both transports (Flask `@app.after_request` and the
native FastAPI `/ask` route in `asgi.py`, which bypasses the Flask/WSGI mount
entirely):

- `Strict-Transport-Security: max-age=63072000; includeSubDomains; preload`
- `X-Frame-Options: SAMEORIGIN`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy` denies geolocation, camera, microphone, payment, USB,
  magnetometer, gyroscope and interest-cohort.
- `Content-Security-Policy`: `default-src 'self'`; `frame-ancestors 'none'`;
  `base-uri 'self'`; `form-action 'self'`; `frame-src` limited to
  `challenges.cloudflare.com`; `script-src`/`connect-src` allow only
  jsDelivr, Clerk (`js.clerk.com`, `clerk.com`, the `clerk.shelah.org` custom
  Frontend API domain, `api.clerk.com`, `*.clerk.accounts.dev`), Cloudflare
  Turnstile, and Sentry's CDN/ingest hosts.
- `X-XSS-Protection` is deliberately omitted (deprecated, ignored by modern
  browsers, can introduce vulnerabilities in old ones).
- `'unsafe-eval'` is **not** present (resolved when the Tailwind CDN JIT
  compiler was replaced with a committed build, `static/css/tailwind.css`).
- **`'unsafe-inline'` is still present** in `script-src`/`style-src`. Moving to
  a nonce-based CSP was evaluated (decided 2026-08-15) and deliberately
  deferred: `templates/index.html` still has 112 inline `onclick=` handlers and
  40 inline `style=` attributes (counted 2026-10-02), and CSP2+ browsers drop
  `'unsafe-inline'` entirely once a nonce is present, so a partial change
  breaks every handler on the page. The real blocker is that markup refactor
  (`onclick` → `addEventListener`, `style=` → classes), not the header. It is
  tracked as an open item (§13).

## 4. Supply chain

- **SRI:** every external `<script>` / `<link rel=stylesheet>` across all of
  `templates/` carries `integrity=` + `crossorigin=` and a pinned exact
  version, with two documented exceptions that cannot be pinned: the Google
  Fonts CSS endpoint (Google serves different `@font-face` payloads per
  User-Agent) and Cloudflare's `turnstile/v0/api.js` (Cloudflare requires it be
  loaded as served).
- **Python dependencies are hash-locked.** `requirements.lock.txt` and
  `requirements-dev.lock.txt` are generated from `requirements.txt` /
  `requirements-dev.txt` with `uv pip compile --generate-hashes`, and CI
  installs them with `pip install --require-hashes --only-binary=:all:` (the
  sole `--no-binary` exceptions are `julian` and `memoization`, zmanim's
  sdist-only dependencies). Pins that closed advisories:
  - `starlette` 1.0.1 → **1.3.1** (closes `PYSEC-2026-248`/`249`/`2280`/`2281`,
    `GHSA-jp82-jpqv-5vv3`, `GHSA-82w8-qh3p-5jfq`, `GHSA-wqp7-x3pw-xc5r`,
    `GHSA-x746-7m8f-x49c`); the smallest bump that closes them, not latest.
  - `PyJWT` **2.15.0** (the smallest version with no known advisory; 2.14.0 still
    carried `PYSEC-2026-4141`); `urllib3` (transitive, via `requests`) **2.8.0**
    for `PYSEC-2026-4175`/`4176`/`4177`; `click`, `h2`, `idna` patched earlier.
  - `pytest` (dev only) 8.4.0 → **9.0.3**, closing `PYSEC-2026-1845`.
  - Each bump was verified against the full pytest suite before pinning.
- **CI (`.github/workflows/ci.yml`)**, on every push, with every third-party
  action pinned to a commit SHA and the runner pinned to `ubuntu-24.04`:
  - `pre-commit run --all-files` (`gitleaks` + `bandit`) — **blocking**, so a
    skipped local hook (`--no-verify`) cannot ship a secret or a
    bandit-flagged pattern.
  - `pip-audit` against each lock file separately (`--disable-pip`) —
    currently **non-blocking** (`continue-on-error`), like the `ruff` step; a
    hard-fail gate needs an established remediation workflow first.
  - The full pytest suite with an 85 % backend coverage floor, the Node test
    suite (`node --test`, blocking because it gates PII-scrubbing
    correctness), and the WCAG 2.1 AA accessibility scan in light and dark
    themes. `npm ci --ignore-scripts` means no dependency's install script runs
    on the runner.
  - SonarCloud also analyses the repository (project key
    `akivayevdayev-debug_Shelah_app`); its security and reliability ratings on
    new code are tracked against the quality gate.
- **Known follow-up:** `starlette.middleware.wsgi.WSGIMiddleware` — which
  `asgi.py` uses to mount the entire Flask app inside the FastAPI app, the core
  of this app's hybrid transport — is deprecated upstream ("will be removed in
  a future release"). It works today; a future starlette major version could
  remove it. Evaluate migrating to `a2wsgi` before that happens.

## 5. Input validation & abuse

Every route in `backend/routes_*.py`, `app.py` and `asgi.py` was audited.
Coordinates go through `_coerce_coordinate(value, min, max)`, numeric
page/size/limit params through `_coerce_int`, Sefaria refs through
`_encode_ref_path()` / `quote()` before any outbound URL, and community/prayer
names are resolved against fixed allowlists before touching a filesystem path.
Three real gaps were found and fixed (each has a regression test):

- **`POST /api/devtools/segment-report`:** a non-string JSON field value
  (e.g. `{"kind": 1}`) crashed the `(x or default).strip()` pattern with an
  unhandled `AttributeError` → 500. Fixed with the same `str(...)` coercion
  every other JSON-body route uses.
- **`get_category_contents()`** (`GET /api/library/category/<path:category>`):
  built its outbound Sefaria URL with a raw `.replace("/", ",")`, letting a
  literal `#` or `?` in the attacker-controlled path truncate or inject a query
  string. Fixed by routing it through `_encode_ref_path()`. The fixed
  `SEFARIA_API` host ruled out SSRF either way.
- **`GET /api/holidays`:** the `year` query param was interpolated straight
  into the outbound Hebcal URL, so `2026&geo=pos&latitude=1` could inject
  parameters. Fixed by coercing through `_coerce_int` (bounded 1583–3000). The
  fixed `hebcal.com` host ruled out SSRF.

**Rate limiting — one ASGI middleware.** All rate limiting runs through
`backend/rate_limit.py`, whose `RateLimitMiddleware` is installed once on
`asgi.fastapi_app`. Starlette's middleware stack wraps the router that also
dispatches to the `WSGIMiddleware`-mounted Flask app, so it sees 100 % of
traffic — every native FastAPI route and every Flask route — exactly once,
before any handler runs. One body shape, `{"error": ..., "code":
"rate_limited"}` with a `Retry-After` header, covers every rejection.

- **Policy table (`_POLICIES`), 60-second windows:**

  | Class | Limit | On store outage |
  | --- | --- | --- |
  | `llm` | 20/min anonymous, 40/min signed in, plus 200/day signed in | **fails closed** |
  | `heavy` | 10/min | fails open |
  | `fanout` | 30/min | fails open |
  | `sidebar` | 90/min | fails open |
  | `feedback` | 10/min | fails open |
  | `telemetry` | 10/min | fails open |
  | `cheap` (default) | 120/min | fails open |
  | `account` | 5/min | fails open |
  | `webhook` | 15/min | fails open |

  `llm` is the one class that fails closed, since an unmetered `/ask` during an
  outage is a real budget hole. Anonymous traffic has no daily cap of its own
  but keeps the tighter per-minute bucket.
- **Route classification** (`_ROUTE_CLASSES`, first-prefix-match wins, with
  `_ROUTE_PATTERNS` checked first): `/ask` and
  `/api/conversations/<id>/ask` → `llm`; `/api/export/chapter` and
  `/api/siddur/full/` → `heavy`; `/api/library/search`, `/api/text/`,
  `/api/word/meaning`, `/api/geocode` and `/api/public/answer` → `fanout`;
  `/api/sidebar/` → `sidebar`; `/api/feedback` → `feedback`;
  `/api/client-errors` → `telemetry`; `/api/user/delete-account` and
  `/api/user/data-export` → `account`; `/api/webhooks/clerk` → `webhook`;
  `/api/conversations` and `/api/siddur/v2/` → `cheap`; anything else → `cheap`.
  `/static/*` is exempt. Every new route must be classified in this table.
- **Store:** a Redis fixed-window counter (`INCR` + conditional `EXPIRE`,
  matching the Vercel edge WAF's algorithm) when `RATE_LIMIT_REDIS_URL` is set
  — **confirmed set in the live Vercel production environment (2026-08-26)**,
  so production traffic is limited against one shared, cross-instance counter.
  When the variable is unset or malformed the middleware falls back to a
  per-process sliding-window store (LRU-capped at 2048 keys), which is for
  local development and misconfiguration, not the production default. The
  fallback logs loudly at boot (`logger.warning` when unset; `logger.critical`
  with `exc_info` when set but the client fails to build), and a malformed
  store must never crash app boot — a fixed regression (production incident
  2026-08-26/27; see `docs/RUNBOOKS.md`'s incident history).
- **Trusted client key:** every IP-keyed bucket uses
  `backend/helpers.py::_resolve_client_ip()` — `X-Vercel-Forwarded-For`, then
  `X-Forwarded-For`, then `X-Real-IP` (first comma-separated value), then the
  ASGI-reported address. There is no second IP-extraction path.
- **Identity-aware keys (`llm` and `account`):** a signed-in caller (Clerk
  `sub`, extracted via `backend.auth.extract_user_id_from_bearer_value`, no
  separate JWT parsing) is keyed per account (`rl:<class>:user:<sub>`) instead
  of sharing an IP bucket, so one heavy signed-in caller behind a shared
  institutional egress IP (a yeshiva, day school or shul network) cannot lock
  out the whole building. `webhook` stays IP-keyed — it is Clerk calling us,
  and Svix signature verification is the real gate on that route.
- **Kill switch:** `RATELIMIT_ENABLED` (default `true`, the flag
  `tests/conftest.py` and `ci.yml` rely on) disables the middleware when
  false.
- **Observability:** every rejection and every store-unavailable event is
  logged with a hashed key (`_hash_key`, sha256 truncated to 16 hex — never a
  raw IP or Clerk `sub`) via `log_mitigation()` / `_capture_backend_error()`
  (§8).

**Spend controls.** Three layers keep a flood or a motivated multi-account
caller bounded:

1. The Vercel WAF rate-limit rule (§7), which rejects traffic before any
   function invocation is billed.
2. The application middleware above.
3. `backend/cost_meter.py`: a per-user daily spend cap
   (`PER_USER_DAILY_BUDGET_USD`, default $2.00, **on by default**, reserved
   atomically through the `check_and_reserve_user_budget` Supabase RPC so
   concurrent requests cannot all slip under the cap) and a global cost
   breaker (`DAILY_BUDGET_USD`, opt-in — a no-op until the operator sets it;
   `is_global_cost_breaker_tripped` pauses `/ask` once total daily spend
   reaches it, regardless of account count).

   Both are computed from the price table in `backend/cost_meter.py`. Gemini
   calls are priced at $0.00 because the project runs on Gemini's free tier
   (so the per-user cap in practice meters Claude usage, the fallback
   provider); update the table if the project ever moves to paid Gemini.

The per-user cap is a **guardrail, not an anti-abuse control** (decided
2026-08-23): it keys on the Clerk `user_id`, and Clerk signup is frictionless
by design, so a caller who hits the cap can create a second account. That was
an accepted, documented tradeoff; the real ceilings against a multi-account
attacker are the global breaker and the IP+JA4-keyed WAF rule, which survives
account rotation.

**Indirect prompt injection via retrieved web text** (`docs/AI_SECURITY_REVIEW.md`
M2). `validate_user_query()` only screens the user's own question, but
Wikipedia, Halachipedia, HebrewBooks and MyMemory text is publicly editable or
crowd-sourced and reaches the model as reference material (the live `/ask`
pre-fetch sections via `build_prompt()`, and the agentic `web_search` /
`search_responsa_external` / `translate_text` tool results). Two layers apply:

- `backend/retrieval_guard.py` drops a snippet carrying injection phrasing
  before it enters the prompt — English paraphrase verbs
  (ignore/disregard/forget/override/set aside…), "act as…/pretend you are…"
  persona jailbreaks, page-level-authority phrasing, plus Hebrew, French,
  Spanish, German, Russian, Portuguese, Italian, Ukrainian, Arabic and Yiddish
  phrasing and zero-width, fullwidth and look-alike-script spellings. Only a
  source label and a marker count are logged (never retrieved text or question
  text), plus a Sentry breadcrumb via `log_retrieval_guard_drop()`. It covers
  web/Halachipedia text, Sefaria snippets, community-knowledge rows,
  user-memory summaries and the community-customs/profile tool handlers; a
  13,727-snippet measurement against real corpora and live connector traffic
  found 0 false positives.
- The pre-fetch sections are wrapped in `<retrieved_context>` tags that both
  system prompts name as data, never instructions (emitted only for non-empty
  sections; `PROMPT_VERSION` is `2026-10-02-source-markers-v4` and the wrapper
  is unchanged in it).

It is a conservative phrase heuristic — bare "you are now" and "ignore any
instructions from his doctor" (or a bare "the owner"/"the Creator") are
deliberately not markers, being normal halachic prose — and one layer beside
the untrusted-data framing and `validate_model_output()`. It is not a
classifier: a truly novel paraphrase is not caught.

Other controls:

- `answer_feedback` metadata (`mode`, `language`, `safety_class`) is
  length-capped and sanitized like `comment`.
- Payload size: `app.py`'s `MAX_CONTENT_LENGTH` (256 KiB) covers every
  Flask-routed blueprint; `asgi.py`'s `request_id_middleware` has its own
  independent `Content-Length` check for the native `/ask` route, which
  bypasses the Flask config.
- The agentic tool-use layer (`AI_AGENTIC_TOOLS`) is off by default in every
  environment; see `docs/AI_TOOLS.md`.

## 6. AuthN/Z review

- **`_verify_clerk_token`** (`backend/auth.py`): `algorithms` is hardcoded to
  `["RS256"]` — no algorithm-confusion risk. `issuer` is always passed to
  `jwt.decode`, so `iss` is verified; expiry is verified by default (PyJWT) and
  never disabled. A missing `CLERK_JWT_ISSUER` fails closed (raises before any
  token is looked at), and both `require_clerk_auth` / `maybe_require_clerk_auth`
  catch any verification exception and return `401` rather than proceeding.
- **Audience verification — config-dependent, ✅ confirmed set in production.**
  When `CLERK_AUDIENCE` is unset (it is optional in `.env.example`) the `aud`
  claim is not checked, so any RS256 token from the right issuer would be
  accepted regardless of which Clerk application minted it — an explicit,
  documented branch, and a real bypass if the Clerk tenant were ever shared
  across applications. `CLERK_AUDIENCE` is **confirmed set in the live Vercel
  production environment** (`vercel env ls production`, 2026-09-02), so
  `audience=CLERK_AUDIENCE` is enforced on every production request. The
  unset branch describes local/dev only.
- Every user-data route (bookmarks, preferences, memories, `ask_history`,
  conversations, share management, devtools) is decorated with
  `@require_clerk_auth` or `@maybe_require_clerk_auth` as appropriate and
  filters by the JWT's own `sub` claim — no route trusts a client-supplied
  `user_id` (no IDOR pattern found).
- Session cookie flags (`app.py`): `SESSION_COOKIE_HTTPONLY=True`,
  `SESSION_COOKIE_SAMESITE="Lax"`, `SESSION_COOKIE_SECURE=is_production_runtime`.
  The native FastAPI `/ask` route relies purely on Bearer tokens with no
  cookie/session dependency; the Flask session cookie only ever holds calendar
  lat/lon, never auth state.
- `CLERK_ENFORCE_AUTH` (`backend/auth.py`) defaults to `True` whenever
  `VERCEL == "1"` or `FLASK_ENV == "production"` and can be overridden
  explicitly in either direction, so it fails toward enforcement in production
  by default.

## 7. Vercel WAF (dashboard-configured) — entered 2026-08-26, last confirmed 2026-09-16

The one layer that makes a flood *free*: per Vercel's WAF rate-limiting docs,
WAF-mitigated traffic incurs no CDN request, no Fast Data Transfer and no
function invocation, unlike `backend.rate_limit.RateLimitMiddleware` below it,
which still pays a full invocation to say "no." It lives in dashboard config
(Project → Firewall), not in code.

**Hobby-tier budget: 3 custom firewall rules in total.** Rate limiting is not
a separate quota; it is one action type on an ordinary custom rule and uses
one of the same three slots. All three are in use:

1. **Rate-limit rule — Enforce, confirmed live 2026-09-16:** path `/ask`,
   method `POST`; key **IP + JA4 digest** (JA4 is a TLS-stack fingerprint that
   survives the IP rotation a real flood uses); fixed window, **60 s**;
   **100 requests/minute**, set from a week of Log-mode traffic data; action
   **429**, with repeat offenders locked out for **15 minutes**.
2. **Deny common scanner paths:** `/.env`, `/.git/*`, `/wp-admin/*`,
   `/vendor/*`, `/phpmyadmin/*` — pure noise against this codebase, and each
   hit would otherwise cost a function invocation just to 404.
3. **Deny non-`GET`/`POST`/`HEAD`/`OPTIONS` methods, site-wide.** ⚠️ The
   dashboard showed this rule's action as **Log, not Deny**, on 2026-09-16 —
   either this description is stale or the rule was never flipped out of its
   testing phase. Needs a quick operator check (§13).

No slot is held in reserve for incident response; if one is needed it has to
come from merging two rules (e.g. scanner-path-deny and method-deny) or from
upgrading off Hobby.

Two things to remember when changing these:

- **Deploy rate-limit rules in Log mode first** and set the threshold from a
  week of real traffic; a guessed number is as likely to lock out real users in
  a legitimate spike as it is to stop an attacker.
- **WAF counters are tracked per region.** The rule is correct as "exactly one
  counter" only because `vercel.json` pins `"regions": ["iad1"]`. If that pin is
  removed or a second region is added, the counter silently splits and the
  effective limit multiplies by the region count — revisit the rule the same day
  a region change ships.

The WAF sits in front of, not instead of, the application middleware (§5) and
the cost breaker. All three should stay conceptually in sync (same route
classes, same posture toward `/ask`) even though the WAF layer lives in the
dashboard.

## 8. Turnstile bot mitigation

Two additions to the abuse stack above.

**Identity-aware rate-limit keys** — see §5. An IP-only bucket treats every
caller behind one shared egress IP as a single caller; signed-in callers get a
per-account bucket, a higher per-minute allowance (`authenticated_max_requests`)
and an explicit daily `/ask` quota (`daily_max_requests`), all in the same
shared store `backend/rate_limit.py` and `backend/cost_meter.py` already use.

**Cloudflare Turnstile — why this and not Vercel BotID.** Anonymous `/ask`
traffic that crosses a per-IP hourly threshold
(`TURNSTILE_ANON_HOURLY_THRESHOLD`, default 5) is gated behind a Turnstile
challenge (`backend/turnstile.py`). Vercel's BotID was considered and
**rejected**: its server-side verification SDK is JavaScript-only, and `/ask`'s
whole request path is Python, so there is no entry point for BotID's
verification step at any plan tier. Turnstile's `siteverify` is one plain HTTPS
POST any language can make, it is free at this project's volume, and it stays
invisible to the large majority of legitimate callers. The call uses async
httpx (never a blocking `requests` call). The feature is gated behind
`TURNSTILE_ENABLED` (default **off**), so every function in
`backend/turnstile.py` is a true no-op — local dev and the test suite are
unaffected — until an operator enables it and supplies `TURNSTILE_SECRET_KEY` /
`TURNSTILE_SITE_KEY` in the deployment environment. (Separately, Clerk's Bot
Sign-up Protection renders its own Turnstile challenge in an iframe, which is
why the CSP's `frame-src` allows `challenges.cloudflare.com`.)

Two deliberately different fail postures live in `backend/turnstile.py`:
`is_challenge_required()` (is a challenge owed at all? — anti-abuse polish)
fails **open** on a store error, so a Redis blip does not block anonymous
`/ask` on top of what the `llm` class's own fail-closed posture already
decided; `verify_turnstile_token()` (is *this* proof valid? — the actual gate)
fails **closed** on a missing token, a missing or misconfigured secret key, or
a `siteverify` error, since that is the one an attacker would want to bypass by
making `siteverify` look unreachable.

**Mitigation observability.** Every rejection — a rate-limit 429 or a Turnstile
403 — emits one structured log line plus a Sentry **breadcrumb** (never a
Sentry event, to avoid drowning the real error signal and burning the free-tier
event quota) via `backend.logging_setup.log_mitigation(tier, route_class,
key_hash, route)`. The key is always hashed before logging, so an attack shows
up as a spike in `mitigation_triggered` lines without putting an identifiable
value in a log store.

**Env vars:** `TURNSTILE_ENABLED`, `TURNSTILE_SECRET_KEY`, `TURNSTILE_SITE_KEY`,
`TURNSTILE_ANON_HOURLY_THRESHOLD` — see `.env.example` for defaults and
`docs/API.md` for the full policy/env matrix alongside the `RATE_LIMIT_*` vars.

## 9. Account-deletion completeness

`/api/user/delete-account` (`backend/routes_privacy.py`) was audited.
Isolation (no caller can touch another user's data) holds **by construction**:
every query is scoped by `.eq("user_id", ...)` with `user_id` read only from the
caller's own verified Clerk JWT, never from a request parameter. Two
completeness gaps were found and fixed:

- **`answer_feedback` is included in export and delete.** `_USER_DATA_TABLES`
  previously missed it even though `backend/routes_feedback.py` writes a real
  Clerk `sub` into it. See `docs/PRIVACY_OPERATIONS.md` for the user-facing
  description.
- **A Clerk `user.deleted` webhook backstops deletions this app doesn't
  initiate.** A user can delete their Clerk identity through Clerk's hosted
  `<UserProfile />`, and an operator can delete one from the Clerk Dashboard —
  neither calls this app's endpoint, so either could orphan a user's data with
  no JWT left to authenticate a retry. `POST /api/webhooks/clerk`
  (`backend/routes_webhooks.py`) closes this. It verifies Clerk's Svix
  signature by hand (HMAC-SHA256 over `"{svix-id}.{svix-timestamp}.{body}"`,
  rejecting anything outside a 300-second timestamp window) rather than adding
  the `svix` package — signature verification **is** this endpoint's
  authentication. On a valid `user.deleted` event it runs the same table
  cascade `delete_account()` uses, keyed on `data.id`; it is idempotent by
  construction. It requires `CLERK_WEBHOOK_SIGNING_SECRET` and an Endpoint in
  Clerk Dashboard → Webhooks subscribed to `user.deleted`.
- **Self-service deletion is enabled** for this Clerk instance (confirmed
  2026-09-02): a signed-in user can delete their account entirely through their
  own action, and the webhook above already catches that path.

**Backups.** Supabase backup / point-in-time-recovery retention was confirmed
2026-09-01: **Free plan — no PITR (a paid Pro-plan add-on) and no automated
backups at all.** See `docs/RUNBOOKS.md`'s "Backups & recovery" and
`docs/LAUNCH_CHECKLIST.md` for the manual `pg_dump` / `supabase db dump`
fallback this implies.

**Logging hygiene.** `user_id` is hashed (`backend.logging_setup.hash_user_id`,
the same sha256-truncated-to-16-hex construction as `rate_limit._hash_key`) at
every `_capture_backend_error` call site that previously passed a raw Clerk
`sub` into Sentry or structured-log context. Field-level per-user encryption /
crypto-shredding was considered and **not** built: every live store this app
controls already gets a real `DELETE` (strictly stronger than shredding), and
the one store shredding would help — provider-managed backups — has an
exposure already time-bounded by the provider's own retention.

## 10. Supabase Advisor scan — 2026-09-03

A live Supabase Advisor scan against the production project surfaced
security-relevant `WARN`-level findings. Full details and the reasoning behind
each decision are in `docs/DATABASE.md`'s "Live Supabase Advisor scan"
subsection; `scripts/sql/migrate_search_path_and_rls_perf_fixes.sql` carries
the statements. **The migration was applied and verified live on 2026-09-03**
(re-running the Advisor afterwards showed the fixed findings gone).

- **`function_search_path_mutable`:** `public.get_schema_snapshot()` and
  `public.set_updated_at_timestamp()` had a role-mutable `search_path`. Neither
  is `SECURITY DEFINER`, so this was the ordinary object-hijacking risk. Fixed
  with `ALTER FUNCTION ... SET search_path = public, pg_temp`.
- **`pg_graphql_anon_table_exposed` / `pg_graphql_authenticated_table_exposed`:**
  `REVOKE SELECT` from `anon` and `authenticated` on `ai_usage_log` and
  `ask_history` (zero-policy, service-role-only tables), and from `anon` only on
  `study_bookmarks`, `user_memories` and `user_preferences`. `authenticated` was
  **deliberately left alone** on those three because the backend's RLS-scoped
  client (`app.py::_get_user_scoped_supabase_client`, the *primary* path in
  `backend/routes_user.py` and `backend/rag.py`) authenticates as exactly that
  role; revoking it would break preferences, semantic bookmarks and both RAG
  memory paths. `community_knowledge` is flagged but is deliberately
  public-read. Those residual findings are the expected remainder.
- **`rls_policy_always_true`** on `answer_feedback.anyone_can_submit_feedback`
  — reviewed and confirmed **intentional**: `POST /api/feedback` accepts
  anonymous submissions (`@maybe_require_clerk_auth`), and the route has its own
  tighter-than-default `feedback` rate-limit class (10 req/60 s).
  Anon `INSERT` must use `returning=minimal`, since anon has no `SELECT` by
  design and an insert that asks for the row back fails with `42501`.

## 11. `npm audit` — 7 high-severity findings, dev/build tooling only (checked 2026-10-02)

`npm audit` reports 7 high-severity advisories; every one is in tooling that
never ships to production or runs on any request path:

| Chain | Pulled in by | Fix |
| --- | --- | --- |
| `tailwindcss@3.4.19` → `chokidar` / `micromatch` / `fast-glob` → `braces` | `tailwindcss` (direct; run locally by `npm run build:css`, its output `static/css/tailwind.css` is committed) | needs Tailwind 4.x — a major-version migration |
| `pa11y-ci` → `cheerio` → `undici` (≤ 6.28.0) | `pa11y-ci` (direct; used only by `npm run test:a11y`) | in-range fix available via `npm audit fix` |
| `pa11y-ci` → `globby` → `glob` → `minimatch` → `brace-expansion` | `pa11y-ci` | in-range fix available via `npm audit fix` |

The earlier `extract-zip` finding (a symlink path traversal in `puppeteer`'s
ZIP extraction, [GHSA-jmr9-qjv8-65gv](https://github.com/advisories/GHSA-jmr9-qjv8-65gv))
is no longer in the tree: `package.json`'s `overrides` pin `pa11y` ^10 and
`puppeteer` ^25.

**Accepted risk, not a gap.** The `tailwindcss` chain only runs when a
developer rebuilds the stylesheet from this repository's own input files; the
`undici` / `brace-expansion` chains are reachable only through the
accessibility scanner, which fetches this project's own pages. None is
reachable through any input the deployed application accepts. The two
`pa11y-ci` chains have a non-breaking fix available and are queued (§13); the
Tailwind chain waits on a deliberate 3 → 4 migration. Re-run `npm audit`
periodically, or when `tailwindcss` / `pa11y-ci` / `puppeteer` cut a release.

## 12. Shared answers & conversations

Two features expose user-authored data and are covered here because they widen
what a stranger can reach.

- **Public share links** (`backend/routes_answer_share.py`). An `ask_history`
  row is private to its owner. The owner can mint a public link: `POST` sets an
  unguessable `share_token` (`secrets.token_urlsafe(16)`, 128 bits of
  randomness) and `is_public`; anyone holding `/a/<token>` can then read the
  answer through `GET /api/public/answer/<token>` without signing in. `DELETE`
  revokes it — the token is cleared, so the old link 404s for good and
  re-sharing mints a new one. The public route selects only the answer columns
  — never `user_id` or the row `id` — and every response carries
  `X-Robots-Tag: noindex, nofollow`. It is rate-limited in the `fanout` class
  per IP, which keeps token guessing slow. The columns come from
  `scripts/sql/migrate_ask_history_share.sql`; until it is applied the routes
  answer `503 {"code": "share_unavailable"}` rather than 500.
- **Conversations** (`backend/routes_conversations.py`). Multi-turn threads are
  stored in `conversations` / `messages` / `citations` under owner-only RLS
  policies, with an application-layer ownership check on every route as
  defense in depth. The thread's `minhag` setting is enforced server-side (the
  stored row's own value, never the request body), and
  `POST /api/conversations/<id>/ask` is rate-limited in the `llm` class — the
  same fail-closed, per-account budget as `/ask`.

## 13. Open items

Items needing a human decision or an operator action, collected in one place:

- **External penetration test / security review** — not yet performed.
- **Git-history residual exposures** (§1): `refs/pull/2/head`–`5/head` and
  hash-addressable old commits still reachable on GitHub; needs a GitHub
  Support request.
- **Vercel WAF rule 3** (§7) showed as *Log* rather than *Deny* on 2026-09-16;
  confirm the intended action in the dashboard.
- **CSP `'unsafe-inline'`** (§3): blocked on refactoring 112 inline `onclick=`
  handlers and 40 `style=` attributes before a nonce-based policy is possible.
- **`WSGIMiddleware` deprecation** (§4): plan the move to `a2wsgi`.
- **`npm audit fix`** (§11) for the two `pa11y-ci` chains; Tailwind 4
  migration for the third.
- **`pip-audit` and `ruff` CI steps are non-blocking** (§4); promote them to
  blocking once a remediation workflow exists.
- **Backups** (§9): the Supabase Free plan has no automated backups or PITR;
  upgrade, or schedule the manual dump.
- **Turnstile** (§8) ships disabled; enabling it is an operator action.
