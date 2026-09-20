# Security — findings & posture (plan.md §8.C)

**Status:** pre-launch security hardening pass, completed 2026-08-17. This is
the written findings report §8.C.7 (plan.md) calls for. It documents what was
audited, what was found, what was fixed as part of this pass, and what
remains an explicit open item for a human decision.

This is an engineering document, not a substitute for the penetration
test / external security review §8.C.7 also calls for before public launch.

See [`docs/LAUNCH_CHECKLIST.md`](LAUNCH_CHECKLIST.md) for how this document's
findings map onto the plan.md §8.H launch-gate checklist, including which
items here are still open.

## Reporting a vulnerability

Email **akiva.yevda@gmail.com** (same contact used across `terms.html`,
`privacy.html`, and `dmca.html`) with details and, if possible, steps to
reproduce. Please do not open a public GitHub issue for a suspected
vulnerability. There is currently no bug-bounty program; reports are
reviewed and fixed on a best-effort basis by a solo maintainer.

## 1. Secrets & key management

**Finding — historical, key rotated; git history rewritten and force-pushed
to `origin/main` on 2026-09-18, with two residual exposures still open
(below). ✅ Rotation confirmed by operator, 2026-09-02.** A Google/Gemini API
key (value not reproduced in this document) was committed
reproduced in this document) was committed
in `test_results.txt` (raw saved output of a manual model-call test,
including the request URL's `?key=` query parameter) across multiple
commits from 2026-05-16 onward, and was still present and tracked in the
working tree at the start of the original 2026-08-17 pass, not just in
old history.

- **Fixed 2026-08-17:** the file was removed from the current tree
  (`_workspace_backups_and_trash/test_results.txt`) and `test_results.txt` /
  `_workspace_backups_and_trash/` were added to `.gitignore` so the pattern
  can't be re-committed.
- **✅ Rotated — confirmed by the operator, 2026-09-02 (Akiva, Google Cloud
  Console).** The leaked key has been rotated/revoked at the source. This
  is no longer a live-credential risk: the old key value is dead and cannot
  be used against the Gemini API regardless of who has it.
- **✅ Git-history purge — `origin/main` rewritten 2026-09-18; two residual
  exposures remain open.** *Correction:* an earlier revision of this bullet
  (commit `042e92b`) stated the purge had already been force-pushed to
  `origin/main` on 2026-09-16 with zero copies remaining. That was wrong.
  No purge was left in place on `origin` at that point: an earlier local
  rewrite (`main` replaced by a rewritten-history commit, `994147b`, at
  2026-09-17 20:44 -0400 — not 2026-09-16) was force-pushed to
  `origin/main` at 20:50 -0400, then reverted to the pre-rewrite `054b7d5`
  at 20:58 -0400 because it had dropped 217 commits that existed only on
  `origin`. That earlier rewrite was also incomplete: the key had been
  hard-wrapped across a line break in `test_results.txt`, so it replaced only
  the head of the key and left a 15–19-character tail in the "scrubbed" blobs.
  - **Rewrite, 2026-09-18:** `git filter-repo --replace-text` (regex rules
    that tolerate the line wrap, plus rules for any prefix/suffix fragment
    of at least 12 characters) was run on a disposable mirror clone of the
    local repo, never the working copy. In that mirror, every one of the
    3,386 objects was scanned for the full key (raw, newline-joined and
    whitespace-joined) and for every 12-character window of it: 0 hits.
    The tip trees of `main`, the `vercel/…` branch and both backup tags were
    byte-identical to their pre-rewrite trees. Commit author, committer,
    dates and messages are unchanged apart from commit hashes quoted inside
    messages, which `filter-repo` renumbers. The rewrite also collapsed the
    duplicate lineages left by the earlier local rewrite, so the history is
    now 392 commits with no dropped `origin`-only work.
  - **Pushed 2026-09-18 15:56 -0400** with
    `git push --force-with-lease=main:054b7d5 origin main`
    (`054b7d5...e536a45`, forced update). A fresh mirror clone of `origin`
    afterwards showed `refs/heads/main` at `e536a45`, its tree identical to
    the local tree, and the same full-key-plus-fragment scan over everything
    reachable from it returned 0 hits. The `vercel/…` branch and
    `refs/pull/1` also returned 0 hits.
  - **Still open — residual exposure 1:** `refs/pull/2/head` through
    `refs/pull/5/head` on GitHub still reach the old, key-bearing commits
    (the same scan flags the full key in each). A push to `main` cannot
    change pull-request refs; removing them needs a request to GitHub
    Support.
  - **Still open — residual exposure 2:** GitHub still serves the old
    commits by hash. On 2026-09-18, after the push, the API returned HTTP
    200 for the old `origin/main` tip `054b7d5` and for the key-bearing
    commit `37df59d`. The repository is public (0 forks at that time), and
    any existing clone keeps the old history. Anyone with an existing clone
    must re-clone or run `git fetch origin && git reset --hard origin/main`.
  - The key itself is rotated (above), so none of this is a live-credential
    risk; the residual exposures matter only as hygiene.
- A `gitleaks` 8.30.1 scan over every commit reachable from all refs of the
  rewritten history (497 commits, run 2026-09-18) reported 4 findings, all
  the same false positive (`shelah-sw-v2-migrated` /
  `shelah-sw-v3-migrated`, held in a `migrationKey` constant in
  `templates/index.html` and `templates/index_backup.html` — a client-side
  localStorage migration-flag string, not a credential). An earlier revision of this bullet cited
  "174 commits / 20 findings"; those figures were not reproducible and are
  superseded.
- `.env` confirmed never tracked (`.gitignore` covers `.env`/`.env.*`;
  `git ls-files` shows only `.env.example`, which ships placeholder values).
- `FLASK_SECRET_KEY`: verified — if unset, `app.py` falls back to
  `os.urandom(32)` (cryptographically strong, just non-persistent across
  restarts) rather than a weak static default, so there's no forced-weak-key
  risk either way. Setting it in prod is still recommended (the existing
  critical-level log message already says so) for session stability, not
  because the fallback is insecure.
- Supabase key separation confirmed correct: `SUPABASE_SECRET_KEY` (service
  role) is read only in `app.py`'s server-side client and never referenced
  in any `templates/*.html` or `static/js/*.js`; only
  `SUPABASE_PUBLISHABLE_KEY`/`CLERK_PUBLISHABLE_KEY` reach the client.
- A repo-wide sweep for hardcoded credential patterns (Google/AWS/Stripe/
  OpenAI-style API key shapes, PEM private key headers) found nothing beyond
  the one finding above.

## 2. Supabase Row-Level Security

- **Third-Party Auth for Clerk: confirmed enabled, 2026-08-24 (verified by
  Akiva in the Supabase dashboard, Authentication → Sign In / Providers →
  Third-Party Auth).** This is the single dashboard setting `auth.uid()`
  depends on to resolve a Clerk JWT (`plan.md` §21.1) — until this line was
  added it lived in no versioned artifact anywhere. This answers `plan.md`
  §21.2.1 and unblocks Prompt 34 (`claude_code_prompts.md`): `auth.uid()`
  should now resolve for real signed-in users, but this was the dashboard
  config confirmation only. **Status as of 2026-08-31: the live acceptance
  test now exists and is committed (`scripts/verify_rls.py`, §21.2.2 STEP
  2/3 — a two-real-user positive round trip plus the cross-user negative
  assertion, run directly against Supabase's REST layer so the app's own
  `.eq("user_id", ...)` filter can't make the negative half trivially
  true), wired as a weekly/manual job
  (`.github/workflows/rls-verify.yml`, STEP 4) rather than a per-PR gate.
  It has NOT yet been run against real credentials** — that needs two
  dedicated Clerk test users' session IDs, which only the operator can
  provision (an engineering pass cannot create Clerk accounts or
  sessions).

  **Empirically confirmed live, 2026-09-04 (`plan.md` §49.4/§49.5,
  §21).** Running the check surfaced two further real gaps beyond the
  2026-08-24 dashboard confirmation above: the *Clerk-side* half of
  Third-Party Auth (the dashboard's own "Enable the Supabase integration"
  toggle, which is what actually injects claims into session tokens) was
  found **Disabled** on Production — only the Supabase side had been
  checked — and activated; and Clerk's integration toggle turned out to
  inject a `role` claim only, never `aud`, which would have silently
  reintroduced the exact "Token is missing the 'aud' claim" failure the
  moment `CLERK_AUDIENCE=authenticated` took effect. Fixed by manually
  adding `aud`/`email`/`app_metadata`/`user_metadata` to Clerk's Claims
  template. With both fixes live, the operator re-ran
  `scripts/verify_rls.py`: Layer 1's positive and negative cross-user
  checks passed on all three tables, and Layer 2's token diagnostic read
  `aud='authenticated'` (previously `None`) — quoted directly, **"RLS is
  enforcing cross-user isolation on every checked table."** RLS is now
  *empirically confirmed*, not just configured and testable; this closes
  `plan.md` §21 (🟢 RESOLVED 2026-09-04) and Prompt 34 (✅ RESOLVED
  2026-09-04). The token-template ambiguity §21.2.1 also asks about is
  resolved by code reading, not this dashboard setting: the live path sends
  Clerk's plain default session token (`templates/index.html`'s `authHeaders()`),
  not a `"supabase"`-templated one, so `scripts/clerk_supabase_rls.py`
  (which expected the latter and had zero production importers) was
  confirmed stale/unused. Its removal was recorded 2026-08-31 (plan.md
  §21.2.1) but the file itself stayed on disk until 2026-09-18, when it was
  actually deleted; the `CLERK_TOKEN_TEMPLATE` env var it alone consumed
  went with it (plan.md §21.2.1; see Prompt 34's 2026-08-24 update for the
  code-level evidence trail).
