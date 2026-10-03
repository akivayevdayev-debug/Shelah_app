# Runbooks

Operational procedures for Sh'elah: deploy, rollback, incident response, and
cost guardrails. It covers Vercel spend guardrails, region/function config,
startup-cost gating, observability, uptime monitoring, backups & recovery,
incident response, rollback, the deploy checklist, and
rate-limiting/cost-ceiling status. Each section records its own
configuration status inline, with the date it was last confirmed.

## Vercel spend guardrails (dashboard actions, not code)

These are **dashboard-only actions** the project owner must take manually;
nothing in the repo can configure them. Recommended values, given the app's
actual cost profile (`/ask` is I/O-bound — Provisioned Memory dominates over
Active CPU):

1. **Spend Management hard cap** (Vercel dashboard → Settings → Billing →
   Spend Management): set a hard monthly cap that stops serving traffic
   before an unexpected bill accrues, rather than a soft alert-only
   threshold. Start conservative (e.g. 2–3× the expected steady-state
   monthly cost observed in a 30-day baseline) and
   revisit once real usage data exists. A hard stop is safer than a soft
   alert for a solo-operated project with no on-call to react to a 2am
   notification.
2. **Usage Notification thresholds** (Vercel dashboard → Settings →
   Notifications → Usage Alerts): configure alerts at 50% and 80% of the
   Spend Management cap (or of the Hobby free-tier allowances, if still on
   Hobby) for each of the four billed meters — Active CPU, Provisioned
   Memory, Invocations, Fast Origin Transfer — so a traffic spike, crawler,
   or runaway agent loop surfaces before it becomes a bill.
3. **AI provider spend caps** (independent of Vercel): set hard monthly
   budget caps on the Anthropic and Google (Gemini) billing dashboards
   directly — these are the other real cost surface and are not bounded by
   anything in the Vercel dashboard. `backend/cost_meter.py` logs token/USD
   per call for visibility, but a provider-side hard cap is the actual
   backstop.

**Status:** the last recorded state (2026-07-30) was not yet configured;
this repository cannot see the Vercel or provider dashboards, so nothing
here confirms they have since been applied. This section documents the
recommended values; someone with dashboard access must apply and re-check
them. Re-visit the cap value once a 30-day usage baseline is captured. The
in-repo layers that *are* verified live are the global daily budget cap
(`DAILY_BUDGET_USD`, below) and the edge WAF rate limit
([`docs/SECURITY.md`](SECURITY.md) §7).

## Region & function configuration

`vercel.json` pins `"regions": ["iad1"]` (us-east) — the cheapest tier
($0.128/CPU-hr, $0.0106/GB-hr) alongside `pdx1`/`cle1`. Revisit if the user
base skews non-US-East and observed latency justifies a different region.

`"fluid": true` is deliberately **not** set in `vercel.json` — that flag
changes the execution model and should only be flipped after
confirming the project's current Fluid state in the dashboard (Fluid is
default for projects created after 2025-04-23; this project's actual state
has not been confirmed here).

## Startup-cost gating

`validate_all_customs_at_startup()` (called from `app.py`) now skips by
default in production imports (`VERCEL=1` or `FLASK_ENV=production`) since
it is a developer/CI safety net, not a production request-path need, and
bills Active CPU on every cold start. Override explicitly with
`VALIDATE_CUSTOMS_AT_STARTUP=1` (force on) or `=0` (force off) in any
environment. CI does not set `VERCEL`/`FLASK_ENV=production`, so the
validation still runs there by default.

`backend/zmanim_engine.py`'s `TimezoneFinder()` instance is now a lazy
singleton (`_get_timezone_finder()`) — it loads a spatial boundary index on
construction, so building it eagerly at import time billed every cold start
even for requests that never resolve a timezone.

## Observability

**Structured logging & request IDs.** `backend/logging_setup.py`'s
`setup_logging()` configures the root logger to emit single-line JSON
records. Every incoming HTTP request is assigned a `request_id`
(`contextvars`-scoped, generated if the client didn't send an
`X-Request-Id` header) that is echoed back on the response and attached to
every log record produced while handling that request:

- **Flask** (`app.py`'s `bind_request_id_context`/`log_request_completion`
  `before_request`/`after_request` hooks) binds it for every WSGI request,
  including ones served directly (not via `asgi.py`).
