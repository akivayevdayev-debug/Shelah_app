# Vercel cost optimization (plan.md §14, Prompt 28)

Status of every §14 sub-item, the cache-tier implementation, and the measurements still owed by the operator. See `.agents/ENGINEERING_RULES.md`'s "Cost & caching" section for the two rules this document backs, and `docs/SECURITY.md` §7 for the WAF layer that sits in front of all of this and is the actual largest invocation-avoidance lever (WAF-mitigated traffic costs nothing on any of the four meters below).

## 1. The billing model (plan.md §14.1)

Four independent meters. **Provisioned Memory is the one that dominates Sh'elah**, not Active CPU:

| Meter | What it measures | Why it matters here |
|---|---|---|
| Active CPU | CPU-milliseconds actually executing; **pauses during I/O** | Small for `/ask` — it's I/O-bound (waiting on Gemini/Claude/Sefaria), not compute-bound |
| Provisioned Memory | Memory × wall-clock instance lifetime, bills **through** I/O waits | The dominant meter here — a 5–30s `/ask` bills its full wall clock at instance memory size regardless of how little CPU it used |
| Invocations | Every request that reaches a function; edge-cache hits excluded | Zero on a CDN hit — this is why §2 below is the largest single lever |
| Fast Origin Transfer | Bytes between CDN and function, both directions; zero on a CDN hit | Same lever as Invocations — cache what's cacheable and both fall together |

**Consequence:** shortening `/ask`'s wall-clock latency is a direct cost reduction (memory bills per second of lifetime), and anything that blocks the FastAPI event loop forces Vercel Fluid to spin up additional instances for concurrent traffic — each with its own Provisioned Memory bill. This is why "no blocking I/O on the event loop" (`.agents/ENGINEERING_RULES.md`) is a billing rule, not just a correctness one.

## 2. Config corrections (plan.md §14.2)

**Done in Prompt 27** (commit `06ae20f`): `vercel.json`'s invalid `"memory": 1024` removed, `"regions": ["iad1"]` pinned (one of the three cheapest regions), `maxDuration: 90` kept (already justified against `/ask`'s p99 + fallback headroom), `ignoreCommand` added to skip builds for doc-only commits. `"fluid": true` deliberately **not** added — no dashboard check was performed to confirm current Fluid state, per the plan's explicit instruction not to flip it blindly.

**Spend guardrails (§14.2.2):** Vercel dashboard Spend Management cap + Usage Notification thresholds. **This is a dashboard action, not code** — see `docs/RUNBOOKS.md`'s "Spend Management hard cap" and "Usage Notification thresholds" sections for the specific values to enter; not duplicated here to avoid two documents drifting on the same numbers.

## 3. The CDN cache-tier table (plan.md §14.3 — this prompt's main body of work)

**Implemented.** `backend/cache_policy.py` is now the single source of truth for `Cache-Control` classification, consulted from both `app.py`'s Flask `after_request` hook (routes reached through the WSGI mount) and `asgi.py`'s `request_id_middleware` (the two native FastAPI routes, `POST /ask` and `GET /api/async/health`, which previously shipped with **no** `Cache-Control` header at all — see the regression test `tests/test_cache_policy.py::test_native_route_previously_had_no_cache_control_now_gets_private`).

| Tier | Header | Routes |
|---|---|---|
| Immutable | `public, s-maxage=86400, stale-while-revalidate=604800` | `/api/library/index`, `/api/library/leaf-refs`, `/api/library/popular`, `/api/texts-index`, `/api/text/*`, `/api/prayer/*`, `/api/siddur/full/*`, `/api/library/category/*` |
| Deterministic-by-date | `public, s-maxage=3600, stale-while-revalidate=86400` | `/api/zmanim`, `/api/zmanim/month`, `/api/daily-study`, `/api/holidays`, `/api/parasha` |
| Corpus-derived | `public, s-maxage=3600, stale-while-revalidate=86400` | `/api/communities/list`, `/api/communities`, `/api/prayers/list`, `/api/word/meaning`, `/api/library/search`, `/api/search/suggest`, `/api/geocode`, `/api/community/*` |
| Private / never cache | `private, no-store` | `/ask`, `/set_location`, every other `/api/*` route (fail-safe default — an unclassified route is never accidentally made public) |