- `STRICT_SUPABASE_RLS` (`app.py`) is a hardcoded `True` literal — enforced
  unconditionally, not just "in prod" — with the comment explaining this is
  a security posture, not per-deployment config.
- `scripts/sql/SUPABASE_RLS_POLICIES.sql` defines `FORCE ROW LEVEL SECURITY`
  + owner-scoped select/insert/update/delete policies for
  `user_preferences`, `user_memories`, `study_bookmarks` — and, as of
  2026-08-31, is the *only* file defining a policy on `user_memories`:
  `scripts/sql/rag_identity_cache_setup.sql` used to also define a fully
  locked-down `using (false)` pair of policies on the same table from
  when it was service-role-only by design, which had silently drifted out
  of sync with `backend/rag.py` growing a request-scoped-client read/write
  path (`docs/DATABASE.md`'s `user_memories` entry has the full history —
  plan.md §21/§30.5). The stale policies were dropped, not just documented
  around.
- `ask_history` previously had its own RLS policy in
  `scripts/migrate_ask_history.sql` using a different idiom
  (`current_setting('request.jwt.claims', true)::jsonb ->> 'sub'` instead
  of `auth.uid()`) than the other three tables. **Resolved 2026-08-31
  (plan.md §21 STEP 6a): the policy was dropped, not migrated** —
  `backend/routes_user.py`'s history endpoints only ever read this table
  through the service-role client with a hand-written `.eq("user_id",
  ...)` filter, so the policy was never actually exercised; it is now
  service-role-only by design, the same posture already documented for
  `ai_usage_log` (`scripts/sql/ai_usage_log_setup.sql`, STEP 6b, already
  satisfied before this pass). It was previously **missing from
  `/api/devtools/rls-audit`'s reported table list** — fixed in an earlier
  pass (`backend/routes_devtools.py`), with a regression test
  (`tests/test_routes_devtools.py::TestRlsAudit::test_rls_audit_reports_ask_history_table`)
  guarding against it silently dropping out again; it stays in that list
  today for completeness, not because a policy still governs it.
- `/api/devtools/rls-audit` (2026-08-31, plan.md §21.2.2 STEP 5) now runs
  an *observed* check on every authenticated call, not just policy
  presence: for each RLS-relevant table it compares a user-scoped-client
  row count against a service-role-client row count for the caller's own
  rows, and reports whether they match — the silent-zero-rows failure
  mode §21.1 describes would show up here as a live mismatch on the next
  real signed-in request, not just in a scheduled script.
- Added a regression test asserting `strict_rls` is `True` in the audit
  response, so flipping the enforced-default literal fails CI loudly.