- **ASGI** (`asgi.py`'s `request_id_middleware`) binds it for every request
  through the FastAPI app — native routes (`/ask`) and the WSGI-mounted
  Flask app alike — and writes a generated id back into the request
  headers so the two layers agree on one id per client request.
- **Thread-pool work** (`app.py`'s `_THREAD_POOL`, `routes_prayers.py`'s
  siddur-fetch pool) does not inherit contextvars automatically — call
  sites use `backend.logging_setup.submit_with_context()` instead of
  `pool.submit()` directly to carry the id across the thread boundary.
- **asyncio tasks** (`asgi.py`'s `ask_async` `asyncio.create_task`/
  `asyncio.to_thread` calls) inherit contextvars automatically per asyncio's
  task-creation semantics — no extra wiring needed once bound at the top of
  the request.

**Sentry.** Conditionally initialized in `backend/logging_setup.py` — a true
no-op (no import errors, no network calls) unless `SENTRY_DSN` is set.
**Set `SENTRY_DSN` in the Vercel project's production environment variables**
to activate reporting; code alone does nothing. Every error routed through
`_capture_backend_error()` (Sentry, structured log line, and optional
`ERROR_LOG_WEBHOOK_URL` POST) carries `request_id` so a Sentry issue can be
correlated back to the exact request's log lines.

**Cost metering.** `backend/cost_meter.py`'s `record_llm_call()` writes
token counts + estimated USD cost to Supabase `ai_usage_log` for every
outbound model call, tagged with `request_id`. Covered call sites: the
async Gemini/Claude calls in `backend/claude.py` (`ask_ai_async`'s
`_call_gemini_httpx_model`/`_call_anthropic_httpx_model`, shared by both
the FastAPI `/ask` route and the sync `_call_claude_model` bridge), the
sync Gemini primary call (`_call_gemini_model`, recorded by its caller
`_call_primary_model`), and `summarize_with_gemini` (semantic-bookmark
summaries, bridged onto the async API via `asyncio.run` since it's called
from a Flask WSGI route).

**Daily budget alert.** `backend/cost_meter.py`'s
`check_daily_budget_and_alert()` sums the current UTC day's `ai_usage_log`
cost and, once it reaches `DAILY_BUDGET_USD`, raises through
`_capture_backend_error` (Sentry + structured log + webhook). Disabled
(no Supabase read at all) unless `DAILY_BUDGET_USD` is set — it's an
opt-in guardrail, not a hard spend cap (see the spend guardrails above for
the actual hard caps, which live in the Vercel/Anthropic/Google billing
dashboards, not in this repo). Exposed at `GET /api/devtools/budget-check`
and wired into `vercel.json`'s `crons` (`0 13 * * *`, once daily —
Hobby-plan compatible). **Set `CRON_SECRET`** in the Vercel project env vars: this
route (and `retention-enforce`) requires the
`Authorization: Bearer <CRON_SECRET>` header Vercel Cron sends
automatically once that var exists. `budget-check` fails closed — it
returns `503` when `CRON_SECRET` is unset and `401` on a mismatch.

**Status:** request-ID propagation, Sentry wiring, and cost-meter coverage
are in the repo. `SENTRY_DSN`, `DAILY_BUDGET_USD`, and `CRON_SECRET` are
operator actions (Vercel dashboard env vars) — none are set by default.
Operator-confirmed in production: `DAILY_BUDGET_USD=10.00` (live since
2026-09-01, reconfirmed 2026-09-04) and `CRON_SECRET` (2026-09-06); the
`SENTRY_DSN` value is not recorded in this repo. When the budget variable
is missing, `backend/cost_meter.py` logs a warning and
`/api/stack/health` reports `security.cost_breaker.configured: false`.

## Uptime monitoring

Uptime monitoring conflicts with the project's "stay inactive"
invocation-cost rule: an external monitor hitting the function-backed
`/api/health` every 30 seconds is **2,880 invocations/day (~86k/month)** of
pure Vercel cost with no user-facing value, and each hit can wake a paused
Fluid instance. The decided resolution is a two-tier check:

1. **Primary check — a static, edge-served asset.** Point it at any file
   under `/static/`, e.g. `https://<domain>/static/favicon.svg`.
   `vercel.json`'s `headers` rule (`"source": "/static/(.*)"`) sets
   `Cache-Control: public, max-age=3600`, and files under `static/` are
   served directly rather than through `api/index.py`, so this check costs
   zero function invocations and zero Active CPU/Provisioned Memory no
   matter how often it runs. **Interval: 5 minutes**, not 30 seconds — the
   number settled on for a solo-operated project; only shorten it if a real incident shows 5 minutes is too slow
   for an acceptable downtime window.