**The cross-user cache-leak this table had to design around:** `GET /api/zmanim`, `/api/zmanim/month`, and `/api/holidays`' last-resort fallback branch all fall back to `get_engine()` (`app.py`) when `lat`/`lon` query params are absent, which reads `session['lat']`/`session['lon']` — i.e. those specific responses are **not** a pure function of the URL, and caching them under the table above would leak one user's session-derived location to whichever user's request next hit the CDN's cached entry for that URL. Fixed with a request-scoped escape hatch: the three call sites (`backend/routes_calendar.py`) set `flask.g.cache_tier_force_private = True` before falling into the session-dependent branch, and `apply_response_cache_policy()` checks that flag before consulting the tier table. Every route that stays in a public tier was verified not to have this property (e.g. `/api/daily-study` was checked against `ShelahEngine.get_daily_learning()`'s actual implementation, which ignores lat/lon entirely).

**`X-Deploy-Hash` header:** added on every non-private cacheable response, sourced from the existing `SENTRY_RELEASE` constant (`app.py`, itself `VERCEL_GIT_COMMIT_SHA`) rather than inventing a second deploy-identifier constant. **Verified 2026-08-23 (plan.md §33.2, Prompt 45):** Vercel's CDN doesn't auto-purge on deploy in the sense of an active delete — instead its cache key includes the unique deployment URL, so each deployment gets a distinct cache namespace and a promoted deployment's traffic naturally misses the previous deployment's (now-orphaned) entries rather than serving them stale. The practical guarantee this codebase relies on (a fresh deploy won't keep serving stale cached content) holds, but `X-Deploy-Hash` itself plays no role in that mechanism — Vercel doesn't key its cache on custom response headers. It remains **diagnostic-only**: useful for matching a served response to the deploy that produced it, not for cache invalidation.

**Static assets (§14.3.2):** not re-verified in this pass — plan.md's §11 rewrite rules already exclude `static/`, `favicon.ico`, `manifest.webmanifest`, `service-worker.js` from the function route. Confirming this on a live preview deploy (checking response headers show no function involvement) is a deploy-time verification step, not something checkable from a headless session — flagged as an open item below.

**Client-side invocation audit (§14.3.3):** done. `grep`-swept every `fetch(...)` and `setInterval(...)` call across `static/js/*.js` and `templates/index.html`. Found exactly one polling interval — `templates/index.html`'s `openDevtoolsInspector()`, which polls `/api/devtools/heartbeat` every 15s — and it is correctly gated: only starts when the hidden devtools-inspector panel (Alt+Shift+I) is explicitly opened, and `clearInterval`'d in `closeDevtoolsInspector()`. No unconditional polling, no re-fetch-on-interval pattern, and no duplicate-fetch-on-load pattern was found running for ordinary visitors. No fix was needed here.

## 4. Fast Origin Transfer (plan.md §14.5)

**Verified, no new code needed.** `_compact_ai_sources` (trims `sources[].lines` down from full arrays) is applied on all three `/ask` return paths in `asgi.py`: the strict-mode block, the successful-synthesis path, the AI-failure fallback path, and now also the new breaker-paused path (`_ask_async_breaker_paused_payload`, Prompt 29b). No debug/echo field was found shipping to production in the `/ask` response shape.

## 5. Invocation reduction & the "stay inactive" rule (plan.md §14.6)

- **No warmers:** confirmed none exist (§14.3.3's audit above covers this — no code path pings a route on an interval "to keep it warm"). Codified as a standing rule in `.agents/ENGINEERING_RULES.md`.
- **Uptime monitoring reconciliation:** already fully documented in `docs/RUNBOOKS.md` (5-minute interval, static-asset primary check + low-frequency `/api/health` secondary check) from earlier work — not duplicated here.
- **`/api/health` (`/api/stack/health`) cheapness:** verified by reading `backend/routes_devtools.py::stack_health()` directly — no outbound network calls, no Sefaria/AI fan-out; the one Supabase-adjacent call (`bool(_get_supabase_client())`) only checks whether a cached client object exists, it does not make a request. Also auth-gated (`@require_clerk_auth`), so it isn't reachable by anonymous scanners either. No change needed.
- **Background work:** `_store_user_memory_summary`/`_store_ask_history` remain correctly awaited in-request (not converted to fire-and-forget) — converting them would extend Provisioned Memory billing past the response without a matching user benefit, exactly the trap plan.md §14.6 item 3 warns against. Left untouched.

## 6. Measurement, verification & guardrails (plan.md §14.7)

- **Zero-behavior-change proof:** `tests/test_cache_policy.py` asserts exact `Cache-Control` values per route tier (both the WSGI and native-ASGI call sites) and that no user-scoped route is ever `public`. Full suite green (`pytest -q`, 90%+ coverage) after every change in this pass.
- **`.agents/ENGINEERING_RULES.md` updated** with the cache-tier and no-warmers rules (§14.7.5).
- **This document (§14.7.6).**
- **Not completed in this pass — requires the operator/a live deploy, cannot be done from a headless session (same category as Prompt 11/31's precedent for deploy-only steps):**
  - **§14.7.1 baseline capture.** 30 days of Vercel Usage-dashboard figures per meter (Usage → Last 30 days → by Project/Region), plus the cached-vs-uncached request ratio, need to be recorded **before** this change's impact can be measured. Recommend capturing this now if it hasn't been already, then re-measuring 7 and 30 days after this deploy ships.
  - **§14.7.4 preview-deploy verification.** Confirm cacheable routes return the expected headers and show CDN `HIT` on a second request; confirm `/ask` and every authed route still show `no-store`; confirm static assets are edge-served; confirm no cross-user leakage on any newly cached route (fetch as user A, then as user B, compare) — all of this needs a real preview deployment, not just the offline test suite.
  - **§14.3.2 static-asset verification**, noted above.

## 7. Exit criteria status (plan.md §14.9)

- [x] `vercel.json` corrected (Prompt 27).
- [x] Cache-tier table implemented, every classified route header-tested, no user-scoped route publicly cacheable.
- [x] Import-time validation lazy/dev-only (Prompt 27, §14.4.1).
- [x] Client-side polling/duplicate-fetch audit complete; monitoring intervals already reconciled in `docs/RUNBOOKS.md`.
- [ ] Spend cap + usage alerts — dashboard action, values documented in `docs/RUNBOOKS.md`, not independently re-verified as actually configured in this pass.
- [x] Full offline suite green; `graphify update .` run.
- [ ] Baseline and post-change usage figures recorded here — **pending operator action, see §6 above.**
- [ ] Zero-user-visible-behavior-change demonstrated on a preview deploy — **pending a live deploy, see §6 above.**

## 8. Explicitly not touched in this pass

- **Search-cardinality/cache-key ordering** (e.g. `/api/library/search`'s query-parameter order affecting cache-key uniqueness at the CDN) — a hit-rate optimization, not a correctness issue; deliberately deprioritized.

Correction: an earlier draft of this section listed AI-provider (Gemini/Claude) circuit breakers as still unwired, citing plan.md §14.4.4/§24.2/§24.5. Those sections were accurate as of 2026-08-19 but Prompt 39/§26.1 shipped the fix the next day (2026-08-20) — `backend/claude.py` now consults `health.is_healthy()`/`record_success`/`record_failure` at all three `/ask` provider entry points. See plan.md §33.3.