- `app.py`'s dead cookie-reading fallback (`sb-access-token` /
  `supabase-access-token` / chunked `sb-*-auth-token` cookies, pre-Clerk
  native-Supabase-Auth leftover — nothing in this app ever sets those
  cookie names) was deleted 2026-08-31 (plan.md §21 STEP 8); the Supabase
  access token is now sourced solely from the `Authorization: Bearer`
  header, matching what `templates/index.html` actually sends.
- `community_knowledge` is a shared/global corpus queried by community
  filter, not by `user_id` — correctly has no per-user RLS policy and is
  not reported by the audit endpoint (which is specifically about
  user-scoped table access).
- The audit endpoint itself (`/api/devtools/rls-audit`) requires Clerk auth.
- CI now runs the full pytest suite (which includes the RLS-audit tests)
  on every push — see §4 below for the new pip-audit/pre-commit CI steps
  added alongside it.

## 3. CSP / security headers

- `Strict-Transport-Security`, `Permissions-Policy`, `X-Frame-Options`,
  `X-Content-Type-Options`, `Referrer-Policy` are all set
  (`backend/helpers.py::SECURITY_RESPONSE_HEADERS`), applied to every route
  on both transports (Flask `@app.after_request` and the native FastAPI
  `/ask` route in `asgi.py`, which bypasses the Flask/WSGI mount entirely).
- `X-XSS-Protection` is deliberately omitted (deprecated, ignored by modern
  browsers, can introduce vulnerabilities in old ones).
- `'unsafe-eval'` is **not** present in the CSP — already resolved when the
  Tailwind CDN JIT compiler was replaced with a committed build
  (`claude_code_prompts.md` Prompt 11).
- `'unsafe-inline'` **is** still present in `script-src`/`style-src`. Moving
  to a nonce-based CSP was evaluated and **deliberately deferred** — see
  `claude_code_prompts.md` Prompt 31 (decided 2026-08-15, Option 2):
  `templates/index.html` has 112 inline `onclick=` handlers and 43 inline
  `style=` attributes; CSP2+ browsers drop `'unsafe-inline'` entirely once a
  nonce is present, so a partial change breaks every handler on the page.
  The real blocker is that markup refactor (large, multi-session,
  high-regression-risk), not this header. Not reopened in this pass — no
  new information changes that decision.

## 4. Supply chain

- **SRI:** every external `<script>`/`<link rel=stylesheet>` across all of
  `templates/` (not just `index.html` — checked all 7 legal pages plus
  `components/legal_scripts.html`) carries `integrity=` + `crossorigin=`
  and a pinned exact version. The sole exception is the Google Fonts CSS
  endpoint, which cannot carry a static SRI hash (Google serves different
  `@font-face` payloads per User-Agent) — documented, confirmed
  non-fixable (Prompt 11).