2. **Secondary check — the function-backed deep check, at low frequency.**
   Point a second, less frequent check at `/api/health` (aliases to
   `/api/stack/health`, `backend/routes_devtools.py:253-260`). Recommended
   interval: **15–30 minutes** — enough to catch "process is up but a
   dependency is broken" without meaningfully adding to the invocation
   budget.
   - **Configuration caveat that matters:** both `/api/health` and
     `/api/stack/health` are gated by `@require_clerk_auth`
     (`backend/routes_devtools.py:58,254` — a security-audit fix, since the
     route body reveals rate-limit thresholds and Clerk/Supabase
     configuration state). An anonymous uptime monitor has no Clerk
     session, so it will always get **`401 Unauthorized`**, never the
     route's real JSON. This is the same behavior
     `scripts/verify_integrations.py` already relies on — a comment at
     `backend/routes_devtools.py:65-66` states outright that a 401 here
     means "the app is up and correctly protecting this route." **Set the
     monitor's expected status to treat `401` as healthy** and alert only
     on `5xx`/timeout/connection failure; a monitor configured to expect a
     bare `200` will page every 15–30 minutes for nothing.
   - If a genuine `200`-with-real-status check is preferred over teaching a
     monitor to treat 401 as healthy, point the secondary check at
     `/api/devtools/heartbeat` instead (`backend/routes_devtools.py:96-136`)
     — deliberately public/unauthenticated (it already backs the in-app
     devtools-inspector panel), and its body reflects Clerk/Supabase
     config presence plus one live library check without leaking
     rate-limit thresholds. Either endpoint satisfies the "secondary,
     low-frequency deep check" requirement.
3. Both endpoints already satisfy the "keep it cheap" requirement:
   `stack_health()`'s `status_summary()` call
   (`backend/health_check.py:190-193`) only reads an in-memory
   circuit-breaker dict — no upstream HTTP fan-out, no Supabase round-trip
   on the default path. If a future change adds a live upstream probe to
   this path, gate it behind an explicit query flag and re-check this note.

**Provider:** nothing in this repo depends on which uptime provider is
used — any service that can hit two URLs on independent schedules with
per-check status-code overrides works. The intended provider is **Sentry
Uptime Monitoring** at a 5-minute interval (it's already in this stack for
error reporting, and uptime checks don't count against the
5,000-events/month error quota). UptimeRobot (free tier) or Better Uptime
are equally workable alternatives if Sentry Uptime isn't enabled.

**Status: not yet configured by the operator.** No uptime monitor is wired
up as of 2026-10-02 — this section records the intervals and target
endpoints so whoever configures the first monitor has concrete values to
enter rather than re-deriving them.

**2026-09-16 clarification:** the operator reported this item as "set up
with Upstash Redis in prod." Upstash Redis is a separate, already-done
piece of infrastructure (it's the backing store for
`backend/rate_limit.py`'s rate limiter, `RATE_LIMIT_REDIS_URL`), not an
uptime-monitoring product — it has no capability to poll a URL on a
schedule or page anyone on downtime. Uptime monitoring specifically (an
external service hitting the two endpoints above on the two schedules
above) is still not configured as far as this repo can verify. If Upstash
offers some monitoring feature this note isn't aware of, or if a
different tool was actually used, this section should be corrected with
whichever provider and URLs were actually configured — but "Redis is
live" and "uptime alerts are live" are two different claims, and only the
first is currently true.

## Backups & recovery

### Supabase database

**Confirmed 2026-09-01 (Akiva): this project is on Supabase's Free plan.**
Point-in-time recovery (PITR) is a paid Pro-plan add-on and does not exist
on this project — there is no automated backup or restore-to-a-point-in-time
capability at all. The manual/on-demand path below (`pg_dump`/`supabase db
dump`) is the *only* backup this project has; nothing runs it on a
schedule today.

**Manual/on-demand backup:**

- Supabase dashboard → Database → Backups → trigger or download a
  snapshot (exact options depend on plan tier, per above).
- Or from a machine with database credentials: `supabase db dump -f
  backup.sql` (Supabase CLI), or a direct `pg_dump
  "$DATABASE_CONNECTION_STRING" -f backup.sql`.

**Restore:**

- No PITR or dashboard-managed snapshot restore on the Free plan (see
  above) — the only restore path is replaying a manual dump: `psql
  "$DATABASE_CONNECTION_STRING" -f backup.sql`, and only if one was taken
  before the incident.

**2026-09-16 — re-confirmed live, unchanged, and a restore test could not
be performed (read-only check):** checked all three tabs of Supabase's
Database → Backups page directly (`Scheduled backups`, `Point in time`,
`Restore to new project`) — all three still say Free plan, all gated
behind a Pro-plan upgrade, exactly as documented above. There is
currently no scheduled backup, no PITR snapshot, and no restore point
that exists anywhere in Supabase for this project to restore *from* — so
"backup restore tested" (checklist item 9) cannot mean "restored a real
Supabase backup," because none exists. The only thing testable today is
the manual path above: take a `pg_dump`, then replay it with `psql`
against a scratch database (never against production) to confirm the
dump is actually restorable. That manual dry run has not been performed
and is the only form this test can currently take without first
upgrading to Supabase Pro.

### What's redeployable from git instead of restored from Supabase

Community customs/config data (`customs/*.json`, loaded at runtime by
`backend/customs.py`) is version-controlled in git, not stored only in
Supabase — the source files survive independent of any Supabase incident.
But the `community_knowledge` Supabase table that `backend/rag.py`'s
`_retrieve_community_knowledge()` queries at request time (called from
`backend/ask_pipeline.py`) is a **derived copy** of that JSON, produced by
`scripts/migrate_customs_to_supabase.py`.

Whether that script can safely re-seed `community_knowledge` after a
Supabase restore/loss — checked by reading the script rather than assumed:
**yes, re-running it is safe and will not create duplicate rows.** Every
row's `id` is a deterministic `sha256` of
`community_name|topic|halakhic_source`
(`scripts/migrate_customs_to_supabase.py:39-41`), and the write path
(`:240`) is `client.table(table_name).upsert(batch, on_conflict="id")` —
the same input JSON always produces the same row IDs, so re-running against
an empty or partially-restored table converges to the same end state
instead of appending duplicates. One precision worth knowing: the script
only upserts rows still present in the *current* `customs/*.json` files —
it never deletes rows for topics that existed in an old Supabase snapshot
but have since been removed or renamed on disk, so a restore-then-re-migrate
can leave orphaned rows the JSON no longer produces (harmless for retrieval
quality — orphans just add stale candidates — but worth knowing if
`community_knowledge` needs to exactly mirror `customs/*.json`).

To re-seed after a table loss: `python3
scripts/migrate_customs_to_supabase.py` (needs `SUPABASE_URL` +
`SUPABASE_SECRET_KEY` — the secret key specifically, per
`resolve_supabase_config()` at `scripts/migrate_customs_to_supabase.py:201-216`
— not the publishable key, since upserts require it). Run with `--dry-run`
first to confirm the parsed row count and sample output before writing.

**Status: confirmed 2026-09-01 — Free plan, no PITR, no automated backups
of any kind.** This is a real, currently-unaddressed gap: a Supabase
incident with no manual `pg_dump` taken recently means total data loss,
no partial recovery. Recommended next step: a scheduled manual backup —
this repo's "no warmers/keepalive/scheduled-request" rule
argues against adding a new Vercel cron purely for this, so it should run
off-platform (e.g. a local or CI-hosted cron invoking `supabase db dump`
or `pg_dump`) rather than as a new Vercel Cron entry. Not yet built —
tracked here as an open item.

## Incident response

**Roles:** solo-operated project, one person holds every role.
`docs/PRIVACY_OPERATIONS.md` §6 defines roles and the 72-hour GDPR
Art. 33 timeline for *data breaches* specifically — this section is the
general-purpose version for incidents that aren't necessarily a breach (an
outage, a broken deploy, a cost spike, a user-facing bug). If a general
incident turns out to involve exposed user data, switch to
`docs/PRIVACY_OPERATIONS.md` §6's process at the point that becomes clear,
rather than running two procedures in parallel.

**Detection sources:**

- Sentry (once `SENTRY_DSN` is set — see Observability above) — error
  alerts and, once configured, the uptime-monitor alerts from the section
  above.
- Vercel dashboard — deployment failures, function logs, runtime errors
  (Runtime Logs tab).
- Direct reports — a user, via whatever contact path is live; a
  researcher, via `docs/SECURITY.md`'s "Reporting a vulnerability" intake.

**Triage:**

1. Confirm the report is real and reproduce it if possible — don't act on
   a single unverified report as if it's confirmed.
2. Classify severity: down for all users (outage), down for some
   (partial), or a correctness/quality bug with no availability impact.
   Severity determines whether rollback is the right first move.