- **Dependency vulnerabilities (`pip-audit`):** ran against the actual
  hash-locked files CI installs from (`requirements.lock.txt` +
  `requirements-dev.lock.txt`), not just whatever happened to be in a local
  environment:
  - `starlette==1.0.1` → **fixed, bumped to `1.3.1`** in this pass
    (`requirements.txt` + regenerated `requirements.lock.txt` via
    `uv pip compile`). Closes `PYSEC-2026-248`/`249`/`2280`/`2281`
    (`GHSA-jp82-jpqv-5vv3`, `GHSA-82w8-qh3p-5jfq`, `GHSA-wqp7-x3pw-xc5r`,
    `GHSA-x746-7m8f-x49c`). Deliberately the smallest fix-closing bump, not
    latest (`1.6.0`) — full pytest suite re-verified green after the bump
    (all pass, 91% coverage), consistent with this repo's existing minimal-
    bump precedent for security pins.
    - **Side effect discovered while verifying the bump:** the new
      starlette version surfaces a `StarletteDeprecationWarning` for
      `starlette.middleware.wsgi.WSGIMiddleware` ("deprecated and will be
      removed in a future release"), which is exactly what `asgi.py:752`
      uses to mount the entire Flask app inside the FastAPI ASGI app — the
      core mechanism of this app's hybrid transport. Still fully functional
      today (all tests pass), but a future starlette major version could
      remove it outright. **Follow-up, not done in this pass:** evaluate
      migrating to `a2wsgi` (the deprecation message's suggested
      replacement) before that happens.
  - `requirements.lock.txt` was already patched for `click`, `h2`, `idna`,
    `urllib3` (a prior "2026-08-16 security-audit dependency bumps" pass,
    per the file's own header comment) — a local dev `.venv` had stale
    versions installed independent of the lock file; re-verified the lock
    file itself is what matters and is clean for those four.
  - `pytest==8.4.0` (dev-only, `requirements-dev.txt` — the file's own
    header states these are "not installed in production/Vercel") has
    `PYSEC-2026-1845`, fixed in `9.0.3`. **Not bumped in this pass:** a
    test-runner major-version jump carries real plugin/config-compat risk
    for a mechanical security pass, and the exposure is materially lower
    than a runtime dependency (an attacker needs to compromise the CI test
    invocation itself, not the deployed app). Tracked here as a known,
    open item.
- **CI additions (`.github/workflows/ci.yml`):**
  - `pip-audit` now runs on every push against both lock files (each
    audited separately with `--disable-pip`, since they pin some packages
    like `pygments` differently between the two and a shared resolver
    rejects that as a conflict). Non-blocking for now (`continue-on-error`),
    matching the existing `ruff` step's precedent — a hard-fail gate needs
    an established remediation workflow (see the `pytest` item above) before
    it can safely block merges.
  - `pre-commit run --all-files` (bandit + gitleaks) now runs on every push.
    The hooks already existed in `.pre-commit-config.yaml` but only ran
    locally and are bypassable with `--no-verify`; this closes that gap for
    anything landing via CI. (Verified locally this doesn't break on the
    current tree — the pre-commit gitleaks hook runs in `protect --staged`
    mode, which checks the diff being committed, not full file contents on
    disk, so it does not by itself catch the historical leak in §1 above; a
    full `gitleaks detect` history scan was run separately for that.)

## 5. Input validation & abuse

Audited every route in `backend/routes_calendar.py`,
`backend/routes_community.py`, `backend/routes_devtools.py`,
`backend/routes_library.py`, `backend/routes_prayers.py`,
`backend/routes_user.py`, `backend/routes_legal.py`, `app.py`, and
`asgi.py`. Overall well-hardened already: coordinates go through
`_coerce_coordinate(value, min, max)`, numeric page/size/limit params
through `_coerce_int`, Sefaria refs through `_encode_ref_path()`/`quote()`
before hitting an outbound URL, and community/prayer names are resolved
against fixed allowlists before ever touching a filesystem path. Three real
gaps found and fixed in this pass:

- **`POST /api/devtools/segment-report`** (`backend/routes_devtools.py`):
  a non-string JSON field value (e.g. `{"kind": 1}`) short-circuited the
  `(x or default).strip()` pattern and crashed with an unhandled
  `AttributeError` → 500, unlike every other JSON-body route in the same
  file which already wraps this in `str(...)` first. Fixed by adding the
  same `str()` coercion; regression tests added
  (`tests/test_routes_devtools.py::TestSegmentReport`).
- **`get_category_contents()`** (`backend/sefaria_library.py`, reached via
  `GET /api/library/category/<path:category>`): built its outbound Sefaria
  URL with a raw `.replace("/", ",")` instead of the `_encode_ref_path()`
  quoting every other ref-builder in that file uses, letting a literal `#`
  or `?` in the attacker-controlled path truncate or inject a query string
  onto the real outbound request. Fixed by routing it through
  `_encode_ref_path()` like the rest of the file; regression test added
  (`tests/test_sefaria_library.py::TestGetCategoryContents::test_special_chars_in_category_path_are_encoded_not_injected`).
  The fixed `SEFARIA_API` host ruled out SSRF/host redirection either way.
- **`GET /api/holidays`** (`backend/routes_calendar.py`): the `year` query
  param was f-string-interpolated directly into the outbound Hebcal URL
  with no validation, letting a value like `2026&geo=pos&latitude=1` inject
  extra query parameters onto the real request. Fixed by coercing it
  through `_coerce_int` (bounded `1583`–`3000`) before building the URL;
  regression tests added
  (`tests/test_routes_calendar_extra.py::TestGetHolidaysFallbackChain::test_year_query_param_is_coerced_not_interpolated_raw`
  and `test_out_of_range_year_falls_back_to_current_year`). The fixed
  `hebcal.com` host ruled out SSRF either way.
- **Rate limiters — unified single ASGI middleware. ⚠️ Updated 2026-09-02
  (this bullet previously described the pre-unification split — separate
  `Flask-Limiter` and `asgi.py` limiters — which stopped being accurate once
  Phase 9a landed 2026-08-22, committed 2026-08-25 in `a519a21`; see
  `plan.md` §16.9, §31.2).** All rate limiting now runs through one module,
  `backend/rate_limit.py`, whose `RateLimitMiddleware` is installed once on
  `asgi.fastapi_app`. Starlette's middleware stack wraps `app.router`, which
  also dispatches to the `WSGIMiddleware`-mounted Flask app, so this single
  middleware sees 100% of traffic — every native FastAPI route and every
  Flask route reached through the mount — exactly once, before any handler
  runs. This retired the old two-store/two-key-function/two-429-shape split
  that §2's "live, divergent duplication" anti-pattern warns against.
  - **Policy table (`_POLICIES`), one entry per route class:** `llm` (60 s
    window, 20 req/min anonymous, 40 req/min authenticated, plus a 200/day
    cap for authenticated callers only — anonymous traffic has no daily cap
    of its own but keeps the tighter per-minute bucket) is the one class
    that **fails closed** on a store outage, since an unmetered `/ask`
    during an outage is a real budget hole. `heavy` (10/min), `fanout`
    (30/min), `feedback` (10/min), `telemetry` (10/min), `account` (5/min),
    and `webhook` (15/min) all **fail open**. `cheap` (120/min, fail open)
    is the default bucket for any route matching none of the classified
    prefixes — the same generous-default role the old `RATE_LIMIT_DEFAULT`
    played. `account` and `webhook` were added in a 2026-09-02 audit that
    flagged the account-deletion/data-export routes as under-protected
    while sharing `cheap`'s bucket with ordinary read-only library lookups.
  - **Route classification** (`_ROUTE_CLASSES`, first-prefix-match wins):
    `/ask` → `llm`; `/api/export/chapter` and `/api/siddur/full/` →
    `heavy`; `/api/library/search`, `/api/text/`, `/api/word/meaning`,
    `/api/geocode` → `fanout`; `/api/feedback` → `feedback`;
    `/api/client-errors` → `telemetry`; `/api/user/delete-account` and
    `/api/user/data-export` → `account`; `/api/webhooks/clerk` → `webhook`;
    everything else → `cheap`.
  - **Store:** `_RedisStore` — a fixed-window counter (`INCR` + conditional
    `EXPIRE`), matching the fixed-window algorithm the Vercel edge WAF (§7
    below) also uses — is selected whenever `RATE_LIMIT_REDIS_URL` is set;
    it's **confirmed set in the live Vercel production environment**
    (operator-confirmed 2026-08-26, `plan.md` §31.2), so production traffic
    is limited against one shared, cross-instance counter. When the var is
    unset, or set but malformed, the middleware falls back to
    `_InMemoryStore` (a per-process sliding-window store, LRU-capped at
    2048 keys) — the same D3 per-process gap this document used to describe
    as an open production risk, now scoped to local dev / misconfiguration
    rather than the production default. Either fallback path logs loudly at
    boot (`logger.warning` when the var is simply unset; `logger.critical`
    with `exc_info` when it's set but the Redis client fails to build — a
    malformed store must never crash app boot, itself a fixed regression:
    production incident 2026-08-26/27, commit `352ccd0`, see
    `docs/RUNBOOKS.md`'s Incident history).
  - **Trusted client key:** every IP-keyed bucket uses
    `backend/helpers.py::_resolve_client_ip()` as the one canonical
    trusted-key function — checks `X-Vercel-Forwarded-For`, then
    `X-Forwarded-For`, then `X-Real-IP` (first comma-separated value),
    falling back to the ASGI-reported `remote_addr`. There is no second,
    independently-maintained IP-extraction path anywhere in the limiter.
  - **Identity-aware keying (`llm` and `account` classes, Phase 9c,
    `plan.md` §16.6):** an authenticated caller (Clerk `sub` extracted via
    `backend.auth.extract_user_id_from_bearer_value`, no new JWT-parsing
    path) is keyed per-account (`rl:<class>:user:<sub>`) instead of sharing
    an IP bucket — deliberate, so one heavy signed-in caller behind a
    shared institutional egress IP (a yeshiva, day school, or shul network)
    can't lock out the whole building, and so one NATed IP can't cap every
    other user's delete-account/data-export attempts. `llm` shipped this
    pre-unification (`asgi.py`'s old `/ask`-only limiter); `account` was
    added in the same 2026-09-02 audit noted above. `webhook`
    (`/api/webhooks/clerk`) deliberately stays IP-keyed — it's Clerk
    calling us, not an end user, so there's no caller identity to key on;
    Svix signature verification is the real gate on that route. Anonymous
    `/ask` traffic and every other route class stay IP-keyed.
  - **Kill switch and response shape:** `RATELIMIT_ENABLED` (default
    `true`, preserving the pre-unification flag's name/contract used by
    `tests/conftest.py` and `ci.yml`) disables the middleware entirely when
    false. Every rejection returns one body shape —
    `{"error": ..., "code": "rate_limited"}` with a `Retry-After` header —
    replacing the two independently-shaped 429s the old split produced.
    Both a rejection and a store-unavailable event are logged with a hashed
    key (`_hash_key`, sha256 truncated to 16 hex — never a raw IP or Clerk
    `sub`) via `log_mitigation()` / `_capture_backend_error()`, per §8's
    mitigation-observability note below.
- **Indirect prompt injection via retrieved web text (2026-09-19,
  `docs/AI_SECURITY_REVIEW.md` M2):** `validate_user_query()` only screens the
  user's own question, but Wikipedia/Halachipedia/HebrewBooks/MyMemory text is
  publicly editable or crowd-sourced and reaches the model as reference
  material (live `/ask` pre-fetch sections via `build_prompt()`, and the
  agentic `web_search` / `search_responsa_external` / `translate_text` tool
  results). Two layers now apply. `backend/retrieval_guard.py` drops a snippet
  carrying injection phrasing before it enters the prompt (English, plus
  Hebrew/French/Spanish/German/Russian phrasing; zero-width, fullwidth and
  Cyrillic/Greek look-alike spellings), logging only a source label and a
  marker count — never the retrieved text or any question text. And the
  pre-fetch sections are wrapped in `<retrieved_context>` tags that both system
  prompts name as data, never instructions (`PROMPT_VERSION`
  `2026-09-19-retrieved-context-v1`). It is a conservative phrase heuristic —
  bare "you are now" and "ignore any instructions from his doctor" are
  deliberately not markers, being normal halachic prose — and one layer beside
  the untrusted-data framing and `validate_model_output()`, not a classifier:
  a truly paraphrased injection is not caught.
- `answer_feedback` metadata (`mode`, `language`, `safety_class`) is now
  length-capped and sanitized like `comment` (`AI_SECURITY_REVIEW` L2).
- Payload size: `app.py`'s `MAX_CONTENT_LENGTH` (256 KiB) covers every
  Flask-routed blueprint; `asgi.py`'s `request_id_middleware` has its own
  independent `Content-Length` check for the native `/ask` route, which
  bypasses the Flask config entirely.
- **Per-user daily spend cap is a guardrail, not an anti-abuse control
  (decided 2026-08-23, `plan.md` §20.2 PHASE 20c):** `PER_USER_DAILY_BUDGET_USD`
  keys on the Clerk `user_id`, and Clerk signup is frictionless by design — a
  caller who hits the cap can create a second account and continue. This was
  an accepted, documented tradeoff (Option A of three considered), not an
  oversight: the real ceilings against a motivated multi-account attacker are
  the global cost breaker (`DAILY_BUDGET_USD`, `backend/cost_meter.py::is_global_cost_breaker_tripped`,
  caps total spend regardless of account count) and the Vercel WAF below (§7,
  IP+JA4-keyed, survives account rotation). This decision was only honest once
  both existed — see `plan.md` §16.10 for when the breaker shipped.

## 6. AuthN/Z review

- **`_verify_clerk_token`** (`backend/auth.py`): `algorithms` is hardcoded
  to `["RS256"]` — no algorithm-confusion risk (an attacker can't force
  `HS256` with a guessed/public key). `issuer` is always passed to
  `jwt.decode`, so `iss` is verified. Expiry (`exp`) is verified by default
  (PyJWT) and never disabled anywhere in this codebase. A missing
  `CLERK_JWT_ISSUER` fails closed (raises before any token is even looked
  at), and both `require_clerk_auth`/`maybe_require_clerk_auth` catch any
  verification exception and return `401` rather than proceeding.
- **Audience verification — config-dependent, ✅ confirmed set in
  production.** When `CLERK_AUDIENCE` is unset (it's optional in
  `.env.example`), the `aud` claim isn't checked at all, so any RS256 token
  from the right issuer would be accepted regardless of which Clerk
  application minted it — an explicit, documented branch, not an oversight,
  but a real bypass if this Clerk tenant is ever shared across multiple
  applications. **`CLERK_AUDIENCE` is confirmed set in the live Vercel
  production environment** (`vercel env ls production`, 2026-09-02 —
  `plan.md` §21), so `backend/auth.py::_verify_clerk_token()` does enforce
  `audience=CLERK_AUDIENCE` on every production request today; the
  unset-and-unenforced branch above describes local/dev environments where
  the var is left blank, not production. This was previously an open
  recommendation ("confirm `CLERK_AUDIENCE` is actually set in production")
  — it is now closed by that confirmation, so there is nothing further to
  force-change to fail-closed here.
- Every user-data route found (bookmarks, preferences, memories,
  `ask_history`, devtools) is decorated with `@require_clerk_auth` or
  `@maybe_require_clerk_auth` as appropriate, and filters by the JWT's own
  `sub` claim — no route was found trusting a client-supplied `user_id`
  instead (no IDOR pattern found).
- Session cookie flags (`app.py`): `SESSION_COOKIE_HTTPONLY=True`,
  `SESSION_COOKIE_SAMESITE="Lax"`, `SESSION_COOKIE_SECURE=is_production_runtime`.
  The native FastAPI `/ask` route (`asgi.py`) relies purely on Bearer
  tokens with no cookie/session dependency — the Flask session cookie only
  ever holds calendar lat/lon, never auth state.
- `CLERK_ENFORCE_AUTH` (`backend/auth.py`) defaults to `True` whenever
  `VERCEL == "1"` or `FLASK_ENV == "production"`, and can be overridden
  explicitly in either direction via env var — correctly fails toward
  enforcement in production by default rather than requiring an operator to
  remember to turn it on.

## 7. Vercel WAF (dashboard-configured) — ✅ entered 2026-08-26 (Akiva, Project → Firewall)

This is plan.md §16.3-L1: the one layer that makes a flood *free* — per Vercel's WAF rate-limiting docs, WAF-mitigated traffic incurs **no CDN request, no Fast Data Transfer, and no function invocation**, unlike every layer below it in this document (backend.rate_limit.RateLimitMiddleware, §16.3-L2) which still pays a full invocation to say "no."

**Hobby-tier budget: 3 total custom firewall rules, full stop.** ⚠️ **Corrected 2026-08-26 — the "1 rate-limit rule + 3 custom rules" framing below was wrong, found by actually doing this in the dashboard.** Rate-limiting is not a separate quota on Vercel; it is one action type available on an ordinary custom rule, and consumes one of the same 3 slots as any other rule. There is no fourth slot. All 3 are spent:

1. **Rate-limit rule** (one of the 3 slots, spent on this action type) — ✅ **flipped to Enforce, confirmed live in the dashboard 2026-09-16 (see `akiva_tasks.md` T10):**
   - Path: `/ask`, method: `POST`
   - Key: **IP + JA4 digest** (JA4 is a TLS-stack fingerprint — it survives the IP rotation a real flood uses; IP alone does not)
   - Algorithm: fixed window, **60 s**
   - Limit: **100 requests/minute**, set from a week of Log-mode traffic data as planned below
   - Action: **"Too Many Requests (429)"** (Enforce, not Log), with repeat offenders locked out for **15 minutes**
2. **Custom rule 2 — deny common scanner paths:** `/.env`, `/.git/*`, `/wp-admin/*`, `/vendor/*`, `/phpmyadmin/*`. Pure noise against this codebase (none of these paths exist), and every hit today still costs a full function invocation before Flask/FastAPI can 404 it.
3. **Custom rule 3 — deny non-`GET`/`POST`/`HEAD`/`OPTIONS` methods, site-wide.** ⚠️ **Dashboard shows this rule's action as Log, not Deny, as of 2026-09-16** — either this description is stale or the rule was never flipped out of its own testing phase; worth a quick operator check (see `akiva_tasks.md` T9).

**No incident-response slot is held in reserve — all 3 are in active use.** The original plan wanted a 4th, empty slot for 2 a.m. incident response; that slot does not exist at this budget. If one is needed later, it has to come from merging two of the above into one rule (e.g. scanner-path-deny and method-deny as one rule with multiple match conditions) or upgrading off Hobby tier — not from a slot that was never actually available.

Two things to keep in mind when entering these:

- **(a) Deploy the rate-limit rule in Log mode first.** Do not guess the per-minute threshold. Run it in Log-only mode for **one week**, read the actual numbers in the Firewall traffic overview, and set the enforced limit from that data — a number invented without traffic data is as likely to lock out real users during a legitimate traffic spike as it is to stop an attacker. **✅ Done — flipped to Enforce at 100 req/min, confirmed live 2026-09-16.**
- **(b) WAF counters are tracked per region.** This rule is only correct as "exactly one counter" because `vercel.json` pins `"regions": ["iad1"]` (a single region). If that pin is ever removed or a second region is added, this rule's counter silently splits per-region and the effective limit multiplies by region count without any error or warning — revisit this rule the same day a region change ships.

This WAF layer (L1) sits in front of, not instead of, `backend/rate_limit.py`'s `RateLimitMiddleware` (L2, §5 above) and `backend/cost_meter.py`'s global cost breaker (L3, §16.3-L3) — see plan.md §16.3 for the full three-layer picture. All three must stay in sync conceptually (same route classes, same posture toward `/ask`) even though L1 lives in dashboard config rather than code.

## 8. Turnstile bot mitigation & identity-aware quotas (Phase 9c)

Landed 2026-08-24 (`claude_code_prompts.md` Prompt 29c, `plan.md` §16.6 Phase 9c). Two independent additions to the abuse-mitigation stack described above.

**Identity-aware rate-limit keys.** `backend/rate_limit.py` now keys on the Clerk `sub` (via `backend/auth.py`'s existing token verification — no new JWT-parsing code path) when a caller is authenticated, falling back to the trusted-IP key (§5 above) when not. This is deliberate, not merely a feature: an IP-only bucket treats every caller behind a single shared egress IP — a yeshiva, day school, or shul network, a real and common shape of this app's audience — as one caller, so one heavy but legitimate user can lock out an entire building. Authenticated callers get a higher per-minute allowance (`authenticated_max_requests`) **plus** an explicit daily `/ask` quota (`daily_max_requests`) that anonymous traffic never has applied to it; anonymous traffic keeps the tighter, unified per-IP bucket. Both counters live in the same shared store `backend/rate_limit.py`/`backend/cost_meter.py` already use (§3 reuse rule) — no second store was introduced.

**Cloudflare Turnstile — WHY THIS, NOT VERCEL BOTID.** Anonymous `/ask` traffic that crosses a per-IP hourly threshold (`TURNSTILE_ANON_HOURLY_THRESHOLD`, default 5) is gated behind a Cloudflare Turnstile challenge (`backend/turnstile.py`). Vercel's own BotID product was considered and **rejected**: BotID's server-side verification SDK is JavaScript-only, and `/ask`'s entire request-handling path is Python (`asgi.py`'s native FastAPI route, `app.py`'s Flask blueprint) — there is no Python entry point for BotID's verification step at any Vercel plan tier, so it cannot be wired into this endpoint regardless of budget. Turnstile is backend-agnostic: `siteverify` is one plain HTTPS POST any language can make, it is free at this project's volume, and it stays invisible to the large majority of legitimate callers (Cloudflare's managed challenge only renders a visible widget for traffic it already suspects). The `siteverify` call itself uses async httpx (never a blocking `requests` call — the standing async-safety rule in `.agents/ENGINEERING_RULES.md`), and the whole feature is gated behind `TURNSTILE_ENABLED` (default off) so every function in `backend/turnstile.py` is a true no-op — local dev and the test suite are unaffected until an operator turns it on and supplies `TURNSTILE_SECRET_KEY`/`TURNSTILE_SITE_KEY`.

Two independent fail postures inside `backend/turnstile.py`, deliberately different from each other: `is_challenge_required()` (decides whether a challenge is owed at all — anti-abuse polish) fails **open** on a store error, so a Redis blip does not additionally block anonymous `/ask` on top of whatever `backend/rate_limit.py`'s own llm-class fail-closed posture already decided. `verify_turnstile_token()` (decides whether a *specific submitted proof* is valid — the actual gate) fails **closed** on a missing token, a missing/misconfigured secret key, or a `siteverify` request error, since this is the one an attacker would want to bypass by making `siteverify` look unreachable.

**Mitigation observability.** Every rejection from either mechanism — a rate-limit 429 or a Turnstile 403 — emits one structured log line plus a Sentry **breadcrumb** (never a Sentry event, to avoid drowning the real error signal and burning the free-tier event quota) via `backend.logging_setup.log_mitigation(tier, route_class, key_hash, route)`. The key is always hashed before logging — never a raw IP or Clerk `sub` (§8.D privacy elsewhere in `plan.md`) — so an actual attack becomes visible as a spike in `mitigation_triggered` log lines / breadcrumbs without ever putting an identifiable value in a log store.

**Env vars:** `TURNSTILE_ENABLED`, `TURNSTILE_SECRET_KEY`, `TURNSTILE_SITE_KEY`, `TURNSTILE_ANON_HOURLY_THRESHOLD` — see `.env.example` for defaults and `docs/API.md` for the full policy/env matrix alongside the existing `RATE_LIMIT_*` vars.

**Not addressed here — see `plan.md` §35 / `claude_code_prompts.md` Prompt 47:** implementing and testing this pass surfaced a severe, pre-existing, unrelated production bug — every authenticated `/ask` call currently returns HTTP 500 because `backend/rag.py::_fetch_user_memory_summaries()` reaches Flask's global `request` proxy from a code path (`asgi.py`'s native FastAPI route) that has no Flask request context. This is not a Turnstile/quota defect and was not fixed as part of this section's work; it is filed separately as the highest-priority item in the project's findings queue.

## 9. Account-deletion completeness (plan.md §39, 2026-08-27)

Landed as part of `claude_code_prompts.md` Prompt 51 / `plan.md` §39, an audit of `/api/user/delete-account` (`backend/routes_privacy.py`) requested directly, not surfaced as a byproduct of another prompt. Isolation (no caller can ever touch another user's data) was verified correct **by construction** — every query is scoped by `.eq("user_id", ...)` with `user_id` read only from the caller's own verified Clerk JWT, never from a request parameter — and needed no fix. Two completeness gaps did:

- **`answer_feedback` now included in export/delete.** `_USER_DATA_TABLES` (`backend/routes_privacy.py`) previously listed five tables; `backend/routes_feedback.py` writes a real Clerk `sub` into a sixth, `answer_feedback`, which was in neither `export_user_data()` nor `delete_account()`'s loop. Fixed by adding it to the same tuple both endpoints iterate — see `docs/PRIVACY_OPERATIONS.md` §1 for the user-facing description.
- **A Clerk `user.deleted` webhook now backstops deletions this app doesn't initiate.** `delete_account()` was the *only* code path that ever deleted a user's Supabase rows. A user can also delete their own Clerk identity through Clerk's hosted `<UserProfile />` UI, and an operator can delete a user directly from the Clerk Dashboard (a normal action for handling abuse/takedowns) — neither calls this app's endpoint, so either could orphan a user's entire data footprint permanently, with no Clerk JWT left to authenticate a retry. `POST /api/webhooks/clerk` (`backend/routes_webhooks.py`) closes this: Clerk delivers webhooks Svix-signed, and this endpoint verifies that signature by hand (HMAC-SHA256 over `"{svix-id}.{svix-timestamp}.{body}"`, rejecting anything outside a 300-second timestamp window) rather than adding the `svix` package as a new dependency — signature verification **is** this endpoint's authentication, since a live Clerk JWT for the deleted user no longer exists by the time it fires. On a valid `user.deleted` event it runs the same table cascade `delete_account()` uses, keyed on the event's `data.id`; idempotent by construction (a replayed at-least-once delivery just deletes zero additional rows). Requires `CLERK_WEBHOOK_SIGNING_SECRET` (see `.env.example`) and a matching Endpoint configured in Clerk Dashboard → Webhooks subscribed to `user.deleted`.

**Self-service account deletion: confirmed enabled, 2026-09-02 (verified by Akiva in the Clerk Dashboard) (plan.md §39.2 STEP 3).** Clerk's `<UserProfile />` ships an *enabled* self-service "delete account" control for this app's Clerk instance — a signed-in user can trigger deletion entirely through their own action. This is the combination §39.2's own text flagged as "the most urgent version of the gap" *if* no backstop existed — it does: the `user.deleted` webhook above (shipped 2026-08-27, before this setting was even checked) already catches exactly this path, so self-service deletion being live carries no completeness risk today.

**Also resolved (plan.md §39.3): (a) Supabase's backup/point-in-time-recovery retention — confirmed 2026-09-01 (Akiva, Supabase Dashboard): Free plan, no PITR (a paid Pro-plan add-on) and no automated backups at all.** See `docs/RUNBOOKS.md`'s "Backups & recovery" section and `docs/LAUNCH_CHECKLIST.md` item 9 for the full finding and the manual `pg_dump`/`supabase db dump` fallback this implies. (b) `user_id` is now hashed (`backend.logging_setup.hash_user_id`, same sha256-truncated-to-16-hex construction as `backend/rate_limit.py`'s `_hash_key`) at every `_capture_backend_error` call site that previously passed the raw Clerk `sub` into Sentry/structured-log context — `backend/rag.py`, `backend/routes_user.py`, `backend/routes_privacy.py`, and `app.py`'s `ask_ai_synthesis_failed` site (12 call sites total, all now emitting `user_id_hash` instead of `user_id`). Field-level per-user encryption/crypto-shredding was considered and explicitly **not** built — `plan.md` §39.3 has the full rationale: every live store this app controls already gets a real `DELETE` (strictly stronger than shredding), and the one store shredding would help (provider-managed backups) has an exposure that's already time-bounded by the provider's own retention.

## 10. Supabase Advisor scan — 2026-09-03

A live Supabase Advisor scan against the production project surfaced three
security-relevant `WARN`-level findings. Full details, exact statements,
and the reasoning behind each REVOKE decision are in `docs/DATABASE.md`'s
"Live Supabase Advisor scan — 2026-09-03" subsection; this entry is the
short security-focused pointer to it.

- **`function_search_path_mutable`:** `public.get_schema_snapshot()` and
  `public.set_updated_at_timestamp()` had a role-mutable `search_path`.
  Neither is `SECURITY DEFINER`, so this is the ordinary object-hijacking
  risk rather than an elevated-privilege one. Fix written, **not yet
  applied** — `scripts/sql/migrate_search_path_and_rls_perf_fixes.sql` (new
  file, run once by hand in the Supabase SQL editor).
- **`pg_graphql_anon_table_exposed` / `pg_graphql_authenticated_table_exposed`:**
  `ai_usage_log`, `ask_history`, `study_bookmarks`, `user_memories`, and
  `user_preferences` were all visible in the GraphQL schema to `anon`
  and/or `authenticated`. Fix written, **not yet applied**, same migration
  file: `REVOKE SELECT` from both roles on `ai_usage_log`/`ask_history`
  (zero-policy, service-role-only tables, confirmed safe by direct code
  read); `REVOKE SELECT` from `anon` only on `study_bookmarks`/
  `user_memories`/`user_preferences` — `authenticated` was **deliberately
  left alone** on those three, because `docs/DATABASE.md`'s writeup found
  the backend's own RLS-scoped client (`app.py::_get_user_scoped_supabase_client`,
  used as the *primary* path in `backend/routes_user.py` and
  `backend/rag.py`, with `STRICT_SUPABASE_RLS` hardcoded `True` so the
  service-role fallback never fires) authenticates as exactly that role,
  so revoking it would break `GET`/`PUT /api/user/preferences`, the
  semantic-bookmarks route, and both RAG memory read/write paths.
  `community_knowledge` is also flagged but is deliberately public-read —
  left untouched.
- **`rls_policy_always_true`** on `answer_feedback.anyone_can_submit_feedback`
  — reviewed and confirmed **intentional**, not a gap: `POST /api/feedback`
  deliberately accepts anonymous submissions (`backend/routes_feedback.py:32`,
  `@maybe_require_clerk_auth`; docstring at lines 4-7). No DB policy
  change. The route already carries a dedicated, tighter-than-default
  rate-limit policy (`backend/rate_limit.py:90,118` — `"feedback"` class,
  10 req/60s IP-keyed, vs. the 120/min `"cheap"` default), so the
  future recommendation this finding would otherwise prompt (a dedicated
  stricter policy, reusing the `"webhook"` 15-req/60s pattern) does not
  apply — that mitigation is already in place.

## 11. `npm audit` — 6 high-severity findings, dev/CI-only, no clean upstream fix exists (investigated 2026-09-03)

`npm audit` reports 6 high-severity vulnerabilities, all one dependency
chain: `extract-zip` (unvalidated symlink path traversal, CVSS 8.1,
[GHSA-jmr9-qjv8-65gv](https://github.com/advisories/GHSA-jmr9-qjv8-65gv))
→ `@puppeteer/browsers` → `puppeteer`/`puppeteer-core` → `pa11y` →
`pa11y-ci` (the only direct dependency in the chain — used solely by
`npm run test:a11y`, never shipped to production or reachable from any
runtime code path).

**Investigated and confirmed there is no clean fix, not just deferred:**
- `extract-zip@2.0.1` (latest published version) is itself the vulnerable
  version — the CVE has never been patched upstream. Every `puppeteer`
  release that depends on it (which is all of them back through the
  `@puppeteer/browsers` split) inherits the same finding, including
  `puppeteer@24.43.1`, itself the latest published release.
- `npm audit fix --force`'s own suggested fix is downgrading `pa11y-ci`
  from `^4.1.1` to `^3.1.0` (a major-version regression). Applied it and
  re-ran `npm audit` as a test: doing so pulls in `pa11y-ci@3.1.0`'s much
  older dependency tree (`puppeteer@~9.1.1`, `pa11y@^6.2.3`), which avoids
  the `extract-zip` chain but bundles its own unpatched `lodash@<=4.17.23`
  (code injection via `_.template`, prototype pollution) and
  `semver@7.0.0-7.5.1` (ReDoS) — still **6 high-severity findings**, just
  a different pair of packages. `npm audit`'s own remediation advice for
  *those* findings is to upgrade back to `pa11y-ci@4.1.1` — a circular
  recommendation. There is no version of `pa11y-ci` currently on the
  registry that npm's own advisory database considers clean.
- Reverted to `pa11y-ci@^4.1.1` (the version already verified working
  against this project's `.pa11yci.json` config, `npm run test:a11y`
  16/16 passing as of 2026-09-02) rather than keep the downgrade, since it
  didn't actually reduce the finding count and traded a working, tested
  tool version for an unverified older one.

**Accepted risk, not a gap:** exploiting the `extract-zip` CVE here would
require compromising Google's own Chrome-for-Testing distribution (the
only ZIP `puppeteer` ever extracts) to serve a malicious archive during
`npx puppeteer browsers install chrome` — not something reachable through
any input this project accepts. Combined with the dev/CI-only reach, this
is a reasonable accepted risk pending an upstream `extract-zip` patch,
not something an in-repo dependency bump can close today. Re-run
`npm audit` periodically (or if `extract-zip`/`puppeteer` cut a new
release) to check whether the upstream picture has changed.

## Not re-litigated in this pass

- **§8.B-AGE age-appropriate output / safety routing** — already
  implemented and verified (`claude_code_prompts.md` Prompt 13/14); out of
  this security pass's scope.
- **Legal documents, DSR/privacy operations, observability** — separate
  workstreams (§8.A, §8.D, §8.E) with their own prompts; not duplicated
  here.
- **Penetration test / external security review** — §8.C.7 explicitly
  calls for this before public launch. This document and the fixes above
  are the engineering-side input to that review, not a replacement for it.