3. If a specific bad deploy is the likely cause, roll back first (see
   Rollback procedure below) and diagnose after — restoring service
   outranks root-causing while users are affected.

**Communication:** this solo-operated project has no status page or
user-facing incident channel today. For anything user-visible and ongoing
beyond a short window, the honest move is a note wherever users would look
first (in-app banner, if one exists, or direct outreach to known-affected
users) rather than silence — that tooling doesn't exist yet in this repo;
treat it as a gap to close, not something already covered.

**Postmortem:** for anything beyond a trivial fix, write down what
happened, the root cause, and what changes (code, process, monitoring)
prevent recurrence. Add it as a dated entry under a new `## Incident log`
subsection in this file (or inline here) so operational history
accumulates in one place instead of being lost to memory.

**Status:** this is a documented procedure, not tooling — no status page,
no paging/on-call system, and no automated incident-ticket creation exist
in this repo. That's an accepted gap for a solo-operated project at this
stage, not an oversight to silently work around.

## Incident history

Dated log, per the Postmortem step above — terse by design (what broke,
root cause, fix commit, date), not a full postmortem. Newest first.

### 2026-08-26 — malformed `RATE_LIMIT_REDIS_URL` crashed app boot

**What broke:** the entire Flask app returned an error on every request in
production, not just rate-limited ones.

**Root cause:** `backend/rate_limit.py`'s `_build_store()` called
`_RedisStore(RATE_LIMIT_REDIS_URL)` unguarded whenever the var was set, with
no handling for an invalid URL. Production's `RATE_LIMIT_REDIS_URL` held an
Upstash REST URL (`https://...`) where a `rediss://` connection string was
expected; `redis.asyncio.Redis.from_url(...)` raised on that scheme at
**import time**, so the exception propagated out of the module import
itself and took the whole app down.

**Fix:** commit `352ccd0` (2026-08-26) — `_build_store()` now wraps the
`_RedisStore(url)` construction in the same graceful in-memory-fallback path
already used for the unset case, logged at CRITICAL since a malformed URL
is a misconfiguration, not an expected absence.

## Rollback procedure (Vercel preview → promote)

**Identify the last-known-good deployment:**

1. Vercel dashboard → project → **Deployments** tab. Each entry lists its
   commit, branch, and status (Ready/Error/Canceled).
2. Find the most recent deployment that was known-good — before the
   change that caused the incident, and ideally one that was itself
   smoke-tested (per the Deploy checklist below) rather than just "the
   previous one."
3. Open that deployment to confirm its commit SHA matches what's expected
   before promoting it.

**Promote it to production:**

- **Dashboard (lead with this path):** on the target deployment's page,
  use the "…" menu → **Promote to Production**. This is the lead path
  here rather than the CLI because `vercel` CLI availability/authentication
  in any given operator environment isn't something this repo can
  guarantee — the dashboard button works from any browser with project
  access, and this repo has no `.vercel/` link checked in either.
- **CLI (if `vercel` is installed and authenticated):** `vercel rollback`
  from the project directory rolls back to the previous deployment;
  `vercel rollback <deployment-url>` targets a specific one. `vercel
  promote <deployment-url>` is the equivalent explicit-promote command.
  Run `vercel link` first if the local checkout isn't already linked to
  the project.

**After rolling back:** treat it as a stopgap, not a fix — the bad
deployment's commit is still on the deploy branch unless reverted there
too. Revert or fix-forward in git so the next push doesn't reintroduce the
same regression, then redeploy normally.

**2026-09-16 — dashboard path confirmed reachable (read-only check, not
executed):** verified live in the Vercel dashboard
(`akivayevdayev-debugs-projects/shelah_app/deployments`) that every past
Ready deployment's "…" menu exposes both **Instant Rollback** and
**Promote** directly, one click away, exactly as this runbook describes —
and the deployment history itself shows this mechanism already gets used
in practice (multiple past entries are themselves labeled "Redeploy of
<sha>"). This confirms the *mechanism* is correctly documented and
reachable. It does not confirm the stronger claim in checklist item 9
("validated on a preview deploy") — no rollback was actually triggered
during this check, deliberately, since promoting/rolling back a real
deployment is a live production action and this pass was read-only recon.
An actual end-to-end validation (roll back a real preview deploy, confirm
traffic serves the older build, roll forward again) is still open and
needs the operator's explicit go-ahead given it touches production.

## Deploy checklist

**Pre-deploy:**

- [ ] `pytest -q` green locally (full suite, 4,700 tests; the 85% coverage
      floor in `pytest.ini` fails the run if it drops). Use the project
      virtualenv (`.venv/bin/python -m pytest`), not the system Python.
- [ ] `npm test` green locally (628 frontend tests).
- [ ] `ruff check .` clean. CI's ruff step (`.github/workflows/ci.yml`)
      is blocking, so a finding fails the build, but running it locally
      first saves the round trip.
- [ ] Any new SQL migration (`scripts/*.sql`, `scripts/sql/*.sql`) has
      been applied to the target Supabase project. There is no
      migration-runner in this repo — migrations are applied manually via
      the Supabase SQL editor or `psql`, so a migration merged alongside
      code that depends on it must be applied *before* that deploy goes
      live, or the deploy will error against a schema that doesn't exist
      yet.
- [ ] Required env vars are set in the Vercel project (production
      environment) for anything the change depends on — cross-check
      against this file's Observability section (`SENTRY_DSN`,
      `DAILY_BUDGET_USD`, `CRON_SECRET`) and the Vercel spend-guardrails
      section above for what's already expected to be set.

**Deploy:**

- Normal path: push to the deploy branch → CI
  (`.github/workflows/ci.yml`) runs tests/lint/security scans → Vercel's
  git integration auto-deploys on a successful push (preview deployment
  for non-production branches, production deployment per the project's
  Vercel Git configuration).
- Manual path: promote a specific deployment from the dashboard (see
  Rollback procedure above) — used to re-promote a known-good build
  rather than ship new code.

**Post-deploy:**

- [ ] Smoke-test `/api/health` (expect `401`, per the Uptime monitoring
      section above — that means "up and correctly protecting this
      route," not "down") or `/api/devtools/heartbeat` (expect `200`) to
      confirm the process is actually serving requests.
- [ ] Watch Sentry (if `SENTRY_DSN` is set) for at least 15 minutes after
      the deploy completes, watching for an error-rate spike, before
      considering the deploy final.
- [ ] Spot-check the actual feature the deploy changed in the live app —
      a healthy health-check endpoint only confirms the process is up,
      not that the change works.

## Rate limiting & cost-ceiling status

Rate limiting is a single Starlette middleware,
`backend/rate_limit.py`'s `RateLimitMiddleware`, registered once on
`asgi.py`'s `fastapi_app`. It covers every native FastAPI route and every
Flask route reached through the `WSGIMiddleware` mount — Flask-Limiter and
the old independent in-process limiter in `asgi.py` are both removed, so
there is one policy and one counter store.

1. **Storage backend is per-instance unless configured.**
   `RATE_LIMIT_REDIS_URL` (`backend/rate_limit.py`) is unset by default,
   which falls back to an in-process store — logged loudly at startup. On
   Vercel Fluid, multiple concurrent instances each keep their own
   counters in that mode, so the effective ceiling is closer to
   `configured_limit × live_instances` than the configured number, and it
   resets on every cold start/deploy. Production points
   `RATE_LIMIT_REDIS_URL` at an Upstash Redis `rediss://…` store
   (confirmed live 2026-08-26 after the incident in the history above), so
   counters are shared across instances there.
2. **Policy is one table, not two hand-kept literals.**
   `backend/rate_limit.py`'s `_POLICIES` dict is the single source of
   truth for every route class's window/limit/fail-open behavior —
   `/api/stack/health`'s `security.policy` field reports it directly, so
   an operator reading that endpoint sees the real configuration instead
   of stale Flask-Limiter-only numbers.

**Cost ceilings (separate concern from rate limiting):** this file's
Observability section above already documents the "Daily budget alert"
(`check_daily_budget_and_alert()`, opt-in via `DAILY_BUDGET_USD`, wired to
the `0 13 * * *` cron) — see that subsection rather than duplicating it
here. The Vercel spend-guardrails section at the top of this file documents
the actual hard spend ceilings (Vercel Spend Management cap,
Anthropic/Google provider-side budget caps) — those, not the rate limiter,
are the real backstop against a runaway bill.

**Status (2026-10-02):** shared-store rate limiting is live in production;
the global cost breaker and the edge WAF rate-limit rule sit in front of
it ([`docs/SECURITY.md`](SECURITY.md) §5 and §7). Local development and CI
use the in-process fallback by design.

---

*This runbook documents its own configuration status inline, section by
section; the dated "confirmed" notes are the last time each fact was
verified.*
